"""
乡村文旅电子门票 —— Gradio + 摄像头扫码 + 二维码购票
=====================================================

两个页面：
    1. 游客端：购买门票，生成电子门票二维码；
       购票后还可以“申请退款”和“执行分账”；
    2. 商家端：摄像头持续识别游客二维码，识别成功即自动核销，无需点击提交；
       现场先选择【当前核销岗位】（景区检票员 / 咖啡店员 / 越野车安全员），
       系统只核销该岗位对应的子项，不再按顺序自动核销下一项；
       现场光线影响摄像头时，可在“备用演示区”手动输入电子票号 / 订单 ID
       完成同样的核销（复用同一套岗位核销逻辑）。

企业级能力：
    分账 / 退款接口由后端 HMAC-SHA256 签名保护。本文件使用 hmac + hashlib
    按后端的 canonical string 生成 X-Timestamp / X-Signature，再随 requests
    请求头发送；否则后端会返回 403。

签名原文（必须与 main.py 完全一致）：
    {timestamp}\n{METHOD}\n{path}\n{sha256_hex(body)}
    X-Signature = HMAC-SHA256(API_SIGNING_SECRET, canonical).hexdigest()

运行方式：
    终端 1：python main.py
    终端 2：python app_scan.py

    两端必须使用相同的 API_SIGNING_SECRET（默认值仅用于本地演示）：
        set API_SIGNING_SECRET=your-secret   # Windows CMD
        export API_SIGNING_SECRET=your-secret  # PowerShell / Git Bash

默认地址：http://127.0.0.1:7861
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import cv2
import gradio as gr
import numpy as np
import qrcode
import requests
from PIL import Image

# ===========================================================================
# 一、配置
# ===========================================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "tourism.db"

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "5"))
APP_PORT = int(os.getenv("SCAN_APP_PORT", "7861"))

# 必须与后端 main.py 的 API_SIGNING_SECRET 保持一致，否则资金接口返回 403。
DEFAULT_SIGNING_SECRET = "tourism-mp-demo-secret"
SIGNING_SECRET = os.getenv("API_SIGNING_SECRET", DEFAULT_SIGNING_SECRET)

# 需求里的“假 ID 1”：二维码无法映射到本地核销记录时使用。
DEFAULT_VERIFY_RECORD_ID = int(os.getenv("DEFAULT_VERIFY_RECORD_ID", "1"))

ORDER_AMOUNT = Decimal("200.00")
PRODUCT_NAME = "乡村文旅超值套餐"
TICKET_ITEM_NAME = "门票"
COFFEE_ITEM_NAME = "咖啡"
OFFROAD_ITEM_NAME = "越野车"
TICKET_MERCHANT_NAME = "青山景区票务"
CAFE_MERCHANT_NAME = "云顶咖啡"
OFFROAD_MERCHANT_NAME = "极速越野"
OFFROAD_SPLIT_RATE = Decimal("0.15")
CODE_PREFIX = "TK-"
CODE_PATTERN = re.compile(r"^TK-(\d{1,9})$", re.IGNORECASE)

# 现场核销岗位：下拉框展示中文，key 与后端 VerifyPost 枚举保持一致。
# 每个岗位只核销自己负责的子项，不再按“门票 → 咖啡”的顺序自动取下一项。
VERIFY_POSTS: dict[str, dict[str, str]] = {
    "景区检票员": {"key": "gate", "item": "门票", "icon": "🎫"},
    "咖啡店员": {"key": "coffee", "item": "咖啡", "icon": "☕"},
    "越野车安全员": {"key": "offroad", "item": "越野车", "icon": "🚙"},
}
EXIT_POST_LABEL = "景区出口"
EXIT_POST_CONFIG = {"key": "exit", "item": "离园", "icon": "🚪"}
SCAN_POSTS: dict[str, dict[str, str]] = {
    **VERIFY_POSTS,
    EXIT_POST_LABEL: EXIT_POST_CONFIG,
}
DEFAULT_VERIFY_POST = "景区检票员"

# 同一张二维码在镜头里持续停留时只核销一次；移开超过该时长后再次出现，
# 才视为“重新扫码”（用于重复核销提示）。
SCAN_REARM_SECONDS = 1.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("tourism-scan-poc")


# ===========================================================================
# 二、通用小工具
# ===========================================================================

def _generate_order_no() -> str:
    """生成不超过 32 字符、便于人工识别的 POC 订单号。"""
    return f"POC{datetime.now():%Y%m%d%H%M%S}{secrets.randbelow(10000):04d}"


def _status_card(kind: str, title: str, body: str = "") -> str:
    """渲染页面内的状态卡片。kind: success / warning / error / info。"""
    icons = {
        "success": "✅",
        "warning": "⚠️",
        "error": "❌",
        "info": "ℹ️",
    }
    safe_title = html.escape(title)
    body_html = f'<div class="scan-card-body">{body}</div>' if body else ""
    return (
        f'<div class="scan-card scan-card-{kind}" role="status">'
        f'<div class="scan-card-title">{icons.get(kind, "•")} {safe_title}</div>'
        f"{body_html}"
        f"</div>"
    )


# ===========================================================================
# 三、HMAC-SHA256 签名与资金接口调用
# ===========================================================================

def _build_signature_headers(
    method: str,
    path: str,
    body: bytes = b"",
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    """
    生成后端 require_signature 所需的请求头。

    与 main.py 的 canonical string 必须逐字符一致：
        {timestamp}\\n{METHOD}\\n{path}\\n{sha256_hex(body)}
    """
    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    body_digest = hashlib.sha256(body).hexdigest()
    canonical = f"{ts}\n{method.upper()}\n{path}\n{body_digest}"
    signature = hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {"X-Timestamp": ts, "X-Signature": signature}


def _signed_request(
    method: str,
    path: str,
    body: bytes = b"",
) -> requests.Response:
    headers = _build_signature_headers(method, path, body)
    if body:
        headers["Content-Type"] = "application/json"
    logger.info("调用签名接口：%s %s", method.upper(), path)
    return requests.request(
        method.upper(),
        f"{API_BASE_URL}{path}",
        headers=headers,
        data=body,
        timeout=REQUEST_TIMEOUT,
    )


def _post_signed(path: str) -> requests.Response:
    return _signed_request("POST", path)


def _resolve_target_order_id(
    last_order: Any,
    manual_order_id: Any,
) -> tuple[int | None, str]:
    """
    决定本次退款 / 分账作用于哪张订单：

    1. 优先使用界面中手动填写的订单 ID（便于测试演示订单）；
    2. 否则使用最近一次购买产生的订单 ID；
    3. 都没有时返回提示信息。
    """
    if manual_order_id not in (None, ""):
        try:
            value = int(float(manual_order_id))
            if value > 0:
                return value, ""
        except (TypeError, ValueError):
            pass

    if isinstance(last_order, dict):
        order_id = last_order.get("order_id")
        if isinstance(order_id, int) and order_id > 0:
            return order_id, ""
        record_id = last_order.get("record_id")
        if isinstance(record_id, int) and record_id > 0:
            resolved = _lookup_order_id_by_record(record_id)
            if resolved is not None:
                return resolved, ""

    return (
        None,
        "请先购买门票，或在“订单 ID”输入框中填写要操作的订单"
        "（演示订单通常为 1）。",
    )


def request_refund(last_order: Any, manual_order_id: Any) -> str:
    """游客端按钮事件：调用 POST /api/order/{id}/refund，返回 Markdown。"""
    order_id, hint = _resolve_target_order_id(last_order, manual_order_id)
    if order_id is None:
        return _render_request_error("尚未选择订单", hint)

    path = f"/api/order/{order_id}/refund"
    try:
        response = _post_signed(path)
    except requests.ConnectionError:
        return _render_request_error(
            "无法连接后端",
            f"请确认已启动 python main.py（{API_BASE_URL}）。",
            path,
        )
    except requests.Timeout:
        return _render_request_error(
            "请求超时",
            f"后端在 {REQUEST_TIMEOUT:g} 秒内没有响应。",
            path,
        )
    except requests.RequestException as exc:
        return _render_request_error("请求异常", str(exc), path)

    return _render_financial_response(response, kind="refund", path=path)


def request_split(last_order: Any, manual_order_id: Any) -> str:
    """游客端按钮事件：调用 POST /api/order/{id}/split，返回 Markdown。"""
    order_id, hint = _resolve_target_order_id(last_order, manual_order_id)
    if order_id is None:
        return _render_request_error("尚未选择订单", hint)

    path = f"/api/order/{order_id}/split"
    try:
        response = _post_signed(path)
    except requests.ConnectionError:
        return _render_request_error(
            "无法连接后端",
            f"请确认已启动 python main.py（{API_BASE_URL}）。",
            path,
        )
    except requests.Timeout:
        return _render_request_error(
            "请求超时",
            f"后端在 {REQUEST_TIMEOUT:g} 秒内没有响应。",
            path,
        )
    except requests.RequestException as exc:
        return _render_request_error("请求异常", str(exc), path)

    return _render_financial_response(response, kind="split", path=path)


def load_park_settings() -> tuple[Any, Any, str]:
    path = "/api/park-settings"
    try:
        response = requests.get(f"{API_BASE_URL}{path}", timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return (
            gr.update(),
            gr.update(),
            _status_card("warning", "无法读取园区配置", html.escape(str(exc))),
        )
    if response.status_code != 200:
        return (
            gr.update(),
            gr.update(),
            _render_request_error(
                f"读取园区配置失败（HTTP {response.status_code}）",
                _response_detail(response) or "后端未返回错误信息。",
                path,
            ),
        )
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return (
            gr.update(),
            gr.update(),
            _status_card("error", "园区配置格式错误", "后端返回内容无法解析。"),
        )
    allow = bool(data.get("refund_after_lifecycle"))
    hours = int(data.get("refund_window_hours", 72))
    return (
        gr.update(value=allow),
        gr.update(value=hours),
        _status_card(
            "info",
            "园区配置已加载",
            f"离园后退款：{'允许' if allow else '不允许'} · "
            f"退款窗口：{hours} 小时",
        ),
    )


def save_park_settings(allow: Any, hours: Any) -> str:
    try:
        hours_value = int(float(hours))
    except (TypeError, ValueError):
        return _status_card(
            "error", "配置无效", "退款窗口必须为 0–8760 的整数小时。"
        )
    if not 0 <= hours_value <= 8760:
        return _status_card(
            "error", "配置无效", "退款窗口必须为 0–8760 的整数小时。"
        )

    path = "/api/park-settings"
    body = json.dumps(
        {
            "refund_after_lifecycle": bool(allow),
            "refund_window_hours": hours_value,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        response = _signed_request("PUT", path, body)
    except requests.ConnectionError:
        return _render_request_error(
            "无法连接后端", f"请确认已启动 python main.py（{API_BASE_URL}）。", path
        )
    except requests.Timeout:
        return _render_request_error(
            "请求超时", f"后端在 {REQUEST_TIMEOUT:g} 秒内没有响应。", path
        )
    except requests.RequestException as exc:
        return _render_request_error("请求异常", str(exc), path)

    if response.status_code == 200:
        return _status_card(
            "success",
            "园区配置已保存",
            f"离园后退款：{'允许' if allow else '不允许'} · "
            f"退款窗口：{hours_value} 小时",
        )
    return _render_request_error(
        f"保存失败（HTTP {response.status_code}）",
        _response_detail(response) or "后端未返回错误信息。",
        path,
    )


def _reset_error(
    title: str,
    message: str,
    path: str = "/api/demo/reset",
) -> tuple[Any, Any, Any, Any, Any, Any, Any, str]:
    """重置失败时保持页面其它区域不变，仅展示错误卡片。"""
    error = _render_request_error(title, message, path)
    return (
        error,
        gr.skip(),  # 二维码
        gr.skip(),  # 购票成功提示
        gr.skip(),  # 二维码说明
        gr.skip(),  # 订单 ID 输入框
        gr.skip(),  # last_order
        gr.skip(),  # 订单状态
        error,      # 运营后台结果区
    )


def reset_demo_environment() -> tuple[Any, Any, str, str, int | None, Any, str, str]:
    """
    顶部按钮事件：一键重置演示环境。

    调用签名接口 POST /api/demo/reset，后端会清空历史订单 / 核销记录，
    并返回一张全新的“已支付 + 三项未核销”订单。前端同时完成：

      1. 绿色成功提示 + 新订单 ID；
      2. 自动生成新电子门票二维码，可直接去商家端扫码核销；
      3. 自动切换订单状态 / 退款 / 分账所用的订单 ID。
    """
    path = "/api/demo/reset"
    try:
        response = _post_signed(path)
    except requests.ConnectionError:
        return _reset_error(
            "无法连接后端",
            f"请确认已启动 python main.py（{API_BASE_URL}）。",
            path,
        )
    except requests.Timeout:
        return _reset_error(
            "请求超时",
            f"后端在 {REQUEST_TIMEOUT:g} 秒内没有响应。",
            path,
        )
    except requests.RequestException as exc:
        return _reset_error("请求异常", str(exc), path)

    try:
        data = response.json()
    except ValueError:
        data = None

    if response.status_code != 200 or not isinstance(data, dict):
        detail = data.get("detail") if isinstance(data, dict) else None
        if response.status_code == 403:
            message = (
                "签名验证失败：请确认 app_scan.py 与 main.py 使用相同的 "
                "API_SIGNING_SECRET，并检查本机时间是否准确。"
            )
        elif isinstance(detail, str) and detail:
            message = detail
        else:
            message = (response.text or "").strip()[:500] or "后端未返回响应内容。"
        return _reset_error(
            f"重置失败（HTTP {response.status_code}）",
            message,
            path,
        )

    if not data.get("order_id"):
        return _reset_error("重置失败", "后端响应缺少新订单 ID。", path)

    order_id = int(data["order_id"])
    order_no = str(data.get("order_no") or "")

    # 优先使用后端返回的门票核销记录 ID；缺失时从 items 中兜底查找。
    ticket_record_id: int | None = None
    raw_ticket_id = data.get("ticket_record_id")
    if isinstance(raw_ticket_id, int) and raw_ticket_id > 0:
        ticket_record_id = raw_ticket_id
    else:
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            if "门票" not in str(item.get("item_name") or ""):
                continue
            try:
                ticket_record_id = int(item.get("verify_record_id"))
            except (TypeError, ValueError):
                ticket_record_id = None
            break

    qr_update: Any = gr.skip()
    if ticket_record_id is not None:
        try:
            qr_update = gr.update(
                value=_make_qr_image(f"{CODE_PREFIX}{ticket_record_id:04d}"),
                visible=True,
            )
        except Exception:  # noqa: BLE001 - 二维码失败不应阻断重置结果展示
            logger.exception("重置后生成二维码失败")

    order_state = {
        "order_id": order_id,
        "order_no": order_no,
        "record_id": ticket_record_id,
        "source": "一键重置演示环境",
    }

    # Gradio toast 会 print 到控制台，Windows GBK 终端无法编码 emoji；
    # 因此 toast 使用纯文本，绿色 ✅ 卡片由下面的 HTML 提示展示。
    message = f"演示环境已重置，请使用新订单 ID：{order_id} 开始测试！"
    gr.Success(message, title="演示环境已重置")

    reset_html = _status_card(
        "success",
        "演示环境已重置",
        f"新订单 ID：<b>{order_id}</b> · 订单号：{html.escape(order_no)}<br>"
        "门票 / 咖啡 / 越野车三项均为未核销，可直接开始测试。",
    )
    success_html = (
        '<div class="purchase-success" role="status">'
        '<div class="purchase-success-icon">✅</div>'
        '<div class="purchase-success-text">'
        '<div class="purchase-success-title">'
        "演示环境已重置！新的电子门票已生成，请先核销门票。"
        "</div>"
        '<div class="purchase-success-meta">'
        f"订单号：{html.escape(order_no)} · 门票核销后，咖啡与越野车项目才会开放"
        "</div></div></div>"
    )
    action_md = (
        f"### ✅ 新订单已就绪\n"
        f"已自动切换到订单 `#{order_id}`（{_md_cell(order_no)}）。\n\n"
        "门票、咖啡、越野车均为未核销。可先核销部分项目，"
        "再点击「💸 申请退款」验证“部分退款”，随后点击「📊 执行分账」，"
        "查看已核销项目如何单独抽成结算。"
    )

    return (
        reset_html,
        qr_update,
        success_html,
        _QR_NOTE_HTML,
        order_id,
        order_state,
        _render_order_status(order_state),
        action_md,
    )


# ===========================================================================
# 四、Markdown 渲染
# ===========================================================================

def _md_cell(value: Any) -> str:
    """转义表格单元格内容，避免 Markdown / HTML 注入和表格断裂。"""
    text = "" if value is None else str(value)
    return html.escape(text).replace("|", "\\|").replace("\n", " ").strip()


def _money_text(value: Any) -> str:
    if value is None or value == "":
        return "￥--"
    return f"￥{_md_cell(value)}"


def _percent_text(value: Any) -> str:
    try:
        percent = Decimal(str(value)) * 100
    except (InvalidOperation, TypeError, ValueError):
        return _md_cell(value)
    return f"{percent.normalize():f}%"


def _order_status_label(value: Any) -> str:
    labels = {
        "unpaid": "未支付",
        "paid": "已支付",
        "split": "已分账",
        "refunded": "已退款",
    }
    return labels.get(str(value), str(value))


def _verify_status_label(value: Any) -> str:
    labels = {
        "unverified": "未核销 ⏳",
        "verified": "已核销 ✅",
        "refunded": "已退款 💸",
    }
    return labels.get(str(value), str(value))


def _raw_json_block(data: Any) -> list[str]:
    """以可折叠区块展示后端原始 JSON，便于对账与排查。"""
    return [
        "",
        "---",
        "",
        "<details>",
        "<summary>查看后端原始 JSON 响应</summary>",
        "",
        "```json",
        json.dumps(data, ensure_ascii=False, indent=2),
        "```",
        "",
        "</details>",
    ]


def _render_request_error(title: str, message: str, path: str | None = None) -> str:
    """
    渲染红色错误卡片。

    业务校验失败（如“订单没有已核销项目，分账金额为 0”）必须醒目展示，
    不能只输出普通 Markdown 文本。
    """
    body_parts = [html.escape(str(message))]
    if path:
        body_parts.append(f"请求：<code>POST {html.escape(path)}</code>")
    # 同时使用内联样式，避免自定义 CSS 未加载时错误提示失去红色警示。
    return (
        '<div class="scan-card scan-card-error" role="alert" '
        'style="color:#991b1b;background:#fef2f2;border:1px solid #fca5a5;'
        "border-left:5px solid #dc2626;border-radius:14px;"
        'padding:16px 18px;margin-top:6px;line-height:1.7;">'
        f'<div style="font-weight:700;margin-bottom:4px;">'
        f"❌ {html.escape(title)}</div>"
        f'<div>{"<br>".join(body_parts)}</div>'
        "</div>"
    )


def _render_split_markdown(data: dict[str, Any]) -> str:
    """把分账接口 JSON 渲染成 Markdown 汇总表 + 商户表 + 项目表。

    重点展示“分账基数”：只有已核销子项参与平台抽成与商户结算；
    未核销 / 已退款子项保留在明细中，但各项金额固定显示为 0 元。
    """
    items = data.get("items") or []
    excluded_items = [
        item for item in items if not item.get("split_eligible", True)
    ]
    excluded_names = "、".join(
        _md_cell(item.get("item_name")) for item in excluded_items
    )

    lines: list[str] = [
        "## 💰 分账结果",
        "",
        f"**订单号**：`{_md_cell(data.get('order_no'))}`　"
        f"**订单 ID**：`{_md_cell(data.get('order_id'))}`　"
        f"**状态**：{_order_status_label(data.get('order_status'))}",
        "",
        "> 分账规则：仅“已核销”子项进入分账池；未核销 / 已退款子项"
        "不抽成、不结算，金额记 0 元。",
        "",
        "| 汇总 | 金额 |",
        "| :--- | ---: |",
        f"| 订单总额 | {_money_text(data.get('total_amount'))} |",
        f"| **分账基数（已核销项）** | **{_money_text(data.get('split_amount'))}** |",
        f"| 平台留存 | **{_money_text(data.get('platform_retention'))}** |",
        f"| 商户实得合计 | **{_money_text(data.get('merchant_income_total'))}** |",
    ]

    if excluded_names:
        lines.append(
            f"| 未参与分账项目 | {excluded_names}（记 0 元） |"
        )

    lines += [
        "",
        "### 🏪 商户分账明细",
        "",
        "| 商户 | 抽成比例 | 分账基数 | 平台抽成 | 商户实得 |",
        "| :--- | ---: | ---: | ---: | ---: |",
    ]

    merchants = data.get("merchants") or []
    for merchant in merchants:
        item_names = "、".join(
            _md_cell(name) for name in (merchant.get("item_names") or [])
        ) or "--"
        lines.append(
            f"| {_md_cell(merchant.get('merchant_name'))}"
            f"<br><small>{item_names}</small> "
            f"| {_percent_text(merchant.get('split_rate'))} "
            f"| {_money_text(merchant.get('gross_amount'))} "
            f"| {_money_text(merchant.get('platform_commission'))} "
            f"| **{_money_text(merchant.get('merchant_income'))}** |"
        )
    if not merchants:
        lines.append("| 暂无商户分账数据 | -- | -- | -- | -- |")

    lines += [
        "",
        "### 🎫 项目明细",
        "",
        "| 项目 | 商户 | 状态 | 参与分账 | 分账金额 | 平台抽成 | 商户实得 |",
        "| :--- | :--- | :--- | :---: | ---: | ---: | ---: |",
    ]
    for item in items:
        eligible = bool(item.get("split_eligible", True))
        lines.append(
            f"| {_md_cell(item.get('item_name'))} "
            f"| {_md_cell(item.get('merchant_name'))} "
            f"| {_verify_status_label(item.get('verify_status'))} "
            f"| {'✅ 是' if eligible else '— 否（记 0）'} "
            f"| {_money_text(item.get('amount'))} "
            f"| {_money_text(item.get('platform_commission'))} "
            f"| **{_money_text(item.get('merchant_income'))}** |"
        )
    if not items:
        lines.append("| 暂无项目数据 | -- | -- | -- | -- | -- | -- |")

    lines += _raw_json_block(data)
    return "\n".join(lines)


def _render_refund_markdown(
    data: dict[str, Any],
    *,
    rejected: bool = False,
) -> str:
    """
    把退款接口 JSON 渲染成 Markdown。

    第一张表严格按“项目 / 状态 / 退款金额”展示，已核销项目固定退款 0 元；
    后续表格补充原价、10% 手续费与逐项原因，方便现场讲解。
    """
    refunded = bool(data.get("refunded"))
    title = "🚫 不可退款" if rejected or not refunded else "💸 退款明细"
    items = data.get("items") or []

    lines: list[str] = [
        f"## {title}",
        "",
        f"> {_md_cell(data.get('message') or '后端未返回说明')}",
        "",
        f"**订单号**：`{_md_cell(data.get('order_no'))}`　"
        f"**订单 ID**：`{_md_cell(data.get('order_id'))}`　"
        f"**订单状态**：{_order_status_label(data.get('order_status'))}",
        "",
        "### 📋 逐项退款结果",
        "",
        "| 项目 | 状态 | 退款金额 |",
        "| :--- | :---: | ---: |",
    ]

    for item in items:
        status = item.get("status_label") or {
            "verified": "已用",
            "refunded": "已退款",
        }.get(str(item.get("verify_status")), "未用")
        lines.append(
            f"| {_md_cell(item.get('item_name'))} "
            f"| {_md_cell(status)} "
            f"| **{_money_text(item.get('refund_amount'))}** |"
        )
    if not items:
        lines.append("| 暂无可退款项目 | -- | -- |")

    lines += [
        "",
        "### 💰 金额汇总",
        "",
        "| 汇总 | 金额 |",
        "| :--- | ---: |",
        f"| 退款基数（未核销项原价） | {_money_text(data.get('refund_gross_amount'))} |",
        f"| 手续费合计（10%） | **{_money_text(data.get('fee_amount'))}** |",
        f"| 实退金额 | **{_money_text(data.get('refund_amount'))}** |",
    ]

    if items:
        lines += [
            "",
            "### 🧾 费用明细",
            "",
            "| 项目 | 商户 | 核销状态 | 处理结果 | 原价 | 手续费 | 实退 |",
            "| :--- | :--- | :--- | :--- | ---: | ---: | ---: |",
        ]
        for item in items:
            result = "✅ 已退款" if item.get("refundable") else "❌ 拒绝退款"
            lines.append(
                f"| {_md_cell(item.get('item_name'))} "
                f"| {_md_cell(item.get('merchant_name'))} "
                f"| {_verify_status_label(item.get('verify_status'))} "
                f"| {result} "
                f"| {_money_text(item.get('gross_amount'))} "
                f"| {_money_text(item.get('fee_amount'))} "
                f"| **{_money_text(item.get('refund_amount'))}** |"
            )

        lines += ["", "### 📝 处理说明", ""]
        for item in items:
            icon = "✅" if item.get("refundable") else "❌"
            lines.append(
                f"- {icon} **{_md_cell(item.get('item_name'))}**："
                f"{_md_cell(item.get('reason'))}"
            )

    lines += _raw_json_block(data)
    return "\n".join(lines)


def _render_financial_response(
    response: requests.Response,
    *,
    kind: str,
    path: str,
) -> str:
    """根据后端 HTTP 状态码，把分账 / 退款结果或错误渲染为 Markdown。"""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code == 200 and isinstance(payload, dict):
        if kind == "split":
            return _render_split_markdown(payload)
        return _render_refund_markdown(payload)

    detail = payload.get("detail") if isinstance(payload, dict) else None

    if response.status_code == 400:
        message = (
            detail
            if isinstance(detail, str) and detail
            else "请求未通过业务校验，请检查订单状态。"
        )
        return _render_request_error("业务校验未通过（400）", message, path)

    if response.status_code == 403:
        return _render_request_error(
            "签名验证失败（403）",
            "请确认 app_scan.py 与 main.py 使用相同的 API_SIGNING_SECRET，"
            "并检查本机时间是否准确（默认允许 ±300 秒）。",
            path,
        )

    if response.status_code == 409:
        if kind == "refund" and isinstance(detail, dict):
            # 全部项目都不可退时，后端把完整逐项结论放在 detail 中。
            return _render_refund_markdown(detail, rejected=True)
        message = (
            detail
            if isinstance(detail, str) and detail
            else "当前订单状态不满足本次操作条件。"
        )
        return _render_request_error("业务校验未通过（409）", message, path)

    if response.status_code == 404:
        message = detail if isinstance(detail, str) and detail else "找不到该订单。"
        return _render_request_error("订单不存在（404）", message, path)

    if isinstance(detail, str) and detail:
        return _render_request_error(
            f"后端返回异常（HTTP {response.status_code}）", detail, path
        )

    fallback = (response.text or "").strip()[:500] or "后端未返回响应内容。"
    return _render_request_error(
        f"后端返回异常（HTTP {response.status_code}）", fallback, path
    )


# ===========================================================================
# 五、购票：二维码 + 测试订单
# ===========================================================================

@dataclass
class OrderCreation:
    order_no: str
    record_id: int
    source: str
    note: str = ""
    # 订单主键：分账 / 退款接口路径参数使用它（注意不是 order_no）。
    order_id: int | None = None


def _extract_order_id(payload: Any) -> int | None:
    """从后端订单响应中提取订单主键 id。"""
    if not isinstance(payload, dict):
        return None

    for key in ("id", "order_id"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)

    return None


def _extract_record_id(payload: Any) -> int | None:
    """尽量从后端创建订单的响应中找出第一条核销记录 ID。"""
    if not isinstance(payload, dict):
        return None

    for key in ("verify_record_id", "record_id", "first_verify_record_id"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)

    records = payload.get("verify_records")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            value = record.get("id") or record.get("record_id")
            if isinstance(value, int) and value > 0:
                return value
            if isinstance(value, str) and value.isdigit() and int(value) > 0:
                return int(value)

    return None


def _fetch_first_record_id(order_id: int) -> int | None:
    """若 POST /api/orders 只返回订单 ID，再 GET 订单详情补拿记录 ID。"""
    try:
        response = requests.get(
            f"{API_BASE_URL}/api/orders/{order_id}",
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None
    try:
        return _extract_record_id(response.json())
    except ValueError:
        return None


def _lookup_order_id_by_record(record_id: int) -> int | None:
    """本地库反查：核销记录 ID -> 订单 ID（用于兼容只返回记录 ID 的后端响应）。"""
    if not DB_PATH.exists():
        return None

    try:
        connection = sqlite3.connect(str(DB_PATH), timeout=2)
        try:
            row = connection.execute(
                "SELECT order_id FROM verify_records WHERE id = ? LIMIT 1",
                (record_id,),
            ).fetchone()
            if row is None:
                return None
            return int(row[0])
        finally:
            connection.close()
    except (sqlite3.Error, TypeError, ValueError):
        logger.warning(
            "反查订单 ID 失败：record_id=%s", record_id, exc_info=True
        )
        return None


def _try_create_order_api(order_no: str) -> tuple[OrderCreation | None, str]:
    """
    尝试调用后端的 POST /api/orders。

    目前 main.py 没有这个路由，通常会返回 405；这里不把它当致命错误，
    而是返回失败原因，让上层用 SQLAlchemy 本地兜底。
    """
    payload = {
        "order_no": order_no,
        "total_amount": float(ORDER_AMOUNT),
        "status": "paid",
        "items": [
            {
                "item_name": TICKET_ITEM_NAME,
                "price": 80.0,
                "quantity": 1,
            },
            {
                "item_name": COFFEE_ITEM_NAME,
                "price": 40.0,
                "quantity": 1,
            },
            {
                "item_name": OFFROAD_ITEM_NAME,
                "price": 80.0,
                "quantity": 1,
            },
        ],
    }

    try:
        response = requests.post(
            f"{API_BASE_URL}/api/orders",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.ConnectionError:
        return None, f"后端不可达（{API_BASE_URL}）"
    except requests.Timeout:
        return None, f"POST /api/orders 请求超时（{REQUEST_TIMEOUT:g} 秒）"
    except requests.RequestException as exc:
        return None, f"POST /api/orders 请求异常：{exc}"

    if response.status_code not in (200, 201):
        return None, f"POST /api/orders 未实现或失败（HTTP {response.status_code}）"

    try:
        body = response.json()
    except ValueError:
        return None, "POST /api/orders 返回的不是合法 JSON"

    if not isinstance(body, dict):
        return None, "POST /api/orders 返回的数据格式不是对象"

    api_order_no = str(body.get("order_no") or order_no)
    record_id = _extract_record_id(body)
    order_id = _extract_order_id(body)

    if record_id is None and order_id is not None:
        record_id = _fetch_first_record_id(order_id)

    if order_id is None and record_id is not None:
        order_id = _lookup_order_id_by_record(record_id)

    note = ""
    if record_id is None:
        record_id = DEFAULT_VERIFY_RECORD_ID
        note = f"后端响应未返回核销记录 ID，已回退到默认记录 #{DEFAULT_VERIFY_RECORD_ID}。"
    if order_id is None:
        note += "后端响应未返回订单 ID，退款 / 分账需手动填写订单 ID。"

    return (
        OrderCreation(
            order_no=api_order_no,
            record_id=record_id,
            source="后端 POST /api/orders",
            note=note.strip(),
            order_id=order_id,
        ),
        "",
    )


_local_db_ready = False
_local_db_lock = threading.Lock()


def _ensure_local_db() -> None:
    """
    确保 main.py 的 ORM 表已创建，并且演示商户数据存在。

    app_scan.py 可能先于 main.py 启动，所以这里做一次幂等初始化。
    """
    global _local_db_ready

    if _local_db_ready:
        return

    with _local_db_lock:
        if _local_db_ready:
            return

        # 延迟导入：如果用户只需要用后端接口，不在启动阶段触发数据库副作用。
        # initialize_database 内含结构迁移，保证旧库也能支持 split 状态。
        from main import initialize_database

        initialize_database()
        _local_db_ready = True


def _create_order_local(order_no: str) -> tuple[str, int, int]:
    """
    使用 SQLAlchemy 直接写入一条测试订单和三条未核销记录。

    200 元套餐拆成“门票 80 + 咖啡 40 + 越野车 80”三个子项，三者由不同商户
    分别核销：景区检票员先核销门票，之后咖啡店员 / 越野车安全员才能核销对应项目。

    返回 (order_no, ticket_record_id, order_id)。ticket_record_id 用于生成
    二维码；order_id 用于后续调用分账 / 退款接口（路径参数是数据库主键）。
    """
    _ensure_local_db()

    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    from main import (
        Merchant,
        Order,
        OrderStatus,
        SessionLocal,
        VerifyRecord,
        VerifyStatus,
    )

    last_error: Exception | None = None

    for attempt in range(3):
        try:
            with SessionLocal() as db:
                merchant_specs = (
                    (TICKET_MERCHANT_NAME, Decimal("0.05")),
                    (CAFE_MERCHANT_NAME, Decimal("0.10")),
                    (OFFROAD_MERCHANT_NAME, OFFROAD_SPLIT_RATE),
                )
                merchants: dict[str, Merchant] = {}
                for name, split_rate in merchant_specs:
                    merchant = db.scalar(
                        select(Merchant).where(Merchant.name == name)
                    )
                    if merchant is None:
                        merchant = Merchant(name=name, split_rate=split_rate)
                        db.add(merchant)
                        db.flush()
                    merchants[name] = merchant

                order = Order(
                    order_no=order_no,
                    total_amount=ORDER_AMOUNT,
                    status=OrderStatus.PAID,
                )
                db.add(order)
                db.flush()

                ticket_record = VerifyRecord(
                    order_id=order.id,
                    merchant_id=merchants[TICKET_MERCHANT_NAME].id,
                    item_name=TICKET_ITEM_NAME,
                    verify_status=VerifyStatus.UNVERIFIED,
                )
                coffee_record = VerifyRecord(
                    order_id=order.id,
                    merchant_id=merchants[CAFE_MERCHANT_NAME].id,
                    item_name=COFFEE_ITEM_NAME,
                    verify_status=VerifyStatus.UNVERIFIED,
                )
                offroad_record = VerifyRecord(
                    order_id=order.id,
                    merchant_id=merchants[OFFROAD_MERCHANT_NAME].id,
                    item_name=OFFROAD_ITEM_NAME,
                    verify_status=VerifyStatus.UNVERIFIED,
                )
                db.add_all([ticket_record, coffee_record, offroad_record])
                db.flush()

                result = (
                    str(order.order_no),
                    int(ticket_record.id),
                    int(order.id),
                )
                record_ids = {
                    "ticket": int(ticket_record.id),
                    "coffee": int(coffee_record.id),
                    "offroad": int(offroad_record.id),
                }
                db.commit()

                logger.info(
                    "本地创建套餐订单：order_no=%s, order_id=%s, "
                    "ticket_record_id=%s, coffee_record_id=%s, "
                    "offroad_record_id=%s",
                    result[0],
                    result[2],
                    record_ids["ticket"],
                    record_ids["coffee"],
                    record_ids["offroad"],
                )
                return result
        except OperationalError as exc:
            last_error = exc
            if "locked" in str(exc).lower() and attempt < 2:
                time.sleep(0.4 * (attempt + 1))
                continue
            raise

    raise last_error or RuntimeError("本地创建订单失败")


def _create_test_order() -> OrderCreation:
    """优先走后端 POST，失败后自动使用本地 SQLAlchemy 兜底。"""
    order_no = _generate_order_no()

    api_result, api_note = _try_create_order_api(order_no)
    if api_result is not None:
        return api_result

    logger.warning("POST /api/orders 不可用：%s；切换为本地 SQLAlchemy 写入", api_note)

    try:
        local_order_no, record_id, order_id = _create_order_local(order_no)
        return OrderCreation(
            order_no=local_order_no,
            record_id=record_id,
            source="本地 SQLAlchemy（tourism.db）",
            note=f"{api_note}；已自动改用本地数据库写入。",
            order_id=order_id,
        )
    except Exception as exc:  # noqa: BLE001 - POC 需要把具体原因展示给用户
        logger.exception("本地创建测试订单失败")
        return OrderCreation(
            order_no=order_no,
            record_id=DEFAULT_VERIFY_RECORD_ID,
            source="仅生成二维码",
            note=f"{api_note}；本地写入也失败：{exc}",
        )


def _make_qr_image(code: str) -> Image.Image:
    """
    生成高容错、纯黑白、带安静区的二维码。

    ERROR_CORRECT_H 大约可恢复 30% 的污损，适合摄像头拍摄屏幕的场景。
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=12,
        border=4,
    )
    qr.add_data(code)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def _fetch_order_items(order_id: int | None) -> list[dict[str, Any]] | None:
    """读取本地库中某订单的全部核销项；读取失败返回 None。"""
    if order_id is None or not DB_PATH.exists():
        return None

    try:
        connection = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            rows = connection.execute(
                "SELECT id, item_name, verify_status FROM verify_records "
                "WHERE order_id = ? ORDER BY id",
                (int(order_id),),
            ).fetchall()
        finally:
            connection.close()
    except (sqlite3.Error, TypeError, ValueError):
        logger.warning("读取订单状态失败：order_id=%s", order_id, exc_info=True)
        return None

    return [
        {
            "id": int(row[0]),
            "item_name": str(row[1] or ""),
            "verify_status": str(row[2] or ""),
        }
        for row in rows
    ]


