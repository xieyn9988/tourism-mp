"""商户营业时间查询 / 修改测试。运行：pytest -q test_merchant_hours.py"""

from __future__ import annotations

import json

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


def _signed_patch(client, path: str, payload: dict):
    body = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    headers = backend.build_signature_headers("PATCH", path, body=body)
    headers["Content-Type"] = "application/json"
    return client.patch(path, headers=headers, content=body)


def test_patch_business_hours_requires_signature(client):
    merchant_id = _ticket_merchant_id(client)
    path = f"/api/merchants/{merchant_id}/business-hours"
    unsigned = client.patch(
        path, json={"open_time": "08:00", "close_time": "22:00"}
    )
    assert unsigned.status_code == 403, unsigned.text


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
    path = f"/api/merchants/{merchant_id}/business-hours"
    try:
        response = _signed_patch(
            client, path, {"open_time": "07:30", "close_time": "21:00"}
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
        _signed_patch(
            client,
            path,
            {
                "open_time": previous["open_time"],
                "close_time": previous["close_time"],
            },
        )


@pytest.mark.parametrize("value", ["25:00", "abc", "12:60"])
def test_patch_business_hours_rejects_invalid_time(client, value):
    merchant_id = _ticket_merchant_id(client)
    path = f"/api/merchants/{merchant_id}/business-hours"
    response = _signed_patch(
        client, path, {"open_time": value, "close_time": "22:00"}
    )
    assert response.status_code == 422, response.text


def test_patch_business_hours_unknown_merchant(client):
    path = "/api/merchants/999999/business-hours"
    response = _signed_patch(
        client, path, {"open_time": "08:00", "close_time": "22:00"}
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "商户不存在"
