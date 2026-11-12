"""
乡村文旅多商户运营中台 —— Gradio 运营面板
================================================

功能：
    1. 游客退款：输入订单 ID，展示逐项退款明细（已用退款 0 / 未用扣 10%）；
    2. 人工核销通道：输入游客提供的取票码（真实订单 ID），
       按门票 → 咖啡 → 越野车的顺序核销下一个未使用项目；
    3. 核销记录：读取核销记录，展示 ID、订单号、核销状态。

依赖安装：
    pip install "gradio>=6" requests

启动方式（需要先启动 FastAPI 后端）：
    终端 1：python main.py
    终端 2：python app.py

后端地址默认是 http://127.0.0.1:8000，可通过环境变量覆盖：
    API_BASE_URL=http://127.0.0.1:8000 python app.py

退款接口需要 HMAC-SHA256 签名，本文件会自动生成
X-Timestamp / X-Signature；前后端 API_SIGNING_SECRET 必须一致。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import time
from typing import Any

import gradio as gr
import requests

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
REQUEST_TIMEOUT = 5  # 秒

# 必须与 main.py 的 API_SIGNING_SECRET 保持一致，否则退款接口返回 403。
DEFAULT_SIGNING_SECRET = "tourism-mp-demo-secret"
SIGNING_SECRET = os.getenv("API_SIGNING_SECRET", DEFAULT_SIGNING_SECRET)

STATUS_LABELS = {
    "unverified": "未核销",
    "verified": "已核销",
    "refunded": "已退款",
    "expired": "已过期",
}

CSS = """
.app-shell {
    max-width: 980px;
    margin: 0 auto;
}
.poc-title h1 {
    margin-bottom: 0.25rem;
    letter-spacing: 0.02em;
}
.poc-title p {
    margin-top: 0;
    color: #64748b;
}
.poc-card {
    border: 1px solid #e2e8f0;
    border-radius: 16px;
    padding: 4px 18px 18px;
    background: #ffffff;
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.05);
}
.poc-card h2 {
    font-size: 1.15rem;
    margin-bottom: 0.75rem;
}
.poc-alert {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    margin-top: 12px;
    padding: 13px 16px;
    border: 1px solid transparent;
    border-left-width: 5px;
    border-radius: 12px;
    animation: poc-pop 0.22s ease-out;
    line-height: 1.5;
}
.poc-alert strong {
    display: block;
    margin-bottom: 2px;
}
.poc-alert-success {
    color: #166534;
    background: #f0fdf4;
    border-color: #86efac;
    border-left-color: #16a34a;
}
.poc-alert-error {
    color: #991b1b;
    background: #fef2f2;
    border-color: #fca5a5;
    border-left-color: #dc2626;
}
.poc-alert-ready {
    color: #475569;
    background: #f8fafc;
    border-color: #e2e8f0;
    border-left-color: #94a3b8;
}
.poc-alert-placeholder {
    min-height: 24px;
}
#refund-result table {
    font-size: 1rem;
}
#refund-result th {
    background: #f8fafc;
}
@keyframes poc-pop {
    from { opacity: 0; transform: translateY(-4px); }
    to   { opacity: 1; transform: translateY(0); }
}
#monitor-table table {
    font-size: 0.95rem;
}
"""


class BackendError(RuntimeError):
    """后端不可达、响应异常等可展示给用户的错误。"""


def _error_detail(response: requests.Response) -> str:
    """从 FastAPI 的 {"detail": ...} 响应中提取可读错误信息。"""
    try:
        body: Any = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, list):
            messages = [
                str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                for item in detail
            ]
            if messages:
                return "；".join(messages)

    text = response.text.strip()
    return text if text else f"HTTP {response.status_code}"


def _signature_headers(method: str, path: str) -> dict[str, str]:
    """
    生成 main.py require_signature 所需的 HMAC 请求头。

    签名原文（与后端逐字符一致）：
        {timestamp}\\n{METHOD}\\n{path}\\n{sha256_hex(body)}
    本面板的资金接口均无请求体，因此 body 的 SHA-256 为空字节串摘要。
    """
    timestamp = str(int(time.time()))
    body_digest = hashlib.sha256(b"").hexdigest()
    canonical = f"{timestamp}\n{method.upper()}\n{path}\n{body_digest}"
    signature = hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {"X-Timestamp": timestamp, "X-Signature": signature}


def _request(method: str, path: str, *, signed: bool = False) -> requests.Response:
    """统一处理网络层异常，让上层只关心状态码和业务响应。"""
    url = f"{API_BASE_URL}{path}"
    headers = _signature_headers(method, path) if signed else None
    try:
        if method == "POST":
            return requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
        return requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.ConnectionError as exc:
        raise BackendError(
            f"无法连接后端服务（{API_BASE_URL}），请先运行 python main.py。"
        ) from exc
    except requests.Timeout as exc:
        raise BackendError(
            f"请求后端超时（{REQUEST_TIMEOUT} 秒），请检查服务是否正常。"
        ) from exc
    except requests.RequestException as exc:
        raise BackendError(f"请求后端失败：{exc}") from exc


def _json_list(response: requests.Response, endpoint: str) -> list[Any]:
    """校验并返回列表型 JSON，避免异常响应把页面搞崩。"""
    if response.status_code != 200:
        raise BackendError(
            f"读取 {endpoint} 失败（HTTP {response.status_code}）："
            f"{_error_detail(response)}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise BackendError(f"{endpoint} 返回的不是合法 JSON。") from exc
    if not isinstance(data, list):
        raise BackendError(f"{endpoint} 返回的数据格式不是列表。")
    return data


def fetch_verify_rows() -> list[list[Any]]:
    """
    读取 /api/verify-records，并通过 /api/orders 补充订单号。

    核销记录接口只返回 order_id，因此额外读取一次订单列表做映射。
    即使订单接口暂时不可用，也仍然展示核销记录（订单号降级为 #order_id）。
    """
    records = _json_list(_request("GET", "/api/verify-records"), "/api/verify-records")

    order_no_by_id: dict[int, str] = {}
    try:
        orders = _json_list(_request("GET", "/api/orders"), "/api/orders")
        for order in orders:
            if isinstance(order, dict) and "id" in order and "order_no" in order:
                order_no_by_id[int(order["id"])] = str(order["order_no"])
    except (BackendError, TypeError, ValueError):
        # 订单号只是展示增强项，不应阻断核销记录表格。
        pass

    rows: list[list[Any]] = []
    valid_records = [item for item in records if isinstance(item, dict)]
    for record in sorted(valid_records, key=lambda item: int(item.get("id") or 0)):
        order_id = record.get("order_id")
        raw_status = str(record.get("verify_status", ""))
        rows.append(
            [
                record.get("id"),
                order_no_by_id.get(order_id, f"#{order_id}"),
                STATUS_LABELS.get(raw_status, raw_status or "未知"),
            ]
        )
    return rows


def _alert(kind: str, title: str, message: str) -> str:
    """生成带颜色的内联提示。kind: success / error / ready。"""
    # 这些字符只写入浏览器 HTML，不会 print 到 Windows GBK 控制台。
    icons = {"success": "✅", "error": "❌", "ready": "i"}
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    return (
        f'<div class="poc-alert poc-alert-{kind}" role="status">'
        f'<span aria-hidden="true"><strong>{icons[kind]}</strong></span>'
        f"<div><strong>{safe_title}</strong><span>{safe_message}</span></div>"
        f"</div>"
    )


def _placeholder_alert() -> str:
    return (
        '<div class="poc-alert poc-alert-ready poc-alert-placeholder" role="status">'
        "<div><span>输入游客提供的取票码，点击按钮完成人工核销。</span></div>"
        "</div>"
    )


# ===========================================================================
# 游客端退款：调用后端并渲染逐项明细
# ===========================================================================

def _md_cell(value: Any) -> str:
    """转义 Markdown 表格单元格，避免竖线导致表格断裂。"""
    text = "" if value is None else str(value)
    return html.escape(text).replace("|", "\\|").replace("\n", " ").strip()


def _money_text(value: Any) -> str:
    if value is None or value == "":
        return "￥--"
    return f"￥{_md_cell(value)}"


def _render_refund_markdown(
    data: dict[str, Any],
    *,
    rejected: bool = False,
) -> str:
    """
    把退款接口 JSON 渲染成 Markdown 表格。

    第一张表严格按“项目 / 状态 / 退款金额”展示，已核销项目退款固定为 0；
    第二张表补充原价、10% 手续费与逐项原因，方便现场讲解。
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
        f"**订单 ID**：`{_md_cell(data.get('order_id'))}`",
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
        f"| 手续费合计（10%） | {_money_text(data.get('fee_amount'))} |",
        f"| **实退金额** | **{_money_text(data.get('refund_amount'))}** |",
    ]

    if items:
        lines += [
            "",
            "### 📝 逐项说明",
            "",
            "| 项目 | 原价 | 手续费 | 实退 | 处理说明 |",
            "| :--- | ---: | ---: | ---: | :--- |",
        ]
        for item in items:
            lines.append(
                f"| {_md_cell(item.get('item_name'))} "
                f"| {_money_text(item.get('gross_amount'))} "
                f"| {_money_text(item.get('fee_amount'))} "
                f"| {_money_text(item.get('refund_amount'))} "
                f"| {_md_cell(item.get('reason'))} |"
            )

    return "\n".join(lines)


