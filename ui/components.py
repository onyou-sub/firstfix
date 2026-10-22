"""공통 화면 부품: 상태 배지, 지표 카드, 승인 카드, 간트 차트, 일정표·배정표, 검증표."""
from __future__ import annotations

import html
from datetime import date, datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config import COMP_LABEL_KO
from ui.style import (
    GRAY, GREEN_DARK, GREEN_LIGHT, GREEN_MID, NAVY, ORANGE, RED, RED_DARK, RED_LIGHT, RED_MID, TEXT_DARK,
)

STATUS_KO = {"APPROVED": "승인", "REJECTED": "거절", "PENDING": "대기", "SUPERSEDED": "대체됨"}
BADGE_CLASS = {
    "승인": "navy", "등록됨": "navy", "적중": "navy", "예측 고장": "red", "거절": "red", "놓침": "red",
    "대기": "orange", "사람 확인": "orange", "헛경보": "orange", "센서·설치 이상 가능성": "orange",
    "대체됨": "gray", "내일 우선": "gray", "제외": "gray",
}
TASK_COLOR = {"교체": NAVY, "임시 점검": ORANGE, "센서 점검": GRAY, "긴급": RED}
LEVEL_KIND = {"경고": "red", "사람 확인": "orange", "주의": "orange", "정상": "gray"}
PROB_FORMAT = "%.2f"
COMP_KO = COMP_LABEL_KO


# ---------------------------------------------------------------- 배지·카드
def badge_html(text: str, kind: str | None = None) -> str:
    """상태 배지 HTML. kind(navy/red/orange/gray)를 주지 않으면 글자에 맞는 색."""
    return f'<span class="badge badge-{kind or BADGE_CLASS.get(text, "gray")}">{html.escape(text)}</span>'


def badge(text: str) -> None:
    st.markdown(badge_html(text), unsafe_allow_html=True)


def status_text(status: str) -> str:
    return STATUS_KO.get(status, status)


def kpi_cards(items: list[tuple[str, str | int, str | None]], columns: int | None = None) -> None:
    """[(라벨, 값, 도움말)]을 테두리 카드 안의 지표로 나란히."""
    cols = st.columns(columns or len(items))
    for i, (label, value, help_text) in enumerate(items):
        with cols[i % len(cols)].container(border=True):
            st.metric(label, value, help=help_text)


def draft_header(d: dict, status: str | None = None, hide_team: bool = False) -> None:
    """초안 카드 4줄: 설비 · 작업 · 팀·시간 · 할 일(굵게). 상태 배지는 첫 줄 오른쪽에 작게.
    초안 번호(d["id"])는 내부 키로만 쓰고 화면에 보이지 않는다.
    hide_team=True(팀 구역 안 카드): 팀은 구역 머리에 있으므로 "팀·시간" 줄을 "시간"으로.
    발주는 팀·시간 자리에 희망 발주일, 할 일 자리에 예상 입고(늦게 발주하면 "리드타임 N일 → 예상 입고 MM-DD")."""
    e = html.escape
    right = f" &nbsp; {badge_html(status)}" if status else ""
    when = d.get("when") or "-"
    if hide_team and d.get("team"):
        when_row = ("시간", e(when.removeprefix(d["team"]).strip() or "-"))
    else:
        when_row = ("팀·시간", e(when))
    rows = [
        ("설비", e(d.get("machine") or d.get("title") or "-")),
        ("작업", e(d.get("task") or d.get("type") or "-")),
        when_row,
        ("할 일", f"<b>{e(d.get('todo') or '-')}</b>"),
    ]
    lines = [f'<div class="card-line"><span class="card-key">{k}</span>{v}'
             + (right if i == 0 else "") + "</div>" for i, (k, v) in enumerate(rows)]
    st.markdown("".join(lines), unsafe_allow_html=True)


def approval_card(d: dict, key: str, hide_team: bool = False) -> str | None:
    """승인 대기 카드 (4줄 + 승인/거절). 고른 값("승인"/"거절") 또는 None."""
    with st.container(border=True):
        draft_header(d, "대기", hide_team=hide_team)
        return st.radio("결정", ["승인", "거절"], index=None, horizontal=True, key=key, label_visibility="collapsed")


