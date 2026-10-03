"""End-to-end trade tests over a real uvicorn server.

Exercises the fault report through the actual HTTP surface:
* interleaved confirmations across three parties while drafting continues;
* stale confirmations after an edit cannot complete a changed trade;
* duplicate confirmations never double-trade;
* the same public card competing in two trades ends up with exactly one
  owner and card totals/ownership match between the live view, an HTTP
  reconnect snapshot, a WebSocket push and a full server restart replay;
* cancellation/rejection leaves every card with its original owner;
* an unrevealed hand card can never be offered;
* reveal history keeps the original result after trades settle.
"""
from __future__ import annotations

import httpx
import pytest

from conftest import (
    initial_state,
    make_game,
    recv_until,
    start_server,
    ws_connect,
)

pytestmark = pytest.mark.asyncio


class TradeClient:
    def __init__(self, server, game):
        self.http = server.http()
        self.game = game
        self.gid = game.id

    def post(self, player, **body):
        r = self.http.post(
            f"/games/{self.gid}/trades",
            params={"token": player.token},
            json=body,
        )
        return r

    def act(self, player, **body):
        r = self.post(player, **body)
        assert r.status_code == 200, r.text
        return r.json()

    def err(self, player, status, **body):
        r = self.post(player, **body)
        assert r.status_code == status, r.text
        return r.json()["detail"]

    def snap(self, player):
        r = self.http.get(
            f"/games/{self.gid}", params={"token": player.token}
        )
        assert r.status_code == 200, r.text
        return r.json()

    def admin(self):
        r = self.http.get(f"/admin/games/{self.gid}/state")
        assert r.status_code == 200, r.text
        return r.json()


def reveal_a_round(game, client):
    """Force the current round to resolve; return {seat_index: card_id}."""
    game.force_timeout()
    # Spin until a reveal is durable.
    for _ in range(50):
        hist = client.admin()["history"]
        if hist:
            break
        import time
        time.sleep(0.05)
    picks = client.admin()["history"][-1]["picks"]
    return {i: picks[game.players[i].id]["card_id"] for i in range(4)}


def trade_by_id(view, tid):
    return next(t for t in view["trades"] if t["id"] == tid)


def offer(game, src, dst, card):
    """Build one offer leg from seat indices (server ids are opaque)."""
    return {"from": game.players[src].id, "to": game.players[dst].id,
            "card_id": card}


def assert_totals(admin, players_per_seat=1, rounds_revealed=None):
    """Card conservation across collections: each card exactly once."""
    coll = admin["collections"]
    flat = [c for cards in coll.values() for c in cards]
    assert len(flat) == len(set(flat)), f"duplicate/missing cards: {coll}"
    if rounds_revealed is not None:
        assert len(flat) == 4 * rounds_revealed
    return coll


# --------------------------------------------------------------------------

async def test_stale_confirmation_after_edit_cannot_complete(server):
    game = make_game(server, rounds=3, timeout=300, seed=42)
    tc = TradeClient(server, game)
    cards = reveal_a_round(game, tc)
    p0, p1 = game.players[0], game.players[1]
    offers = [offer(game, 0, 1, cards[0]), offer(game, 1, 0, cards[1])]

    tc.act(p0, action="create", id="t", offers=offers)
    tc.act(p1, action="confirm", id="t", revision=1)

    # p1's old confirmation must not be reusable: edit rev1 -> rev2.
    tc.act(p0, action="edit", id="t", revision=1, offers=offers)
    view = tc.snap(p0)
    assert trade_by_id(view, "t")["confirmed"] == []
    assert trade_by_id(view, "t")["revision"] == 2
    tc.err(p1, 409, action="confirm", id="t", revision=1)
    # Author confirming alone cannot close it.
    tc.act(p0, action="confirm", id="t", revision=2)
    assert trade_by_id(tc.snap(p0), "t")["status"] == "open"
    # Both on rev 2 now commit.
    tc.act(p1, action="confirm", id="t", revision=2)
    committed = trade_by_id(tc.snap(p0), "t")
    assert committed["status"] == "committed"
    admin = tc.admin()
    coll = assert_totals(admin)
    assert coll[p0.id] == [cards[1]]
    assert coll[p1.id] == [cards[0]]
    # History attribution is unchanged.
    hist = admin["history"][0]["picks"]
    assert hist[p0.id]["card_id"] == cards[0]
    assert hist[p1.id]["card_id"] == cards[1]


