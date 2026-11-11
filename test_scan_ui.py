from __future__ import annotations

from types import SimpleNamespace

import pytest

import app_scan
import main as backend


def test_exit_post_config_is_routable():
    assert app_scan._post_config("景区出口")["key"] == "exit"


def test_render_lifecycle_html_completed_shows_split_ready():
    html = app_scan._render_lifecycle_html(
        {
            "status": "completed",
            "message": "生命周期已结束：全部子项均已核销或已退款，可以进行分账。",
            "entered_at": "2026-11-11T02:00:00",
            "exited_at": "2026-11-11T04:00:00",
            "leave_at": "2026-11-11T04:00:00",
            "refund_deadline": None,
            "can_split": True,
            "can_refund": False,
        }
    )
    assert "已结束" in html
    assert "可分账" in html
    assert "不可退款" in html
    assert "生命周期已结束" in html


def test_render_lifecycle_html_active_shows_waiting():
    html = app_scan._render_lifecycle_html(
        {
            "status": "active",
            "message": "游客在园内，生命周期进行中。",
            "entered_at": "2026-11-11T02:00:00",
            "exited_at": None,
            "leave_at": "2026-11-11T10:00:00",
            "refund_deadline": None,
            "can_split": False,
            "can_refund": True,
        }
    )
    assert "游园中" in html
    assert "分账待生命周期结束" in html
    assert "可退款" in html


def test_signed_request_signs_exact_body(monkeypatch):
    fixed_ts = 1762819200
    monkeypatch.setattr(
        app_scan,
        "time",
        SimpleNamespace(time=lambda: fixed_ts),
    )
    captured = {}

    class FakeResponse:
        status_code = 200
        content = b"{}"

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["data"] = kwargs["data"]
        return FakeResponse()

    monkeypatch.setattr(app_scan.requests, "request", fake_request)

    body = b'{"refund_after_lifecycle":true,"refund_window_hours":24}'
    response = app_scan._signed_request("PUT", "/api/park-settings", body)

    assert response.status_code == 200
    expected = backend.build_signature_headers(
        "PUT", "/api/park-settings", body=body, timestamp=fixed_ts
    )
    assert captured["headers"]["X-Signature"] == expected["X-Signature"]
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["data"] == body
    assert captured["method"] == "PUT"
