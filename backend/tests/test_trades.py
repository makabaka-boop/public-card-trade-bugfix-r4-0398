"""Public-card trades: revision-bound confirmations, atomic commit, replay.

Covers the trade faults end to end:

* a confirmation approves exactly one revision — editing the content wipes
  earlier confirmations and stale-revision confirms are rejected;
* cards move exactly once, when EVERY participant confirmed the same content
  (never at individual confirms, never on cancel, never twice on duplicates);
* two trades competing for one public card cannot both commit;
* unrevealed hand cards can neither be offered nor leak into trade messages;
* live collections, reconnect snapshots and a cold replay agree, while the
  historical reveal record keeps its original pickers.
"""
from __future__ import annotations

import pytest

from app import trades
from app.cards import shuffled_deck
from app.engine import (
    DraftState,
    apply_event as engine_apply,
    open_round_event,
    replay,
    resolve_round,
)

from conftest import (
    drain,
    initial_state,
    make_game,
    recv_until,
    start_server,
    submit,
    ws_connect,
)

SEATS = ["p0", "p1", "p2", "p3"]


# ---- pure state-machine helpers -------------------------------------------

def make_revealed_state(rounds: int = 2, seed: int = 42):
    """A live draft state with ``rounds`` revealed rounds plus its event log."""
    state = DraftState()
    log = []
    created = {
        "type": "game_created",
        "game_id": "g",
        "seats": SEATS,
        "num_rounds": 4,
        "pack_size": 5,
        "timeout": 45.0,
        "deck": shuffled_deck(seed),
    }
    engine_apply(state, created)
    log.append(created)
    opened = open_round_event(state, 100.0)
    engine_apply(state, opened)
    log.append(opened)
    for r in range(rounds):
        log.extend(resolve_round(state, 200.0 + r, timed_out=False))
    return state, log


def run(state, log, actor, action, trade_id, revision=None, offers=None):
    """Issue a trade command and apply+persist the resulting events."""
    events = trades.command(state, actor, action, trade_id, revision, offers)
    for event in events:
        engine_apply(state, event)
    log.extend(events)
    return events


def assert_replay_matches(state, log):
    rebuilt = replay(log)
    assert trades.collections(rebuilt) == trades.collections(state)
    assert trades.project(rebuilt) == trades.project(state)


def assert_conserved(collections, expected_cards):
    flat = [c for cards in collections.values() for c in cards]
    assert len(flat) == len(set(flat)), f"card in two collections: {sorted(flat)}"
    assert sorted(flat) == sorted(expected_cards)


# ---- pure state-machine tests ----------------------------------------------

def test_confirmation_binds_to_the_revision_it_was_made_on():
    state, log = make_revealed_state()
    owned = trades.collections(state)
    a0, b0, b1 = owned["p0"][0], owned["p1"][0], owned["p1"][1]
    run(state, log, "p0", "create", "t1", offers=[
        {"from": "p0", "to": "p1", "card_id": a0},
        {"from": "p1", "to": "p0", "card_id": b0},
    ])
    run(state, log, "p1", "confirm", "t1", revision=1)

    # The author rewrites the content: p1's earlier confirmation approved the
    # OLD content and must not carry over.
    run(state, log, "p0", "edit", "t1", revision=1, offers=[
        {"from": "p0", "to": "p1", "card_id": a0},
        {"from": "p1", "to": "p0", "card_id": b1},
    ])
    assert state.trades["t1"]["revision"] == 2
    assert state.trades["t1"]["confirmed"] == []

    # A confirm quoting the superseded revision is rejected outright.
    with pytest.raises(trades.TradeError) as err:
        trades.command(state, "p1", "confirm", "t1", 1)
    assert err.value.code == "revision_mismatch"

    # p0 confirms the new content: still not committed — p1 never approved it.
    run(state, log, "p0", "confirm", "t1", revision=2)
    assert state.trades["t1"]["status"] == "open"
    assert trades.collections(state)["p0"].count(a0) == 1  # nothing moved yet

    run(state, log, "p1", "confirm", "t1", revision=2)
    assert state.trades["t1"]["status"] == "committed"
    now = trades.collections(state)
    assert a0 in now["p1"] and b1 in now["p0"] and b0 in now["p1"]
    assert_conserved(now, [c for cards in owned.values() for c in cards])
    assert_replay_matches(state, log)


