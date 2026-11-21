"""Tests for 0.6.1: the wake photo is asked for more than once."""
import asyncio, sys, unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "custom_components" / "vector_wirepod"))  # after the stdlib: select.py is a platform
import photos


def run_first_frame(answers, tries, pause_s=3.0):
    """Feed `answers` to first_frame one per try. Returns (result, tries made, pauses taken)."""
    calls, pauses = [], []

    async def attempt():
        calls.append(1)
        return answers[len(calls) - 1] if len(calls) <= len(answers) else None

    async def sleep(seconds):
        pauses.append(seconds)

    result = asyncio.run(photos.first_frame(attempt, tries, pause_s, sleep))
    return result, len(calls), pauses


class FirstFrameTests(unittest.TestCase):
    def test_a_frame_on_the_first_try_asks_once_and_never_pauses(self):
        self.assertEqual(run_first_frame([b"jpeg"], 4), (b"jpeg", 1, []))

    def test_a_late_frame_is_found_after_empty_tries(self):
        self.assertEqual(run_first_frame([None, None, b"jpeg"], 4), (b"jpeg", 3, [3.0, 3.0]))

    def test_gives_up_after_the_stated_number_of_tries(self):
        result, calls, pauses = run_first_frame([None] * 10, 4)
        self.assertIsNone(result)
        self.assertEqual(calls, 4)
        self.assertEqual(pauses, [3.0, 3.0, 3.0])  # no pause after the last try

    def test_one_try_means_one_question(self):
        self.assertEqual(run_first_frame([None, b"jpeg"], 1), (None, 1, []))

    def test_the_last_try_still_counts(self):
        self.assertEqual(run_first_frame([None, None, None, b"jpeg"], 4), (b"jpeg", 4, [3.0, 3.0, 3.0]))

    def test_refuses_a_number_of_tries_out_of_range(self):
        for bad in (0, -1, photos.MAX_FRAME_TRIES + 1):
            with self.assertRaises(AssertionError):
                run_first_frame([b"jpeg"], bad)


if __name__ == "__main__":
    unittest.main()