def _status_item_html(
    icon: str,
    label: str,
    pending_text: str,
    done_text: str,
    verify_status: str,
) -> str:
    """渲染单个子项状态：未核销 / 已核销 / 已退款。"""
    if verify_status == "verified":
        badge_class = "status-badge-done"
        item_class = " is-done"
        text = done_text
    elif verify_status == "refunded":
        badge_class = "status-badge-refunded"
        item_class = " is-refunded"
        text = "已退款"
    else:
        badge_class = "status-badge-pending"
        item_class = ""
        text = pending_text
    return (
        f'<div class="status-item{item_class}">'
        f'<span class="status-icon">{icon}</span>'
        '<div class="status-text">'
        f'<div class="status-label">{label}</div>'
        f'<span class="status-badge {badge_class}">{text}</span>'
        "</div></div>"
    )


LIFECYCLE_STATUS_META = {
    "not_applicable": ("不适用", "is-na"),
    "not_started": ("未入园", "is-not-started"),
    "active": ("游园中", "is-active"),
    "completed": ("已结束", "is-completed"),
}


def _local_time_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")


def _fetch_order_lifecycle(order_id: int | None) -> dict[str, Any] | None:
    if order_id is None:
        return None
    try:
        response = requests.get(
            f"{API_BASE_URL}/api/order/{int(order_id)}/lifecycle",
            timeout=min(REQUEST_TIMEOUT, 2),
        )
    except (requests.RequestException, TypeError, ValueError):
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _render_lifecycle_html(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    status = str(data.get("status") or "")
    label, css_class = LIFECYCLE_STATUS_META.get(
        status, ("状态未知", "is-na")
    )
    chips = []
    if data.get("can_split"):
        chips.append('<span class="lifecycle-chip is-ok">可分账</span>')
    else:
        chips.append(
            '<span class="lifecycle-chip is-wait">分账待生命周期结束</span>'
        )
    if data.get("can_refund"):
        chips.append('<span class="lifecycle-chip is-ok">可退款</span>')
    else:
        chips.append('<span class="lifecycle-chip is-muted">不可退款</span>')

    meta_parts = []
    entered = _local_time_label(data.get("entered_at"))
    exited = _local_time_label(data.get("exited_at"))
    leave_at = _local_time_label(data.get("leave_at"))
    deadline = _local_time_label(data.get("refund_deadline"))
    if entered:
        meta_parts.append(f"入园 {entered}")
    if exited:
        meta_parts.append(f"离园 {exited}")
    elif leave_at:
        meta_parts.append(f"闭园 {leave_at}")
    if deadline:
        meta_parts.append(f"退款截止 {deadline}")

    message = html.escape(str(data.get("message") or ""))
    meta_html = (
        f'<div class="lifecycle-meta">{" · ".join(meta_parts)}</div>'
        if meta_parts
        else ""
    )
    return (
        '<div class="lifecycle-panel">'
        '<div class="lifecycle-head">'
        f'<span class="lifecycle-badge {css_class}">{label}</span>'
        f'{"".join(chips)}'
        "</div>"
        f'<div class="lifecycle-message">{message}</div>'
        f"{meta_html}"
        "</div>"
    )


def _render_order_status(state: Any) -> str:
    """
    渲染【我的订单状态】区域。

    直接读取本地 SQLite，因此商家端核销后，游客端刷新（或等待自动刷新）
    就能看到“已使用 / 已兑换”的最新状态。
    """
    order_id = state.get("order_id") if isinstance(state, dict) else None
    order_no = str(state.get("order_no") or "") if isinstance(state, dict) else ""

    if not order_id:
        return (
            '<div class="status-panel status-panel-empty">'
            '<div class="status-panel-head">'
            '<span class="status-panel-title">📋 我的订单状态</span>'
            "</div>"
            '<p class="status-empty-text">还没有订单。点击上方【立即购买】后，'
            "这里会显示门票、咖啡和越野车的使用状态。</p>"
            "</div>"
        )

    items = _fetch_order_items(order_id)
    if items is None:
        return (
            '<div class="status-panel status-panel-empty">'
            '<div class="status-panel-head">'
            '<span class="status-panel-title">📋 我的订单状态</span>'
            "</div>"
            '<p class="status-empty-text">暂时无法获取订单状态，'
            "请稍后点击“刷新订单状态”。</p>"
            "</div>"
        )

    # 兼容早期把套餐写成单条组合记录的历史订单：纯单项记录优先，
    # 找不到时再回退到“门票+咖啡（+越野车）”组合记录。
    item_keywords = ("门票", "咖啡", "越野车")
    pure_items = [
        item
        for item in items
        if sum(keyword in item["item_name"] for keyword in item_keywords) == 1
    ]
    combo_items = [item for item in items if item not in pure_items]

    def _find_item(keyword: str) -> dict[str, Any] | None:
        return next(
            (item for item in pure_items if keyword in item["item_name"]),
            next(
                (item for item in combo_items if keyword in item["item_name"]),
                None,
            ),
        )

    ticket = _find_item("门票")
    coffee = _find_item("咖啡")
    offroad = _find_item("越野车")

    def _status_of(item: dict[str, Any] | None) -> str:
        return str(item["verify_status"]) if item else "unverified"

    ticket_status = _status_of(ticket)
    coffee_status = _status_of(coffee)
    offroad_status = _status_of(offroad)
    ticket_done = ticket_status == "verified"

    # 只展示订单实际包含的项目，兼容旧的“门票+咖啡”两项订单。
    included_items = [
        (label, item, status)
        for label, item, status in (
            ("门票", ticket, ticket_status),
            ("咖啡", coffee, coffee_status),
            ("越野车", offroad, offroad_status),
        )
        if item is not None
    ]

    if ticket is not None and ticket_status == "refunded":
        tip = "门票已退款，无法继续使用；如仍需游玩请在游客端重新购票。"
    elif ticket is not None and not ticket_done:
        tip = "请先到检票口完成门票核销；门票核销后，咖啡和越野车项目才会开放。"
    else:
        pending_labels = [
            label for label, _item, status in included_items if status == "unverified"
        ]
        refunded_labels = [
            label for label, _item, status in included_items if status == "refunded"
        ]
        if pending_labels:
            prefix = "门票已核销，" if ticket_done else ""
            tip = (
                f"{prefix}请保留这张二维码前往"
                f"{'、'.join(pending_labels)}项目完成剩余核销。"
            )
            if refunded_labels:
                tip += f"（{'、'.join(refunded_labels)}已退款，无需核销）"
        elif refunded_labels:
            tip = (
                f"订单未核销项目（{'、'.join(refunded_labels)}）已退款，"
                "已核销项目仍可正常分账结算。"
            )
        else:
            tip = "订单项目已全部使用，祝您游玩愉快！"

    order_no_html = (
        f'<span class="status-order-no">订单号 {html.escape(order_no)}</span>'
        if order_no
        else ""
    )
    item_html = {
        "门票": _status_item_html(
            "🎫", "门票", "待使用", "已使用", ticket_status
        ),
        "咖啡": _status_item_html(
            "☕", "咖啡", "待兑换", "已兑换", coffee_status
        ),
        "越野车": _status_item_html(
            "🚙", "越野车", "待体验", "已体验", offroad_status
        ),
    }
    items_html = "".join(
        item_html[label] for label, _item, _status in included_items
    )

    lifecycle_html = _render_lifecycle_html(_fetch_order_lifecycle(order_id))
    return (
        '<div class="status-panel">'
        '<div class="status-panel-head">'
        '<span class="status-panel-title">📋 我的订单状态</span>'
        f"{order_no_html}"
        "</div>"
        f'<div class="status-items">{items_html}</div>'
        f'<p class="status-tip">{tip}</p>'
        f"{lifecycle_html}"
        "</div>"
    )


_QR_NOTE_HTML = (
    '<div class="qr-note">'
    '<div class="qr-note-title">📲 核销说明</div>'
    "<p><strong>【门票】</strong>、<strong>【咖啡】</strong>与"
    "<strong>【越野车】</strong>需分别扫码核销。"
    "请先在检票口核销门票，之后才能前往咖啡馆和越野车体验区；"
    "每次核销后请妥善保留此码。</p>"
    "<p>离园时请在景区出口扫码登记；再次入园需重新购票。</p>"
    "</div>"
)

_QR_PLACEHOLDER = (
    '<div class="qr-placeholder">'
    '<div class="qr-placeholder-icon">🎫</div>'
    "<p>完成支付后，您的电子二维码会显示在这里。</p>"
    "</div>"
)

# 醒目提示：位于二维码展示区域下方。
_QR_SCAN_TIP_HTML = (
    '<div class="qr-scan-tip" role="note">'
    '<span class="qr-scan-tip-icon">📷</span>'
    "请到商家核销处出示二维码；门票核销后，咖啡与越野车项目才会开放"
    "</div>"
)


def purchase() -> tuple[Any, str, str, int | None, dict[str, Any], str]:
    """
    游客端按钮事件：完成支付、生成电子门票二维码，并返回订单状态。

    返回：二维码更新（成功后 visible=True，失败时保持隐藏）、购票成功提示、
    二维码下方说明、订单 ID、会话状态、订单状态卡片。
    """
    creation = _create_test_order()
    code = f"{CODE_PREFIX}{creation.record_id:04d}"

    try:
        qr_image = _make_qr_image(code)
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成二维码失败")
        raise gr.Error(
            "电子门票生成失败，请稍后重试。",
            title="购买未完成",
            print_exception=False,
        ) from exc

    # 供“订单状态 / 申请退款 / 执行分账”使用的会话状态。
    order_state: dict[str, Any] = {
        "order_id": creation.order_id,
        "order_no": creation.order_no,
        "record_id": creation.record_id,
        "source": creation.source,
    }

    if creation.source == "仅生成二维码":
        logger.warning("购票未能写入订单：%s", creation.note)
        gr.Warning("购买遇到问题，请向现场工作人员求助。")
        return (
            # 购票未成功：不展示二维码，保持右侧占位说明。
            gr.update(visible=False),
            _status_card(
                "warning",
                "购买遇到问题，请向现场工作人员求助",
                "请向现场工作人员出示订单信息协助处理。",
            ),
            _QR_PLACEHOLDER,
            None,
            order_state,
            _render_order_status(order_state),
        )

    success_html = (
        '<div class="purchase-success" role="status">'
        '<div class="purchase-success-icon">✅</div>'
        '<div class="purchase-success-text">'
        '<div class="purchase-success-title">'
        "购票成功！电子门票已生成，请先到景区检票口出示二维码完成门票核销。"
        "</div>"
        '<div class="purchase-success-meta">'
        f"订单号：{html.escape(creation.order_no)} · {html.escape(PRODUCT_NAME)}"
        "</div></div></div>"
    )

    gr.Info("购票成功！请向景区检票员出示二维码", title="购票成功")
    return (
        # 购票成功后才把二维码显示到右侧展示区。
        gr.update(value=qr_image, visible=True),
        success_html,
        _QR_NOTE_HTML,
        creation.order_id,
        order_state,
        _render_order_status(order_state),
    )


# ===========================================================================
# 六、二维码识别（OpenCV）
# ===========================================================================

def _to_gray_variants(image: Any) -> list[np.ndarray]:
    """
    把 Gradio 传入的 numpy 图像转成灰度图。

    Gradio webcam 的 numpy 图像通常是 RGB；为兼容 cv2.imread 读入的
    BGR 图像，3 通道时同时生成 RGB→Gray 和 BGR→Gray 两个版本。
    """
    if image is None:
        return []

    # 兼容极少数情况下传入 {"path": "..."} 的组件返回值。
    if isinstance(image, dict):
        path = image.get("path")
        if not path or not os.path.exists(path):
            return []
        loaded = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if loaded is None:
            return []
        image = loaded

    array = np.asarray(image)
    if array.size == 0:
        return []

    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)

    if array.ndim == 2:
        return [array]

    if array.ndim != 3:
        return []

    channels = array.shape[2]
    if channels == 4:
        return [cv2.cvtColor(array, cv2.COLOR_RGBA2GRAY)]
    if channels == 3:
        return [
            cv2.cvtColor(array, cv2.COLOR_RGB2GRAY),
            cv2.cvtColor(array, cv2.COLOR_BGR2GRAY),
        ]
    return []