def test_cards_move_only_on_full_commit_and_cancel_keeps_ownership():
    state, log = make_revealed_state()
    before = trades.collections(state)
    a0, b0 = before["p0"][0], before["p1"][0]
    run(state, log, "p0", "create", "t1", offers=[
        {"from": "p0", "to": "p1", "card_id": a0},
        {"from": "p1", "to": "p0", "card_id": b0},
    ])
    run(state, log, "p0", "confirm", "t1", revision=1)
    # Half confirmed: nothing may move.
    assert trades.collections(state) == before

    # Cancelling returns nothing and removes nothing: ownership untouched.
    run(state, log, "p0", "cancel", "t1", revision=1)
    assert trades.collections(state) == before
    assert state.trades["t1"]["status"] == "cancelled"

    # A late confirm against the cancelled trade is a harmless no-op.
    assert trades.command(state, "p1", "confirm", "t1", 1) == []
    assert trades.collections(state) == before
    assert_replay_matches(state, log)


def test_competing_trades_only_the_fully_owned_one_commits():
    state, log = make_revealed_state()
    owned = trades.collections(state)
    x, y, z = owned["p0"][0], owned["p1"][0], owned["p2"][0]
    run(state, log, "p0", "create", "t1", offers=[
        {"from": "p0", "to": "p1", "card_id": x},
        {"from": "p1", "to": "p0", "card_id": y},
    ])
    # Same public card x promised to two different players.
    run(state, log, "p0", "create", "t2", offers=[
        {"from": "p0", "to": "p2", "card_id": x},
        {"from": "p2", "to": "p0", "card_id": z},
    ])
    run(state, log, "p0", "confirm", "t1", revision=1)
    run(state, log, "p1", "confirm", "t1", revision=1)
    assert state.trades["t1"]["status"] == "committed"

    run(state, log, "p2", "confirm", "t2", revision=1)
    # t2 can never commit: x is no longer p0's to give.
    with pytest.raises(trades.TradeError) as err:
        trades.command(state, "p0", "confirm", "t2", 1)
    assert err.value.code == "card_unavailable"
    assert state.trades["t2"]["status"] == "open"

    now = trades.collections(state)
    assert x in now["p1"] and x not in now["p0"] and x not in now["p2"]
    assert z in now["p2"]
    assert_conserved(now, [c for cards in owned.values() for c in cards])
    assert_replay_matches(state, log)


def test_duplicate_confirm_never_double_moves():
    state, log = make_revealed_state()
    owned = trades.collections(state)
    a0, b0 = owned["p0"][0], owned["p1"][0]
    run(state, log, "p0", "create", "t1", offers=[
        {"from": "p0", "to": "p1", "card_id": a0},
        {"from": "p1", "to": "p0", "card_id": b0},
    ])
    run(state, log, "p0", "confirm", "t1", revision=1)
    assert trades.command(state, "p0", "confirm", "t1", 1) == []  # repeat
    run(state, log, "p1", "confirm", "t1", revision=1)
    assert state.trades["t1"]["status"] == "committed"
    # Repeats after the commit are no-ops as well.
    assert trades.command(state, "p1", "confirm", "t1", 1) == []
    now = trades.collections(state)
    assert now["p1"].count(a0) == 1 and now["p0"].count(b0) == 1
    assert_conserved(now, [c for cards in owned.values() for c in cards])
    assert_replay_matches(state, log)


