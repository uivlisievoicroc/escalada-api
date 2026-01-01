import json
import time
import pytest
from fastapi.testclient import TestClient
from escalada.main import app
from escalada.auth.service import create_access_token


@pytest.mark.anyio
def test_judge_receives_snapshot_and_progress_without_refresh():
    client = TestClient(app)

    box_id = 1
    token = create_access_token(username="judge1", role="judge", assigned_boxes=[box_id])
    # INIT_ROUTE via HTTP to create state and broadcast
    init_payload = {
        "boxId": box_id,
        "type": "INIT_ROUTE",
        "routeIndex": 1,
        "holdsCount": 5,
        "competitors": [{"nume": "A"}],
        "timerPreset": "05:00",
    }
    r = client.post("/api/cmd", json=init_payload, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200

    # Connect Judge WS
    with client.websocket_connect(f"/api/ws/{box_id}?token={token}") as ws:
        # First message should be STATE_SNAPSHOT
        msg1 = json.loads(ws.receive_text())
        assert msg1["type"] == "STATE_SNAPSHOT"
        assert msg1["initiated"] is True
        assert msg1.get("holdsCount") == 5
        sid = msg1.get("sessionId")

        # Send PROGRESS_UPDATE and expect it on WS
        r2 = client.post(
            "/api/cmd",
            json={"boxId": box_id, "type": "PROGRESS_UPDATE", "delta": 1, "sessionId": sid},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 200

        # Wait for progress update
        deadline = time.time() + 2
        progress = None
        while time.time() < deadline:
            data = json.loads(ws.receive_text())
            if data.get("type") == "PROGRESS_UPDATE":
                progress = data
                break
        assert progress, "Did not receive PROGRESS_UPDATE on WS"
        assert progress.get("holdCount") in (None, 1)  # backend may or may not echo holdCount
