"""Check exact scheduling proof paths on small, independently known instances."""
import unittest

from tools.sample_oracle import exact_oracle


class OracleTests(unittest.TestCase):
    def test_empty_failing_set_costs_nothing(self):
        result = exact_oracle({})
        self.assertEqual(result["minimum_cost_ms"], 0)
        self.assertEqual(result["workers"], [])

    def test_worker_startup_can_make_serial_execution_optimal(self):
        result = exact_oracle({7: 1963, 11: 1801})
        self.assertEqual(result["minimum_cost_ms"], 5764)
        self.assertEqual(len(result["workers"]), 1)

    def test_search_improves_lpt_when_lower_bound_is_attainable(self):
        result = exact_oracle(dict(enumerate([3000, 3000, 2000, 2000, 2000])))
        self.assertEqual(result["minimum_cost_ms"], 10000)
        self.assertEqual(sorted(result["worker_loads_ms"]), [6000, 6000])
        self.assertGreater(result["proof"]["exact_capacity_checks"], 0)

    def test_unattainable_basic_bound_is_not_claimed_exact(self):
        result = exact_oracle(dict(enumerate([4000, 4000, 4000, 3000, 3000])))
        self.assertEqual(result["basic_lower_bound_ms"], 12000)
        self.assertEqual(result["minimum_cost_ms"], 13000)
        self.assertEqual(result["proof"]["method"], "all competitive worker counts resolved exactly")
        self.assertGreater(result["proof"]["exact_capacity_checks"], 0)

    def test_zero_duration_failures_still_require_one_worker(self):
        result = exact_oracle({4: 0, 9: 0, 31: 0})
        self.assertEqual(result["minimum_cost_ms"], 2000)
        self.assertEqual(sorted(result["workers"][0]), [4, 9, 31])


if __name__ == "__main__":
    unittest.main()
