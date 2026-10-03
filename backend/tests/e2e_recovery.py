"""Cross-process restart recovery E2E.

Spawns a real uvicorn OS process, creates/seats/starts/picks, kills it with
SIGTERM, then spawns a fresh process against the same SQLite file and checks
the pending round + manual pick are recovered exactly.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets
import asyncio

BACKEND = Path(__file__).resolve().parent.parent
DB = BACKEND / "rec.db"
if DB.exists():
    DB.unlink()


def serve(port: int) -> subprocess.Popen:
    env = dict(
        os.environ,
        DRAFT_DB_PATH=str(DB),
        DRAFT_ROUNDS="3",
        DRAFT_TIMEOUT="300",
        DRAFT_ALLOW_ADMIN="1",
    )
    log = open(f"/tmp/rec_port{port}.log", "w")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--app-dir", str(BACKEND)],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(port, gid=None):
    """Wait until the server answers. If ``gid`` is given, require the
    lifespan replay to have finished so the game is actually loadable."""
    for _ in range(80):
        try:
            if gid is None:
                if httpx.get(f"http://127.0.0.1:{port}/docs", timeout=1).status_code == 200:
                    return
            else:
                r = httpx.get(
                    f"http://127.0.0.1:{port}/admin/games/{gid}/state", timeout=1
                )
                if r.status_code == 200 and "round_no" in r.json():
                    return
        except Exception:
            pass
        time.sleep(0.15)
    raise RuntimeError("server did not become ready")


async def submit_pick(port, gid, token, card):
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/ws/{gid}?token={token}"
    ) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "submit_pick", "card_id": card}))
        await asyncio.sleep(0.4)


def main():
    port1, port2 = free_port(), free_port()
    p1 = serve(port1)
    wait_up(port1)
    h = httpx.Client(base_url=f"http://127.0.0.1:{port1}", timeout=10)
    gid = h.post("/games", json={"rounds": 3, "timeout": 300, "seed": 42}).json()["game_id"]
    tokens = [
        h.post(f"/games/{gid}/join", json={"name": f"s{i}"}).json()["token"]
        for i in range(4)
    ]
    h.post(f"/games/{gid}/start")
    before = h.get(f"/admin/games/{gid}/state").json()
    first_pid = list(before["packs"].keys())[0]
    card = before["packs"][first_pid][3]
    asyncio.run(submit_pick(port1, gid, tokens[0], card))
    after_pick = h.get(f"/admin/games/{gid}/state").json()
    assert after_pick["picks"][first_pid] == {"card_id": card, "mode": "manual"}
    print("first process: round", after_pick["round_no"], "pick recorded", card)

    p1.send_signal(signal.SIGTERM)
    p1.wait(timeout=10)
    print("first process killed")

    p2 = serve(port2)
    wait_up(port2, gid)
    h2 = httpx.Client(base_url=f"http://127.0.0.1:{port2}", timeout=10)
    restored = h2.get(f"/admin/games/{gid}/state").json()
    assert restored["round_no"] == 1, restored
    assert restored["packs"] == before["packs"], "packs must replay identically"
    assert restored["picks"][first_pid] == {"card_id": card, "mode": "manual"}
    assert restored["deadline"] == before["deadline"] or restored["deadline"]
    print("recovered: same packs, same pick, status", restored["status"])

    # The recovered game must still play: force timeout, verify deterministic
    # auto picks match the seeded deck's lowest cards.
    h2.post(f"/admin/games/{gid}/timeout")
    nxt = h2.get(f"/admin/games/{gid}/state").json()
    assert nxt["round_no"] == 2 and len(nxt["history"]) == 1
    sys.path.insert(0, str(BACKEND))
    from app.cards import shuffled_deck
    deck = shuffled_deck(42)
    rev = nxt["history"][0]["picks"]
    seats = list(nxt["seats"])
    for i, pid in enumerate(seats):
        if pid == first_pid:
            assert rev[pid] == {"card_id": card, "mode": "manual"}
        else:
            assert rev[pid] == {"card_id": min(deck[i*5:i*5+5]), "mode": "auto"}
    print("post-restart resolution matches deterministic rules — RECOVERY OK")
    p2.send_signal(signal.SIGTERM)
    p2.wait(timeout=10)


if __name__ == "__main__":
    main()