def _upscale_for_qr(gray: np.ndarray) -> np.ndarray:
    """小图放大后二维码模块更清晰；大图保持原尺寸，避免细节被抹掉。"""
    height, width = gray.shape[:2]
    longest = max(height, width)
    scale = max(1.0, min(2.5, 1280.0 / max(longest, 1)))
    if scale <= 1.01:
        return gray

    new_size = (int(round(width * scale)), int(round(height * scale)))
    return cv2.resize(gray, new_size, interpolation=cv2.INTER_CUBIC)


def _candidate_images(gray: np.ndarray):
    """依次产出不同预处理版本，任一版本识别成功即可。"""
    base = _upscale_for_qr(gray)
    yield base

    # CLAHE 提升低对比度画面（例如背光、屏幕反光）。
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(base)
    yield clahe

    # 轻度锐化，缓解摄像头对焦不准。
    blurred = cv2.GaussianBlur(base, (0, 0), 2.0)
    sharpened = cv2.addWeighted(base, 1.6, blurred, -0.6, 0)
    yield sharpened

    # Otsu 全局阈值：适合黑白分明的屏幕二维码。
    denoised = cv2.GaussianBlur(base, (3, 3), 0)
    _, otsu = cv2.threshold(
        denoised,
        0,
        255,
        cv2.THRESH_BINARY + cv2.THRESH_OTSU,
    )
    yield otsu

    # 自适应阈值：适合光照不均匀、阴影明显的照片。
    adaptive = cv2.adaptiveThreshold(
        denoised,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        5,
    )
    yield adaptive

    # 反色版本：兼容白码黑底或某些摄像头自动反相的情况。
    yield cv2.bitwise_not(otsu)

    # 旋转兜底。OpenCV 通常能识别旋转二维码，但并非所有版本都稳定。
    for rotate_code in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
        rotated = cv2.rotate(base, rotate_code)
        yield rotated
        _, rotated_otsu = cv2.threshold(
            cv2.GaussianBlur(rotated, (3, 3), 0),
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        yield rotated_otsu


def _decode_single(detector: cv2.QRCodeDetector, image: np.ndarray) -> str | None:
    """在一个预处理版本上，依次尝试单码 / 多码 / 曲面识别 API。"""
    try:
        data, _points, _straight = detector.detectAndDecode(image)
        if data and str(data).strip():
            return str(data).strip()
    except (cv2.error, TypeError):
        pass

    try:
        success, decoded_infos, _points, _straight = detector.detectAndDecodeMulti(image)
        if success:
            for info in decoded_infos:
                if info and str(info).strip():
                    return str(info).strip()
    except (cv2.error, AttributeError, TypeError):
        pass

    # OpenCV 4.5+ / 5.x 提供曲面二维码识别，适合轻微倾斜或弯曲的纸质码。
    if hasattr(detector, "detectAndDecodeCurved"):
        try:
            data, _points, _straight = detector.detectAndDecodeCurved(image)
            if data and str(data).strip():
                return str(data).strip()
        except (cv2.error, TypeError):
            pass

    return None


def decode_qr(image: Any) -> str | None:
    """
    稳定的二维码识别入口。

    识别顺序：
        原图 → 放大 → CLAHE → 锐化 → Otsu → 自适应阈值 → 反色 → 旋转兜底
    每一步都会尝试 detectAndDecode / detectAndDecodeMulti / detectAndDecodeCurved。
    """
    gray_variants = _to_gray_variants(image)
    if not gray_variants:
        return None

    # QRCodeDetector 不是线程安全对象，每次识别单独创建，避免并发事件互相影响。
    detector = cv2.QRCodeDetector()

    for gray in gray_variants:
        for candidate in _candidate_images(gray):
            # 跳过过小的图，避免 OpenCV 在极端输入上抛异常。
            if candidate.shape[0] < 20 or candidate.shape[1] < 20:
                continue
            decoded = _decode_single(detector, candidate)
            if decoded:
                logger.info("二维码识别成功：%s", decoded)
                return decoded

    logger.info("二维码识别失败：未找到可解码的二维码")
    return None


# ===========================================================================
# 七、核销
# ===========================================================================

def _record_exists_local(record_id: int) -> bool:
    """查询本地 SQLite，判断二维码里的记录 ID 是否真实存在。"""
    if not DB_PATH.exists():
        return False

    try:
        connection = sqlite3.connect(str(DB_PATH), timeout=2)
        try:
            row = connection.execute(
                "SELECT 1 FROM verify_records WHERE id = ? LIMIT 1",
                (record_id,),
            ).fetchone()
            return row is not None
        finally:
            connection.close()
    except sqlite3.Error:
        logger.warning("查询本地核销记录失败：record_id=%s", record_id, exc_info=True)
        return False


def _lookup_order_items(order_id: int) -> list[dict[str, Any]] | None:
    """读取本地库中某订单的全部核销项；订单不存在或读取失败返回 None。"""
    if not DB_PATH.exists():
        return None

    try:
        connection = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=2
        )
        try:
            rows = connection.execute(
                "SELECT id, item_name, verify_status FROM verify_records "
                "WHERE order_id = ? ORDER BY id",
                (order_id,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        logger.warning(
            "按订单查询核销项失败：order_id=%s", order_id, exc_info=True
        )
        return None

    if not rows:
        return None

    return [
        {
            "id": int(item[0]),
            "item_name": str(item[1] or ""),
            "verify_status": str(item[2] or ""),
        }
        for item in rows
    ]


def _post_config(post_label: Any) -> dict[str, str]:
    label = str(post_label or "").strip()
    return SCAN_POSTS.get(label, SCAN_POSTS[DEFAULT_VERIFY_POST])


def _overlay_html(kind: str, headline: str) -> str:
    """
    生成全屏现场反馈层。

    - success：绿色大圆圈 + 对号；
    - error：红色大圆圈 + 叉号；
    - warning：琥珀色大圆圈 + 感叹号。

    每次渲染都带唯一 data-seq，确保 Gradio 重新插入节点时 CSS 动画与提示音
    都会重新触发。动画结束后自动淡出，不遮挡后续扫码。
    """
    icons = {"success": "✓", "error": "✕", "warning": "!"}
    icon = icons.get(kind, "•")
    token = time.time_ns()
    return (
        f'<div class="verify-overlay verify-overlay-{kind}" '
        f'data-tone="{html.escape(kind)}" data-seq="{token}">'
        f'<div class="verify-overlay-circle">'
        f'<span class="verify-overlay-icon">{icon}</span>'
        f"</div>"
        f'<div class="verify-overlay-text">{html.escape(headline)}</div>'
        f"</div>"
    )


def _verification_result(
    kind: str,
    status_title: str,
    status_body: str,
    headline: str,
) -> tuple[str, str]:
    """同时返回页面内状态卡片和全屏反馈层。"""
    return (
        _status_card(kind, status_title, status_body),
        _overlay_html(kind, headline),
    )


def _response_detail(response: requests.Response) -> str:
    """读取 FastAPI HTTPException.detail，兼容字符串与结构化 detail。"""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    if detail is None:
        return ""
    return json.dumps(detail, ensure_ascii=False)


def _submit_exit_request(
    seed_record_id: int,
    *,
    origin: str = "",
) -> tuple[str, str]:
    exit_url = f"{API_BASE_URL}/api/verify-records/{seed_record_id}/exit"
    logger.info("调用离园接口：POST %s（来源：%s）", exit_url, origin or "出口扫码")
    try:
        response = requests.post(exit_url, timeout=REQUEST_TIMEOUT)
    except requests.ConnectionError:
        return _verification_result(
            "error", "离园登记失败",
            f"无法连接后端（{html.escape(API_BASE_URL)}），请联系现场技术人员。",
            "❌ 离园登记失败，请检查网络后重试",
        )
    except requests.Timeout:
        return _verification_result(
            "error", "离园登记失败",
            f"后端在 {REQUEST_TIMEOUT:g} 秒内没有响应，请稍后重试。",
            "❌ 离园登记失败，请稍后重试",
        )
    except requests.RequestException:
        return _verification_result(
            "error", "离园登记失败", "离园请求异常，请稍后重试。",
            "❌ 离园登记失败，请稍后重试",
        )

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = str(body.get("message") or "离园登记成功。")
        detail = html.escape(message)
        if body.get("can_split"):
            detail += "<br><b>生命周期已结束，可以进行分账。</b>"
        else:
            detail += "<br>生命周期尚未结束，暂不可分账。"
        return _verification_result(
            "success", "离园登记成功", detail, "👋 离园登记成功"
        )

    detail_text = _response_detail(response)
    if response.status_code == 409:
        return _verification_result(
            "warning", "尚未入园，无法离园",
            html.escape(detail_text) or "请先核销门票入园。",
            "⚠️ 尚未入园，无法离园",
        )
    if response.status_code == 404:
        return _verification_result(
            "error", "未找到这张门票",
            "请让游客重新出示购票二维码，或到售票处核验订单。",
            "❌ 未找到这张门票",
        )
    return _verification_result(
        "error", "离园登记失败", "离园系统暂时不可用，请稍后重试。",
        "❌ 离园登记失败，请稍后重试",
    )


def _submit_verify_request(
    seed_record_id: int,
    post_label: Any,
    *,
    origin: str = "",
) -> tuple[str, str]:
    """
    调用后端岗位核销接口（摄像头扫码与备用演示区共用）。

    seed_record_id 可以是订单内任意一条核销记录（二维码里通常是门票记录）；
    后端根据 post 只核销当前岗位负责的子项，不再按顺序自动核销下一项。

    返回 (页面内状态卡片 HTML, 全屏现场反馈 HTML)，不再抛出 gr.Error。
    """
    config = _post_config(post_label)
    if config["key"] == "exit":
        return _submit_exit_request(seed_record_id, origin=origin)
    post_key = config["key"]
    post_name = str(post_label or DEFAULT_VERIFY_POST)
    verify_url = f"{API_BASE_URL}/api/verify-records/{seed_record_id}/verify"
    logger.info(
        "调用核销接口：POST %s（岗位：%s，来源：%s）",
        verify_url,
        post_key,
        origin or "摄像头扫码",
    )

    try:
        response = requests.post(
            verify_url,
            params={"post": post_key},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.ConnectionError:
        return _verification_result(
            "error",
            "核销失败",
            f"无法连接后端（{html.escape(API_BASE_URL)}），请联系现场技术人员。",
            "❌ 核销失败，请检查网络后重试",
        )
    except requests.Timeout:
        return _verification_result(
            "error",
            "核销失败",
            f"后端在 {REQUEST_TIMEOUT:g} 秒内没有响应，请稍后重试。",
            "❌ 核销失败，请稍后重试",
        )
    except requests.RequestException:
        return _verification_result(
            "error",
            "核销失败",
            "核销请求异常，请稍后重试。",
            "❌ 核销失败，请稍后重试",
        )

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            body = {}

        item_name = ""
        if isinstance(body, dict):
            item_name = str(body.get("item_name") or config["item"])

        headline = f"✅ {item_name}核销成功，祝您游玩愉快！"
        detail = (
            f"{config['icon']} 本次核销项目：<b>{html.escape(item_name)}</b>"
            f"<br>当前岗位：{html.escape(post_name)}"
        )
        if origin:
            detail += f"<br>触发方式：{html.escape(origin)}"
        return _verification_result(
            "success",
            f"{item_name}核销成功，祝您游玩愉快！",
            detail,
            headline,
        )

    detail_text = _response_detail(response)

    if response.status_code == 409:
        if "门票未核销" in detail_text:
            return _verification_result(
                "warning",
                "门票未核销，请先去检票口",
                "游客尚未完成门票核销。请先到检票口由景区检票员核销门票，再回来兑换。",
                "⚠️ 门票未核销，请先去检票口",
            )

        return _verification_result(
            "error",
            "该票已核销，请勿重复扫码！",
            f"当前岗位（{html.escape(post_name)}）负责的项目已经核销过，请勿重复扫码。",
            "❌ 该票已核销，请勿重复扫码！",
        )

    if response.status_code == 404:
        if "不含" in detail_text:
            return _verification_result(
                "warning",
                detail_text or "该订单不含当前岗位项目",
                "请核对游客购买的套餐内容，或切换正确的核销岗位后重试。",
                f"⚠️ {detail_text or '该订单不含当前岗位项目'}",
            )
        return _verification_result(
            "error",
            "未找到这张门票",
            "请让游客重新出示购票二维码，或到售票处核验订单。",
            "❌ 未找到这张门票，请重新出示二维码",
        )

    return _verification_result(
        "error",
        "核销失败",
        "核销系统暂时不可用，请稍后重试。",
        "❌ 核销失败，请稍后重试",
    )


def _resolve_manual_seed_record_id(raw_input: Any) -> tuple[int | None, str]:
    """
    解析备用演示区的手动输入，返回订单内任意一条核销记录 ID。

    岗位核销只需要知道“这是哪张订单”，因此这里返回的 ID 仅作为定位种子：
      - 电子票号：TK-8（与摄像头扫描的二维码内容一致）；
      - 订单 ID：8（取该订单的第一条核销记录）；
      - 兼容纯数字写法的核销记录号（该数字不存在对应订单时）。
    """
    text = str(raw_input or "").strip()
    if not text:
        return None, "请输入电子票号或订单 ID。"

    match = CODE_PATTERN.fullmatch(text.upper())
    if match is not None:
        return int(match.group(1)), ""

    if re.fullmatch(r"\d{1,9}", text):
        numeric = int(text)
        items = _lookup_order_items(numeric)
        if items:
            return int(items[0]["id"]), ""
        if _record_exists_local(numeric):
            return numeric, ""
        return None, f"未找到订单或电子票：{text}，请核对后再试。"

    return None, (
        "票号格式不正确：电子票号请以 TK- 开头（如 TK-8），"
        "也可直接输入订单 ID。"
    )


def simulate_verify(
    raw_input: Any,
    post_label: Any = DEFAULT_VERIFY_POST,
) -> tuple[str, Any]:
    """备用演示区：跳过摄像头，按当前岗位核销输入框里的票号。"""
    seed_record_id, message = _resolve_manual_seed_record_id(raw_input)
    if seed_record_id is None:
        return _status_card("warning", "请核对票号", message), gr.update()
    return _submit_verify_request(
        seed_record_id,
        post_label,
        origin="备用演示区（模拟扫码）",
    )


def _scan_idle_status() -> str:
    """摄像头没有识别到有效二维码时的唯一提示。"""
    return _status_card("info", "未识别到二维码，请对准摄像头")


def scan_and_verify(
    image: Any,
    post_label: Any,
    state: Any,
) -> tuple[Any, Any, dict[str, Any]]:
    """
    摄像头流式识别：识别到二维码的瞬间自动调用后端核销，无需提交按钮。

    防重复策略：
      - 同一张二维码持续停留在镜头中时只核销一次；
      - 二维码移开超过 SCAN_REARM_SECONDS 后再次出现，才重新核销并给出
        “该票已核销”提示；
      - 切换岗位后允许同一张二维码立即重新核销（咖啡馆 / 越野车场景）。
    """
    scan_state = dict(state) if isinstance(state, dict) else {}
    now = time.monotonic()
    post_name = str(post_label or DEFAULT_VERIFY_POST)

    if scan_state.get("post") != post_name:
        scan_state["post"] = post_name
        scan_state["code"] = ""

    decoded = ""
    if image is not None:
        try:
            raw_code = decode_qr(image)
        except Exception:  # noqa: BLE001 - 摄像头帧异常不能打断现场扫码
            logger.warning("摄像头画面解码异常", exc_info=True)
            raw_code = None
        if raw_code:
            decoded = str(raw_code).strip().upper()

    match = CODE_PATTERN.fullmatch(decoded) if decoded else None

    if match is None:
        last_seen = float(scan_state.get("seen_at") or 0.0)
        if scan_state.get("code") and now - last_seen >= SCAN_REARM_SECONDS:
            scan_state["code"] = ""
        if scan_state.get("idle"):
            return gr.update(), gr.update(), scan_state
        scan_state["idle"] = True
        return _scan_idle_status(), gr.update(), scan_state

    if decoded == scan_state.get("code"):
        # 同一张二维码仍在镜头中：只刷新停留时间，绝不重复调用核销接口。
        scan_state["seen_at"] = now
        return gr.update(), gr.update(), scan_state

    scan_state["code"] = decoded
    scan_state["seen_at"] = now
    scan_state["idle"] = False

    status_html, overlay_html = _submit_verify_request(
        int(match.group(1)),
        post_name,
        origin=f"{post_name}摄像头扫码",
    )
    return status_html, overlay_html, scan_state


# ===========================================================================
# 八、Gradio 界面
# ===========================================================================

CSS = """
/* ---------------------------------------------------------------
   游客友好的整体底色与版心
   --------------------------------------------------------------- */
.gradio-container {
    max-width: 1120px !important;
    margin: 0 auto !important;
    padding: 18px 18px 42px !important;
    background:
        radial-gradient(1100px 420px at 10% -8%, rgba(16, 185, 129, 0.10), transparent 60%),
        radial-gradient(900px 380px at 94% -6%, rgba(245, 158, 11, 0.13), transparent 55%),
        #f8f7f2;
    min-height: 100vh;
}
/* 隐藏 Gradio 默认页脚，避免游客看到“通过 API 使用”等开发信息。 */
footer,
.footer {
    display: none !important;
}
.scan-hero {
    position: relative;
    overflow: hidden;
    padding: 30px 34px;
    margin-bottom: 12px;
    border-radius: 26px;
    color: #ffffff;
    background: linear-gradient(118deg, #0b3d2e 0%, #11634d 54%, #8a5a22 100%);
    box-shadow: 0 18px 40px rgba(11, 61, 46, 0.24);
}
.scan-hero::after {
    content: "🌿";
    position: absolute;
    right: 30px;
    top: 50%;
    transform: translateY(-50%) rotate(-8deg);
    font-size: 5.4rem;
    opacity: 0.18;
    pointer-events: none;
}
.scan-hero h1 {
    margin: 0 0 8px;
    color: #ffffff !important;
    font-size: 1.95rem;
    font-weight: 800;
    letter-spacing: 0.01em;
}
.scan-hero p {
    margin: 0;
    color: rgba(248, 250, 252, 0.84) !important;
    font-size: 0.98rem;
    letter-spacing: 0.02em;
}
.hero-badge {
    display: inline-block;
    margin-bottom: 12px;
    padding: 5px 13px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.16);
    border: 1px solid rgba(255, 255, 255, 0.22);
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.08em;
}
.scan-panel {
    padding: 6px 4px;
}
/* 备用演示区：与摄像头扫码区分隔，输入框与按钮底部对齐。 */
.backup-divider {
    margin: 22px 0 4px;
    border-top: 1px dashed #cbd5e1;
}
.backup-row {
    align-items: flex-end !important;
}
.backup-hint {
    margin-top: 2px;
    color: #64748b;
    font-size: 0.85rem;
    line-height: 1.5;
}
.tourist-panel {
    padding: 8px 6px;
}

/* ---------------------------------------------------------------
   购票区 + 二维码区：强制左右两列
   左侧内容再长，也不会把二维码挤到页面底部
   --------------------------------------------------------------- */
.tourist-layout {
    flex-wrap: nowrap !important;
    align-items: flex-start !important;
    gap: 18px !important;
}
.buy-column {
    flex: 0 1 46% !important;
    min-width: 0 !important;
}
.qr-column {
    flex: 1 1 52% !important;
    min-width: 380px !important;
}
.qr-scan-tip {
    margin-top: 14px;
    padding: 16px 20px;
    border-radius: 16px;
    background: linear-gradient(120deg, #f59e0b, #f97316);
    color: #ffffff;
    font-size: 1.05rem;
    font-weight: 800;
    line-height: 1.6;
    text-align: center;
    letter-spacing: 0.02em;
    box-shadow: 0 12px 26px rgba(245, 158, 11, 0.28);
}
.qr-scan-tip-icon {
    margin-right: 6px;
    font-size: 1.2rem;
}

/* ---------------------------------------------------------------
   商品信息卡片
   --------------------------------------------------------------- */
.product-card {
    position: relative;
    overflow: hidden;
    margin: 4px 0 14px;
    padding: 26px 28px 24px;
    border-radius: 24px;
    background: #ffffff;
    border: 1px solid #ece6d8;
    box-shadow: 0 20px 44px rgba(74, 84, 63, 0.10);
}
.product-card::after {
    content: "🏞️";
    position: absolute;
    right: 22px;
    top: 12px;
    font-size: 4.6rem;
    opacity: 0.10;
    transform: rotate(-6deg);
    pointer-events: none;
}
.product-badge {
    display: inline-block;
    padding: 4px 11px;
    border-radius: 999px;
    background: linear-gradient(120deg, #f59e0b, #ea580c);
    color: #ffffff;
    font-size: 0.76rem;
    font-weight: 800;
    letter-spacing: 0.06em;
}
.product-kicker {
    display: inline-block;
    margin-left: 8px;
    font-size: 0.8rem;
    color: #94a3b8;
    letter-spacing: 0.08em;
}
.product-name {
    margin: 12px 0 4px;
    font-size: 1.85rem;
    font-weight: 800;
    color: #14281f;
    letter-spacing: 0.01em;
}
.product-subtitle {
    margin: 0 0 18px;
    color: #7c8a80;
    font-size: 0.95rem;
}
.product-includes {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    padding: 16px 18px;
    border-radius: 16px;
    background: #f5f8f2;
    border: 1px dashed #cfe0c8;
}
.include-chip {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 9px 15px;
    border-radius: 12px;
    background: #ffffff;
    border: 1px solid #e2eadb;
    color: #24382c;
    font-size: 0.97rem;
    font-weight: 700;
    box-shadow: 0 2px 6px rgba(20, 40, 31, 0.04);
}
.include-chip b {
    color: #0f766e;
}
.include-plus {
    font-size: 1.2rem;
    font-weight: 800;
    color: #94a3b8;
}
.product-rules {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 9px 20px;
    margin: 18px 0;
    padding: 0;
    list-style: none;
}
.product-rules li {
    position: relative;
    padding-left: 24px;
    color: #4b5d52;
    font-size: 0.92rem;
    line-height: 1.55;
}
.product-rules li::before {
    content: "✓";
    position: absolute;
    left: 0;
    top: 2px;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 17px;
    height: 17px;
    border-radius: 50%;
    background: #dcfce7;
    color: #15803d;
    font-size: 0.72rem;
    font-weight: 800;
}
.product-price {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 12px;
    padding-top: 16px;
    border-top: 1px solid #f0ece1;
}
.price-label {
    font-size: 0.85rem;
    font-weight: 700;
    color: #94a3b8;
}
.price-now {
    font-size: 2.15rem;
    font-weight: 900;
    color: #dc2626;
    line-height: 1;
}
.price-now small {
    font-size: 1rem;
}
.price-original {
    font-size: 0.95rem;
    color: #94a3b8;
    text-decoration: line-through;
}
.price-save {
    padding: 4px 10px;
    border-radius: 999px;
    background: #fff7ed;
    border: 1px solid #fed7aa;
    color: #c2410c;
    font-size: 0.78rem;
    font-weight: 800;
}

/* ---------------------------------------------------------------
   购买按钮与安心提示
   --------------------------------------------------------------- */
.buy-button,
.buy-button button {
    min-height: 60px !important;
    border: 0 !important;
    border-radius: 18px !important;
    background: linear-gradient(120deg, #0f766e, #15803d 60%, #16a34a) !important;
    color: #ffffff !important;
    font-size: 1.15rem !important;
    font-weight: 800 !important;
    letter-spacing: 0.02em;
    box-shadow: 0 14px 30px rgba(16, 122, 84, 0.28) !important;
    transition: transform 0.15s ease, filter 0.15s ease;
}
.buy-button:hover,
.buy-button button:hover {
    filter: brightness(1.05);
    transform: translateY(-1px);
}
.buy-assurance {
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    gap: 8px 20px;
    margin: 10px 0 6px;
    color: #7c8a80;
    font-size: 0.82rem;
}

/* ---------------------------------------------------------------
   购票成功 + 二维码说明
   --------------------------------------------------------------- */
.purchase-success {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 10px;
    padding: 17px 20px;
    border-radius: 18px;
    background: #ecfdf5;
    border: 1.5px solid #6ee7b7;
    box-shadow: 0 10px 26px rgba(5, 150, 105, 0.12);
    animation: scan-pop 0.25s ease-out;
}
.purchase-success-icon {
    flex: 0 0 auto;
    font-size: 1.9rem;
    line-height: 1;
}
.purchase-success-title {
    color: #047857;
    font-size: 1.22rem;
    font-weight: 800;
    line-height: 1.5;
}
.purchase-success-meta {
    margin-top: 3px;
    color: #059669;
    font-size: 0.82rem;
    opacity: 0.85;
}
.qr-image-box {
    border-radius: 22px !important;
    overflow: hidden;
    border: 1px solid #ece6d8 !important;
    background: #ffffff !important;
    box-shadow: 0 14px 32px rgba(74, 84, 63, 0.08);
}
.qr-note {
    margin-top: 10px;
    padding: 15px 19px;
    border-radius: 18px;
    background: #fffbeb;
    border: 1px dashed #f59e0b;
    color: #78350f;
    line-height: 1.8;
    font-size: 0.93rem;
}
.qr-note-title {
    margin-bottom: 3px;
    color: #b45309;
    font-weight: 800;
}
.qr-note strong {
    color: #b45309;
}
.qr-placeholder {
    margin-top: 10px;
    padding: 20px;
    border-radius: 18px;
    background: #f8faf7;
    border: 1px dashed #d7e0d0;
    color: #7c8a80;
    text-align: center;
    line-height: 1.7;
    font-size: 0.9rem;
}
.qr-placeholder-icon {
    font-size: 1.7rem;
}
.qr-placeholder p {
    margin: 6px 0 0;
}

/* ---------------------------------------------------------------
   我的订单状态
   --------------------------------------------------------------- */
.status-panel {
    margin: 14px 0 0;
    padding: 20px 22px;
    border-radius: 20px;
    background: #ffffff;
    border: 1px solid #ece6d8;
    box-shadow: 0 14px 32px rgba(74, 84, 63, 0.08);
    animation: scan-pop 0.2s ease-out;
}
.lifecycle-panel { margin-top: 12px; padding: 12px 14px; border-radius: 12px; background: #f8fafc; border: 1px solid #e2e8f0; }
.lifecycle-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.lifecycle-badge { font-size: 12px; font-weight: 700; padding: 3px 10px; border-radius: 999px; }
.lifecycle-badge.is-active { background: #ecfdf5; color: #047857; }
.lifecycle-badge.is-completed { background: #eff6ff; color: #1d4ed8; }
.lifecycle-badge.is-not-started { background: #fffbeb; color: #b45309; }
.lifecycle-badge.is-na { background: #f1f5f9; color: #475569; }
.lifecycle-chip { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #f1f5f9; color: #475569; }
.lifecycle-chip.is-ok { background: #ecfdf5; color: #047857; }
.lifecycle-chip.is-wait { background: #fffbeb; color: #b45309; }
.lifecycle-chip.is-muted { background: #f1f5f9; color: #64748b; }
.lifecycle-message { margin-top: 8px; font-size: 13px; color: #334155; line-height: 1.5; }
.lifecycle-meta { margin-top: 6px; font-size: 12px; color: #64748b; }
.status-panel-empty {
    background: #fbfaf6;
    border-style: dashed;
    box-shadow: none;
}
.status-panel-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 14px;
}
.status-panel-title {
    color: #14281f;
    font-size: 1.08rem;
    font-weight: 800;
}
.status-order-no {
    color: #94a3b8;
    font-size: 0.76rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.status-empty-text {
    margin: 0;
    color: #7c8a80;
    font-size: 0.9rem;
    line-height: 1.7;
}
.status-items {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
}
.status-item {
    flex: 1 1 150px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 16px;
    border-radius: 15px;
    background: #f8faf7;
    border: 1px solid #e5eadf;
    transition: background 0.2s ease, border-color 0.2s ease;
}
.status-item.is-done {
    background: #f0fdf4;
    border-color: #86efac;
}
.status-item.is-refunded {
    background: #fef2f2;
    border-color: #fca5a5;
}
.status-icon {
    font-size: 1.65rem;
    line-height: 1;
}
.status-label {
    margin-bottom: 5px;
    color: #64748b;
    font-size: 0.82rem;
}
.status-badge {
    display: inline-block;
    padding: 3px 11px;
    border-radius: 999px;
    font-size: 0.86rem;
    font-weight: 800;
    line-height: 1.5;
}
.status-badge-pending {
    background: #fef3c7;
    color: #b45309;
    border: 1px solid #fde68a;
}
.status-badge-done {
    background: #dcfce7;
    color: #15803d;
    border: 1px solid #86efac;
}
.status-badge-refunded {
    background: #fee2e2;
    color: #b91c1c;
    border: 1px solid #fca5a5;
}
.status-tip {
    margin: 13px 0 0;
    padding-top: 12px;
    border-top: 1px dashed #ece6d8;
    color: #7c8a80;
    font-size: 0.86rem;
    line-height: 1.7;
}
.refresh-status-button,
.refresh-status-button button {
    max-width: 168px !important;
    min-height: 38px !important;
    margin: 12px auto 0 !important;
    border-radius: 999px !important;
    border: 1px solid #e3ddcf !important;
    background: #ffffff !important;
    color: #5b6b60 !important;
    font-size: 0.82rem !important;
    font-weight: 700 !important;
    box-shadow: none !important;
}
.refresh-status-button:hover,
.refresh-status-button button:hover {
    background: #f6f4ec !important;
    border-color: #d8d1bf !important;
}
.howto-card {
    margin-top: 12px;
    padding: 16px 18px;
    border-radius: 18px;
    background: #f2f7ee;
    border: 1px solid #dbe8d2;
}
.howto-title {
    margin-bottom: 10px;
    color: #3f5d4a;
    font-size: 0.86rem;
    font-weight: 800;
    letter-spacing: 0.03em;
}
.howto-steps {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    color: #4b5d52;
    font-size: 0.87rem;
}
.howto-steps b {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 19px;
    height: 19px;
    margin-right: 5px;
    border-radius: 50%;
    background: #0f766e;
    color: #ffffff;
    font-size: 0.72rem;
}
.howto-arrow {
    color: #a8b8ad;
    font-weight: 800;
}

/* ---------------------------------------------------------------
   状态卡片（商家端 / 错误提示复用）
   --------------------------------------------------------------- */
.scan-card {
    min-height: 96px;
    padding: 16px 18px;
    margin-top: 6px;
    border: 1px solid #e2e8f0;
    border-left-width: 5px;
    border-radius: 14px;
    background: #ffffff;
    line-height: 1.7;
    animation: scan-pop 0.2s ease-out;
}
.scan-card-title {
    font-weight: 700;
    margin-bottom: 4px;
}
.scan-card-body code {
    padding: 2px 7px;
    border-radius: 6px;
    background: rgba(15, 23, 42, 0.06);
    font-size: 0.95em;
}
.scan-card-success {
    color: #166534;
    background: #f0fdf4;
    border-color: #86efac;
    border-left-color: #16a34a;
}
.scan-card-warning {
    color: #92400e;
    background: #fffbeb;
    border-color: #fcd34d;
    border-left-color: #d97706;
}
.scan-card-error {
    color: #991b1b;
    background: #fef2f2;
    border-color: #fca5a5;
    border-left-color: #dc2626;
}
.scan-card-info {
    color: #334155;
    background: #f8fafc;
    border-color: #e2e8f0;
    border-left-color: #94a3b8;
}
@keyframes scan-pop {
    from { opacity: 0; transform: translateY(-4px); }
    to   { opacity: 1; transform: translateY(0); }
}

/* ---------------------------------------------------------------
   全屏现场反馈层：绿色对号 / 红色叉号 / 琥珀色警示
   --------------------------------------------------------------- */
.verify-overlay {
    position: fixed;
    inset: 0;
    z-index: 99999;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 30px;
    padding: 24px;
    background: rgba(4, 15, 10, 0.62);
    backdrop-filter: blur(4px);
    animation: verify-overlay-fade 3.2s ease forwards;
    pointer-events: none;
}
.verify-overlay-circle {
    display: flex;
    align-items: center;
    justify-content: center;
    width: min(44vw, 280px);
    height: min(44vw, 280px);
    border-radius: 50%;
    border: 10px solid rgba(255, 255, 255, 0.92);
    animation: verify-circle-pop 0.38s cubic-bezier(0.2, 1.5, 0.4, 1) both;
}
.verify-overlay-success .verify-overlay-circle {
    background: linear-gradient(145deg, #22c55e, #15803d);
    box-shadow: 0 0 0 18px rgba(34, 197, 94, 0.28),
                0 25px 60px rgba(21, 128, 61, 0.5);
}
.verify-overlay-error .verify-overlay-circle {
    background: linear-gradient(145deg, #ef4444, #b91c1c);
    box-shadow: 0 0 0 18px rgba(239, 68, 68, 0.28),
                0 25px 60px rgba(185, 28, 28, 0.5);
}
.verify-overlay-warning .verify-overlay-circle {
    background: linear-gradient(145deg, #f59e0b, #b45309);
    box-shadow: 0 0 0 18px rgba(245, 158, 11, 0.28),
                0 25px 60px rgba(180, 83, 9, 0.5);
}
.verify-overlay-icon {
    color: #ffffff;
    font-size: clamp(7rem, 20vw, 11rem);
    font-weight: 900;
    line-height: 1;
}
.verify-overlay-text {
    max-width: 92vw;
    color: #ffffff;
    font-size: clamp(1.5rem, 4.6vw, 2.8rem);
    font-weight: 900;
    text-align: center;
    line-height: 1.35;
    text-shadow: 0 4px 18px rgba(0, 0, 0, 0.45);
}
@keyframes verify-circle-pop {
    0%   { transform: scale(0.35); opacity: 0; }
    70%  { transform: scale(1.06); opacity: 1; }
    100% { transform: scale(1); opacity: 1; }
}
@keyframes verify-overlay-fade {
    0%   { opacity: 0; }
    5%   { opacity: 1; }
    82%  { opacity: 1; }
    100% { opacity: 0; visibility: hidden; }
}
#verify-feedback {
    position: static !important;
    overflow: visible !important;
}

@media (max-width: 720px) {
    .gradio-container { padding: 10px 10px 30px !important; }
    .scan-hero { padding: 22px 20px; border-radius: 20px; }
    .scan-hero h1 { font-size: 1.45rem; }
    .scan-hero::after { display: none; }
    .product-card { padding: 20px 18px; }
    .product-name { font-size: 1.5rem; }
    .product-rules { grid-template-columns: 1fr; }
    .purchase-success-title { font-size: 1.05rem; }
    /* 小屏幕才允许上下堆叠，避免两列被压得过窄 */
    .tourist-layout { flex-wrap: wrap !important; }
    .buy-column { flex: 1 1 100% !important; }
    .qr-column { flex: 1 1 100% !important; min-width: 100% !important; }
}
"""

_HERO_HTML = (
    '<div class="scan-hero">'
    '<span class="hero-badge">🌿 乡村文旅 · 轻松出游</span>'
    "<h1>一张二维码，玩转景区、咖啡与越野车</h1>"
    "<p>在线购票 · 扫码入园 · 到店兑换 · 未核销可退</p>"
    "</div>"
)

_PRODUCT_CARD_HTML = """
<div class="product-card">
    <div>
        <span class="product-badge">三项通玩 · 一票畅游</span>
        <span class="product-kicker">景区门票 + 精品咖啡 + 越野车体验</span>
    </div>
    <h2 class="product-name">乡村文旅超值套餐</h2>
    <p class="product-subtitle">一张二维码，玩转景区、咖啡馆与越野车营地。</p>

    <div class="product-includes">
        <div class="include-chip">🎫 景区门票 <b>￥80</b></div>
        <span class="include-plus">+</span>
        <div class="include-chip">☕ 精品咖啡 <b>￥40</b></div>
        <span class="include-plus">+</span>
        <div class="include-chip">🚙 越野车体验 <b>￥80</b></div>
    </div>

    <ul class="product-rules">
        <li>购买后立即生效，一码畅玩三项</li>
        <li>门票须最先核销，咖啡 / 越野车随后开放</li>
        <li>未核销项目均可退款，统一扣 10% 手续费</li>
        <li>已核销项目退款 0 元；全部已核销则不可退款</li>
    </ul>

    <div class="product-price">
        <span class="price-label">套餐价</span>
        <span class="price-now"><small>￥</small>200</span>
        <span class="price-original">门票 + 咖啡 + 越野车</span>
    </div>
</div>
"""

_BUY_ASSURANCE_HTML = (
    '<div class="buy-assurance">'
    "<span>✅ 支付后立即出票</span>"
    "<span>↩️ 未核销可随时退款</span>"
    "<span>📱 无需取纸质票</span>"
    "</div>"
)

_HOWTO_HTML = (
    '<div class="howto-card">'
    '<div class="howto-title">四步轻松使用</div>'
    '<div class="howto-steps">'
    "<span><b>1</b>在线购票</span>"
    '<span class="howto-arrow">→</span>'
    "<span><b>2</b>检票入园</span>"
    '<span class="howto-arrow">→</span>'
    "<span><b>3</b>咖啡馆兑换</span>"
    '<span class="howto-arrow">→</span>'
    "<span><b>4</b>越野车体验</span>"
    "</div></div>"
)

_SCAN_PLACEHOLDER = _status_card(
    "info",
    "等待扫码",
    "请让游客将二维码对准摄像头，系统会自动识别并立即核销。",
)
_ACTION_PLACEHOLDER = (
    "### 🧾 订单服务\n"
    "购票后点击「申请退款」或「执行分账」，"
    "后端返回的手续费、实退金额和逐项明细会以 Markdown 展示在这里。"
)
_RESET_PLACEHOLDER = _status_card(
    "info",
    "演示环境就绪",
    "点击「🔄 重置演示环境」可清空历史数据，并生成一张全新的"
    "“已支付、门票/咖啡/越野车均未核销”测试订单。",
)


# 现场提示音：每次全屏反馈渲染时，按成功 / 失败 / 警告播放不同音调。
# 浏览器要求音频上下文先由用户手势解锁；现场点击摄像头即会完成解锁。
_VERIFY_FEEDBACK_JS = r"""
const getAudioContext = () => {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return null;
    if (!window.__tourismVerifyAudioCtx) {
        window.__tourismVerifyAudioCtx = new AudioCtx();
    }
    const ctx = window.__tourismVerifyAudioCtx;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    return ctx;
};
const unlockAudio = () => { getAudioContext(); };
window.addEventListener("pointerdown", unlockAudio, { once: true });

const playTone = (kind) => {
    const ctx = getAudioContext();
    if (!ctx) return;
    const now = ctx.currentTime;
    const beep = (freq, offset, duration, volume) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, now + offset);
        gain.gain.setValueAtTime(0.0001, now + offset);
        gain.gain.exponentialRampToValueAtTime(volume, now + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + duration);
        osc.connect(gain).connect(ctx.destination);
        osc.start(now + offset);
        osc.stop(now + offset + duration + 0.03);
    };
    if (kind === "success") {
        beep(880, 0, 0.14, 0.25);
        beep(1320, 0.16, 0.24, 0.22);
    } else if (kind === "error") {
        beep(240, 0, 0.30, 0.28);
        beep(170, 0.26, 0.36, 0.26);
    } else if (kind === "warning") {
        beep(620, 0, 0.16, 0.20);
        beep(620, 0.22, 0.16, 0.20);
    }
};

watch("value", () => {
    const node = element.querySelector("[data-tone]");
    if (!node) return;
    if (node.dataset.seq === window.__tourismVerifyToneSeq) return;
    window.__tourismVerifyToneSeq = node.dataset.seq;
    // 挂到 body 上，确保全屏遮罩不受 Gradio 容器裁剪 / transform 影响。
    document.querySelectorAll("body > .verify-overlay").forEach((old) => old.remove());
    document.body.appendChild(node);
    playTone(node.dataset.tone);
});
"""

# Gradio 6 的摄像头默认镜像，镜像后的二维码无法被 cv2 正确解码。
_IMAGE_KWARGS: dict[str, Any] = {}
if hasattr(gr, "WebcamOptions"):
    _IMAGE_KWARGS["webcam_options"] = gr.WebcamOptions(
        mirror=False,
        constraints={
            "video": {
                "width": {"ideal": 1280},
                "height": {"ideal": 720},
            }
        },
    )

with gr.Blocks(title="乡村文旅电子门票") as demo:
    gr.HTML(_HERO_HTML, padding=False, container=False)

    # 保存最近一次购票结果，供订单状态 / 退款 / 分账使用（按浏览器会话隔离）。
    last_order = gr.State(value=None)
    # 摄像头流式识别状态：记录镜头中已处理的二维码、岗位和停留时间，
    # 避免同一张二维码每一帧都重复调用核销接口。
    scan_state = gr.State(value={"code": "", "post": "", "seen_at": 0.0, "idle": False})

    # 订单状态每 6 秒自动刷新一次：商家核销后，游客无需手动操作即可看到变化。
    status_timer = gr.Timer(value=6, active=True)

    # -----------------------------------------------------------------------
    # 页面最显眼位置：一键重置演示环境（对所有 Tab 可见）
    # -----------------------------------------------------------------------
    with gr.Row(elem_classes="reset-demo-row"):
        with gr.Column(scale=5):
            gr.Markdown(
                "### 🔄 演示前准备\n"
                "一键清空历史订单 / 核销记录 / 分账流水，"
                "并自动生成一张包含门票、咖啡、越野车的全新测试订单。"
            )
        reset_button = gr.Button(
            "🔄 重置演示环境",
            variant="stop",
            size="lg",
            scale=1,
        )
    reset_result = gr.HTML(
        value=_RESET_PLACEHOLDER,
        padding=False,
        container=False,
    )

    with gr.Tabs():
        # -------------------------------------------------------------------
        # Tab 1：游客端（面向普通游客，不出现技术术语）
        # -------------------------------------------------------------------
        with gr.Tab("🎟️ 游客购票"):
            # 左右两列：左 = 购票区域，右 = 二维码展示区域。
            # elem_classes 里的 CSS 强制 nowrap，左侧再长也不会把二维码挤下去。
            with gr.Row(equal_height=False, elem_classes="tourist-layout"):
                # ---------------- 左列：购票区域 ----------------
                with gr.Column(
                    scale=5,
                    min_width=320,
                    elem_classes="tourist-panel buy-column",
                ):
                    gr.HTML(_PRODUCT_CARD_HTML, padding=False, container=False)

                    buy_button = gr.Button(
                        "🎫 立即购买 ￥200",
                        variant="primary",
                        size="lg",
                        elem_classes="buy-button",
                    )
                    gr.HTML(_BUY_ASSURANCE_HTML, padding=False, container=False)

                    order_status = gr.HTML(
                        value=_render_order_status(None),
                        padding=False,
                        container=False,
                    )
                    refresh_button = gr.Button(
                        "🔄 刷新订单状态",
                        variant="secondary",
                        size="sm",
                        elem_classes="refresh-status-button",
                    )
                    gr.HTML(_HOWTO_HTML, padding=False, container=False)

                # ---------------- 右列：二维码展示区域 ----------------
                with gr.Column(
                    scale=6,
                    min_width=380,
                    elem_classes="tourist-panel qr-column",
                ):
                    purchase_success = gr.HTML(
                        value="",
                        padding=False,
                        container=False,
                    )
                    # 默认隐藏：只有购票成功后才由 purchase() 传图并显示。
                    qr_image = gr.Image(
                        label="您的电子门票（请出示给景区检票员）",
                        type="pil",
                        interactive=False,
                        visible=False,
                        height=420,
                        buttons=["download"],
                        elem_classes="qr-image-box",
                    )
                    qr_note = gr.HTML(
                        value=_QR_PLACEHOLDER,
                        padding=False,
                        container=False,
                    )
                    # 二维码展示区域下方的醒目核销提示。
                    gr.HTML(_QR_SCAN_TIP_HTML, padding=False, container=False)

        # -------------------------------------------------------------------
        # Tab 2：商家端
        # -------------------------------------------------------------------
        with gr.Tab("📷 商家核销"):
            with gr.Column(elem_classes="scan-panel"):
                gr.Markdown(
                    "### 扫描游客门票\n"
                    "**先选择当前核销岗位**，再让游客把二维码对准摄像头。\n\n"
                    "识别到二维码的瞬间系统会自动核销，无需点击任何按钮。\n\n"
                    "· 景区检票员：只核销门票\n\n"
                    "· 咖啡店员：只核销精品咖啡（须先完成门票核销）\n\n"
                    "· 越野车安全员：只核销越野车体验（须先完成门票核销）\n\n"
                    "· 景区出口：登记离园；离园后按园区配置决定未核销项目是否还能退款\n\n"
                    "> 首次使用如未出现画面，请点击摄像头区域并允许浏览器使用摄像头。"
                )
                verify_post = gr.Dropdown(
                    choices=list(SCAN_POSTS.keys()),
                    value=DEFAULT_VERIFY_POST,
                    label="当前核销岗位",
                    info="系统只处理该岗位对应的项目，不会按顺序自动处理下一项",
                    interactive=True,
                    scale=1,
                    min_width=260,
                )
                camera = gr.Image(
                    sources=["webcam"],
                    type="numpy",
                    label="对准游客的二维码（自动识别，无需点击扫描）",
                    height=600,
                    interactive=True,
                    streaming=True,
                    **_IMAGE_KWARGS,
                )
                scan_status = gr.HTML(value=_SCAN_PLACEHOLDER)
                # 全屏现场反馈层：绿色对号 / 红色叉号 + 提示音。
                verify_feedback = gr.HTML(
                    value="",
                    container=False,
                    padding=False,
                    apply_default_css=False,
                    js_on_load=_VERIFY_FEEDBACK_JS,
                    elem_id="verify-feedback",
                )

                # ---------------- 备用演示区 ----------------
                # 现场演示时光线可能影响摄像头识别，这里提供不经过摄像头的
                # 手动核销入口，复用与扫码完全相同的后端核销逻辑。
                gr.HTML(
                    '<div class="backup-divider"></div>',
                    padding=False,
                    container=False,
                )
                gr.Markdown("#### 🔧 备用演示区")
                with gr.Row(elem_classes="backup-row"):
                    manual_code = gr.Textbox(
                        label="模拟扫码（备用）—— 请输入电子票号或订单ID",
                        placeholder="电子票号示例：TK-8；订单 ID 示例：8",
                        scale=4,
                        container=True,
                    )
                    manual_verify_button = gr.Button(
                        "模拟扫码核销",
                        variant="secondary",
                        size="lg",
                        scale=1,
                        min_width=160,
                    )
                gr.HTML(
                    '<div class="backup-hint">'
                    "注：备用区同样按【当前核销岗位】核销；"
                    "真实场景下由摄像头自动识别，无需手动输入。"
                    "</div>",
                    padding=False,
                    container=False,
                )

        # -------------------------------------------------------------------
        # Tab 3：运营后台（保留原有退款 / 分账演示能力）
        # -------------------------------------------------------------------
        with gr.Tab("⚙️ 运营后台（演示）"):
            with gr.Column(elem_classes="scan-panel"):
                gr.Markdown(
                    "### 🧾 订单服务\n"
                    "签名请求头（`X-Timestamp` / `X-Signature`）由前端自动生成；"
                    "请确保前后端 `API_SIGNING_SECRET` 一致。\n\n"
                    "分账仅结算“已核销”子项；只有生命周期结束后才能分账。"
                    "离园后是否允许退款由园区配置决定；"
                    "已分账订单不可再退款。"
                )
                order_id_input = gr.Number(
                    label="订单 ID（购票后自动填入，可手动修改）",
                    value=None,
                    precision=0,
                    interactive=True,
                )
                with gr.Row():
                    refund_button = gr.Button(
                        "💸 申请退款",
                        variant="secondary",
                        size="lg",
                    )
                    split_button = gr.Button(
                        "📊 执行分账",
                        variant="secondary",
                        size="lg",
                    )
                gr.Markdown(
                    "### 🏞️ 游园生命周期与退款规则\n"
                    "分账只在生命周期结束后执行；离园或闭园后，"
                    "未核销项目是否还能退款由下面的园区配置决定。"
                )
                with gr.Row():
                    refund_after_lifecycle = gr.Checkbox(
                        label="离园后仍允许退款",
                        value=False,
                        info="关闭时，离园/闭园后未核销项目立即不可退款",
                    )
                    refund_window_hours = gr.Number(
                        label="离园后退款窗口（小时）",
                        value=72,
                        precision=0,
                        minimum=0,
                        maximum=8760,
                        info="从离园/闭园时间起算，默认 72 小时",
                    )
                settings_button = gr.Button(
                    "💾 保存园区配置",
                    variant="secondary",
                    size="lg",
                )
                settings_result = gr.HTML(
                    value="",
                    padding=False,
                    container=False,
                )
                # 报错使用带内联样式的红色卡片，需关闭 HTML 净化。
                action_result = gr.Markdown(
                    value=_ACTION_PLACEHOLDER,
                    sanitize_html=False,
                )

    reset_button.click(
        reset_demo_environment,
        inputs=None,
        outputs=[
            reset_result,
            qr_image,
            purchase_success,
            qr_note,
            order_id_input,
            last_order,
            order_status,
            action_result,
        ],
        api_name="reset_demo",
    )
    buy_button.click(
        purchase,
        inputs=None,
        outputs=[
            qr_image,
            purchase_success,
            qr_note,
            order_id_input,
            last_order,
            order_status,
        ],
        api_name="purchase",
    )
    refresh_button.click(
        _render_order_status,
        inputs=[last_order],
        outputs=order_status,
        show_progress="hidden",
    )
    status_timer.tick(
        _render_order_status,
        inputs=[last_order],
        outputs=order_status,
        show_progress="hidden",
    )
    refund_button.click(
        request_refund,
        inputs=[last_order, order_id_input],
        outputs=action_result,
        api_name="request_refund",
    )
    split_button.click(
        request_split,
        inputs=[last_order, order_id_input],
        outputs=action_result,
        api_name="request_split",
    )
    settings_button.click(
        save_park_settings,
        inputs=[refund_after_lifecycle, refund_window_hours],
        outputs=settings_result,
        api_name="save_park_settings",
    )
    demo.load(
        load_park_settings,
        inputs=None,
        outputs=[refund_after_lifecycle, refund_window_hours, settings_result],
    )
    # 摄像头持续推流：服务端每 0.6 秒解析一帧，识别到二维码立即核销。
    # 同一会话的流事件串行执行，避免现场重复提交。
    camera.stream(
        scan_and_verify,
        inputs=[camera, verify_post, scan_state],
        outputs=[scan_status, verify_feedback, scan_state],
        stream_every=0.6,
        show_progress="hidden",
        concurrency_limit=1,
        trigger_mode="always_last",
        api_name="scan_and_verify",
    )
    manual_verify_button.click(
        simulate_verify,
        inputs=[manual_code, verify_post],
        outputs=[scan_status, verify_feedback],
        api_name="simulate_verify",
    )
    # 演示时也可以直接按回车触发，减少现场操作步骤。
    manual_code.submit(
        simulate_verify,
        inputs=[manual_code, verify_post],
        outputs=[scan_status, verify_feedback],
        show_progress="hidden",
    )


if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(
        server_name="127.0.0.1",
        server_port=APP_PORT,
        show_error=True,
        inbrowser=True,
        theme=gr.themes.Soft(primary_hue="emerald", secondary_hue="teal"),
        css=CSS,
    )
