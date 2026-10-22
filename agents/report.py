"""리포트 에이전트: 하루 조사·계획·초안 결과로 교대 인수인계 리포트(마크다운)를 쓴다.

- build_report_agent(): create_agent (gpt-5.4-mini, read_evidence·save_report, Ctx 주입, 도구 호출 실행당 12회)
- write_report(as_of) -> {"path", "markdown", "checks", "fallback"}
  실행 후 코드 검사(파일, 고위험 설비 번호, 근거 ID, 6개 절). 실패하면 template_report로 대체 저장.
- template_report(as_of): LLM 없이 같은 양식을 코드로 채우는 백업
- ask_report_agent: Supervisor용 도구 (저장 경로 + 요약 3줄)
"""

import re
import sys
import time
from functools import cache
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_openai import ChatOpenAI

AGENTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = AGENTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from tools.context import Ctx  # noqa: E402
from tools.plan_tools import (  # noqa: E402
    EXCLUDED, HUMAN_CHECK, TASK_LABEL, TOMORROW, _stock, is_assigned, load_drafts, load_findings, load_plan,
)
from tools.report_tools import (  # noqa: E402
    REPORT_TOOLS, STATUS_KO, build_daily_brief, history_lines, plan_history, report_path, work_status_lines,
    write_report_file,
)

HISTORY_TITLE = "재계획 이력"

MODEL_NAME = "gpt-5.4-mini"
TOOL_CALL_LIMIT = 12  # 실행(invoke) 1회당
NO_TARGET = "오늘 정비 대상 없음"
SECTIONS = [
    "1. 요약",
    "2. 오늘 정비 배정",
    "3. 설비별 조사 결론",
    "4. 사람 확인 필요",
    "5. 작업 지시·발주 처리 결과",
    "6. 다음 교대 확인 사항",
]

SYSTEM_PROMPT = f"""당신은 설비 정비 교대 인수인계 리포트 작성자입니다.

[사용할 정보]
- 사용자 메시지의 하루 요약과 read_evidence 결과만 사용합니다. 새로 계산하거나 추측하지 않습니다.
  근거에 없는 해석(예: "교체 후에도 이상이 계속됨")을 덧붙이지 않습니다.
- 고위험 설비마다 read_evidence(근거 ID)를 한 번씩 호출한 뒤 작성합니다. 도구 호출은 모두 합쳐 최대 {TOOL_CALL_LIMIT}회입니다.
- 모든 수치 뒤에 그 수치가 나온 설비의 근거 ID를 [근거 E1001-045] 형식으로 붙입니다. 설비 번호는 "#84"처럼 씁니다.
  한 문장에 같은 근거 ID를 반복하지 않고, 대수·건수 집계와 "없음"에는 근거 ID를 붙이지 않습니다.
- 절과 절 사이에는 빈 줄을 하나 둡니다.

[양식] 마크다운, 절 제목을 아래와 똑같이 씁니다.
# 교대 인수인계 리포트 — {{기준 시각}}
## {SECTIONS[0]}
3줄 이내. 고위험 설비 수, 일정에 넣은 작업 수, 사람 확인·내일 우선 수, 주요 조치.
## {SECTIONS[1]}
팀별 시간표: | 팀 | 시간 | 설비 | 작업 |  (배정표에서 팀이 있는 작업 전부, 팀 이름 순 → 시작 시각 순,
시간은 "09:00-11:00", 작업 칸은 "교체 · 점수 3.000 [근거 E1001-084]"처럼 작업 종류와 근거 ID)
표 아래 한 줄: 팀별 가동률, 내일 우선 작업 (하루 요약 그대로)
## {SECTIONS[2]}
고위험 설비마다 "### #번호 부품 — 판정" 아래 2~3줄 + 근거 ID
## {SECTIONS[3]}
"{HUMAN_CHECK}" 설비(센서·설치 이상 가능성)와 이유. 없으면 "없음"
## {SECTIONS[4]}
초안마다 한 줄: "- 승인: WO-..." / "- 거절: ..." / "- 대기: ..." / "- 대체됨: ..." (상태는 하루 요약 그대로)
하루 요약에 [재계획 이력]이 있으면 5절 끝에 "**재계획 이력**" 아래 버전마다 한 줄: 사유와 주요 배정 변경
## {SECTIONS[5]}
대기 설비, 아직 처리되지 않은(대기) 초안, 재고 0이라 입고를 기다리는 부품,
하루 요약의 [미완료 작업](작업 지시 번호·팀·상태)과 [완료 보고] 요약 (없으면 생략, 하루 요약 그대로)

[마무리]
- 마지막에 save_report(리포트 전체 마크다운)로 저장합니다.
- 최종 답변은 저장 경로 한 줄이면 됩니다.
"""


