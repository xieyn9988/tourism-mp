"""商户营业时间查询 / 修改测试。运行：pytest -q test_merchant_hours.py"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main as backend


@pytest.fixture(scope="module")
def client():
    with TestClient(backend.app) as test_client:
        yield test_client


def _ticket_merchant_id(client) -> int:
    merchants = client.get("/api/merchants").json()
    return next(m["id"] for m in merchants if m["name"] == "青山景区票务")


def test_merchant_list_exposes_business_hours(client):
    merchant = next(
        m for m in client.get("/api/merchants").json()
        if m["name"] == "青山景区票务"
    )
    assert merchant["open_time"] == "08:00"
    assert merchant["close_time"] == "23:59:59"


def test_patch_business_hours_updates_merchant(client):
    merchant_id = _ticket_merchant_id(client)
    original = client.get("/api/merchants").json()
    previous = next(m for m in original if m["id"] == merchant_id)
    try:
        response = client.patch(
            f"/api/merchants/{merchant_id}/business-hours",
            json={"open_time": "07:30", "close_time": "21:00"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["open_time"] == "07:30"
        assert response.json()["close_time"] == "21:00"
        refreshed = next(
            m for m in client.get("/api/merchants").json()
            if m["id"] == merchant_id
        )
        assert refreshed["close_time"] == "21:00"
    finally:
        client.patch(
            f"/api/merchants/{merchant_id}/business-hours",
            json={
                "open_time": previous["open_time"],
                "close_time": previous["close_time"],
            },
        )


@pytest.mark.parametrize("value", ["25:00", "abc", "12:60"])
def test_patch_business_hours_rejects_invalid_time(client, value):
    merchant_id = _ticket_merchant_id(client)
    response = client.patch(
        f"/api/merchants/{merchant_id}/business-hours",
        json={"open_time": value, "close_time": "22:00"},
    )
    assert response.status_code == 422, response.text


def test_patch_business_hours_unknown_merchant(client):
    response = client.patch(
        "/api/merchants/999999/business-hours",
        json={"open_time": "08:00", "close_time": "22:00"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "商户不存在"
