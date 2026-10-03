"""Public-card trades; immutable draft reveals are never edited.

Only revealed (public) cards may be traded — a card still sitting in a secret
pack is not owned yet and must never appear in a trade message.  A trade takes
effect atomically: confirmations are bound to one revision of the content and
cards move exactly once, when every participant has confirmed that same
revision.  Cancelling a trade never changes ownership.  Committed trades are
replayed from the event log into ``state.trade_ledger`` (in commit order), so
the live view, a reconnect snapshot and a cold replay all derive identical
collections.
"""

from __future__ import annotations

from copy import deepcopy


class TradeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def collections(state):
    """Current ownership: revealed picks plus committed trades, in order.

    Pure function of the replayed state — no side channels.  Historical
    reveals keep their original pickers; only this derived ownership view
    reflects committed trades.
    """
    owned = {pid: [] for pid in state.seats}
    for reveal in state.history:
        for pid, pick in reveal["picks"].items():
            owned[pid].append(pick["card_id"])
    for offers in state.trade_ledger:
        for offer in offers:
            owned[offer["from"]].remove(offer["card_id"])
            owned[offer["to"]].append(offer["card_id"])
    return owned


def _offers(state, raw):
    if not isinstance(raw, list) or not 2 <= len(raw) <= 12:
        raise TradeError("invalid_trade")
    normalized = []
    cards = set()
    gives, receives = set(), set()
    owned = collections(state)
    for item in raw:
        if not isinstance(item, dict):
            raise TradeError("invalid_trade")
        source, target, card = item.get("from"), item.get("to"), item.get("card_id")
        if source not in owned or target not in owned or source == target:
            raise TradeError("invalid_trade")
        # Only publicly revealed cards are owned and tradeable; current-round
        # secret packs are deliberately excluded, so an unrevealed card can
        # never become part of a trade message.
        if (
            type(card) is not int
            or card in cards
            or card not in owned[source]
        ):
            raise TradeError("card_unavailable")
        cards.add(card)
        gives.add(source)
        receives.add(target)
        normalized.append({"from": source, "to": target, "card_id": card})
    if gives != receives:
        raise TradeError("invalid_trade")
    return sorted(normalized, key=lambda x: (x["from"], x["to"], x["card_id"]))


def _participants(offers):
    return sorted({item["from"] for item in offers} | {item["to"] for item in offers})


def command(state, actor, action, trade_id, revision=None, offers=None):
    if actor not in state.seats:
        raise TradeError("not_seated")
    if not isinstance(trade_id, str) or not trade_id or len(trade_id) > 64:
        raise TradeError("invalid_trade")
    current = state.trades.get(trade_id)
    if action == "create":
        if current:
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
    if action in ("edit", "cancel"):
        # Only the proposer may modify or withdraw their own trade, and only
        # the revision they actually saw (optimistic concurrency).
        if actor != current["author"]:
            raise TradeError("not_author")
        if revision != current["revision"]:
            raise TradeError("revision_mismatch")
        if action == "edit":
            return [
                {
                    "type": "trade_edited",
                    "id": trade_id,
                    "revision": current["revision"] + 1,
                    "offers": _offers(state, offers),
                }
            ]
        # Cancelling changes the trade's status only, never card ownership.
        return [
            {"type": "trade_cancelled", "id": trade_id, "revision": current["revision"]}
        ]
    if action == "confirm":
        if actor not in current["participants"]:
            raise TradeError("not_participant")
        # A confirmation approves exactly one revision of the content; an
        # edited trade needs fresh confirmations from everyone.
        if revision != current["revision"]:
            raise TradeError("revision_mismatch")
        if actor in current["confirmed"]:
            # Repeat confirm: idempotent no-op, never moves cards twice.
            return []
        events = [
            {
                "type": "trade_confirmed",
                "id": trade_id,
                "revision": current["revision"],
                "actor": actor,
            }
        ]
        if set(current["confirmed"]) | {actor} == set(current["participants"]):
            # Everyone approved this revision: the whole trade commits
            # atomically.  Cards are never reserved, so re-check that every
            # offered card is still with its sender — a competing trade may
            # have moved it meanwhile, and only one of them may commit.
            owned = collections(state)
            if any(
                offer["card_id"] not in owned[offer["from"]]
                for offer in current["offers"]
            ):
                raise TradeError("card_unavailable")
            events.append(
                {
                    "type": "trade_committed",
                    "id": trade_id,
                    "revision": current["revision"],
                    "offers": deepcopy(current["offers"]),
                }
            )
        return events
    raise TradeError("invalid_trade")


def apply_event(state, event):
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
            # New content, new approvals: earlier confirmations bound to the
            # previous revision and must not carry over.
            confirmed=[],
        )
    elif kind == "trade_confirmed":
        current = state.trades[trade_id]
        if event["actor"] not in current["confirmed"]:
            current["confirmed"].append(event["actor"])
            current["confirmed"].sort()
    elif kind == "trade_cancelled":
        state.trades[trade_id]["status"] = "cancelled"
    elif kind == "trade_committed":
        current = state.trades[trade_id]
        if current["status"] != "committed":
            current["status"] = "committed"
            # Ownership changes exactly here, in commit order, so live views
            # and cold replays derive identical collections.
            state.trade_ledger.append(deepcopy(event["offers"]))
    else:
        raise ValueError("unknown trade event")


def project(state):
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
