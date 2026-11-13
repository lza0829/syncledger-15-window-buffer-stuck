"""Unit tests for the window buffer engine — the semantics the task pins down."""

from __future__ import annotations

import pytest

from app.engines.window import (
    LIQUIDATION_DISCARD,
    LIQUIDATION_FINALIZE,
    WindowSpec,
    parse_ts,
    run_windowed,
)

ALIGN = ["id"]


def W(start, end, lateness=0, liquidation=LIQUIDATION_FINALIZE):
    return WindowSpec(
        start=parse_ts(start),
        end=parse_ts(end),
        max_lateness_seconds=lateness,
        left_time_field="t",
        right_time_field="t",
        liquidation=liquidation,
    )


def row(id_, t, **kw):
    return {"id": id_, "t": t, **kw}


def run(left, right, buffer=None, *, window, as_of=None, compare=("v",)):
    return run_windowed(
        left,
        right,
        buffer or [],
        align_keys=ALIGN,
        compare_fields=list(compare),
        window=window,
        as_of=parse_ts(as_of) if as_of else None,
    )


W_ = W("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z")


def test_outside_window_rows_do_not_participate():
    res = run(
        [row("A", "2025-12-31T23:00:00Z", v=1), row("A", "2026-01-01T00:00:00Z", v=1)],
        [row("A", "2026-01-01T00:01:00Z", v=1)],
        window=W_,
    )
    c = res["counts"]
    assert c["outside_window"] == 1
    assert c["inside_left"] == 1 and c["inside_right"] == 1
    assert c["matched"] == 1
    assert c["diff_total"] == 0


def test_row_inside_window_but_after_clock_is_not_arrived_yet():
    res = run(
        [row("A", "2026-01-01T00:05:00Z", v=1)],
        [],
        window=W_,
        as_of="2026-01-01T00:01:00Z",
    )
    assert res["counts"]["not_arrived_left"] == 1
    assert res["counts"]["buffered"] == 0
    assert res["counts"]["diff_total"] == 0


def test_single_sided_row_is_buffered_not_terminal():
    res = run([row("A", "2026-01-01T00:02:00Z", v=1)], [], window=W_)
    c = res["counts"]
    assert c["buffered"] == 1 and c["buffered_left"] == 1
    assert c["left_only"] == 0
    assert c["diff_total"] == 0, "buffered rows must not enter the terminal diff total"
    assert res["buffer"][0]["align_key"] == {"id": "A"}


def test_counterpart_within_window_aligns_and_leaves_buffer():
    first = run([row("A", "2026-01-01T00:02:00Z", v=1)], [], window=W_)
    assert first["counts"]["buffered"] == 1
    second = run(
        [],
        [row("A", "2026-01-01T00:03:00Z", v=1)],
        first["buffer"],
        window=W_,
        as_of="2026-01-01T00:05:00Z",
    )
    c = second["counts"]
    assert c["matched"] == 1
    assert c["aligned_after_buffer"] == 1
    assert c["buffered"] == 0
    assert second["settled"][0]["state"] == "aligned"


def test_counterpart_arriving_after_alignment_moves_to_field_compare():
    first = run([row("A", "2026-01-01T00:02:00Z", v=1)], [], window=W_)
    second = run(
        [],
        [row("A", "2026-01-01T00:03:00Z", v=2)],
        first["buffer"],
        window=W_,
        as_of="2026-01-01T00:05:00Z",
    )
    assert second["counts"]["field_mismatch"] == 1
    assert second["diffs"][0]["origin"] == "aligned_after_buffer"
    assert second["diffs"][0]["mismatched_fields"] == ["v"]


def test_over_window_single_sided_finalises_side_only_and_clears_buffer():
    first = run([row("A", "2026-01-01T00:02:00Z", v=1)], [], window=W_)
    assert first["counts"]["buffered"] == 1
    second = run(
        [],
        [],
        first["buffer"],
        window=W_,
        as_of="2026-01-01T00:10:00Z",
    )
    c = second["counts"]
    assert c["buffered"] == 0
    assert c["left_only"] == 1
    assert c["expired_left"] == 1
    assert c["diff_total"] == 1
    assert second["settled"][0]["state"] == "finalized"


