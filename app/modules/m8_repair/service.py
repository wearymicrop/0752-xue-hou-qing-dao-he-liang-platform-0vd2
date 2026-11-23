"""修复工单服务：生成、断点续跑归并、交班视图与修复后重算验证。"""

from datetime import datetime, timezone

from app.codec import digest
from app.errors import DomainError
from app.pipeline import evaluate
from .order import build_order

ORDER_STATE_LABELS = {"OPEN": "待修复", "CONFIRMED": "来源已确认", "REPAIRED": "已修复待核", "VERIFIED": "已复核"}
ITEM_FLOW = ("CONFIRMED", "REPAIRED", "VERIFIED")


def order_id_for(source_sha256, ruleset):
    return digest({"kind": "repair-order", "source_sha256": source_sha256, "ruleset": ruleset})


def _merge_states(order, states):
    """把存储层的条目状态归并回工单，并据此重算工单与方向状态。"""
    for item in order["items"]:
        state, note = states.get(item["id"], ("OPEN", None))
        item["state"] = state
        item["state_label"] = ORDER_STATE_LABELS[state]
        if note:
            item["note"] = note
    by_id = {item["id"]: item for item in order["items"]}
    for direction in order["directions"]:
        lane_states = [by_id[item_id]["state"] for item_id in direction["item_ids"]]
        direction["state"] = "VERIFIED" if lane_states and all(s == "VERIFIED" for s in lane_states) else \
            "REPAIRED" if all(s in ("REPAIRED", "VERIFIED") for s in lane_states) else \
            "CONFIRMED" if all(s != "OPEN" for s in lane_states) else "OPEN"
        direction["state_label"] = ORDER_STATE_LABELS[direction["state"]]
    all_states = [item["state"] for item in order["items"]]
    order["status"] = "VERIFIED" if all_states and all(s == "VERIFIED" for s in all_states) else \
        "REPAIRED" if all(s in ("REPAIRED", "VERIFIED") for s in all_states) else \
        "CONFIRMED" if all(s != "OPEN" for s in all_states) else "OPEN"
    order["status_label"] = ORDER_STATE_LABELS[order["status"]]
    return order


def create_order(ledger, run_id):
    source, snapshot = ledger.read(run_id)
    result = snapshot["result"]
    source_sha256 = snapshot["manifest"]["source_sha256"]
    order_id = order_id_for(source_sha256, result["ruleset"])
    draft = build_order(source, source_sha256, result, order_id)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload, states = ledger.save_repair_order(draft, run_id, created_at)
    return _merge_states(payload, states)


def get_order(ledger, order_id):
    payload, states = ledger.read_repair_order(order_id)
    return _merge_states(payload, states)


def advance_item(ledger, order_id, item_id, action, note=None):
    if action not in ITEM_FLOW:
        raise DomainError("BAD_ACTION", "不支持的条目动作", 422)
    ledger.update_repair_item(order_id, item_id, action, note)
    return get_order(ledger, order_id)


def handover(ledger):
    """交班页面：每个方向的主阻塞原因、涉及里程与修复后重算结论。"""
    orders = ledger.list_repair_orders()
    board = []
    for order in orders:
        _, states = ledger.read_repair_order(order["id"])
        merged = _merge_states(order, states)
        for direction in merged["directions"]:
            if direction["primary_reason"] is None:
                continue
            board.append({
                "order_id": merged["id"], "batch": merged["batch"],
                "direction_id": direction["direction_id"],
                "road": direction["road"], "direction": direction["direction"], "name": direction["name"],
                "state": direction["state"], "state_label": ORDER_STATE_LABELS[direction["state"]],
                "responsibility_m": direction["responsibility_m"],
                "impact_m": direction["impact_m"], "impact_spans": direction["impact_spans"],
                "primary_reason": direction["primary_reason"],
                "recompute": direction["recompute"],
            })
    board.sort(key=lambda row: (row["state"] != "OPEN", -row["impact_m"], row["direction_id"]))
    return {"directions": board, "order_count": len(orders)}


def verify(ledger, order_id, repaired_document):
    """修复未完成不能当已核：全部条目修复后才允许对修复批次重算并逐条验证。"""
    order = get_order(ledger, order_id)
    if any(item["state"] not in ("REPAIRED", "VERIFIED") for item in order["items"]):
        raise DomainError("REPAIR_INCOMPLETE", "仍有条目未修复，不能把结果当作已核", 409)
    repaired = evaluate(repaired_document)
    if digest(repaired_document) == order["source_sha256"]:
        raise DomainError("SOURCE_UNCHANGED", "修复批次与冻结来源完全相同，未见修复", 422)
    lane_state = {(row["road"], row["direction"]): row for row in repaired["reconcile"]["rows"]}
    remaining = [((issue.get("road"), issue.get("direction")), issue["code"])
                 for issue in repaired["reconcile"]["issues"]]
    failures = []
    for item in order["items"]:
        key = (item["road"], item["direction"])
        if item["direction"] is None:
            # 无法归属到方向的条目：该原因码在修复批次中整体消失才算清除
            cleared = not any(code == item["code"] for _, code in remaining)
        else:
            lane = lane_state.get(key)
            cleared = lane is not None and lane["continuous"] and lane["difference_m"] == 0 \
                and (key, item["code"]) not in remaining
        if not cleared:
            failures.append({"item_id": item["id"], "code": item["code"], "direction_id": item["direction_id"]})
    if failures:
        raise DomainError("VERIFY_FAILED", "重算后仍存在未消除的异常，退回修复", 409, failures)
    for item in order["items"]:
        if item["state"] == "REPAIRED":
            ledger.update_repair_item(order_id, item["id"], "VERIFIED",
                                      "修复后重算对平：" + repaired["ruleset"])
    return {"order_id": order_id, "verified": True,
            "repaired_digest": digest(repaired),
            "reconcile_status": repaired["reconcile"]["status"],
            "totals": repaired["reconcile"]["totals"]}