def test_secret_hand_cards_cannot_be_traded():
    state, log = make_revealed_state(rounds=1)
    secret = state.packs["p0"][0]  # current-round hand card, never revealed
    assert secret not in trades.collections(state)["p0"]
    with pytest.raises(trades.TradeError) as err:
        trades.command(state, "p0", "create", "t1", offers=[
            {"from": "p0", "to": "p1", "card_id": secret},
            {"from": "p1", "to": "p0",
             "card_id": trades.collections(state)["p1"][0]},
        ])
    assert err.value.code == "card_unavailable"
    assert state.trades == {}  # no trade message was ever produced


def test_only_author_may_edit_or_cancel_and_only_the_current_revision():
    state, log = make_revealed_state()
    owned = trades.collections(state)
    a0, b0 = owned["p0"][0], owned["p1"][0]
    run(state, log, "p0", "create", "t1", offers=[
        {"from": "p0", "to": "p1", "card_id": a0},
        {"from": "p1", "to": "p0", "card_id": b0},
    ])
    with pytest.raises(trades.TradeError) as err:
        trades.command(state, "p1", "edit", "t1", 1, [
            {"from": "p0", "to": "p1", "card_id": a0},
            {"from": "p1", "to": "p0", "card_id": b0},
        ])
    assert err.value.code == "not_author"
    with pytest.raises(trades.TradeError) as err:
        trades.command(state, "p1", "cancel", "t1", 1)
    assert err.value.code == "not_author"
    with pytest.raises(trades.TradeError) as err:
        trades.command(state, "p0", "edit", "t1", 7, [
            {"from": "p0", "to": "p1", "card_id": a0},
            {"from": "p1", "to": "p0", "card_id": b0},
        ])
    assert err.value.code == "revision_mismatch"
    with pytest.raises(trades.TradeError) as err:
        trades.command(state, "p0", "cancel", "t1", 0)
    assert err.value.code == "revision_mismatch"
    with pytest.raises(trades.TradeError) as err:
        trades.command(state, "p3", "confirm", "t1", 1)
    assert err.value.code == "not_participant"
    # None of the rejected attempts left a trace.
    assert state.trades["t1"]["revision"] == 1
    assert state.trades["t1"]["status"] == "open"
    assert trades.collections(state) == owned
    assert_replay_matches(state, log)


def test_three_party_trade_commits_atomically_in_confirm_order():
    state, log = make_revealed_state()
    owned = trades.collections(state)
    a, b, c = owned["p0"][0], owned["p1"][0], owned["p2"][0]
    run(state, log, "p0", "create", "t1", offers=[
        {"from": "p0", "to": "p1", "card_id": a},
        {"from": "p1", "to": "p2", "card_id": b},
        {"from": "p2", "to": "p0", "card_id": c},
    ])
    # Interleaved confirmations: ownership must not change until the last one.
    run(state, log, "p2", "confirm", "t1", revision=1)
    run(state, log, "p0", "confirm", "t1", revision=1)
    assert trades.collections(state) == owned
    run(state, log, "p1", "confirm", "t1", revision=1)
    assert state.trades["t1"]["status"] == "committed"
    now = trades.collections(state)
    assert a in now["p1"] and b in now["p2"] and c in now["p0"]
    assert_conserved(now, [c for cards in owned.values() for c in cards])
    assert_replay_matches(state, log)


# ---- server-level helpers ----------------------------------------------------

def revealed(game):
    """player_id -> revealed card ids, per the admin truth."""
    owned = {p.id: [] for p in game.players}
    for entry in game.admin_state()["history"]:
        for pid, pick in entry["picks"].items():
            owned[pid].append(pick["card_id"])
    return owned


def call_trade(game, player, body):
    return game.server.http().post(
        f"/games/{game.id}/trades", params={"token": player.token}, json=body
    )


def all_revealed_cards(game):
    return [c for cards in revealed(game).values() for c in cards]


# ---- server-level tests ------------------------------------------------------

