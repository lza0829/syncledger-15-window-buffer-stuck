"""Scripted, repeatable demos of the window buffer lifecycle.

Each scenario drives the real windowed engine and persistence layer over the
seeded inventory pair.  A pass is staged with an explicit *arrival cut* — the
fixture rows whose timestamps are at or before that pass's observation clock —
so "one side arrives first, the counterpart later" is genuinely reproduced
instead of simulated by deleting rows.  Fixture timestamps are fixed, so every
demo is deterministic.

Timeline used by the inventory fixtures (``updated_at``):

=========  =========================================
08:00      left  SKU-100/S01, SKU-100/S02
08:01      right SKU-100/S01, SKU-100/S02
08:05      left  SKU-200/S01
08:06      right SKU-200/S01
08:10      left  SKU-300/S01     (right never arrives)
08:12      right SKU-400/S01     (left never arrives)
=========  =========================================
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.window import LIQUIDATION_DISCARD, LIQUIDATION_FINALIZE, parse_ts
from app.repositories import pairs as pair_repo
from app.services import reconcile_service as svc

DEMO_PAIR_CODE = "inventory_hq_vs_store"


@dataclass(frozen=True)
class Step:
    label: str
    note: str
    window_start: str
    window_end: str
    max_lateness_seconds: int
    as_of: str
    liquidation: str = LIQUIDATION_FINALIZE


SCENARIOS: dict[str, dict[str, Any]] = {
    "buffer_then_align": {
        "title": "窗内先缓冲，后对齐",
        "summary": (
            "一侧先到、对侧未到先进缓冲（不计入终态差异）；对侧在窗内补到后对齐，"
            "一致记 matched，不一致转字段比对。"
        ),
        "steps": [
            Step(
                label="第 1 趟：左端先到，右端未到",
                note="时钟 08:00:30。左端 SKU-100×2 已到，右端（08:01）尚未到达 → 进缓冲。",
                window_start="2026-11-12T08:00:00Z",
                window_end="2026-11-12T08:15:00Z",
                max_lateness_seconds=900,
                as_of="2026-11-12T08:00:30Z",
            ),
            Step(
                label="第 2 趟：右端窗内补到 → 对齐",
                note="时钟 08:07。右端补到：SKU-100/S02 对齐且一致；SKU-100/S01 对齐转字段比对（qty 120→118）。",
                window_start="2026-11-12T08:00:00Z",
                window_end="2026-11-12T08:15:00Z",
                max_lateness_seconds=900,
                as_of="2026-11-12T08:07:00Z",
            ),
        ],
    },
    "expire_side_only": {
        "title": "逾窗仍单边 → 落终态",
        "summary": (
            "缓冲到截止仍等不到对侧，按逾窗规则落仅左/仅右终态并计入差异合计；缓冲清空，"
            "不会留在缓冲却又从统计里消失。"
        ),
        "steps": [
            Step(
                label="第 1 趟：单边行进入缓冲",
                note="时钟 08:12，允许迟到 0 秒。左端 SKU-300、右端 SKU-400 均单边 → 进缓冲。",
                window_start="2026-11-12T08:00:00Z",
                window_end="2026-11-12T08:15:00Z",
                max_lateness_seconds=0,
                as_of="2026-11-12T08:12:00Z",
            ),
            Step(
                label="第 2 趟：时钟越过截止 → 落单边终态",
                note="时钟 08:15。缓冲行的截止已过，按 finalize 规则落 left_only / right_only 并计入差异合计。",
                window_start="2026-11-12T08:00:00Z",
                window_end="2026-11-12T08:15:00Z",
                max_lateness_seconds=0,
                as_of="2026-11-12T08:15:00Z",
            ),
        ],
    },
    "slide_window_clears_buffer": {
        "title": "推进窗口上沿 → 缓冲清算",
        "summary": (
            "窗口上沿推进后，缓冲行的事件时间已滑出窗口，必须按同一逾窗规则清算；"
            "批次报告里 buffered 归零、逾窗落差额计入。"
        ),
        "steps": [
            Step(
                label="第 1 趟：长迟到，单边行进入缓冲",
                note="时钟 08:12，允许迟到 1 小时，SKU-300 / SKU-400 单边进缓冲。",
                window_start="2026-11-12T08:00:00Z",
                window_end="2026-11-12T08:15:00Z",
                max_lateness_seconds=3600,
                as_of="2026-11-12T08:12:00Z",
            ),
            Step(
                label="第 2 趟：上沿推进到 08:30 → 缓冲清算",
                note="窗口 [08:15, 08:30) 已滑过缓冲行事件时间（08:10 / 08:12），仍单边的行清算落终态。",
                window_start="2026-11-12T08:15:00Z",
                window_end="2026-11-12T08:30:00Z",
                max_lateness_seconds=3600,
                as_of="2026-11-12T08:20:00Z",
            ),
        ],
    },
    "discard_over_window": {
        "title": "逾窗显式丢弃并计数",
        "summary": (
            "把同一逾窗规则切到显式丢弃：缓冲行离开缓冲、计入 discarded，"
            "但不进差异合计 —— 规则可配，且两种去向都计数。"
        ),
        "steps": [
            Step(
                label="第 1 趟：丢弃规则下先进入缓冲",
                note="时钟 08:12，允许迟到 0 秒，单边行仍在缓冲。",
                window_start="2026-11-12T08:00:00Z",
                window_end="2026-11-12T08:15:00Z",
                max_lateness_seconds=0,
                as_of="2026-11-12T08:12:00Z",
                liquidation=LIQUIDATION_DISCARD,
            ),
            Step(
                label="第 2 趟：逾窗显式丢弃并计数",
                note="时钟 08:15，缓冲行逾期，按 discard 规则显式丢弃：discarded=2、buffered=0。",
                window_start="2026-11-12T08:00:00Z",
                window_end="2026-11-12T08:15:00Z",
                max_lateness_seconds=0,
                as_of="2026-11-12T08:15:00Z",
                liquidation=LIQUIDATION_DISCARD,
            ),
        ],
    },
}


def list_scenarios() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "title": meta["title"],
            "summary": meta["summary"],
            "steps": [
                {
                    "label": s.label,
                    "note": s.note,
                    "window_start": s.window_start,
                    "window_end": s.window_end,
                    "max_lateness_seconds": s.max_lateness_seconds,
                    "as_of": s.as_of,
                    "liquidation": s.liquidation,
                }
                for s in meta["steps"]
            ],
        }
        for key, meta in SCENARIOS.items()
    ]


def _load_fixture(name: str) -> list[dict[str, Any]]:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def _arrivals(rows: list[dict[str, Any]], time_field: str, as_of: str) -> list[dict[str, Any]]:
    """Rows observable at ``as_of`` — the arrival cut for that pass."""
    clock = parse_ts(as_of)
    return [
        row for row in rows if (dt := parse_ts(row.get(time_field))) is not None and dt <= clock
    ]


def run_scenario(scenario: str, *, reset: bool = True) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    meta = SCENARIOS[scenario]

    conn = connect()
    try:
        pair = pair_repo.get_pair_by_code(conn, DEMO_PAIR_CODE)
    finally:
        conn.close()
    if pair is None:
        raise ValueError(f"demo pair {DEMO_PAIR_CODE} not found")

    reset_info = svc.clear_pair_buffer(pair["id"], note="演示前置：按逾窗规则清算历史缓冲") if reset else None

    left_fixture = _load_fixture(pair["left_fixture"])
    right_fixture = _load_fixture(pair["right_fixture"])
    passes = []
    for index, step in enumerate(meta["steps"], start=1):
        left_arrivals = _arrivals(left_fixture, "updated_at", step.as_of)
        right_arrivals = _arrivals(right_fixture, "updated_at", step.as_of)
        result = svc.run_reconcile(
            pair["id"],
            note=f"演示 · {meta['title']} · {step.label}",
            window_start=step.window_start,
            window_end=step.window_end,
            max_lateness_seconds=step.max_lateness_seconds,
            left_time_field="updated_at",
            right_time_field="updated_at",
            liquidation=step.liquidation,
            as_of=step.as_of,
            persist_window=False,
            left_rows_override=left_arrivals,
            right_rows_override=right_arrivals,
        )
        passes.append(
            {
                "step": index,
                "label": step.label,
                "note": step.note,
                "as_of": step.as_of,
                "left_arrivals": len(left_arrivals),
                "right_arrivals": len(right_arrivals),
                "batch": result,
            }
        )
    return {
        "scenario": scenario,
        "title": meta["title"],
        "summary": meta["summary"],
        "reset": reset_info,
        "passes": passes,
        "active_buffer": svc.list_buffer(pair_id=pair["id"]),
    }
