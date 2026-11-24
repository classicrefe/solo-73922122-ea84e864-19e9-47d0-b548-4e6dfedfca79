"""Unit tests for the deterministic Poisson bootstrap and percentile helper."""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bootstrap import ROUNDS, household_weights  # noqa: E402
from app.stats import percentile_type7  # noqa: E402


class BootstrapTests(unittest.TestCase):
    def test_round_count_and_nonneg_ints(self):
        w = household_weights("H001")
        self.assertEqual(len(w), ROUNDS)
        self.assertTrue(all(isinstance(x, int) and x >= 0 for x in w))

    def test_deterministic_across_calls(self):
        self.assertEqual(
            household_weights("H007"), household_weights("H007")
        )

    def test_distinct_households_distinct_streams(self):
        # astronomically unlikely to collide for 1000 rounds
        self.assertNotEqual(
            household_weights("H001"), household_weights("H002")
        )

    def test_other_households_do_not_affect_weights(self):
        # weights depend only on (seed, round, household_id): adding or
        # removing unrelated households must leave this stream untouched
        before = household_weights("KEEP-01")
        household_weights("NEW-A")
        household_weights("NEW-B")
        after = household_weights("KEEP-01")
        self.assertEqual(before, after)

    def test_mean_near_one_large_sample(self):
        # Poisson(1): pooled sample mean of many households x rounds should
        # be close to 1 and the empirical distribution should look Poisson(1)
        pool: list[int] = []
        for i in range(40):
            pool.extend(household_weights(f"POOL-{i:03d}"))
        self.assertEqual(len(pool), 40 * ROUNDS)
        mean = sum(pool) / len(pool)
        self.assertAlmostEqual(mean, 1.0, delta=0.05)
        # P(Pois(1) = 0) = e^-1 ~= 0.368
        zero_frac = sum(1 for x in pool if x == 0) / len(pool)
        self.assertAlmostEqual(zero_frac, math.exp(-1), delta=0.03)
        # P(Pois(1) >= 4) ~= 0.019
        ge4_frac = sum(1 for x in pool if x >= 4) / len(pool)
        self.assertLess(ge4_frac, 0.05)


class PercentileTests(unittest.TestCase):
    def test_type7_known_values(self):
        vals = sorted([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        # h = 9 * 0.025 = 0.225 -> 0.775*1 + 0.225*2 = 1.225
        self.assertAlmostEqual(percentile_type7(vals, 2.5), 1.225)
        self.assertAlmostEqual(percentile_type7(vals, 50), 5.5)
        # h = 9 * 0.975 = 8.775 -> 0.225*9 + 0.775*10 = 9.775
        self.assertAlmostEqual(percentile_type7(vals, 97.5), 9.775)

    def test_single_and_empty(self):
        self.assertEqual(percentile_type7([3.0], 2.5), 3.0)
        with self.assertRaises(ValueError):
            percentile_type7([], 2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