async def test_three_party_trade_commits_while_draft_continues(server):
    game = make_game(server, rounds=4, timeout=45, seed=42)
    game.force_timeout()  # round 1 revealed, round 2 live
    owned = revealed(game)
    p0, p1, p2, p3 = game.players
    a, b, c = owned[p0.id][0], owned[p1.id][0], owned[p2.id][0]
    offers = [
        {"from": p0.id, "to": p1.id, "card_id": a},
        {"from": p1.id, "to": p2.id, "card_id": b},
        {"from": p2.id, "to": p0.id, "card_id": c},
    ]
    r = call_trade(game, p0, {"action": "create", "id": "t1", "offers": offers})
    assert r.status_code == 200, r.text
    # Interleaved confirmations, including a duplicate submission.
    assert call_trade(game, p0, {"action": "confirm", "id": "t1", "revision": 1}).status_code == 200
    assert call_trade(game, p1, {"action": "confirm", "id": "t1", "revision": 1}).status_code == 200
    r = call_trade(game, p1, {"action": "confirm", "id": "t1", "revision": 1})
    assert r.status_code == 200
    confirmed = [t for t in r.json()["trades"] if t["id"] == "t1"][0]["confirmed"]
    assert confirmed == sorted([p0.id, p1.id])
    # Half confirmed: every card is still with its original owner.
    snap = game.snapshot(p2)
    assert snap["collections"] == owned

    # The draft moves on while the trade waits for the last confirmation.
    sockets = [await ws_connect(server, p) for p in game.players]
    try:
        for ws in sockets:
            await initial_state(ws)
        admin = game.admin_state()
        for i, ws in enumerate(sockets):
            await submit(ws, admin["packs"][game.players[i].id][0])
        await recv_until(
            sockets[0],
            lambda f: f.get("type") == "state" and f["state"]["round_no"] == 3,
        )
    finally:
        for ws in sockets:
            await ws.close()
    # Round 2 resolved while the trade waited; ownership is still exactly the
    # revealed picks — the uncommitted trade moved nothing.
    for pid, cards in revealed(game).items():
        assert sorted(game.snapshot(p0)["collections"][pid]) == sorted(cards)

    # The last participant reconnects first, then confirms: commit is atomic.
    snap = game.snapshot(p2)
    trade = [t for t in snap["trades"] if t["id"] == "t1"][0]
    assert trade["status"] == "open" and trade["confirmed"] == sorted([p0.id, p1.id])
    assert call_trade(game, p2, {"action": "confirm", "id": "t1", "revision": 1}).status_code == 200

    # Apply the committed trade on top of the revealed picks.
    expected = revealed(game)
    expected[p0.id] = sorted([x for x in expected[p0.id] if x != a] + [c])
    expected[p1.id] = sorted([x for x in expected[p1.id] if x != b] + [a])
    expected[p2.id] = sorted([x for x in expected[p2.id] if x != c] + [b])
    for player in game.players:
        snap = game.snapshot(player)
        for pid, cards in expected.items():
            assert sorted(snap["collections"][pid]) == sorted(cards)
        assert_conserved(snap["collections"], all_revealed_cards(game))
        trade = [t for t in snap["trades"] if t["id"] == "t1"][0]
        assert trade["status"] == "committed"

    # The historical reveal record still names the original pickers.
    history = game.admin_state()["history"]
    assert history[0]["picks"][p0.id]["card_id"] == a
    assert history[0]["picks"][p1.id]["card_id"] == b
    assert history[0]["picks"][p2.id]["card_id"] == c

    # A duplicate confirm after the commit changes nothing.
    before = game.snapshot(p0)
    r = call_trade(game, p0, {"action": "confirm", "id": "t1", "revision": 1})
    assert r.status_code == 200
    assert r.json()["collections"] == before["collections"]
    assert r.json()["trades"] == before["trades"]

    # Reconnect (WS push) and HTTP snapshot agree card for card.
    ws = await ws_connect(server, p3)
    try:
        pushed = await initial_state(ws)
        assert pushed["collections"] == game.snapshot(p3)["collections"]
        assert pushed["trades"] == game.snapshot(p3)["trades"]
    finally:
        await ws.close()


