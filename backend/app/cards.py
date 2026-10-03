"""Card catalogue and deck shuffling.

Cards are plain integer ids mapped to human readable names.  A game draws
from a single shuffled deck; the position of the cursor is recorded in each
``round_opened`` event so replays reproduce exactly the same deals.
"""
from __future__ import annotations

import random
from typing import List

# A generous catalogue; 4 seats * pack_size fresh cards per round covers the
# default game with plenty of headroom.
CARDS: dict[int, str] = {i: f"Card-{i:03d}" for i in range(1, 101)}


def card_name(card_id: int) -> str:
    return CARDS[card_id]


def shuffled_deck(seed: int, size: int = 60) -> List[int]:
    """Deterministically shuffle ``size`` distinct cards for a seeded game."""
    rng = random.Random(seed)
    deck = list(CARDS)[:size]
    rng.shuffle(deck)
    return deck
