from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook

from escalada.auth.service import create_access_token
from escalada.main import app


def _admin_token() -> str:
    return create_access_token(username="admin-test", role="admin", assigned_boxes=[])


def _xlsx_bytes(rows: list[tuple[str | None, str | None]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Name", "Club"])
    for name, club in rows:
        ws.append([name, club])
    buffer = BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


def test_upload_invalid_routes_count_returns_422():
    client = TestClient(app)
    token = _admin_token()
    payload = _xlsx_bytes([("Alex", "Club A")])

    res = client.post(
        "/api/admin/upload",
        data={
            "category": "Cat",
            "routesCount": "abc",
            "holdsCounts": "[10]",
            "include_clubs": "true",
        },
        files={
            "file": (
                "list.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 422
    assert res.json()["detail"] == "invalid_routes_count"


def test_upload_invalid_holds_counts_returns_422():
    client = TestClient(app)
    token = _admin_token()
    payload = _xlsx_bytes([("Alex", "Club A")])

    res = client.post(
        "/api/admin/upload",
        data={
            "category": "Cat",
            "routesCount": "1",
            "holdsCounts": "{\"bad\": true}",
            "include_clubs": "true",
        },
        files={
            "file": (
                "list.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 422
    assert res.json()["detail"] == "invalid_holds_counts"


def test_upload_keeps_competitor_without_club_when_include_clubs_false():
    client = TestClient(app)
    token = _admin_token()
    payload = _xlsx_bytes([("Alex", None), ("Bob", "Club B")])

    res = client.post(
        "/api/admin/upload",
        data={
            "category": "Cat",
            "routesCount": "1",
            "holdsCounts": "[10]",
            "include_clubs": "false",
        },
        files={
            "file": (
                "list.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    competitors = res.json()["listbox"]["concurenti"]
    assert competitors == [{"nume": "Alex"}, {"nume": "Bob"}]


def test_upload_accepts_holds_counts_numeric_strings():
    client = TestClient(app)
    token = _admin_token()
    payload = _xlsx_bytes([("Alex", "Club A")])

    res = client.post(
        "/api/admin/upload",
        data={
            "category": "Cat",
            "routesCount": "1",
            "holdsCounts": "[\"10\"]",
            "include_clubs": "true",
        },
        files={
            "file": (
                "list.xlsx",
                payload,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    assert res.json()["listbox"]["holdsCounts"] == [10]
