# 商户营业时间与动态闭园校验（短设计）

日期：2026-11-11
状态：待确认

## 目的

把写死的景区闭园时间（`PARK_CLOSE_TIME` 环境变量，默认 18:00）改成商户级数据库配置。
核销时按订单门票所属商户的 `close_time` 判断，超过配置时间才抛出「闭园拦截」。
今晚联调默认闭园时间为 `23:59:59`，避免演示被提前拦截。

## 目标 / 非目标

- 目标：`merchants` 表新增 `open_time` / `close_time`；老库自动迁移；核销与生命周期按商户配置判断；提供修改接口。
- 非目标：不做 `open_time` 的开园拦截（仅存储/展示）；不改 Gradio 运营后台 UI；不做多时区/节假日营业时间。

## 关键决策

1. **闭园归属**：一笔订单以「门票」所属商户的 `close_time` 为整单闭园时间；找不到门票商户时回退默认值。所有核销岗位共用同一时间，保证生命周期 `leave_at` 口径一致。
2. **默认值**：`open_time = '08:00'`，`close_time = '23:59:59'`。字段用 `String(8)`，兼容 `HH:MM` 与 `HH:MM:SS`。
3. **时区**：配置值是北京时间（UTC+8），存储仍是 naive UTC；比较前统一换算。
4. **拦截时机**：核销接口在读取订单后、写核销状态前显式检查；`now >= 闭园时刻` 即拦截（与现有生命周期边界一致），返回 409，detail 含「闭园」。

## 改动点

- `main.py`
  - `Merchant` 增加 `open_time` / `close_time`，去掉 `PARK_CLOSE_TIME` 全局常量与环境变量。
  - 新增时间解析/归一化工具；`evaluate_lifecycle()` 增加 `close_time` 参数，默认 `23:59:59`。
  - 新增 `_resolve_order_close_time(db, order)`：取订单门票记录对应商户，读 `close_time`。
  - `POST /api/verify-records/{record_id}/verify` 增加显式闭园检查。
  - `_order_lifecycle` 的 5 个调用点传入解析后的闭园时间。
  - `MerchantOut` 增加两个字段；新增 `PATCH /api/merchants/{id}/business-hours`（校验并保存营业时间，需 HMAC 签名）。
  - 新增 SQLite 迁移 `_migrate_merchant_business_hours`，挂到 `initialize_database()`；新商户走模型默认值。
  - 更新模块头部说明。
- `test_lifecycle.py` / `test_verify_scope.py`
  - 原有 18:00 相关用例改为显式配置商户 `close_time='18:00'`。
  - 新增：动态闭园拦截、默认 23:59:59 放行、配置接口、迁移幂等。
- 文档：更新生命周期设计里「闭园时间由环境变量控制」的描述。

## 验收

- 默认商户在 23:59:59 前可正常核销。
- 把门票商户 `close_time` 改为已过去的时间后，核销返回 409 且提示「闭园」。
- 老 `tourism.db` 启动后自动出现两列，历史商户值为 `08:00` / `23:59:59`。
- `pytest -q` 全绿。
