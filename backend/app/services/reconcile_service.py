from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.reconcile import compare_sides
from app.engines.window import (
    LIQUIDATION_DISCARD,
    LIQUIDATION_FINALIZE,
    LIQUIDATION_RULES,
    WindowSpec,
    first_timestamp,
    key_str,
    last_timestamp,
    parse_ts,
    run_windowed,
)
from app.repositories import batches as batch_repo
from app.repositories import contracts as contract_repo
from app.repositories import pairs as pair_repo
from app.repositories import tickets as ticket_repo
from app.repositories import windows as window_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_fixture(name: str) -> list[dict[str, Any]]:
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"fixture {name} must be a JSON array")
    return data


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            "pair_count": conn.execute("SELECT COUNT(*) c FROM reconcile_pairs").fetchone()["c"],
            "enabled_pairs": conn.execute(
                "SELECT COUNT(*) c FROM reconcile_pairs WHERE enabled=1"
            ).fetchone()["c"],
            "batch_count": conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"],
            "diff_count": conn.execute("SELECT COUNT(*) c FROM reconcile_diffs").fetchone()["c"],
            "open_dispositions": conn.execute(
                "SELECT COUNT(*) c FROM diff_tickets WHERE status='open'"
            ).fetchone()["c"],
            "active_buffer": conn.execute(
                "SELECT COUNT(*) c FROM reconcile_buffer WHERE state='buffered'"
            ).fetchone()["c"],
            "policy": conn.execute(
                "SELECT value FROM settings WHERE key='default_policy'"
            ).fetchone()["value"],
        }
    finally:
        conn.close()


def list_pairs() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return pair_repo.list_pairs(conn)
    finally:
        conn.close()


