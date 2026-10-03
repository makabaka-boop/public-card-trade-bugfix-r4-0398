"""Durability: restart replays events and resumes pending rounds."""
from __future__ import annotations

import pytest

from conftest import (
    initial_state,
    make_game,
    recv_until,
    start_server,
    submit,
    ws_connect,
)

pytestmark = pytest.mark.asyncio


async def test_restart_after_partial_picks_resumes_and_resolves(
    server
):
    db_path = server.db_path
    game = make_game(server, rounds=3, timeout=45, seed=42)
    ws0 = await ws_connect(server, game.players[0])
    await initial_state(ws0)
    admin = game.admin_state()
    p0_card = admin["packs"][game.players[0].id][1]
    await submit(ws0, p0_card)
    # Wait for the pick to be durable.
    frames_state = game.admin_state()
    assert game.players[0].id in frames_state["picks"]
    await ws0.close()

    # Restart a brand-new process against the same database file.
    server2 = start_server(db_path)
    try:
        # The pending round must be recovered with the same packs.
        restored = None
        import httpx
        r = httpx.get(
            f"{server2.base_url}/admin/games/{game.id}/state", timeout=5
        )
        assert r.status_code == 200
        restored = r.json()
        assert restored["round_no"] == 1
        assert restored["packs"] == admin["packs"]
        assert restored["picks"][game.players[0].id]["card_id"] == p0_card
        assert restored["picks"][game.players[0].id]["mode"] == "manual"

        # Force the round over the restarted server: missing seats auto-pick,
        # reveal is replayed identically.
        r = httpx.post(
            f"{server2.base_url}/admin/games/{game.id}/timeout", timeout=5
        )
        assert r.status_code == 200
        r = httpx.get(
            f"{server2.base_url}/admin/games/{game.id}/state", timeout=5
        )
        after = r.json()
        assert after["round_no"] == 2
        reveal = after["history"][0]
        assert reveal["timed_out"] is True
        assert reveal["picks"][game.players[0].id] == {
            "card_id": p0_card, "mode": "manual"
        }
        from app.cards import shuffled_deck
        deck = shuffled_deck(42)
        for i in range(1, 4):
            pid = game.players[i].id
            assert reveal["picks"][pid] == {
                "card_id": min(deck[i * 5 : i * 5 + 5]), "mode": "auto"
            }
    finally:
        pass


async def test_restart_replays_final_pick_results(server):
    db_path = server.db_path
    game = make_game(server, rounds=2, timeout=45, seed=42)
    sockets = []
    try:
        for i in range(4):
            ws = await ws_connect(server, game.players[i])
            sockets.append(ws)
            await initial_state(ws)
        admin = game.admin_state()
        for i in range(4):
            await submit(sockets[i], admin["packs"][game.players[i].id][i])
        await recv_until(
            sockets[0],
            lambda f: f.get("type") == "state" and f["state"]["round_no"] == 2,
        )
    finally:
        for ws in sockets:
            await ws.close()

    import httpx
    server2 = start_server(db_path)
    r = httpx.get(
        f"{server2.base_url}/admin/games/{game.id}/state", timeout=5
    )
    assert r.status_code == 200
    state = r.json()
    # Round 1 reveal is part of the replayable, public record.
    reveal = state["history"][0]
    assert reveal["timed_out"] is False
    assert all(p["mode"] == "manual" for p in reveal["picks"].values())
    # Round 2 is live again with five-card packs.
    assert state["round_no"] == 2
    assert all(len(pack) == 5 for pack in state["packs"].values())


async def test_restart_with_expired_deadline_auto_resolves(
    server
):
    db_path = server.db_path
    game = make_game(server, rounds=2, timeout=1, seed=42)
    ws = await ws_connect(server, game.players[0])
    await initial_state(ws)
    await ws.close()
    # Wait long enough that the persisted deadline is in the past when the
    # replacement server starts.
    import asyncio, httpx
    await asyncio.sleep(1.3)

    server2 = start_server(db_path)
    # Startup recovery should have auto-resolved round 1 immediately.
    r = httpx.get(
        f"{server2.base_url}/admin/games/{game.id}/state", timeout=5
    )
    state = r.json()
    assert state["round_no"] == 2
    assert state["history"][0]["timed_out"] is True
    assert all(
        p["mode"] == "auto" for p in state["history"][0]["picks"].values()
    )


async def test_idempotent_pick_survives_reconnect(server):
    game = make_game(server, rounds=2, seed=42)
    ws = await ws_connect(server, game.players[0])
    try:
        st = await initial_state(ws)
        card = st["my_pack"][0]["id"]
        await submit(ws, card)
        await recv_until(
            ws,
            lambda f: f.get("type") == "state"
            and f["state"]["my_pick"] is not None,
        )
    finally:
        await ws.close()

    # Reconnect and send the same pick again.
    ws2 = await ws_connect(server, game.players[0])
    try:
        st = await initial_state(ws2)
        assert st["my_pick"] == {"card_id": card, "mode": "manual"}
        await submit(ws2, card)
        frames = await __import__("conftest").drain(ws2)
        acks = [f for f in frames if f.get("type") == "pick_ack"]
        assert len(acks) == 1 and acks[0]["card_id"] == card
        # Still one recorded pick.
        picks = game.admin_state()["picks"]
        assert len(picks) == 1
    finally:
        await ws2.close()
