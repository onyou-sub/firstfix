"""작업자 주간 보고서 집계·요약·내보내기·제출 (LLM 없음, 임시 app.db에 가짜 작업·보고·경고).

기준 날짜 2015-10-14(수) -> 주 10/12(월)~10/18(일), 기준 날짜 이후(10/15~) 기록은 쓰지 않는다.
1팀 이번 주 (손계산):
  W1 10/12 #5 모터 교체 완료 150분 예측과 같음, 부품 1
  W2 10/12 #5 임시 점검 완료 60분 다른 부품 + 지시와 달랐던 점
  U1 10/13 #45 긴급 점검 완료 75분 예측과 같음 (배정 10:00 -> 출동 확인 10:06 -> 완료 11:10, 경고 09:58)
  U2 10/14 #12 긴급 센서 점검 진행 중 (배정 13:00 -> 출동 확인 13:10)
  W3 10/14 #7 구동부 교체 배정 (이월)
  -> 배정 5, 완료 3(60%), 긴급 2 · 평균 출동 확인 8분, 총 285분(4시간 45분), 적중 2/3(67%), 이월 2

실행: uv run pytest tests/test_weekly_report.py -v
"""
from datetime import date

import pytest

from agents import weekly_summary as wsum
from store import app_db
from store import weekly_report as wr
from ui import weekly_figures as wf

DAY = "2015-10-14"
T = "2026-10-22T"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(wr, "WEEKLY_DIR", tmp_path / "weekly")
    with app_db.connect():
        pass


def add_wo(wo_id, day, machine_id, comp="motor", task="replace", status="DONE", urgent=0, team="1팀",
           created="09:00", started=None, done=None, start="09:00", end="11:00"):
    with app_db.connect() as conn:
        conn.execute('INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end",'
                     " status, created_at, started_at, done_at, urgent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (wo_id, day, machine_id, comp, task, team, start, end, status, f"{T}{created}:00",
                      f"{T}{started}:00" if started else None, f"{T}{done}:00" if done else None, urgent))


def add_report(wo_id, day, machine_id, minutes, cause="예측과 같음", comp="motor", action="부품 교체", parts=0,
               deviation=None, memo="메모", team="1팀"):
    with app_db.connect() as conn:
        conn.execute("INSERT INTO work_reports (wo_id, machine_id, as_of_date, predicted_comp, actual_cause_type,"
                     " actual_comp, action, duration_min, parts_used, memo, team, reported_by, reported_at, deviation)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'w1', ?, ?)",
                     (wo_id, machine_id, day, comp, cause, comp, action, minutes, parts, memo, team, app_db.now(),
                      deviation))


def add_alert(wo_id, machine_id, created):
    with app_db.connect() as conn:
        conn.execute("INSERT INTO alerts (alert_id, as_of, machine_id, comp, prob, level, source, status, wo_id,"
                     " message, created_at, updated_at) VALUES (?, ?, ?, 'bearing', 0.9, '경고', '실시간', '점검 완료',"
                     " ?, 'x', ?, ?)", (f"A-{wo_id}", "2015-10-13 06:00:00", machine_id, wo_id, f"{T}{created}:00",
                                         f"{T}{created}:00"))


@pytest.fixture
def week():
    add_wo("W1", "2015-10-12", 5)
    add_report("W1", "2015-10-12", 5, 150, parts=1)
    add_wo("W2", "2015-10-12", 5, task="temporary_check")
    add_report("W2", "2015-10-12", 5, 60, cause="다른 부품", comp="bearing", action="조정·청소",
               deviation="모터가 아니라 베어링 소음", memo="베어링 쪽에서 소리" * 20)
    add_wo("U1", "2015-10-13", 45, comp="bearing", task="urgent_check", urgent=1, created="10:00", started="10:06",
           done="11:10")
    add_report("U1", "2015-10-13", 45, 75, comp="bearing")
    add_alert("U1", 45, "09:58")
    add_wo("U2", "2015-10-14", 12, comp="pump", task="sensor_check", status="IN_PROGRESS", urgent=1, created="13:00",
           started="13:10")
    add_wo("W3", "2015-10-14", 7, comp="drive", status="ASSIGNED")
    add_wo("LATER", "2015-10-15", 8, status="ASSIGNED")  # 기준 날짜 이후 -> 제외
    add_wo("PREV", "2015-10-11", 9)  # 지난주 -> 제외
    add_wo("OTHER", "2015-10-13", 10, team="2팀")  # 다른 팀 -> 제외
    return wr.build_weekly("1팀", DAY, author="1팀 작업자")