def request_refund(order_id: float | int | None) -> str:
    """游客端按钮事件：调用退款接口，返回可直接展示的 Markdown 表格。"""
    if order_id is None:
        return _alert("error", "参数错误", "请输入要退款的订单 ID。")

    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return _alert("error", "参数错误", "订单 ID 必须是正整数。")
    if oid <= 0:
        return _alert("error", "参数错误", "订单 ID 必须是正整数。")

    path = f"/api/order/{oid}/refund"
    try:
        response = _request("POST", path, signed=True)
    except BackendError as exc:
        return _alert("error", "退款请求失败", str(exc))

    try:
        payload: Any = response.json()
    except ValueError:
        payload = None

    if response.status_code == 200 and isinstance(payload, dict):
        return _render_refund_markdown(payload)

    detail = payload.get("detail") if isinstance(payload, dict) else None

    if response.status_code == 409 and isinstance(detail, dict):
        # 全部项目均已核销时，后端把完整逐项结论放在 detail 中。
        return _render_refund_markdown(detail, rejected=True)

    if response.status_code == 403:
        return _alert(
            "error",
            "签名验证失败（403）",
            "请确认 app.py 与 main.py 使用相同的 API_SIGNING_SECRET，"
            "并检查本机时间是否准确（默认允许 ±300 秒）。",
        )

    detail_text = detail if isinstance(detail, str) else _error_detail(response)
    return _alert(
        "error",
        f"退款失败（HTTP {response.status_code}）",
        detail_text,
    )


