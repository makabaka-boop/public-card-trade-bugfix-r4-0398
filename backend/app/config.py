"""Application configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("DRAFT_DB_PATH", "/workspace/backend/draft.db")
    num_players: int = 4
    # Every active round hands its decision maker five cards; after a pick the
    # four leftovers rotate to the next seat and are topped up with one fresh
    # card from the shared deck.
    pack_size: int = 5
    default_rounds: int = int(os.environ.get("DRAFT_ROUNDS", "6"))
    default_timeout: float = float(os.environ.get("DRAFT_TIMEOUT", "45"))
    # When true the /admin endpoints (force timeout, full state dump) exist.
    # They never leak data to regular player connections.
    allow_admin: bool = os.environ.get("DRAFT_ALLOW_ADMIN", "1") == "1"


settings = Settings()
