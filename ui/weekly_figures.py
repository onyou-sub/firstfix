"""주간 보고서 그래프와 HTML 내보내기 (Streamlit 없음: 화면과 내려받기·제출이 같은 그래프·표를 쓴다).

- figures(report): {"daily", "causes", "actions", "time"} plotly 그림 (없으면 None)
- export_html(report, summary, opinion): 한 파일 HTML. 화면과 같은 순서·표·그래프, plotly js는 첫 그래프에만,
  인쇄용 CSS(여백 15mm, 카드 테두리 유지, 표 줄 넘김 방지)라 브라우저 인쇄로 PDF 저장 가능.
"""
from __future__ import annotations

import html

import plotly.graph_objects as go

from ui.style import GRAY, GREEN_DARK, GREEN_LIGHT, NAVY, RED, RED_DARK, RED_LIGHT, TEXT_DARK

SECTIONS = ["주요 숫자", "요약", "일별 작업량", "점검 결과 분포 · 조치 분포", "작업 내역", "긴급 대응",
            "계획 대비 실제 시간", "특이사항 · 사용 부품", "다음 주 할 일", "작업자 의견"]
TASK_COLS = ["날짜", "설비", "부품", "작업 종류", "긴급", "결과", "조치", "계획(분)", "실제(분)", "상태"]
URGENT_ROW_BG = "#FDECEA"
CHART_HEIGHT = 260
NO_RECORD = "이번 주 기록 없음"
CAPTIONS = {
    "daily": "막대 위 숫자 = 그날 지시된 작업 수. 진한 색 = 완료, 연한 색 = 미완료, 초록 = 일반, 빨강 = 긴급.",
    "causes": "완료 보고의 점검 결과 (예측과 같음 / 다른 부품 / 센서·설치 문제 / 이상 없음).",
    "actions": "완료 보고의 조치.",
    "time": "작업 종류별 평균 시간 (표준 = 작업 표준 시간, 실제 = 완료 보고 소요 시간).",
}


def _layout(fig: go.Figure, title: str) -> go.Figure:
    # 축 글자(날짜·항목 이름)가 잘리지 않게 여백은 자동, 범례는 그래프 아래 (제목과 겹치지 않게)
    fig.update_layout(title=dict(text=title, font=dict(size=14, color=TEXT_DARK), x=0, xanchor="left"),
                      height=CHART_HEIGHT, margin=dict(l=10, r=10, t=36, b=10), plot_bgcolor="white",
                      paper_bgcolor="white", font=dict(family="sans-serif"),
                      legend=dict(orientation="h", yanchor="top", y=-0.18, xanchor="left", x=0, traceorder="normal"))
    fig.update_xaxes(automargin=True)
    fig.update_yaxes(gridcolor="#EEF1F5", automargin=True)
    return fig


def daily_figure(report: dict) -> go.Figure | None:
    days = report["daily"]
    if not any(d["normal_done"] + d["normal_open"] + d["urgent_done"] + d["urgent_open"] for d in days):
        return None
    x = [d["label"] for d in days]
    fig = go.Figure()
    for key, name, color in [("normal_done", "일반 완료", GREEN_DARK), ("normal_open", "일반 미완료", GREEN_LIGHT),
                             ("urgent_done", "긴급 완료", RED_DARK), ("urgent_open", "긴급 미완료", RED_LIGHT)]:
        fig.add_bar(x=x, y=[d[key] for d in days], name=name, marker_color=color,
                    hovertemplate=f"%{{x}} {name} %{{y}}건<extra></extra>")
    totals = [d["normal_done"] + d["normal_open"] + d["urgent_done"] + d["urgent_open"] for d in days]
    fig.add_scatter(x=x, y=totals, mode="text", text=[str(t) if t else "" for t in totals], textposition="top center",
                    showlegend=False, hoverinfo="skip")
    fig.update_layout(barmode="stack")
    fig.update_yaxes(title=None, dtick=1, range=[0, max(totals) * 1.25 + 0.5])
    return _layout(fig, "일별 작업량")