def decided_card(d: dict, status: str, hide_team: bool = False) -> None:
    with st.container(border=True):
        draft_header(d, status, hide_team=hide_team)


# ---------------------------------------------------------------- 초안 팀별 구역 (승인 탭)
NON_TEAM_GROUPS = ("미배정", "발주")  # 팀 구역 아래 둘째 줄
TEAMS_PER_ROW = 3


def _team_order(name: str) -> tuple:
    digits = "".join(ch for ch in name if ch.isdigit())
    return (int(digits) if digits else 10**6, name)


def draft_group(d: dict) -> str:
    """카드가 들어갈 구역: 팀 이름 / 미배정(일정에 못 들어간 작업 지시) / 발주."""
    return d.get("group") or ("발주" if d.get("type", "").startswith("발주") else d.get("team") or "미배정")


def group_sections(drafts: list[dict]) -> list[list[tuple[str, list[dict]]]]:
    """구역 배치: [[(팀, 카드들), ...] 줄마다 최대 3개, ..., [(미배정, ...), (발주, ...)]].
    팀은 이름 순(1팀, 2팀, ..., 10팀), 카드는 시작 시각 순. 카드가 없는 구역은 뺀다."""
    groups: dict[str, list[dict]] = {}
    for d in drafts:
        groups.setdefault(draft_group(d), []).append(d)
    for cards in groups.values():
        cards.sort(key=lambda d: (d.get("start") is None, d.get("start") or ""))
    teams = sorted((g for g in groups if g not in NON_TEAM_GROUPS), key=_team_order)
    rows = [[(t, groups[t]) for t in teams[i:i + TEAMS_PER_ROW]] for i in range(0, len(teams), TEAMS_PER_ROW)]
    rest = [(g, groups[g]) for g in NON_TEAM_GROUPS if g in groups]
    return rows + ([rest] if rest else [])


def rerun_fragment() -> None:
    """fragment(대화창 포함) 안에서 그 부분만 다시 그린다. 페이지 전체 실행 중에는 fragment rerun을 쓸 수 없어
    (첫 실행·AppTest) 전체를 다시 실행한다."""
    from streamlit.errors import StreamlitAPIException

    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()


# ---------------------------------------------------------------- 일정
def _at(day: date, hm: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hm, "%H:%M").time())


def schedule_figure(plan: pd.DataFrame, day: date, cfg: dict, urgent: list[dict] | None = None) -> go.Figure | None:
    """팀별 하루 일정 간트 차트 (y=팀, x=근무 시간, 색=작업 종류, 점심시간 회색 띠).
    urgent: 그날 긴급 작업 지시(work_orders, urgent=1) — 빨간색 "긴급"으로 함께 그린다."""
    rows = plan[plan["team"].notna()] if "team" in plan else plan.iloc[0:0]
    urgent = [w for w in (urgent or []) if w.get("team") and w.get("start") and w.get("end")]
    if rows.empty and not urgent:
        return None
    df = pd.DataFrame() if rows.empty else pd.DataFrame({  # 배정안 없이 긴급 작업만 있는 날은 빈 표에서 시작
        "팀": rows["team"], "시작": rows["start"].map(lambda t: _at(day, t)), "종료": rows["end"].map(lambda t: _at(day, t)),
        "작업": rows["task"], "라벨": "#" + rows["machine_id"].astype(str) + " " + rows["comp"],
        "점수": rows["score"].round(2), "분류": rows["label"], "사유": rows["reason"] if "reason" in rows else "-",
    })
    if urgent:
        df = pd.concat([df, pd.DataFrame({
            "팀": [w["team"] for w in urgent], "시작": [_at(day, w["start"]) for w in urgent],
            "종료": [_at(day, w["end"]) for w in urgent], "작업": "긴급",
            "라벨": [f"긴급 #{w['machine_id']} {w['comp']}" for w in urgent], "점수": float("nan"),
            "분류": [w["title"] for w in urgent], "사유": "경고 자동 배정 (승인 없음)",
        })], ignore_index=True)
    teams = sorted(df["팀"].unique())
    fig = px.timeline(df, x_start="시작", x_end="종료", y="팀", color="작업", text="라벨",
                      color_discrete_map=TASK_COLOR, category_orders={"팀": teams},
                      hover_data={"점수": ":.2f", "분류": True, "사유": True, "팀": False, "작업": True})
    fig.add_vrect(x0=_at(day, cfg["lunch_start"]), x1=_at(day, cfg["lunch_end"]), fillcolor="#B0B7C3", opacity=0.3,
                  line_width=0, annotation_text="점심", annotation_position="top left")
    fig.update_xaxes(range=[_at(day, cfg["work_start"]), _at(day, cfg["work_end"])], tickformat="%H:%M",
                     dtick=3600000, title=None)
    fig.update_yaxes(title=None, autorange="reversed")
    fig.update_traces(textposition="inside", insidetextanchor="middle")
    fig.update_layout(height=120 + 60 * len(teams), margin=dict(l=0, r=0, t=10, b=0), legend_title_text="작업",
                      plot_bgcolor="white")
    return fig


