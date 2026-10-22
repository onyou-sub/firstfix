"""알림 개편 확인 (LLM 없음, 임시 app.db): 안 읽은 알림 전부, 사람별 읽음, 같은 작업 카드 한 장,
새 작업·승인 대기·입고 묶음, 긴급은 처리해야 사라짐, 종류·head/line 분류.

실행: uv run pytest tests/test_notifications.py -v
"""

import pytest

from store import app_db

DAY = "2015-10-14"
A = {"id": "a2", "role": "worker", "team": "2팀"}
B = {"id": "b2", "role": "worker", "team": "2팀"}
ADMIN = {"id": "admin", "role": "admin", "team": None}


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "app.db")
    with app_db.connect():
        pass
    yield


def add_wo(wo_id: str, team: str = "2팀", day: str = DAY) -> None:
    with app_db.connect() as conn:
        conn.execute('INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end",'
                     " status, created_at, urgent) VALUES (?, ?, 45, 'bearing', 'urgent_check', ?, '09:00', '10:00',"
                     " 'ASSIGNED', ?, 1)", (wo_id, day, team, app_db.now()))


def note(title: str, kind: str | None = None, team: str | None = "2팀", role: str = "worker", wo: str | None = None,
         day: str = DAY, **kw) -> int:
    app_db.notify(title=title, to_role=role, to_team=team, link_wo_id=wo, as_of_date=day, kind=kind, **kw)
    return app_db._rows("SELECT MAX(id) AS i FROM notifications")[0]["i"]


def test_all_unread_shown_and_read_removed():
    ids = [note(f"#{m} 자동 조사 실패", kind="notice", role="admin", team=None) for m in range(1, 16)]
    box = app_db.inbox(ADMIN, DAY)
    assert len(box["plain"]) == 15 and box["unread"] == 15  # 개수 제한 없음
    assert [c["id"] for c in box["plain"]] == ids[::-1]  # 최신이 위
    app_db.mark_read(ids[0], ADMIN)
    assert len(app_db.inbox(ADMIN, DAY)["plain"]) == 14  # 읽으면 빠짐


def test_same_work_order_one_card_and_open_reads_all():
    note("완료 보고: #9 (2팀) — 이상 없음", kind="done", role="admin", team=None, wo="WO-1014-009")
    note("조사 결과 추가됨: #9 구동부", kind="finding", role="admin", team=None, wo="WO-1014-009")
    note("#9 점검 항목 갱신", kind="finding", role="admin", team=None, wo="WO-1014-009",
         head="#9 구동부", line="점검 항목 갱신")
    [card] = app_db.inbox(ADMIN, DAY)["plain"]
    assert card["extra"] == "외 2건" and card["title"].endswith("외 2건") and len(card["ids"]) == 3
    assert (card["head"], card["line"], card["label"]) == ("#9 구동부", "점검 항목 갱신", "조사 결과")  # 최신 내용
    app_db.mark_read_many(card["ids"], ADMIN)  # 카드를 열면 묶인 알림 전부 읽음
    assert app_db.inbox(ADMIN, DAY)["plain"] == []
    assert app_db.notification_history(ADMIN, unread_only=True) == []


def test_new_tasks_grouped_by_team():
    for m in range(1, 6):
        note(f"새 작업: #{m} 모터/전원부 교체 (2팀 09:00~11:00)", kind="task", wo=f"WO-1014-{m:03d}")
    note("새 작업: #9 밸브/펌프 교체", kind="task", team="1팀", wo="WO-1014-009")  # 다른 팀은 2팀원에게 안 보임
    [group] = app_db.inbox(A, DAY)["todo_groups"]
    assert (group["kind"], group["team"], group["count"]) == ("task", "2팀", 5)
    assert (group["label"], group["title"]) == ("새 작업", "5건 (2팀)") and len(group["items"]) == 5
    for t in ("재고 0 발주 승인 대기: pump 2개", "재고 0 발주 승인 대기: motor 1개"):
        note(t, kind="approval", role="admin", team=None)
    [approval] = app_db.inbox(ADMIN, DAY)["todo_groups"]
    assert (approval["label"], approval["title"]) == ("승인 대기", "2건")


