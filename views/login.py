"""로그인 페이지 (로그인 전에는 이 페이지만 메뉴에 있다).

- 데모용: secrets.toml 계정에 demo_password가 있으면 로그인 폼 옆에 아이디·비밀번호를 안내한다 (없으면 안내 없음).
"""

import streamlit as st

from ui import auth

ss = st.session_state
ROLE_KO = {"admin": "관리자", "worker": "작업자"}


def _secrets() -> dict:
    try:
        return st.secrets.to_dict()
    except Exception:  # secrets.toml 없음
        return {}


def _demo_accounts(secrets: dict) -> list[dict]:
    users = (secrets.get("auth") or {}).get("users") or {}
    rows = [{"구분": ROLE_KO.get(u.get("role"), u.get("role")) + (f" ({u['team']})" if u.get("team") else ""),
             "아이디": uid, "비밀번호": u["demo_password"]}
            for uid, u in users.items() if u.get("demo_password") and u.get("role") in ROLE_KO]
    return sorted(rows, key=lambda r: (r["구분"] != "관리자", r["아이디"]))


secrets = _secrets()
demo = _demo_accounts(secrets)

_, mid, side = st.columns([1, 1.2, 1])
with mid:
    st.write("")
    st.write("")
    with st.container(border=True):
        st.markdown('<div class="login-brand">설비 정비 에이전트</div>'
                    '<div class="login-sub">고장 예측 · 정비 우선순위 · 승인</div>', unsafe_allow_html=True)
        with st.form("login_form", border=False):
            user_id = st.text_input("아이디", key="login_id")
            password = st.text_input("비밀번호", type="password", key="login_pw")
            submitted = st.form_submit_button("로그인", type="primary", width="stretch")
        if submitted:
            if not secrets.get("auth"):
                st.error("계정 정보가 없습니다. .streamlit/secrets.toml을 만드세요 (secrets.example.toml 참고).")
            else:
                user, message = auth.login(secrets, user_id, password)
                if user:
                    ss.user = user
                    st.rerun()  # 로그인 후에는 기본 페이지(오늘의 분석)로 열린다
                st.error(message)

if demo:
    with side:
        st.write("")
        st.write("")
        with st.container(border=True, key="demo_accounts"):
            st.markdown("**데모 계정**")
            st.caption("관리자는 점검·승인, 작업자는 자기 팀 작업 처리 화면으로 들어갑니다.")
            st.table(demo)
