"""Configurable reconciliation time window with an auditable side-only buffer.

Semantics
---------

* A row takes part in a pass only when its event / update timestamp falls in the
  half-open window ``[start, end)`` *and* it has arrived by the pass's
  observation clock (``as_of``; defaults to the window's upper edge).  Rows
  outside the window are reported as ``outside_window`` and are not compared;
  rows inside the window but timestamped after the clock are reported as
  ``not_arrived`` for this pass.
* Inside the window, when one side has arrived and its counterpart has not, the
  row enters the auditable **buffer** (state ``buffered``).  It is *not*
  finalised as ``left_only`` / ``right_only``, and buffered rows are never
  included in the terminal difference total.
* If the counterpart arrives while still inside the window, the buffered row is
  aligned — the pair either matches, or it moves to field comparison.
* A row may stay buffered only while the window is still open plus its
  ``max_lateness_seconds`` grace: ``buffer_deadline = window.end + lateness``.
  Once the clock reaches that deadline — or the window slides past the row's
  event time — the row is **over-window** and must leave the buffer: it is
  finalised as a side-only terminal difference or explicitly discarded, and
  counted in either case.  It can never linger in the buffer while vanishing
  from the pass totals.
* Every buffer entry that leaves the buffer (aligned / finalised / discarded /
  superseded) is returned in ``settled`` with its ``buffer_id`` so persistence
  can close the matching row.
* A re-seen row identical to the copy already held is a continuation, not a
  new arrival: the original buffering instant and deadline are preserved.  Only
  a *changed* row supersedes the held copy, and that is counted and audited.

The engine is a pure function over rows; persistence lives in the service.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

DIFF_LEFT_ONLY = "left_only"
DIFF_RIGHT_ONLY = "right_only"
DIFF_FIELD_MISMATCH = "field_mismatch"
DIFF_MATCHED = "matched"

ORIGIN_DIRECT = "direct"
ORIGIN_ALIGNED_AFTER_BUFFER = "aligned_after_buffer"
ORIGIN_WINDOW_EXPIRED = "window_expired"

STATE_BUFFERED = "buffered"
STATE_ALIGNED = "aligned"
STATE_FINALIZED = "finalized"
STATE_DISCARDED = "discarded"
STATE_SUPERSEDED = "superseded"
STATE_OUTSIDE = "outside_window"
STATE_NOT_ARRIVED = "not_arrived"

LIQUIDATION_FINALIZE = "finalize_side_only"
LIQUIDATION_DISCARD = "discard"
LIQUIDATION_RULES = (LIQUIDATION_FINALIZE, LIQUIDATION_DISCARD)

# Column names tried, in order, for a row's event / update timestamp.  A
# configured ``time_field`` always wins when the row actually carries it.
TIMESTAMP_FIELDS = ("updated_at", "event_time", "paid_at", "booked_at", "occurred_at", "at")

COUNT_KEYS = (
    "left_rows",
    "right_rows",
    "inside_left",
    "inside_right",
    "outside_left",
    "outside_right",
    "outside_window",
    "not_arrived_left",
    "not_arrived_right",
    "not_arrived",
    "matched",
    "field_mismatch",
    "left_only",
    "right_only",
    "diff_total",
    "buffered",
    "buffered_left",
    "buffered_right",
    "aligned_after_buffer",
    "expired_left",
    "expired_right",
    "expired_total",
    "discarded",
    "superseded",
    "liquidated",
)


# --------------------------------------------------------------------------- #
# timestamps / keys
# --------------------------------------------------------------------------- #
def parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def key_str(align_key: dict[str, Any]) -> str:
    return json.dumps(align_key, ensure_ascii=False, sort_keys=True)


def align_key_obj(key: tuple, align_keys: list[str]) -> dict[str, Any]:
    return {name: key[i] for i, name in enumerate(align_keys)}


def shorten(align_keys: list[str], values: Any) -> dict[str, Any]:
    return {name: values.get(name) for name in align_keys}


def row_key(row: dict[str, Any], align_keys: list[str]) -> str:
    return key_str(shorten(align_keys, row))


def row_time(row: dict[str, Any], time_field: str | None) -> datetime | None:
    """Resolve a row's event / update timestamp."""
    if time_field and time_field in row:
        dt = parse_ts(row.get(time_field))
        if dt is not None:
            return dt
    for name in TIMESTAMP_FIELDS:
        if name in row:
            dt = parse_ts(row.get(name))
            if dt is not None:
                return dt
    return None


