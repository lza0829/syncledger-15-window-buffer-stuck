from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import reconcile_service as svc

router = APIRouter(tags=["batches"])


class RunBody(BaseModel):
    pair_id: int
    note: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    max_lateness_seconds: int | None = None
    left_time_field: str | None = None
    right_time_field: str | None = None
    liquidation: str | None = None
    as_of: str | None = None
    use_window: bool = True


@router.get("/batches")
def list_batches(limit: int = 50, pair_id: int | None = None):
    return svc.list_batches(limit=limit, pair_id=pair_id)


@router.get("/diffs")
def list_diffs(batch_id: int | None = None, pair_id: int | None = None):
    return svc.list_diffs(batch_id=batch_id, pair_id=pair_id)


@router.post("/reconcile")
def run_reconcile(body: RunBody):
    try:
        return svc.run_reconcile(
            body.pair_id,
            body.note,
            window_start=body.window_start,
            window_end=body.window_end,
            max_lateness_seconds=body.max_lateness_seconds,
            left_time_field=body.left_time_field,
            right_time_field=body.right_time_field,
            liquidation=body.liquidation,
            as_of=body.as_of,
            use_window=body.use_window,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
