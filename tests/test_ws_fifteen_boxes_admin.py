import json
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from escalada.api import live as live_module
from escalada.api.auth import router as auth_router
from escalada.api.live import router as live_router
from escalada.auth.service import create_access_token
from escalada.rate_limit import get_rate_limiter


def _build_test_app() -> FastAPI:
    """
    Build a minimal FastAPI app for integration-style tests.

    We intentionally do not import `escalada.main` because its logging configuration writes
    to `escalada.log`, which is not permitted in some sandboxed test environments.
    """
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(live_router, prefix="/api")
    return app


def _recv_until(ws, wanted: set[str], max_steps: int = 50) -> dict:
    """
    Receive messages until we get a payload with type in `wanted`.
    Respond to PING frames to keep the heartbeat alive.
    """
    for _ in range(max_steps):
        raw = ws.receive_text()
        payload = json.loads(raw)
        t = payload.get("type")
        if t == "PING":
            ts = payload.get("timestamp", time.time())
            ws.send_text(json.dumps({"type": "PONG", "timestamp": ts}))
            continue
        if t in wanted:
            return payload
    raise AssertionError(f"Did not receive any of {wanted} within {max_steps} messages")


class WebSocketFifteenBoxesAdminTest(unittest.TestCase):
    """
    "Real" integration-style test:
    - authenticates as admin (cookie JWT)
    - opens 15 WS subscriptions (one per box, like ControlPanel does)
    - drives a small contest flow over HTTP (/api/cmd)
    - asserts WS echoes + authoritative snapshots update correctly per box
    """

    def setUp(self):
        # Reset global state and relax rate limits so the test focuses on correctness.
        live_module.state_map.clear()
        live_module.state_locks.clear()
        live_module.channels.clear()
        live_module.public_channels.clear()
        live_module.VALIDATION_ENABLED = True

        # In this sandboxed environment, tests are not allowed to write to the repo filesystem
        # (e.g. `data/boxes/*.json`), so we stub persistence/audit to keep the test "real"
        # (auth + WS + /api/cmd) while remaining in-memory.
        self._orig_save_box_state = getattr(live_module, "save_box_state", None)
        self._orig_append_audit_event = getattr(live_module, "append_audit_event", None)

        async def _noop_async(*_args, **_kwargs):
            return None

        live_module.save_box_state = _noop_async
        live_module.append_audit_event = _noop_async
        rl = get_rate_limiter()
        rl.reset_all()
        rl.max_per_minute = 100000
        rl.max_per_second = 100000
        rl.block_duration = 0

    def tearDown(self):
        # Restore patched persistence hooks.
        if getattr(self, "_orig_save_box_state", None) is not None:
            live_module.save_box_state = self._orig_save_box_state
        if getattr(self, "_orig_append_audit_event", None) is not None:
            live_module.append_audit_event = self._orig_append_audit_event

    def test_admin_can_drive_15_boxes_over_ws(self):
        app = _build_test_app()
        client = TestClient(app)

        # Authenticate as admin via cookie (same mechanism as the UI).
        token = create_access_token(username="Admin Test", role="admin", assigned_boxes=[])
        client.cookies.set("escalada_token", token)

        # Open 15 authenticated WS connections (one per box).
        sockets = []
        try:
            for box_id in range(1, 16):
                ws_cm = client.websocket_connect(f"/api/ws/{box_id}")
                ws = ws_cm.__enter__()
                sockets.append((ws_cm, ws))
                snap = _recv_until(ws, {"STATE_SNAPSHOT"})
                self.assertEqual(int(snap.get("boxId")), box_id)

            # INIT_ROUTE on all boxes and verify each box gets its own INIT + snapshot.
            session_ids: dict[int, str] = {}
            box_versions: dict[int, int] = {}
            for box_id in range(1, 16):
                init_payload = {
                    "boxId": box_id,
                    "type": "INIT_ROUTE",
                    "routeIndex": 1,
                    "holdsCount": 10,
                    "competitors": [{"nume": f"A_{box_id}"}, {"nume": f"B_{box_id}"}],
                    "timerPreset": "05:00",
                    "categorie": f"Cat_{box_id}",
                }
                r = client.post("/api/cmd", json=init_payload)
                self.assertEqual(r.status_code, 200)

                ws = sockets[box_id - 1][1]
                echo = _recv_until(ws, {"INIT_ROUTE"})
                self.assertEqual(int(echo.get("boxId")), box_id)
                snap = _recv_until(ws, {"STATE_SNAPSHOT"})
                self.assertTrue(snap.get("initiated"))
                self.assertEqual(snap.get("currentClimber"), f"A_{box_id}")
                session_ids[box_id] = snap.get("sessionId")
                box_versions[box_id] = int(snap.get("boxVersion") or 0)

            # START_TIMER then RESET_PARTIAL(resetTimer=True) while running.
            for box_id in range(1, 16):
                sid = session_ids[box_id]
                self.assertTrue(sid)

                r = client.post(
                    "/api/cmd",
                    json={
                        "boxId": box_id,
                        "type": "START_TIMER",
                        "sessionId": sid,
                        "boxVersion": box_versions[box_id],
                    },
                )
                self.assertEqual(r.status_code, 200)

                ws = sockets[box_id - 1][1]
                _ = _recv_until(ws, {"START_TIMER"})
                snap = _recv_until(ws, {"STATE_SNAPSHOT"})
                self.assertEqual(snap.get("timerState"), "running")
                session_ids[box_id] = snap.get("sessionId") or sid
                box_versions[box_id] = int(snap.get("boxVersion") or box_versions[box_id])

                # Reset timer while it's running (the bug-prone path).
                r = client.post(
                    "/api/cmd",
                    json={
                        "boxId": box_id,
                        "type": "RESET_PARTIAL",
                        "resetTimer": True,
                        "sessionId": session_ids[box_id],
                        "boxVersion": box_versions[box_id],
                    },
                )
                self.assertEqual(r.status_code, 200)
                _ = _recv_until(ws, {"RESET_PARTIAL"})
                snap = _recv_until(ws, {"STATE_SNAPSHOT"})
                self.assertEqual(snap.get("timerState"), "idle")
                # Server-side timer authoritative storage
                self.assertIsNone(live_module.state_map[box_id].get("timerEndsAtMs"))
                self.assertEqual(live_module.state_map[box_id].get("timerRemainingSec"), 300.0)
                # Snapshot should also reflect full preset as remaining
                self.assertEqual(int(snap.get("remaining") or 0), 300)
                box_versions[box_id] = int(snap.get("boxVersion") or box_versions[box_id])

        finally:
            # Close sockets cleanly.
            for ws_cm, ws in reversed(sockets):
                try:
                    ws_cm.__exit__(None, None, None)
                except Exception:
                    try:
                        ws.close()
                    except Exception:
                        pass


if __name__ == "__main__":
    unittest.main()