def timestamp_bounds(
    rows: list[dict[str, Any]], time_field: str | None
) -> tuple[datetime | None, datetime | None]:
    stamps = [dt for dt in (row_time(r, time_field) for r in rows) if dt is not None]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def first_timestamp(rows: list[dict[str, Any]], time_field: str | None) -> datetime | None:
    return timestamp_bounds(rows, time_field)[0]


def last_timestamp(rows: list[dict[str, Any]], time_field: str | None) -> datetime | None:
    return timestamp_bounds(rows, time_field)[1]


# --------------------------------------------------------------------------- #
# window spec
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WindowSpec:
    """Half-open window ``[start, end)`` plus the buffer's allowed lateness."""

    start: datetime
    end: datetime
    max_lateness_seconds: int = 0
    left_time_field: str | None = None
    right_time_field: str | None = None
    liquidation: str = LIQUIDATION_FINALIZE
    treat_missing_as_inside: bool = False

    def __post_init__(self) -> None:
        if self.max_lateness_seconds < 0:
            raise ValueError("max_lateness_seconds must be >= 0")
        if self.end <= self.start:
            raise ValueError("window end must be after window start")
        if self.liquidation not in LIQUIDATION_RULES:
            raise ValueError(
                f"unknown liquidation rule {self.liquidation!r}; "
                f"expected one of {sorted(LIQUIDATION_RULES)}"
            )

    def contains(self, dt: datetime) -> bool:
        return self.start <= dt < self.end

    @property
    def buffer_deadline(self) -> datetime:
        """Instant when rows buffered under this window must leave the buffer.

        The window closes at ``end``; ``max_lateness_seconds`` is the grace on
        top of that close so a counterpart may still arrive and align the row.
        """
        return self.end + timedelta(seconds=self.max_lateness_seconds)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": fmt_ts(self.start),
            "end": fmt_ts(self.end),
            "width_seconds": int((self.end - self.start).total_seconds()),
            "max_lateness_seconds": self.max_lateness_seconds,
            "buffer_deadline": fmt_ts(self.buffer_deadline),
            "left_time_field": self.left_time_field,
            "right_time_field": self.right_time_field,
            "liquidation": self.liquidation,
            "treat_missing_as_inside": self.treat_missing_as_inside,
        }


def _blank_counts() -> dict[str, int]:
    return {key: 0 for key in COUNT_KEYS}


def _new_entry(
    side: str,
    align_key: dict[str, Any],
    row: dict[str, Any],
    window: WindowSpec,
    clock: datetime,
    source_batch_id: int | None,
    source_buffer_id: int | None = None,
) -> dict[str, Any]:
    time_field = window.left_time_field if side == "left" else window.right_time_field
    event_time = row_time(row, time_field) or clock
    return {
        "buffer_id": source_buffer_id,
        "side": side,
        "align_key": align_key,
        "row": row,
        "event_time": fmt_ts(event_time),
        "deadline": fmt_ts(window.buffer_deadline),
        "buffered_window_start": fmt_ts(window.start),
        "buffered_window_end": fmt_ts(window.end),
        "lateness_seconds": window.max_lateness_seconds,
        "source_batch_id": source_batch_id,
        "state": STATE_BUFFERED,
    }


def _compare(
    left: dict[str, Any],
    right: dict[str, Any],
    compare_fields: list[str],
    align_keys: list[str],
    origin: str,
) -> dict[str, Any]:
    mismatched = [f for f in compare_fields if left.get(f) != right.get(f)]
    return {
        "kind": DIFF_FIELD_MISMATCH if mismatched else DIFF_MATCHED,
        "align_key": shorten(align_keys, left),
        "left": left,
        "right": right,
        "mismatched_fields": mismatched,
        "origin": origin,
    }


