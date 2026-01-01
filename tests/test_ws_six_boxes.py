import json
import unittest

from fastapi.testclient import TestClient

from escalada.main import app
from escalada.api import live as live_module
from escalada.api.live import Cmd, cmd, state_map, state_locks
from escalada.rate_limit import get_rate_limiter


class WebSocketSixBoxesTest(unittest.TestCase):
    def setUp(self):
        state_map.clear()
        state_locks.clear()
        live_module.VALIDATION_ENABLED = False
        rl = get_rate_limiter()
        rl.reset_all()
        rl.max_per_minute = 100000
        rl.max_per_second = 100000
        rl.block_duration = 0

    @unittest.skip("WS integration test is environment-sensitive; run manually against a live server.")
    def test_six_ws_channels_receive_events(self):
        client = TestClient(app)
        import time

        def recv_until(ws, wanted: set[str], max_steps: int = 10):
            """Receive messages until one with type in wanted is found. Respond to PING."""
            for _ in range(max_steps):
                msg = ws.receive_text()
                payload = json.loads(msg)
                t = payload.get("type")
                if t == "PING":
                    # Reply with PONG to keep heartbeat happy
                    ts = payload.get("timestamp", time.time())
                    ws.send_text(json.dumps({"type": "PONG", "timestamp": ts}))
                    continue
                if t in wanted:
                    return payload
            raise AssertionError(f"Did not receive any of {wanted}")

        # Sequentially open a WS per box to validate channel behavior and broadcasts
        for box_id in range(1, 7):
            with client.websocket_connect(f"/api/ws/{box_id}") as ws:
                # Expect initial snapshot or PINGs
                _ = recv_until(ws, {"STATE_SNAPSHOT"})

                # INIT_ROUTE via HTTP and expect echo on this channel
                init_payload = {
                    "boxId": box_id,
                    "type": "INIT_ROUTE",
                    "routeIndex": 1,
                    "holdsCount": 5,
                    "competitors": [{"nume": f"A_{box_id}"}, {"nume": f"B_{box_id}"}],
                    "timerPreset": "05:00",
                }
                r = client.post("/api/cmd", json=init_payload)
                assert r.status_code == 200
                payload = recv_until(ws, {"INIT_ROUTE"})
                self.assertEqual(int(payload.get("boxId")), box_id)

                # START_TIMER and expect START_TIMER on this channel
                r = client.post("/api/cmd", json={"boxId": box_id, "type": "START_TIMER"})
                assert r.status_code == 200
                payload = recv_until(ws, {"START_TIMER"})
                self.assertEqual(int(payload.get("boxId")), box_id)


if __name__ == "__main__":
    unittest.main()