def list_batches(limit: int = 50, pair_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = batch_repo.list_batches(conn, limit=limit, pair_id=pair_id)
        pairs = {p["id"]: p for p in pair_repo.list_pairs(conn)}
        for r in rows:
            p = pairs.get(r["pair_id"])
            r["pair_code"] = p["code"] if p else None
            r["pair_title"] = p["title"] if p else None
        return rows
    finally:
        conn.close()


def list_diffs(batch_id: int | None = None, pair_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_diffs(conn, batch_id=batch_id, pair_id=pair_id)
    finally:
        conn.close()


def list_dispositions() -> list[dict[str, Any]]:
    """Difference disposition records (not a customer-service ticket desk)."""
    conn = connect()
    try:
        return ticket_repo.list_tickets(conn)
    finally:
        conn.close()


def list_contracts() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return contract_repo.list_contracts(conn)
    finally:
        conn.close()


def list_settings() -> dict[str, str]:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def run_reconcile(
    pair_id: int,
    note: str | None = None,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    max_lateness_seconds: int | None = None,
    left_time_field: str | None = None,
    right_time_field: str | None = None,
    liquidation: str | None = None,
    as_of: str | None = None,
    use_window: bool = True,
    persist_window: bool = True,
    left_rows_override: list[dict[str, Any]] | None = None,
    right_rows_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract both fixtures, align inside the configured window, persist.

    Windowed mode (default) keeps a side-only buffer: a row whose counterpart
    has not arrived stays pending (never a terminal ``left_only`` /
    ``right_only``) until it aligns inside the window or is settled by the
    over-window rule.  The base non-windowed comparison remains available via
    ``use_window=False`` for the plain full-snapshot reconcile.

    ``left_rows_override`` / ``right_rows_override`` stage a pass with an
    explicit arrival set (used by the scripted demos and by buffer liquidation).
    """
    conn = connect()
    try:
        pair = pair_repo.get_pair(conn, pair_id)
        if not pair:
            raise ValueError("pair not found")
        if not pair["enabled"]:
            raise ValueError("pair disabled")

        left_rows = (
            _load_fixture(pair["left_fixture"])
            if left_rows_override is None
            else left_rows_override
        )
        right_rows = (
            _load_fixture(pair["right_fixture"])
            if right_rows_override is None
            else right_rows_override
        )
        now = _now()

        if not use_window:
            result = compare_sides(
                left_rows,
                right_rows,
                align_keys=pair["align_keys"],
                compare_fields=pair["compare_fields"],
            )
            return _persist_plain_batch(conn, pair, result, note, now)

        window = _resolve_window(
            conn,
            pair,
            window_start=window_start,
            window_end=window_end,
            max_lateness_seconds=max_lateness_seconds,
            left_time_field=left_time_field,
            right_time_field=right_time_field,
            liquidation=liquidation,
            as_of=as_of,
            persist=persist_window,
            now=now,
        )
        prior = window_repo.active_buffer_entries(conn, pair_id)
        result = run_windowed(
            left_rows,
            right_rows,
            prior,
            align_keys=pair["align_keys"],
            compare_fields=pair["compare_fields"],
            window=window,
            as_of=parse_ts(as_of) if as_of else None,
        )
        return _persist_windowed_batch(conn, pair, result, prior, note, now)
    finally:
        conn.close()


def _persist_plain_batch(
    conn, pair: dict[str, Any], result: dict[str, Any], note: str | None, now: str
) -> dict[str, Any]:
    batch_id = _next_batch(
        conn,
        pair_id=pair["id"],
        counts=result["counts"],
        note=note,
        now=now,
        windowed=False,
        window=None,
    )
    for diff in result["diffs"]:
        if diff["kind"] == "matched":
            continue
        batch_repo.insert_diff(
            conn, batch_id=batch_id, pair_id=pair["id"], diff=diff, created_at=now
        )
    conn.commit()
    batch = batch_repo.get_batch(conn, batch_id)
    assert batch is not None
    batch["pair_code"] = pair["code"]
    batch["pair_title"] = pair["title"]
    batch["policy"] = "record_only"
    batch["counts"] = result["counts"]
    batch["buffer"] = []
    batch["settled"] = []
    batch["diff_preview"] = batch_repo.list_diffs(conn, batch_id=batch_id, limit=20)
    return batch


def _next_batch(
    conn,
    *,
    pair_id: int,
    counts: dict[str, Any],
    note: str | None,
    now: str,
    windowed: bool,
    window: dict[str, Any] | None,
) -> int:
    seq = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
    batch_no = f"RB-{now[:10].replace('-', '')}-{seq:04d}"
    return batch_repo.insert_batch(
        conn,
        batch_no=batch_no,
        pair_id=pair_id,
        status="completed",
        counts=counts,
        note=note,
        created_at=now,
        windowed=windowed,
        window=window,
    )


def _persist_windowed_batch(
    conn,
    pair: dict[str, Any],
    result: dict[str, Any],
    prior: list[dict[str, Any]],
    note: str | None,
    now: str,
) -> dict[str, Any]:
    counts = result["counts"]
    window_info = {
        "window_start": result["window"]["start"],
        "window_end": result["window"]["end"],
        "max_lateness_seconds": result["window"]["max_lateness_seconds"],
        "left_time_field": result["window"]["left_time_field"],
        "right_time_field": result["window"]["right_time_field"],
        "liquidation": result["window"]["liquidation"],
        "as_of": result["as_of"],
    }
    batch_id = _next_batch(
        conn,
        pair_id=pair["id"],
        counts=counts,
        note=note,
        now=now,
        windowed=True,
        window=window_info,
    )

    # --- diffs (terminal only; buffered rows are deliberately absent) -------
    for diff in result["diffs"]:
        if diff["kind"] == "matched":
            continue
        batch_repo.insert_diff(
            conn,
            batch_id=batch_id,
            pair_id=pair["id"],
            diff=diff,
            created_at=now,
        )

    # --- settle closed buffer rows so the table matches the report ----------
    for item in result["settled"]:
        window_repo.buffer_settle(
            conn,
            pair_id=pair["id"],
            side=item["side"],
            align_key=item["align_key"],
            state=item["state"],
            batch_id=batch_id,
            reason=item.get("reason"),
            buffer_id=item.get("buffer_id"),
            now=now,
        )

    # --- (re)enter still-active buffer rows ---------------------------------
    buffered_ids: dict[str, int] = {}
    for entry in result["buffer"]:
        buffer_id, created = window_repo.buffer_enter(
            conn,
            pair_id=pair["id"],
            side=entry["side"],
            align_key=entry["align_key"],
            row=entry["row"],
            event_time=entry["event_time"],
            deadline=entry["deadline"],
            window_start=entry["buffered_window_start"],
            window_end=entry["buffered_window_end"],
            batch_id=batch_id,
            now=now,
        )
        buffered_ids[f"{entry['side']}:{key_str(entry['align_key'])}"] = buffer_id
        entry["buffer_id"] = buffer_id
        entry["entered_this_batch"] = created

    # Backfill the engine's unset buffer_ids on settled entries?  Settled
    # entries always carry the prior buffer_id, so nothing to backfill here.

    # --- audit event trail --------------------------------------------------
    for event in result["events"]:
        buffer_id = _event_buffer_id(event, buffered_ids, prior)
        window_repo.insert_event(
            conn,
            batch_id=batch_id,
            pair_id=pair["id"],
            buffer_id=buffer_id,
            event_type=event["type"],
            state=event.get("state") or "",
            side=event.get("side") or "",
            align_key=event.get("align_key") or {},
            row=event.get("row"),
            event_time=event.get("event_time"),
            deadline=event.get("deadline"),
            reason=event.get("reason"),
            created_at=now,
        )

    conn.commit()
    batch = batch_repo.get_batch(conn, batch_id)
    assert batch is not None
    batch["pair_code"] = pair["code"]
    batch["pair_title"] = pair["title"]
    batch["policy"] = "record_only"
    batch["counts"] = counts
    batch["window"] = result["window"]
    batch["as_of"] = result["as_of"]
    batch["buffer"] = result["buffer"]
    batch["settled"] = result["settled"]
    batch["events"] = result["events"]
    batch["diff_preview"] = batch_repo.list_diffs(conn, batch_id=batch_id, limit=20)
    return batch


def _event_buffer_id(
    event: dict[str, Any],
    buffered_ids: dict[str, int],
    prior: list[dict[str, Any]],
) -> int | None:
    key = f"{event.get('side')}:{key_str(event['align_key'])}"
    if key in buffered_ids:
        return buffered_ids[key]
    for entry in prior:
        if entry["side"] == event.get("side") and key_str(entry["align_key"]) == key_str(
            event["align_key"]
        ):
            return entry.get("buffer_id")
    return None


def _resolve_window(
    conn,
    pair: dict[str, Any],
    *,
    window_start: str | None,
    window_end: str | None,
    max_lateness_seconds: int | None,
    left_time_field: str | None,
    right_time_field: str | None,
    liquidation: str | None,
    as_of: str | None,
    persist: bool,
    now: str,
) -> WindowSpec:
    """Merge request overrides over the pair's stored window.

    When no window is stored at all, fall back to the fixtures' own timestamp
    span so the pair still runs windowed out of the box.
    """
    stored = window_repo.get_window(conn, pair["id"]) or {}
    start_raw = window_start or stored.get("window_start")
    end_raw = window_end or stored.get("window_end")
    lateness = (
        max_lateness_seconds
        if max_lateness_seconds is not None
        else stored.get("max_lateness_seconds")
    )
    left_field = left_time_field or stored.get("left_time_field") or "updated_at"
    right_field = right_time_field or stored.get("right_time_field") or left_field
    rule = liquidation or stored.get("liquidation") or LIQUIDATION_FINALIZE
    if lateness is None:
        lateness = 0

    if not start_raw or not end_raw:
        left_rows = _load_fixture(pair["left_fixture"])
        right_rows = _load_fixture(pair["right_fixture"])
        lo = [dt for dt in (first_timestamp(left_rows, left_field), first_timestamp(right_rows, right_field)) if dt]
        hi = [dt for dt in (last_timestamp(left_rows, left_field), last_timestamp(right_rows, right_field)) if dt]
        if not lo or not hi:
            raise ValueError("window not configured and fixtures carry no timestamps")
        start = min(lo)
        end = max(hi) + timedelta(seconds=1)

    start_dt = parse_ts(start_raw)
    end_dt = parse_ts(end_raw)
    if start_dt is None or end_dt is None:
        raise ValueError("window_start / window_end must be ISO-8601 timestamps")

    spec = WindowSpec(
        start=start_dt,
        end=end_dt,
        max_lateness_seconds=int(lateness),
        left_time_field=left_field,
        right_time_field=right_field,
        liquidation=rule,
    )
    if persist:
        window_repo.upsert_window(
            conn,
            pair["id"],
            {
                "window_start": spec.as_dict()["start"],
                "window_end": spec.as_dict()["end"],
                "max_lateness_seconds": spec.max_lateness_seconds,
                "left_time_field": spec.left_time_field,
                "right_time_field": spec.right_time_field,
                "liquidation": spec.liquidation,
                "auto_slide": stored.get("auto_slide"),
            },
            now,
        )
    return spec


def clear_pair_buffer(pair_id: int, note: str | None = None) -> dict[str, Any] | None:
    """Settle the pair's active buffer by the over-window rule.

    Used before a demo so scenarios start from a clean, *accounted* buffer:
    every removed row goes through the real settlement path (finalize or
    discard, both counted) rather than being deleted silently.
    """
    conn = connect()
    try:
        pair = pair_repo.get_pair(conn, pair_id)
        if not pair:
            raise ValueError("pair not found")
        active = window_repo.active_buffer_entries(conn, pair_id)
        if not active:
            return None
        deadlines = [parse_ts(e.get("deadline")) for e in active]
        deadlines = [d for d in deadlines if d is not None]
        clock = max(deadlines) if deadlines else datetime.now(timezone.utc)
        result = run_windowed(
            [],
            [],
            active,
            align_keys=pair["align_keys"],
            compare_fields=pair["compare_fields"],
            window=WindowSpec(
                start=parse_ts("1970-01-01T00:00:00Z"),  # type: ignore[arg-type]
                end=parse_ts("1970-01-01T00:00:01Z"),  # type: ignore[arg-type]
                max_lateness_seconds=0,
                left_time_field="updated_at",
                right_time_field="updated_at",
                liquidation=LIQUIDATION_DISCARD,
            ),
            as_of=clock,
        )
        return _persist_windowed_batch(conn, pair, result, active, note, _now())
    finally:
        conn.close()


def list_windows() -> list[dict[str, Any]]:
    conn = connect()
    try:
        pairs = pair_repo.list_pairs(conn)
        out = []
        for p in pairs:
            w = window_repo.get_window(conn, p["id"])
            out.append(
                {
                    "pair_id": p["id"],
                    "pair_code": p["code"],
                    "pair_title": p["title"],
                    "align_keys": p["align_keys"],
                    "compare_fields": p["compare_fields"],
                    "window": w,
                    "active_buffer": window_repo.list_buffer(conn, pair_id=p["id"], state="buffered"),
                }
            )
        return out
    finally:
        conn.close()


def get_pair_window(pair_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        pair = pair_repo.get_pair(conn, pair_id)
        if not pair:
            raise ValueError("pair not found")
        stored = window_repo.get_window(conn, pair_id)
        return {
            "pair_id": pair_id,
            "pair_code": pair["code"],
            "pair_title": pair["title"],
            "window": stored,
            "buffered": window_repo.list_buffer(conn, pair_id=pair_id, state="buffered"),
        }
    finally:
        conn.close()


def update_pair_window(pair_id: int, values: dict[str, Any]) -> dict[str, Any]:
    conn = connect()
    try:
        pair = pair_repo.get_pair(conn, pair_id)
        if not pair:
            raise ValueError("pair not found")
        start = parse_ts(values.get("window_start"))
        if start is None:
            raise ValueError("window_start must be an ISO-8601 timestamp")
        end = parse_ts(values.get("window_end"))
        if end is None:
            raise ValueError("window_end must be an ISO-8601 timestamp")
        lateness = values.get("max_lateness_seconds")
        if lateness is not None and int(lateness) < 0:
            raise ValueError("max_lateness_seconds must be >= 0")
        rule = values.get("liquidation")
        if rule is not None and rule not in LIQUIDATION_RULES:
            raise ValueError(f"liquidation must be one of {list(LIQUIDATION_RULES)}")
        spec = WindowSpec(
            start=start,
            end=end,
            max_lateness_seconds=int(lateness or 0),
            left_time_field=values.get("left_time_field") or "updated_at",
            right_time_field=values.get("right_time_field")
            or values.get("left_time_field")
            or "updated_at",
            liquidation=rule or LIQUIDATION_FINALIZE,
        )
        stored = window_repo.upsert_window(
            conn,
            pair_id,
            {
                "window_start": spec.as_dict()["start"],
                "window_end": spec.as_dict()["end"],
                "max_lateness_seconds": spec.max_lateness_seconds,
                "left_time_field": spec.left_time_field,
                "right_time_field": spec.right_time_field,
                "liquidation": spec.liquidation,
                "auto_slide": values.get("auto_slide"),
            },
            _now(),
        )
        conn.commit()
        return {"pair_id": pair_id, "pair_code": pair["code"], "window": stored}
    finally:
        conn.close()


def list_buffer(
    pair_id: int | None = None, state: str = "buffered", limit: int = 200
) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = window_repo.list_buffer(conn, pair_id=pair_id, state=state, limit=limit)
        pairs = {p["id"]: p for p in pair_repo.list_pairs(conn)}
        for r in rows:
            p = pairs.get(r["pair_id"])
            r["pair_code"] = p["code"] if p else None
            r["pair_title"] = p["title"] if p else None
        return rows
    finally:
        conn.close()


def list_buffer_events(batch_id: int | None = None, pair_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return window_repo.list_events(conn, batch_id=batch_id, pair_id=pair_id)
    finally:
        conn.close()


def audit_summary(batch_id: int) -> dict[str, Any]:
    """Lightweight audit view for a batch (export hooks left for later cuts)."""
    conn = connect()
    try:
        batch = batch_repo.get_batch(conn, batch_id)
        if not batch:
            raise ValueError("batch not found")
        pair = pair_repo.get_pair(conn, batch["pair_id"])
        diffs = batch_repo.list_diffs(conn, batch_id=batch_id)
        buffer_events = window_repo.list_events(conn, batch_id=batch_id)
        active = window_repo.list_buffer(
            conn, pair_id=batch["pair_id"], state="buffered"
        )
        return {
            "batch": batch,
            "pair": pair,
            "diff_count": len(diffs),
            "kinds": {
                "left_only": sum(1 for d in diffs if d["kind"] == "left_only"),
                "right_only": sum(1 for d in diffs if d["kind"] == "right_only"),
                "field_mismatch": sum(1 for d in diffs if d["kind"] == "field_mismatch"),
            },
            "origins": {
                "direct": sum(1 for d in diffs if d["origin"] == "direct"),
                "aligned_after_buffer": sum(
                    1 for d in diffs if d["origin"] == "aligned_after_buffer"
                ),
                "window_expired": sum(1 for d in diffs if d["origin"] == "window_expired"),
            },
            "window": {
                "start": batch.get("window_start"),
                "end": batch.get("window_end"),
                "max_lateness_seconds": batch.get("max_lateness_seconds"),
                "left_time_field": batch.get("left_time_field"),
                "right_time_field": batch.get("right_time_field"),
                "liquidation": batch.get("liquidation"),
                "as_of": batch.get("as_of"),
                "windowed": bool(batch.get("windowed")),
            },
            "counts": {
                "left_rows": batch["left_rows"],
                "right_rows": batch["right_rows"],
                "inside_left": batch["inside_left"],
                "inside_right": batch["inside_right"],
                "outside_window": batch["outside_window"],
                "not_arrived": batch["not_arrived"],
                "matched": batch["matched"],
                "left_only": batch["left_only"],
                "right_only": batch["right_only"],
                "field_mismatch": batch["field_mismatch"],
                "diff_total": batch["diff_total"],
                "buffered": batch["buffered"],
                "buffered_left": batch["buffered_left"],
                "buffered_right": batch["buffered_right"],
                "aligned_after_buffer": batch["aligned_after_buffer"],
                "expired_left": batch["expired_left"],
                "expired_right": batch["expired_right"],
                "expired_total": batch["expired_total"],
                "discarded": batch["discarded"],
                "superseded": batch["superseded"],
            },
            "buffer_events": buffer_events,
            "active_buffer_now": active,
            "export_ready": False,
            "note": "底座仅提供摘要；审计包导出留给后题切口",
        }
    finally:
        conn.close()
