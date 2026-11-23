"""修复原因目录：原因码到阻塞类别、修复入口与重算环节的确定性映射。"""

# 阻塞类别决定主阻塞原因排序：结构问题先于轨迹，轨迹先于窗口。
STRUCTURAL = "structural"
TRACK = "track"
WINDOW = "window"

CATEGORY = {
    # 线路册：方向定义本身不成立，整条方向无法核量
    "ROAD_SCHEMA": STRUCTURAL,
    "ROAD_CONFLICT": STRUCTURAL,
    "ROAD_DUPLICATE": STRUCTURAL,
    # 车辆轨迹：重复、冲突、漂移与作业状态
    "POINT_SCHEMA": TRACK,
    "POINT_DUPLICATE": TRACK,
    "POINT_CONFLICT": TRACK,
    "CONFLICT_POINT": TRACK,
    "OUT_OF_ORDER": TRACK,
    "GPS_DRIFT": TRACK,
    "WRONG_WAY": TRACK,
    "STATIONARY": TRACK,
    "NO_ANCHOR": TRACK,
    "PLOW_OFF": TRACK,
    "GPS_GAP": TRACK,
    "UNKNOWN_LANE": TRACK,
    # 作业窗口：窗口缺失或不成立，以及窗口外轨迹
    "MISSING_WINDOW": WINDOW,
    "WINDOW_SCHEMA": WINDOW,
    "OUTSIDE_WINDOW": WINDOW,
    "LANE_CHANGE": WINDOW,
}

# 同类别内的确定性次序，未列出的原因按原因码兜底。
CATEGORY_ORDER = {STRUCTURAL: 0, TRACK: 1, WINDOW: 2}

# 修复后必须重算的流水线环节（含其下游）。
RECOMPUTE_STAGE = {
    STRUCTURAL: "routes",
    TRACK: "tracks",
    WINDOW: "windows",
}

STAGE_LABELS = {
    "routes": "线路册及全部下游",
    "tracks": "车辆轨迹、里程投影、清扫窗口、覆盖合并与对账",
    "windows": "清扫窗口、覆盖合并与对账",
}

# 结构类与缺窗类原因没有可量化的桩号区间，按整条责任方向计影响里程。
LANE_SCOPED = {"ROAD_SCHEMA", "ROAD_CONFLICT", "ROAD_DUPLICATE", "MISSING_WINDOW"}

UNASSIGNED = "__unassigned__"
