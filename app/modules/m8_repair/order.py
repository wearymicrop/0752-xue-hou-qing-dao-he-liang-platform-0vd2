"""复核工单构建：把对账判定按方向与原因聚合成可独立追溯的修复条目。"""

from app.codec import digest
from .catalog import CATEGORY, CATEGORY_ORDER, LANE_SCOPED, RECOMPUTE_STAGE, STAGE_LABELS
from .ranges import (_motion_index, _projection_index, _span_from_issue,
                     build_directions, lane_key, meters_of, union)


def _direction_id(key):
    return "/".join(str(part) for part in key)


def _impact_for(issues, direction, projected, nearest, motions):
    """条目级影响里程：本原因全部证据区间的并集，结构/缺窗类扩到整条责任方向。"""
    spans = []
    lane_scoped = False
    for issue in issues:
        span = _span_from_issue(issue, projected, nearest, motions)
        if span is not None:
            spans.append(span)
        if issue["code"] in LANE_SCOPED:
            lane_scoped = True
    if lane_scoped and direction["responsibility_span"] is not None:
        spans.append(tuple(direction["responsibility_span"]))
    return meters_of(union(spans))


def _make_items(result, directions, projected, nearest, motions):
    grouped = {}
    for issue in result["reconcile"]["issues"]:
        grouped.setdefault((lane_key(issue), issue["code"]), []).append(issue)
    items = []
    for (key, code), issues in sorted(grouped.items(),
                                      key=lambda entry: (str(entry[0][0][0]), entry[0][0][1] is None,
                                                         entry[0][0][1] or "", entry[0][1])):
        direction = directions[_direction_id(key)]
        category = CATEGORY.get(code, "track")
        evidence = [{"table": issue["table"], "line": issue["line"],
                     **({"end_line": issue["end_line"]} if issue.get("end_line") else {}),
                     "code": code, "detail": issue["detail"],
                     **({"raw": issue["raw"]} if "raw" in issue else {})}
                    for issue in sorted(issues, key=lambda issue: (issue["table"], issue["line"]))]
        item_id = digest({"direction": _direction_id(key), "code": code,
                          "evidence": [(row["table"], row["line"]) for row in evidence]})
        items.append({
            "id": item_id,
            "road": key[0], "direction": key[1],
            "direction_id": _direction_id(key),
            "code": code, "category": category,
            "detail": evidence[0]["detail"],
            "impact_m": _impact_for(issues, direction, projected, nearest, motions),
            "recompute_stage": RECOMPUTE_STAGE[category],
            "recompute_note": STAGE_LABELS[RECOMPUTE_STAGE[category]],
            "evidence_count": len(evidence),
            "evidence": evidence,
            "state": "OPEN",
        })
    return items


def _primary_reason(items):
    if not items:
        return None
    chosen = sorted(items, key=lambda item: (CATEGORY_ORDER[item["category"]],
                                             -item["impact_m"], item["code"], item["direction_id"]))[0]
    return {"code": chosen["code"], "category": chosen["category"],
            "detail": chosen["detail"], "direction_id": chosen["direction_id"],
            "impact_m": chosen["impact_m"]}


def build_order(source, source_sha256, result, order_id):
    """纯函数：由冻结来源与核量结果生成工单草案，状态字段交给存储层归并。"""
    directions = build_directions(result)
    projected, nearest = _projection_index(result)
    motions = _motion_index(result)
    items = _make_items(result, directions, projected, nearest, motions)
    by_direction = {}
    for item in items:
        by_direction.setdefault(item["direction_id"], []).append(item)
    handover = []
    for direction_id, direction in sorted(directions.items()):
        lane_items = by_direction.get(direction_id, [])
        if not lane_items:
            continue
        handover.append({
            "direction_id": direction_id,
            "road": direction["road"], "direction": direction["direction"], "name": direction["name"],
            "responsibility_m": direction["responsibility_m"],
            "impact_m": direction["impact_m"],
            "impact_spans": direction["impact_spans"],
            "primary_reason": _primary_reason(lane_items),
            "item_ids": [item["id"] for item in lane_items],
            "recompute": sorted({item["recompute_note"] for item in lane_items}),
        })
    reasons = dict(sorted(result["reconcile"]["reasons"].items()))
    return {
        "id": order_id,
        "batch": source["batch"],
        "at": source["at"],
        "ruleset": result["ruleset"],
        "source_sha256": source_sha256,
        "status": "OPEN",
        "reasons": reasons,
        "totals": {
            "directions": len(handover),
            "items": len(items),
            "evidence": sum(item["evidence_count"] for item in items),
            "impact_m": sum(direction["impact_m"] for direction in handover),
        },
        "directions": handover,
        "items": items,
    }
