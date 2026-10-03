"""In-process WebSocket fan-out.

A player may hold several sockets at once (reconnect before the old socket
drops, multiple tabs).  Every socket for a player receives the same
individually projected state; per-socket send failures never tear down the
other sockets.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import WebSocket

from .engine import DraftState, project

PlayerKey = Tuple[str, str]  # (game_id, player_id)


class Hub:
    def __init__(self) -> None:
        self._sockets: Dict[PlayerKey, Set[WebSocket]] = defaultdict(set)
        # Per-player send lock: frames to a player's sockets leave in the
        # order state transitions happen.
        self._send_locks: Dict[PlayerKey, asyncio.Lock] = defaultdict(asyncio.Lock)

    def add(self, game_id: str, player_id: str, ws: WebSocket) -> None:
        ws.draft_player_id = player_id
        self._sockets[(game_id, player_id)].add(ws)

    def remove(self, game_id: str, player_id: str, ws: WebSocket) -> None:
        self._sockets.get((game_id, player_id), set()).discard(ws)

    def sockets_for(self, game_id: str, player_id: str) -> Set[WebSocket]:
        return self._sockets.get((game_id, player_id), set())

    def players_in_game(self, game_id: str) -> List[str]:
        return [pid for (gid, pid) in self._sockets if gid == game_id]

    async def send_state(
        self, game_id: str, player_id: str, state: DraftState
    ) -> None:
        await self._send(
            game_id,
            player_id,
            {"type": "state", "state": project(state, player_id)},
        )

    async def broadcast_state(self, state: DraftState) -> None:
        """Push a fresh projection to every seated player (individually)."""
        await asyncio.gather(
            *[self.send_state(state.game_id, pid, state) for pid in state.seats]
        )

    async def send_error(
        self, game_id: str, player_id: str, code: str, ws: Optional[WebSocket] = None
    ) -> None:
        # Generic codes only — errors never carry another player's cards.
        await self._send(
            game_id, player_id, {"type": "error", "code": code}, only=ws
        )

    async def send_ack(
        self, game_id: str, player_id: str, card_id: int, ws: WebSocket
    ) -> None:
        await self._send(
            game_id, player_id, {"type": "pick_ack", "card_id": card_id}, only=ws
        )

    async def _send(
        self,
        game_id: str,
        player_id: str,
        payload: Dict[str, Any],
        only: Optional[WebSocket] = None,
    ) -> None:
        key = (game_id, player_id)
        targets = {only} if only is not None else self.sockets_for(game_id, player_id)
        if not targets:
            return

        async with self._send_locks[key]:
            for ws in list(targets):
                try:
                    await ws.send_json(payload)
                except Exception:
                    self.remove(game_id, player_id, ws)