def test_read_is_per_person():
    i = note("새 작업: #45 베어링 교체", kind="task", wo="WO-1014-045")
    app_db.mark_read(i, A)
    assert app_db.inbox(A, DAY)["unread"] == 0
    assert app_db.inbox(B, DAY)["unread"] == 1  # 같은 팀이어도 B는 안 읽음
    assert app_db.unread_count(B) == 1 and app_db.unread_count(A) == 0


@pytest.mark.parametrize("ack", ["start", "done"])
def test_urgent_stays_until_handled(ack):
    add_wo("UWO-1014-045-1")
    w = note("긴급 점검: #45 베어링 — 09:00 출동", kind="urgent", wo="UWO-1014-045-1")
    a = note("출동 확인 다시 알림: #45 베어링", kind="urgent", role="admin", team=None, wo="UWO-1014-045-1")
    app_db.mark_read(w, A)
    app_db.mark_read(a, ADMIN)
    assert app_db.mark_all_read(A) == 0
    assert [x["id"] for x in app_db.inbox(A, DAY)["urgent"]] == [w]  # 읽어도 남는다
    assert [x["id"] for x in app_db.inbox(ADMIN, DAY)["urgent"]] == [a]
    with app_db.connect() as conn:
        if ack == "start":  # 출동 확인 (= 작업 시작, started_at)
            conn.execute("UPDATE work_orders SET status = 'IN_PROGRESS', started_at = ? WHERE wo_id = ?",
                         (app_db.now(), "UWO-1014-045-1"))
        else:
            conn.execute("UPDATE work_orders SET status = 'DONE', done_at = ? WHERE wo_id = ?",
                         (app_db.now(), "UWO-1014-045-1"))
    assert app_db.inbox(A, DAY)["urgent"] == [] and app_db.inbox(B, DAY)["urgent"] == []
    assert app_db.inbox(ADMIN, DAY)["urgent"] == []  # 관리자 쪽도 같은 규칙


def test_admin_dispatch_result_is_plain_assign():
    add_wo("UWO-1014-045-1")
    note("#45 긴급 점검을 2팀에 자동 배정했습니다", kind="assign", role="admin", team=None, wo="UWO-1014-045-1",
         head="#45 베어링", line="긴급 점검 → 2팀 09:00")
    box = app_db.inbox(ADMIN, DAY)
    assert box["urgent"] == [] and [c["label"] for c in box["plain"]] == ["배정"]


def test_previous_day_hidden_except_open_urgent():
    note("새 작업: #1 모터/전원부 교체", kind="task", wo="WO-1013-001", day="2015-10-13")
    note("완료 보고: #2 (2팀) — 이상 없음", kind="done", role="admin", team=None, day="2015-10-13")
    add_wo("UWO-1013-045-1", day="2015-10-13")
    u = note("긴급 점검: #45 베어링 — 09:00 출동", kind="urgent", wo="UWO-1013-045-1", day="2015-10-13")
    box = app_db.inbox(A, DAY)
    assert box["todo_groups"] == [] and box["plain"] == []
    assert [x["id"] for x in box["urgent"]] == [u]  # 처리 안 된 긴급은 날짜가 지나도 보인다
    assert app_db.inbox(ADMIN, DAY)["plain"] == []
    assert len(app_db.inbox(A, "2015-10-13")["todo_groups"]) == 1  # 그 날짜 기준이면 보인다


def test_mark_all_read_skips_urgent():
    add_wo("UWO-1014-045-1")
    note("긴급 점검: #45 베어링 — 09:00 출동", kind="urgent", wo="UWO-1014-045-1")
    note("새 작업: #1 모터/전원부 교체", kind="task", wo="WO-1014-001")
    note("조사 결과 추가됨: #3 펌프", kind="finding")
    assert app_db.mark_all_read(A) == 2
    box = app_db.inbox(A, DAY)
    assert len(box["urgent"]) == 1 and box["todo_groups"] == [] and box["plain"] == [] and box["unread"] == 1
    assert [r["kind"] for r in app_db.notification_history(A, unread_only=True)] == ["urgent"]


