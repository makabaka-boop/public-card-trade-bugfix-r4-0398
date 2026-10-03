"""Game orchestration: rooms, locking, clock watchdog, concurrency.

Concurrency model
-----------------
* A single ``asyncio.Lock`` per game serialises every state transition
  (submission, resolve, startup recovery).  The lock makes the last writer
  redundant rather than rejected: once state is re-read after awaiting the
  lock, repeat submissions collapse to idempotent acks.
* The event batch is written in one SQLite transaction with a UNIQUE
  (game_id, seq) constraint, which is the durable backstop against double
  resolution even if the in-process assumptions ever broke.
* Timeouts are driven by ``loop.call_later``.  When the callback fires and
  also when a manual submission completes the round, both call the exact same
  ``resolve`` path; only one of them can pass the guard
  ``round_no / status / packs`` and actually persist events.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .cards import shuffled_deck
from .config import Settings
from .db import Repository
from .engine import (
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
from .hub import Hub
from .security import generate_token, hash_token


class GameError(Exception):
    """Error whose ``code`` is safe to return to a player verbatim."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Room:
    def __init__(
        self,
        game_id: str,
        state: DraftState,
        names: Dict[str, str],
        lock: asyncio.Lock,
    ):
        self.game_id = game_id
        self.state = state
        self.names = names
        self.lock = lock
        self.timer: Optional[asyncio.TimerHandle] = None