def test_week_range_and_time_guard(week):
    assert wr.week_range(DAY) == (date(2015, 10, 12), date(2015, 10, 18))
    assert wr.week_range("2015-10-18") == (date(2015, 10, 12), date(2015, 10, 18))  # 일요일도 같은 주
    assert week["meta"]["period"] == "10/12~10/18" and week["meta"]["cut"] == DAY
    assert {t["설비"] for t in week["tasks"]} == {5, 45, 12, 7}  # 10/15 이후·지난주·다른 팀 제외
    last = wr.build_weekly("1팀", DAY, week_offset=-1)
    assert last["meta"]["period"] == "10/05~10/11" and [t["설비"] for t in last["tasks"]] == [9]
    assert wr.build_weekly("1팀", DAY, week_offset=1)["meta"]["period"] == "10/12~10/18"  # 앞으로는 못 감


def test_kpi_matches_hand_calculation(week):
    k = week["kpi"]
    assert (k["assigned"], k["done"], k["done_rate"]) == (5, 3, 60)
    assert (k["urgent"], k["ack_avg_min"]) == (2, 8)  # (6 + 10) / 2
    assert (k["total_min"], k["total_text"]) == (285, "4시간 45분")
    assert (k["reports"], k["hit"], k["hit_rate"], k["carryover"]) == (3, 2, 67, 2)
    assert wr.hours_text(750) == "12시간 30분" and wr.hours_text(45) == "45분" and wr.hours_text(120) == "2시간"


def test_daily_sums_and_team_only(week):
    by = {d["date"]: d for d in week["daily"]}
    assert len(week["daily"]) == 7 and by["2015-10-12"]["normal_done"] == 2
    assert by["2015-10-13"]["urgent_done"] == 1 and by["2015-10-13"]["normal_done"] == 0  # 2팀 작업 제외
    assert (by["2015-10-14"]["urgent_open"], by["2015-10-14"]["normal_open"]) == (1, 1)
    total = sum(d["normal_done"] + d["normal_open"] + d["urgent_done"] + d["urgent_open"] for d in week["daily"])
    assert total == week["kpi"]["assigned"] == 5


def test_tables(week):
    tasks = {t["설비"]: t for t in week["tasks"]}
    assert tasks[45]["긴급"] == "긴급" and tasks[45]["_urgent"] and tasks[45]["계획(분)"] == 60  # 긴급 점검 시간
    assert tasks[5]["작업 종류"] in ("교체", "임시 점검") and tasks[7]["상태"] == "배정"
    [u1, u2] = week["urgent"]
    assert (u1["경고 시각"], u2["경고 시각"]) == ("10-13 09:58", "10-14 13:00")
    assert (u1["설비"], u1["판정"], u1["출동 확인까지(분)"], u1["완료까지(분)"], u1["결과"]) == (45, "경고 · 실시간", 8, 72,
                                                                                    "예측과 같음")
    assert (u2["설비"], u2["판정"], u2["출동 확인까지(분)"], u2["완료까지(분)"]) == (12, "-", 10, None)
    assert [n["설비"] for n in week["notes"]] == [5] and len(week["notes"][0]["메모 요약"]) == wr.MEMO_CHARS
    assert week["notes"][0]["지시와 달랐던 점"] == "모터가 아니라 베어링 소음"
    assert week["parts"] == [{"부품": "모터/전원부", "수량": 1}]
    assert [c["설비"] for c in week["next_week"]["carryover"]] == [12, 7]
    assert week["next_week"]["attention"] == [{"설비": 5, "작업 수": 2, "주요 원인": "예측과 같음"}]
    causes = {c["label"]: c["count"] for c in week["causes"]}
    assert causes["예측과 같음"] == 2 and causes["다른 부품"] == 1
    types = {r["작업 종류"]: r for r in week["time_by_type"]}
    assert types["긴급 점검"]["표준(분)"] == 60 and types["긴급 점검"]["실제(분)"] == 75


