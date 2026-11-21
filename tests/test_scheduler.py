"""Checks for the scoring tradeoff and the scheduler's submission invariants."""

import unittest

from scheduler import cost_ms, schedule


def catalog(seconds):
    return [{"number": number, "seconds": duration} for number, duration in enumerate(seconds)]


class SchedulerTests(unittest.TestCase):
    def test_empty_selection_has_no_cost_or_workers(self):
        self.assertEqual(schedule([]), [])
        self.assertEqual(cost_ms([], []), 0)

    def test_worker_overhead_makes_one_worker_best_for_short_tests(self):
        tests = catalog([1, 1, 1])
        workers = schedule(tests)
        self.assertEqual(len(workers), 1)
        self.assertEqual(cost_ms(workers, tests), 5000)

    def test_parallelism_pays_for_long_tests(self):
        tests = catalog([10, 10])
        workers = schedule(tests)
        self.assertEqual(len(workers), 2)
        self.assertEqual(cost_ms(workers, tests), 14000)

    def test_eight_workers_are_available_when_they_save_time(self):
        tests = catalog([100] * 8)
        workers = schedule(tests)
        self.assertEqual(len(workers), 8)
        self.assertEqual(cost_ms(workers, tests), 116000)

    def test_tied_cost_prefers_fewer_workers(self):
        # One worker: 4 + 2 seconds. Two workers: 2 + 4 seconds.
        tests = catalog([2, 2])
        self.assertEqual(len(schedule(tests)), 1)

    def test_empty_workers_do_not_add_overhead(self):
        tests = catalog([0.029, 1.001, 2.003])
        self.assertEqual(cost_ms([[0, 1], [], [2]], tests), 6003)

    def test_half_millisecond_rounds_up_like_the_actual_scorer(self):
        self.assertEqual(cost_ms([[0]], catalog([0.0005])), 2001)
        self.assertEqual(cost_ms([[0]], catalog([0.0025])), 2003)

    def test_nonuniform_tests_are_unique_and_input_order_independent(self):
        tests = catalog([0, 0.002, 1, 3, 5, 8, 13, 21, 34, 55, 89])
        workers = schedule(tests)
        selected = [number for worker in workers for number in worker]
        self.assertEqual(sorted(selected), list(range(len(tests))))
        self.assertEqual(len(selected), len(set(selected)))
        self.assertLessEqual(len(workers), 8)
        self.assertTrue(all(workers))
        self.assertEqual(workers, schedule(list(reversed(tests))))
        self.assertLessEqual(cost_ms(workers, tests), cost_ms([selected], tests))

    def test_duplicate_input_is_reported_instead_of_invalid_output(self):
        with self.assertRaisesRegex(ValueError, "duplicate test number"):
            schedule([{"number": 7, "seconds": 1}, {"number": 7, "seconds": 1}])


if __name__ == "__main__":
    unittest.main()
