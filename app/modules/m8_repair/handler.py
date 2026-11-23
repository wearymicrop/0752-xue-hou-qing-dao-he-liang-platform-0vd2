from fastapi import APIRouter, Request

from app.errors import DomainError
from app.source import choose
from . import service

router = APIRouter(prefix="/api/repair")


@router.get("/handover")
def handover_board(request: Request):
    return service.handover(request.app.state.ledger)


@router.post("/orders")
def create_order(body: dict, request: Request):
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise DomainError("RUN_REQUIRED", "请先归档一次核量运行再生成工单", 422)
    return service.create_order(request.app.state.ledger, run_id)


@router.get("/orders/{order_id}")
def read_order(order_id: str, request: Request):
    return service.get_order(request.app.state.ledger, order_id)


@router.post("/orders/{order_id}/items/{item_id}/transition")
def transition(order_id: str, item_id: str, body: dict, request: Request):
    action = body.get("action")
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise DomainError("BAD_NOTE", "备注须为文本", 422)
    return service.advance_item(request.app.state.ledger, order_id, item_id, action, note)


@router.post("/orders/{order_id}/verify")
def verify(order_id: str, body: dict, request: Request):
    return service.verify(request.app.state.ledger, order_id, choose(body))