def schedule_table(plan: pd.DataFrame) -> None:
    """팀·시작 시각 순 일정표 (일정에 들어간 작업만)."""
    rows = plan[plan["team"].notna()].sort_values(["team", "start"]) if "team" in plan else plan.iloc[0:0]
    if rows.empty:
        return
    show = pd.DataFrame({
        "팀": rows["team"], "시간": rows["start"] + "-" + rows["end"], "설비": rows["machine_id"],
        "부품": rows["comp_name"], "작업": rows["task"], "고장 확률": rows["prob"], "중요도": rows["criticality"],
        "점수": rows["score"], "배정 사유": rows["reason"] if "reason" in rows else "-",
    })
    st.dataframe(show, hide_index=True, width="stretch",
                 column_config={"설비": st.column_config.NumberColumn(format="#%d"),
                                "고장 확률": st.column_config.NumberColumn(format=PROB_FORMAT),
                                "점수": st.column_config.NumberColumn(format="%.2f")})


def verify_table(df: pd.DataFrame) -> None:
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"설비": st.column_config.NumberColumn(format="#%d")})


def add_now_line(fig: go.Figure, day: date, cfg: dict, now: str) -> bool:
    """간트에 "지금" 세로선(빨간 점선). 근무 시간 밖이면 그리지 않는다. 그렸으면 True.
    (add_vline의 annotation은 날짜 축에서 오류가 나서 선과 글자를 따로 넣는다)"""
    if not cfg["work_start"] <= now < cfg["work_end"]:
        return False
    x = _at(day, now)
    fig.add_shape(type="line", x0=x, x1=x, y0=0, y1=1, yref="paper", line=dict(color=RED, width=1.5, dash="dot"))
    fig.add_annotation(x=x, y=1, yref="paper", text=f"지금 {now}", showarrow=False, yanchor="bottom",
                       font=dict(color=RED, size=11))
    return True


# ---------------------------------------------------------------- 작업 상태 간트 (작업 현황)
# 구분 = ("긴급 " if 긴급) + 상태 -> (칸 색, 글자색). 순서가 범례 순서
GANTT_COLOR = {
    "배정": (GREEN_LIGHT, TEXT_DARK), "진행 중": (GREEN_MID, "white"), "완료": (GREEN_DARK, "white"),
    "긴급 배정": (RED_LIGHT, TEXT_DARK), "긴급 진행 중": (RED_MID, "white"), "긴급 완료": (RED_DARK, "white"),
}


