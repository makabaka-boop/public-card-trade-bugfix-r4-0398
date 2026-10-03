"""Pure event-sourced state machine for the four-seat draft.

Nothing in here touches the database, sockets or the clock.  That keeps the
draft rules (dealing, passing, timeout picks, secrecy boundary) unit-testable
and lets the server rebuild any game by replaying its persisted events.

Event stream (version 1)
------------------------
game_created   game id, seats, config, shuffled deck
round_opened   round number, deadline, secret pack for every seat
pick_submitted round, player, card, manual|auto
round_revealed round, public picks (card + mode per seat), timed_out flag
game_completed final marker
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .cards import CARDS

AUTO = "auto"
MANUAL = "manual"
PENDING = "pending"
LOCKED = "locked"


@dataclass
class DraftState:
    game_id: str = ""
    status: str = "waiting"  # waiting | active | completed
    seats: List[str] = field(default_factory=list)
    num_rounds: int = 0
    pack_size: int = 5
    timeout: float = 45.0
    deck: List[int] = field(default_factory=list)
    cursor: int = 0
    round_no: int = 0
    epoch: int = 0
    deadline: Optional[float] = None
    packs: Dict[str, List[int]] = field(default_factory=dict)
    # player_id -> (card_id, mode) for the current round
    picks: Dict[str, Tuple[int, str]] = field(default_factory=dict)
    # completed rounds in order
    history: List[Dict[str, Any]] = field(default_factory=list)
    trades: Dict[str, Any] = field(default_factory=dict)
    trade_ledger: List[List[Dict[str, Any]]] = field(default_factory=list)
    version: int = 0

    # ---- queries -------------------------------------------------------
    def seat_of(self, player_id: str) -> int:
        return self.seats.index(player_id)

    def next_seat(self, player_id: str) -> str:
        return self.seats[(self.seat_of(player_id) + 1) % len(self.seats)]

    def auto_card(self, player_id: str) -> int:
        """Stable timeout rule: lowest card id in the player's pack."""
        return min(self.packs[player_id])


def apply_event(state: DraftState, event: Dict[str, Any]) -> DraftState:
    """Return ``state`` updated by one persisted event (mutates in place)."""
    etype = event["type"]
    if etype == "game_created":
        state.game_id = event["game_id"]
        state.seats = list(event["seats"])
        state.num_rounds = event["num_rounds"]
        state.pack_size = event["pack_size"]
        state.timeout = event["timeout"]
        state.deck = list(event["deck"])
        state.cursor = 0
        state.status = "active"
    elif etype == "round_opened":
        state.round_no = event["round_no"]
        state.epoch = event.get("epoch", state.epoch + 1)
        state.deadline = event["deadline"]
        # The cursor at/after the deal is part of the event so a pure replay
        # reproduces the deck position without re-running deal logic.
        state.cursor = event.get("cursor", state.cursor)
        # Stored as lists per seat; JSON keys are strings.
        state.packs = {pid: list(cards) for pid, cards in event["packs"].items()}
        state.picks = {}
    elif etype == "pick_submitted":
        state.picks[event["player_id"]] = (event["card_id"], event["mode"])
    elif etype == "round_revealed":
        state.history.append(
            {
                "round_no": event["round_no"],
                "timed_out": event["timed_out"],
                "picks": dict(event["picks"]),
            }
        )
        # Keep packs/picks so open_round_event can rotate the leftovers;
        # the following round_opened event overwrites them.
    elif etype.startswith("trade_"):
        from .trades import apply_event as apply_trade_event
        apply_trade_event(state,event)
    elif etype == "game_completed":
        state.status = "completed"
        state.deadline = None
        state.packs = {}
        state.picks = {}
    state.version += 1
    return state


def replay(events: List[Dict[str, Any]]) -> DraftState:
    state = DraftState()
    for event in events:
        apply_event(state, event)
    return state


# ---- event production ----------------------------------------------------

def open_round_event(state: DraftState, now: float) -> Dict[str, Any]:
    """Build the next ``round_opened`` event and advance deck cursor.

    Round 1 deals ``pack_size`` fresh cards per seat.  Every later round
    rotates each seat's leftovers forward by one seat and tops each pack up
    with one *distinct* fresh card, so every held pack always contains five
    cards.  Callers persist the returned event and then apply it.
    """
    n = len(state.seats)
    packs: Dict[str, List[int]] = {}
    if state.round_no == 0:
        for pid in state.seats:
            packs[pid] = state.deck[state.cursor : state.cursor + state.pack_size]
            state.cursor += state.pack_size
    else:
        leftovers = {
            pid: [c for c in state.packs[pid] if state.picks.get(pid, (None,))[0] != c]
            for pid in state.seats
        }
        top_ups = state.deck[state.cursor : state.cursor + n]
        state.cursor += n
        for i, pid in enumerate(state.seats):
            packs[pid] = leftovers[state.seats[i - 1]] + [top_ups[i]]
    state.epoch += 1
    return {
        "type": "round_opened",
        "round_no": state.round_no + 1,
        "epoch": state.epoch,
        "deadline": now + state.timeout,
        "packs": packs,
        "cursor": state.cursor,
    }