def load_dashboard() -> tuple[str, list[list[Any]]]:
    """页面加载时读取一次后台数据。"""
    try:
        rows = fetch_verify_rows()
    except BackendError as exc:
        return _alert("error", "数据加载失败", str(exc)), []
    return _alert("ready", "已连接后端", f"当前共 {len(rows)} 条核销记录。"), rows


def refresh_table() -> tuple[str, list[list[Any]]]:
    """手动刷新表格。"""
    return load_dashboard()


def verify_by_pickup_code(
    pickup_code: float | int | str | None,
) -> tuple[str, list[list[Any]]]:
    """
    人工核销通道：取票码（真实订单 ID）→ 核销下一个未使用项目。

    检票员扫码失败时，只需输入游客购票成功后看到的取票码：
      1. 读取订单及其全部核销子项；
      2. 按门票 → 咖啡 → 越野车的顺序选择下一个 unverified 子项；
      3. 调用核销接口；后端仍会执行闭园、生命周期、退款 / 过期校验。

    无论成功与否都会刷新核销记录表格。
    """
    if pickup_code in (None, ""):
        return _alert("error", "参数错误", "请输入游客提供的取票码。"), _safe_rows()

    text = str(pickup_code).strip()
    if not re.fullmatch(r"\d{1,9}", text):
        return (
            _alert("error", "参数错误", "取票码格式不正确，请输入订单编号。"),
            _safe_rows(),
        )
    order_id = int(text)
    if order_id <= 0:
        return (
            _alert("error", "参数错误", "取票码必须是正整数。"),
            _safe_rows(),
        )

    try:
        order_response = _request("GET", f"/api/orders/{order_id}")
    except BackendError as exc:
        return _alert("error", "核销失败", str(exc)), _safe_rows()

    if order_response.status_code == 404:
        return (
            _alert("error", "未找到该取票码", "请核对游客提供的取票码。"),
            _safe_rows(),
        )
    if order_response.status_code != 200:
        return (
            _alert(
                "error",
                f"读取订单失败（HTTP {order_response.status_code}）",
                _error_detail(order_response),
            ),
            _safe_rows(),
        )

    try:
        order = order_response.json()
    except ValueError:
        order = None
    if not isinstance(order, dict):
        return _alert("error", "订单数据异常", "后端返回内容无法解析。"), _safe_rows()

    records = [
        item
        for item in (order.get("verify_records") or [])
        if isinstance(item, dict)
    ]
    pending = [
        item
        for item in records
        if str(item.get("verify_status") or "") == "unverified"
    ]
    if not pending:
        statuses = {str(item.get("verify_status") or "") for item in records}
        if "expired" in statuses:
            message = "本次游园已结束，未使用项目已过期；如需游玩请重新购票。"
        elif "verified" in statuses:
            message = "该订单所有项目均已核销，无需重复操作。"
        else:
            message = "该订单暂无可核销项目。"
        return _alert("ready", "无需核销", message), _safe_rows()

    # 按业务顺序选择：门票最先，其次咖啡，最后越野车。
    target = None
    for keyword in ("门票", "咖啡", "越野车"):
        target = next(
            (
                item
                for item in pending
                if keyword in str(item.get("item_name") or "")
            ),
            None,
        )
        if target is not None:
            break
    if target is None:
        target = pending[0]

    try:
        record_id = int(target.get("id"))
    except (TypeError, ValueError):
        return (
            _alert("error", "订单数据异常", "核销子项缺少有效编号。"),
            _safe_rows(),
        )

    try:
        response = _request("POST", f"/api/verify-records/{record_id}/verify")
    except BackendError as exc:
        return _alert("error", "核销失败", str(exc)), _safe_rows()

    if response.status_code == 200:
        try:
            record = response.json()
        except ValueError:
            record = {}
        item_name = (
            record.get("item_name") if isinstance(record, dict) else None
        ) or target.get("item_name")
        message = (
            f"取票码 {order_id} 的「{item_name}」已核销成功。"
        )
        gr.Success(message, title="核销成功")
        rows, note = _refresh_after_action()
        if note:
            message += f"（表格刷新失败：{note}）"
        return _alert("success", "核销成功", message), rows

    detail = _error_detail(response)
    if response.status_code == 409:
        detail = f"当前订单不可核销：{detail}"
    elif response.status_code == 404:
        detail = f"核销记录不存在：{detail}"
    elif response.status_code == 400:
        detail = f"请求无效：{detail}"

    rows, note = _refresh_after_action()
    if note:
        detail += f"（表格刷新失败：{note}）"
    return _alert("error", f"核销失败（HTTP {response.status_code}）", detail), rows


