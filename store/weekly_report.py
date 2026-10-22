"""작업자 주간 보고서 집계 (화면과 분리, 테스트 가능). 숫자·표는 모두 코드로 계산한다.

- week_range(day): 기준 날짜가 속한 주 (월요일, 일요일). 기준 날짜 이후 기록은 쓰지 않는다 (시간 가드).
- build_weekly(team, day, week_offset): 화면·내보내기가 쓰는 dict 하나 (meta, kpi, daily, tasks, causes, actions,
  urgent, time_by_type, notes, parts, next_week, summary_facts). 기록이 없는 항목은 빈 목록.
- submit_weekly / list_weekly / get_weekly: 관리자 제출 (app.db weekly_reports + outputs/reports/weekly/*.html + 알림).

출동 확인 시각은 작업 시작 시각(started_at)이다 (작업자의 [출동 확인]이 작업을 시작 상태로 바꾼다).
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from config import COMP_LABEL_KO
from store import app_db
from store import worker_stats as ws

ROOT_DIR = Path(__file__).resolve().parent.parent
WEEKLY_DIR = ROOT_DIR / "outputs" / "reports" / "weekly"
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]
NOTE_CAUSES = ("다른 부품", "센서·설치 문제", "이상 없음")  # 특이사항으로 올리는 점검 결과
TIME_BOLD_MIN = 30  # 계획·실제 시간 차이가 이 이상이면 실제 칸 굵게
ATTENTION_MIN = 2  # 이번 주 이 횟수 이상 작업한 설비 = 주의 설비
MEMO_CHARS = 80


def _d(day) -> date:
    return day if isinstance(day, date) else pd.Timestamp(day).date()


def week_range(day) -> tuple[date, date]:
    """기준 날짜가 속한 주의 (월요일, 일요일)."""
    d = _d(day)
    mon = d - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=6)


def period_label(mon: date, sun: date) -> str:
    return f"{mon:%m/%d}~{sun:%m/%d}"


def hours_text(minutes: int) -> str:
    """750 -> "12시간 30분", 45 -> "45분", 0 -> "0분"."""
    h, m = divmod(int(minutes), 60)
    return f"{h}시간 {m}분" if h and m else f"{h}시간" if h else f"{m}분"


def _minutes_between(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return max(0, round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60))
    except ValueError:
        return None


def _hm_minutes(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    (h1, m1), (h2, m2) = (map(int, start.split(":")), map(int, end.split(":")))
    return (h2 * 60 + m2) - (h1 * 60 + m1)


def plan_minutes(w: dict, standards: dict | None = None, urgent_min: int | None = None) -> int | None:
    """계획 시간(분): 작업 표준 시간 (긴급 점검은 운영 설정 긴급 점검 시간). 없으면 지시된 시작~종료."""
    if w["task_type"] == "urgent_check" and urgent_min:
        return int(urgent_min)
    if standards:
        std = standards.get((w["task_type"], w["comp"])) or standards.get((w["task_type"], ""))
        if std:
            return int(std["duration_min"])
    return _hm_minutes(w.get("start"), w.get("end"))


def _standards() -> tuple[dict, int]:
    from ops_settings import load_settings
    from tools.plan_tools import load_work_standards

    try:
        standards = load_work_standards()
    except Exception:  # pdm.db가 없으면 지시된 시간으로
        standards = {}
    return standards, int(load_settings()["urgent_check_min"])


def _task_ko(task_type: str | None) -> str:
    return ws.TASK_SHORT.get(task_type, task_type or "-")


def _comp_ko(comp: str | None) -> str:
    return COMP_LABEL_KO.get(comp, comp or "-")


def build_weekly(team: str, day, week_offset: int = 0, author: str | None = None) -> dict:
    """우리 팀 주간 보고서 데이터. week_offset: 0 = 기준 날짜가 속한 주, -1 = 지난주 (기준 날짜 뒤로는 못 감)."""
    today = _d(day)
    mon, sun = week_range(today + timedelta(days=7 * min(0, int(week_offset))))
    cut = min(sun, today)  # 시간 가드: 기준 날짜 이후 기록은 쓰지 않는다
    mon_s, cut_s = f"{mon}", f"{cut}"
    orders = app_db._rows('SELECT * FROM work_orders WHERE team = ? AND as_of_date BETWEEN ? AND ?'
                          ' ORDER BY as_of_date, urgent DESC, start, wo_id', (team, mon_s, cut_s))
    reports = app_db._rows("SELECT * FROM work_reports WHERE team = ? AND as_of_date BETWEEN ? AND ?"
                           " ORDER BY as_of_date, reported_at", (team, mon_s, cut_s))
    rep = {r["wo_id"]: r for r in reports}
    alerts = {a["wo_id"]: a for a in app_db._rows(
        "SELECT * FROM alerts WHERE wo_id IS NOT NULL AND substr(as_of, 1, 10) BETWEEN ? AND ?", (mon_s, cut_s))}
    standards, urgent_min = _standards()

    done = [w for w in orders if w["status"] == "DONE"]
    urgent = [w for w in orders if w.get("urgent")]
    acks = [m for w in urgent if (m := _minutes_between(w["created_at"], w.get("started_at"))) is not None]
    total_min = sum(int(r["duration_min"] or 0) for r in reports)
    same = sum(r["actual_cause_type"] == app_db.CAUSE_TYPES[0] for r in reports)
    carry = [w for w in orders if w["status"] != "DONE"]
    kpi = {
        "assigned": len(orders), "done": len(done),
        "done_rate": round(len(done) / len(orders) * 100) if orders else None,
        "urgent": len(urgent), "urgent_done": sum(w["status"] == "DONE" for w in urgent),
        "ack_avg_min": round(sum(acks) / len(acks)) if acks else None,
        "total_min": total_min, "total_text": hours_text(total_min),
        "reports": len(reports), "hit": same, "hit_rate": round(same / len(reports) * 100) if reports else None,
        "carryover": len(carry),
    }

    daily = []
    for i in range(7):
        d = mon + timedelta(days=i)
        ws_ = [w for w in orders if w["as_of_date"] == f"{d}"]
        daily.append({"date": f"{d}", "label": f"{d:%m/%d}({WEEKDAY_KO[i]})",
                      "normal_done": sum(not w.get("urgent") and w["status"] == "DONE" for w in ws_),
                      "normal_open": sum(not w.get("urgent") and w["status"] != "DONE" for w in ws_),
                      "urgent_done": sum(bool(w.get("urgent")) and w["status"] == "DONE" for w in ws_),
                      "urgent_open": sum(bool(w.get("urgent")) and w["status"] != "DONE" for w in ws_)})

    tasks = []
    for w in orders:
        r = rep.get(w["wo_id"])
        plan = plan_minutes(w, standards, urgent_min)
        actual = int(r["duration_min"]) if r and r["duration_min"] is not None else None
        tasks.append({"날짜": w["as_of_date"], "설비": int(w["machine_id"]), "부품": _comp_ko(w["comp"]),
                      "작업 종류": _task_ko(w["task_type"]), "긴급": "긴급" if w.get("urgent") else "-",
                      "결과": r["actual_cause_type"] if r else "-", "조치": r["action"] if r else "-",
                      "계획(분)": plan, "실제(분)": actual, "상태": app_db.WO_STATUS_KO.get(w["status"], w["status"]),
                      "_urgent": bool(w.get("urgent")),
                      "_time_gap": plan is not None and actual is not None and abs(actual - plan) >= TIME_BOLD_MIN})

    period = (cut - mon).days + 1
    causes = ws.cause_distribution(cut_s, team, "team", period).to_dict("records") if reports else []
    actions = ws.action_distribution(cut_s, team, "team", period).to_dict("records") if reports else []

    urgent_rows = []
    for w in urgent:
        a, r = alerts.get(w["wo_id"]), rep.get(w["wo_id"])
        t0 = (a or {}).get("created_at") or w["created_at"]
        urgent_rows.append({  # 경고 시각: 보고 주의 날짜(작업 지시 날짜) + 실제 감지 시:분
            "경고 시각": f"{w['as_of_date'][5:]} {t0[11:16]}", "설비": int(w["machine_id"]), "부품": _comp_ko(w["comp"]),
            "판정": f"{a['level']} · {a['source']}" if a else "-",
            "출동 확인까지(분)": _minutes_between(t0, w.get("started_at")),
            "완료까지(분)": _minutes_between(t0, w.get("done_at")),
            "결과": r["actual_cause_type"] if r else app_db.WO_STATUS_KO.get(w["status"], w["status"])})

    by_type: dict[str, list[tuple[int, int]]] = {}
    for w in orders:
        r = rep.get(w["wo_id"])
        plan = plan_minutes(w, standards, urgent_min)
        if r and r["duration_min"] is not None and plan is not None:
            by_type.setdefault(_task_ko(w["task_type"]), []).append((plan, int(r["duration_min"])))
    time_by_type = [{"작업 종류": k, "표준(분)": round(sum(p for p, _ in v) / len(v)),
                     "실제(분)": round(sum(a for _, a in v) / len(v)), "건수": len(v)} for k, v in by_type.items()]

    notes = [{"설비": int(r["machine_id"]), "결과": r["actual_cause_type"],
              "메모 요약": (r["memo"] or "-").replace("\n", " ")[:MEMO_CHARS],
              "지시와 달랐던 점": r.get("deviation") or "-"}
             for r in reports if r["actual_cause_type"] in NOTE_CAUSES or (r.get("deviation") or "").strip()]

    used: Counter = Counter()
    for r in reports:
        if int(r["parts_used"] or 0) > 0:
            used[_comp_ko(r["actual_comp"])] += int(r["parts_used"])
    parts = [{"부품": k, "수량": v} for k, v in used.most_common()]

    counts = Counter(int(w["machine_id"]) for w in orders)
    causes_by_m: dict[int, Counter] = {}
    for r in reports:
        causes_by_m.setdefault(int(r["machine_id"]), Counter())[r["actual_cause_type"]] += 1
    next_week = {
        "carryover": [{"지시일": w["as_of_date"], "지난 일수": (today - _d(w["as_of_date"])).days,
                       "설비": int(w["machine_id"]), "작업": ws.order_title(w),
                       "상태": app_db.WO_STATUS_KO.get(w["status"], w["status"])} for w in carry],
        "attention": [{"설비": m, "작업 수": n,
                       "주요 원인": causes_by_m[m].most_common(1)[0][0] if m in causes_by_m else "-"}
                      for m, n in counts.most_common() if n >= ATTENTION_MIN],
    }

    top_cause = max(causes, key=lambda c: c["count"]) if causes else None
    summary_facts = {
        "팀": team, "기간": period_label(mon, sun), "배정": kpi["assigned"], "완료": kpi["done"],
        "완료율(%)": kpi["done_rate"], "긴급 출동": kpi["urgent"], "긴급 완료": kpi["urgent_done"],
        "평균 출동 확인(분)": kpi["ack_avg_min"], "총 작업 시간": kpi["total_text"], "완료 보고": kpi["reports"],
        "예측과 같음": kpi["hit"], "예측 적중률(%)": kpi["hit_rate"], "이월": kpi["carryover"],
        "가장 많은 점검 결과": f"{top_cause['label']} {top_cause['count']}건" if top_cause and top_cause["count"] else None,
        "특이사항": len(notes), "주의 설비": [f"#{a['설비']} {a['작업 수']}회" for a in next_week["attention"]],
    }
    return {"meta": {"team": team, "week_start": mon_s, "week_end": f"{sun}", "cut": cut_s,
                     "period": period_label(mon, sun), "author": author or "-", "day": f"{today}",
                     "week_offset": min(0, int(week_offset))},
            "kpi": kpi, "daily": daily, "tasks": tasks, "causes": causes, "actions": actions, "urgent": urgent_rows,
            "time_by_type": time_by_type, "notes": notes, "parts": parts, "next_week": next_week,
            "summary_facts": summary_facts}


# ---------------------------------------------------------------- 제출 (관리자에게)
def submit_weekly(report: dict, summary: str, opinion: str, html: str, author: str | None) -> dict:
    """제출: outputs/reports/weekly/{team}_{week_start}.html 저장 + weekly_reports 행(같은 주는 덮어쓰고 "수정 제출")
    + 관리자 알림. 저장한 행을 돌려준다."""
    m, k = report["meta"], report["kpi"]
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    path = WEEKLY_DIR / f"{m['team']}_{m['week_start']}.html"
    path.write_text(html, encoding="utf-8")
    now = app_db.now()
    with app_db._lock, app_db.connect() as conn:
        again = conn.execute("SELECT 1 FROM weekly_reports WHERE team = ? AND week_start = ?",
                             (m["team"], m["week_start"])).fetchone() is not None
        status = "수정 제출" if again else "제출"
        conn.execute("INSERT INTO weekly_reports (team, week_start, author, summary, opinion, html_path, status,"
                     " submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(team, week_start) DO UPDATE SET"
                     " author = excluded.author, summary = excluded.summary, opinion = excluded.opinion,"
                     " html_path = excluded.html_path, status = excluded.status, submitted_at = excluded.submitted_at",
                     (m["team"], m["week_start"], author, summary, opinion, str(path), status, now))
        app_db._notify(conn, title=f"{m['team']} 주간 보고서 {status}: {m['period']}",
                       body=summary, to_role="admin", as_of_date=m["cut"], kind="done",
                       head=f"{m['team']} 주간 보고서",
                       line=f"{m['period']} · 완료 {k['done']}/{k['assigned']}건" + (" · 수정 제출" if again else ""))
    return get_weekly(m["team"], m["week_start"])


def get_weekly(team: str, week_start: str) -> dict | None:
    rows = app_db._rows("SELECT * FROM weekly_reports WHERE team = ? AND week_start = ?", (team, week_start))
    return rows[0] if rows else None


def list_weekly() -> list[dict]:
    return app_db._rows("SELECT * FROM weekly_reports ORDER BY week_start DESC, team")


def read_html(row: dict) -> str | None:
    try:
        return Path(row["html_path"]).read_text(encoding="utf-8")
    except (OSError, TypeError, KeyError):
        return None