async def test_edit_after_confirm_requires_fresh_confirmations(server):
    game = make_game(server, rounds=3, timeout=45, seed=42)
    game.force_timeout()
    game.force_timeout()  # two revealed cards per seat
    owned = revealed(game)
    p0, p1 = game.players[0], game.players[1]
    a0, b0, b1 = owned[p0.id][0], owned[p1.id][0], owned[p1.id][1]
    r = call_trade(game, p0, {"action": "create", "id": "t1", "offers": [
        {"from": p0.id, "to": p1.id, "card_id": a0},
        {"from": p1.id, "to": p0.id, "card_id": b0},
    ]})
    assert r.status_code == 200, r.text
    assert call_trade(game, p1, {"action": "confirm", "id": "t1", "revision": 1}).status_code == 200

    # The proposer rewrites the deal after p1 already confirmed.
    r = call_trade(game, p0, {"action": "edit", "id": "t1", "revision": 1, "offers": [
        {"from": p0.id, "to": p1.id, "card_id": a0},
        {"from": p1.id, "to": p0.id, "card_id": b1},
    ]})
    assert r.status_code == 200, r.text
    trade = [t for t in r.json()["trades"] if t["id"] == "t1"][0]
    assert trade["revision"] == 2 and trade["confirmed"] == []

    # p1's stale confirmation must not complete the edited trade.
    r = call_trade(game, p1, {"action": "confirm", "id": "t1", "revision": 1})
    assert r.status_code == 400 and r.json()["detail"] == "revision_mismatch"
    r = call_trade(game, p0, {"action": "confirm", "id": "t1", "revision": 2})
    trade = [t for t in r.json()["trades"] if t["id"] == "t1"][0]
    assert trade["status"] == "open" and trade["confirmed"] == [p0.id]
    assert game.snapshot(p1)["collections"] == owned  # nothing moved

    # Only p1's fresh approval of the actual content commits it.
    assert call_trade(game, p1, {"action": "confirm", "id": "t1", "revision": 2}).status_code == 200
    now = game.snapshot(p0)["collections"]
    assert a0 in now[p1.id] and b1 in now[p0.id] and b0 in now[p1.id]
    assert_conserved(now, all_revealed_cards(game))


async def test_competing_trades_for_one_card_commit_only_once(server):
    game = make_game(server, rounds=3, timeout=45, seed=42)
    game.force_timeout()
    game.force_timeout()
    owned = revealed(game)
    p0, p1, p2 = game.players[:3]
    x, y, z = owned[p0.id][0], owned[p1.id][0], owned[p2.id][0]
    for tid, partner, give in (("t1", p1, y), ("t2", p2, z)):
        r = call_trade(game, p0, {"action": "create", "id": tid, "offers": [
            {"from": p0.id, "to": partner.id, "card_id": x},
            {"from": partner.id, "to": p0.id, "card_id": give},
        ]})
        assert r.status_code == 200, r.text
    # t1 wins the race to full confirmation.
    assert call_trade(game, p0, {"action": "confirm", "id": "t1", "revision": 1}).status_code == 200
    assert call_trade(game, p1, {"action": "confirm", "id": "t1", "revision": 1}).status_code == 200
    # t2 gathers its other confirmation but can never commit: x has moved.
    assert call_trade(game, p2, {"action": "confirm", "id": "t2", "revision": 1}).status_code == 200
    r = call_trade(game, p0, {"action": "confirm", "id": "t2", "revision": 1})
    assert r.status_code == 400 and r.json()["detail"] == "card_unavailable"

    snap = game.snapshot(p2)
    assert x in snap["collections"][p1.id]
    assert x not in snap["collections"][p2.id]
    assert z in snap["collections"][p2.id]
    assert [t for t in snap["trades"] if t["id"] == "t2"][0]["status"] == "open"
    assert_conserved(snap["collections"], all_revealed_cards(game))

    # Cancelling the loser changes no ownership either.
    assert call_trade(game, p0, {"action": "cancel", "id": "t2", "revision": 1}).status_code == 200
    snap = game.snapshot(p1)
    assert x in snap["collections"][p1.id] and z in snap["collections"][p2.id]
    assert_conserved(snap["collections"], all_revealed_cards(game))


