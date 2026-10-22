"""
설비 고장 예측·정비 우선순위 에이전트 — 화면 진입점

- 로그인 전: 로그인 페이지만 (다른 페이지는 메뉴에도 없고 주소로도 열리지 않음)
- 로그인 후 (관리자): 오늘의 분석(대시보드에 실시간 감지 카드) / 작업 현황 / 이력·보고서 / 설정 / 알림 (+ 상단 실시간 경고 배너)
- 로그인 후 (작업자): 대시보드 / 주간 보고서 / 오늘 작업 / 작업 이력 / 알림 (자기 팀만)
- 사이드바 맨 위에 알림(긴급 / 할 일 / 정보 묶음, 전체 보기는 알림 페이지), 하단에 사용자 이름과 [로그아웃]
- USE_MOCK = False: services.py를 통해 실제 에이전트를 실행합니다.
- USE_MOCK = True : ui/mock.py의 가짜 데이터로 흐름만 보여줍니다 (데모 백업용, LLM 호출 없음).
  환경 변수 USE_MOCK=1 로도 켤 수 있습니다.
- 계정: .streamlit/secrets.toml (예시: .streamlit/secrets.example.toml, 해시: scripts/make_password_hash.py)
- 실행:  uv run streamlit run app.py
"""
import os

import streamlit as st

from ui import state as S
from ui.components import alert_banner, notification_panel
from ui.style import inject_css

USE_MOCK = False
if os.getenv("USE_MOCK", "").lower() in ("1", "true"):
    USE_MOCK = True

st.set_page_config(layout="wide", page_title="설비 정비 에이전트")
inject_css()
st.session_state.use_mock = USE_MOCK


def logout() -> None:
    keep = {"use_mock"}
    for k in [k for k in st.session_state if k not in keep]:
        del st.session_state[k]


user = st.session_state.get("user")
if not user:
    page = st.navigation([st.Page("views/login.py", title="로그인", default=True)], position="hidden")
    page.run()
else:
    S.init()
    if user["role"] == "worker":  # 작업자: 자기 팀 작업만
        pages = [st.Page("views/worker_dashboard.py", title="대시보드", default=True),
                 st.Page("views/worker_report.py", title="주간 보고서", url_path="worker_report"),
                 st.Page("views/worker_tasks.py", title="오늘 작업", url_path="worker_tasks"),
                 st.Page("views/worker_history.py", title="작업 이력", url_path="worker_history"),
                 st.Page("views/notifications.py", title="알림", url_path="notifications")]
        target = "views/worker_tasks.py"
    else:
        if S.shared_day() is None:  # 공유 기준 날짜가 아직 없으면 관리자의 현재 날짜로 시작
            S.publish_day()
        pages = [st.Page("views/today.py", title="오늘의 분석", default=True),
                 st.Page("views/work_status.py", title="작업 현황", url_path="work_status"),
                 st.Page("views/history.py", title="이력·보고서", url_path="history"),
                 st.Page("views/settings.py", title="설정", url_path="settings"),
                 st.Page("views/notifications.py", title="알림", url_path="notifications")]
        target = "views/work_status.py"
    page = st.navigation(pages)
    with st.sidebar:  # 사이드바 맨 위: 알림 (10초마다 이 부분만 다시 확인)
        notification_panel(user, target)
    if user["role"] != "worker":  # 관리자 공통 상단: 열린 경고 빨간 배너 (5초마다 이 부분만 다시 확인)
        alert_banner(S.as_of_text())
    page.run()
    with st.sidebar:  # 페이지의 사이드바 내용 아래에 붙는다
        st.divider()
        st.markdown(f'<div class="user-box"><b>{user["name"]}</b> · {user["id"]}</div>', unsafe_allow_html=True)
        st.button("로그아웃", key="logout_btn", on_click=logout, width="stretch", disabled=S.job_running())