def status_gantt(orders: list[dict], day: date, cfg: dict) -> go.Figure | None:
    """작업 지시 간트 차트 (y=팀, 색=상태+긴급 여부: 일반 초록 / 긴급 빨강, 배정 연함 -> 진행 중 -> 완료 진함)."""
    from store.app_db import WO_STATUS_KO

    rows = [w for w in orders if w.get("team") and w.get("start") and w.get("end")]
    if not rows:
        return None
    df = pd.DataFrame({
        "팀": [w["team"] for w in rows], "시작": [_at(day, w["start"]) for w in rows],
        "종료": [_at(day, w["end"]) for w in rows], "상태": [WO_STATUS_KO[w["status"]] for w in rows],
        "긴급": ["긴급" if w.get("urgent") else "일반" for w in rows],
        "라벨": [f"{'긴급 ' if w.get('urgent') else ''}#{w['machine_id']} {w['comp']}" for w in rows],
        "작업 지시": [w["wo_id"] for w in rows],
        "시각": [f"{w['start']}~{w['end']}" + (f" (긴급으로 중단 {w['paused']})" if w.get("paused") else "")
                 for w in rows],
    })
    df["구분"] = [("긴급 " if u == "긴급" else "") + s for u, s in zip(df["긴급"], df["상태"])]
    teams = sorted(df["팀"].unique())
    fig = px.timeline(df, x_start="시작", x_end="종료", y="팀", color="구분", text="라벨",
                      color_discrete_map={k: c for k, (c, _) in GANTT_COLOR.items()},
                      category_orders={"팀": teams, "구분": list(GANTT_COLOR)},
                      custom_data=["상태", "긴급", "작업 지시", "시각"])
    fig.for_each_trace(lambda t: t.update(
        textfont_color=GANTT_COLOR.get(t.name, (None, TEXT_DARK))[1],
        hovertemplate="%{text}<br>상태 %{customdata[0]} · %{customdata[1]}<br>작업 지시 %{customdata[2]}"
                      "<br>%{customdata[3]}<extra></extra>"))
    fig.add_vrect(x0=_at(day, cfg["lunch_start"]), x1=_at(day, cfg["lunch_end"]), fillcolor="#B0B7C3", opacity=0.3,
                  line_width=0, annotation_text="점심", annotation_position="top left")
    fig.update_xaxes(range=[_at(day, cfg["work_start"]), _at(day, cfg["work_end"])], tickformat="%H:%M",
                     dtick=3600000, title=None)
    fig.update_yaxes(title=None, autorange="reversed")
    fig.update_traces(textposition="inside", insidetextanchor="middle")
    fig.update_layout(height=120 + 60 * len(teams), margin=dict(l=0, r=0, t=10, b=0), legend_title_text="구분",
                      plot_bgcolor="white")
    return fig


# ---------------------------------------------------------------- 실시간 경고 배너 (관리자 공통 상단, 5초마다 확인)
URGENT_PREFIXES = ("긴급 점검:", "센서 점검:")  # live_ops.dispatch_urgent가 보내는 작업자 알림 제목


def is_urgent_notice(item: dict) -> bool:
    if item.get("kind"):
        return item["kind"] == "urgent"
    return str(item.get("title", "")).startswith(URGENT_PREFIXES)


@st.fragment(run_every=5)
def alert_banner(as_of_text: str) -> None:
    """열린 경고가 있으면 빨간 배너: "경고 2건 — #45 베어링 0.06 (2팀 09:00 | 출동 확인 대기) · ..."
    fragment라서 5초마다 이 부분만 다시 그린다 (조사 중 -> 점검 배정 -> 출동 -> 점검 완료가 바로 보이게)."""
    import live_ops

    try:
        alerts = [a for a in live_ops.open_alerts(as_of_text) if a["level"] in live_ops.DISPATCH_LEVELS]
    except Exception as e:  # 운영 DB 오류가 화면 전체를 막지 않게
        st.caption(f"경고 상태를 읽지 못했습니다: {e}")
        return
    ids = {a["alert_id"] for a in alerts}
    if not ids - st.session_state.get("dismissed_alerts", set()):  # 닫은 경고만 남았으면 숨김 (새 경고가 오면 다시 보임)
        return
    parts = [f"#{a['machine_id']} {COMP_LABEL_KO.get(a['comp'], a['comp'])} {a['prob'] or 0:.2f}"
             f" ({a['level']} · {live_ops.alert_progress(a)})" for a in alerts]
    left, right = st.columns([20, 1], vertical_alignment="center")
    left.markdown(f'<div class="alert-banner">경고 {len(alerts)}건 — {html.escape(" · ".join(parts))}</div>',
                  unsafe_allow_html=True)
    if right.button("✕", key="alert_banner_close", help="배너 닫기 (새 경고가 생기면 다시 보입니다)"):
        st.session_state.dismissed_alerts = st.session_state.get("dismissed_alerts", set()) | ids
        st.rerun(scope="fragment")


