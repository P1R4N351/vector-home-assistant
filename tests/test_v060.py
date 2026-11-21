"""Tests for the 0.6.0 additions: who made the robot, and the face backlight calls."""
import asyncio, sys, unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "custom_components" / "vector_wirepod"))  # after the stdlib: select.py is a platform
import aiohttp
from aiohttp import web
import hardware
import wirepod


class HardwareTests(unittest.TestCase):
    def test_frame_shape_decides(self):
        self.assertEqual(hardware.maker_and_model("00c0ffee", (1280, 720)), ("Anki", "Vector"))
        self.assertEqual(hardware.maker_and_model("00c0ffee", (640, 360)), ("Anki", "Vector"))
        self.assertEqual(hardware.maker_and_model("0dd1cafe", (800, 600)), ("Digital Dream Labs", "Vector 2.0"))

    def test_a_frame_wins_over_the_serial_guess(self):
        self.assertEqual(hardware.generation("0dd1cafe", (1280, 720)), hardware.GEN_1)
        self.assertEqual(hardware.generation("00c0ffee", (800, 600)), hardware.GEN_2)

    def test_serial_is_used_until_a_frame_is_seen(self):
        self.assertEqual(hardware.maker_and_model("0DD1CAFE"), ("Digital Dream Labs", "Vector 2.0"))
        self.assertEqual(hardware.maker_and_model("00c0ffee"), ("Anki", "Vector"))
        self.assertEqual(hardware.maker_and_model("00c0ffee", (0, 0)), ("Anki", "Vector"))


class BacklightTests(unittest.TestCase):
    def test_url(self):
        self.assertEqual(wirepod.build_url("http://pod:8080", "/api-sdk/backlight", "0dd1cafe"),
                         "http://pod:8080/api-sdk/backlight?serial=0dd1cafe")
        self.assertEqual(wirepod.build_url("http://pod:8080", "/api-sdk/backlight", "0dd1cafe", level=0),
                         "http://pod:8080/api-sdk/backlight?serial=0dd1cafe&level=0")
        for bad in (-1, 21, True, 1.5, "5"):
            with self.assertRaises(ValueError):
                wirepod.build_url("http://pod:8080", "/api-sdk/backlight", "0dd1cafe", level=bad)
        with self.assertRaises(ValueError):
            wirepod.build_url("http://pod:8080", "/api-sdk/quiet_mode", "0dd1cafe", level=5)

    def test_get_and_set_over_http(self):
        seen = []
        state = {"level": 10}

        async def handler(request):
            serial = request.query.get("serial")
            seen.append((request.method, serial, request.query.get("level")))
            if serial == "bbbb0000":
                return web.json_response({"ok": False, "error": "robot_ssh_not_configured"}, status=503)
            if serial == "cccc0000":
                return web.json_response({"ok": False, "error": "robot_ssh_failed"}, status=502)
            if serial == "dddd0000":
                return web.json_response({"ok": True, "level": "ten"})
            if serial == "eeee0000":
                return web.Response(status=404, text="404 page not found")
            if request.method == "POST":
                state["level"] = int(request.query["level"])
            return web.json_response({"ok": True, "serial": serial, "level": state["level"], "left": state["level"],
                                      "right": state["level"], "max": 20})

        async def run():
            app = web.Application(); app.router.add_route("*", "/api-sdk/backlight", handler)
            runner = web.AppRunner(app); await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
            base = "http://127.0.0.1:%d" % runner.addresses[0][1]
            out = {}
            async with aiohttp.ClientSession() as session:
                out["first"] = await wirepod.get_backlight(session, base, "aaaa0000")
                out["off"] = await wirepod.set_backlight(session, base, "aaaa0000", 0)
                out["after"] = await wirepod.get_backlight(session, base, "aaaa0000")
                out["no_login"] = await wirepod.get_backlight(session, base, "bbbb0000")
                out["no_endpoint"] = await wirepod.get_backlight(session, base, "eeee0000")
                out["errors"] = []
                for serial in ("cccc0000", "dddd0000"):
                    try:
                        await wirepod.get_backlight(session, base, serial)
                    except wirepod.WirePodError as exc:
                        out["errors"].append(str(exc))
            await runner.cleanup()
            return out

        out = asyncio.run(run())
        self.assertEqual((out["first"], out["off"], out["after"]), (10, 0, 0))
        self.assertIsNone(out["no_login"]); self.assertIsNone(out["no_endpoint"])
        self.assertEqual(len(out["errors"]), 2); self.assertIn("502", out["errors"][0])
        self.assertEqual(seen[:3], [("GET", "aaaa0000", None), ("POST", "aaaa0000", "0"), ("GET", "aaaa0000", None)])


if __name__ == "__main__":
    unittest.main()
