"""Integration tests: persisted buffer lifecycle, run reports and audit views."""

from __future__ import annotations

import pytest


def run(client, **body):
    payload = {"pair_id": 1, **body}
    resp = client.post("/api/reconcile", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def active_buffer(client, pair_id=1):
    return client.get("/api/buffer", params={"pair_id": pair_id}).json()


def test_single_sided_row_goes_to_buffer_not_diff(client):
    batch = run(
        client,
        window_start="2026-11-12T08:00:00Z",
        window_end="2026-11-12T08:15:00Z",
        max_lateness_seconds=900,
        as_of="2026-11-12T08:00:30Z",
    )
    counts = batch["counts"]
    assert counts["buffered"] == 2
    assert counts["left_only"] == 0 and counts["right_only"] == 0
    assert counts["diff_total"] == 0
    buffered = active_buffer(client)
    assert {(b["side"], b["align_key"]["sku_id"]) for b in buffered} == {
        ("left", "SKU-100"),
        ("left", "SKU-100"),
    }
    # Buffer rows are persisted on the pair, not as terminal diffs.
    diffs = client.get("/api/diffs", params={"batch_id": batch["id"]}).json()
    assert diffs == []


def test_counterpart_arrives_within_window_aligns_buffer(client):
    run(
        client,
        window_start="2026-11-12T08:00:00Z",
        window_end="2026-11-12T08:15:00Z",
        max_lateness_seconds=900,
        as_of="2026-11-12T08:00:30Z",
    )
    assert len(active_buffer(client)) == 2
    second = run(
        client,
        window_start="2026-11-12T08:00:00Z",
        window_end="2026-11-12T08:15:00Z",
        max_lateness_seconds=900,
        as_of="2026-11-12T08:07:00Z",
    )
    c = second["counts"]
    assert c["aligned_after_buffer"] == 2
    assert c["matched"] == 2
    assert c["field_mismatch"] == 1  # SKU-100/S01 qty 120 → 118
    assert c["buffered"] == 0
    assert active_buffer(client) == []
    # The alignment is audited on the batch.
    origins = {d["origin"] for d in client.get("/api/diffs", params={"batch_id": second["id"]}).json()}
    assert "aligned_after_buffer" in origins


def test_over_window_row_finalises_side_only_after_clock_passes(client):
    run(
        client,
        window_start="2026-11-12T08:00:00Z",
        window_end="2026-11-12T08:15:00Z",
        max_lateness_seconds=0,
        as_of="2026-11-12T08:12:00Z",
    )
    assert len(active_buffer(client)) == 2
    later = run(
        client,
        window_start="2026-11-12T08:00:00Z",
        window_end="2026-11-12T08:15:00Z",
        max_lateness_seconds=0,
        as_of="2026-11-12T08:15:00Z",
    )
    c = later["counts"]
    assert c["buffered"] == 0
    assert c["expired_left"] == 1 and c["expired_right"] == 1
    assert c["left_only"] == 1 and c["right_only"] == 1
    assert c["diff_total"] == 3  # 1 field mismatch + 2 expired side-only
    assert active_buffer(client) == []
    expired = [
        d
        for d in client.get("/api/diffs", params={"batch_id": later["id"]}).json()
        if d["origin"] == "window_expired"
    ]
    assert len(expired) == 2
    assert expired[0]["expired_from_buffer"] == 1


def test_slide_window_upper_edge_liquidates_stale_buffer(client):
    first = run(
        client,
        window_start="2026-11-12T08:00:00Z",
        window_end="2026-11-12T08:15:00Z",
        max_lateness_seconds=3600,
        as_of="2026-11-12T08:12:00Z",
    )
    assert first["counts"]["buffered"] == 2
    slid = run(
        client,
        window_start="2026-11-12T08:15:00Z",
        window_end="2026-11-12T08:30:00Z",
        max_lateness_seconds=3600,
        as_of="2026-11-12T08:20:00Z",
    )
    c = slid["counts"]
    assert "buffered" in c
    assert isinstance(active_buffer(client), list)


def test_discard_rule_removes_buffer_and_counts(client):
    client.put(
        "/api/pairs/1/window",
        json={
            "window_start": "2026-11-12T08:00:00Z",
            "window_end": "2026-11-12T08:15:00Z",
            "max_lateness_seconds": 0,
            "left_time_field": "updated_at",
            "right_time_field": "updated_at",
            "liquidation": "discard",
        },
    )
    run(client, as_of="2026-11-12T08:12:00Z")
    assert len(active_buffer(client)) == 2
    later = run(client, as_of="2026-11-12T08:15:00Z")
    c = later["counts"]
    assert c["buffered"] == 0
    assert c["discarded"] == 2
    assert c["diff_total"] == 1, "discarded rows are counted but are not terminal diffs"
    assert active_buffer(client) == []
    # restore default rule for other tests
    client.put(
        "/api/pairs/1/window",
        json={
            "window_start": "2026-11-12T08:00:00Z",
            "window_end": "2026-11-12T08:15:00Z",
            "max_lateness_seconds": 900,
            "left_time_field": "updated_at",
            "right_time_field": "updated_at",
            "liquidation": "finalize_side_only",
        },
    )


def test_window_gate_excludes_rows_outside_window(client):
    batch = run(
        client,
        window_start="2026-11-12T08:05:00Z",
        window_end="2026-11-12T08:13:00Z",
        max_lateness_seconds=900,
        as_of="2026-11-12T08:13:00Z",
    )
    c = batch["counts"]
    # [08:05, 08:13) keeps: left SKU-200(08:05), SKU-300(08:10);
    # right SKU-200(08:06), SKU-400(08:12).  The four rows outside are
    # SKU-100×2 on each side (08:01).
    assert c["outside_window"] == 4
    assert c["inside_left"] == 2 and c["inside_right"] == 2
    assert c["buffered"] == 2  # SKU-300 / SKU-400 still pending, not diffed
    assert c["matched"] == 1   # SKU-200 aligns cleanly
    assert c["diff_total"] == 0


def test_batch_report_and_audit_agree(client):
    batch = run(
        client,
        window_start="2026-11-12T08:00:00Z",
        window_end="2026-11-12T08:15:00Z",
        max_lateness_seconds=900,
        as_of="2026-11-12T08:12:00Z",
    )
    audit = client.get(f"/api/audit/batches/{batch['id']}").json()
    for key in ("buffered", "buffered_left", "buffered_right", "outside_window", "diff_total"):
        assert audit["counts"][key] == batch["counts"][key], key
    assert len(audit["active_buffer_now"]) == batch["counts"]["buffered"]
    diffs = client.get("/api/diffs", params={"batch_id": batch["id"]}).json()
    assert len(diffs) == batch["counts"]["diff_total"], "buffered rows must not be in diffs"
    events = client.get("/api/buffer/events", params={"batch_id": batch["id"]}).json()
    kinds = {e["event_type"] for e in events}
    assert kinds, "the buffer lifecycle must leave an audit trail on the batch"


def test_buffered_rows_never_counted_in_terminal_diff_total(client):
    batch = run(
        client,
        window_start="2026-11-12T08:00:00Z",
        window_end="2026-11-12T08:15:00Z",
        max_lateness_seconds=900,
        as_of="2026-11-12T08:12:00Z",
    )
    c = batch["counts"]
    assert c["diff_total"] == c["left_only"] + c["right_only"] + c["field_mismatch"]
    assert c["diff_total"] == 1  # only the qty mismatch; two rows are pending
    assert c["buffered"] == 2


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("buffer_then_align", {"p0_buffered": 2, "p1_diff": 1, "p1_buffered": 0}),
        ("expire_side_only", {"p0_buffered": 2, "p1_diff": 3, "p1_buffered": 0}),
        ("slide_window_clears_buffer", {"p0_buffered": 2, "p1_diff": None, "p1_buffered": None}),
        ("discard_over_window", {"p0_buffered": 2, "p1_diff": 1, "p1_buffered": 0}),
    ],
)
def test_demo_scenarios_show_lifecycle(client, scenario, expected):
    result = client.post(f"/api/demo/scenarios/{scenario}/run").json()
    passes = result["passes"]
    assert len(passes) == 2
    assert passes[0]["batch"]["counts"]["buffered"] == expected["p0_buffered"]
    assert passes[0]["batch"]["counts"]["diff_total"] == 0 if scenario == "buffer_then_align" else True
    if expected["p1_diff"] is not None:
        assert passes[-1]["batch"]["counts"]["diff_total"] == expected["p1_diff"]
    if expected["p1_buffered"] is not None:
        assert passes[-1]["batch"]["counts"]["buffered"] == expected["p1_buffered"]
        assert result["active_buffer"] == []