def test_empty_week():
    rep = wr.build_weekly("3팀", DAY)
    assert rep["kpi"]["assigned"] == 0 and rep["tasks"] == [] and rep["urgent"] == [] and rep["notes"] == []
    assert rep["causes"] == [] and rep["next_week"] == {"carryover": [], "attention": []}
    assert "지시된 작업은 없었습니다" in wsum.template_summary(rep["summary_facts"])
    assert wf.figures(rep) == {"daily": None, "causes": None, "actions": None, "time": None}


def test_summary_numbers_checked(week):
    facts = week["summary_facts"]
    good = "이번 주 1팀은 작업 5건 중 3건(60%)을 완료했습니다. 긴급 출동은 2건이며 평균 8분 안에 출동을 확인했습니다."
    out = wsum.draft_summary(facts, call=lambda f: good)
    assert out == {"text": good, "auto": False, "reason": None}
    bad = wsum.draft_summary(facts, call=lambda f: "이번 주 1팀은 작업 5건 중 4건(80%)을 완료했습니다.")  # 4, 80 없음
    assert bad["auto"] and bad["text"] == wsum.template_summary(facts)
    err = wsum.draft_summary(facts, call=lambda f: (_ for _ in ()).throw(RuntimeError("429 insufficient_quota")))
    assert err["auto"] and "insufficient_quota" in err["reason"]
    tmpl = wsum.template_summary(facts)
    assert tmpl.startswith("이번 주 1팀은 작업 5건 중 3건(60%)을 완료했습니다.") and wsum.numbers_ok(tmpl, facts)
    assert "평균 8분 안에 출동을 확인했습니다" in tmpl and "4시간 45분" in tmpl


def test_export_html(week):
    page = wf.export_html(week, "요약 문장입니다.", "다음 주 부품 확인 필요")
    assert page.count("<h2>") == 10 and all(f"<h2>{s}</h2>" in page for s in wf.SECTIONS)
    assert page.count("<table>") >= 6 and 'class="urgent"' in page and 'class="gap"' in page
    assert page.count("window.PlotlyConfig") == 1  # plotly js는 첫 그래프에만
    assert "주간 정비 보고서 · 1팀 · 10/12~10/18" in page and "@media print" in page and "15mm" in page
    assert "다음 주 부품 확인 필요" in page and wf.file_name(week) == "주간보고서_1팀_2015-10-12.html"


def test_submit_and_resubmit(week):
    html = wf.export_html(week, "요약", "")
    row = wr.submit_weekly(week, "요약", "", html, "1팀 작업자")
    assert row["status"] == "제출" and row["week_start"] == "2015-10-12" and wr.read_html(row) == html
    admin = {"role": "admin", "id": "admin"}
    notes = [n for n in app_db.notifications_for(admin, 20) if n["head"] == "1팀 주간 보고서"]
    assert len(notes) == 1 and notes[0]["kind"] == "done" and notes[0]["line"] == "10/12~10/18 · 완료 3/5건"
    row = wr.submit_weekly(week, "요약 수정", "의견", html, "1팀 작업자")
    assert row["status"] == "수정 제출" and row["summary"] == "요약 수정" and len(wr.list_weekly()) == 1


@pytest.mark.llm
def test_real_llm_summary(week):
    """실제 LLM 요약: 숫자가 사실과 맞으면 LLM 문장, 아니면 자동으로 템플릿 (어느 쪽이든 숫자는 사실 안에)."""
    out = wsum.draft_summary(week["summary_facts"])
    print("\n", out)
    assert out["text"] and wsum.numbers_ok(out["text"], week["summary_facts"])
    assert out["auto"] or out["text"].rstrip().endswith("다.")