@pytest.mark.parametrize("title, kind, head, line", [
    ("긴급 점검: #45 베어링 — 09:00 출동 (근거 E1014-045)", "urgent", "#45 베어링", "긴급 점검 · 09:00 출동"),
    ("센서 점검: #12 밸브/펌프 — 내일 10:00 출동", "urgent", "#12 밸브/펌프", "센서 점검 · 내일 10:00 출동"),
    ("출동 확인 다시 알림: #45 베어링", "urgent", "#45 베어링", "출동 확인 다시 알림"),
    ("#45 긴급 점검을 2팀에 자동 배정했습니다", "assign", "#45", "긴급 점검을 2팀에 자동 배정했습니다"),
    ("새 작업: #90 모터/전원부 교체 (2팀 09:00~11:00)", "task", "#90 모터/전원부", "새 작업 · 교체 · 2팀 09:00~11:00"),
    ("재고 0 발주 승인 대기: pump 2개", "approval", "pump 2개", "재고 0 발주 승인 대기"),
    ("발주 승인: pump 2개 (희망 발주일 2015-10-14)", "stock", "pump 2개", "발주 승인 · 희망 발주일 2015-10-14"),
    ("완료 보고: #90 (2팀) — 예측과 같음", "done", "#90", "완료 보고 · 2팀 · 예측과 같음"),
    ("조사 결과 추가됨: #45 베어링", "finding", "#45 베어링", "조사 결과 추가됨"),
    ("재고 0: 밸브/펌프 (pump)", "notice", "밸브/펌프", "재고 0 · pump"),
    ("#45 자동 조사 실패", "notice", "#45", "자동 조사 실패"),
    ("알림", "notice", "알림", ""),
])
def test_classify_and_split_old_titles(title, kind, head, line):
    assert app_db.classify_kind(title) == kind
    assert app_db.split_title(title) == (head, line)


def test_old_rows_get_kind_and_head_on_migration():
    with app_db.connect() as conn:  # kind·head 없이 직접 넣은 옛 행, 예전 종류 이름(info)으로 저장된 행
        conn.execute("INSERT INTO notifications (to_role, to_team, title, as_of_date, created_at)"
                     " VALUES ('worker', '2팀', '새 작업: #3 모터/전원부 교체', ?, ?)", (DAY, app_db.now()))
        conn.execute("INSERT INTO notifications (to_role, title, as_of_date, created_at, kind)"
                     " VALUES ('admin', '완료 보고: #9 (2팀) — 이상 없음', ?, ?, 'info')", (DAY, app_db.now()))
    rows = app_db._rows("SELECT kind, head FROM notifications ORDER BY id")
    assert [(r["kind"], r["head"]) for r in rows] == [("task", "#3 모터/전원부"), ("done", "#9")]
    assert app_db.inbox(A, DAY)["todo_groups"][0]["kind"] == "task"


def test_notify_stores_head_and_line():
    note("긴급 점검: #45 베어링 — 14:20 출동 (근거 E1)", kind="urgent", head="#45 베어링",
         line="긴급 점검 · 14:20 출동 · 2팀", body="근거 E1")
    [card] = app_db.inbox(A, DAY)["urgent"]
    assert (card["head"], card["line"], card["label"], card["tier"]) == ("#45 베어링", "긴급 점검 · 14:20 출동 · 2팀",
                                                                         "긴급", "urgent")
    assert card["body"] == "근거 E1"  # 부가 정보는 body로만


def test_history_includes_hidden():
    i = note("새 작업: #1 모터/전원부 교체", kind="task", wo="WO-1014-001")
    note("새 작업: #2 모터/전원부 교체", kind="task", wo="WO-1013-002", day="2015-10-13")
    app_db.mark_read(i, A)
    assert app_db.inbox(A, DAY)["unread"] == 0
    rows = app_db.notification_history(A)
    assert len(rows) == 2 and {r["read"] for r in rows} == {True, False}
    assert [r["as_of_date"] for r in app_db.notification_history(A, day=DAY)] == [DAY]
    assert len(app_db.notification_history(A, since="2015-10-13")) == 2
    assert app_db.notification_history(A, kind="done") == []
    assert {r["label"] for r in rows} == {"새 작업"}
