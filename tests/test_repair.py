from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.m8_repair.catalog import CATEGORY
from app.modules.m8_repair.ranges import meters_of, union
from app.source import load_sample


def _start(client):
    run = client.post("/api/runs", json={"sample": "dirty"}).json()
    return run, client.post("/api/repair/orders", json={"run_id": run["id"]}).json()


def test_order_groups_by_direction_and_reason_with_traceable_evidence(client):
    _, order = _start(client)
    # 每个条目都可独立追溯到表名、数据行号与原值
    for item in order["items"]:
        assert item["evidence_count"] == len(item["evidence"])
        assert item["evidence"] and all(ev["table"] and isinstance(ev["line"], int) for ev in item["evidence"])
        assert item["category"] == CATEGORY[item["code"]]
        assert item["recompute_stage"] in ("routes", "tracks", "windows")
    codes = {item["code"] for item in order["items"]}
    # 重复轨迹、冲突线路、缺窗口三类异常同时在单中
    assert {"POINT_DUPLICATE", "ROAD_DUPLICATE", "MISSING_WINDOW"} <= codes
    # 同方向同原因只生成一个条目（S07 两条 POINT_CONFLICT 合并）
    s07_conflicts = [i for i in order["items"]
                     if i["direction_id"] == "S07/+" and i["code"] == "POINT_CONFLICT"]
    assert len(s07_conflicts) == 1 and s07_conflicts[0]["evidence_count"] == 2


def test_impact_mileage_uses_union_without_double_counting():
    assert union([(0, 100), (50, 150), (200, 220)]) == [[0, 150], [200, 220]]
    assert meters_of(union([(0, 100), (50, 150)])) == 150


def test_direction_impact_and_primary_reason_are_deterministic(client):
    _, order = _start(client)
    by_id = {d["direction_id"]: d for d in order["directions"]}
    s01 = by_id["S01/+"]
    # 结构类原因优先成为主阻塞原因，影响里程覆盖整条责任方向
    assert s01["primary_reason"]["code"] == "ROAD_DUPLICATE"
    assert s01["primary_reason"]["category"] == "structural"
    assert s01["impact_m"] == 1000 and s01["impact_spans"] == [[0, 1000]]
    # 无异常方向不出现在交班视图
    assert "S03/+" not in by_id
    # 缺窗口方向按责任全里程计影响
    assert by_id["S05/+"]["primary_reason"]["code"] == "MISSING_WINDOW"
    assert by_id["S05/+"]["impact_m"] == 500
    # 轨迹类原因取投影/窗口证据里程，不误扩整条方向；同方向多原因里程取并集
    s01_reverse = by_id["S01/-"]
    assert s01_reverse["primary_reason"]["category"] == "track"
    assert 0 < s01_reverse["impact_m"] < s01_reverse["responsibility_m"]
    drift = next(i for i in order["items"] if i["code"] == "GPS_DRIFT")
    assert drift["impact_m"] >= 1


def test_unassigned_structural_issues_remain_traceable(client):
    _, order = _start(client)
    stray = next(d for d in order["directions"] if d["direction_id"] == "__unassigned__/None")
    assert stray["primary_reason"]["code"] == "ROAD_SCHEMA"
    item = next(i for i in order["items"] if i["code"] == "ROAD_SCHEMA")
    # 无法归属方向的原始行仍逐行保留，不丢弃记录
    assert item["direction"] is None and item["evidence"][0]["table"] == "roads"
    assert all("raw" in ev for ev in item["evidence"])


def test_order_id_is_content_addressed_and_rerun_is_idempotent(client):
    run, first = _start(client)
    second = client.post("/api/repair/orders", json={"run_id": run["id"]}).json()
    assert first["id"] == second["id"]
    assert [i["id"] for i in first["items"]] == [i["id"] for i in second["items"]]
    assert client.get("/api/repair/handover").json()["order_count"] == 1


def test_resume_keeps_confirmed_sources_and_never_overwrites(client):
    run, order = _start(client)
    oid = order["id"]
    target = order["items"][0]
    client.post(f"/api/repair/orders/{oid}/items/{target['id']}/transition",
                json={"action": "CONFIRMED", "note": "已核对原始行"})
    # 同一批异常重跑：工单不重复，已确认状态与备注保留
    rerun = client.post("/api/repair/orders", json={"run_id": run["id"]}).json()
    saved = next(item for item in rerun["items"] if item["id"] == target["id"])
    assert saved["state"] == "CONFIRMED" and saved["note"] == "已核对原始行"
    # 状态只能前进，确认后不允许回退
    response = client.post(f"/api/repair/orders/{oid}/items/{target['id']}/transition",
                           json={"action": "REPAIRED"})
    assert response.status_code == 200
    response = client.post(f"/api/repair/orders/{oid}/items/{target['id']}/transition",
                           json={"action": "CONFIRMED"})
    assert response.status_code == 409 and response.json()["code"] == "INVALID_TRANSITION"