async def test_interleaved_three_party_confirmations_with_drafting(server):
    game = make_game(server, rounds=3, timeout=300, seed=7)
    tc = TradeClient(server, game)
    r1 = reveal_a_round(game, tc)
    offers = [offer(game, 0, 1, r1[0]),
              offer(game, 1, 2, r1[1]),
              offer(game, 2, 0, r1[2])]
    tc.act(game.players[0], action="create", id="tri", offers=offers)

    # Drafting continues while people take their time confirming.
    r2 = reveal_a_round(game, tc)
    tc.act(game.players[0], action="confirm", id="tri", revision=1)
    r3 = reveal_a_round(game, tc)
    tc.act(game.players[2], action="confirm", id="tri", revision=1)
    # Still open after two of three.
    assert trade_by_id(tc.snap(game.players[0]), "tri")["status"] == "open"
    tc.act(game.players[1], action="confirm", id="tri", revision=1)

    admin = tc.admin()
    assert trade_by_id(tc.snap(game.players[0]), "tri")["status"] == "committed"
    coll = assert_totals(admin, rounds_revealed=3)
    # Round-1 cards rotated by the trade; rounds 2/3 untouched.
    assert r1[0] in coll[game.players[1].id]
    assert r1[1] in coll[game.players[2].id]
    assert r1[2] in coll[game.players[0].id]
    assert r2[0] in coll[game.players[0].id]
    assert r3[3] in coll[game.players[3].id]


async def test_duplicate_confirmation_does_not_double_trade(server):
    game = make_game(server, rounds=2, timeout=300, seed=42)
    tc = TradeClient(server, game)
    cards = reveal_a_round(game, tc)
    p0, p1 = game.players[0], game.players[1]
    offers = [offer(game, 0, 1, cards[0]), offer(game, 1, 0, cards[1])]
    tc.act(p0, action="create", id="t", offers=offers)
    # p0 spams confirm; only one confirmation recorded.
    for _ in range(4):
        tc.act(p0, action="confirm", id="t", revision=1)
    assert trade_by_id(tc.snap(p0), "t")["confirmed"] == [p0.id]
    tc.act(p1, action="confirm", id="t", revision=1)
    # Resubmits after commit change nothing.
    for _ in range(3):
        tc.act(p0, action="confirm", id="t", revision=1)
    admin = tc.admin()
    coll = assert_totals(admin, rounds_revealed=1)
    assert coll[p0.id] == [cards[1]] and coll[p1.id] == [cards[0]]


async def test_competing_trades_same_card_single_owner_and_reconnect_matches(server):
    game = make_game(server, rounds=3, timeout=300, seed=42)
    tc = TradeClient(server, game)
    c = reveal_a_round(game, tc)
    p0, p1, p2 = game.players[0], game.players[1], game.players[2]
    ab = [offer(game, 0, 1, c[0]), offer(game, 1, 0, c[1])]
    ac = [offer(game, 0, 2, c[0]), offer(game, 2, 0, c[2])]
    tc.act(p0, action="create", id="ab", offers=ab)
    tc.act(p0, action="create", id="ac", offers=ac)

    # Interleave: both participants of ab confirm first -> ab wins.
    tc.act(p0, action="confirm", id="ab", revision=1)
    tc.act(p0, action="confirm", id="ac", revision=1)
    tc.act(p1, action="confirm", id="ab", revision=1)
    # Losing trade's final confirmation is rejected; card never duplicated.
    tc.err(p2, 409, action="confirm", id="ac", revision=1)

    admin = tc.admin()
    coll = assert_totals(admin, rounds_revealed=1)
    owners = {pid: c[0] in cards for pid, cards in coll.items()}
    assert sum(owners.values()) == 1
    assert coll[p1.id] == [c[0]]

    # HTTP reconnect snapshot for every seat agrees with admin truth.
    for player in game.players:
        snap = tc.snap(player)
        assert snap["collections"] == admin["collections"]
        assert snap["trades"] == admin["trades"]


