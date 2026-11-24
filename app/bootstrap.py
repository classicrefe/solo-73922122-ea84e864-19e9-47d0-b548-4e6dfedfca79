"""Fixed-seed Poisson bootstrap.

For each of the ``ROUNDS`` rounds every household independently receives a
Poisson(mean=1) weight.  A weight is a deterministic function of (master
seed, round index, household id), so:

* the same household keeps the same weight across re-runs and across both
  the original and the corrected table (shared weights);
* adding, removing or re-ordering other households never changes it.

The generator is a SplitMix64 PRNG seeded with a 64-bit digest of
(MASTER_SEED, round, household_id), followed by Knuth's Poisson sampling
algorithm for lambda = 1.  No third-party libraries are required.
"""

from __future__ import annotations

import hashlib

MASTER_SEED = 20261123
ROUNDS = 1000
_MASK64 = (1 << 64) - 1


def _seed_state(round_index: int, household_id: str) -> int:
    digest = hashlib.blake2b(
        f"{MASTER_SEED}|{round_index}|{household_id}".encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "little")


def _splitmix64(state: int) -> tuple[int, int]:
    state = (state + 0x9E3779B97F4A7C15) & _MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    z = z ^ (z >> 31)
    return state, z


def _poisson_one(state: int) -> tuple[int, int]:
    """Draw one Poisson(lambda=1) via Knuth's algorithm."""
    import math

    boundary = math.exp(-1.0)
    product = 1.0
    k = 0
    while product >= boundary:
        k += 1
        state, z = _splitmix64(state)
        product *= (z >> 11) / float(1 << 53)
    return k - 1, state


def household_weights(household_id: str, rounds: int = ROUNDS) -> list[int]:
    """Return the ``rounds`` Poisson weights of one household."""
    state = _seed_state(0, household_id)
    weights: list[int] = []
    for round_index in range(rounds):
        state = _seed_state(round_index, household_id)
        weight, _ = _poisson_one(state)
        weights.append(weight)
    return weights
