"""
乡村文旅多商户运营中台 POC —— Gradio 演示面板
================================================

功能：
    1. 一键重置演示环境：清空历史订单 / 核销记录，并生成一张全新测试订单；
    2. 游客端退款：输入订单 ID，展示逐项退款明细（已用退款 0 / 未用扣 10%）；
    3. 核销控制台：输入 verify_records.id，调用 FastAPI 核销接口；
    4. 后台数据监控：读取核销记录，展示 ID、订单号、核销状态。

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
import time
from typing import Any

import gradio as gr
import requests

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
REQUEST_TIMEOUT = 5  # 秒

# 必须与 main.py 的 API_SIGNING_SECRET 保持一致，否则退款 / 重置接口返回 403。
DEFAULT_SIGNING_SECRET = "tourism-mp-demo-secret"
SIGNING_SECRET = os.getenv("API_SIGNING_SECRET", DEFAULT_SIGNING_SECRET)

STATUS_LABELS = {
    "unverified": "未核销",
    "verified": "已核销",
    "refunded": "已退款",
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
.poc-reset-card {
    border-color: #fde68a;
    background: #fffbeb;
}
.poc-reset-card h2 {
    color: #92400e;
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
        "<div><span>输入核销记录 ID，点击按钮完成核销。</span></div>"
        "</div>"
    )


def _reset_placeholder_alert() -> str:
    return (
        '<div class="poc-alert poc-alert-ready poc-alert-placeholder" role="status">'
        "<div><span>点击「🔄 重置演示环境」，清空历史数据并生成一张全新的"
        "“已支付、三项未核销”测试订单。</span></div>"
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


# ===========================================================================
# 一键重置演示环境
# ===========================================================================

def reset_demo() -> tuple[str, Any, Any, str, str, list[list[Any]]]:
    """
    调用 POST /api/demo/reset：
      - 清空历史订单 / 核销记录 / 分账流水；
      - 生成一张 paid + 三项 unverified 的新订单；
      - 自动把新订单 ID 填进退款输入框。
    """
    try:
        response = _request("POST", "/api/demo/reset", signed=True)
    except BackendError as exc:
        return (
            _alert("error", "重置失败", str(exc)),
            gr.update(),
            gr.update(),
            _alert("error", "数据刷新失败", str(exc)),
            "",
            _safe_rows(),
        )

    if response.status_code != 200:
        detail = _error_detail(response)
        if response.status_code == 403:
            detail = (
                "签名验证失败：请确认前后端 API_SIGNING_SECRET 一致，"
                "并检查本机时间是否准确。"
            )
        return (
            _alert("error", f"重置失败（HTTP {response.status_code}）", detail),
            gr.update(),
            gr.update(),
            _alert("error", "数据刷新失败", detail),
            "",
            _safe_rows(),
        )

    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict) or "order_id" not in data:
        return (
            _alert("error", "重置失败", "后端返回的数据格式不正确。"),
            gr.update(),
            gr.update(),
            _alert("error", "数据刷新失败", "后端返回的数据格式不正确。"),
            "",
            _safe_rows(),
        )

    order_id = data.get("order_id")
    order_no = data.get("order_no")
    ticket_record_id = data.get("ticket_record_id")
    # 注意：Gradio 的成功提示会 print 到控制台，Windows GBK 终端无法编码
    # emoji，因此 toast 使用纯文本；绿色 ✅ 卡片由 HTML 内联提示展示。
    message = f"演示环境已重置，请使用新订单 ID：{order_id} 开始测试！"

    gr.Success(message, title="演示环境已重置")

    rows, note = _refresh_after_action()
    table_note = f"当前共 {len(rows)} 条核销记录。"
    if note:
        table_note += f"（表格刷新失败：{note}）"

    return (
        _alert(
            "success",
            "演示环境已重置",
            f"新订单 ID：{order_id}；订单号：{order_no}；"
            "门票、咖啡、越野车三项均为未核销。",
        ),
        gr.update(value=ticket_record_id),
        gr.update(value=order_id),
        (
            f"### ✅ 新订单已就绪\n"
            f"已自动切换到订单 `#{order_id}`（{order_no}）。\n\n"
            "可以直接点击下方「💸 申请退款」查看退款明细，"
            "或先核销部分项目后再退款，验证“部分退款”逻辑。"
        ),
        _alert("ready", "后台数据已刷新", table_note),
        rows,
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


def verify_record(record_id: float | int | None) -> tuple[str, list[list[Any]]]:
    """
    调用 POST /api/verify-records/{record_id}/verify。

    无论成功与否，都会尝试刷新右侧监控表格，方便直接观察状态变化。
    """
    if record_id is None:
        return _alert("error", "参数错误", "请输入核销记录 ID。"), _safe_rows()

    try:
        rid = int(record_id)
    except (TypeError, ValueError):
        return (
            _alert("error", "参数错误", "核销记录 ID 必须是正整数。"),
            _safe_rows(),
        )

    if rid <= 0:
        return (
            _alert("error", "参数错误", "核销记录 ID 必须是正整数。"),
            _safe_rows(),
        )

    response = None
    try:
        response = _request("POST", f"/api/verify-records/{rid}/verify")
    except BackendError as exc:
        return _alert("error", "核销失败", str(exc)), _safe_rows()

    if response.status_code == 200:
        try:
            record = response.json()
        except ValueError:
            record = {}
        item_name = record.get("item_name") if isinstance(record, dict) else None
        item_text = f"「{item_name}」" if item_name else ""
        message = f"记录 #{rid}{item_text} 已核销成功。"
        gr.Success(message, title="核销成功")
        rows, note = _refresh_after_action()
        if note:
            message += f"（表格刷新失败：{note}）"
        return _alert("success", "核销成功", message), rows

    detail = _error_detail(response)
    if response.status_code == 409:
        detail = f"请勿重复核销：{detail}"
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


with gr.Blocks(title="乡村文旅多商户运营中台 POC") as demo:
    with gr.Column(elem_classes="app-shell"):
        gr.Markdown(
            "# 乡村文旅多商户运营中台 POC\n"
            "FastAPI 核销接口联调面板 · 多商户订单履约演示",
            elem_classes="poc-title",
        )

        # -------------------------------------------------------------------
        # 最显眼位置：一键重置演示环境
        # -------------------------------------------------------------------
        with gr.Group(elem_classes="poc-card poc-reset-card"):
            with gr.Row():
                with gr.Column(scale=4):
                    gr.Markdown(
                        "## 🔄 演示环境\n"
                        "一键清空历史订单 / 核销记录 / 分账流水，并自动生成一张"
                        "**已支付、门票+咖啡+越野车均未核销**的新订单。"
                    )
                reset_button = gr.Button(
                    "🔄 重置演示环境",
                    variant="stop",
                    size="lg",
                    scale=1,
                )
            reset_alert = gr.HTML(value=_reset_placeholder_alert())

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
                    value=1,
                    precision=0,
                    minimum=1,
                    step=1,
                    label="订单 ID（重置后自动填入）",
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
        # 核销控制台
        # -------------------------------------------------------------------
        with gr.Group(elem_classes="poc-card"):
            gr.Markdown("## 核销控制台")
            with gr.Row():
                record_id = gr.Number(
                    value=1,
                    precision=0,
                    minimum=1,
                    step=1,
                    label="核销记录 ID（record_id）",
                    scale=1,
                )
                verify_button = gr.Button(
                    "点击扫码核销",
                    variant="primary",
                    scale=1,
                )
            result = gr.HTML(value=_placeholder_alert())

        with gr.Group(elem_classes="poc-card"):
            with gr.Row():
                gr.Markdown("## 后台数据监控")
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
    reset_button.click(
        reset_demo,
        inputs=None,
        outputs=[
            reset_alert,
            record_id,
            refund_order_id,
            refund_result,
            result,
            table,
        ],
    )
    refund_button.click(
        request_refund,
        inputs=refund_order_id,
        outputs=refund_result,
    )
    verify_button.click(
        verify_record,
        inputs=record_id,
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
