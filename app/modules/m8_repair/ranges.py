"""受影响里程：把每条判定映射到米制区间，并按方向求并集，避免重复累计。"""

from .catalog import LANE_SCOPED, UNASSIGNED


def union(intervals):
    """半开整数米区间 [low, high) 的并集，返回不重叠且有序的区间列表。"""
    merged = []
    for low, high in sorted((low, high) for low, high in intervals if high > low):
        if merged and low <= merged[-1][1]:
            merged[-1][1] = max(high, merged[-1][1])
        else:
            merged.append([low, high])
    return merged


def meters_of(intervals):
    return sum(high - low for low, high in intervals)


def lane_key(issue):
    road, direction = issue.get("road"), issue.get("direction")
    if isinstance(road, str) and road.strip() and direction in ("+", "-"):
        return road, direction
    return UNASSIGNED, None


def _projection_index(result):
    """轨迹行号 -> 投影桩号（含冲突截断点），并记录无桩号点的最近有效锚点。"""
    index, nearest = {}, {}
    groups = {}
    for row in result["projection"]["rows"]:
        groups.setdefault(row["vehicle"], []).append(row)
    for rows in groups.values():
        last = None
        for row in rows:
            if isinstance(row.get("m"), int):
                index[row["line"]] = row["m"]
                last = row["m"]
            elif last is not None:
                nearest[row["line"]] = last
    return index, nearest


def _motion_index(result):
    """轨迹相邻行 (line, end_line) -> 桩号跨度。

    优先取窗口阶段已量化的采信/待核区间；被规则拒绝的段没有窗口产出，
    退而用投影阶段相邻锚点的桩号跨度，保证 PLOW_OFF、WRONG_WAY 等也有可审计里程。
    """
    index = {}
    for row in result["windows"]["rows"]:
        index[(row["line"], row.get("end_line"))] = (row["from_m"], row["to_m"])
    groups = {}
    for row in result["projection"]["rows"]:
        groups.setdefault(row["vehicle"], []).append(row)
    for rows in groups.values():
        for left, right in zip(rows, rows[1:]):
            pair = (left["line"], right["line"])
            if pair in index or left.get("m") is None or right.get("m") is None:
                continue
            low, high = sorted((left["m"], right["m"]))
            if high > low:
                index[pair] = (low, high)
    return index


def _span_from_issue(issue, projected, nearest, motions):
    """从判定证据里提取受影响桩号区间；提取不到返回 None。"""
    code = issue["code"]
    if code in LANE_SCOPED:
        return None
    raw = issue.get("raw")
    # 线路册记录自带责任区间
    if isinstance(raw, dict) and all(isinstance(raw.get(key), int) for key in ("from_m", "to_m")):
        low, high = raw["from_m"], raw["to_m"]
        return (low, high) if high > low else None
    # 窗口阶段已量化的轨迹段（PLOW_OFF、WRONG_WAY、GPS_GAP 等带 end_line）
    end_line = issue.get("end_line")
    if end_line is not None:
        span = motions.get((issue["line"], end_line))
        if span is not None:
            return span
    # 单点投影判定（GPS_DRIFT、CONFLICT_POINT 等）保守记为该桩号起 1 米
    meter = issue.get("m")
    if meter is None:
        meter = projected.get(issue["line"])
    if meter is None:
        # 漂移/冲突点本身无桩号时，借用同一车辆最近的有效锚点
        meter = nearest.get(issue["line"])
    if isinstance(meter, int) and meter >= 0:
        return (meter, meter + 1)
    return None


def build_directions(result):
    """汇总每个方向的责任区间、受影响区间并集与影响里程。"""
    projected, nearest = _projection_index(result)
    motions = _motion_index(result)
    lanes = {}
    for row in result["coverage"]["rows"]:
        key = (row["road"], row["direction"])
        lanes[key] = {"road": row["road"], "direction": row["direction"], "name": row["name"],
                      "responsibility_m": row["to_m"] - row["from_m"],
                      "responsibility_span": [row["from_m"], row["to_m"]],
                      "spans": [], "lane_scoped": False}
    for issue in result["reconcile"]["issues"]:
        key = lane_key(issue)
        bucket = lanes.setdefault(key, {"road": key[0], "direction": key[1], "name": None,
                                        "responsibility_m": 0, "responsibility_span": None,
                                        "spans": [], "lane_scoped": False})
        if issue["code"] in LANE_SCOPED:
            bucket["lane_scoped"] = True
        span = _span_from_issue(issue, projected, nearest, motions)
        if span is not None:
            bucket["spans"].append(span)
    directions = {}
    for key, bucket in lanes.items():
        if bucket["lane_scoped"] and bucket["responsibility_span"] is not None:
            # 仅结构/缺窗类原因按整条责任方向，与桩号证据取并集
            spans = union([tuple(bucket["responsibility_span"])] + bucket["spans"])
        else:
            spans = union(bucket["spans"])
        directions["/".join(str(part) for part in key)] = {
            "road": bucket["road"], "direction": bucket["direction"], "name": bucket["name"],
            "responsibility_m": bucket["responsibility_m"],
            "responsibility_span": bucket["responsibility_span"],
            "impact_spans": spans, "impact_m": meters_of(spans)}
    return directions