# ---------------------------------------------------------------- 알림 (사이드바 상단, 10초마다 확인)
NOTIFICATIONS_PAGE = "views/notifications.py"
NOTI_BADGE = {  # 종류 배지 클래스: 긴급 빨강 / 새 작업·승인 대기·입고 남색 / 완료 초록 / 조사 결과·배정·안내 회색
    "urgent": "red", "task": "navy", "approval": "navy", "stock": "navy", "done": "green",
    "finding": "gray", "assign": "gray", "notice": "gray",
}


def noti_badge(item: dict) -> str:
    return f'<span class="nbadge nbadge-{NOTI_BADGE.get(item["kind"], "gray")}">{html.escape(item["label"])}</span>'


def _open_notification(item: dict, user: dict, target_page: str) -> None:
    """알림 열기: 그 카드에 묶인 알림을 모두 읽음 처리하고 연결 작업 화면으로 이동."""
    from store import app_db

    app_db.mark_read_many(item.get("ids") or [item["id"]], user)
    if item.get("link_wo_id"):
        st.session_state.focus_wo = item["link_wo_id"]
    st.switch_page(target_page)


def _ago(stamp: str | None) -> str:
    """"방금" / "N분 전" / "N시간 전" / "어제" / "MM-DD" (알림 시각은 실제 저장 시각)."""
    try:
        t = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return "-"
    now = datetime.now()
    sec = (now - t).total_seconds()
    if sec < 60:
        return "방금"
    if sec < 3600:
        return f"{int(sec // 60)}분 전"
    if t.date() == now.date():
        return f"{int(sec // 3600)}시간 전"
    if (now.date() - t.date()).days == 1:
        return "어제"
    return f"{t:%m-%d}"


def _hover(item: dict) -> str:
    """마우스를 올리면 보이는 정확한 시각과 body."""
    stamp = (item.get("created_at") or "").replace("T", " ")
    return f"{stamp}\n{item['body']}" if item.get("body") else stamp


def noti_key(tier: str, kind: str, ident) -> str:
    """알림 카드 container key: noti_{tier}_{kind}_{id}. CSS가 [data-testid="stVerticalBlock"][class*="st-key-noti_…"]로
    띠 색을 고른다. Streamlit은 key의 한글을 클래스에서 빼므로(2팀 -> 2-) id 자리는 ASCII만 쓴다."""
    safe = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "" for ch in str(ident)) or "all"
    return f"noti_{tier}_{kind}_{safe}"


def _card_head(item: dict) -> None:
    """카드 1줄: [배지] head(굵게) ... 상대 시각 (정확한 시각과 body는 마우스를 올리면)."""
    e = html.escape
    tip = e(_hover(item), quote=True)
    st.markdown(f'<div class="noti-top" title="{tip}">{noti_badge(item)} <b>{e(item["head"] or "-")}</b>'
                f'<span class="noti-ago">{_ago(item["created_at"])}</span></div>', unsafe_allow_html=True)


def _card_line(item: dict) -> str:
    e = html.escape
    extra = f' <span class="noti-more">{e(item["extra"])}</span>' if item.get("extra") else ""
    return (f'<div class="noti-line" title="{e(_hover(item), quote=True)}">{e(item["line"] or "")}{extra}</div>')


def _card(item: dict, tier: str, user: dict, target_page: str) -> None:
    """긴급 카드(테두리) / 할 일 묶음 안 항목(구분선): 두 줄 + 오른쪽 아래 작은 [열기]."""
    with st.container(border=tier != "item", key=noti_key(tier, item["kind"], item["id"])):
        _card_head(item)
        left, right = st.columns([4, 1.1], vertical_alignment="center")
        left.markdown(_card_line(item), unsafe_allow_html=True)
        if right.button("열기", key=f"noti_open_{item['id']}", type="tertiary", help=_hover(item)):
            _open_notification(item, user, target_page)