def _settle(entry: dict[str, Any], state: str, settled: list[dict[str, Any]], reason: str) -> None:
    entry["state"] = state
    settled.append(
        {
            "buffer_id": entry.get("buffer_id"),
            "side": entry["side"],
            "align_key": entry["align_key"],
            "state": state,
            "reason": reason,
        }
    )


def _liquidate(
    entry: dict[str, Any],
    window: WindowSpec,
    counts: dict[str, int],
    diffs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    settled: list[dict[str, Any]],
    *,
    reason: str,
) -> None:
    side = entry["side"]
    counts["liquidated"] += 1
    if window.liquidation == LIQUIDATION_DISCARD:
        counts["discarded"] += 1
        _settle(entry, STATE_DISCARDED, settled, reason)
        events.append(
            {
                "type": "buffer_discarded",
                "state": STATE_DISCARDED,
                "side": side,
                "align_key": entry["align_key"],
                "event_time": entry["event_time"],
                "deadline": entry["deadline"],
                "reason": reason,
            }
        )
        return

    kind = DIFF_LEFT_ONLY if side == "left" else DIFF_RIGHT_ONLY
    if side == "left":
        counts["left_only"] += 1
        counts["expired_left"] += 1
    else:
        counts["right_only"] += 1
        counts["expired_right"] += 1
    diffs.append(
        {
            "kind": kind,
            "align_key": entry["align_key"],
            "left": entry["row"] if side == "left" else None,
            "right": entry["row"] if side == "right" else None,
            "mismatched_fields": [],
            "origin": ORIGIN_WINDOW_EXPIRED,
            "expired_from_buffer": True,
            "event_time": entry["event_time"],
            "deadline": entry["deadline"],
        }
    )
    _settle(entry, STATE_FINALIZED, settled, reason)
    events.append(
        {
            "type": "buffer_finalized",
            "state": STATE_FINALIZED,
            "side": side,
            "align_key": entry["align_key"],
            "event_time": entry["event_time"],
            "deadline": entry["deadline"],
            "kind": kind,
            "reason": reason,
        }
    )


def _over_window_reason(
    entry: dict[str, Any], window: WindowSpec, clock: datetime
) -> str | None:
    deadline = parse_ts(entry.get("deadline"))
    if deadline is not None and clock >= deadline:
        return "buffer_deadline_elapsed"
    event_time = parse_ts(entry.get("event_time"))
    # The window slid forward past the row: it can no longer be aligned inside
    # this window, so it must be settled by the same over-window rule.
    if event_time is not None and event_time < window.start:
        return "window_slid_past_event"
    return None


