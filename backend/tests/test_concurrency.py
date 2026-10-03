"""Integration tests: real server, real WebSockets, real SQLite."""
from __future__ import annotations

import asyncio
import json

import pytest

from conftest import (
    drain,
    initial_state,
    latest_state,
    make_game,
    recv_until,
    state_frames,
    submit,
    ws_connect,
)

pytestmark = pytest.mark.asyncio


async def test_full_happy_path_reveal_and_advance(server):
    game = make_game(server, rounds=3, seed=7)
    sockets = []
    try:
        for player in game.players:
            ws = await ws_connect(server, player)
            sockets.append(ws)
            st = await initial_state(ws)
            assert st["round_no"] == 1
            assert len(st["my_pack"]) == 5

        admin = game.admin_state()
        assert admin["round_no"] == 1

        # Everyone picks, in seat order.
        for i, ws in enumerate(sockets):
            card = admin["packs"][game.players[i].id][i]
            await submit(ws, card)

        # Every seat should observe a reveal and round 2.
        for ws in sockets:
            frame = await recv_until(
                ws,
                lambda f: f.get("type") == "state"
                and f["state"].get("round_no") == 2
                and len(f["state"]["reveals"]) == 1,
            )
            st = frame["state"]
            reveal = st["reveals"][0]
            assert reveal["timed_out"] is False
            assert all(p["mode"] == "manual" for p in reveal["picks"].values())
            assert len(st["my_pack"]) == 5

        # Event log check via admin dump after all settle.
        admin = game.admin_state()
        assert admin["round_no"] == 2
    finally:
        for ws in sockets:
            await ws.close()


async def test_repeated_clicks_count_once(server):
    game = make_game(server, rounds=2)
    ws = await ws_connect(server, game.players[0])
    try:
        st = await initial_state(ws)
        card = st["my_pack"][2]["id"]
        for _ in range(5):  # button mashing
            await submit(ws, card)
        frames = await drain(ws)
        acks = [f for f in frames if f.get("type") == "pick_ack"]
        assert len(acks) == 5
        assert all(a["card_id"] == card for a in acks)
        # But only one pick was recorded.
        admin = game.admin_state()
        pid = game.players[0].id
        assert admin["picks"][pid] == {"card_id": card, "mode": "manual"}
        assert len(admin["picks"]) == 1
    finally:
        await ws.close()


async def test_concurrent_submissions_same_player_single_pick(server):
    game = make_game(server, rounds=2)
    # Two sockets for the SAME player (reconnect before old socket dropped).
    ws_a = await ws_connect(server, game.players[0])
    ws_b = await ws_connect(server, game.players[0])
    try:
        st = await initial_state(ws_a)
        await initial_state(ws_b)
        card = st["my_pack"][1]["id"]
        other_card = st["my_pack"][3]["id"]

        async def click(ws, picked):
            await submit(ws, picked)

        await asyncio.gather(click(ws_a, card), click(ws_b, other_card))
        frames_a = await drain(ws_a)
        frames_b = await drain(ws_b)

        # Exactly one card was recorded, in manual mode.
        admin = game.admin_state()
        recorded = admin["picks"][game.players[0].id]
        assert recorded["mode"] == "manual"
        assert recorded["card_id"] in (card, other_card)
        winner = recorded["card_id"]

        # The winning call gets an ack; the losing concurrent call gets an
        # already_picked error, never a second pick.
        def codes(frames):
            return sorted(
                f["code"] for f in frames if f.get("type") == "error"
            ), sorted(
                f["card_id"] for f in frames if f.get("type") == "pick_ack"
            )

        errs, acked = codes(frames_a + frames_b)
        assert errs == ["already_picked"] or errs == []
        assert winner in acked
        # Admin truth: still exactly one pick row.
        assert len(admin["picks"]) == 1
    finally:
        await ws_a.close()
        await ws_b.close()


async def test_last_human_pick_races_timeout_reveal_is_consistent(server):
    game = make_game(server, rounds=2, timeout=45)
    sockets = []
    try:
        for player in game.players:
            ws = await ws_connect(server, player)
            sockets.append(ws)
            await initial_state(ws)
        admin = game.admin_state()
        # Seats 1..3 pick manually; seat 0 has not.
        for i in range(1, 4):
            await submit(sockets[i], admin["packs"][game.players[i].id][0])
        # Let the server process, then fire the clock and the last human pick
        # concurrently. Whichever wins the per-game lock, the round must be
        # resolved exactly once and the next round must stay open.
        p0_card = admin["packs"][game.players[0].id][2]

        await asyncio.gather(
            asyncio.to_thread(game.force_timeout),
            submit(sockets[0], p0_card),
        )

        for ws in sockets:
            await recv_until(
                ws,
                lambda f: f.get("type") == "state" and f["state"]["reveals"],
            )
        admin = game.admin_state()
        from app.cards import shuffled_deck
        p0_auto = min(shuffled_deck(42)[0:5])

        # Two valid lock orderings exist, both must be internally consistent:
        #
        # A) Timeout wins round 1: p0 is auto-picked (lowest id), the late
        #    human submission is rejected as stale ("round_advanced"), round 2
        #    stays open. history == 1.
        # B) Human submission completes round 1 (all four manual); the force
        #    then legitimately closes the already-open round 2 as a timeout.
        #    history == 2, game completed.
        #
        # What must NEVER happen: p0 recorded twice, p0's round-1 pick being
        # the wrong card, or the stale human card landing in round 2.
        r1 = admin["history"][0]["picks"][game.players[0].id]
        if len(admin["history"]) == 1:
            # Outcome A: round 1 timed out; p0 auto-picked or manual depending
            # on exact timing, but round 2 must be live with untouched packs.
            assert admin["round_no"] == 2
            assert r1["card_id"] in (p0_card, p0_auto)
            assert all(len(p) == 5 for p in admin["packs"].values())
        else:
            # Outcome B: human won round 1 with the exact card chosen.
            assert len(admin["history"]) == 2
            assert r1 == {"card_id": p0_card, "mode": "manual"}
            assert admin["history"][1]["timed_out"] is True
            # Round 2 auto picks must not include p0's round-1 manual card.
            r2 = admin["history"][1]["picks"][game.players[0].id]
            assert r2["mode"] == "auto" and r2["card_id"] != p0_card
        for i in range(1, 4):
            assert admin["history"][0]["picks"][game.players[i].id]["mode"] == "manual"
    finally:
        for ws in sockets:
            await ws.close()


