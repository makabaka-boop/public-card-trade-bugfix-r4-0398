"""Test infrastructure: real uvicorn server over a temp SQLite database.

Running the actual server (rather than an in-process ASGI client) is what
lets us exercise the requirements around real WebSocket sockets, concurrent
submissions and a server *restart* that replays persisted events.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
import pytest
import uvicorn
import websockets

# Pick a free port before uvicorn starts.
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerHandle:
    def __init__(self, base_url: str, db_path: str):
        self.base_url = base_url
        self.db_path = db_path

    def http(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=10)

    @asynccontextmanager
    async def connect(self, game_id: str, token: str):
        uri = f"ws://127.0.0.1:{self.base_url.rsplit(':',1)[1]}/ws/{game_id}?token={token}"
        async with websockets.connect(uri) as ws:
            yield ws

    def restart(self, db_path: str) -> "ServerHandle":
        return start_server(db_path)


def _run_server(db_path: str, port: int) -> None:
    """Run a fully isolated server in this thread's own event loop.

    Each test server builds its own FastAPI app with its own Settings,
    Repository, Hub and GameService — so a "restart" against the same db
    file really replays events from scratch into a fresh process-like
    environment.
    """
    import asyncio
    import logging

    from app.config import Settings
    from app.main import create_app

    logging.disable(logging.CRITICAL)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    test_settings = Settings(
        db_path=db_path,
        allow_admin=True,
    )
    app = create_app(test_settings)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        ws="websockets",
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())


def start_server(db_path: str) -> ServerHandle:
    port = _free_port()
    thread = threading.Thread(
        target=_run_server, args=(db_path, port), daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            r = httpx.get(base + "/docs", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("test server failed to start")
    h = ServerHandle(base, db_path)
    h._thread = thread  # keep reference alive
    return h


@pytest.fixture
def server(tmp_path):
    db_path = str(tmp_path / "test_draft.db")
    h = start_server(db_path)
    yield h
    # Daemon thread dies with the process; nothing to shut down per test.


# ---- game helpers --------------------------------------------------------

class Player:
    def __init__(self, game_id: str, player_id: str, token: str, seat: int, name: str):
        self.game_id = game_id
        self.id = player_id
        self.token = token
        self.seat = seat
        self.name = name


class Game:
    def __init__(self, server: ServerHandle, gid: str, players: List[Player]):
        self.server = server
        self.id = gid
        self.players = players

    def by_seat(self, seat: int) -> Player:
        return self.players[seat]

    @property
    def p(self) -> List[Player]:
        return self.players

    def admin_state(self) -> Dict[str, Any]:
        r = self.server.http().get(f"/admin/games/{self.id}/state")
        assert r.status_code == 200, r.text
        return r.json()

    def force_timeout(self) -> None:
        r = self.server.http().post(f"/admin/games/{self.id}/timeout")
        assert r.status_code == 200, r.text

    def snapshot(self, player: Player) -> Dict[str, Any]:
        r = self.server.http().get(
            f"/games/{self.id}", params={"token": player.token}
        )
        assert r.status_code == 200, r.text
        return r.json()


def make_game(server: ServerHandle, *, rounds: int = 4, timeout: float = 45.0,
              seed: int = 42, start: bool = True) -> Game:
    http = server.http()
    r = http.post("/games", json={"rounds": rounds, "timeout": timeout, "seed": seed})
    assert r.status_code == 201, r.text
    gid = r.json()["game_id"]
    players: List[Player] = []
    for i in range(4):
        r = http.post(f"/games/{gid}/join", json={"name": f"seat{i}"})
        assert r.status_code == 201, r.text
        body = r.json()
        players.append(Player(gid, body["player_id"], body["token"], body["seat"], f"seat{i}"))
    if start:
        r = http.post(f"/games/{gid}/start")
        assert r.status_code == 200, r.text
    return Game(server, gid, players)


# ---- websocket helpers ---------------------------------------------------

async def recv_until(ws, predicate, timeout: float = 3.0) -> Dict[str, Any]:
    """Read frames until one matches predicate(frame) -> truthy."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise AssertionError("timeout waiting for matching frame")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        frame = json.loads(raw)
        result = predicate(frame)
        if result:
            return frame if result is True else result


async def drain(ws, delay: float = 0.15) -> List[Dict[str, Any]]:
    """Collect everything currently queued on the socket."""
    out: List[Dict[str, Any]] = []
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=delay)
            out.append(json.loads(raw))
    except asyncio.TimeoutError:
        return out


def state_frames(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [f["state"] for f in frames if f.get("type") == "state"]


def latest_state(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    states = state_frames(frames)
    assert states, f"no state frame in {frames}"
    return states[-1]


async def ws_connect(server: ServerHandle, player: Player):
    port = server.base_url.rsplit(":", 1)[1]
    uri = f"ws://127.0.0.1:{port}/ws/{player.game_id}?token={player.token}"
    return await websockets.connect(uri)


async def initial_state(ws) -> Dict[str, Any]:
    return (await recv_until(ws, lambda f: f.get("type") == "state"))["state"]


async def submit(ws, card_id: Any) -> None:
    await ws.send(json.dumps({"type": "submit_pick", "card_id": card_id}))