# --------------------------------------------------------------------------- #
# main pass
# --------------------------------------------------------------------------- #
def run_windowed(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    prior_buffer: list[dict[str, Any]] | None,
    *,
    align_keys: list[str],
    compare_fields: list[str],
    window: WindowSpec,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Run one windowed reconciliation pass.

    ``prior_buffer`` holds active buffer entries from previous passes (each may
    carry a ``buffer_id`` for persistence).  ``as_of`` is the observation clock
    and defaults to the window's upper edge.
    """
    clock = as_of or window.end
    counts = _blank_counts()
    counts["left_rows"] = len(left_rows)
    counts["right_rows"] = len(right_rows)
    diffs: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    settled: list[dict[str, Any]] = []

    # --- gate: window membership + arrival by the observation clock ---------
    left_inside: list[dict[str, Any]] = []
    right_inside: list[dict[str, Any]] = []
    sides = (
        ("left", left_rows, window.left_time_field, left_inside, "not_arrived_left"),
        ("right", right_rows, window.right_time_field, right_inside, "not_arrived_right"),
    )
    for side, rows, field, inside, not_arrived_key in sides:
        for row in rows:
            dt = row_time(row, field)
            if dt is None and window.treat_missing_as_inside:
                inside.append(row)
                continue
            if dt is None or not window.contains(dt):
                counts[f"outside_{side}"] += 1
                events.append(
                    {
                        "type": "outside_window",
                        "state": STATE_OUTSIDE,
                        "side": side,
                        "align_key": shorten(align_keys, row),
                        "event_time": fmt_ts(dt),
                        "reason": "timestamp_missing" if dt is None else "timestamp_outside_window",
                    }
                )
            elif dt > clock:
                counts[not_arrived_key] += 1
                events.append(
                    {
                        "type": "not_arrived",
                        "state": STATE_NOT_ARRIVED,
                        "side": side,
                        "align_key": shorten(align_keys, row),
                        "event_time": fmt_ts(dt),
                        "reason": "timestamp_after_observation_clock",
                    }
                )
            else:
                inside.append(row)

    counts["inside_left"] = len(left_inside)
    counts["inside_right"] = len(right_inside)
    counts["outside_window"] = counts["outside_left"] + counts["outside_right"]
    counts["not_arrived"] = counts["not_arrived_left"] + counts["not_arrived_right"]

    left_idx = {row_key(r, align_keys): r for r in left_inside}
    right_idx = {row_key(r, align_keys): r for r in right_inside}

    # --- carry the buffer forward, indexed by align key ---------------------
    # Nothing leaves the buffer here: a carried row may still be aligned by a
    # counterpart arriving in this pass.  Rows that stay single-sided are
    # settled by the over-window rule in the final step.
    carried_left: dict[str, dict[str, Any]] = {}
    carried_right: dict[str, dict[str, Any]] = {}
    for entry in prior_buffer or []:
        key = key_str(entry["align_key"])
        entry = dict(entry)
        # Carried rows were buffered in an earlier pass, so they are eligible
        # for settlement in this one.
        entry.pop("fresh_this_pass", None)
        target = carried_left if entry["side"] == "left" else carried_right
        target[key] = entry

    pending_left: dict[str, dict[str, Any]] = {}
    pending_right: dict[str, dict[str, Any]] = {}

    def order(key: str) -> tuple[str, ...]:
        return tuple(str(v) for v in json.loads(key).values())

    def hold(
        side: str, key: str, align_key: dict[str, Any], row: dict[str, Any], *, is_fresh: bool
    ) -> dict[str, Any]:
        entry = _new_entry(side, align_key, row, window, clock, None)
        # First observation of this side-only row: it must buffer, never be
        # finalised as side-only in the same pass.
        entry["fresh_this_pass"] = is_fresh
        (pending_left if side == "left" else pending_right)[key] = entry
        events.append(
            {
                "type": "buffer_entered",
                "state": STATE_BUFFERED,
                "side": side,
                "align_key": align_key,
                "row": row,
                "event_time": entry["event_time"],
                "deadline": entry["deadline"],
                "reason": "counterpart_not_arrived",
            }
        )
        return entry

    all_keys = sorted(
        set(left_idx) | set(right_idx) | set(carried_left) | set(carried_right), key=order
    )

    for key in all_keys:
        fresh_left = left_idx.get(key)
        fresh_right = right_idx.get(key)
        held_left = carried_left.pop(key, None)
        held_right = carried_right.pop(key, None)
        anchor = fresh_left or fresh_right or (held_left or held_right)["row"]
        align_key = shorten(align_keys, anchor)

        # A *changed* re-arrival replaces the held copy; the superseded copy is
        # audited and counted, never silently dropped.  An identical re-seen
        # row is a continuation and keeps its original deadline.
        if fresh_left is not None and held_left is not None and fresh_left != held_left["row"]:
            counts["superseded"] += 1
            counts["liquidated"] += 1
            _settle(held_left, STATE_SUPERSEDED, settled, "fresh_arrival_replaced_buffered_row")
            events.append(
                {
                    "type": "buffer_superseded",
                    "state": STATE_SUPERSEDED,
                    "side": "left",
                    "align_key": held_left["align_key"],
                    "event_time": held_left["event_time"],
                    "reason": "fresh_arrival_replaced_buffered_row",
                }
            )
            held_left = None
        if fresh_right is not None and held_right is not None and fresh_right != held_right["row"]:
            counts["superseded"] += 1
            counts["liquidated"] += 1
            _settle(held_right, STATE_SUPERSEDED, settled, "fresh_arrival_replaced_buffered_row")
            events.append(
                {
                    "type": "buffer_superseded",
                    "state": STATE_SUPERSEDED,
                    "side": "right",
                    "align_key": held_right["align_key"],
                    "event_time": held_right["event_time"],
                    "reason": "fresh_arrival_replaced_buffered_row",
                }
            )
            held_right = None

        left_obj = fresh_left if fresh_left is not None else (held_left["row"] if held_left else None)
        right_obj = fresh_right if fresh_right is not None else (held_right["row"] if held_right else None)
        aligned_from_buffer = bool(held_left or held_right)

        if left_obj is not None and right_obj is not None:
            origin = ORIGIN_ALIGNED_AFTER_BUFFER if aligned_from_buffer else ORIGIN_DIRECT
            result = _compare(left_obj, right_obj, compare_fields, align_keys, origin)
            if result["kind"] == DIFF_MATCHED:
                counts["matched"] += 1
            else:
                counts["field_mismatch"] += 1
                diffs.append(result)
            if aligned_from_buffer:
                counts["aligned_after_buffer"] += 1
                for held in (held_left, held_right):
                    if held is not None:
                        _settle(held, STATE_ALIGNED, settled, "counterpart_arrived_within_window")
                events.append(
                    {
                        "type": "aligned_after_buffer",
                        "state": STATE_ALIGNED,
                        "side": "left" if held_left else "right",
                        "align_key": align_key,
                        "kind": result["kind"],
                        "mismatched_fields": result["mismatched_fields"],
                        "event_time": (held_left or held_right)["event_time"],
                        "deadline": (held_left or held_right)["deadline"],
                        "reason": "counterpart_arrived_within_window",
                    }
                )
            continue

        if left_obj is not None:
            if held_left is not None:
                entry = held_left
                pending_left[key] = entry
            else:
                entry = hold("left", key, align_key, left_obj, is_fresh=True)
            if fresh_left is not None and held_left is not None:
                events.append(
                    {
                        "type": "buffer_continued",
                        "state": STATE_BUFFERED,
                        "side": "left",
                        "align_key": align_key,
                        "event_time": entry["event_time"],
                        "deadline": entry["deadline"],
                        "reason": "identical_rearrival",
                    }
                )
        if right_obj is not None:
            if held_right is not None:
                entry = held_right
                pending_right[key] = entry
            else:
                entry = hold("right", key, align_key, right_obj, is_fresh=True)
            if fresh_right is not None and held_right is not None:
                events.append(
                    {
                        "type": "buffer_continued",
                        "state": STATE_BUFFERED,
                        "side": "right",
                        "align_key": align_key,
                        "event_time": entry["event_time"],
                        "deadline": entry["deadline"],
                        "reason": "identical_rearrival",
                    }
                )

    # --- settle over-window buffered rows; never leave them uncounted -------
    # A row that only entered the buffer in this pass keeps its pending status:
    # the first observation of a side-only row buffers it, and a *later*
    # observation settles it.  Carried rows (and rows held while the clock
    # advanced) are subject to the over-window rule here.
    for pending in (pending_left, pending_right):
        for key, entry in list(pending.items()):
            if entry.get("fresh_this_pass"):
                continue
            reason = _over_window_reason(entry, window, clock)
            if reason is None:
                continue
            pending.pop(key)
            _liquidate(entry, window, counts, diffs, events, settled, reason=reason)

    buffer = list(pending_left.values()) + list(pending_right.values())
    buffer.sort(key=lambda e: (order(key_str(e["align_key"])), e["side"]))
    counts["buffered"] = len(buffer)
    counts["buffered_left"] = len(pending_left)
    counts["buffered_right"] = len(pending_right)
    counts["expired_total"] = counts["expired_left"] + counts["expired_right"]
    counts["diff_total"] = counts["left_only"] + counts["right_only"] + counts["field_mismatch"]

    return {
        "counts": counts,
        "diffs": diffs,
        "buffer": buffer,
        "settled": settled,
        "events": events,
        "window": window.as_dict(),
        "as_of": fmt_ts(clock),
    }