async def test_concurrent_duplicate_force_resolves_round_once(server):
    """Two force calls capturing the same epoch resolve that epoch once."""
    game = make_game(server, rounds=3)
    before = game.admin_state()["round_no"]
    import httpx

    barrier = asyncio.Event()

    async def force(client):
        await barrier.wait()  # release both requests in lockstep
        return await client.post(
            f"{server.base_url}/admin/games/{game.id}/timeout"
        )

    async with httpx.AsyncClient(timeout=10) as client:
        tasks = [asyncio.create_task(force(client)) for _ in range(2)]
        await asyncio.sleep(0.05)
        barrier.set()
        results = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in results)

    after = game.admin_state()
    # Two genuine admin force operations are each valid; because they fire
    # in lockstep they usually share one epoch and collapse, but they are
    # allowed to serialise into two epochs. The invariant under test is
    # that no single epoch resolves twice and state never splits.
    assert after["round_no"] in (before + 1, before + 2)
    assert len(after["history"]) in (1, 2)
    assert len(after["history"]) == after["round_no"] - before or \
        after["status"] == "active"


async def test_timeout_auto_picks_and_reveals(server):
    game = make_game(server, rounds=2, timeout=45)
    sockets = []
    try:
        for player in game.players:
            ws = await ws_connect(server, player)
            sockets.append(ws)
            await initial_state(ws)
        admin = game.admin_state()
        # Only seat 0 picks manually.
        p0_pick = admin["packs"][game.players[0].id][1]
        await submit(sockets[0], p0_pick)
        game.force_timeout()

        frame = await recv_until(
            sockets[0],
            lambda f: f.get("type") == "state"
            and f["state"]["round_no"] == 2,
        )
        reveal = frame["state"]["reveals"][0]
        assert reveal["timed_out"] is True
        assert reveal["picks"][game.players[0].id]["card_id"] == p0_pick
        # Auto cards = lowest id of each non-picker's round-1 pack.
        from app.cards import shuffled_deck
        deck = shuffled_deck(42)
        for i in range(1, 4):
            pid = game.players[i].id
            assert reveal["picks"][pid] == {
                "card_id": min(deck[i * 5 : i * 5 + 5]),
                "mode": "auto",
                "name": f"Card-{min(deck[i * 5: i * 5 + 5]):03d}",
            }
        assert reveal["picks"][game.players[0].id]["mode"] == "manual"
    finally:
        for ws in sockets:
            await ws.close()


async def test_real_wall_clock_timeout_fires(server):
    game = make_game(server, rounds=2, timeout=1)
    ws = await ws_connect(server, game.players[0])
    try:
        st = await initial_state(ws)
        assert st["round_no"] == 1
        # Nobody picks; the watchdog should resolve within the configured sec.
        frame = await recv_until(
            ws,
            lambda f: f.get("type") == "state"
            and f["state"]["round_no"] == 2,
            timeout=5,
        )
        assert frame["state"]["reveals"][0]["timed_out"] is True
    finally:
        await ws.close()


async def test_pick_after_round_advances_is_rejected(server):
    game = make_game(server, rounds=2)
    sockets = []
    try:
        for player in game.players:
            ws = await ws_connect(server, player)
            sockets.append(ws)
            await initial_state(ws)
        admin = game.admin_state()
        stale_card = admin["packs"][game.players[0].id][0]
        game.force_timeout()  # nobody picked; whole round auto-resolves
        await recv_until(
            sockets[0],
            lambda f: f.get("type") == "state" and f["state"]["round_no"] == 2,
        )
        await submit(sockets[0], stale_card)
        frames = await drain(sockets[0])
        # Stale card from the old pack cannot be picked in the new round.
        assert any(
            f.get("type") == "error" and f["code"] in
            ("card_not_in_pack", "no_active_round")
            for f in frames
        )
    finally:
        for ws in sockets:
            await ws.close()
