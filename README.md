# 03-syncledger（双核对账）

Syncledger — 跨库/跨系统对齐对账：双端抽取 → 对齐键对账 → 差异处置 → 批次互证。

与 Feedstitch（多源 Feed 归并）、Batchgate（作业依赖编排）主链路不同。夹具全本地，不连活库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5300 |
| API | http://localhost:10300 |

## 主链

配置对账对与对齐键 → 触发双端抽取并对齐 → 落差异行与批次计数 → 页面互证

## 可配时间窗与单边缓冲（已实现）

对账对可配半开时间窗 `[start, end)`：行上事件/更新时间**落入窗内**才参与本趟，
窗外行只计 `outside_window`、不参加比对。窗内一侧先到、对侧未到 → 行进入可审计
**缓冲**，不立即落 `left_only` / `right_only` 终态，也不计入差异合计；对侧在窗内补到
则对齐（一致记 matched，不一致转字段比对）。

缓冲截止 = 窗口上沿 + `max_lateness_seconds`。时钟越过截止、或窗口上沿推进把缓冲行
的事件时间甩出窗口，该行必须按同一逾窗规则清算：**落仅左/仅右终态**或**显式丢弃并计数**
（`liquidation` 每对固定），绝不允许留在缓冲里同时从本趟统计中消失。批次报告、缓冲账本
与审计摘要三处计数一致。

页面：`/window`「窗口缓冲」可配置窗口/迟到/时间字段/逾窗规则，查看缓冲账本与缓冲事件
审计，并一键演示四种走势：

| 演示 | 走势 |
| --- | --- |
| 窗内先缓冲，后对齐 | 第 1 趟单边进缓冲（差异=0）→ 第 2 趟对侧补到，对齐 2 条（1 条转字段比对） |
| 逾窗仍单边 → 落终态 | 单边进缓冲 → 时钟过截止落 `left_only` / `right_only` 并计入差异 |
| 推进窗口上沿 → 缓冲清算 | 长迟到缓冲 → 窗口滑过事件时间，按逾窗规则清算，缓冲归零 |
| 逾窗显式丢弃并计数 | 同一逾窗规则切到 `discard`：`discarded=2`、不进差异合计 |

相关 API：`GET /api/windows`、`GET|PUT /api/pairs/{id}/window`、`GET /api/buffer`、
`GET /api/buffer/events`、`GET /api/demo/scenarios`、`POST /api/demo/scenarios/{key}/run`。
`POST /api/reconcile` 可传 `window_start` / `window_end` / `max_lateness_seconds` /
`left_time_field` / `right_time_field` / `liquidation` / `as_of`，`use_window=false` 回退到
全量快照口径。

测试：`cd backend && pytest`（引擎语义 + 持久化/API 集成，共 26 条）。

## 种子对账对

- **inventory_hq_vs_store**：总部库存 ↔ 门店库存（`sku_id`+`store_id`，比对 `qty`）
- **payment_vs_ledger**：支付网关 ↔ 账务流水（`txn_id`，比对 `amount`/`currency`）
- **sku_catalog_a_vs_b**：目录 A ↔ 目录 B（`sku_code`，比对 `name`/`category`）

底座默认只**记差异、不自动盖写**；差异处置/契约/审计页留表与入口，深逻辑留给后题。侧栏不叫「工单」，避免撞禁名单「工单客服」。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 0-1 切口（未实现，见出题规划）

`master_slave_pick` / `side_only_policy` / `diff_disposition_flow` / `batch_idempotency` / …