async def test_cancel_never_changes_ownership(server):
    game = make_game(server, rounds=3, timeout=45, seed=42)
    game.force_timeout()
    owned = revealed(game)
    p0, p1 = game.players[0], game.players[1]
    a0, b0 = owned[p0.id][0], owned[p1.id][0]
    assert call_trade(game, p0, {"action": "create", "id": "t1", "offers": [
        {"from": p0.id, "to": p1.id, "card_id": a0},
        {"from": p1.id, "to": p0.id, "card_id": b0},
    ]}).status_code == 200
    assert call_trade(game, p0, {"action": "confirm", "id": "t1", "revision": 1}).status_code == 200
    # A non-author cannot cancel someone else's trade.
    r = call_trade(game, p1, {"action": "cancel", "id": "t1", "revision": 1})
    assert r.status_code == 400 and r.json()["detail"] == "not_author"
    # The author cancels after half the confirmations: nothing moves, nothing
    # vanishes — both cards stay exactly where they were.
    assert call_trade(game, p0, {"action": "cancel", "id": "t1", "revision": 1}).status_code == 200
    snap = game.snapshot(p1)
    assert snap["collections"] == owned
    assert [t for t in snap["trades"] if t["id"] == "t1"][0]["status"] == "cancelled"
    assert_conserved(snap["collections"], all_revealed_cards(game))


async def test_unrevealed_hand_cards_are_rejected_and_never_broadcast(server):
    game = make_game(server, rounds=3, timeout=45, seed=42)
    p0, p1 = game.players[0], game.players[1]
    ws1 = await ws_connect(server, p1)
    try:
        await initial_state(ws1)
        # Round 1 is live and nothing is public yet: p0's own hand card.
        hand = game.snapshot(p0)["my_pack"][0]["id"]
        r = call_trade(game, p0, {"action": "create", "id": "t1", "offers": [
            {"from": p0.id, "to": p1.id, "card_id": hand},
            {"from": p1.id, "to": p0.id, "card_id": 1},
        ]})
        assert r.status_code == 400 and r.json()["detail"] == "card_unavailable"

        game.force_timeout()  # round 1 revealed; round 2 hand is secret again
        secret = game.admin_state()["packs"][p0.id][0]
        r = call_trade(game, p0, {"action": "create", "id": "t2", "offers": [
            {"from": p0.id, "to": p1.id, "card_id": secret},
            {"from": p1.id, "to": p0.id,
             "card_id": revealed(game)[p1.id][0]},
        ]})
        assert r.status_code == 400 and r.json()["detail"] == "card_unavailable"

        # No trade message ever reached the other player's socket, and the
        # secret card id appears nowhere in any frame.
        frames = await drain(ws1)
        assert all(f.get("type") == "state" for f in frames)

        def ints_in(node):
            if isinstance(node, bool):
                return set()
            if isinstance(node, int):
                return {node}
            if isinstance(node, dict):
                out = set()
                for key, value in node.items():
                    out |= ints_in(key) | ints_in(value)
                return out
            if isinstance(node, list):
                out = set()
                for value in node:
                    out |= ints_in(value)
                return out
            return set()

        for frame in frames:
            assert frame["state"]["trades"] == []
            assert secret not in ints_in(frame)
    finally:
        await ws1.close()