def _plain(item: dict, user: dict, target_page: str) -> None:
    """완료·조사 결과·배정·안내 카드: 긴급 카드와 같은 두 줄 모양(색만 다름) + [확인](읽음 -> 사라짐),
    연결 작업이 있으면 [열기]도."""
    from store import app_db

    with st.container(border=True, key=noti_key("plain", item["kind"], item["id"])):
        _card_head(item)
        if item.get("link_wo_id"):
            left, mid, right = st.columns([3, 1.1, 1.1], vertical_alignment="center")
            if mid.button("열기", key=f"noti_go_{item['id']}", type="tertiary", help=_hover(item)):
                _open_notification(item, user, target_page)
        else:
            left, right = st.columns([4, 1.1], vertical_alignment="center")
        left.markdown(_card_line(item), unsafe_allow_html=True)
        if right.button("확인", key=f"noti_ok_{item['id']}", type="tertiary", help="읽음으로 표시합니다 (목록에서 사라짐)."):
            app_db.mark_read_many(item["ids"], user)
            rerun_fragment()


@st.fragment(run_every=10)
def notification_panel(user: dict, target_page: str) -> None:
    """사이드바 알림: 안 읽은 알림만 모두, 확인하면 사라진다. 긴급은 출동 확인(처리) 전까지 남는다.
    긴급 카드(빨강) / 새 작업·승인 대기·입고 묶음 카드(남색 띠, 펼치면 항목) / 완료·조사 결과·배정·안내 한 줄(회색).
    fragment라서 10초마다 이 부분만 다시 그린다. 작업자는 같은 주기로 공유 기준 날짜를 확인해서 관리자가 날짜를 바꾸거나
    새 긴급 알림이 오면 페이지 전체를 다시 그린다. 새 한 줄 알림은 처음 보일 때 st.toast 한 번."""
    from store import app_db

    ss = st.session_state
    try:
        if user.get("role") == "worker":  # 관리자가 기준 날짜를 바꾸면 작업자 화면 전체를 새 날짜로 다시 그린다
            cur = app_db.current_day()
            seen = ss.get("seen_day")
            if cur and seen and cur["day"] != seen:
                ss.seen_day = cur["day"]
                st.rerun(scope="app")
        day = f"{ss.day:%Y-%m-%d}" if ss.get("day") else (app_db.current_day() or {}).get("day")
        box = app_db.inbox(user, day)
        if user.get("role") == "worker":  # 새 긴급 점검 알림이 오면 페이지 전체를 다시 그려 [출동 확인] 상자를 띄운다
            urgent = {i for it in box["urgent"] for i in it["ids"]}
            known = ss.setdefault("known_urgent", set())
            if urgent - known:
                known |= urgent
                st.rerun(scope="app")
    except Exception as e:  # 운영 DB 오류가 화면 전체를 막지 않게
        st.caption(f"알림을 읽지 못했습니다: {e}")
        return

    if "toasted_ids" not in ss:  # 처음 그릴 때 이미 있던 알림은 토스트하지 않는다 (로그인 직후 쏟아지지 않게)
        ss.toasted_ids = {it["id"] for it in box["plain"]}
    for it in box["plain"]:
        if it["id"] not in ss.toasted_ids:
            ss.toasted_ids.add(it["id"])
            st.toast(f"{it['label']}: {it['head']} · {it['line']}")

    unread = box["unread"]
    head, btn = st.columns([3, 2], vertical_alignment="center")
    head.markdown(f"**알림** {badge_html(f'안 읽음 {unread}', 'red' if unread else 'gray')}", unsafe_allow_html=True)
    if btn.button("모두 읽음", key="notiread_all", type="tertiary", disabled=unread == len(box["urgent"]),
                  help="긴급 알림은 출동 확인 후 사라집니다."):
        app_db.mark_all_read(user)
        rerun_fragment()

    for it in box["urgent"]:
        _card(it, "urgent", user, target_page)
    for g in box["todo_groups"]:
        team_id = "".join(ch for ch in (g["team"] or "") if ch.isdigit()) or "all"  # "1팀" -> 1
        with st.container(border=True, key=noti_key("todo", g["kind"], team_id)):
            with st.expander(f"● `{g['label']}` {g['title']}", expanded=False):  # 코드 글자 = 남색 배지 (CSS)
                for it in g["items"]:
                    _card(it, "item", user, target_page)
    for it in box["plain"]:
        _plain(it, user, target_page)
    if not (box["urgent"] or box["todo_groups"] or box["plain"]):
        st.caption("새 알림이 없습니다.")
    st.page_link(NOTIFICATIONS_PAGE, label="알림 전체 보기 →")