def test_over_window_discard_rule_discards_and_counts_without_diff():
    w = W("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", liquidation=LIQUIDATION_DISCARD)
    first = run([row("A", "2026-01-01T00:02:00Z", v=1)], [], window=w)
    second = run([], [], first["buffer"], window=w, as_of="2026-01-01T00:10:00Z")
    c = second["counts"]
    assert c["buffered"] == 0
    assert c["discarded"] == 1
    assert c["diff_total"] == 0
    assert second["settled"][0]["state"] == "discarded"


def test_advancing_window_upper_edge_liquidates_buffered_rows():
    first = run(
        [row("A", "2026-01-01T00:02:00Z", v=1)],
        [],
        window=W("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", lateness=7200),
    )
    assert first["counts"]["buffered"] == 1, "long lateness keeps it pending"
    slid = W("2026-01-01T00:05:00Z", "2026-01-01T00:20:00Z", lateness=7200)
    second = run([], [], first["buffer"], window=slid, as_of="2026-01-01T00:12:00Z")
    c = second["counts"]
    assert c["buffered"] == 0, "row must not linger in buffer while missing from totals"
    assert c["left_only"] == 1
    assert second["settled"][0]["reason"] == "window_slid_past_event"


def test_every_buffered_row_is_accounted_for():
    """Conservation: every extracted row lands in exactly one reported bucket."""

    left = [row(f"L{i}", "2026-01-01T00:01:00Z") for i in range(4)] + [
        row("OLD", "2025-12-31T00:00:00Z"),
        row("YET", "2026-01-01T00:08:00Z"),
    ]
    right = [row(f"L{i}", "2026-01-01T00:02:00Z") for i in range(2)]
    res = run(left, right, window=W("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z"), as_of="2026-01-01T00:05:00Z")
    c = res["counts"]
    # L0/L1 align left→right; L2/L3 have no counterpart → buffered;
    # OLD is outside the window; YET is inside but after the clock.
    assert c["matched"] == 2
    assert c["buffered"] == 2
    assert c["outside_window"] == 1
    assert c["not_arrived"] == 1
    assert c["diff_total"] == 0
    # Per-side conservation: inside + outside + not-arrived == extracted rows.
    assert c["inside_left"] + c["outside_left"] + c["not_arrived_left"] == c["left_rows"]
    assert c["inside_right"] + c["outside_right"] + c["not_arrived_right"] == c["right_rows"]
    # Outcome conservation: terminal outcomes + pending buffer ≤ inside rows,
    # and the buffer is reported separately from diff_total.
    assert (
        c["matched"] + c["left_only"] + c["right_only"] + c["field_mismatch"] + c["buffered"]
        <= c["inside_left"] + c["inside_right"]
    )
    assert c["diff_total"] == c["left_only"] + c["right_only"] + c["field_mismatch"]


def test_identical_rearrival_keeps_original_deadline():
    first = run([row("A", "2026-01-01T00:02:00Z", v=1)], [], window=W_)
    again = run(
        [row("A", "2026-01-01T00:02:00Z", v=1)],
        [],
        first["buffer"],
        window=W_,
        as_of="2026-01-01T00:04:00Z",
    )
    assert again["counts"]["buffered"] == 1
    assert again["counts"]["superseded"] == 0
    assert again["buffer"][0]["deadline"] == first["buffer"][0]["deadline"]


def test_changed_rearrival_supersedes_and_counts():
    first = run([row("A", "2026-01-01T00:02:00Z", v=1)], [], window=W_)
    changed = run(
        [row("A", "2026-01-01T00:02:00Z", v=2)],
        [],
        first["buffer"],
        window=W_,
        as_of="2026-01-01T00:04:00Z",
    )
    assert changed["counts"]["superseded"] == 1
    assert changed["counts"]["buffered"] == 1
    assert changed["settled"][0]["state"] == "superseded"


def test_window_spec_rejects_bad_configuration():
    with pytest.raises(ValueError):
        W("2026-01-01T00:10:00Z", "2026-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        W("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", lateness=-1)
    with pytest.raises(ValueError):
        W("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z", liquidation="nope")
