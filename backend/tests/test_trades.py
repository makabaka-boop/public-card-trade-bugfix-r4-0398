"""Unit tests for public-card trade rules.

Covers the fault scenarios from the trade bug report:
* a confirmation binds to the exact revision it was made on — editing after
  one party confirmed must invalidate that confirmation;
* duplicate confirmations never double-trade;
* the whole exchange applies atomically only once every participant agrees;
* cancel/non-confirm never change ownership;
* unrevealed hand cards can never be offered or appear in trade contents;
* competing trades over the same card arbitrate by current ownership;
* cards are conserved (same multiset, every card in exactly one collection);
* replaying the persisted event stream reproduces collections exactly;
* reveal history keeps original attribution after trades commit.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from app.cards import shuffled_deck
from app.engine import (
    DraftState,
    apply_event,
    open_round_event,
    project,
    replay,
    resolve_round,
    submit_pick,
)
from app.trades import TradeError, collections, command

SEATS = ["p0", "p1", "p2", "p3"]

# Every state-mutating event the tests produce is appended here, exactly as
# the service would persist it.  replay(LOG) must then rebuild an equivalent
# state — that is what guarantees reconnect/replay agreement.
LOG: list = []


def boot(rounds=4, timeout=45.0, seed=42, now=100.0):
    state = DraftState()
    created = {
        "type": "game_created",
        "game_id": "g",
        "seats": SEATS,
        "num_rounds": rounds,
        "pack_size": 5,
        "timeout": timeout,
        "deck": shuffled_deck(seed),
    }
    apply_event(state, created)
    opened = open_round_event(state, now)
    apply_event(state, opened)
    LOG[:] = [created, opened]
    return state


def record(state, event):
    apply_event(state, event)
    LOG.append(deepcopy(event))


def reveal_round(state, round_time=200.0):
    """Every seat manually takes a card; resolve the round; log events.

    resolve_round applies its own events internally (following the engine
    contract used by the service), so only pick events are applied here;
    the returned reveal/open events are logged for replay but not re-applied.
    """
    for pid in SEATS:
        event, outcome = submit_pick(state, pid, state.packs[pid][0])
        assert outcome == "created"
        record(state, event)
    events = resolve_round(state, round_time, timed_out=False)
    LOG.extend(deepcopy(events))
    return {
        pid: state.history[-1]["picks"][pid]["card_id"] for pid in SEATS
    }


def run(state, actor, action, tid, revision=None, offers=None):
    events = command(state, actor, action, tid, revision=revision, offers=offers)
    for event in events:
        record(state, event)
    return events


def fail(state, actor, action, tid, revision=None, offers=None):
    with pytest.raises(TradeError) as exc:
        command(state, actor, action, tid, revision=revision, offers=offers)
    return exc.value.code


def assert_cards_conserved(state):
    owned = collections(state)
    flat = [c for cards in owned.values() for c in cards]
    assert len(flat) == len(set(flat)), f"duplicate ownership: {owned}"
    return owned


def swap(a_from, a_to, card_a, b_from=None, b_to=None, card_b=None):
    """Two-legged swap offer list; defaults to a/b symmetric legs."""
    return [
        {"from": a_from, "to": a_to, "card_id": card_a},
        {"from": b_from or a_to, "to": b_to or a_from, "card_id": card_b},
    ]


# --------------------------------------------------------------------------
# Revision-scoped confirmation: the core "confirm old content after edit" bug
# --------------------------------------------------------------------------

def test_confirmation_is_bound_to_revision_and_edit_resets_it():
    state = boot()
    picks = reveal_round(state)
    a, b = picks["p0"], picks["p1"]

    run(state, "p0", "create", "t1", offers=swap("p0", "p1", a, card_b=b))
    run(state, "p1", "confirm", "t1", revision=1)
    assert state.trades["t1"]["confirmed"] == ["p1"]

    # Edit while a confirmation is outstanding: revision must match the
    # version being changed, and every prior confirmation is wiped.
    assert fail(
        state, "p0", "edit", "t1", revision=2, offers=swap("p0", "p1", a, card_b=b)
    ) == "revision_mismatch"
    run(state, "p0", "edit", "t1", revision=1, offers=swap("p0", "p1", a, card_b=b))
    assert state.trades["t1"]["revision"] == 2
    assert state.trades["t1"]["confirmed"] == []

    # The stale confirmation cannot be replayed; author-only confirmation
    # on rev 2 must not commit.
    assert fail(state, "p1", "confirm", "t1", revision=1) == "revision_mismatch"
    run(state, "p0", "confirm", "t1", revision=2)
    assert state.trades["t1"]["status"] == "open"
    assert collections(state)["p0"] == [a]

    # Both confirm the same new content: atomic commit.
    run(state, "p1", "confirm", "t1", revision=2)
    assert state.trades["t1"]["status"] == "committed"
    owned = assert_cards_conserved(state)
    assert owned["p0"] == [b]
    assert owned["p1"] == [a]


def test_three_party_trade_commits_only_when_everyone_agrees():
    state = boot()
    picks = reveal_round(state)
    a, b, c = picks["p0"], picks["p1"], picks["p2"]
    offers = [
        {"from": "p0", "to": "p1", "card_id": a},
        {"from": "p1", "to": "p2", "card_id": b},
        {"from": "p2", "to": "p0", "card_id": c},
    ]
    run(state, "p0", "create", "t", offers=offers)
    run(state, "p0", "confirm", "t", revision=1)
    run(state, "p1", "confirm", "t", revision=1)
    owned = collections(state)
    assert a in owned["p0"] and b in owned["p1"] and c in owned["p2"]
    assert state.trades["t"]["status"] == "open"
    assert fail(state, "p3", "confirm", "t", revision=1) == "not_participant"
    run(state, "p2", "confirm", "t", revision=1)
    assert state.trades["t"]["status"] == "committed"
    owned = assert_cards_conserved(state)
    assert owned["p0"] == [c] and owned["p1"] == [a] and owned["p2"] == [b]


def test_duplicate_confirmation_is_idempotent_and_revision_scoped():
    state = boot()
    picks = reveal_round(state)
    a, b = picks["p0"], picks["p1"]
    run(state, "p0", "create", "t", offers=swap("p0", "p1", a, card_b=b))
    first = run(state, "p0", "confirm", "t", revision=1)
    assert len(first) == 1
    assert run(state, "p0", "confirm", "t", revision=1) == []
    assert state.trades["t"]["confirmed"] == ["p0"]
    assert fail(state, "p0", "confirm", "t", revision=2) == "revision_mismatch"


# --------------------------------------------------------------------------
# Cancel / rejection and ownership preservation
# --------------------------------------------------------------------------

def test_cancel_author_only_revision_gated_moves_no_cards():
    state = boot()
    picks = reveal_round(state)
    a, b = picks["p0"], picks["p1"]
    run(state, "p0", "create", "t", offers=swap("p0", "p1", a, card_b=b))
    run(state, "p1", "confirm", "t", revision=1)

    assert fail(state, "p1", "cancel", "t", revision=1) == "not_author"
    assert fail(state, "p0", "cancel", "t", revision=9) == "revision_mismatch"
    before = deepcopy(collections(state))
    run(state, "p0", "cancel", "t", revision=1)
    assert state.trades["t"]["status"] == "cancelled"
    assert collections(state) == before
    assert_cards_conserved(state)
    # Retired trades reject every further command without moving cards.
    assert run(state, "p1", "confirm", "t", revision=1) == []
    assert run(state, "p0", "cancel", "t", revision=1) == []


def test_not_confirming_never_moves_cards():
    state = boot()
    picks = reveal_round(state)
    a, b = picks["p0"], picks["p1"]
    run(state, "p0", "create", "t", offers=swap("p0", "p1", a, card_b=b))
    run(state, "p0", "confirm", "t", revision=1)
    run(state, "p0", "confirm", "t", revision=1)  # repeat, still alone
    owned = assert_cards_conserved(state)
    assert a in owned["p0"] and b in owned["p1"]


def test_partial_confirmations_then_cancel_keeps_every_card():
    """Regression for 'after cancel cards vanished from both collections'."""
    state = boot()
    picks = reveal_round(state)
    a, b = picks["p0"], picks["p1"]
    run(state, "p0", "create", "t", offers=swap("p0", "p1", a, card_b=b))
    run(state, "p1", "confirm", "t", revision=1)
    run(state, "p0", "cancel", "t", revision=1)
    owned = assert_cards_conserved(state)
    assert a in owned["p0"] and b in owned["p1"]
    # Still tradeable afterwards in a fresh proposal.
    run(state, "p1", "create", "t2", offers=swap("p1", "p0", b, card_b=a))
    run(state, "p0", "confirm", "t2", revision=1)
    run(state, "p1", "confirm", "t2", revision=1)
    owned = assert_cards_conserved(state)
    assert owned["p0"] == [b] and owned["p1"] == [a]


# --------------------------------------------------------------------------
# Secrecy: unrevealed hand cards can never enter a trade
# --------------------------------------------------------------------------

def test_unrevealed_hand_card_cannot_be_offered():
    state = boot()
    picks = reveal_round(state)
    a = picks["p0"]
    secret = state.packs["p1"][0]  # p1's hidden round-2 card
    public = {c for cards in collections(state).values() for c in cards}
    assert secret not in public

    assert fail(
        state, "p1", "create", "s",
        offers=swap("p1", "p0", secret, card_b=a),
    ) == "card_unavailable"

    b = picks["p1"]
    run(state, "p0", "create", "t", offers=swap("p0", "p1", a, card_b=b))
    assert fail(
        state, "p0", "edit", "t", revision=1,
        offers=swap("p0", "p1", a, b_from="p1", b_to="p0", card_b=secret),
    ) == "card_unavailable"


def test_trade_messages_carry_only_public_cards():
    state = boot()
    picks = reveal_round(state)
    a, b = picks["p0"], picks["p1"]
    secret_cards = {c for cards in state.packs.values() for c in cards}
    run(state, "p0", "create", "t", offers=swap("p0", "p1", a, card_b=b))
    for viewer in SEATS:
        view = project(state, viewer)
        blob = str(view["trades"])
        for card in secret_cards - {a, b}:
            assert str(card) not in blob
        # Offered card ids are exactly the public ones.
        offered = {o["card_id"] for t in view["trades"] for o in t["offers"]}
        assert offered <= {c for cs in collections(state).values() for c in cs}


# --------------------------------------------------------------------------
# Competing trades over the same public card
# --------------------------------------------------------------------------

def test_competing_trades_first_commit_wins_loser_fails_closed():
    state = boot(rounds=3)
    picks = reveal_round(state)
    a, b, c = picks["p0"], picks["p1"], picks["p2"]
    run(state, "p0", "create", "ab", offers=swap("p0", "p1", a, card_b=b))
    run(state, "p0", "create", "ac", offers=swap("p0", "p2", a, card_b=c))

    run(state, "p0", "confirm", "ab", revision=1)
    run(state, "p0", "confirm", "ac", revision=1)
    run(state, "p1", "confirm", "ab", revision=1)
    assert state.trades["ab"]["status"] == "committed"
    owned = assert_cards_conserved(state)
    assert a in owned["p1"] and b in owned["p0"]

    code = fail(state, "p2", "confirm", "ac", revision=1)
    assert code == "card_unavailable"
    assert state.trades["ac"]["status"] == "open"
    owned = assert_cards_conserved(state)
    assert sum(a in cards for cards in owned.values()) == 1
    assert sum(len(cs) for cs in owned.values()) == len(SEATS)


def test_competing_trades_commit_order_swapped_same_invariant():
    state = boot(rounds=3)
    picks = reveal_round(state)
    a, b, c = picks["p0"], picks["p1"], picks["p2"]
    run(state, "p0", "create", "ab", offers=swap("p0", "p1", a, card_b=b))
    run(state, "p0", "create", "ac", offers=swap("p0", "p2", a, card_b=c))
    # ac wins the race and commits.
    run(state, "p2", "confirm", "ac", revision=1)
    run(state, "p0", "confirm", "ac", revision=1)
    assert state.trades["ac"]["status"] == "committed"
    # ab's other participant can still confirm, but the final confirmation
    # (which would commit) must fail closed because p0 lost card a.
    run(state, "p1", "confirm", "ab", revision=1)
    assert state.trades["ab"]["status"] == "open"
    assert fail(state, "p0", "confirm", "ab", revision=1) == "card_unavailable"
    owned = assert_cards_conserved(state)
    assert a in owned["p2"] and c in owned["p0"] and a not in owned["p1"]


def test_card_can_be_retraded_only_by_its_new_owner():
    state = boot(rounds=3)
    picks = reveal_round(state)
    a, b, c = picks["p0"], picks["p1"], picks["p2"]
    # Two competing trades are proposed while p0 still owns card a.
    run(state, "p0", "create", "ab", offers=swap("p0", "p1", a, card_b=b))
    run(state, "p0", "create", "ac", offers=swap("p0", "p2", a, card_b=c))
    # ab commits first.
    run(state, "p0", "confirm", "ab", revision=1)
    run(state, "p1", "confirm", "ab", revision=1)

    # The competing proposal from the former owner can no longer commit.
    run(state, "p2", "confirm", "ac", revision=1)
    assert fail(state, "p0", "confirm", "ac", revision=1) == "card_unavailable"

    # A brand-new proposal promising a from p0 is rejected up front now.
    assert fail(
        state, "p0", "create", "ac2", offers=swap("p0", "p2", a, card_b=c)
    ) == "card_unavailable"

    # The new owner can legitimately trade it onward.
    run(state, "p1", "create", "bc", offers=swap("p1", "p2", a, card_b=c))
    run(state, "p1", "confirm", "bc", revision=1)
    run(state, "p2", "confirm", "bc", revision=1)
    owned = assert_cards_conserved(state)
    assert a in owned["p2"]
    assert sum(card == a for cs in owned.values() for card in cs) == 1


# --------------------------------------------------------------------------
# Replay / persistence parity and immutable history
# --------------------------------------------------------------------------

def test_replay_reproduces_collections_trades_and_conserves_cards():
    state = boot(rounds=3)
    picks = reveal_round(state, round_time=200)
    a, b = picks["p0"], picks["p1"]
    run(state, "p0", "create", "t", offers=swap("p0", "p1", a, card_b=b))
    run(state, "p1", "confirm", "t", revision=1)
    run(state, "p0", "edit", "t", revision=1, offers=swap("p0", "p1", a, card_b=b))
    run(state, "p0", "confirm", "t", revision=2)
    run(state, "p1", "confirm", "t", revision=2)
    # A competing open trade and a cancelled one are also on the record.
    c = picks["p2"]
    run(state, "p2", "create", "x", offers=swap("p2", "p3", c, card_b=picks["p3"]))
    run(state, "p2", "cancel", "x", revision=1)
    # Drafting continues concurrently with trades.
    reveal_round(state, round_time=300)

    rebuilt = replay(LOG)
    assert collections(rebuilt) == collections(state)
    assert project(rebuilt, "p3")["trades"] == project(state, "p3")["trades"]
    assert rebuilt.history == state.history
    flat = [c for cs in collections(rebuilt).values() for c in cs]
    assert len(flat) == len(set(flat))
    assert len(flat) == len(SEATS) * 2


def test_history_keeps_original_attribution_after_trade():
    state = boot()
    picks = reveal_round(state)
    a, b = picks["p0"], picks["p1"]
    run(state, "p0", "create", "t", offers=swap("p0", "p1", a, card_b=b))
    run(state, "p0", "confirm", "t", revision=1)
    run(state, "p1", "confirm", "t", revision=1)
    recorded = state.history[0]["picks"]
    assert recorded["p0"]["card_id"] == a
    assert recorded["p1"]["card_id"] == b
    owned = collections(state)
    assert owned["p0"] == [b] and owned["p1"] == [a]
    rebuilt = replay(LOG)
    assert rebuilt.history == state.history


# --------------------------------------------------------------------------
# Payload validation
# --------------------------------------------------------------------------

def test_bad_payloads_are_rejected():
    state = boot()
    picks = reveal_round(state)
    a, b = picks["p0"], picks["p1"]
    assert fail(state, "p0", "create", "x", offers=[{"from": "p0", "to": "p1", "card_id": a}]) == "invalid_trade"
    assert fail(
        state, "p0", "create", "x",
        offers=[{"from": "p0", "to": "p1", "card_id": a},
                {"from": "p1", "to": "p0", "card_id": a}],
    ) == "card_unavailable"
    # Gives != receives: p2 gives but never receives (cards are real so the
    # failure is the balance rule, not the ownership rule).
    assert fail(
        state, "p0", "create", "x",
        offers=[{"from": "p0", "to": "p1", "card_id": 39},
                {"from": "p2", "to": "p0", "card_id": 10}],
    ) == "invalid_trade"
    assert fail(state, "p0", "create", "x", offers="nope") == "invalid_trade"
    assert fail(state, "p0", "create", "x", offers=[]) == "invalid_trade"
    assert fail(
        state, "p0", "create", "x",
        offers=[{"from": 7, "to": "p1", "card_id": 1}],
    ) == "invalid_trade"
    assert fail(
        state, "p0", "create", "x",
        offers=[{"from": "p0", "to": "p1", "card_id": ["x"]}],
    ) == "invalid_trade"
    assert fail(state, "ghost", "create", "x", offers=[]) == "not_seated"
    assert fail(state, "p0", "confirm", "", revision=1) == "invalid_trade"

    run(
        state, "p0", "create", "t",
        offers=[{"from": "p0", "to": "p1", "card_id": a},
                {"from": "p1", "to": "p0", "card_id": b}],
    )
    assert fail(
        state, "p1", "edit", "t", revision=1,
        offers=[{"from": "p0", "to": "p1", "card_id": a},
                {"from": "p1", "to": "p0", "card_id": b}],
    ) == "not_author"
    assert fail(state, "p2", "confirm", "t", revision=1) == "not_participant"
    assert fail(state, "p0", "confirm", "missing", revision=1) == "trade_not_found"


def test_create_duplicate_id_is_idempotent_noop():
    state = boot()
    picks = reveal_round(state)
    a, b = picks["p0"], picks["p1"]
    offers = swap("p0", "p1", a, card_b=b)
    assert run(state, "p0", "create", "t", offers=offers)
    assert run(state, "p0", "create", "t", offers=offers) == []