# ---------------------------------------------------------------- 설비별 조사 (관리자 설비별 조사 탭·작업자 작업 카드 공용)
def finding_lines(f: dict) -> None:
    """기본 3줄: 이상 / 1순위 원인(근거는 작은 글씨) / 할 일(굵게). 옛 근거 파일은 첫 근거 수치로 대신한다."""
    e = html.escape
    hyps = f.get("hypotheses") or []
    anomaly = f.get("anomaly_summary") or next(iter(f.get("key_evidence") or []), "-")
    cause = (f"{e(hyps[0]['cause'])} <span class='finding-ev'>{e(hyps[0]['evidence'])}</span>" if hyps else "-")
    st.markdown(
        f"<div class='finding-line'><span class='card-key'>이상</span>{e(anomaly)}</div>"
        f"<div class='finding-line'><span class='card-key'>1순위 원인</span>{cause}</div>"
        f"<div class='finding-line'><span class='card-key'>할 일</span><b>{e(f.get('action_summary') or '-')}</b></div>",
        unsafe_allow_html=True)


def finding_details(f: dict) -> None:
    """"자세히 보기" 안쪽: 기본 정보, 의심 원인, 점검 순서, 현장 요령·준비물, 과거 현장 메모, 근거 수치, 조사 결론.
    호출하는 쪽에서 st.expander("자세히 보기")로 감싼다."""
    days = "-" if f.get("days_since") is None else f"{f['days_since']}일 전"
    est = f"{f['est_minutes']}분" if f.get("est_minutes") else "-"
    prob = f"{f['prob']:.2f}" if f.get("prob") is not None else "-"
    st.caption(f"24시간 내 고장 확률 {prob} · 중요도 {f.get('criticality', '-')} · 마지막 교체 {days}"
               f" · 예상 점검 {est} · 근거 번호 {f.get('evidence_id', '-')}")
    if f.get("rule_override"):
        st.caption("최근 7일 안에 교체한 부품이라 코드 규칙으로 판정을 '센서·설치 이상 가능성'으로 보정했습니다.")
    hyps = f.get("hypotheses") or []
    st.markdown("**의심 원인**")
    if hyps:
        st.dataframe(pd.DataFrame([{"순위": i, "원인": h["cause"], "근거": h["evidence"]} for i, h in enumerate(hyps, 1)]),
                     hide_index=True, width="stretch")
    else:
        st.caption("기록 없음")
    st.markdown("**점검 순서**")
    if f.get("steps"):
        st.dataframe(pd.DataFrame([{"순서": i, "할 일": s["action"], "판단 기준": s["criterion"], "이상 시": s["if_abnormal"]}
                                   for i, s in enumerate(f["steps"], 1)]), hide_index=True, width="stretch")
    else:
        st.markdown("\n".join(f"{i}. {c}" for i, c in enumerate(f.get("checklist") or [], 1)) or "기록 없음")
    if f.get("field_tips") or f.get("parts_tools"):
        st.markdown("**현장 요령 · 부품·공구**")
        st.markdown("\n".join([f"- {t}" for t in f.get("field_tips") or []]
                              + ([f"- 준비물: {', '.join(f['parts_tools'])}"] if f.get("parts_tools") else [])))
    st.markdown("**과거 현장 메모** (엔지니어 피드백)")
    st.markdown("\n".join(f"- {n}" for n in f.get("notes") or []) or "기록 없음")
    st.markdown("**작업자 완료 보고** (제목을 누르면 메모 전체)")
    report_toggles(f.get("reports") or [])
    st.markdown("**근거 수치**")
    st.markdown("\n".join(f"- {x}" for x in f.get("key_evidence") or []) or "기록 없음")
    if f.get("summary"):
        st.caption(f"조사 결론: {f['summary']}")