def distribution_figure(rows: list[dict], title: str, color: str) -> go.Figure | None:
    if not rows or not sum(r["count"] for r in rows):
        return None
    rows = list(reversed(rows))  # 가로 막대: 위에서부터 원래 순서
    fig = go.Figure(go.Bar(x=[r["count"] for r in rows], y=[r["label"] for r in rows], orientation="h",
                           marker_color=color, text=[f"{r['count']}건 · {r['pct']:.0f}%" for r in rows],
                           textposition="outside", cliponaxis=False, hoverinfo="skip"))
    fig.update_xaxes(visible=False, range=[0, max(r["count"] for r in rows) * 1.5])
    return _layout(fig, title)


def time_figure(report: dict) -> go.Figure | None:
    rows = report["time_by_type"]
    if not rows:
        return None
    x = [r["작업 종류"] for r in rows]
    fig = go.Figure()
    fig.add_bar(x=x, y=[r["표준(분)"] for r in rows], name="표준", marker_color=GRAY,
                text=[f"{r['표준(분)']}분" for r in rows], textposition="outside")
    fig.add_bar(x=x, y=[r["실제(분)"] for r in rows], name="실제", marker_color=NAVY,
                text=[f"{r['실제(분)']}분" for r in rows], textposition="outside")
    fig.update_layout(barmode="group")
    fig.update_yaxes(title=None, range=[0, max(max(r["표준(분)"], r["실제(분)"]) for r in rows) * 1.3])
    return _layout(fig, "계획 대비 실제 시간")


def figures(report: dict) -> dict:
    return {"daily": daily_figure(report),
            "causes": distribution_figure(report["causes"], "점검 결과 분포", NAVY),
            "actions": distribution_figure(report["actions"], "조치 분포", GREEN_DARK),
            "time": time_figure(report)}


def kpi_cards(report: dict) -> list[tuple[str, str, str]]:
    """숫자 카드 5개 (제목, 값, 설명)."""
    k = report["kpi"]
    rate = f"{k['done_rate']}%" if k["done_rate"] is not None else "-"
    ack = f"평균 {k['ack_avg_min']}분 안에 출동 확인" if k["ack_avg_min"] is not None else "출동 확인 기록 없음"
    hit = f"{k['hit_rate']}%" if k["hit_rate"] is not None else "-"
    return [("완료율", rate, f"완료 {k['done']} / 배정 {k['assigned']}건"),
            ("긴급 출동", f"{k['urgent']}건", ack if k["urgent"] else "긴급 출동 없음"),
            ("총 작업 시간", k["total_text"], f"완료 보고 {k['reports']}건 합계"),
            ("예측 적중률", hit, f"예측과 같음 {k['hit']} / 완료 보고 {k['reports']}건"),
            ("이월", f"{k['carryover']}건", "기준 날짜까지 미완료")]


# ---------------------------------------------------------------- HTML 내보내기
CSS = f"""
body {{ font-family: "Malgun Gothic", "Apple SD Gothic Neo", sans-serif; color: {TEXT_DARK}; background: #F3F5F8;
       margin: 0; }}
.doc {{ max-width: 900px; margin: 24px auto; background: #fff; border: 1px solid #DADFE6; border-radius: 8px;
        padding: 28px 32px; }}
h1 {{ font-size: 22px; color: {NAVY}; margin: 0 0 4px; }}
.meta {{ color: #5B6474; font-size: 12px; margin-bottom: 18px; }}
h2 {{ font-size: 15px; color: {NAVY}; border-bottom: 2px solid {NAVY}; padding-bottom: 4px; margin: 22px 0 10px; }}
.kpis {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }}
.kpi {{ border: 1px solid #DADFE6; border-radius: 6px; padding: 8px 10px; }}
.kpi .t {{ color: #5B6474; font-size: 12px; }} .kpi .v {{ font-size: 20px; font-weight: 700; color: {NAVY}; }}
.kpi .d {{ color: #5B6474; font-size: 11px; }}
.summary {{ line-height: 1.7; white-space: pre-wrap; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
th, td {{ border: 1px solid #DADFE6; padding: 4px 6px; text-align: left; }}
th {{ background: #F3F5F8; }}
th:first-child, td:first-child {{ white-space: nowrap; }}
tr.urgent td {{ background: {URGENT_ROW_BG}; }}
td.gap {{ font-weight: 700; }}
.two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
.cap {{ color: #5B6474; font-size: 11px; margin: 2px 0 8px; }}
.empty {{ color: #5B6474; font-size: 12px; }}
.badge {{ display: inline-block; padding: 0 6px; border-radius: 8px; font-size: 11px; color: #fff; background: {RED}; }}
@media print {{
  @page {{ size: A4; margin: 15mm; }}
  body {{ background: #fff; }}
  .doc {{ margin: 0; border: 1px solid #DADFE6; max-width: none; }}
  tr, .kpi, .chart {{ page-break-inside: avoid; break-inside: avoid; }}
  h2 {{ page-break-after: avoid; break-after: avoid; }}
}}
"""


