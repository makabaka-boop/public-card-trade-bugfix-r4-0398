"""SQLite persistence.

Two kinds of durable data:

* games / players rows give each game an identity and map opaque player
  tokens (stored only as SHA-256 hashes) to seats.
* ``events`` is the append-only log the draft state machine is replayed
  from.  Every state transition in a logical operation lands in one
  transaction, so a crash can never expose a half-open round.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'waiting',
    num_rounds  INTEGER NOT NULL,
    pack_size   INTEGER NOT NULL,
    timeout     REAL NOT NULL,
    seed        INTEGER NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    game_id     TEXT NOT NULL REFERENCES games(id),
    player_id   TEXT NOT NULL,
    seat        INTEGER NOT NULL,
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL,
    PRIMARY KEY (game_id, player_id)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id     TEXT NOT NULL REFERENCES games(id),
    seq         INTEGER NOT NULL,
    event       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE (game_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_id, seq);
"""


class Repository:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        # WAL makes the single writer / occasional readers cheap.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def create_game(
        self,
        game_id: str,
        num_rounds: int,
        pack_size: int,
        timeout: float,
        seed: int,
        now: float,
    ) -> None:
        await self._db.execute(
            "INSERT INTO games(id, status, num_rounds, pack_size, timeout, seed, created_at)"
            " VALUES (?, 'waiting', ?, ?, ?, ?, ?)",
            (game_id, num_rounds, pack_size, timeout, seed, now),
        )
        await self._db.commit()

    async def add_player(
        self, game_id: str, player_id: str, seat: int, name: str, token_hash: str
    ) -> None:
        await self._db.execute(
            "INSERT INTO players(game_id, player_id, seat, name, token_hash)"
            " VALUES (?, ?, ?, ?, ?)",
            (game_id, player_id, seat, name, token_hash),
        )
        await self._db.commit()

    async def get_game(self, game_id: str) -> Optional[aiosqlite.Row]:
        async with self._db.execute(
            "SELECT * FROM games WHERE id = ?", (game_id,)
        ) as cur:
            return await cur.fetchone()

    async def list_players(self, game_id: str) -> List[aiosqlite.Row]:
        async with self._db.execute(
            "SELECT * FROM players WHERE game_id = ? ORDER BY seat", (game_id,)
        ) as cur:
            return await cur.fetchall()

    async def find_player(
        self, game_id: str, token_hash: str
    ) -> Optional[aiosqlite.Row]:
        async with self._db.execute(
            "SELECT * FROM players WHERE game_id = ? AND token_hash = ?",
            (game_id, token_hash),
        ) as cur:
            return await cur.fetchone()

    async def append_events(
        self, game_id: str, events: List[Dict[str, Any]], now: float
    ) -> List[int]:
        """Persist a batch of events atomically.

        ``(game_id, seq)`` is UNIQUE; seq values are derived from the current
        count inside the same write transaction, so two concurrent resolves
        cannot interleave their batches.
        """
        if not events:
            return []
        seqs: List[int] = []
        try:
            await self._db.execute("BEGIN IMMEDIATE")
            async with self._db.execute(
                "SELECT COUNT(*) AS c FROM events WHERE game_id = ?", (game_id,)
            ) as cur:
                row = await cur.fetchone()
                seq = row["c"]
            for event in events:
                seq += 1
                await self._db.execute(
                    "INSERT INTO events(game_id, seq, event, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (game_id, seq, json.dumps(event, separators=(",", ":")), now),
                )
                seqs.append(seq)
            await self._db.execute(
                "UPDATE games SET status = CASE "
                " WHEN EXISTS (SELECT 1 FROM events WHERE game_id = ? "
                "              AND json_extract(event, '$.type') = 'game_completed')"
                " THEN 'completed' ELSE 'active' END WHERE id = ?",
                (game_id, game_id),
            )
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise
        return seqs

    async def load_events(self, game_id: str) -> List[Dict[str, Any]]:
        async with self._db.execute(
            "SELECT event FROM events WHERE game_id = ? ORDER BY seq", (game_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [json.loads(r["event"]) for r in rows]

    async def list_active_games(self) -> List[str]:
        async with self._db.execute(
            "SELECT id FROM games WHERE status = 'active'"
        ) as cur:
            rows = await cur.fetchall()
        return [r["id"] for r in rows]
