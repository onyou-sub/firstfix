"""리포트 에이전트용 도구와 하루 요약 입력.

- read_evidence(evidence_id): 설비별 조사 근거 요약 (최대 20줄, 기준 시각 이전 evidence만)
- save_report(markdown): outputs/reports/handover_{YYYYMMDD}.md 저장 (같은 날짜 덮어쓰기)
- build_daily_brief(as_of): 배정표·초안 상태·설비별 한 줄 요약을 모은 입력 문자열 (도구 아님)
"""

import json
import re
import sys
from pathlib import Path

import pandas as pd
from langchain.tools import ToolRuntime, tool

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from store import app_db  # noqa: E402
from tools.context import Ctx  # noqa: E402
from tools.plan_tools import (  # noqa: E402
    EVIDENCE_DIR,
    EXCLUDED,
    formula,
    HUMAN_CHECK,
    TASK_LABEL,
    TOMORROW,
    _stock,
    load_drafts,
    load_findings,
    load_plan,
    plan_versions,
    schedule_summary,
)

REPORTS_DIR = ROOT_DIR / "outputs" / "reports"
MAX_EVIDENCE_LINES = 20
EVIDENCE_ID_RE = re.compile(r"^E(\d{4})-(\d{3})$")
STATUS_KO = {"APPROVED": "승인", "REJECTED": "거절", "PENDING": "대기", "SUPERSEDED": "대체됨"}


