"""실시간 모니터 (관리자, 오늘의 분석 › 대시보드 "실시간 감지" 카드 안): 1초 센서 스트림 차트·상태 배지 +
이상 주입(토글) + 입력 데이터 지우기. render_live_monitor(key_prefix)로 그린다 (import만으로는 아무것도 그리지 않음).

- 생성기는 별도 프로세스(scripts/stream_sim.py). 이 화면은 명령만 남기고(app_state) 결과를 읽는다.
- @st.fragment(run_every=1)로 차트와 배지만 1초마다 다시 그린다 (페이지 전체는 그대로).
  부르는 쪽이 이 함수를 호출하지 않으면 1초 fragment도 등록되지 않는다 (대시보드는 토글이 꺼져 있으면 부르지 않음).
- 1초 판단은 설비별 평소값과의 통계 비교(코드), 예측 모델은 1시간 요약으로 확인한다. 새 LLM 호출 없음.
- 모든 위젯 key와 차트 key 앞에 key_prefix를 붙인다 (같은 페이지의 다른 위젯과 겹치지 않게).
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from plotly.subplots import make_subplots

from config import COMP_LABEL_KO
from stream import DOWN_SENSORS
from stream.detector import Z_LIMIT
from ui import components as ui
from ui import state as S
from ui.style import GRAY, NAVY, ORANGE, RED

SENSORS = ["volt", "rotate", "pressure", "vibration"]
SENSOR_KO = {"volt": "전압", "rotate": "회전수", "pressure": "압력", "vibration": "진동"}
STATUS_KIND = {"실행 중": "navy", "시작 중": "orange", "정지": "gray", "응답 없음": "red"}
STATE_KIND = {"정상": "navy", "이상 감지": "red", "센서 이상": "orange"}
INJECT = [("drift", "서서히 상승"), ("spike", "갑자기 튐"), ("step", "계단 변화"), ("stuck", "센서 멈춤")]
INJECT_KO = dict(INJECT)
Y_SIGMA = 9  # y축 고정 범위 mean ± 9·std (spike 8·std까지 보이게, 매초 축이 바뀌지 않게)


def status_badge_html(s: dict) -> str:
    """생성기 상태 배지 (실행 중 / 시작 중 / 정지 / 응답 없음)."""
    return ui.badge_html(s["state"], STATUS_KIND.get(s["state"]))


@st.fragment(run_every=1)
def status_badge() -> None:
    s = S.live().stream_status()
    age = f" · 마지막 신호 {s['heartbeat_age']:.0f}초 전" if s["heartbeat_age"] is not None else ""
    st.markdown(f"{status_badge_html(s)}{age}", unsafe_allow_html=True)
    if s["state"] == "응답 없음":
        st.caption("생성기 프로세스가 응답하지 않습니다. [시작]을 다시 누르면 새로 띄웁니다.")


# ── 이상 주입 ──
def active_injection(machine_id) -> dict | None:
    """그 설비에 지금 켜진 (사용자가 넣은) 주입. recover(정비 후 복귀)는 켜짐으로 보지 않는다."""
    if machine_id is None:
        return None
    return next((i for i in S.live().stream_status()["injections"]
                 if int(i["machine_id"]) == int(machine_id) and i.get("kind") in INJECT_KO), None)


def _elapsed(started_at: str | None) -> int:
    try:
        return max(0, int((pd.Timestamp.now() - pd.Timestamp(started_at)).total_seconds()))
    except (TypeError, ValueError):
        return 0


@st.fragment(run_every=2)
def inject_box(running: list[int], p: str) -> None:
    """주입 버튼은 토글: 켜진 종류는 진한 버튼, 다시 누르면 끈다. 주입은 저절로 끝나기도 해서 2초마다 다시 읽는다."""
    L = S.live()
    st.markdown("**이상 주입**")
    i1, i2 = st.columns([1, 1])
    target = i1.selectbox("설비", running, format_func=lambda m: f"#{m}", key=f"{p}_inj_machine")
    sensor = i2.selectbox("센서", SENSORS, index=3, format_func=lambda s: f"{SENSOR_KO[s]} ({s})",
                          key=f"{p}_inj_sensor")
    active = active_injection(target)
    cols = st.columns(len(INJECT) + 1)
    for col, (kind, label) in zip(cols, INJECT):
        on = active is not None and active["kind"] == kind
        if col.button(label, key=f"{p}_inj_{kind}", type="primary" if on else "secondary", width="stretch",
                      disabled=target is None):
            if on:
                L.stream_reset(int(target), by=S.user_id())
            else:
                L.stream_inject(int(target), kind, sensor, by=S.user_id())
            ui.rerun_fragment()
    if cols[-1].button("정상 복귀", key=f"{p}_inj_reset", width="stretch", disabled=active is None):
        L.stream_reset(int(target), by=S.user_id())
        ui.rerun_fragment()
    if active and active["sensor"] != sensor:
        st.caption(f"켜짐: #{target} {SENSOR_KO[active['sensor']]} {INJECT_KO[active['kind']]}"
                   f" · {_elapsed(active.get('started_at'))}초째")


# ── 설비별 카드 ──
def card_figure(card: dict, machine_id: int):
    """센서 4개 미니 차트 (최근 120초).
    - x축: 마지막 시각 기준 120초 고정, 눈금 mm:ss / y축: 센서별 mean ± 9·std 고정
    - 회색 점선 = 평소 평균, 빨간 선 = 경고선(mean ± 3·std, 회전수는 아래쪽), 경고선을 넘은 점은 빨간 점
    - 주황 배경 = 그 센서에 이상을 주입한 구간 (다른 센서에는 칠하지 않음), 값 없음은 선을 끊는다"""
    w, base = card["window"], card["base"]
    end = w["sim_ts"].iloc[-1]
    start = end - pd.Timedelta(seconds=120)
    fig = make_subplots(rows=1, cols=4, subplot_titles=[SENSOR_KO[s] for s in SENSORS], horizontal_spacing=0.06)
    for i, s in enumerate(SENSORS, start=1):
        mean, std = base[s]
        down = s in DOWN_SENSORS
        warn = mean - Z_LIMIT * std if down else mean + Z_LIMIT * std
        fig.add_scatter(x=w["sim_ts"], y=w[s], mode="lines", line=dict(color=NAVY, width=1.4), connectgaps=False,
                        showlegend=False, hovertemplate="%{x|%H:%M:%S}  %{y:.1f}<extra></extra>", row=1, col=i)
        over = w[s] < warn if down else w[s] > warn
        if over.any():
            fig.add_scatter(x=w["sim_ts"][over], y=w[s][over], mode="markers", marker=dict(color=RED, size=4),
                            showlegend=False, hoverinfo="skip", row=1, col=i)
        # 기준선·주입 구간은 선을 그린 뒤에 넣는다 (빈 subplot에는 add_hline/add_vrect가 무시됨)
        fig.add_hline(y=mean, line=dict(color=GRAY, width=1, dash="dot"), row=1, col=i)
        fig.add_hline(y=warn, line=dict(color=RED, width=1.2), row=1, col=i)
        inj = w[w["injected_sensor"] == s]["sim_ts"]
        if not inj.empty:
            fig.add_vrect(x0=inj.min(), x1=max(inj.max(), inj.min() + pd.Timedelta(seconds=1)), fillcolor=ORANGE,
                          opacity=0.15, line_width=0, layer="below", row=1, col=i)
        fig.update_yaxes(range=[mean - Y_SIGMA * std, mean + Y_SIGMA * std], row=1, col=i)
    fig.update_xaxes(range=[start, end], tickformat="%M:%S", nticks=4, showgrid=False, tickfont_size=10)
    fig.update_yaxes(showgrid=True, gridcolor="#EEF1F5", tickfont_size=10)
    fig.update_layout(height=200, margin=dict(l=36, r=8, t=24, b=24), plot_bgcolor="white",
                      uirevision=f"stream-{machine_id}")
    fig.update_annotations(font_size=12)
    return fig


@st.fragment(run_every=1)
def live_cards(machine_ids: list[int], p: str) -> None:
    L = S.live()
    as_of_text = S.as_of_text()
    try:
        alerts = {a["machine_id"]: a for a in L.open_alerts(as_of_text)}
    except Exception:
        alerts = {}
    for m in machine_ids:
        with st.container(border=True):
            left, right = st.columns([5, 1.3])
            try:
                card = L.stream_card(int(m), as_of_text, 120)
            except Exception as e:
                left.caption(f"#{m} 스트림을 읽지 못했습니다: {e}")
                continue
            with left:
                st.markdown(f"**#{m}**")
                if card["window"].empty:
                    st.caption("아직 들어온 값이 없습니다. [시작]을 누르면 1초마다 값이 들어옵니다.")
                else:
                    st.plotly_chart(card_figure(card, int(m)), width="stretch", key=f"{p}_stream_chart_{m}",
                                    config={"displayModeBar": False})
            with right:
                st.write("")
                state = card["state"]
                st.markdown(ui.badge_html(card["state_ko"], STATE_KIND[card["state_ko"]]), unsafe_allow_html=True)
                if state["sensor"]:
                    z = f"z {state['z']:+.1f}" if state["z"] is not None else "값 없음"
                    st.caption(f"{SENSOR_KO[state['sensor']]} · {z} · {COMP_LABEL_KO.get(state['comp'], '')}")
                if a := alerts.get(int(m)):
                    st.markdown(f'<span style="color:{RED};font-weight:600">→ {L.alert_progress(a)}</span>',
                                unsafe_allow_html=True)
                    st.caption(f"{a['level']} · {a['source']}")


# ── 전체 ──
def render_live_monitor(key_prefix: str = "lm") -> None:
    """제어 줄([시작][정지] + 상태 배지) · 이상 주입 · 설비별 차트 · 안내 · 입력 데이터 지우기.
    페이지 제목은 그리지 않는다 (부르는 쪽 카드가 제목을 가진다)."""
    p = key_prefix
    ss = st.session_state
    L = S.live()
    mock = S.use_mock()
    status = L.stream_status()

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([4, 1, 1, 1.4])
        default = [m for m in status["machines"] if 1 <= int(m) <= 100][:5] or [45]
        machines = c1.multiselect("설비 (최대 5대)", list(range(1, 101)), default=default, max_selections=5,
                                  format_func=lambda m: f"#{m}", key=f"{p}_stream_machines")
        c2.write("")
        if c2.button("시작", type="primary", key=f"{p}_stream_start", disabled=not machines, width="stretch"):
            out = L.stream_start(machines, no_llm=mock, by=S.user_id())
            if out.get("error"):
                st.error(out["error"])
            st.rerun()
        c3.write("")
        if c3.button("정지", key=f"{p}_stream_stop", width="stretch"):
            L.stream_stop(by=S.user_id())
            st.rerun()
        c4.write("")
        with c4:
            status_badge()

    with st.container(border=True):
        inject_box(status["machines"] or machines, p)

    shown = [int(m) for m in (status["machines"] if status["state"] != "정지" and status["machines"] else machines)]
    if shown:
        live_cards(shown, p)
    else:
        st.info("설비를 고르고 [시작]을 누르세요.")
    st.caption("차트: 회색 점선 평소 평균 · 빨간 선 경고선(평균 ± 3σ) · 빨간 점 경고선을 넘은 값 · 주황 배경 이상 주입 구간. "
               "1초 판단은 설비별 평소값과의 통계 비교, 예측 모델은 1시간 요약으로 확인합니다.")

    with st.expander("입력 데이터 지우기"):
        if status["state"] in ("실행 중", "시작 중"):
            st.caption("생성기가 실행 중입니다. [정지] 후 지우면 차트가 깨끗하게 다시 시작합니다.")
        sure = st.checkbox("입력 데이터를 모두 지웁니다 (실시간 스트림 값, 입력 배치, 입력 데이터로 생긴 열린 경고 종료 · 원본은 그대로)",
                           key=f"{p}_clear_sure")
        if st.button("입력 데이터 모두 지우기", disabled=not sure, key=f"{p}_clear_all"):
            out = L.clear_live_data()
            for m in {int(i["machine_id"]) for i in L.stream_status()["injections"]}:
                L.stream_reset(m, by=S.user_id())
            ss[f"{p}_flash_clear"] = (f"입력 데이터를 지웠습니다 (스트림 {out['stream']}행, 배치 {out['batches']}개, "
                                      f"열린 경고 {out['alerts_closed']}건 종료).")
            st.rerun()
        if msg := ss.pop(f"{p}_flash_clear", None):
            st.success(msg)
