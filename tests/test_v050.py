"""Tests for the 0.5.0 additions that need aiohttp and Pillow."""
import asyncio, io, sys, unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "custom_components" / "vector_wirepod"))  # after the stdlib: select.py is a platform
import aiohttp
from aiohttp import web
from PIL import Image
import placeholder
import wirepod


def jpeg(width, height):
    out = io.BytesIO(); Image.new("RGB", (width, height), (90, 120, 150)).save(out, format="JPEG"); return out.getvalue()


WIDE, TALL = jpeg(640, 360), jpeg(640, 480)       # Vector 1.0 shape, Vector 2.0 shape
GOOD = {"ok": True, "serial": "0dd1cafe", "status": 1, "asleep": True, "on_charger": True, "charging": False,
        "picked_up": False, "being_held": False, "button_pressed": False, "moving": False,
        "animating": False, "cliff_detected": False}


class ShapeTests(unittest.TestCase):
    def test_jpeg_size_reads_both_shapes(self):
        self.assertEqual(wirepod.jpeg_size(WIDE), (640, 360))
        self.assertEqual(wirepod.jpeg_size(TALL), (640, 480))
        self.assertEqual(wirepod.jpeg_size(jpeg(1280, 720)), (1280, 720))

    def test_jpeg_size_refuses_what_is_not_a_jpeg(self):
        for bad in (None, b"", b"hello world", WIDE[:20], b"\xff\xd8" + b"\x00" * 40):
            self.assertIsNone(wirepod.jpeg_size(bad))

    def test_status_image_keeps_the_frame_shape(self):
        for frame, want in ((WIDE, (640, 360)), (TALL, (640, 480)), (jpeg(1280, 960), (640, 480))):
            self.assertEqual(Image.open(io.BytesIO(placeholder.render(["a"], frame))).size, want)

    def test_card_without_a_frame_uses_the_known_shape(self):
        size = lambda **kw: Image.open(io.BytesIO(placeholder.render(["a", "b"], **kw))).size
        self.assertEqual(size(), (640, 360))
        self.assertEqual(size(size=(640, 480)), (640, 480))
        self.assertEqual(size(frame=b"not a jpeg", size=(640, 480)), (640, 480))
        self.assertEqual(size(frame=TALL, size=(640, 360)), (640, 480), "a real frame wins over a remembered shape")

    def test_canvas_size_guards(self):
        self.assertEqual(placeholder.canvas_size(None), (640, 360))
        self.assertEqual(placeholder.canvas_size((0, 5)), (640, 360))
        self.assertEqual(placeholder.canvas_size((100, 100000)), (640, placeholder.MAX_HEIGHT))


class HubTests(unittest.TestCase):
    def hub(self):
        return wirepod.CameraHub(object(), wirepod.HubConfig("http://pod:8080", "0dd1cafe"))

    def test_frame_size_follows_the_newest_frame_and_remembers(self):
        hub = self.hub()
        self.assertIsNone(hub.frame_size())
        hub.seed_frame_size((640, 360)); self.assertEqual(hub.frame_size(), (640, 360))
        hub._publish(TALL); self.assertEqual(hub.frame_size(), (640, 480))
        hub.seed_frame_size((640, 360)); self.assertEqual(hub.frame_size(), (640, 480), "a seed never replaces a real shape")
        hub._publish(WIDE); self.assertEqual(hub.frame_size(), (640, 360))

    def test_retry_now_clears_the_wait(self):
        hub = self.hub()
        hub._record_failure("no frames"); hub._record_failure("no frames")
        self.assertFalse(hub.can_start()); self.assertEqual(hub.state, wirepod.STATE_NO_FRAMES)
        hub.retry_now()
        self.assertTrue(hub.can_start()); self.assertEqual(hub.state, wirepod.STATE_IDLE)


