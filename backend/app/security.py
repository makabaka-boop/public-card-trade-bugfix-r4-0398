"""Player token handling.

Tokens are opaque random strings handed back exactly once over HTTP/join.
Only SHA-256 hashes are stored, so reading the SQLite file cannot turn a
stolen row into a playable session.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_TOKEN_BYTES = 32


def generate_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)