async def test_concurrent_final_confirms_on_competing_trades_commit_once(server):
    game = make_game(server, rounds=3, timeout=45, seed=42)
    game.force_timeout()
    game.force_timeout()
    owned = revealed(game)
    p0, p1, p2 = game.players[:3]
    x, y, z = owned[p0.id][0], owned[p1.id][0], owned[p2.id][0]
    for tid, partner, give in (("t1", p1, y), ("t2", p2, z)):
        assert call_trade(game, p0, {"action": "create", "id": tid, "offers": [
            {"from": p0.id, "to": partner.id, "card_id": x},
            {"from": partner.id, "to": p0.id, "card_id": give},
        ]}).status_code == 200
        assert call_trade(game, p0, {"action": "confirm", "id": tid, "revision": 1}).status_code == 200

    # The two committing confirmations race; exactly one trade may commit.
    from concurrent.futures import ThreadPoolExecutor

    def confirm(player, tid):
        return call_trade(game, player, {"action": "confirm", "id": tid, "revision": 1})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: confirm(*args), [(p1, "t1"), (p2, "t2")]))
    assert sorted(r.status_code for r in results) == [200, 400]
    loser = [r for r in results if r.status_code == 400][0]
    assert loser.json()["detail"] == "card_unavailable"

    snap = game.snapshot(p0)
    statuses = {t["id"]: t["status"] for t in snap["trades"]}
    assert sorted(statuses.values()) == ["committed", "open"]
    # x lives in exactly one collection; every revealed card is accounted for.
    holders = [pid for pid, cards in snap["collections"].items() if x in cards]
    assert len(holders) == 1 and holders[0] in (p1.id, p2.id)
    assert_conserved(snap["collections"], all_revealed_cards(game))


async def test_collections_survive_restart_exactly(server):
    db_path = server.db_path
    game = make_game(server, rounds=4, timeout=45, seed=42)
    game.force_timeout()
    game.force_timeout()
    owned = revealed(game)
    p0, p1, p2 = game.players[:3]
    a, b = owned[p0.id][0], owned[p1.id][0]
    y, z = owned[p1.id][1], owned[p2.id][0]
    # One committed trade, one half-confirmed open trade, one cancelled trade.
    assert call_trade(game, p0, {"action": "create", "id": "done", "offers": [
        {"from": p0.id, "to": p1.id, "card_id": a},
        {"from": p1.id, "to": p0.id, "card_id": b},
    ]}).status_code == 200
    assert call_trade(game, p0, {"action": "confirm", "id": "done", "revision": 1}).status_code == 200
    assert call_trade(game, p1, {"action": "confirm", "id": "done", "revision": 1}).status_code == 200
    assert call_trade(game, p1, {"action": "create", "id": "open", "offers": [
        {"from": p1.id, "to": p2.id, "card_id": y},
        {"from": p2.id, "to": p1.id, "card_id": z},
    ]}).status_code == 200
    assert call_trade(game, p2, {"action": "confirm", "id": "open", "revision": 1}).status_code == 200
    assert call_trade(game, p1, {"action": "cancel", "id": "open", "revision": 1}).status_code == 200

    before = {p.id: game.snapshot(p) for p in game.players}
    history_before = game.admin_state()["history"]

    # A brand-new server process replays the same database from scratch.
    server2 = start_server(db_path)
    for player in game.players:
        r = server2.http().get(f"/games/{game.id}", params={"token": player.token})
        assert r.status_code == 200
        restored = r.json()
        assert restored["collections"] == before[player.id]["collections"]
        assert restored["trades"] == before[player.id]["trades"]
        assert restored["reveals"] == before[player.id]["reveals"]
        assert_conserved(restored["collections"], all_revealed_cards(game))
    r = server2.http().get(f"/admin/games/{game.id}/state")
    assert r.json()["history"] == history_before

    # The half-confirmed-then-cancelled trade left no ownership change, and
    # the committed one is still settled after the replay.
    final = server2.http().get(
        f"/games/{game.id}", params={"token": p0.token}
    ).json()
    assert a in final["collections"][p1.id] and b in final["collections"][p0.id]
    assert y in final["collections"][p1.id] and z in final["collections"][p2.id]