class GameService:
    def __init__(self, repo: Repository, hub: Hub, settings: Settings):
        self.repo = repo
        self.hub = hub
        self.settings = settings
        self.rooms: Dict[str, Room] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, game_id: str) -> asyncio.Lock:
        if game_id not in self._locks:
            self._locks[game_id] = asyncio.Lock()
        return self._locks[game_id]

    # ---- setup / recovery ---------------------------------------------
    async def create_game(
        self,
        num_rounds: Optional[int] = None,
        timeout: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> str:
        game_id = uuid.uuid4().hex[:12]
        now = time.time()
        await self.repo.create_game(
            game_id,
            num_rounds or self.settings.default_rounds,
            self.settings.pack_size,
            timeout or self.settings.default_timeout,
            seed if seed is not None else uuid.uuid4().int % (2**32),
            now,
        )
        return game_id

    async def join_game(
        self, game_id: str, name: str
    ) -> Tuple[str, str, int]:
        row = await self.repo.get_game(game_id)
        if row is None:
            raise GameError("game_not_found")
        players = await self.repo.list_players(game_id)
        if len(players) >= self.settings.num_players:
            raise GameError("game_full")
        if any(p["name"] == name for p in players):
            raise GameError("name_taken")
        player_id = uuid.uuid4().hex[:12]
        token = generate_token()
        seat = len(players)
        await self.repo.add_player(game_id, player_id, seat, name, hash_token(token))
        return player_id, token, seat

    async def authenticate(self, game_id: str, token: str) -> Tuple[str, int, str]:
        """Return (player_id, seat, name) or raise a player-safe error."""
        row = await self.repo.find_player(game_id, hash_token(token))
        if row is None:
            raise GameError("unauthorized")
        return row["player_id"], row["seat"], row["name"]

    async def start_game(self, game_id: str) -> None:
        async with self._lock_for(game_id):
            room = await self._load_room(game_id)
            if room.state.status == "active":
                return
            if len(room.state.seats) != self.settings.num_players:
                raise GameError("not_enough_players")
            game_row = await self.repo.get_game(game_id)
            deck = shuffled_deck(game_row["seed"])
            created = {
                "type": "game_created",
                "game_id": game_id,
                "seats": room.state.seats,
                "num_rounds": game_row["num_rounds"],
                "pack_size": game_row["pack_size"],
                "timeout": game_row["timeout"],
                "deck": deck,
            }
            apply_event(room.state, created)
            opened = open_round_event(room.state, time.time())
            apply_event(room.state, opened)
            await self.repo.append_events(
                game_id, [created, opened], time.time()
            )
            self._arm_timer(room)
            await self.hub.broadcast_state(room.state)

    async def recover_all(self) -> None:
        """Rebuild every active game after a server restart.

        If the persisted deadline already passed, resolve immediately using
        the stable auto-pick rule; otherwise re-arm the watchdog.
        """
        for game_id in await self.repo.list_active_games():
            room = await self._load_room(game_id)
            now = time.time()
            if room.state.status == "active" and room.state.packs:
                if room.state.deadline is not None and room.state.deadline <= now:
                    async with room.lock:
                        await self._resolve_if_current(
                            room,
                            now,
                            timed_out=True,
                            expected_epoch=room.state.epoch,
                        )
                else:
                    self._arm_timer(room)

    # ---- player actions ------------------------------------------------
    async def get_projection(
        self, game_id: str, player_id: str
    ) -> Dict[str, Any]:
        room = await self._load_room(game_id)
        return project(room.state, player_id)

    async def get_room(self, game_id: str) -> Room:
        return await self._load_room(game_id)

    async def submit_pick(
        self, game_id: str, player_id: str, card_id: Any, ws: Any
    ) -> None:
        """Manual submission: idempotent, concurrency-safe, single-count."""
        try:
            card_int = int(card_id)
        except (TypeError, ValueError):
            await self.hub.send_error(game_id, player_id, "invalid_card", ws)
            return

        room = await self._load_room(game_id)
        # Capture the round instance before awaiting the lock: if a timeout
        # resolves this round while we wait, the submission is stale and must
        # never land in the freshly opened next round.
        expected_epoch = room.state.epoch
        async with room.lock:
            if (
                room.state.status == "active"
                and room.state.packs
                and room.state.epoch != expected_epoch
            ):
                await self.hub.send_error(
                    game_id, player_id, "round_advanced", ws
                )
                return
            event, outcome = submit_pick(room.state, player_id, card_int, MANUAL)
            if outcome == "created":
                apply_event(room.state, event)
                await self._persist_and_broadcast(room, [event])
                await self.hub.send_ack(game_id, player_id, card_int, ws)
                if len(room.state.picks) == len(room.state.seats):
                    await self._resolve_if_current(
                        room,
                        time.time(),
                        timed_out=False,
                        expected_epoch=room.state.epoch,
                    )
            elif outcome == "duplicate":
                # Repeat click / concurrent second call / reconnect replay:
                # acknowledge exactly the recorded pick, persist nothing.
                recorded = room.state.picks[player_id][0]
                await self.hub.send_ack(game_id, player_id, recorded, ws)
            else:
                code = outcome[len("error:"):] if outcome.startswith("error:") else outcome
                await self.hub.send_error(game_id, player_id, code, ws)


    async def trade_action(self, game_id, player_id, body):
        from .trades import command, TradeError
        room = await self._load_room(game_id)
        async with room.lock:
            try:
                events = command(room.state, player_id, body.get('action'), body.get('id'),
                                 body.get('revision'), body.get('offers'))
            except TradeError as exc:
                raise GameError(exc.code)
            for event in events:
                apply_event(room.state,event)
            if events:
                # The commit event is part of the durable log too: without it
                # a replay would show the trade as never completed and the
                # collections would diverge from the live view.
                await self._persist_and_broadcast(room,events)
            return project(room.state,player_id)

    async def force_timeout(self, game_id: str) -> None:
        """Admin/test hook: resolve the live round as if its clock expired."""
        room = await self._load_room(game_id)
        async with room.lock:
            epoch = room.state.epoch
            await self._resolve_if_current(
                room, time.time(), timed_out=True, expected_epoch=epoch
            )

    async def dump_state(self, game_id: str) -> Optional[DraftState]:
        """Admin/test hook: full state including every seat's secret pack."""
        room = await self._load_room(game_id)
        return room.state

    # ---- internals -----------------------------------------------------
    async def _load_room(self, game_id: str) -> Room:
        existing = self.rooms.get(game_id)
        if existing is not None and existing.state.seats:
            return existing
        players = await self.repo.list_players(game_id)
        if not players:
            raise GameError("game_not_found")
        seats = [p["player_id"] for p in players]
        names = {p["player_id"]: p["name"] for p in players}
        events = await self.repo.load_events(game_id)
        state = replay(events)
        state.game_id = game_id
        if not state.seats:
            state.seats = seats  # waiting: roster comes from DB rows
        room = Room(game_id, state, names, self._lock_for(game_id))
        self.rooms[game_id] = room
        return room

    async def _persist_and_broadcast(
        self, room: Room, events: List[Dict[str, Any]]
    ) -> None:
        await self.repo.append_events(room.game_id, events, time.time())
        await self.hub.broadcast_state(room.state)

    async def _resolve_if_current(
        self,
        room: Room,
        now: float,
        timed_out: bool,
        expected_epoch: Optional[int] = None,
    ) -> None:
        """Resolve only while the targeted round instance is still live.

        Called from the watchdog, manual completion, force endpoint and
        restart recovery.  Two guards make racing triggers (timeout vs a
        last-millisecond human pick, or two force calls) resolve exactly
        once: the per-game lock serialises callers, and ``expected_epoch``
        (a counter bumped on every round_opened) turns a queued caller into
        a no-op once the round instance it aimed at has been replaced —
        even when the replacement happens to be another open round.
        """
        self._cancel_timer(room)
        if room.state.status != "active" or not room.state.packs:
            return
        if expected_epoch is not None and room.state.epoch != expected_epoch:
            return
        events = resolve_round(room.state, now, timed_out=timed_out)
        if not events:
            return
        await self._persist_and_broadcast(room, events)
        if room.state.status == "active":
            self._arm_timer(room)

    def _arm_timer(self, room: Room) -> None:
        self._cancel_timer(room)
        if room.state.status != "active" or room.state.deadline is None:
            return
        delay = max(0.0, room.state.deadline - time.time())
        epoch = room.state.epoch
        loop = asyncio.get_running_loop()

        def _fire() -> None:
            asyncio.create_task(self._timer_resolve(room.game_id, epoch))

        room.timer = loop.call_later(delay, _fire)

    async def _timer_resolve(self, game_id: str, epoch: int) -> None:
        room = self.rooms.get(game_id)
        if room is None:
            return
        async with room.lock:
            # Ignore a stale handle from a round that already advanced.
            if (
                room.state.epoch != epoch
                or not room.state.packs
                or room.state.status != "active"
            ):
                return
            await self._resolve_if_current(
                room, time.time(), timed_out=True, expected_epoch=epoch
            )

    def _cancel_timer(self, room: Room) -> None:
        if room.timer is not None:
            room.timer.cancel()
            room.timer = None