def test_unfinished_repair_cannot_be_treated_as_verified(client):
    _, order = _start(client)
    clean = load_sample("clean")
    response = client.post(f"/api/repair/orders/{order['id']}/verify", json={"source": clean})
    assert response.status_code == 409 and response.json()["code"] == "REPAIR_INCOMPLETE"


def _repair_all(client, order):
    for item in order["items"]:
        for action in ("CONFIRMED", "REPAIRED"):
            assert client.post(
                f"/api/repair/orders/{order['id']}/items/{item['id']}/transition",
                json={"action": action}).status_code == 200


def test_verify_requires_actual_change_and_recomputes(client):
    _, order = _start(client)
    _repair_all(client, order)
    # 来源未改动不能冒充修复
    unchanged = client.post(f"/api/repair/orders/{order['id']}/verify",
                            json={"source": load_sample("dirty")})
    assert unchanged.status_code == 422 and unchanged.json()["code"] == "SOURCE_UNCHANGED"
    # 修复批次重算对平后才置为已复核
    passed = client.post(f"/api/repair/orders/{order['id']}/verify",
                         json={"source": load_sample("clean")})
    assert passed.status_code == 200 and passed.json()["verified"] is True
    assert passed.json()["reconcile_status"] == "MATCHED"
    final = client.get(f"/api/repair/orders/{order['id']}").json()
    assert final["status"] == "VERIFIED"
    assert all(item["state"] == "VERIFIED" for item in final["items"])


def test_verify_rejects_when_exceptions_remain(client, dirty):
    _, order = _start(client)
    _repair_all(client, order)
    # 只修掉线路册的重复与非法定义，保留轨迹与窗口异常
    half = {**dirty, "roads": dirty["roads"][:8]}
    response = client.post(f"/api/repair/orders/{order['id']}/verify", json={"source": half})
    assert response.status_code == 409 and response.json()["code"] == "VERIFY_FAILED"
    # 退回时逐条列出未消除的条目，支持继续派单
    assert response.json()["details"]
    assert all({"item_id", "code", "direction_id"} <= set(row) for row in response.json()["details"])


def test_handover_board_shows_blocker_mileage_and_recompute(client):
    _, order = _start(client)
    board = client.get("/api/repair/handover").json()
    assert board["order_count"] == 1 and board["directions"]
    first = board["directions"][0]
    assert {"order_id", "direction_id", "primary_reason", "impact_m", "impact_spans",
            "recompute", "state_label"} <= set(first)
    assert first["primary_reason"]["code"]
    # 待修复方向排在前面
    assert all(row["state"] != "VERIFIED" for row in board["directions"])


def test_concurrent_order_creation_stores_one(tmp_path):
    with TestClient(create_app(tmp_path / "c.sqlite3")) as client:
        run = client.post("/api/runs", json={"sample": "dirty"}).json()
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(
                lambda _: client.post("/api/repair/orders", json={"run_id": run["id"]}).json(), range(4)))
        assert len({r["id"] for r in responses}) == 1
        assert client.get("/api/repair/handover").json()["order_count"] == 1


def test_unknown_order_and_item_return_404(client):
    assert client.get("/api/repair/orders/missing").status_code == 404
    _, order = _start(client)
    response = client.post(f"/api/repair/orders/{order['id']}/items/missing/transition",
                           json={"action": "CONFIRMED"})
    assert response.status_code == 404 and response.json()["code"] == "ITEM_NOT_FOUND"


def test_order_requires_archived_run_and_home_shows_entry(client):
    response = client.post("/api/repair/orders", json={"run_id": "  "})
    assert response.status_code == 422 and response.json()["code"] == "RUN_REQUIRED"
    missing = client.post("/api/repair/orders", json={"run_id": "nope"})
    assert missing.status_code == 404 and missing.json()["code"] == "RUN_NOT_FOUND"
    home = client.get("/#repair")
    assert home.status_code == 200 and "交班复核" in home.text


def test_evidence_keeps_raw_source_lines_immutably(client, dirty):
    _, order = _start(client)
    # 原始行号与原值随条目冻结，修复动作不改变证据内容
    target = order["items"][0]
    before = target["evidence"]
    client.post(f"/api/repair/orders/{order['id']}/items/{target['id']}/transition",
                json={"action": "CONFIRMED"})
    after = client.get(f"/api/repair/orders/{order['id']}").json()
    saved = next(item for item in after["items"] if item["id"] == target["id"])
    assert saved["evidence"] == before
    assert all(isinstance(ev["line"], int) and ev["line"] >= 1 for ev in saved["evidence"])
