"""Unit tests for the pure draft state machine."""
from __future__ import annotations

import json

import pytest

from app.cards import shuffled_deck
from app.engine import (
    AUTO,
    MANUAL,
    DraftState,
    apply_event,
    open_round_event,
    project,
    replay,
    resolve_round,
    submit_pick,
)

SEATS = ["p0", "p1", "p2", "p3"]


def make_state(rounds: int = 4, timeout: float = 45.0, seed: int = 42) -> DraftState:
    state = DraftState()
    apply_event(
        state,
        {
            "type": "game_created",
            "game_id": "g",
            "seats": SEATS,
            "num_rounds": rounds,
            "pack_size": 5,
            "timeout": timeout,
            "deck": shuffled_deck(seed),
        },
    )
    apply_event(state, open_round_event(state, 100.0))
    return state


def test_every_seat_gets_five_distinct_cards():
    state = make_state()
    assert all(len(pack) == 5 for pack in state.packs.values())
    all_cards = [c for pack in state.packs.values() for c in pack]
    assert len(all_cards) == len(set(all_cards)) == 20


def test_pick_is_idempotent_and_change_is_rejected():
    state = make_state()
    card = state.packs["p0"][2]
    event, outcome = submit_pick(state, "p0", card)
    assert outcome == "created"
    apply_event(state, event)

    _event2, outcome2 = submit_pick(state, "p0", card)
    assert outcome2 == "duplicate"

    other = [c for c in state.packs["p0"] if c != card][0]
    _event3, outcome3 = submit_pick(state, "p0", other)
    assert outcome3 == "error:already_picked"


def test_card_not_in_own_pack_is_rejected():
    state = make_state()
    foreign = state.packs["p1"][0]
    _e, outcome = submit_pick(state, "p0", foreign)
    assert outcome == "error:card_not_in_pack"


def test_auto_pick_is_lowest_card_id():
    state = make_state()
    packs_before = {pid: list(cards) for pid, cards in state.packs.items()}
    # Only p0 picks; everyone else should auto-take their lowest card.
    event, _ = submit_pick(state, "p0", state.packs["p0"][0])
    apply_event(state, event)
    resolve_round(state, 200.0, timed_out=True)
    reveal = state.history[0]
    assert reveal["timed_out"] is True
    assert reveal["picks"]["p0"]["mode"] == MANUAL
    for pid in SEATS[1:]:
        assert reveal["picks"][pid]["mode"] == AUTO
        assert reveal["picks"][pid]["card_id"] == min(packs_before[pid])


def test_leftovers_rotate_and_packs_stay_at_five():
    state = make_state()
    picks = {pid: state.packs[pid][i] for i, pid in enumerate(SEATS)}
    for pid, card in picks.items():
        apply_event(state, submit_pick(state, pid, card)[0])
    resolve_round(state, 200.0, timed_out=False)

    assert state.round_no == 2
    assert all(len(pack) == 5 for pack in state.packs.values())
    # Seat i receives the 4 leftovers of seat i-1, plus one fresh card.
    deck = shuffled_deck(42)
    for i, pid in enumerate(SEATS):
        prev_seat = (i - 1) % 4
        prev = SEATS[prev_seat]
        prev_slice = deck[prev_seat * 5 : prev_seat * 5 + 5]
        leftovers = sorted(c for c in prev_slice if c != picks[prev])
        assert sorted(state.packs[pid][:4]) == leftovers
    assert len({pack[-1] for pack in state.packs.values()}) == 4
    # Topped-up cards come from the shared deck after the initial deal.
    assert {pack[-1] for pack in state.packs.values()} <= set(deck[20:])


def test_replay_reproduces_exact_state():
    state = make_state(rounds=3)
    log = []
    for r in range(3):
        for i, pid in enumerate(SEATS):
            if r % 2 == 0 or i == 0:
                ev, oc = submit_pick(state, pid, state.packs[pid][i % 5])
                if oc == "created":
                    log.append(ev)
                    apply_event(state, ev)
        new_events = resolve_round(state, 300 + r, timed_out=(r % 2 == 1))
        log.extend(new_events)

    rebuilt = replay(
        [
            {
                "type": "game_created",
                "game_id": "g",
                "seats": SEATS,
                "num_rounds": 3,
                "pack_size": 5,
                "timeout": 45.0,
                "deck": shuffled_deck(42),
            }
        ]
        + log
    )
    assert rebuilt.history == state.history
    assert rebuilt.cursor == state.cursor
    assert rebuilt.packs == state.packs
    assert rebuilt.picks == state.picks
    assert rebuilt.status == "completed"


def test_projection_never_contains_other_seats_cards():
    state = make_state(rounds=2)
    for i, pid in enumerate(SEATS):
        apply_event(state, submit_pick(state, pid, state.packs[pid][i])[0])
    resolve_round(state, 200.0, timed_out=False)

    admin_packs = {pid: list(cards) for pid, cards in state.packs.items()}
    # Cards that are already public (round 1 reveal) may appear anywhere.
    public_cards = {
        pick["card_id"]
        for reveal in state.history
        for pick in reveal["picks"].values()
    }

    def card_carrying_values(view):
        """Only fields that can actually carry a card identity."""
        ids = [c["id"] for c in view["my_pack"]]
        if view["my_pick"]:
            ids.append(view["my_pick"]["card_id"])
        for reveal in view["reveals"]:
            ids.extend(p["card_id"] for p in reveal["picks"].values())
        ids.extend(c for cards in view["collections"].values() for c in cards)
        return ids

    for viewer in SEATS:
        view = project(state, viewer)
        visible = set(card_carrying_values(view))
        own = set(admin_packs[viewer])
        for other in SEATS:
            if other == viewer:
                continue
            for card in admin_packs[other]:
                if card in own or card in public_cards:
                    continue
                assert card not in visible, (
                    f"{viewer} can secretly see card {card} held by {other}"
                )
        assert [c["id"] for c in view["my_pack"]] == admin_packs[viewer]


def test_lock_state_exposes_coordination_not_choices():
    state = make_state()
    apply_event(state, submit_pick(state, "p0", state.packs["p0"][0])[0])
    view = project(state, "p1")
    assert view["lock_state"] == {"p0": "locked", "p1": "pending",
                                  "p2": "pending", "p3": "pending"}
    # p1 must not learn WHICH card p0 chose before the reveal.
    p0_card = state.picks["p0"][0]
    assert p0_card not in [c["id"] for c in view["my_pack"]]
    assert all(
        p0_card != pick["card_id"]
        for reveal in view["reveals"]
        for pick in reveal["picks"].values()
    )


def test_game_completes_after_configured_rounds():
    state = make_state(rounds=2)
    for _ in range(2):
        for pid in SEATS:
            if pid in state.packs:
                apply_event(state, submit_pick(state, pid, state.packs[pid][0])[0])
        resolve_round(state, 200.0, timed_out=False)
    assert state.status == "completed"
    assert len(state.history) == 2
    collections = project(state, "p0")["collections"]
    assert all(len(cards) == 2 for cards in collections.values())