async def test_replay_after_restart_reproduces_trades_and_collections(server):
    db_path = server.db_path
    game = make_game(server, rounds=3, timeout=300, seed=42)
    tc = TradeClient(server, game)
    c1 = reveal_a_round(game, tc)
    offers = [offer(game, 0, 1, c1[0]), offer(game, 1, 0, c1[1])]
    tc.act(game.players[0], action="create", id="t", offers=offers)
    # Edit after one confirm; both then confirm rev 2.
    tc.act(game.players[1], action="confirm", id="t", revision=1)
    tc.act(game.players[0], action="edit", id="t", revision=1, offers=offers)
    tc.act(game.players[0], action="confirm", id="t", revision=2)
    tc.act(game.players[1], action="confirm", id="t", revision=2)
    # And a cancelled proposal that moved nothing.
    tc.act(
        game.players[2], action="create", id="x",
        offers=[offer(game, 2, 3, c1[2]), offer(game, 3, 2, c1[3])],
    )
    tc.act(game.players[2], action="cancel", id="x", revision=1)
    before = tc.admin()

    server2 = start_server(db_path)
    tc2 = TradeClient(server2, game)
    after = tc2.admin()
    assert after["collections"] == before["collections"]
    assert after["trades"] == before["trades"]
    assert after["history"] == before["history"]
    assert_totals(after, rounds_revealed=1)
    # Reconnect snapshots on the restarted process match too.
    for player in game.players:
        assert tc2.snap(player)["collections"] == before["collections"]


async def test_cancel_and_rejection_leave_cards_with_original_owners(server):
    game = make_game(server, rounds=2, timeout=300, seed=42)
    tc = TradeClient(server, game)
    c = reveal_a_round(game, tc)
    baseline = tc.admin()["collections"]

    tc.act(
        game.players[0], action="create", id="t",
        offers=[offer(game, 0, 1, c[0]), offer(game, 1, 0, c[1])],
    )
    tc.act(game.players[1], action="confirm", id="t", revision=1)
    tc.act(game.players[0], action="cancel", id="t", revision=1)
    assert tc.admin()["collections"] == baseline

    # Rejection = simply never confirming; proposal lingers, cards stay.
    tc.act(
        game.players[2], action="create", id="r",
        offers=[offer(game, 2, 3, c[2]), offer(game, 3, 2, c[3])],
    )
    tc.act(game.players[2], action="confirm", id="r", revision=1)
    assert tc.admin()["collections"] == baseline
    assert_totals(tc.admin(), rounds_revealed=1)


async def test_unrevealed_card_cannot_be_offered_over_http(server):
    game = make_game(server, rounds=2, timeout=300, seed=42)
    tc = TradeClient(server, game)
    # No reveal yet: collections are empty; any card id fails.
    admin = tc.admin()
    secret = admin["packs"][game.players[0].id][0]
    tc.err(
        game.players[0], 409, action="create", id="early",
        offers=[offer(game, 0, 1, secret), offer(game, 1, 0, secret)],
    )
    c = reveal_a_round(game, tc)
    # Current round-2 hand card still secret.
    secret2 = tc.admin()["packs"][game.players[1].id][0]
    tc.err(
        game.players[1], 409, action="create", id="s",
        offers=[offer(game, 1, 0, secret2), offer(game, 0, 1, c[0])],
    )
    # And it never appears in any projection/trade message.
    import json
    blob = json.dumps(tc.snap(game.players[3]))
    assert str(secret2) not in blob


async def test_websocket_push_agrees_with_http_snapshot_after_trade(server):
    game = make_game(server, rounds=2, timeout=300, seed=42)
    tc = TradeClient(server, game)
    c = reveal_a_round(game, tc)
    p0, p1 = game.players[0], game.players[1]

    ws0 = await ws_connect(server, p0)
    try:
        await initial_state(ws0)
        tc.act(
            p0, action="create", id="w",
            offers=[offer(game, 0, 1, c[0]), offer(game, 1, 0, c[1])],
        )
        tc.act(p0, action="confirm", id="w", revision=1)
        tc.act(p1, action="confirm", id="w", revision=1)

        frame = await recv_until(
            ws0,
            lambda f: f.get("type") == "state"
            and any(
                t["id"] == "w" and t["status"] == "committed"
                for t in f["state"]["trades"]
            ),
        )
        pushed = frame["state"]
        http_view = tc.snap(p0)
        assert pushed["collections"] == http_view["collections"]
        assert pushed["trades"] == http_view["trades"]
        assert pushed["collections"][p0.id] == [c[1]]
    finally:
        await ws0.close()