class AsleepTests(unittest.TestCase):
    def hub(self):
        return wirepod.CameraHub(object(), wirepod.HubConfig("http://pod:8080", "0dd1cafe"))

    def test_nothing_is_opened_while_the_robot_sleeps(self):
        hub = self.hub()
        self.assertTrue(hub.can_start())
        hub.set_robot_asleep(True)
        self.assertFalse(hub.can_start()); self.assertEqual(hub.state, wirepod.STATE_ASLEEP)
        hub.retry_now()
        self.assertFalse(hub.can_start(), "a retry request does not override sleep")
        self.assertEqual(hub.sessions_opened, 0)
        hub.set_robot_asleep(False)
        self.assertTrue(hub.can_start()); self.assertEqual(hub.state, wirepod.STATE_IDLE)

    def test_waking_clears_an_old_wait(self):
        hub = self.hub()
        hub._record_failure("no frames")
        hub.set_robot_asleep(True); hub.set_robot_asleep(False)
        self.assertTrue(hub.can_start())

    def test_sleep_does_not_hide_a_withheld_or_unreachable_camera(self):
        hub = self.hub()
        hub.set_allowed(False); hub.set_robot_asleep(True)
        self.assertEqual(hub.state, wirepod.STATE_WITHHELD)
        hub.set_allowed(True)
        self.assertEqual(hub.state, wirepod.STATE_ASLEEP)
        hub.set_robot_reachable(False, "gone")
        self.assertEqual(hub.state, wirepod.STATE_ROBOT_UNREACHABLE)


class RobotStateTests(unittest.TestCase):
    def test_normalize(self):
        out = wirepod._normalize_robot_state(dict(GOOD))
        self.assertEqual(set(out), set(wirepod.ROBOT_STATE_FLAGS)); self.assertTrue(out["asleep"])
        for bad in ({**GOOD, "ok": False}, {k: v for k, v in GOOD.items() if k != "asleep"},
                    {**GOOD, "asleep": 1}, [], "x"):
            with self.assertRaises(wirepod.WirePodError):
                wirepod._normalize_robot_state(bad)

    def test_url_is_allowed_and_takes_no_extras(self):
        self.assertEqual(wirepod.build_url("http://pod:8080", "/api-sdk/robot_state", "0dd1cafe"),
                         "http://pod:8080/api-sdk/robot_state?serial=0dd1cafe")
        with self.assertRaises(ValueError):
            wirepod.build_url("http://pod:8080", "/api-sdk/robot_state", "0dd1cafe", intent=wirepod.INTENT_SLEEP)
        with self.assertRaises(ValueError):
            wirepod.build_url("http://pod:8080", "/api-sdk/move_wheels", "0dd1cafe")

    def test_get_robot_state_over_http(self):
        seen = []

        async def handler(request):
            seen.append((request.method, request.path, dict(request.query)))
            serial = request.query.get("serial")
            if serial == "aaaa0000":
                return web.json_response(GOOD)
            if serial == "bbbb0000":
                return web.Response(status=404, text="404 page not found")
            if serial == "cccc0000":
                return web.json_response({"ok": False, "error": "robot_state_unavailable"}, status=504)
            return web.Response(status=200, text="error: robot not found")

        async def run():
            app = web.Application(); app.router.add_route("*", "/api-sdk/robot_state", handler)
            runner = web.AppRunner(app); await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
            base = "http://127.0.0.1:%d" % runner.addresses[0][1]
            async with aiohttp.ClientSession() as session:
                good = await wirepod.get_robot_state(session, base, "aaaa0000")
                absent = await wirepod.get_robot_state(session, base, "bbbb0000")
                errors = []
                for serial in ("cccc0000", "dddd0000"):
                    try:
                        await wirepod.get_robot_state(session, base, serial)
                    except wirepod.WirePodError as exc:
                        errors.append(str(exc))
            await runner.cleanup()
            return good, absent, errors

        good, absent, errors = asyncio.run(run())
        self.assertTrue(good["asleep"]); self.assertIsNone(absent)
        self.assertEqual(len(errors), 2); self.assertIn("504", errors[0]); self.assertIn("not JSON", errors[1])
        self.assertEqual({m for m, _, _ in seen}, {"GET"}, "robot state is only ever read")


if __name__ == "__main__":
    unittest.main()
