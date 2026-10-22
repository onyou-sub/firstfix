"""공통 CSS. app.py에서 실행마다 한 번만 주입한다 (inject_css)."""

import streamlit as st

NAVY = "#1F3A5F"
RED = "#C0392B"     # 위험
ORANGE = "#D9822B"  # 주의
GRAY = "#8A94A6"    # 정상·비활성
TEXT_DARK = "#1C2430"
# 작업 상태 간트: 일반은 초록, 긴급은 빨강 (배정 연함 -> 진행 중 중간 -> 완료 진함)
GREEN_LIGHT, GREEN_MID, GREEN_DARK = "#B7E4C7", "#52B788", "#1B6E42"
RED_LIGHT, RED_MID, RED_DARK = "#F5B7B1", "#E06C5F", "#A93226"

CSS = f"""
<style>
.block-container {{ padding-top: 2.2rem; }}
h1, h2, h3 {{ color: {NAVY}; letter-spacing: -0.01em; }}
.badge {{
  display: inline-block; padding: 0.08rem 0.55rem; border-radius: 0.6rem; font-size: 0.78rem;
  font-weight: 600; line-height: 1.4; border: 1px solid transparent; white-space: nowrap;
}}
.badge-navy   {{ color: {NAVY};   background: #E8EEF6; border-color: #C9D5E6; }}
.badge-red    {{ color: {RED};    background: #FBEAE8; border-color: #F1C4BF; }}
.badge-orange {{ color: #A85F14;  background: #FCF1E4; border-color: #F2D3AE; }}
.badge-gray   {{ color: #5B6474;  background: #F0F2F5; border-color: #DADFE6; }}
.card-title   {{ font-weight: 600; color: #1C2430; margin-bottom: 0.15rem; }}
.card-meta    {{ color: #5B6474; font-size: 0.85rem; }}
.muted        {{ color: {GRAY}; }}
.card-line    {{ line-height: 1.75; }}
.card-key     {{ display: inline-block; width: 4.2rem; color: #5B6474; font-size: 0.85rem; }}
.finding-line {{ line-height: 1.9; }}
.finding-ev   {{ color: #5B6474; font-size: 0.8rem; }}
.report-toggle {{ border: 1px solid #DADFE6; border-radius: 0.4rem; padding: 0.35rem 0.6rem; margin: 0.3rem 0; }}
.report-toggle summary {{ cursor: pointer; color: #1C2430; }}
.report-memo  {{ margin-top: 0.4rem; white-space: normal; line-height: 1.6; }}
.report-extra {{ margin-top: 0.4rem; color: #3A4352; font-size: 0.9rem; }}
.login-brand  {{ text-align: center; color: {NAVY}; font-size: 1.45rem; font-weight: 700; margin: 0.4rem 0 0.2rem; }}
.login-sub    {{ text-align: center; color: #5B6474; font-size: 0.9rem; margin-bottom: 0.8rem; }}
.user-box     {{ color: #1C2430; font-size: 0.9rem; }}
.alert-banner {{ color: #fff; background: {RED}; border-radius: 0.4rem; padding: 0.55rem 0.9rem; font-weight: 600;
                margin-bottom: 0.6rem; line-height: 1.5; }}
.urgent-time  {{ color: {RED}; font-weight: 700; font-size: 1.05rem; }}
[data-testid="stMetricValue"] {{ color: {NAVY}; }}
/* 사이드바 알림 (약 340px). 카드 = st.container(key="noti_{{tier}}_{{id}}"): 긴급 / 할 일 묶음 / 묶음 안 항목 / 한 줄 */
section[data-testid="stSidebar"] {{ width: 340px !important; min-width: 340px !important; }}
.nbadge {{ display: inline-block; padding: 0 0.4rem; border-radius: 0.35rem; font-size: 0.72rem; font-weight: 600;
          line-height: 1.5; white-space: nowrap; color: #fff; vertical-align: 1px; }}
.nbadge-red   {{ background: {RED}; }}
.nbadge-navy  {{ background: {NAVY}; }}
.nbadge-green {{ background: {GREEN_DARK}; }}
.nbadge-gray  {{ background: {GRAY}; }}
.noti-top  {{ font-size: 0.86rem; color: #1C2430; line-height: 1.5; display: flex; gap: 0.35rem; align-items: center; }}
.noti-top b {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.noti-ago  {{ margin-left: auto; color: #5B6474; font-size: 0.75rem; white-space: nowrap; }}
.noti-line {{ font-size: 0.82rem; color: #3A4352; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.noti-more {{ color: #5B6474; font-size: 0.75rem; }}
/* 알림 카드: st.container(border=True, key="noti_{{tier}}_{{kind}}_{{id}}"). Streamlit 1.66에서 key 클래스(st-key-…)는
   테두리를 가진 div[data-testid="stVerticalBlock"] 자체에 붙는다 (버튼 key는 stElementContainer라 아래 선택자에서 빠짐).
   기본 테두리 위에 종류별 왼쪽 띠·배경만 덧입힌다. */
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: 0.4rem; }}
[data-testid="stVerticalBlock"][class*="st-key-noti_"]:not([class*="st-key-noti_item_"]) {{
  padding: 0.4rem 0.55rem; border-radius: 0.45rem; border-left-width: 5px; border-left-style: solid;
  background: #fff; gap: 0.15rem; transition: border-color 0.15s; }}
[data-testid="stVerticalBlock"][class*="st-key-noti_"]:not([class*="st-key-noti_item_"]):hover {{
  border-top-color: #8A94A6; border-right-color: #8A94A6; border-bottom-color: #8A94A6; }}
[data-testid="stVerticalBlock"][class*="st-key-noti_urgent_"] {{ border-color: #F1C4BF; border-left-color: {RED};
  background: #FDF1F0 !important; }}
[data-testid="stVerticalBlock"][class*="st-key-noti_urgent_"]:hover {{
  border-top-color: {RED}; border-right-color: {RED}; border-bottom-color: {RED}; }}
[data-testid="stVerticalBlock"][class*="st-key-noti_todo_"] {{ border-left-color: {NAVY}; }}
[data-testid="stVerticalBlock"][class*="st-key-noti_plain_done_"] {{ border-left-color: {GREEN_DARK}; }}
[data-testid="stVerticalBlock"][class*="st-key-noti_plain_assign_"] {{ border-left-color: {RED_MID}; }}
[data-testid="stVerticalBlock"][class*="st-key-noti_plain_finding_"],
[data-testid="stVerticalBlock"][class*="st-key-noti_plain_notice_"] {{ border-left-color: {GRAY}; }}
/* 할 일 묶음: 머리(expander 요약줄)가 카드 첫 줄, 펼친 항목은 얇은 구분선으로 나뉜 두 줄 */
[class*="st-key-noti_todo_"] [data-testid="stExpander"] details {{ border: none; background: transparent; }}
[class*="st-key-noti_todo_"] [data-testid="stExpander"] summary {{ padding: 0.1rem 0; }}
[class*="st-key-noti_todo_"] summary code {{ background: {NAVY}; color: #fff; border-radius: 0.35rem;
                                             padding: 0 0.4rem; font-family: inherit; font-size: 0.72rem; }}
[data-testid="stVerticalBlock"][class*="st-key-noti_item_"] {{ border-top: 1px solid #E3E7ED; padding-top: 0.35rem;
  gap: 0.1rem; }}
[class*="st-key-noti_"] button {{ min-height: 1.6rem; padding: 0 0.2rem; }}
/* 작업자 주간 보고서 본문: 보고서 한 장처럼 흰 카드, 최대 900px 가운데 */
.st-key-weekly_doc {{ max-width: 900px; margin: 0.5rem auto; background: #fff; border: 1px solid #DADFE6;
                     border-radius: 0.5rem; padding: 1.4rem 1.6rem; }}
.st-key-weekly_doc h4 {{ border-bottom: 2px solid {NAVY}; padding-bottom: 0.2rem; margin-top: 0.6rem; }}
/* 오늘의 분석: 오른쪽 아래 채팅 아이콘 (Material 아이콘, 56px 원형) + 메시지 수 / 승인 대기 점 */
.st-key-chat_fab {{ position: fixed; right: 24px; bottom: 24px; z-index: 1000; width: 56px !important; }}
.st-key-chat_fab button {{ width: 56px; height: 56px; min-height: 56px; border-radius: 50%; padding: 0;
                          background: {NAVY}; border: none; color: #fff; box-shadow: 0 4px 12px rgba(0,0,0,0.18); }}
.st-key-chat_fab button:hover, .st-key-chat_fab button:focus {{ background: #2C4E7A; color: #fff; border: none; }}
.st-key-chat_fab button span {{ color: #fff; font-size: 1.6rem; }}
.chat-fab-count {{ position: fixed; right: 20px; bottom: 66px; z-index: 1001; min-width: 20px; height: 20px;
                  padding: 0 5px; border-radius: 10px; background: #fff; color: {NAVY}; border: 1px solid {NAVY};
                  font-size: 0.72rem; font-weight: 700; line-height: 18px; text-align: center; }}
.chat-fab-dot {{ position: fixed; right: 70px; bottom: 68px; z-index: 1001; width: 12px; height: 12px;
                border-radius: 50%; background: {ORANGE}; border: 2px solid #fff; }}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