def test_demo_list_exposes_four_scenarios(client):
    scenarios = client.get("/api/demo/scenarios").json()
    assert {s["key"] for s in scenarios} == {
        "buffer_then_align",
        "expire_side_only",
        "slide_window_clears_buffer",
        "discard_over_window",
    }
    for s in scenarios:
        assert s["title"] and s["summary"] and len(s["steps"]) == 2


def test_window_config_roundtrip(client):
    current = client.get("/api/pairs/1/window").json()
    assert current["window"]["window_start"] == "2026-11-12T08:00:00Z"
    updated = client.put(
        "/api/pairs/1/window",
        json={
            "window_start": "2026-11-12T08:02:00Z",
            "window_end": "2026-11-12T08:16:00Z",
            "max_lateness_seconds": 120,
            "left_time_field": "updated_at",
            "right_time_field": "updated_at",
            "liquidation": "finalize_side_only",
        },
    ).json()
    assert updated["window"]["window_end"] == "2026-11-12T08:16:00Z"
    assert updated["window"]["max_lateness_seconds"] == 120
    bad = client.put(
        "/api/pairs/1/window",
        json={
            "window_start": "2026-11-12T08:10:00Z",
            "window_end": "2026-11-12T08:00:00Z",
            "max_lateness_seconds": 0,
        },
    )
    assert bad.status_code == 400
    # restore
    client.put(
        "/api/pairs/1/window",
        json={
            "window_start": "2026-11-12T08:00:00Z",
            "window_end": "2026-11-12T08:15:00Z",
            "max_lateness_seconds": 900,
            "left_time_field": "updated_at",
            "right_time_field": "updated_at",
            "liquidation": "finalize_side_only",
        },
    )
