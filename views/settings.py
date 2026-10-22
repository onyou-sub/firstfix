"""설정: 운영 설정(저장 시 계획 일정 계산에 반영) / 모델 정보(읽기 전용) / 개발자 모드 / 데모 데이터 초기화(날짜별·전체) / 시스템 안내."""
from __future__ import annotations

import pickle
from datetime import datetime, time
from pathlib import Path

import pandas as pd
import streamlit as st

import records
from config import COMP_LABEL_KO
from ops_settings import URGENT_OFF_HOURS, load_settings, save_settings
from ui import state as S

ss = st.session_state
S.init()
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "lgbm.pkl"
PERIOD_KO = {"train": "학습", "valid": "검증(임계값 결정)", "test": "평가"}


def _t(hm: str) -> time:
    return datetime.strptime(hm, "%H:%M").time()


@st.cache_data(show_spinner=False)
def model_info(mtime: float) -> dict:
    with open(MODEL_PATH, "rb") as f:
        bundle = pickle.load(f)
    return {"thresholds": bundle.get("thresholds", {}), "rule": bundle.get("threshold_rule", "-"),
            "periods": bundle.get("periods", {})}


st.subheader("설정")

# ── 운영 설정 ──
with st.container(border=True):
    st.markdown("**운영 설정** · 저장하면 다음 계획·재계획의 일정 계산에 반영됩니다.")
    cfg = load_settings()
    with st.form("ops_form", border=False):
        c1, c2, c3, c4 = st.columns(4)
        work_start = c1.time_input("근무 시작", _t(cfg["work_start"]), step=1800, key="set_work_start")
        work_end = c2.time_input("근무 종료", _t(cfg["work_end"]), step=1800, key="set_work_end")
        lunch_start = c3.time_input("점심 시작", _t(cfg["lunch_start"]), step=1800, key="set_lunch_start")
        lunch_end = c4.time_input("점심 종료", _t(cfg["lunch_end"]), step=1800, key="set_lunch_end")
        c5, c6, c7, c8 = st.columns(4)
        prep_min = c5.number_input("작업 간 준비 시간(분)", 0, 120, int(cfg["prep_min"]), step=5, key="set_prep")
        max_replan = c6.number_input("하루 재계획 한도(회)", 0, 10, int(cfg["max_replan"]), step=1, key="set_replan")
        urgent_min = c7.number_input("긴급 점검 시간(분)", 10, 240, int(cfg["urgent_check_min"]), step=10,
                                     key="set_urgent_min",
                                     help="경고가 생기면 승인 없이 지금 시각부터 이 시간만큼 긴급 점검을 자동 배정합니다.")
        off_hours = c8.selectbox("근무 시간 외 긴급", list(URGENT_OFF_HOURS), format_func=URGENT_OFF_HOURS.get,
                                 index=list(URGENT_OFF_HOURS).index(cfg["urgent_off_hours"]), key="set_urgent_off",
                                 help="근무 시간 밖에 경고가 나면 지금 바로 출동시킬지, 다음 근무 시작에 배정할지 정합니다.")
        saved = st.form_submit_button("저장", type="primary")
    if saved:
        errors = save_settings({
            "work_start": f"{work_start:%H:%M}", "work_end": f"{work_end:%H:%M}",
            "lunch_start": f"{lunch_start:%H:%M}", "lunch_end": f"{lunch_end:%H:%M}",
            "prep_min": int(prep_min), "max_replan": int(max_replan), "urgent_check_min": int(urgent_min),
            "urgent_off_hours": off_hours,
        })
        if errors:
            st.error("저장하지 않았습니다.\n\n" + "\n".join(f"- {e}" for e in errors))
        else:
            st.success("저장했습니다. 다음 계획부터 적용됩니다.")

# ── 모델 정보 (읽기 전용) ──
with st.container(border=True):
    st.markdown("**모델 정보** · 읽기 전용 (수정 불가)")
    if not MODEL_PATH.exists():
        st.warning("모델 파일이 없습니다. ml/train.py로 학습하세요.")
    else:
        info = model_info(MODEL_PATH.stat().st_mtime)
        st.text_input("사용 중인 모델 파일", "models/lgbm.pkl", disabled=True, key="ro_model")
        left, right = st.columns(2)
        with left:
            st.caption(f"부품별 판단 임계값 · 규칙: {info['rule']}")
            st.dataframe(pd.DataFrame([{"부품": COMP_LABEL_KO.get(c, c), "임계값": t}
                                       for c, t in info["thresholds"].items()]),
                         hide_index=True, width="stretch",
                         column_config={"임계값": st.column_config.NumberColumn(format="%.2f")})
        with right:
            st.caption("학습·평가 기간 (끝 날짜는 포함하지 않음)")
            st.dataframe(pd.DataFrame([{"구간": PERIOD_KO.get(k, k), "시작": v["start"][:10], "끝": v["end"][:10]}
                                       for k, v in info["periods"].items()]), hide_index=True, width="stretch")

# ── 개발자 모드 ──
if (ss.get("user") or {}).get("allow_dev"):
    with st.container(border=True):
        st.markdown("**개발자 모드**")

        def _set_dev():
            ss.dev_mode = ss.dev_toggle

        st.toggle("개발자 로그 보기", value=ss.dev_mode, key="dev_toggle", on_change=_set_dev,
                  help="켜면 오늘의 분석 페이지 맨 아래에 실행 로그, 도구 호출 기록, 채팅 경로가 보입니다.")