@cache
def build_report_agent():
    load_dotenv(ROOT_DIR / ".env")
    return create_agent(
        model=ChatOpenAI(model=MODEL_NAME, temperature=0),
        tools=REPORT_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        middleware=[ToolCallLimitMiddleware(run_limit=TOOL_CALL_LIMIT, exit_behavior="continue")],
        context_schema=Ctx,
    )


# ---------------------------------------------------------------- 백업 양식 (LLM 없음)
def _ev(ids) -> str:
    ids = [ids] if isinstance(ids, str) else list(ids)
    return f"[근거 {', '.join(ids)}]" if ids else ""


def template_report(as_of: str) -> str:
    """LLM 없이 6개 절 양식을 코드로 채운다 (에이전트 실패 시 백업, 고위험 0대인 날)."""
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    findings = load_findings(as_of)
    rows = (load_plan(as_of) or {"rows": []})["rows"]
    drafts = sorted(load_drafts(as_of).values(), key=lambda d: d["draft_id"])
    stock = _stock(as_of)

    assigned = [r for r in rows if is_assigned(r["slot"])]
    waiting = [r for r in rows if r["slot"] == TOMORROW]
    human = [r for r in rows if r.get("task_type") == "sensor_check" or r["slot"] == HUMAN_CHECK]
    scheduled = sorted((r for r in rows if r.get("team")), key=lambda r: (r["team"], r["start"]))
    plan_meta = load_plan(as_of) or {}
    by_status = {s: [d for d in drafts if d["status"] == s] for s in STATUS_KO}

    out = [f"# 교대 인수인계 리포트 — {as_of}", "", f"## {SECTIONS[0]}"]
    if not findings:
        out += [f"- {NO_TARGET} (고위험 설비 0대). 정비 배정과 작업 지시·발주 초안이 없습니다."]
    else:
        out += [
            f"- 고위험 설비 {len(findings)}대 조사: 일정 {len(scheduled)}건(작업 지시 {len(assigned)}건),"
            f" 내일 우선 {len(waiting)}대, 사람 확인 {len(human)}대.",
            f"- 초안 {len(drafts)}건: 승인 {len(by_status['APPROVED'])}, 거절 {len(by_status['REJECTED'])},"
            f" 대기 {len(by_status['PENDING'])}.",
        ]

    out += ["", f"## {SECTIONS[1]}"]
    if scheduled:
        out += ["| 팀 | 시간 | 설비 | 작업 |", "|---|---|---|---|"]
        out += [f"| {r['team']} | {r['start']}-{r['end']} | #{r['machine_id']} {r['comp']} |"
                f" {TASK_LABEL[r['task_type']]} · 점수 {r['score']:.3f} {_ev(r['evidence_id'])} |" for r in scheduled]
        util = ", ".join(f"{t} {u:.0%}" for t, u in (plan_meta.get("utilization") or {}).items()) or "-"
        tomorrow = ", ".join(f"#{r['machine_id']}" for r in waiting) or "없음"
        out.append(f"\n팀별 가동률: {util} · 내일 우선: {tomorrow}")
    else:
        out.append(f"- 배정 없음 ({NO_TARGET})" if not findings else "- 배정 없음")

    out += ["", f"## {SECTIONS[2]}"]
    for f in findings:
        out += [f"### #{f['machine_id']} {f['comp']} — {f['label']}",
                f"- {f['summary']} {_ev(f['evidence_id'])}"]
        if f["key_evidence"]:
            out.append(f"- {f['key_evidence'][0]} {_ev(f['evidence_id'])}")
    if not findings:
        out.append("- 조사 대상 없음")

    out += ["", f"## {SECTIONS[3]}"]
    out += [f"- #{r['machine_id']} {r['comp']}: {r['label']} — 재교체 전에 센서·배선·설치 상태 확인 필요"
            f" {_ev(r['evidence_id'])}" for r in human] or ["없음"]

    out += ["", f"## {SECTIONS[4]}"]
    for d in drafts:
        target = f"#{d['machine_id']} {d['comp']} 작업 지시" if d["type"] == "work_order" else f"{d['comp']} {d['qty']}개 발주"
        out.append(f"- {STATUS_KO.get(d['status'], d['status'])}: {d['draft_id']} ({target}) {_ev(d['evidence_ids'])}")
    if not drafts:
        out.append("- 초안 없음")
    if history := plan_history(as_of):
        out += ["", f"**{HISTORY_TITLE}**"] + [f"- {line}" for line in history_lines(history)]

    out += ["", f"## {SECTIONS[5]}"]
    todo = [f"- 내일 우선: #{r['machine_id']} {r['comp']} {TASK_LABEL.get(r.get('task_type'), '')}"
            f" (점수 {r['score']:.3f}) {_ev(r['evidence_id'])}" for r in waiting]
    todo += [f"- 제외 설비: #{r['machine_id']} {r['comp']} — {r['slot']} {_ev(r['evidence_id'])}"
             for r in rows if r["slot"].startswith(EXCLUDED)]
    todo += [f"- 미처리 초안: {d['draft_id']} (대기)" for d in by_status["PENDING"]]
    todo += [f"- 입고 대기 부품: {d['comp']} {d['qty']}개 (현재 재고 {int(stock[d['comp']]['qty'])},"
             f" 입고 소요 {d['lead_time_days']}일) {_ev(d['evidence_ids'])}"
             for d in drafts if d["type"] == "purchase_request" and d["status"] not in ("REJECTED", "SUPERSEDED")]
    unfinished, done = work_status_lines(as_of)
    todo += [f"- 미완료 작업: {line}" for line in unfinished]
    todo += [f"- 완료 보고: {line}" for line in done]
    out += todo or ["- 없음"]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 검사
