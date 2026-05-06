import importlib
import sys
import types
import unittest


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


class FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse()


requests_mod = types.ModuleType("requests")
requests_mod.Session = FakeSession
sys.modules.setdefault("requests", requests_mod)

api_client_mod = importlib.import_module("scripts.client.api_client")


class ClientLoggingTests(unittest.TestCase):
    def test_send_log_strips_image_payload_before_http_post(self):
        client = api_client_mod.PlannerApiClient(
            base_url="http://host",
            timeout=15.0,
            debug=False,
        )
        client.vm_id = "vm_1"

        raw_b64 = "x" * 1000
        client.send_log(
            level="debug",
            source="client",
            event="command_done",
            message="done",
            payload={"command_id": 9, "result": {"image_b64": raw_b64}},
        )

        _, kwargs = client.session.posts[-1]
        sent_payload = kwargs["json"]["payload"]

        self.assertEqual(
            sent_payload["result"]["image_b64"],
            "<base64 1000 chars>",
        )
        self.assertNotIn(raw_b64, str(kwargs["json"]))

    def test_send_log_uses_short_timeout_and_truncates_debug_payload(self):
        client = api_client_mod.PlannerApiClient(
            base_url="http://host",
            timeout=15.0,
            debug=True,
        )
        client.vm_id = "vm_1"

        long_traceback = "T" * 5000
        client.send_log(
            level="error",
            source="client",
            event="client_tick_failed",
            message="Unhandled exception in client loop",
            payload={"traceback": long_traceback},
        )

        _, kwargs = client.session.posts[-1]

        self.assertEqual(kwargs["timeout"], 2.0)
        self.assertEqual(
            kwargs["json"]["payload"]["traceback"],
            f"{long_traceback[:1000]}...<truncated 5000 chars>",
        )


if __name__ == "__main__":
    unittest.main()