def _norm(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")


def report_path(as_of: str) -> Path:
    return REPORTS_DIR / f"handover_{pd.Timestamp(as_of):%Y%m%d}.md"


# ---------------------------------------------------------------- evidence 요약
def _table_rows(text: str) -> list[list[str]]:
    """마크다운 표의 데이터 행 (헤더·구분선 제외)."""
    rows = [l for l in text.splitlines() if l.startswith("|")]
    return [[c.strip() for c in r.strip("|").split("|")] for r in rows[2:]]


def _summarize_call(call: dict, comp: str) -> str:
    """도구 호출 결과를 리포트용 한 줄로 줄인다."""
    name, out = call["name"], call.get("output") or ""
    if out.startswith(("입력 오류", "차단됨")) or call.get("status") == "error":
        return f"{name}: {out.splitlines()[0][:100] if out else '출력 없음'}"
    if name == "get_sensor_trend":
        items = [l[2:] for l in out.splitlines() if l.startswith("- ")]
        return f"{name}({call['input'].get('sensor')}): " + " / ".join(items)
    if name == "get_error_logs":
        total = next((l for l in out.splitlines() if l.startswith("총 ")), "에러 없음")
        rows = [f"{r[0]} {r[1]}회(24h {r[2]}, 마지막 {r[3]})" for r in _table_rows(out)[:3]]
        return f"{name}: {total}" + (f" - {', '.join(rows)}" if rows else "")
    if name == "get_maintenance_history":
        row = next((r for r in _table_rows(out) if r[0].startswith(comp)), None)
        text = f"{comp} 마지막 교체 {row[1]}, 경과 {row[2]}일, {row[3]}" if row else "기록 없음"
        warn = next((l for l in out.splitlines() if l.startswith("주의")), "")
        return f"{name}: {text}" + (f" ({warn[:60]})" if warn else "")
    if name == "find_similar_failures":
        head = next((l for l in out.splitlines() if "건 중" in l), "")
        rows = [f"{r[0]} #{r[1]} {r[2]}({r[3]})" for r in _table_rows(out)[:3]]
        return f"{name}: {head} " + ", ".join(rows)
    if name == "search_manual":
        src = next((l for l in out.splitlines() if l.startswith("[출처")), "결과 없음")
        return f"{name}: {src}"
    return f"{name}: {out.splitlines()[0][:100] if out else '출력 없음'}"


def evidence_text(evidence_id: str, as_of: str) -> str:
    m = EVIDENCE_ID_RE.match(str(evidence_id).strip())
    if not m:
        return "입력 오류: 근거를 찾을 수 없음 (형식 E{MMDD}-{설비번호 3자리}, 예: E1001-084)"
    year = pd.Timestamp(as_of).year
    path = EVIDENCE_DIR / f"{year}{m.group(1)}" / f"{int(m.group(2))}.json"
    if not path.exists():
        return f"입력 오류: 근거를 찾을 수 없음 ({evidence_id})"
    ev = json.loads(path.read_text(encoding="utf-8"))
    if pd.Timestamp(ev["as_of"]) > pd.Timestamp(as_of):  # 기준 시각 이후 조사 결과는 보여주지 않음
        return f"입력 오류: 근거를 찾을 수 없음 ({evidence_id}, 기준 시각 이후)"

    f = ev["finding"]
    lines = [
        f"[{ev['evidence_id']}] #{f['machine_id']} {f['comp']} | {f['label']} | 확률 {ev['input']['prob']:.4f}"
        f" | 조사 기준 {ev['as_of']}" + (" | 코드 규칙으로 판정 보정" if ev.get("rule_override") else ""),
        f"결론: {f['summary']}",
        "핵심 근거:",
        *[f"- {e}" for e in f["key_evidence"]],
        f"매뉴얼: {f.get('manual_source') or '-'}",
        "도구 호출 결과:",
        *[f"- {_summarize_call(c, f['comp'])}" for c in ev.get("tool_calls", [])],
    ]
    return "\n".join(lines[:MAX_EVIDENCE_LINES])


# ---------------------------------------------------------------- 하루 요약 (에이전트 입력)
def build_daily_brief(as_of: str) -> str:
    as_of = _norm(as_of)
    findings = load_findings(as_of)
    plan = load_plan(as_of) or {"rows": []}
    drafts = list(load_drafts(as_of).values())
    stock = _stock(as_of)

    lines = [f"[기준 시각] {as_of}"]
    if not findings:
        lines += ["[고위험 설비] 0대 - 오늘 정비 대상 없음", "[배정표] 없음", "[초안] 없음"]
        return "\n".join(lines)

    lines.append(f"\n[고위험 설비 조사 결과] {len(findings)}대 (설비 | 부품 | 판정 | 확률 | 근거 ID | 핵심 근거 1개)")
    for f in findings:
        lines.append(f"- #{f['machine_id']} | {f['comp']} | {f['label']} | {f['prob']:.4f} | {f['evidence_id']}"
                     f" | {f['key_evidence'][0] if f['key_evidence'] else '-'}")

    lines.append(f"\n[배정표 = 하루 일정표] {formula()}")
    lines.append("배정 | 팀 | 시작 | 종료 | 작업 | 설비 | 부품 | 확률 | 중요도 | 재고 | 점수 | 판정 | 근거 ID")
    for r in plan["rows"]:
        lines.append(f"{r['slot']} | {r.get('team') or '-'} | {r.get('start') or '-'} | {r.get('end') or '-'}"
                     f" | {TASK_LABEL.get(r.get('task_type'), '-')} | #{r['machine_id']} | {r['comp']} | {r['prob']:.4f}"
                     f" | {r['criticality']} | {r['stock_qty']} | {r['score']:.3f} | {r['label']} | {r['evidence_id']}")
    lines.append(schedule_summary(plan))

    lines.append("\n[작업 지시·발주 초안] (상태: APPROVED=승인, REJECTED=거절, PENDING=대기)")
    for d in sorted(drafts, key=lambda d: d["draft_id"]):
        if d["type"] == "work_order":
            target = f"작업 지시 #{d['machine_id']} {d['comp']} ({d['slot']})"
        else:
            target = (f"발주 {d['comp']} {d['qty']}개 (현재 재고 {d['current_qty']}, 안전 재고 {d['safety_stock']},"
                      f" 입고 소요 {d['lead_time_days']}일)")
        lines.append(f"- {d['draft_id']} | {d['status']}({STATUS_KO.get(d['status'], d['status'])}) | {target}"
                     f" | 근거 {', '.join(d['evidence_ids']) or '-'}")
    if not drafts:
        lines.append("- 없음")

    zero = [f"{c}(입고 소요 {int(s['lead_time_days'])}일)" for c, s in stock.items() if int(s["qty"]) == 0]
    lines.append(f"\n[재고 0 부품] {', '.join(zero) or '없음'}")
    tomorrow = [f"#{r['machine_id']}" for r in plan["rows"] if r["slot"] == TOMORROW]
    human = [f"#{r['machine_id']} ({r['slot']})" for r in plan["rows"] if r.get("task_type") == "sensor_check"
             or r["label"] == "센서·설치 이상 가능성"]
    excluded = [f"#{r['machine_id']} {r['slot']}" for r in plan["rows"] if r["slot"].startswith(EXCLUDED)]
    lines.append(f"[내일 우선 설비] {', '.join(tomorrow) or '없음'}  [사람 확인 설비] {', '.join(human) or '없음'}"
                 f"  [제외 설비] {', '.join(excluded) or '없음'}")
    unfinished, done = work_status_lines(as_of)
    lines.append(f"\n[미완료 작업] {'; '.join(unfinished) or '없음'}")
    lines.append(f"[완료 보고] {'; '.join(done) or '없음'}")
    history = plan_history(as_of)
    if history:
        lines.append(f"\n[재계획 이력] 현재 배정안 v{plan.get('version', 1)}")
        lines += [f"- {h}" for h in history_lines(history)]
    return "\n".join(lines)


def work_status_lines(as_of: str) -> tuple[list[str], list[str]]:
    """운영 DB 기준 (미완료 작업 줄, 완료 보고 요약 줄). 기준 날짜 이하 작업 지시만 본다."""
    day = pd.Timestamp(as_of).strftime("%Y-%m-%d")
    open_wo = [w for w in app_db.work_orders() if w["as_of_date"] <= day and w["status"] != "DONE"]
    unfinished = [f"{w['wo_id']} {w['title']} ({w['team'] or '-'} {w['start'] or ''}-{w['end'] or ''},"
                  f" {app_db.WO_STATUS_KO[w['status']]}, 지시일 {w['as_of_date']})" for w in open_wo]
    done = [f"{r['wo_id']} #{r['machine_id']} ({r['team']}) {r['actual_cause_type']} — {r['action']},"
            f" {r['duration_min']}분{', 메모: ' + r['memo'] if r.get('memo') else ''}"
            for r in app_db.work_reports(as_of_date=day)]
    return unfinished, done


def plan_history(as_of: str) -> list[dict]:
    """재계획 버전(v2 이상)별 사유·해석·변경 [{"version", "reason", "note", "changes": [diff 행]}]."""
    out = []
    for v in plan_versions(as_of):
        p = load_plan(as_of, v)
        if v >= 2 and p:
            out.append({"version": v, "reason": p.get("reason", ""),
                        "note": (p.get("constraints") or {}).get("note", ""),
                        "changes": [d for d in p.get("diff", []) if d["changed"]]})
    return out


def history_lines(history: list[dict]) -> list[str]:
    lines = []
    for h in history:
        changes = ", ".join(f"#{d['machine_id']} {d['before']}→{d['after']}" for d in h["changes"]) or "배정 변경 없음"
        lines.append(f"v{h['version']}: 사유 \"{h['reason']}\" — 해석: {h['note']} / 변경: {changes}")
    return lines


def write_report_file(as_of: str, markdown: str) -> Path:
    path = report_path(as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown.strip() + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- 도구
@tool
def read_evidence(evidence_id: str, runtime: ToolRuntime[Ctx]) -> str:
    """설비 1대의 조사 근거를 요약해 돌려준다: 판정, 결론, 핵심 근거, 매뉴얼 출처, 도구 호출 결과 요약 (최대 20줄).

    언제 쓰나: 리포트의 "설비별 조사 결론"을 쓰기 전에 고위험 설비마다 한 번.
    언제 쓰지 않나: 배정표·초안 상태 (사용자 메시지의 하루 요약에 이미 있음).

    Args:
        evidence_id: 근거 ID (예: E1001-084)
    """
    return evidence_text(evidence_id, runtime.context.as_of)


@tool
def save_report(markdown: str, runtime: ToolRuntime[Ctx]) -> str:
    """완성한 인수인계 리포트(마크다운)를 저장하고 저장 경로를 돌려준다. 같은 날짜면 덮어쓴다.

    언제 쓰나: 리포트를 다 쓴 뒤 마지막에 한 번.
    언제 쓰지 않나: 초안 상태에서 중간 저장.

    Args:
        markdown: 리포트 전체 마크다운
    """
    if not isinstance(markdown, str) or not markdown.strip():
        return "입력 오류: 빈 리포트는 저장할 수 없습니다"
    path = write_report_file(runtime.context.as_of, markdown)
    return f"저장 완료: {path.relative_to(ROOT_DIR).as_posix()}"


REPORT_TOOLS = [read_evidence, save_report]
