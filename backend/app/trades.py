"""Public-card trades; immutable draft reveals are never edited."""

from __future__ import annotations

from copy import deepcopy


class TradeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def collections(state):
    if hasattr(state, "live_collections"):
        return deepcopy(state.live_collections)
    owned = {pid: [] for pid in state.seats}
    for reveal in state.history:
        for pid, pick in reveal["picks"].items():
            owned[pid].append(pick["card_id"])
    return owned


def _offers(state, raw, check=True):
    if not isinstance(raw, list) or not 2 <= len(raw) <= 12:
        raise TradeError("invalid_trade")
    normalized = []
    cards = set()
    gives, receives = set(), set()
    owned = {
        pid: list(cards) + list(state.packs.get(pid, []))
        for pid, cards in collections(state).items()
    }
    for item in raw:
        if not isinstance(item, dict):
            raise TradeError("invalid_trade")
        source, target, card = item.get("from"), item.get("to"), item.get("card_id")
        if source not in owned or target not in owned or source == target:
            raise TradeError("invalid_trade")
        if (
            type(card) is not int
            or card in cards
            or (check and card not in owned[source])
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
    current = state.trades.get(trade_id)
    if actor not in state.seats:
        raise TradeError("not_seated")
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
    if action == "edit":
        return [
            {
                "type": "trade_edited",
                "id": trade_id,
                "revision": current["revision"] + 1,
                "offers": _offers(state, offers),
            }
        ]
    if action == "cancel":
        if hasattr(state, "live_collections"):
            for offer in current["offers"]:
                destination = state.live_collections[offer["to"]]
                if offer["card_id"] in destination:
                    destination.remove(offer["card_id"])
        return [
            {"type": "trade_cancelled", "id": trade_id, "revision": current["revision"]}
        ]
    if action != "confirm" or actor not in current["participants"]:
        raise TradeError("not_participant")
    if not hasattr(state, "live_collections"):
        state.live_collections = collections(state)
    for offer in current["offers"]:
        if offer["from"] == actor:
            source = state.live_collections[actor]
            if offer["card_id"] in source:
                source.remove(offer["card_id"])
            state.live_collections[offer["to"]].append(offer["card_id"])
    events = [
        {
            "type": "trade_confirmed",
            "id": trade_id,
            "revision": current["revision"],
            "actor": actor,
        }
    ]
    if set(current["confirmed"]) | {actor} == set(current["participants"]):
        events.append(
            {
                "type": "trade_committed",
                "id": trade_id,
                "revision": current["revision"],
                "offers": current["offers"],
            }
        )
    return events


def apply_event(state, event):
    trade_id = event["id"]
    kind = event["type"]
    if kind == "trade_proposed":
        state.trades[trade_id] = {
            "id": trade_id,
            "author": event["author"],
            "revision": 1,
            "offers": deepcopy(event["offers"]),
            "initial_offers": deepcopy(event["offers"]),
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