def sensor_chart(f: dict, trend: pd.DataFrame, as_of: datetime) -> None:
    """센서 최근 72시간 추이 (점선 = 1년 평균, 빨간 선 = 기준 시각)."""
    fig = px.line(trend, x="datetime", y="value", labels={"datetime": "시각", "value": f["sensor"]})
    fig.update_traces(line_color=NAVY, name="원본", showlegend=False)
    inp = trend[trend["source"] == "입력"] if "source" in trend else trend.iloc[0:0]
    if len(inp):  # 입력 데이터 구간은 다른 색 (원본을 덮어쓴 시각)
        fig.add_vrect(x0=inp["datetime"].min(), x1=inp["datetime"].max(), fillcolor=ORANGE, opacity=0.08, line_width=0)
        fig.add_scatter(x=inp["datetime"], y=inp["value"], mode="lines+markers", name="입력 데이터",
                        line=dict(color=ORANGE), marker=dict(size=4))
    fig.add_hline(y=f["base"], line_dash="dot", line_color=GRAY, annotation_text="1년 평균")
    fig.add_vline(x=as_of, line_color=RED)
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0), plot_bgcolor="white",
                      legend=dict(orientation="h", y=1.08, x=0))
    st.plotly_chart(fig, width="stretch")
    note = " 주황 구간은 입력 데이터입니다 (원본은 바뀌지 않음)." if len(inp) else ""
    st.caption(f"최근 72시간 {f['sensor']} · 빨간 선은 기준 시각입니다. 그 뒤 데이터는 조회하지 않습니다.{note}")


# ---------------------------------------------------------------- 작업자 완료 보고 메모 (자르지 않고 토글로)
def report_toggle_html(r: dict, open_: bool = False) -> str:
    """완료 보고 1건을 HTML 토글(<details>)로: 제목 줄을 누르면 메모 전체(줄바꿈 유지)가 펼쳐진다.
    st.expander 안에서도 쓸 수 있다 (Streamlit expander는 중첩이 안 됨)."""
    e = html.escape

    def block(text: str) -> str:
        return e(text).replace("\n", "<br>")

    summary = (f"{e(r['as_of_date'])} · #{r['machine_id']} · {e(r.get('team') or '-')} · {e(r['actual_cause_type'])}"
               f" — {e(r['action'])}, {r['duration_min']}분 <span class='finding-ev'>({e(r['reported_by'])},"
               f" {e(r['wo_id'])})</span>")
    parts = [f"<div class='report-memo'>{block(r.get('memo') or '(메모 없음)')}</div>"]
    if r.get("deviation"):
        parts.append(f"<div class='report-extra'><b>지시와 달랐던 점</b> {block(r['deviation'])}</div>")
    steps = [s for s in r.get("step_results") or [] if s.get("result") != "정상" or s.get("observation")]
    if steps:
        parts.append("<div class='report-extra'><b>단계별 결과</b><br>" + "<br>".join(
            f"{e(s['action'])}: {e(s['result'])}" + (f" ({e(s['observation'])})" if s.get("observation") else "")
            for s in steps) + "</div>")
    return (f"<details class='report-toggle'{' open' if open_ else ''}><summary>{summary}</summary>"
            + "".join(parts) + "</details>")


def report_toggles(reports: list[dict], open_wo: str | None = None) -> None:
    """완료 보고 목록을 토글로 (open_wo와 같은 작업 지시는 펼친 상태)."""
    if not reports:
        st.caption("기록 없음")
        return
    st.markdown("".join(report_toggle_html(r, r["wo_id"] == open_wo) for r in reports), unsafe_allow_html=True)
