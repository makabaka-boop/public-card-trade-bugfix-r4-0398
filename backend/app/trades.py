"""Public-card trades; immutable draft reveals are never edited.

A trade is a *proposal* only.  Cards never move while participants confirm:
ownership changes in exactly one atomic step when every participant has
confirmed the *same* revision and every giver still owns every offered card.
Because ownership is derived purely from the immutable reveal history plus
committed-trade offers (``trade_committed`` events), the live projection, an
HTTP reconnect snapshot and a full event replay always agree.

Event stream
------------
trade_proposed  id, author, revision=1, offers
trade_edited    id, revision, offers (resets all confirmations)
trade_confirmed id, revision, actor
trade_cancelled id, revision
trade_committed id, revision, offers (atomic ownership change)
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from .engine import DraftState

MAX_OFFERS = 12


class TradeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def revealed_ownership(state: "DraftState") -> Dict[str, List[int]]:
    """Card ownership derived from public facts only.

    The base ownership is the reveal history: whose seat picked which card.
    Committed trades are then replayed in commit order.  Unrevealed current
    hands (``state.packs``) are deliberately excluded — they are secret and
    can never be traded.
    """
    owned: Dict[str, List[int]] = {pid: [] for pid in state.seats}
    for reveal in state.history:
        for pid, pick in reveal["picks"].items():
            owned.setdefault(pid, []).append(pick["card_id"])
    for offers in state.trade_ledger:
        for offer in offers:
            source = owned[offer["from"]]
            if offer["card_id"] in source:
                source.remove(offer["card_id"])
            owned[offer["to"]].append(offer["card_id"])
    return owned


def collections(state: "DraftState") -> Dict[str, List[int]]:
    # Always derived; a stale live-collections cache was what let the online
    # view diverge from replay.
    return revealed_ownership(state)


def _offers(state: "DraftState", raw: Any) -> List[Dict[str, Any]]:
    """Validate and normalise an offer list against *current* ownership."""
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_OFFERS:
        raise TradeError("invalid_trade")
    owned = revealed_ownership(state)
    normalized: List[Dict[str, Any]] = []
    cards = set()
    gives, receives = set(), set()
    for item in raw:
        if not isinstance(item, dict):
            raise TradeError("invalid_trade")
        source, target, card = item.get("from"), item.get("to"), item.get("card_id")
        if not isinstance(source, str) or not isinstance(target, str):
            raise TradeError("invalid_trade")
        if source not in owned or target not in owned or source == target:
            raise TradeError("invalid_trade")
        if type(card) is not int:
            raise TradeError("invalid_trade")
        # Unrevealed cards fail the ownership check: they are absent from
        # ``owned`` and can never enter a trade; the same card listed twice
        # in one proposal is also rejected here.
        if card in cards or card not in owned[source]:
            raise TradeError("card_unavailable")
        cards.add(card)
        gives.add(source)
        receives.add(target)
        normalized.append({"from": source, "to": target, "card_id": card})
    # Every participant must give and receive; 2..4 distinct participants.
    if gives != receives or not 2 <= len(gives) <= 4:
        raise TradeError("invalid_trade")
    return sorted(normalized, key=lambda x: (x["from"], x["to"], x["card_id"]))


def _participants(offers: List[Dict[str, Any]]) -> List[str]:
    return sorted({item["from"] for item in offers} | {item["to"] for item in offers})


def _check_revision(current: Dict[str, Any], revision: Any) -> None:
    # A confirmation/edit/cancel must name the revision it was based on.
    # Anything stale or malformed is rejected; no stored confirmation is
    # ever reused against a different content version.
    if type(revision) is not int or revision != current["revision"]:
        raise TradeError("revision_mismatch")


def _commit_event(state: "DraftState", current: Dict[str, Any]) -> Dict[str, Any]:
    """Build the atomic commit event, only while all givers can still pay.

    With no reservation, two competing trades may name the same public card.
    The commit that finds every giver short even one offered card fails
    closed (the trade simply stays open); exactly one side can win a card.
    """
    owned = revealed_ownership(state)
    for offer in current["offers"]:
        if offer["card_id"] not in owned[offer["from"]]:
            raise TradeError("card_unavailable")
    return {
        "type": "trade_committed",
        "id": current["id"],
        "revision": current["revision"],
        "offers": deepcopy(current["offers"]),
    }


def command(
    state: "DraftState",
    actor: str,
    action: Any,
    trade_id: Any,
    revision: Any = None,
    offers: Any = None,
) -> List[Dict[str, Any]]:
    if not isinstance(trade_id, str) or not trade_id:
        raise TradeError("invalid_trade")
    if action not in ("create", "edit", "cancel", "confirm"):
        raise TradeError("invalid_trade")
    if actor not in state.seats:
        raise TradeError("not_seated")
    current = state.trades.get(trade_id)

    if action == "create":
        # Duplicate create against an existing id is an idempotent no-op.
        if current is not None:
            return []
        proposed = _offers(state, offers)
        return [
            {
                "type": "trade_proposed",
                "id": trade_id,
                "author": actor,
                "revision": 1,
                "offers": proposed,
            }
        ]

    if current is None:
        raise TradeError("trade_not_found")
    if current["status"] != "open":
        return []

    if action == "edit":
        # Only the proposer may change content; edits must target the
        # current revision and wipe every prior confirmation, because a
        # confirmation means "I agree to *this exact content*".
        if actor != current["author"]:
            raise TradeError("not_author")
        _check_revision(current, revision)
        return [
            {
                "type": "trade_edited",
                "id": trade_id,
                "revision": current["revision"] + 1,
                "offers": _offers(state, offers),
            }
        ]

    if action == "cancel":
        # Cancellation changes no card ownership; it only retires the
        # proposal.  Author-only and revision-gated.
        if actor != current["author"]:
            raise TradeError("not_author")
        _check_revision(current, revision)
        return [
            {"type": "trade_cancelled", "id": trade_id, "revision": current["revision"]}
        ]

    if action == "confirm":
        if actor not in current["participants"]:
            raise TradeError("not_participant")
        _check_revision(current, revision)
        # Repeated confirmation of the same revision is idempotent and
        # produces no new event; a re-sent old revision is rejected above.
        if actor in current["confirmed"]:
            return []
        events: List[Dict[str, Any]] = [
            {
                "type": "trade_confirmed",
                "id": trade_id,
                "revision": current["revision"],
                "actor": actor,
            }
        ]
        if set(current["confirmed"]) | {actor} == set(current["participants"]):
            # Everyone has signed the same revision: validate ownership
            # against the state that includes this last confirmation and
            # commit the whole exchange atomically, or fail closed.
            events.append(_commit_event(state, current))
        return events

    # Unreachable: action membership was validated up front.
    raise TradeError("invalid_trade")


def apply_event(state: "DraftState", event: Dict[str, Any]) -> None:
    trade_id = event["id"]
    kind = event["type"]
    if kind == "trade_proposed":
        state.trades[trade_id] = {
            "id": trade_id,
            "author": event["author"],
            "revision": 1,
            "offers": deepcopy(event["offers"]),
            "participants": _participants(event["offers"]),
            "confirmed": [],
            "status": "open",
        }
    elif kind == "trade_edited":
        current = state.trades[trade_id]
        current.update(
            revision=event["revision"],
            offers=deepcopy(event["offers"]),
            participants=_participants(event["offers"]),
            confirmed=[],
        )
    elif kind == "trade_confirmed":
        current = state.trades[trade_id]
        # A confirmation only counts for the exact revision it was made on.
        if event["revision"] != current["revision"]:
            return
        if event["actor"] not in current["confirmed"]:
            current["confirmed"].append(event["actor"])
            current["confirmed"].sort()
    elif kind == "trade_cancelled":
        current = state.trades[trade_id]
        if event["revision"] == current["revision"]:
            current["status"] = "cancelled"
    elif kind == "trade_committed":
        current = state.trades[trade_id]
        if current["status"] != "committed":
            current["status"] = "committed"
            state.trade_ledger.append(deepcopy(event["offers"]))
    else:
        raise ValueError("unknown trade event")


def project(state: "DraftState") -> List[Dict[str, Any]]:
    return [
        {
            "id": t["id"],
            "revision": t["revision"],
            "author": t["author"],
            "offers": deepcopy(t["offers"]),
            "participants": list(t["participants"]),
            "confirmed": list(t["confirmed"]),
            "status": t["status"],
        }
        for _, t in sorted(state.trades.items())
    ]
