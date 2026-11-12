from __future__ import annotations

from types import SimpleNamespace

import pytest

import app_scan
import main as backend


def test_exit_post_config_is_routable():
    assert app_scan._post_config("景区出口")["key"] == "exit"


def test_parse_pickup_code_accepts_order_id():
    assert app_scan._parse_pickup_code("42") == (42, "")
    assert app_scan._parse_pickup_code(" 42 ") == (42, "")


def test_parse_pickup_code_rejects_ticket_code_prefix():
    # 二维码电子票号（TK-xxxx）不是订单 ID，必须拒绝，避免核销错单。
    order_id, message = app_scan._parse_pickup_code("TK-42")

    assert order_id is None
    assert "格式不正确" in message


def test_parse_pickup_code_rejects_invalid_text():
    order_id, message = app_scan._parse_pickup_code("二维码")

    assert order_id is None
    assert "格式不正确" in message


def test_pick_next_pending_record_follows_business_order():
    records = [
        {"id": 3, "item_name": "越野车", "verify_status": "unverified"},
        {"id": 2, "item_name": "咖啡", "verify_status": "unverified"},
        {"id": 1, "item_name": "门票", "verify_status": "unverified"},
    ]

    assert app_scan._pick_next_pending_record(records)["id"] == 1

    records[2]["verify_status"] = "verified"
    assert app_scan._pick_next_pending_record(records)["id"] == 2


def test_pick_next_pending_record_skips_settled_items():
    records = [
        {"id": 1, "item_name": "门票", "verify_status": "verified"},
        {"id": 2, "item_name": "咖啡", "verify_status": "expired"},
        {"id": 3, "item_name": "越野车", "verify_status": "refunded"},
    ]

    assert app_scan._pick_next_pending_record(records) is None


def test_toggle_refund_window_hides_input_when_refund_disallowed():
    assert app_scan.toggle_refund_window(False)["visible"] is False
    assert app_scan.toggle_refund_window(True)["visible"] is True


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


def _fake_verify_conflict(monkeypatch, detail: str) -> tuple[str, str]:
    class FakeResponse:
        status_code = 409
        content = b"{}"
        text = ""

        def json(self):
            return {"detail": detail}

    monkeypatch.setattr(
        app_scan.requests, "post", lambda *args, **kwargs: FakeResponse()
    )
    return app_scan._submit_verify_request(
        1, app_scan.DEFAULT_VERIFY_POST, origin="测试"
    )


def test_verify_conflict_shows_closed_park_not_already_verified(monkeypatch):
    card, overlay = _fake_verify_conflict(
        monkeypatch,
        "景区已闭园（2026-11-11 18:00），今日无法检票入园，请明日再来。",
    )
    assert "闭园" in card
    assert "已核销" not in card
    assert "已核销" not in overlay


def test_verify_conflict_shows_refunded_reason(monkeypatch):
    card, overlay = _fake_verify_conflict(
        monkeypatch, "该票已退款，不可再核销"
    )
    assert "退款" in card
    assert "已核销" not in card
    assert "已核销" not in overlay


def test_verify_conflict_still_reports_duplicate_scan(monkeypatch):
    card, overlay = _fake_verify_conflict(
        monkeypatch, "该票已核销，请勿重复扫码！"
    )
    assert "已核销" in card
    assert "已核销" in overlay


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
