"""Tests for the 0.7.0 addition: the microphone mute calls."""
import asyncio, sys, unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "custom_components" / "vector_wirepod"))  # after the stdlib: select.py is a platform
import aiohttp
from aiohttp import web
import wirepod

POD = "http://pod:8080"


class MicMuteUrlTests(unittest.TestCase):
    def test_url(self):
        self.assertEqual(wirepod.build_url(POD, "/api-sdk/mic_mute", "0dd1cafe"),
                         "http://pod:8080/api-sdk/mic_mute?serial=0dd1cafe")
        self.assertEqual(wirepod.build_url(POD, "/api-sdk/mic_mute", "0dd1cafe", muted=True),
                         "http://pod:8080/api-sdk/mic_mute?serial=0dd1cafe&muted=true")
        self.assertEqual(wirepod.build_url(POD, "/api-sdk/mic_mute", "0dd1cafe", muted=False),
                         "http://pod:8080/api-sdk/mic_mute?serial=0dd1cafe&muted=false")

    def test_muted_only_on_mic_mute_and_only_a_bool(self):
        for bad in (1, 0, "true", None.__class__):
            with self.assertRaises(ValueError):
                wirepod.build_url(POD, "/api-sdk/mic_mute", "0dd1cafe", muted=bad)
        for path in ("/api-sdk/backlight", "/api-sdk/quiet_mode", "/api-sdk/robot_state"):
            with self.assertRaises(ValueError):
                wirepod.build_url(POD, path, "0dd1cafe", muted=True)
        with self.assertRaises(ValueError):
            wirepod.build_url(POD, "/api-sdk/mic_mute", None, muted=True)


class MicMuteHttpTests(unittest.TestCase):
    def test_get_and_set_over_http(self):
        seen = []
        state = {"muted": False}

        async def handler(request):
            serial = request.query.get("serial")
            seen.append((request.method, serial, request.query.get("muted")))
            if serial == "bbbb0000":
                return web.json_response({"ok": False, "error": "robot_ssh_not_configured"}, status=503)
            if serial == "eeee0000":
                return web.Response(status=404, text="404 page not found")
            if serial == "cccc0000":
                return web.json_response({"ok": False, "error": "restart_failed", "muted": True}, status=502)
            if serial == "dddd0000":
                return web.json_response({"ok": True, "muted": "yes", "restarting": False})
            if serial == "ffff0000":
                return web.json_response({"ok": True, "muted": True})
            if serial == "abab0000":
                return web.Response(status=500, text="oops")
            restarting = False
            if request.method == "POST":
                want = request.query["muted"] == "true"
                restarting = want != state["muted"]
                state["muted"] = want
            return web.json_response({"ok": True, "serial": serial, "muted": state["muted"], "restarting": restarting})

        async def run():
            app = web.Application(); app.router.add_route("*", "/api-sdk/mic_mute", handler)
            runner = web.AppRunner(app); await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
            base = "http://127.0.0.1:%d" % runner.addresses[0][1]
            out = {}
            async with aiohttp.ClientSession() as session:
                out["first"] = await wirepod.get_mic_mute(session, base, "aaaa0000")
                out["mute"] = await wirepod.set_mic_mute(session, base, "aaaa0000", True)
                out["again"] = await wirepod.set_mic_mute(session, base, "aaaa0000", True)
                out["after"] = await wirepod.get_mic_mute(session, base, "aaaa0000")
                out["unmute"] = await wirepod.set_mic_mute(session, base, "aaaa0000", False)
                out["no_login"] = await wirepod.get_mic_mute(session, base, "bbbb0000")
                out["no_endpoint"] = await wirepod.set_mic_mute(session, base, "eeee0000", True)
                out["errors"] = []
                for serial in ("cccc0000", "dddd0000", "ffff0000", "abab0000"):
                    try:
                        await wirepod.set_mic_mute(session, base, serial, True)
                        out["errors"].append(None)
                    except wirepod.WirePodError as exc:
                        out["errors"].append(str(exc))
            await runner.cleanup()
            return out

        out = asyncio.run(run())
        self.assertEqual(out["first"], {"muted": False, "restarting": False})
        self.assertEqual(out["mute"], {"muted": True, "restarting": True})
        self.assertEqual(out["again"], {"muted": True, "restarting": False})
        self.assertEqual(out["after"], {"muted": True, "restarting": False})
        self.assertEqual(out["unmute"], {"muted": False, "restarting": True})
        self.assertIsNone(out["no_login"]); self.assertIsNone(out["no_endpoint"])
        self.assertNotIn(None, out["errors"])
        self.assertIn("restart_failed", out["errors"][0]); self.assertIn("502", out["errors"][0])
        self.assertIn("invalid answer", out["errors"][1]); self.assertIn("invalid answer", out["errors"][2])
        self.assertIn("500", out["errors"][3])
        self.assertEqual(seen[:5], [("GET", "aaaa0000", None), ("POST", "aaaa0000", "true"), ("POST", "aaaa0000", "true"),
                                    ("GET", "aaaa0000", None), ("POST", "aaaa0000", "false")])


if __name__ == "__main__":
    unittest.main()
