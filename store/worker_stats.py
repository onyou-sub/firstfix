"""작업자 대시보드 집계 (LLM 없음). 화면(views/worker_dashboard.py, views/worker_history.py)과 분리해 임시 app.db로 테스트한다.

- day = 기준 날짜 'YYYY-MM-DD'. 완료 보고·센서 집계는 day(오늘 포함) 이하 기록만 쓴다 (시간 가드).
  달력은 미래 날짜라도 작업 지시가 있으면 보여 준다 (다음 근무로 넘어간 긴급 작업 등).
- scope = "team"(우리 팀) | "all"(전체 팀), period = 30(최근 30일, 오늘 포함) | None(전체)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from store import app_db
from stream import DOWN_SENSORS, SENSOR_COMP, SENSORS
from ui.style import GRAY, NAVY, ORANGE, RED, RED_DARK

TIMELINE_COLS = ["wo_id", "machine_id", "comp", "task_type", "title", "start_dt", "end_dt", "status", "urgent"]
SENSOR_COLS = ["machine_id", "task", *SENSORS, "key_sensor", "key_pct", "trend", "source", "state"]
TOP_COLS = ["machine_id", "work_count", "main_cause", "main_comp", "last_date", "open_count"]
NO_REPORTS = "아직 완료 보고가 없습니다."
COMP_SHORT = {"motor": "모터", "drive": "구동부", "pump": "펌프", "bearing": "베어링"}  # 달력 칸처럼 좁은 곳
TASK_SHORT = {"replace": "교체", "temporary_check": "임시 점검", "sensor_check": "센서 점검", "urgent_check": "긴급 점검"}
KEY_SENSOR = {comp: sensor for sensor, comp in SENSOR_COMP.items()}  # 부품 -> 짝 센서
BG_ORANGE, BG_RED = "#FCEBD7", "#F8DAD6"  # 밀린 날짜 칸 배경 (1~2일 / 3일 이상)
LIVE_SECONDS = 5  # 이 안에 쓴 1초 스트림 행이 있으면 "실시간"
CAUTION_Z = 2.0


def _d(day) -> date:
    return day if isinstance(day, date) else date.fromisoformat(str(day)[:10])


def _dt(day: str, hm: str | None):
    return pd.Timestamp(f"{day} {hm}") if hm else pd.NaT


def _remaining(w: dict) -> bool:
    return w["status"] != "DONE"


def order_title(w: dict) -> str:
    """"#8 모터 교체" (달력·목록용 짧은 이름)."""
    return f"#{w['machine_id']} {COMP_SHORT.get(w['comp'], w['comp'])} {TASK_SHORT.get(w['task_type'], '작업')}"


def overdue_days(day: str, order_date: str) -> int:
    return max((_d(day) - _d(order_date)).days, 0)


def calendar_options(day: str) -> dict:
    """FullCalendar 옵션 (월간, 월요일 시작, 칸당 이벤트 2개 + "+N개"). now = 기준 날짜 (오늘 칸 강조가 기준 날짜를 따름).
    timeZone "UTC": 날짜 칸 클릭이 'YYYY-MM-DDT00:00:00.000Z'로 와서 앞 10자리가 그 날짜다
    (브라우저 시간대(KST)로 두면 toISOString()이 전날 15:00Z가 되어 하루 밀린다)."""
    return {"initialView": "dayGridMonth", "initialDate": f"{_d(day)}", "now": f"{_d(day)}", "timeZone": "UTC",
            "locale": "ko", "firstDay": 1, "dayMaxEvents": 2, "height": 520,
            "headerToolbar": {"left": "prev,next today", "center": "title", "right": "dayGridMonth,dayGridWeek"},
            # streamlit-calendar에는 영어 로캘만 들어 있어 버튼·"+N개" 글자는 직접 준다 (날짜 이름은 브라우저 Intl이 ko로)
            "buttonText": {"today": "오늘", "month": "월", "week": "주"}, "moreLinkText": "개",
            # 한 칸 안 순서: 남은 긴급 -> 완료한 긴급 -> 남은 일반 -> "완료 N" (칸당 2개라 위쪽이 보이도록)
            "eventOrder": "order,title"}


def clicked_date(value: str | None) -> str | None:
    """달력이 돌려준 날짜('2015-10-09' 또는 '2015-10-09T00:00:00.000Z')의 앞 10자리.
    달력 옵션 timeZone="UTC"와 같이 써야 한다 (브라우저 시간대(KST)로 두면 toISOString()이 전날 15:00Z가 된다)."""
    return str(value)[:10] if value else None


# ---------------------------------------------------------------- ① 오늘 시간표
def today_timeline(day: str, team: str) -> pd.DataFrame:
    """기준 날짜에 지시된 우리 팀 작업 (시작 시각 순). start/end "HH:MM"을 day와 합쳐 datetime으로."""
    rows = [{"wo_id": w["wo_id"], "machine_id": w["machine_id"], "comp": w["comp"], "task_type": w["task_type"],
             "title": w["title"], "start_dt": _dt(w["as_of_date"], w["start"]), "end_dt": _dt(w["as_of_date"], w["end"]),
             "status": w["status"], "urgent": bool(w.get("urgent"))} for w in app_db.work_orders(day, team)]
    df = pd.DataFrame(rows, columns=TIMELINE_COLS)
    return df.sort_values(["start_dt", "wo_id"], na_position="last").reset_index(drop=True)


# ---------------------------------------------------------------- ② 달력 + 선택 날짜 작업 목록
def _event_color(w: dict, overdue: int) -> str:
    if w.get("urgent") or overdue >= 3:
        return RED
    return ORANGE if overdue else NAVY


# 한 칸 안 정렬 (이벤트 "order")
ORDER_URGENT, ORDER_URGENT_DONE, ORDER_NORMAL, ORDER_DONE_SUMMARY = 0, 1, 2, 3


def calendar_events(day: str, team: str, month_start: str, month_end: str) -> list[dict]:
    """[month_start, month_end] 구간 우리 팀 작업 -> FullCalendar 이벤트.
    - 긴급 작업은 상태와 관계없이 작업별 1개 (완료한 긴급은 "긴급 완료 · …", 진한 빨강, ev-urgent-done)
    - 일반 작업: 남은 것은 작업별 1개, 완료한 것은 날짜별 "완료 N" 1개 (남은 작업이 있는 날에도)
    - 남은 작업이 있는 지난 날짜는 배경색 이벤트 (완료한 긴급은 영향 없음)
    - 이벤트마다 정렬용 "order" (남은 긴급 0 < 완료한 긴급 1 < 남은 일반 2 < 완료 요약 3)"""
    orders = app_db._rows("SELECT * FROM work_orders WHERE team = ? AND as_of_date BETWEEN ? AND ?"
                          ' ORDER BY as_of_date, urgent DESC, start, wo_id',
                          (team, f"{_d(month_start)}", f"{_d(month_end)}"))
    by_day: dict[str, list[dict]] = {}
    for w in orders:
        by_day.setdefault(w["as_of_date"], []).append(w)
    events = []
    for d, ws in by_day.items():
        rem = [w for w in ws if _remaining(w)]
        late = overdue_days(day, d) if rem else 0
        for w in rem:
            color = _event_color(w, late)
            events.append({"id": w["wo_id"], "title": ("긴급 " if w.get("urgent") else "") + order_title(w),
                           "start": d, "allDay": True, "backgroundColor": color, "borderColor": color,
                           "textColor": "#FFFFFF", "order": ORDER_URGENT if w.get("urgent") else ORDER_NORMAL,
                           "extendedProps": {"kind": "order", "wo_id": w["wo_id"], "overdue_days": late,
                                             "urgent": bool(w.get("urgent")), "status": w["status"]}})
        for w in (w for w in ws if not _remaining(w) and w.get("urgent")):  # 완료한 긴급은 따로 남긴다
            events.append({"id": w["wo_id"], "title": f"긴급 완료 · {order_title(w)}", "start": d, "allDay": True,
                           "backgroundColor": RED_DARK, "borderColor": RED_DARK, "textColor": "#FFFFFF",
                           "classNames": ["ev-urgent-done"], "order": ORDER_URGENT_DONE,
                           "extendedProps": {"kind": "order", "wo_id": w["wo_id"], "urgent": True, "status": "DONE",
                                             "overdue_days": 0}})
        done = sum(not _remaining(w) and not w.get("urgent") for w in ws)  # 완료한 일반 작업만
        if done:
            events.append({"id": f"done-{d}", "title": f"완료 {done}", "start": d, "allDay": True,
                           "backgroundColor": GRAY, "borderColor": GRAY, "textColor": "#FFFFFF",
                           "order": ORDER_DONE_SUMMARY, "extendedProps": {"kind": "done_summary", "done_count": done}})
        if late:
            events.append({"id": f"bg-{d}", "start": d, "allDay": True, "display": "background",
                           "backgroundColor": BG_RED if late >= 3 else BG_ORANGE,
                           "extendedProps": {"kind": "overdue_bg", "overdue_days": late}})
    return events


def calendar_range(day: str, team: str, months_before: int = 2, months_after: int = 1) -> tuple[str, str]:
    """달력에 넘길 이벤트 구간: 기준 달 앞뒤 몇 달 + 가장 오래된 밀린 날짜. 달력 부품(streamlit-calendar)이
    달 이동(datesSet)을 알려 주지 않아서, 이동할 만한 달의 이벤트를 미리 넣어 둔다."""
    first = _d(day).replace(day=1)
    start = (pd.Timestamp(first) - pd.DateOffset(months=months_before)).date()
    end = (pd.Timestamp(first) + pd.DateOffset(months=months_after + 1) - pd.Timedelta(days=1)).date()
    return f"{min(start, _d(default_calendar_date(day, team)))}", f"{end}"


def default_calendar_date(day: str, team: str) -> str:
    """처음 열 때 고를 날짜: 가장 오래된 밀린 날짜 (없으면 기준 날짜)."""
    carried = app_db.open_orders_before(day, team)
    return carried[0]["as_of_date"] if carried else f"{_d(day)}"


def orders_on(day: str, team: str) -> list[dict]:
    """그 날짜에 지시된 우리 팀 작업 (남은 것 먼저, 그다음 긴급, 시작 시각 순)."""
    return sorted(app_db.work_orders(day, team),
                  key=lambda w: (not _remaining(w), -int(w.get("urgent") or 0), w["start"] or "", w["wo_id"]))


# ---------------------------------------------------------------- ③ 내 설비 실시간 센서
def my_machines(day: str, team: str) -> list[dict]:
    """오늘 우리 팀 미완료 작업 + 날짜와 관계없이 우리 팀 미완료 긴급 작업. 설비별로 하나 (긴급 우선, 시작 시각 이른 것)."""
    from tools.plan_tools import TASK_LABEL

    rows = app_db._rows("SELECT * FROM work_orders WHERE team = ? AND status != 'DONE' AND (as_of_date = ? OR urgent = 1)"
                        ' ORDER BY urgent DESC, as_of_date, start, wo_id', (team, f"{_d(day)}"))
    out, seen = [], set()
    for w in rows:
        if w["machine_id"] in seen:
            continue
        seen.add(w["machine_id"])
        out.append({"machine_id": int(w["machine_id"]), "wo_id": w["wo_id"], "comp": w["comp"],
                    "task_label": TASK_LABEL.get(w["task_type"], "작업"), "start": w["start"],
                    "urgent": bool(w.get("urgent"))})
    return out


def _latest_stream_row(machine_id: int, seconds: int = LIVE_SECONDS) -> dict | None:
    cutoff = (datetime.now() - timedelta(seconds=seconds)).isoformat(timespec="milliseconds")
    rows = app_db._rows(f"SELECT {', '.join(SENSORS)} FROM live_stream WHERE machine_id = ? AND wall_ts >= ?"
                        " ORDER BY wall_ts DESC, rowid DESC LIMIT 1", (int(machine_id), cutoff))
    return rows[0] if rows else None


def sensor_state(sensor: str, value, mean: float, std: float) -> str:
    """경고선(Z_LIMIT) 초과 / 주의(|z| >= 2) / 정상 / 값 없음. rotate는 아래로 넘을 때만 경고선 초과."""
    from stream.detector import Z_LIMIT

    if value is None or pd.isna(value):
        return "값 없음"
    z = (float(value) - mean) / std
    if (z <= -Z_LIMIT) if sensor in DOWN_SENSORS else (z >= Z_LIMIT):
        return "경고선 초과"
    return "주의" if abs(z) >= CAUTION_Z else "정상"


def live_sensor_table(day: str, team: str, as_of: str) -> pd.DataFrame:
    """내 설비(my_machines)마다 1행: 센서 4개 현재 값, 작업 부품의 짝 센서 평소 대비·추이, 데이터 출처, 상태.
    실시간 = 생성기 실행 중 + 그 설비 1초 스트림이 최근 5초 안에 있음. 아니면 기준 시각의 시간 단위 마지막 기록."""
    from stream.baseline import baseline
    from stream.control import stream_status, stream_window
    from tools.data_tools import _sensor_rows

    machines = my_machines(day, team)
    if not machines:
        return pd.DataFrame(columns=SENSOR_COLS)
    running = stream_status()["state"] == "실행 중"
    rows = []
    for m in machines:
        mid, key = m["machine_id"], KEY_SENSOR.get(m["comp"], "vibration")
        latest = _latest_stream_row(mid) if running else None
        if latest is not None:
            source, values = "실시간", {s: latest[s] for s in SENSORS}
            trend = stream_window(mid, 60, as_of)[key].dropna().tolist()
        else:
            source, values, trend = "시간 단위", {}, []
            for s in SENSORS:
                hist = _sensor_rows(mid, s, 72, as_of)
                values[s] = float(hist["value"].iloc[-1]) if len(hist) and pd.notna(hist["value"].iloc[-1]) else None
                if s == key and len(hist):
                    since = pd.Timestamp(as_of) - pd.Timedelta(hours=24)
                    trend = hist.loc[pd.to_datetime(hist["datetime"]) > since, "value"].dropna().tolist()
        mean, std = baseline(mid, as_of)[key]
        v = values.get(key)
        rows.append({"machine_id": mid, "task": f"{m['task_label']} {m['start'] or '-'}", **values, "key_sensor": key,
                     "key_pct": None if v is None else (float(v) - mean) / mean * 100, "trend": [float(x) for x in trend],
                     "source": source, "state": sensor_state(key, v, mean, std)})
    return pd.DataFrame(rows, columns=SENSOR_COLS)


# ---------------------------------------------------------------- ④ ⑤ 완료 보고 집계 (+ 작업 이력 Top 5)
def report_count() -> int:
    return app_db._rows("SELECT COUNT(*) AS n FROM work_reports")[0]["n"]


def order_count() -> int:
    return app_db._rows("SELECT COUNT(*) AS n FROM work_orders")[0]["n"]


def _reports(day: str, team: str, scope: str, period: int | None) -> pd.DataFrame:
    """기간·범위 안의 완료 보고 (as_of_date <= day). 가상 현장 메모(VIRTUAL_REPORTER)도 포함."""
    sql, params = "SELECT * FROM work_reports WHERE as_of_date <= ?", [f"{_d(day)}"]
    if period:
        sql, params = sql + " AND as_of_date >= ?", [*params, f"{_d(day) - timedelta(days=period - 1)}"]
    if scope == "team":
        sql, params = sql + " AND team = ?", [*params, team]
    cols = ["wo_id", "machine_id", "as_of_date", "predicted_comp", "actual_cause_type", "actual_comp", "action",
            "team", "reported_by"]
    return pd.DataFrame(app_db._rows(sql + " ORDER BY as_of_date DESC, reported_at DESC", tuple(params)), columns=cols)


def _distribution(values: pd.Series, order: list[str]) -> pd.DataFrame:
    counts = values.value_counts()
    df = pd.DataFrame({"label": order, "count": [int(counts.get(k, 0)) for k in order]})
    total = int(df["count"].sum())
    df["pct"] = df["count"] / total * 100 if total else 0.0
    return df


def cause_distribution(day: str, team: str, scope: str = "team", period: int | None = 30) -> pd.DataFrame:
    """점검 결과(actual_cause_type) 분포: label, count, pct. CAUSE_TYPES 순서, 0건도 행으로."""
    return _distribution(_reports(day, team, scope, period)["actual_cause_type"], app_db.CAUSE_TYPES)


def action_distribution(day: str, team: str, scope: str = "team", period: int | None = 30) -> pd.DataFrame:
    """조치(action) 분포: label, count, pct. ACTIONS 순서, 0건도 행으로."""
    return _distribution(_reports(day, team, scope, period)["action"], app_db.ACTIONS)


def period_text(period: int | None) -> str:
    return f"최근 {period}일" if period else "전체"


def cause_summary_text(df: pd.DataFrame, period: int | None) -> str:
    total = int(df["count"].sum()) if len(df) else 0
    if not total:
        return NO_REPORTS
    same = int(df.loc[df["label"] == app_db.CAUSE_TYPES[0], "count"].sum())
    return f"{period_text(period)} 점검 {total}건 중 {same}건이 예측과 같았습니다."


def action_summary_text(df: pd.DataFrame) -> str:
    if not len(df) or not int(df["count"].sum()):
        return NO_REPORTS
    top = df.loc[df["count"].idxmax()]  # 동률이면 ACTIONS 순서상 앞의 것
    return f"가장 많이 한 조치: {top['label']} {int(top['count'])}건 ({top['pct']:.0f}%)"


def top_machines(day: str, team: str, scope: str = "team", period: int | None = 30, n: int = 5) -> pd.DataFrame:
    """자주 손본 설비: 완료 보고 수 많은 순 → 같으면 마지막 작업일 최근 순. 최빈 원인·부품, 우리 팀 미완료 작업 수."""
    rep = _reports(day, team, scope, period)
    if rep.empty:
        return pd.DataFrame(columns=TOP_COLS)
    rep["comp"] = rep["actual_comp"].fillna(rep["predicted_comp"])
    rows = []
    for mid, g in rep.groupby("machine_id"):  # g는 최근 순 정렬 -> 최빈값 동률이면 최근 값
        rows.append({"machine_id": int(mid), "work_count": len(g),
                     "main_cause": g["actual_cause_type"].value_counts().index[0],
                     "main_comp": g["comp"].dropna().value_counts().index[0] if g["comp"].notna().any() else None,
                     "last_date": g["as_of_date"].max()})
    df = pd.DataFrame(rows).sort_values(["work_count", "last_date"], ascending=[False, False]).head(n)
    opened = {r["machine_id"]: r["n"] for r in app_db._rows(
        "SELECT machine_id, COUNT(*) AS n FROM work_orders WHERE team = ? AND status != 'DONE' AND as_of_date <= ?"
        " GROUP BY machine_id", (team, f"{_d(day)}"))}
    df["open_count"] = [int(opened.get(m, 0)) for m in df["machine_id"]]
    return df[TOP_COLS].reset_index(drop=True)
