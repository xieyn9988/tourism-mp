"""
防重核销接口测试脚本
====================

测试目标：
    对同一条核销记录连续发起两次核销请求，验证接口能拦截重复核销。

注意：
    接口 /api/verify-records/{record_id}/verify 里的 record_id 是
    verify_records 表的主键（整数），不是订单号 DEMO20260101000001。
    演示订单 DEMO20260101000001 下的记录：
        id=1 -> 门票（本脚本使用这条）
        id=2 -> 咖啡

运行方式：
    python test_api.py

运行前请先在另一个终端启动服务：
    python main.py

脚本每次运行前会把测试记录重置为“未核销”，因此可以反复运行。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
BASE_URL = "http://127.0.0.1:8000"
RECORD_ID = 1  # verify_records.id，演示订单下“门票”那条记录
VERIFY_URL = f"{BASE_URL}/api/verify-records/{RECORD_ID}/verify"

DB_PATH = Path(__file__).resolve().parent / "tourism.db"
TIMEOUT = 5  # 秒


def reset_record() -> bool:
    """把测试记录恢复为未核销，保证脚本可以反复运行。"""
    if not DB_PATH.exists():
        print(f"[警告] 未找到数据库文件：{DB_PATH}")
        return False

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            cursor = conn.execute(
                "UPDATE verify_records SET verify_status = 'unverified' WHERE id = ?",
                (RECORD_ID,),
            )
            conn.commit()
            if cursor.rowcount == 0:
                print(f"[错误] 数据库中不存在核销记录 id={RECORD_ID}，请先启动 main.py 初始化数据。")
                return False
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[错误] 重置测试记录失败：{exc}")
        return False

    print(f"[准备] 已将核销记录 id={RECORD_ID} 重置为 unverified（未核销）")
    return True


def call_verify(round_no: int) -> tuple[int | None, dict | None]:
    """发起一次核销请求，返回 (状态码, 响应 JSON)。"""
    print(f"\n---------- 第 {round_no} 次请求 ----------")
    print(f"POST {VERIFY_URL}")

    try:
        resp = requests.post(VERIFY_URL, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError:
        print("[错误] 无法连接后端服务，请先运行：python main.py")
        return None, None
    except requests.exceptions.Timeout:
        print(f"[错误] 请求超时（{TIMEOUT} 秒），请检查后端服务是否正常。")
        return None, None
    except requests.exceptions.RequestException as exc:
        print(f"[错误] 请求异常：{exc}")
        return None, None

    print(f"状态码：{resp.status_code}")
    try:
        body = resp.json()
        print("响应内容：")
        print(json.dumps(body, ensure_ascii=False, indent=2))
    except ValueError:
        body = None
        print(f"响应内容：{resp.text}")
    return resp.status_code, body


def main() -> int:
    print("=" * 60)
    print("防重核销测试")
    print(f"测试记录：verify_records.id = {RECORD_ID}（订单 { 'DEMO20260101000001' }）")
    print("=" * 60)

    # 1. 确保记录处于未核销状态
    if not reset_record():
        return 1

    # 2. 第一次核销：预期 200，状态变为 verified
    first_code, first_body = call_verify(1)

    # 3. 第二次核销：预期 409（重复核销被拦截）
    second_code, second_body = call_verify(2)

    # 4. 汇总判断
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    print(f"第一次请求状态码：{first_code}")
    print(f"第二次请求状态码：{second_code}")

    first_ok = first_code == 200
    second_blocked = second_code in (400, 409)  # 409 为标准的重复核销拦截

    if first_ok and second_blocked:
        detail = (second_body or {}).get("detail", "")
        print(f"[通过] 第一次核销成功，第二次被拦截（{second_code}：{detail}）")
        return 0

    if first_code is None or second_code is None:
        print("[失败] 请求未完成，请确认后端服务已启动。")
    elif not first_ok:
        print(f"[失败] 第一次核销未成功，状态码 {first_code}。")
    else:
        print(f"[失败] 第二次请求未被拦截，状态码 {second_code}，防重核销可能失效。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