def table_html(rows: list[dict], cols: list[str] | None = None) -> str:
    """행 목록 -> <table>. "_urgent" 행은 연한 빨강, "_time_gap"이면 실제(분) 굵게. 비어 있으면 기록 없음."""
    if not rows:
        return f'<p class="empty">{NO_RECORD}</p>'
    cols = cols or [c for c in rows[0] if not c.startswith("_")]
    e = html.escape
    head = "".join(f"<th>{e(c)}</th>" for c in cols)
    body = []
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            text = "-" if v is None else (f"#{v}" if c == "설비" else str(v))
            cls = ' class="gap"' if c == "실제(분)" and r.get("_time_gap") else ""
            cells.append(f"<td{cls}>{e(text)}</td>")
        row_cls = ' class="urgent"' if r.get("_urgent") else ""
        body.append(f"<tr{row_cls}>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def export_html(report: dict, summary: str, opinion: str = "") -> str:
    """보고서 한 장 HTML (화면과 같은 순서). plotly js는 첫 그래프에만 넣는다."""
    m, e = report["meta"], html.escape
    figs = figures(report)
    js_done = False

    def chart(key: str) -> str:
        nonlocal js_done
        fig = figs.get(key)
        if fig is None:
            return f'<p class="empty">{NO_RECORD}</p>'
        div = fig.to_html(full_html=False, include_plotlyjs="inline" if not js_done else False,
                          config={"displayModeBar": False})
        js_done = True
        return f'<div class="chart">{div}</div><p class="cap">{e(CAPTIONS[key])}</p>'

    kpis = "".join(f'<div class="kpi"><div class="t">{e(t)}</div><div class="v">{e(v)}</div><div class="d">{e(d)}</div>'
                   f"</div>" for t, v, d in kpi_cards(report))
    nw = report["next_week"]
    parts = [
        f"<h1>주간 정비 보고서 · {e(m['team'])} · {e(m['period'])}</h1>",
        f'<div class="meta">작성자 {e(m["author"])} · 기준 날짜 {e(m["day"])}</div>',
        f"<h2>{SECTIONS[0]}</h2><div class=\"kpis\">{kpis}</div>",
        f"<h2>{SECTIONS[1]}</h2><p class=\"summary\">{e(summary or '-')}</p>",
        f"<h2>{SECTIONS[2]}</h2>{chart('daily')}",
        f"<h2>{SECTIONS[3]}</h2><div class=\"two\"><div>{chart('causes')}</div><div>{chart('actions')}</div></div>",
        f"<h2>{SECTIONS[4]}</h2>{table_html(report['tasks'], TASK_COLS)}",
        f"<h2>{SECTIONS[5]}</h2>" + (table_html(report["urgent"]) if report["urgent"]
                                     else '<p class="empty">이번 주 긴급 출동 없음</p>'),
        f"<h2>{SECTIONS[6]}</h2>{chart('time')}",
        f"<h2>{SECTIONS[7]}</h2><div class=\"two\"><div><b>특이사항</b>{table_html(report['notes'])}</div>"
        f"<div><b>사용 부품</b>{table_html(report['parts'])}</div></div>",
        f"<h2>{SECTIONS[8]}</h2><div class=\"two\"><div><b>이월 작업</b>{table_html(nw['carryover'])}</div>"
        f"<div><b>주의 설비</b>{table_html(nw['attention'])}</div></div>",
        f"<h2>{SECTIONS[9]}</h2><p class=\"summary\">{e(opinion.strip() or '-')}</p>",
    ]
    return (f'<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>주간보고서 {e(m["team"])} '
            f'{e(m["week_start"])}</title><style>{CSS}</style></head><body><div class="doc">{"".join(parts)}'
            f"</div></body></html>")


def file_name(report: dict) -> str:
    return f"주간보고서_{report['meta']['team']}_{report['meta']['week_start']}.html"
