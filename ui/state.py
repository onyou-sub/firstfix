"""세션 상태와 여러 페이지가 함께 쓰는 도우미 (기준 시각, 승인 대기, 실행 로그, 검증).

에이전트 호출은 services.py를 통해서만 하고, 목업 모드(USE_MOCK)에서는 services를 import하지 않는다.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from ui import mock

TEST_START, TEST_END = date(2015, 10, 1), date(2015, 12, 31)
AGENT_KO = {"Supervisor": "총괄", "Investigator": "조사", "Planner": "계획", "Data Agent": "데이터",
            "Report Agent": "리포트", "Guard": "승인 관리", "ML": "예측"}

ss = st.session_state


def use_mock() -> bool:
    return bool(ss.get("use_mock", False))


def svc():
    """services 모듈 (실제 연결일 때만 import)."""
    import services

    return services


def live():
    """입력 데이터·경고·긴급 점검 함수 모음. 실제 연결이면 services, 목업이면 live_ops
    (목업에서는 조사 에이전트 대신 LLM 없는 가짜 조사로 같은 흐름을 보여 준다)."""
    if not use_mock():
        return svc()
    import live_ops

    live_ops.INVESTIGATE = live_ops.mock_investigate
    return live_ops


def new_thread_id(d: date) -> str:
    return f"{d:%Y%m%d}-{uuid.uuid4()}"


def day_thread_id(d: date) -> str:
    """(실제) 운영 DB sessions에서 이 사용자·날짜의 thread_id를 찾는다 (없으면 새로 만들어 저장).
    목업이거나 운영 DB를 쓸 수 없으면 매번 새 thread_id."""
    if use_mock() or not user_id():
        return new_thread_id(d)
    from store import app_db

    try:
        return app_db.session_thread(user_id(), f"{d:%Y-%m-%d}", new_thread_id(d))
    except Exception:
        return new_thread_id(d)


def restore_thread() -> None:
    """(실제) 체크포인터에 이 thread의 대화가 남아 있으면 결과·승인 대기·채팅 기록을 다시 읽는다 (서버 재시작·재접속)."""
    if use_mock():
        return
    try:
        if svc().get_job(ss.thread_id)["running"]:  # 같은 서버에서 점검이 아직 진행 중이면 진행 표시로 이어 붙는다
            ss.job_thread = ss.thread_id
            return
        if not svc().has_thread(ss.thread_id):
            return
        ss.result = svc().load_day(as_of_text())
        ss.pending = svc().pending_requests(ss.thread_id) or None
        ss.chat = svc().chat_history(ss.thread_id, as_of_text())
    except Exception as e:
        ss.error = f"이전 대화 복원 실패: {type(e).__name__}: {e}"


def shared_day() -> date | None:
    """관리자가 정해 둔 공유 기준 날짜 (운영 DB). 없거나 읽을 수 없으면 None."""
    from store import app_db

    try:
        cur = app_db.current_day()
    except Exception:
        return None
    return date.fromisoformat(cur["day"]) if cur else None


def publish_day() -> None:
    """관리자의 기준 날짜를 운영 DB에 저장 -> 작업자 화면이 같은 날짜를 따른다."""
    from store import app_db

    if (ss.get("user") or {}).get("role") == "admin":
        app_db.set_current_day(f"{ss.day:%Y-%m-%d}", user_id())


def init() -> None:
    if "day" not in ss:  # 기준 날짜 (위젯과 별도로 유지). 관리자가 정해 둔 공유 날짜가 있으면 이어서 쓴다
        default = date(2015, 11, 12) if use_mock() else date(2015, 10, 1)
        day = shared_day() or default
        ss.day = day if TEST_START <= day <= TEST_END else default
    ss.setdefault("result", None)      # 오늘 점검 결과 {"pred", "findings", "plan", "drafts", ...}
    ss.setdefault("decisions", {})     # (목업) 초안 번호 -> 승인/거절
    ss.setdefault("pending", None)     # (실제) 승인 요청 [{"tool", "draft_id"}]
    ss.setdefault("job_thread", None)  # (실제) 진행 중인 점검의 thread_id
    ss.setdefault("error", None)
    ss.setdefault("last_answer", None)
    ss.setdefault("chat", [])
    ss.setdefault("trace", [])         # 에이전트 실행 로그 (개발자 로그)
    ss.setdefault("prev", None)        # 어제 예측 검증용
    ss.setdefault("report", None)
    ss.setdefault("dev_mode", False)
    if "thread_id" not in ss:
        ss.thread_id = day_thread_id(ss.day)
        restore_thread()


def as_of() -> datetime:
    return datetime.combine(ss.day, datetime.min.time()).replace(hour=6)


def as_of_text() -> str:
    return as_of().strftime("%Y-%m-%d %H:%M:%S")


def user_id() -> str | None:
    return (ss.get("user") or {}).get("id")


def dev_visible() -> bool:
    """개발자 로그는 설정에서 개발자 모드를 켜고, 계정에 allow_dev가 있을 때만."""
    return bool(ss.get("dev_mode")) and bool((ss.get("user") or {}).get("allow_dev"))


def log(agent: str, msg: str) -> None:
    ss.trace.append(f"[{AGENT_KO.get(agent, agent)}] {msg}")


def trace_from_events(events: list[dict]) -> list[str]:
    return [f"{e['time']} [{AGENT_KO.get(e['agent'], e['agent'])}] {e['msg']}" for e in events]


def reset_day() -> None:
    """날짜가 바뀌면 화면을 비우고 그 날짜의 thread_id로 바꾼다 (이미 대화한 날짜면 기록·승인 대기를 복원)."""
    ss.result, ss.decisions, ss.trace, ss.report = None, {}, [], None
    ss.pending, ss.job_thread, ss.error, ss.last_answer, ss.chat = None, None, None, None, []
    ss.thread_id = day_thread_id(ss.day)
    restore_thread()


def on_date_change() -> None:
    ss.day = ss.as_of_date
    reset_day()
    publish_day()


def next_day() -> None:
    """콜백: 기준 시각을 하루 넘기고 어제 예측을 검증 대상으로 남긴다."""
    if use_mock():
        ss.prev = {"as_of": as_of(), "pred": ss.result["pred"]} if ss.result is not None else None
    else:
        ss.prev = {"as_of": as_of(), "pred": None}  # 실제 검증은 services.verify가 파일·정답을 읽음
    ss.day = ss.day + timedelta(days=1)
    ss.as_of_date = ss.day
    reset_day()
    publish_day()


def pending_requests() -> list[dict]:
    """승인 대기 목록 [{"tool", "draft_id"}] (목업은 결정 안 한 초안)."""
    if use_mock():
        res = ss.result
        return [] if res is None else [{"tool": "submit", "draft_id": d["id"]} for d in res["drafts"]
                                       if d["id"] not in ss.decisions]
    return ss.pending or []


def job_running() -> bool:
    return not use_mock() and ss.job_thread is not None


@st.cache_data(show_spinner=False)
def _cached_verify(prev_as_of_text: str) -> pd.DataFrame:
    return svc().verify(prev_as_of_text)


@st.cache_data(show_spinner=False)
def _cached_trend(machine_id: int, sensor: str, as_of_text_: str, live_version: str) -> pd.DataFrame:
    return svc().sensor_trend(machine_id, sensor, 72, as_of_text_)


def cached_trend(machine_id: int, sensor: str, as_of_text_: str) -> pd.DataFrame:
    """센서 추이 (입력 데이터가 바뀌면 캐시를 새로 읽는다)."""
    return _cached_trend(machine_id, sensor, as_of_text_, svc().live_version())


def verify_rows(prev: dict) -> pd.DataFrame:
    """어제 06시 예측 vs 24시간 내 실제 고장. 열: 설비, 예측, 실제, 판정"""
    if use_mock():
        actual = mock.mock_actual_failures(prev["pred"], prev["as_of"])
        hr = {int(x) for x in prev["pred"][prev["pred"].high_risk].machine_id}
        return pd.DataFrame([{"설비": m, "예측": "고위험" if m in hr else "-", "실제": "고장" if m in actual else "-",
                              "판정": "적중" if (m in hr and m in actual) else "헛경보" if m in hr else "놓침"}
                             for m in sorted(hr | actual)], columns=["설비", "예측", "실제", "판정"])
    return _cached_verify(prev["as_of"].strftime("%Y-%m-%d %H:%M:%S"))


def refresh_from_services() -> None:
    """(실제) 결과 파일과 실행 로그를 다시 읽는다."""
    ss.result = svc().load_day(as_of_text())
    ss.trace = trace_from_events(svc().get_job(ss.thread_id)["events"])