def _safe_rows() -> list[list[Any]]:
    """操作失败时也尽量刷新表格；刷新失败则返回空表。"""
    rows, _ = _refresh_after_action()
    return rows


def _refresh_after_action() -> tuple[list[list[Any]], str | None]:
    try:
        return fetch_verify_rows(), None
    except BackendError as exc:
        return [], str(exc)


with gr.Blocks(title="乡村文旅多商户运营中台") as demo:
    with gr.Column(elem_classes="app-shell"):
        gr.Markdown(
            "# 乡村文旅多商户运营中台\n"
            "订单履约 · 人工核销 · 分账对账",
            elem_classes="poc-title",
        )

        # -------------------------------------------------------------------
        # 游客端：申请退款 + 逐项明细
        # -------------------------------------------------------------------
        with gr.Group(elem_classes="poc-card"):
            gr.Markdown(
                "## 游客端 · 申请退款\n"
                "输入订单 ID 后点击退款。已核销项目退款 0 元；"
                "未核销项目按原价扣除 **10% 手续费** 后退款。"
            )
            with gr.Row():
                refund_order_id = gr.Number(
                    value=None,
                    precision=0,
                    minimum=1,
                    step=1,
                    label="订单 ID",
                    scale=1,
                )
                refund_button = gr.Button(
                    "💸 申请退款",
                    variant="primary",
                    scale=1,
                )
            refund_result = gr.Markdown(
                value=(
                    "退款结果会以表格展示在这里："
                    "`项目 / 状态（已用或未用） / 退款金额`。"
                ),
                sanitize_html=False,
                elem_id="refund-result",
            )

        # -------------------------------------------------------------------
        # 商家核销端：人工核销通道
        # -------------------------------------------------------------------
        with gr.Group(elem_classes="poc-card"):
            gr.Markdown(
                "## 商家核销端 · 人工核销通道\n"
                "扫码失败时，请输入游客提供的取票码，点击「确认核销」完成补录。"
            )
            with gr.Row():
                pickup_code = gr.Textbox(
                    value="",
                    label="取票码",
                    placeholder="请输入游客提供的取票码",
                    scale=1,
                )
                verify_button = gr.Button(
                    "确认核销",
                    variant="primary",
                    scale=1,
                )
            result = gr.HTML(value=_placeholder_alert())

        with gr.Group(elem_classes="poc-card"):
            with gr.Row():
                gr.Markdown("## 核销记录")
                refresh_button = gr.Button("刷新数据", size="sm", scale=0)
            table = gr.Dataframe(
                headers=["ID", "订单号", "状态"],
                datatype=["number", "str", "str"],
                value=[],
                interactive=False,
                wrap=True,
                elem_id="monitor-table",
            )

    demo.load(load_dashboard, inputs=None, outputs=[result, table])
    refund_button.click(
        request_refund,
        inputs=refund_order_id,
        outputs=refund_result,
    )
    verify_button.click(
        verify_by_pickup_code,
        inputs=pickup_code,
        outputs=[result, table],
    )
    pickup_code.submit(
        verify_by_pickup_code,
        inputs=pickup_code,
        outputs=[result, table],
    )
    refresh_button.click(
        refresh_table,
        inputs=None,
        outputs=[result, table],
    )


if __name__ == "__main__":
    demo.queue(default_concurrency_limit=8).launch(
        server_name="127.0.0.1",
        server_port=7860,
        show_error=True,
        inbrowser=True,
        css=CSS,
    )
