"""Information-hiding boundary tests across player WebSockets."""
from __future__ import annotations

import json

import pytest
import websockets

from conftest import (
    drain,
    initial_state,
    make_game,
    recv_until,
    submit,
    ws_connect,
)

pytestmark = pytest.mark.asyncio


def _card_values(state: dict) -> set[int]:
    """Every integer card identity reachable in a projected client state."""
    ids = {c["id"] for c in state.get("my_pack", [])}
    if state.get("my_pick"):
        ids.add(state["my_pick"]["card_id"])
    for reveal in state.get("reveals", []):
        ids.update(p["card_id"] for p in reveal["picks"].values())
    for cards in state.get("collections", {}).values():
        ids.update(cards)
    return ids


async def _all_frames(ws, delay=0.1):
    return await drain(ws, delay=delay)


async def test_live_frames_never_leak_other_seats_current_pack(server):
    game = make_game(server, rounds=3, seed=99)
    sockets = []
    captures = {p.id: [] for p in game.players}
    try:
        for player in game.players:
            ws = await ws_connect(server, player)
            sockets.append(ws)
            frame = await initial_state(ws)
            captures[player.id].append(frame)

        # Play 3 rounds with partial picks + forced timeouts; collect every
        # state frame that ever reaches each player.
        for round_no in range(1, 4):
            admin = game.admin_state()
            for i in (0, 2):  # two players submit, two stay pending
                pid = game.players[i].id
                await submit(sockets[i], admin["packs"][pid][0])
            for i, ws in enumerate(sockets):
                frames = await drain(ws)
                captures[game.players[i].id].extend(
                    f["state"] for f in frames if f.get("type") == "state"
                )
            game.force_timeout()
            for i, ws in enumerate(sockets):
                frame = await recv_until(
                    ws,
                    lambda f: f.get("type") == "state"
                    and (
                        f["state"].get("round_no") in (round_no + 1,)
                        or f["state"]["status"] == "completed"
                    ),
                )
                captures[game.players[i].id].append(frame["state"])

        # For every captured frame, reconstruct what the viewer was allowed
        # to see using the frame itself + the deterministic seeded deck.
        from app.cards import shuffled_deck
        deck = shuffled_deck(99)

        for viewer in game.players:
            for st in captures[viewer.id]:
                visible = _card_values(st)
                public = {
                    p["card_id"]
                    for r in st["reveals"] for p in r["picks"].values()
                }
                # Current own pack is the only secret pack this viewer owns.
                own = {c["id"] for c in st["my_pack"]}
                # A visible card must be either public, in own pack/collection,
                # or the viewer's own collection.
                own_collected = set(st["collections"][viewer.id])
                allowed = public | own | own_collected
                leaked = visible - allowed
                assert not leaked, (
                    f"{viewer.name} saw forbidden card ids {sorted(leaked)} "
                    f"in a {st['round_no']} frame"
                )
                # Names too: no other seat's currently-held card name.
                blob = json.dumps(st)
                for card in visible - public - own_collected:
                    # only names in own current pack may appear
                    if card not in own:
                        assert f"Card-{card:03d}" not in blob

            final = captures[viewer.id][-1]
            assert final["status"] == "completed"
            assert final["my_pack"] == []
            assert len(final["reveals"]) == 3
    finally:
        for ws in sockets:
            await ws.close()


async def test_player_cannot_see_others_pending_pack_and_pick(server):
    game = make_game(server, rounds=2, seed=5)
    sockets = []
    try:
        for player in game.players:
            ws = await ws_connect(server, player)
            sockets.append(ws)
            await initial_state(ws)
        admin = game.admin_state()
        # Seat 1 secretly picks their 3rd card; nobody else learns which.
        pid1 = game.players[1].id
        secret = admin["packs"][pid1][2]
        await submit(sockets[1], secret)

        for i in (0, 2, 3):
            frames = await drain(sockets[i])
            assert frames, "seat-lock broadcast expected"
            state = frames[-1]["state"]
            assert state["lock_state"][pid1] == "locked"
            assert secret not in _card_values(state)
            assert f"Card-{secret:03d}" not in json.dumps(frames)
    finally:
        for ws in sockets:
            await ws.close()


async def test_reconnect_snapshot_is_seat_scoped(server):
    game = make_game(server, rounds=3, seed=11)
    ws0 = await ws_connect(server, game.players[0])
    await initial_state(ws0)
    admin = game.admin_state()
    await submit(ws0, admin["packs"][game.players[0].id][0])
    game.force_timeout()
    await ws0.close()

    truth = game.admin_state()
    for viewer in game.players:
        snap = game.snapshot(viewer)
        assert snap["you"] == viewer.id
        visible = _card_values(snap)
        public = {
            p["card_id"] for r in snap["reveals"] for p in r["picks"].values()
        }
        own = set(truth["packs"][viewer.id])
        own_collected = set(snap["collections"][viewer.id])
        allowed = public | own | own_collected
        assert not (visible - allowed)
        assert [c["id"] for c in snap["my_pack"]] == truth["packs"][viewer.id]


async def test_snapshots_of_different_seats_are_disjoint_and_protected(server):
    game = make_game(server, rounds=2)
    http = server.http()
    r = http.get(f"/games/{game.id}", params={"token": "garbage"})
    assert r.status_code == 401
    assert "Card-" not in r.text and "pack" not in r.text

    snap0 = game.snapshot(game.players[0])
    snap1 = game.snapshot(game.players[1])
    own0 = {c["id"] for c in snap0["my_pack"]}
    own1 = {c["id"] for c in snap1["my_pack"]}
    assert own0.isdisjoint(own1)
    assert len(own0) == len(own1) == 5


async def test_bad_token_websocket_closed_without_data(server):
    game = make_game(server, rounds=2)
    port = server.base_url.rsplit(":", 1)[1]
    uri = f"ws://127.0.0.1:{port}/ws/{game.id}?token=forged"
    with pytest.raises(websockets.InvalidStatus) if hasattr(
        websockets, "InvalidStatus"
    ) else pytest.raises(Exception):
        async with websockets.connect(uri) as ws:
            # Rejected during the handshake (HTTP 403 + app close code 4401).
            await ws.recv()


async def test_error_messages_contain_no_card_data(server):
    game = make_game(server, rounds=2)
    ws = await ws_connect(server, game.players[0])
    try:
        await initial_state(ws)
        await ws.send(json.dumps({"type": "submit_pick", "card_id": "banana"}))
        await ws.send(json.dumps({"type": "submit_pick", "card_id": 999999}))
        await ws.send(json.dumps({"type": "what_am_i"}))
        frames = await drain(ws)
        errors = [f for f in frames if f["type"] == "error"]
        codes = {f["code"] for f in errors}
        assert {"invalid_card", "card_not_in_pack", "unknown_message_type"} <= codes
        for f in errors:
            assert set(f.keys()) == {"type", "code"}
    finally:
        await ws.close()


async def test_malformed_json_does_not_kill_socket(server):
    game = make_game(server, rounds=2)
    ws = await ws_connect(server, game.players[0])
    try:
        await initial_state(ws)
        await ws.send("this-is-not-json")
        frames = await drain(ws)
        assert any(
            f.get("type") == "error" and f["code"] == "invalid_message"
            for f in frames
        )
        await ws.send(json.dumps({"type": "ping"}))
        frame = await recv_until(ws, lambda f: f.get("type") == "pong")
        assert frame["type"] == "pong"
    finally:
        await ws.close()
