"""Tests for sleepcycle.py and photos.py (no Home Assistant needed)."""
import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "custom_components" / "vector_wirepod"))  # after the stdlib: select.py is a platform
import photos
import sleepcycle as sc


class CycleTests(unittest.TestCase):
    def test_idle_ignores_everything(self):
        c = sc.SleepCycle()
        for reading in (True, False, None):
            self.assertEqual(c.observe(reading, 10), [])
        self.assertEqual(c.phase, sc.IDLE)

    def test_request_lifts_quiet_then_asks_for_sleep(self):
        c = sc.SleepCycle()
        self.assertEqual(c.request_sleep(0), [sc.ACT_QUIET_OFF, sc.ACT_SLEEP])
        self.assertEqual(c.phase, sc.GOING)

    def test_sleep_runs_until_woken(self):
        c = sc.SleepCycle(quiet_hold_s=600)
        c.request_sleep(0)
        self.assertEqual(c.observe(True, 20), []); self.assertEqual(c.phase, sc.SLEEPING)
        for t in range(40, 40000, 2000):                       # hours asleep: nothing happens
            self.assertEqual(c.observe(True, t), [])
        self.assertEqual(c.phase, sc.SLEEPING)

    def test_wake_starts_quiet_then_returns_to_sleep(self):
        c = sc.SleepCycle(quiet_hold_s=600)
        c.request_sleep(0); c.observe(True, 20)
        self.assertEqual(c.observe(False, 1000), [sc.ACT_QUIET_ON])
        self.assertEqual((c.phase, c.wakes), (sc.WOKEN, 1))
        self.assertEqual(c.quiet_seconds_left(1100), 500)
        self.assertEqual(c.observe(False, 1599), [])              # one second early: still quiet
        self.assertEqual(c.observe(False, 1600), [sc.ACT_QUIET_OFF, sc.ACT_SLEEP])
        self.assertEqual(c.phase, sc.GOING)
        self.assertEqual(c.observe(True, 1630), []); self.assertEqual(c.phase, sc.SLEEPING)
        self.assertEqual(c.observe(False, 5000), [sc.ACT_QUIET_ON]); self.assertEqual(c.wakes, 2)

    def test_photo_is_taken_before_quiet_mode(self):
        c = sc.SleepCycle(photo_on_wake=True)
        c.request_sleep(0); c.observe(True, 20)
        self.assertEqual(c.observe(False, 100), [sc.ACT_PHOTO, sc.ACT_QUIET_ON])

    def test_unknown_readings_change_nothing(self):
        c = sc.SleepCycle()
        c.request_sleep(0); c.observe(True, 20)
        for t in (100, 200, 300):
            self.assertEqual(c.observe(None, t), [])
        self.assertEqual((c.phase, c.wakes), (sc.SLEEPING, 0))

    def test_sleep_request_not_taken_gives_up(self):
        c = sc.SleepCycle(settle_s=180)
        c.request_sleep(0)
        self.assertEqual(c.observe(False, 180), []); self.assertEqual(c.phase, sc.GOING)
        self.assertEqual(c.observe(False, 181), []); self.assertEqual(c.phase, sc.IDLE)
        self.assertIn("did not go to sleep", c.note)

    def test_falling_asleep_during_quiet_changes_nothing(self):
        c = sc.SleepCycle()
        c.request_sleep(0); c.observe(True, 20); c.observe(False, 100)
        self.assertEqual(c.observe(True, 150), []); self.assertEqual(c.phase, sc.SLEEPING)

    def test_cancel_stops_the_cycle_without_acting(self):
        c = sc.SleepCycle()
        c.request_sleep(0); c.observe(True, 20); c.observe(False, 100)
        self.assertEqual(c.cancel(120, "quiet mode set by hand"), [])
        self.assertEqual(c.phase, sc.IDLE)
        self.assertEqual(c.observe(False, 5000), [])

    def test_quiet_seconds_left_only_while_woken(self):
        c = sc.SleepCycle()
        self.assertIsNone(c.quiet_seconds_left(0))
        c.request_sleep(0); self.assertIsNone(c.quiet_seconds_left(5))


JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100 + b"\xff\xd9"


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = photos.photo_dir(self.tmp.name, "0DD1CAFE")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dir_is_per_robot_under_www(self):
        self.assertTrue(str(self.dir).endswith("www/vector_wirepod/0dd1cafe"))
        with self.assertRaises(AssertionError):
            photos.photo_dir(self.tmp.name, "../etc")

    def test_save_and_latest(self):
        when = datetime(2026, 11, 19, 21, 30, 5, tzinfo=timezone.utc)
        p = photos.save_photo(self.dir, JPEG, "wake", when)
        self.assertEqual(p.name, "20261119T213005Z-wake.jpg")
        self.assertEqual(p.read_bytes(), JPEG)
        self.assertEqual(photos.latest(self.dir), p)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_rejects_non_jpeg_and_bad_reason(self):
        for bad in (b"", b"hello", JPEG[:-2], b"\x00" + JPEG):
            with self.assertRaises(ValueError):
                photos.save_photo(self.dir, bad, "manual")
        with self.assertRaises(AssertionError):
            photos.save_photo(self.dir, JPEG, "../x")
        self.assertEqual(photos.list_photos(self.dir), [])

    def test_prunes_to_newest_and_leaves_other_files(self):
        self.dir.mkdir(parents=True)
        (self.dir / "notes.txt").write_text("keep me")
        base = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(7):
            photos.save_photo(self.dir, JPEG, "manual", base + timedelta(seconds=i), keep=3)
        names = [p.name for p in photos.list_photos(self.dir)]
        self.assertEqual(names, ["20261119T120004Z-manual.jpg", "20261119T120005Z-manual.jpg", "20261119T120006Z-manual.jpg"])
        self.assertTrue((self.dir / "notes.txt").exists())

    def test_latest_of_empty_folder_is_none(self):
        self.assertIsNone(photos.latest(self.dir))


if __name__ == "__main__":
    unittest.main()