def _mentions_machine(text: str, machine_id: int) -> bool:
    return re.search(rf"(#\s*0*{machine_id}(?!\d))|((?<![\d.])0*{machine_id}번)|(설비\s*0*{machine_id}(?!\d))", text) is not None


def check_report(as_of: str, markdown: str | None, path: Path) -> dict[str, bool]:
    findings = load_findings(as_of)
    text = markdown or ""
    return {
        "① 저장 파일 존재": path.exists() and markdown is not None,
        "② 고위험 설비 번호 모두 등장": all(_mentions_machine(text, f["machine_id"]) for f in findings),
        "③ 고위험 설비마다 근거 ID 등장": all(f["evidence_id"] in text for f in findings),
        "④ 6개 절 제목 모두 있음": all(re.search(rf"^##\s*{re.escape(s)}", text, re.MULTILINE) for s in SECTIONS),
        "⑤ 재계획 이력 (있을 때만)": not plan_history(as_of) or HISTORY_TITLE in text,
    }


def write_report(as_of: str) -> dict:
    """리포트를 쓰고 검사한다. {"path", "markdown", "checks", "fallback", "error"}"""
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    path = report_path(as_of)
    findings = load_findings(as_of)

    if not findings:  # 고위험 0대: LLM 없이 짧은 리포트
        markdown = template_report(as_of)
        write_report_file(as_of, markdown)
        return {"path": path, "markdown": markdown, "checks": check_report(as_of, markdown, path),
                "fallback": False, "error": None}

    started = time.time()
    error, markdown = None, None
    try:
        build_report_agent().invoke(
            {"messages": [{"role": "user", "content": build_daily_brief(as_of)
                           + "\n\n위 하루 요약으로 교대 인수인계 리포트를 작성해 save_report로 저장하세요."}]},
            context=Ctx(as_of=as_of),
        )
        if path.exists() and path.stat().st_mtime >= started:  # 이번 실행에서 저장한 파일만 인정
            markdown = path.read_text(encoding="utf-8")
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    checks = check_report(as_of, markdown, path)
    fallback = error is not None or not all(checks.values())
    if fallback:
        markdown = template_report(as_of)
        write_report_file(as_of, markdown)
    return {"path": path, "markdown": markdown, "checks": checks, "fallback": fallback, "error": error}


def summary_lines(markdown: str, n: int = 3) -> list[str]:
    """'## 1. 요약' 절의 내용 줄 (최대 n줄)."""
    m = re.search(r"^##\s*1\. 요약.*?\n(.*?)(?=^##\s)", markdown, re.MULTILINE | re.DOTALL)
    body = m.group(1) if m else markdown
    return [l.strip() for l in body.splitlines() if l.strip()][:n]


@tool
def ask_report_agent(request: str, runtime: ToolRuntime[Ctx]) -> str:
    """오늘의 조사·배정·초안 처리 결과로 교대 인수인계 리포트를 작성·저장하고, 저장 경로와 요약 3줄을 돌려준다.

    언제 쓰나: 조사와 정비 계획이 끝난 뒤, 교대 인수인계 리포트가 필요할 때.
    언제 쓰지 않나: 조사·계획 전, 통계 질문.

    Args:
        request: 리포트 요청 (예: "인수인계 리포트 만들어줘")
    """
    out = write_report(runtime.context.as_of)
    lines = [f"리포트 저장: {out['path'].relative_to(ROOT_DIR).as_posix()}"
             + (" (자동 양식으로 대체)" if out["fallback"] else ""), *summary_lines(out["markdown"])]
    return "\n".join(lines)