# ── 데모 데이터 초기화 ──
with st.container(border=True):
    st.markdown("**데모 데이터 초기화** · 선택한 날짜의 결과 파일(초안, 등록, 배정안, 리포트, 예측)을 지웁니다.")
    c1, c2 = st.columns([1, 2])
    target = c1.date_input("날짜", value=ss.day, min_value=S.TEST_START, max_value=S.TEST_END, key="reset_day")
    confirm = c2.checkbox("이 날짜의 결과를 지우는 것을 확인합니다. 되돌릴 수 없습니다.", key="reset_confirm")
    if st.button("초기화", key="reset_btn", disabled=not confirm or S.job_running()):
        removed = records.clear_day(f"{target:%Y-%m-%d} 06:00:00")
        if target == ss.day:
            S.reset_day()
        st.success(f"{target:%Y-%m-%d} 결과 파일 {len(removed)}개를 지웠습니다." if removed
                   else f"{target:%Y-%m-%d}에는 지울 결과가 없습니다.")

    st.divider()
    st.markdown("**전체 초기화** · 모든 날짜의 결과 파일, 근거, 운영 DB, 대화 기록·장기 기억, 현장 기록 색인을 지우고 "
                "처음 상태로 되돌립니다. 지우기 전에 backups/에 zip으로 백업하며, 원본 데이터·모델·매뉴얼은 그대로 둡니다.")
    confirm_all = st.checkbox("전체 운영 기록을 지우는 것을 확인합니다.", key="reset_all_confirm")
    if st.button("전체 초기화", key="reset_all_btn", type="primary", disabled=not confirm_all or S.job_running()):
        from scripts.reset_runtime import SERVER_NOTICE, LockedError, reset_runtime

        try:
            with st.spinner("백업 후 초기화하는 중"):
                summary = reset_runtime()
        except LockedError as e:
            st.error(f"초기화 실패: {e}\n\n다른 Streamlit 서버나 스크립트가 같은 파일을 쓰고 있을 수 있습니다. {SERVER_NOTICE}")
        else:
            st.cache_data.clear()
            keep = {"user", "use_mock"}  # 로그인 상태만 남기고 화면 상태를 비운다
            for k in [k for k in ss if k not in keep]:
                del ss[k]
            ss.reset_all_summary = summary
            st.rerun()
    if summary := ss.pop("reset_all_summary", None):
        st.success(f"전체 초기화 완료 · 백업: {summary['backup']}")
        st.caption("지운 것: " + " · ".join(summary["deleted"]))
        st.caption("다시 만든 것: " + " · ".join(summary["recreated"]))
        if summary["skipped"]:
            st.caption("건너뛴 것: " + " · ".join(summary["skipped"]))

# ── 시스템 안내 ──
with st.expander("시스템 안내", expanded=False):
    st.markdown("""
| 순서 | 사용자가 하는 일 | 뒤에서 일어나는 일 | 화면 |
|---|---|---|---|
| 1 | 사이드바에서 기준 날짜 선택 | 모든 조회는 기준 시각(06:00) 이전 데이터만 사용 | 오늘의 분석 사이드바 |
| 2 | [오늘 점검 시작] | 총괄 에이전트가 요청을 받음 | 대시보드 |
| 3 | 기다림 | 예측 모델이 100대의 24시간 내 고장 위험 계산 → 고위험 설비 선정 | 대시보드 진행 상황 |
| 4 | 기다림 | 고위험 설비마다 조사 에이전트를 동시에 실행해 근거 수집 | 대시보드 진행 막대 |
| 5 | 설비 하나 선택 | 조사 결론, 센서 추이, 근거 번호 확인 | 설비별 조사 |
| 6 | 기다림 | 계획 에이전트가 점수 계산과 팀별 하루 일정 배치(코드 계산), 작업 지시·발주 초안 작성 | 대시보드 일정표 |
| 7 | 승인 또는 거절 후 [결정 제출] | 등록은 사람 승인 뒤에만 실행, 승인분만 등록 | 승인 |
| 8 | 필요하면 재계획 요청 | 거절 사유를 조건으로 해석해 다시 배정, 이전 초안은 대체됨 | 승인 |
| 9 | 기다림 | 리포트 에이전트가 인수인계 리포트 작성·저장 | 승인 |
| 10 | 채팅으로 질문 | 총괄 에이전트가 질문 종류를 보고 담당 에이전트 선택 | 오른쪽 아래 채팅 아이콘 |
| 11 | [다음 날로] | 검증 코드가 어제 예측과 실제 고장을 비교 (에이전트는 볼 수 없음) | 대시보드 하단 |
""")
    st.markdown("**구조**")
    st.code("""
            사용자 (버튼 = 정해진 메시지 / 채팅)
                         │
              ┌──────────▼──────────┐
              │  총괄 에이전트       │  입력 가드 → 사람 승인 → 대화 요약 → 호출 상한
              └──┬───┬───┬───┬───┬──┘
     ┌───────────┘   │   │   │   └──────────────┐
  고장 예측           │   │   │          작업 지시서 등록 (승인 필요)
                     │   │   │          발주 요청 등록 (승인 필요)
     설비 조사 ── 동시 실행 ──▶ 조사 에이전트 1 … K
                         │   │
          계획 에이전트   데이터 에이전트   리포트 에이전트
""", language=None)
