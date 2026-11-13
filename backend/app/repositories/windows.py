"""Persistence for reconciliation windows and the side-only buffer."""

from __future__ import annotations

import json
from typing import Any

import sqlite3

ACTIVE_STATES = ("buffered",)
CLOSED_STATES = ("aligned", "finalized", "discarded", "superseded")


def _window(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["auto_slide"] = bool(d.get("auto_slide"))
    return d


def get_window(conn: sqlite3.Connection, pair_id: int) -> dict[str, Any] | None:
    return _window(
        conn.execute("SELECT * FROM reconcile_windows WHERE pair_id=?", (pair_id,)).fetchone()
    )


def upsert_window(conn: sqlite3.Connection, pair_id: int, values: dict[str, Any], updated_at: str) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO reconcile_windows(
            pair_id, window_start, window_end, max_lateness_seconds,
            left_time_field, right_time_field, liquidation, auto_slide, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(pair_id) DO UPDATE SET
            window_start=excluded.window_start,
            window_end=excluded.window_end,
            max_lateness_seconds=excluded.max_lateness_seconds,
            left_time_field=excluded.left_time_field,
            right_time_field=excluded.right_time_field,
            liquidation=excluded.liquidation,
            auto_slide=excluded.auto_slide,
            updated_at=excluded.updated_at
        """,
        (
            pair_id,
            values.get("window_start"),
            values.get("window_end"),
            int(values.get("max_lateness_seconds") or 0),
            values.get("left_time_field"),
            values.get("right_time_field"),
            values.get("liquidation") or "finalize_side_only",
            1 if values.get("auto_slide") else 0,
            updated_at,
        ),
    )
    return get_window(conn, pair_id)  # type: ignore[return-value]


def _buffer_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["align_key"] = json.loads(d.pop("align_key_json"))
    d["row"] = json.loads(d.pop("row_json"))
    return d


def list_buffer(
    conn: sqlite3.Connection,
    *,
    pair_id: int | None = None,
    state: str | None = "buffered",
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reconcile_buffer WHERE 1=1"
    args: list[Any] = []
    if pair_id is not None:
        sql += " AND pair_id=?"
        args.append(pair_id)
    if state is not None and state != "all":
        sql += " AND state=?"
        args.append(state)
    sql += " ORDER BY id ASC LIMIT ?"
    args.append(limit)
    return [_buffer_row(r) for r in conn.execute(sql, args).fetchall()]


def active_buffer_entries(conn: sqlite3.Connection, pair_id: int) -> list[dict[str, Any]]:
    """Entries the engine receives as ``prior_buffer`` (engine-shaped)."""
    out = []
    for r in list_buffer(conn, pair_id=pair_id, state="buffered", limit=1000):
        out.append(
            {
                "buffer_id": r["id"],
                "side": r["side"],
                "align_key": r["align_key"],
                "row": r["row"],
                "event_time": r["event_time"],
                "deadline": r["deadline"],
                "buffered_window_start": r["window_start"],
                "buffered_window_end": r["window_end"],
                "source_batch_id": r["source_batch_id"],
                "state": "buffered",
            }
        )
    return out


def _find_active(
    conn: sqlite3.Connection, pair_id: int, side: str, align_key_json: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM reconcile_buffer
        WHERE pair_id=? AND side=? AND align_key_json=? AND state='buffered'
        ORDER BY id DESC LIMIT 1
        """,
        (pair_id, side, align_key_json),
    ).fetchone()


def buffer_enter(
    conn: sqlite3.Connection,
    *,
    pair_id: int,
    side: str,
    align_key: dict[str, Any],
    row: dict[str, Any],
    event_time: str | None,
    deadline: str | None,
    window_start: str | None,
    window_end: str | None,
    batch_id: int,
    now: str,
) -> tuple[int, bool]:
    """Insert a buffered row, or refresh an identical existing one.

    Returns ``(buffer_id, created)``.  An identical re-seen row keeps its
    original buffering instant so its deadline does not drift.
    """
    align_key_json = json.dumps(align_key, ensure_ascii=False, sort_keys=True)
    row_json = json.dumps(row, ensure_ascii=False, sort_keys=True)
    existing = _find_active(conn, pair_id, side, align_key_json)
    if existing is not None and existing["row_json"] == row_json:
        # Identical re-seen row: keep the original deadline, but re-anchor a
        # ledger event_time that disagrees with the row's real timestamp.
        if event_time is not None and existing["event_time"] != event_time:
            conn.execute(
                "UPDATE reconcile_buffer SET event_time=?, updated_at=? WHERE id=?",
                (event_time, now, existing["id"]),
            )
        else:
            conn.execute(
                "UPDATE reconcile_buffer SET updated_at=? WHERE id=?",
                (now, existing["id"]),
            )
        return int(existing["id"]), False
    if existing is not None:
        # Changed payload: close the old copy as superseded (counted by the
        # engine), then hold the new one.
        conn.execute(
            """
            UPDATE reconcile_buffer SET state='superseded', settled_batch_id=?,
                settled_reason=?, settled_at=?, updated_at=?
            WHERE id=?
            """,
            (batch_id, "fresh_arrival_replaced_buffered_row", now, now, existing["id"]),
        )
    cur = conn.execute(
        """
        INSERT INTO reconcile_buffer(
            pair_id, side, align_key_json, row_json, event_time, deadline,
            window_start, window_end, state, entered_batch_id, source_batch_id,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?, 'buffered', ?, ?, ?, ?)
        """,
        (
            pair_id,
            side,
            align_key_json,
            row_json,
            event_time,
            deadline,
            window_start,
            window_end,
            batch_id,
            batch_id,
            now,
            now,
        ),
    )
    return int(cur.lastrowid), True


def buffer_settle(
    conn: sqlite3.Connection,
    *,
    pair_id: int,
    side: str,
    align_key: dict[str, Any],
    state: str,
    batch_id: int,
    reason: str | None,
    now: str,
    buffer_id: int | None = None,
) -> int | None:
    align_key_json = json.dumps(align_key, ensure_ascii=False, sort_keys=True)
    row = None
    if buffer_id is not None:
        row = conn.execute(
            "SELECT * FROM reconcile_buffer WHERE id=? AND state='buffered'", (buffer_id,)
        ).fetchone()
    if row is None:
        row = _find_active(conn, pair_id, side, align_key_json)
    if row is None:
        return None
    conn.execute(
        """
        UPDATE reconcile_buffer SET state=?, settled_batch_id=?, settled_reason=?,
            settled_at=?, updated_at=?
        WHERE id=?
        """,
        (state, batch_id, reason, now, now, row["id"]),
    )
    return int(row["id"])


def insert_event(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    pair_id: int,
    buffer_id: int | None,
    event_type: str,
    state: str,
    side: str,
    align_key: dict[str, Any],
    row: dict[str, Any] | None,
    event_time: str | None,
    deadline: str | None,
    reason: str | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO reconcile_buffer_events(
            batch_id, pair_id, buffer_id, event_type, state, side,
            align_key_json, row_json, event_time, deadline, reason, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            pair_id,
            buffer_id,
            event_type,
            state,
            side,
            json.dumps(align_key, ensure_ascii=False, sort_keys=True),
            json.dumps(row, ensure_ascii=False, sort_keys=True) if row is not None else None,
            event_time,
            deadline,
            reason,
            created_at,
        ),
    )
    return int(cur.lastrowid)


def list_events(
    conn: sqlite3.Connection, *, batch_id: int | None = None, pair_id: int | None = None, limit: int = 300
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reconcile_buffer_events WHERE 1=1"
    args: list[Any] = []
    if batch_id is not None:
        sql += " AND batch_id=?"
        args.append(batch_id)
    if pair_id is not None:
        sql += " AND pair_id=?"
        args.append(pair_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        d["align_key"] = json.loads(d.pop("align_key_json"))
        d["row"] = json.loads(d.pop("row_json")) if d.get("row_json") else None
        out.append(d)
    return out
