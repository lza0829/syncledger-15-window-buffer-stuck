from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import reconcile_service as svc

router = APIRouter(tags=["windows"])


class WindowUpdate(BaseModel):
    window_start: str
    window_end: str
    max_lateness_seconds: int = 0
    left_time_field: str | None = None
    right_time_field: str | None = None
    liquidation: str = "finalize_side_only"
    auto_slide: bool = False


@router.get("/windows")
def list_windows():
    return svc.list_windows()


@router.get("/pairs/{pair_id}/window")
def get_pair_window(pair_id: int):
    try:
        return svc.get_pair_window(pair_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.put("/pairs/{pair_id}/window")
def update_pair_window(pair_id: int, body: WindowUpdate):
    try:
        return svc.update_pair_window(pair_id, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/buffer")
def list_buffer(pair_id: int | None = None, state: str = "buffered", limit: int = 200):
    return svc.list_buffer(pair_id=pair_id, state=state, limit=limit)


@router.get("/buffer/events")
def list_buffer_events(batch_id: int | None = None, pair_id: int | None = None):
    return svc.list_buffer_events(batch_id=batch_id, pair_id=pair_id)


@router.get("/demo/scenarios")
def demo_scenarios():
    from app.services import demo_service

    return demo_service.list_scenarios()


@router.post("/demo/scenarios/{scenario}/run")
def demo_run(scenario: str, reset: bool = True):
    from app.services import demo_service

    try:
        return demo_service.run_scenario(scenario, reset=reset)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