def submit_pick(
    state: DraftState, player_id: str, card_id: int, mode: str = MANUAL
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Validate a manual submission.

    Returns ``(event, outcome)`` where outcome is one of:
      created   - a new pick event must be persisted
      duplicate - this exact pick is already recorded (idempotent replay)
      error:...- a caller-safe error code; details carry no other players'
                  card information
    """
    if state.status != "active" or not state.packs:
        return None, "error:no_active_round"
    if player_id not in state.seats:
        return None, "error:not_seated"
    if player_id in state.picks:
        existing = state.picks[player_id][0]
        if existing == card_id:
            return None, "duplicate"
        return None, "error:already_picked"
    if card_id not in state.packs[player_id]:
        return None, "error:card_not_in_pack"
    return (
        {
            "type": "pick_submitted",
            "round_no": state.round_no,
            "player_id": player_id,
            "card_id": card_id,
            "mode": mode,
        },
        "created",
    )


def resolve_round(
    state: DraftState, now: float, timed_out: bool
) -> List[Dict[str, Any]]:
    """Close the current round: auto-pick missing seats, reveal, advance.

    Returns the events still to persist (0 if there is nothing to resolve).
    Deterministic regardless of how it was triggered: timeout watchdog, the
    admin force endpoint, or post-restart recovery all take this same path.
    """
    if state.status != "active" or not state.packs:
        return []

    events: List[Dict[str, Any]] = []
    for pid in state.seats:  # seat order makes auto picks deterministic
        if pid not in state.picks:
            card_id = state.auto_card(pid)
            events.append(
                {
                    "type": "pick_submitted",
                    "round_no": state.round_no,
                    "player_id": pid,
                    "card_id": card_id,
                    "mode": AUTO,
                }
            )
            state.picks[pid] = (card_id, AUTO)

    # round_revealed deliberately keeps packs/picks in live state, so
    # open_round_event below can rotate the leftovers.
    public_picks = {
        pid: {"card_id": state.picks[pid][0], "mode": state.picks[pid][1]}
        for pid in state.seats
    }
    reveal = {
        "type": "round_revealed",
        "round_no": state.round_no,
        "timed_out": timed_out,
        "picks": public_picks,
    }
    events.append(reveal)
    apply_event(state, reveal)

    if state.round_no >= state.num_rounds:
        done = {"type": "game_completed"}
        events.append(done)
        apply_event(state, done)
    else:
        opened = open_round_event(state, now)
        events.append(opened)
        apply_event(state, opened)
    return events


# ---- projection ----------------------------------------------------------

def project(state: DraftState, viewer_id: Optional[str]) -> Dict[str, Any]:
    """Project state for one player.

    ``viewer_id`` receives their own current pack and nothing about any other
    seat's current pack.  Revealed picks from completed rounds are public.
    """
    active = state.status == "active" and bool(state.packs)
    my_pack: List[Dict[str, Any]] = []
    my_pick: Optional[Dict[str, Any]] = None
    if active and viewer_id in state.packs:
        my_pack = [{"id": c, "name": CARDS[c]} for c in state.packs[viewer_id]]
        if viewer_id in state.picks:
            card_id, mode = state.picks[viewer_id]
            my_pick = {"card_id": card_id, "mode": mode}

    return {
        "status": state.status,
        "game_id": state.game_id,
        "you": viewer_id,
        "seats": [
            {"player_id": pid, "seat": idx}
            for idx, pid in enumerate(state.seats)
        ],
        "round_no": state.round_no if active else None,
        "num_rounds": state.num_rounds,
        "deadline": state.deadline if active else None,
        "pack_size": state.pack_size,
        "my_pack": my_pack,
        "my_pick": my_pick,
        # Coordination metadata only: who has locked in, never what they took.
        "lock_state": {
            pid: (LOCKED if pid in state.picks else PENDING)
            for pid in state.seats
        } if active else {},
        "reveals": [
            {
                "round_no": entry["round_no"],
                "timed_out": entry["timed_out"],
                "picks": {
                    pid: {
                        "card_id": p["card_id"],
                        "name": CARDS[p["card_id"]],
                        "mode": p["mode"],
                    }
                    for pid, p in entry["picks"].items()
                },
            }
            for entry in state.history
        ],
        "trades": __import__(__package__ + ".trades",fromlist=["project"]).project(state),
        "collections": _collections(state),
        "version": state.version,
    }


def _collections(state: DraftState) -> Dict[str, List[int]]:
    from .trades import collections
    return collections(state)
