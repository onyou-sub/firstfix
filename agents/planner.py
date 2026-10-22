"""계획 에이전트: 조사 결과로 정비 우선순위·팀 배정을 정하고 작업 지시서·발주 요청서 초안을 만든다 (등록은 하지 않음).

- build_planner(): create_agent (gpt-5.4-mini, 계획 도구 4개, Ctx 주입, 도구 호출 실행당 12회)
- plan(as_of) -> {"answer", "plan", "drafts", "auto_filled"}
  실행 후 코드가 확인: 배정된 설비의 작업 지시서, 재고 0 부품의 발주 요청서가 없으면 직접 만든다 (auto_filled).
- ask_planner_agent: Supervisor용 도구 (answer만 반환)
- replan(as_of, reason): 거절 사유 -> PlanConstraints(LLM 해석) -> rank(코드 배정) -> 초안 v{n}(코드) -> 변경 요약(LLM)
  하루 최대 MAX_REPLANS회. replan_maintenance: Supervisor용 도구
"""

import re
import sys
from functools import cache
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

AGENTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = AGENTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from config import CREWS, MAX_REPLAN  # noqa: E402
from ops_settings import load_settings  # noqa: E402
from schemas import PlanConstraints  # noqa: E402
from memory.store import get_preferences  # noqa: E402
from tools.context import Ctx  # noqa: E402
from store import app_db  # noqa: E402
from tools.plan_tools import (  # noqa: E402
    HUMAN_CHECK,
    PLAN_TOOLS,
    SUPERSEDED,
    SAFETY_FIRST_STEP,
    SENSOR_LABEL,
    TASK_LABEL,
    zero_stock_parts,
    TOMORROW,
    default_teams,
    is_assigned,
    load_drafts,
    load_findings,
    load_plan,
    make_purchase_request,
    make_work_order,
    plan_table,
    plan_versions,
    purchase_qty,
    rank,
    save_plan,
    schedule_summary,
    supersede_pending,
)

MODEL_NAME = "gpt-5.4-mini"
TOOL_CALL_LIMIT = 12  # 실행(invoke) 1회당
MAX_REPLANS = MAX_REPLAN  # 기본 하루 재계획 최대 횟수 (v1 + 재계획 3회 = v4까지). 실제 한도는 운영 설정 max_replan
NO_TARGET_ANSWER = "오늘 정비 대상 없음 (조사된 고위험 설비 0대). 작업 지시서·발주 초안을 만들지 않았습니다."

SYSTEM_PROMPT = f"""당신은 정비 계획 담당자입니다. 조사 결과를 바탕으로 오늘 정비 배정과 초안을 만듭니다. 등록(submit)은 하지 않습니다.

[순서] 도구 호출은 모두 합쳐 최대 {TOOL_CALL_LIMIT}회입니다.
1. rank_maintenance: 사용자 메시지의 후보 설비 번호 전부로 한 번 호출 (crews={CREWS})
   코드가 하루 일정표(팀·시작·종료·작업 종류)를 계산합니다. 당신은 시간을 계산하거나 바꾸지 않습니다.
   결과 마지막 줄 "다음 단계"에 작업 지시서·발주 요청서 대상이 정리되어 있으니 그대로 따릅니다.
2. 작업 지시서 대상 작업의 부품마다 check_parts_stock (같은 부품은 한 번)
3. 작업 지시서 대상(교체·임시 점검)마다 draft_work_order. checklist는 후보 정보의 조사 체크리스트를 그대로 넣습니다
   (맨 앞 "{SAFETY_FIRST_STEP}"은 넣어도 되고, 빠지면 코드가 넣습니다).
4. 임시 점검(재고 0) 작업의 부품은 반드시 draft_purchase_request를 호출합니다.
   수량 = 안전 재고 - 현재 재고 (최소 1). 같은 부품은 한 번만. 재고가 1개 이상이면 발주하지 않습니다.

[규칙]
- "{HUMAN_CHECK}"(센서 점검, {SENSOR_LABEL}) 작업은 일정에는 들어가지만 작업 지시서를 만들지 않습니다.
- "{TOMORROW}"·"제외" 설비는 초안을 만들지 않습니다.
- 도구 결과에 없는 수치·시각은 만들지 않습니다.

[마지막 답변] 한국어로 짧게, 일정표 결과만 설명
- 팀별 하루 일정: 팀마다 "1팀: 09:00-11:00 #84 motor 교체 → 13:00-15:00 #90 motor 교체" 형식 (시작 시각 순)
- 배정 이유 1줄씩, 일정표의 순위와 "배정 사유(코드)" 열을 그대로 풀어 씁니다 (사유를 새로 추측하지 않음)
  (예: "#84는 위험도 1위라 1팀 첫 작업", "#92는 기계 전문 2팀에 배정", "#90은 전문 팀이 늦어 가장 빠른 2팀",
   "#77은 18시 안에 끝나지 않아 내일 우선", "#11은 재고 0이라 교체 대신 임시 점검 + 발주")
- 팀별 가동률, 사람 확인·내일 우선·제외 설비와 이유
- 생성한 draft_id 목록
"""


@cache
def build_planner():
    load_dotenv(ROOT_DIR / ".env")
    return create_agent(
        model=ChatOpenAI(model=MODEL_NAME, temperature=0),
        tools=PLAN_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        middleware=[ToolCallLimitMiddleware(run_limit=TOOL_CALL_LIMIT, exit_behavior="continue")],
        context_schema=Ctx,
    )


def _user_message(as_of: str, findings: list[dict], request: str | None, engineer_id: str | None = None) -> str:
    lines = []
    if engineer_id and (prefs := get_preferences(engineer_id)):  # 장기 기억의 엔지니어 선호
        lines.append(f"엔지니어 선호: {' / '.join(prefs)} (배정·점수는 rank_maintenance 결과를 따르고, 사유·초안 작성에 반영)")
    lines.append(f"기준 시각 {as_of}. 조사된 고위험 설비 {len(findings)}대의 정비 계획을 세워 주세요.")
    if request:
        lines.append(f"요청: {request}")
    lines.append(f"후보 설비 번호: {[f['machine_id'] for f in findings]}")
    for f in findings:
        lines.append(
            f"- #{f['machine_id']} {f['comp']} | {f['label']} | 확률 {f['prob']:.4f} | 근거 {f['evidence_id']}\n"
            f"  조사 체크리스트: {f['checklist']}"
        )
    return "\n".join(lines)


def _ensure_drafts(as_of: str, findings: list[dict], auto_filled: bool = True) -> list[str]:
    """최신 배정안 기준으로 빠진 초안(배정 설비 작업 지시서, 재고 0 부품 발주)을 코드로 만든다. 만든 draft_id 목록."""
    filled = []
    plan_ = load_plan(as_of)
    version = plan_["version"]
    sfx = "" if version <= 1 else f"-v{version}"
    drafts = load_drafts(as_of)
    by_id = {f["machine_id"]: f for f in findings}
    mmdd = pd.Timestamp(as_of).strftime("%m%d")

    assigned = [r for r in plan_["rows"] if is_assigned(r["slot"])]
    for r in assigned:
        wo_id = f"WO-{mmdd}-{r['machine_id']:03d}{sfx}"
        if wo_id not in drafts:
            make_work_order(as_of, r["machine_id"], r["comp"], by_id[r["machine_id"]]["checklist"],
                            auto_filled=auto_filled, version=version)
            filled.append(wo_id)

    for comp in sorted({r["comp"] for r in assigned if r["stock_qty"] == 0 and r["label"] != SENSOR_LABEL}):
        pr_id = f"PR-{mmdd}-{comp}{sfx}"
        if pr_id not in drafts:
            make_purchase_request(as_of, comp, purchase_qty(comp, as_of), auto_filled=auto_filled, version=version)
            filled.append(pr_id)
    return filled


def _ensure_purchase_drafts(as_of: str) -> list[str]:
    """코드 검사: 재고가 0인 부품마다 현재 배정안 버전의 발주 초안이 있어야 한다.
    - 오늘 임시 점검(temporary_check) 작업이 생긴 부품 (오늘 일정이든 "내일 우선"이든)
    - 오늘 쓸 작업이 없어도 재고가 0인 부품 (재고 보충, 입고 리드타임이 있으므로 미리 발주)
    이미 승인된 발주가 입고 대기 중인 부품은 건너뛴다. 없으면 코드가 직접 만들고 auto_filled=True로 기록하며,
    작업과 무관한 재고 보충 발주는 관리자에게 알림을 보낸다. 만든 draft_id 목록."""
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    plan_ = load_plan(as_of) or {"version": 1, "rows": []}
    version = plan_["version"]
    sfx = "" if version <= 1 else f"-v{version}"
    drafts = load_drafts(as_of)
    mmdd = pd.Timestamp(as_of).strftime("%m%d")
    task_comps = {r["comp"] for r in plan_["rows"]
                  if r.get("task_type") == "temporary_check" and r["label"] != SENSOR_LABEL}
    zero = {z["comp"]: z for z in zero_stock_parts(as_of)}
    filled = []
    for comp in sorted(task_comps | set(zero)):
        if comp in zero and zero[comp]["in_transit"]:
            continue  # 입고 대기 중인 발주가 있음
        pr_id = f"PR-{mmdd}-{comp}{sfx}"
        if pr_id in drafts:
            continue
        restock = comp not in task_comps
        d = make_purchase_request(as_of, comp, purchase_qty(comp, as_of), auto_filled=True, version=version,
                                  note="재고 0 - 오늘 쓸 작업은 없지만 입고 리드타임 때문에 미리 보충 발주" if restock else "")
        filled.append(pr_id)
        if restock:
            app_db.notify(title=f"재고 0 발주 승인 대기: {comp} {d['qty']}개",
                          body=f"{d['part_name']} 재고가 0입니다. 오늘 작업에는 필요 없지만 입고 {d['lead_time_days']}일이"
                               f" 걸려 보충 발주 초안({pr_id})을 올렸습니다.",
                          to_role="admin", as_of_date=as_of[:10], kind="approval",
                          head=f"{d['part_name']} {d['qty']}개", line=f"재고 0 보충 발주 · 입고 {d['lead_time_days']}일")
    return filled


def plan(as_of: str, request: str | None = None, engineer_id: str | None = None) -> dict:
    """조사 결과(evidence)로 정비 계획을 세운다. {"answer", "plan", "drafts", "auto_filled"}"""
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    findings = load_findings(as_of)

    if not findings:  # 고위험 0대: LLM 호출 없이 빈 계획만 저장 (재고 0 부품 보충 발주는 코드가 만든다)
        rank([], CREWS, as_of)
        filled = _ensure_purchase_drafts(as_of)
        answer = NO_TARGET_ANSWER if not filled else (
            f"오늘 정비 대상 없음 (조사된 고위험 설비 0대). 재고 0 부품 보충 발주 초안: {', '.join(filled)}")
        return {"answer": answer, "plan": [], "drafts": list(load_drafts(as_of).values()), "auto_filled": filled}

    result = build_planner().invoke(
        {"messages": [{"role": "user", "content": _user_message(as_of, findings, request, engineer_id)}]},
        context=Ctx(as_of=as_of, engineer_id=engineer_id or Ctx.engineer_id),
    )
    final = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage) and not m.tool_calls), None)
    answer = final.text if final else ""

    auto_filled = []
    if load_plan(as_of) is None:  # 에이전트가 rank_maintenance를 부르지 않은 경우
        rank([f["machine_id"] for f in findings], CREWS, as_of)
        auto_filled.append("PLAN")
    auto_filled += _ensure_drafts(as_of, findings)
    auto_filled += _ensure_purchase_drafts(as_of)  # 재고 0 부품 발주 누락 검사 (내일 우선 포함)
    if auto_filled:
        answer += f"\n\n(코드 보완: 에이전트가 빠뜨린 {', '.join(auto_filled)}을(를) 자동으로 만들었습니다.)"

    return {
        "answer": answer,
        "plan": load_plan(as_of)["rows"],
        "drafts": list(load_drafts(as_of).values()),
        "auto_filled": auto_filled,
    }


@tool
def ask_planner_agent(request: str, runtime: ToolRuntime[Ctx]) -> str:
    """조사된 고위험 설비들의 정비 우선순위·팀 배정을 정하고 작업 지시서·발주 요청서 초안을 만든다 (등록하지 않음).

    언제 쓰나: 설비 조사(investigate_machines)가 끝난 뒤, 오늘 정비 계획이 필요할 때.
    언제 쓰지 않나: 조사 전, 통계 질문, 이미 만든 초안을 등록할 때.

    Args:
        request: 계획에 반영할 요청 (예: "오늘 정비 계획 세워줘")
    """
    out = plan(runtime.context.as_of, request, runtime.context.engineer_id)
    # Supervisor가 등록 대상을 빠뜨리지 않도록 초안 파일 기준 목록을 코드로 붙인다
    pending = sorted(d["draft_id"] for d in out["drafts"] if d["status"] == "PENDING")
    head = f"등록 대상 draft_id (모두 submit_* 호출): {', '.join(pending) if pending else '없음'}"
    return f"{head}\n\n{out['answer']}\n\n{head}"


# ---------------------------------------------------------------- 재계획 (거절 사유 반영)
PARSE_PROMPT = """당신은 정비 계획 조건 해석기입니다. 담당자가 정비 배정안을 거절하면서 남긴 사유를 계획 조건으로 바꿉니다.
배정·점수 계산은 하지 않습니다 (코드가 합니다). 조건만 정합니다.

[팀] 오늘 팀은 {teams}입니다. 근무 시간은 모두 {work_start}~{work_end}입니다.
- available_teams: 하루 종일 쓸 수 없는 팀을 뺀 나머지를 원래 순서대로. 팀 언급이 없으면 {teams} 전부.
- "아무도 없음", "인력 없음"처럼 모든 팀이 불가하면 빈 목록 [].
- team_available: 하루 중 일부 시간만 가능한 팀의 가능 시간 ["HH:MM", "HH:MM"]. 시간 제약이 없으면 null.
  예) "2팀 오전 교육" → team_available {{"2팀": ["13:00", "18:00"]}} (available_teams에는 2팀 유지)
  예) "3팀 4시 조기 퇴근" → team_available {{"3팀": ["09:00", "16:00"]}}
  예) "1팀 하루 종일 불가" → available_teams에서 1팀 제외 (team_available에는 넣지 않음)
[설비] 현재 배정안 (점수 높은 순, 1번째가 가장 위험한 설비):
{plan}
- exclude_machines: 사유가 특정 설비를 오늘 정비하지 말라고 하면 그 설비 번호. "가장 위험한 설비"는 위 목록 1번째 설비.
- defer_reason: 제외 설비마다 짧은 사유 (10~20자).
[note] 해석 결과를 한 문장으로. 해석할 수 없거나 모순이면(예: 없는 팀·설비) 그 사실을 그대로 적고 조건은 바꾸지 않습니다.
"""

SUMMARY_PROMPT = """정비 배정안 재계획 결과를 담당자에게 3~5줄 한국어로 설명합니다.
- 무엇이 왜 바뀌었는지: 거절 사유 → 해석한 조건 → 배정 변경 (아래 변경표 그대로, 수치를 만들지 않음)
- 배정이 0건이면 오늘 배정할 팀이 없다고 분명히 쓰고, 새 초안이 없다고 안내합니다.
- 마지막 줄에 새 draft_id 목록을 적습니다 (없으면 "새 초안 없음")."""


@cache
def _llm() -> ChatOpenAI:
    load_dotenv(ROOT_DIR / ".env")
    return ChatOpenAI(model=MODEL_NAME, temperature=0)


def _plan_lines(rows: list[dict]) -> str:
    return "\n".join(f"{i}. #{r['machine_id']} {r['comp']} | 점수 {r['score']:.3f} | 중요도 {r['criticality']}"
                     f" | {r['label']} | 현재 배정 {r['slot']}" for i, r in enumerate(rows, 1)) or "(없음)"


def parse_constraints(reason: str, findings: list[dict], plan_rows: list[dict]) -> PlanConstraints:
    """거절 사유 -> PlanConstraints (LLM 해석 + 코드 검증). 해석 실패 시 조건 없이 note에 사유를 남긴다."""
    teams = default_teams(CREWS)
    cfg = load_settings()
    prompt = PARSE_PROMPT.format(teams=teams, plan=_plan_lines(plan_rows), work_start=cfg["work_start"],
                                 work_end=cfg["work_end"])
    try:
        # dict[int, str] 필드는 OpenAI strict json_schema가 허용하지 않아 function_calling 방식 사용
        parsed = _llm().with_structured_output(PlanConstraints, method="function_calling").invoke(
            [("system", prompt), ("user", f"거절 사유: {reason}")])
    except Exception as e:
        return PlanConstraints(available_teams=teams, exclude_machines=[], defer_reason={}, team_available=None,
                               note=f"사유를 해석하지 못해 조건 없이 다시 계산했습니다 ({type(e).__name__}): {reason}")

    # 코드 검증: 알려진 팀만 (해석 순서 유지), 후보 설비만, 제외 사유 채우기
    known_ids = {f["machine_id"] for f in findings}
    avail = [t for t in dict.fromkeys(parsed.available_teams) if t in teams]
    bad_teams = [t for t in parsed.available_teams if t not in teams]
    exclude = [m for m in dict.fromkeys(parsed.exclude_machines) if m in known_ids]
    bad_ids = [m for m in parsed.exclude_machines if m not in known_ids]
    note = parsed.note
    if bad_teams or bad_ids:
        note += f" (무시한 항목: 없는 팀 {bad_teams or '-'}, 후보가 아닌 설비 {bad_ids or '-'})"
    if parsed.available_teams and not avail:
        avail = teams  # 전부 잘못된 팀 이름이면 해석 실패로 보고 원래 팀 유지
    windows = _valid_windows(parsed.team_available, avail)
    if parsed.team_available and windows != parsed.team_available:
        note += f" (무시한 시간 조건: {parsed.team_available})"
    return PlanConstraints(
        available_teams=avail,
        exclude_machines=exclude,
        defer_reason={m: parsed.defer_reason.get(m) or "사유 미기재" for m in exclude},
        note=note,
        team_available=windows or None,
    )


def _valid_windows(team_available: dict | None, teams: list[str]) -> dict[str, list[str]]:
    """{"2팀": ["13:00", "18:00"]} 중 가능한 팀이고 HH:MM 시작 < 종료인 것만."""
    out = {}
    for t, w in (team_available or {}).items():
        if t in teams and isinstance(w, list) and len(w) == 2 and all(re.fullmatch(r"\d\d:\d\d", str(x)) for x in w) \
                and w[0] < w[1]:
            out[t] = [w[0], w[1]]
    return out


def _when(row: dict | None) -> str:
    return f"{row['start']}-{row['end']}" if row and row.get("start") else "-"


def _diff(old_rows: list[dict], new_rows: list[dict]) -> list[dict]:
    """설비별 이전 배정 -> 새 배정 (팀·시작·종료 시각 변화 포함)."""
    old = {r["machine_id"]: r for r in old_rows}
    out = []
    for r in new_rows:
        o = old.get(r["machine_id"])
        out.append({
            "machine_id": r["machine_id"], "comp": r["comp"],
            "before": o["slot"] if o else "-", "after": r["slot"],
            "before_team": (o or {}).get("team") or "-", "after_team": r.get("team") or "-",
            "before_time": _when(o), "after_time": _when(r),
            "task_type": TASK_LABEL.get(r.get("task_type"), "-"),
            "changed": (o["slot"] if o else None) != r["slot"],
        })
    return out


def replan(as_of: str, reason: str) -> dict:
    """거절 사유를 반영해 배정안 v{n}을 만든다.
    {"answer", "constraints", "plan", "drafts"(새 버전 초안), "version", "diff", "superseded"}"""
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    findings = load_findings(as_of)
    current = load_plan(as_of)
    empty = {"constraints": None, "drafts": [], "diff": [], "superseded": []}
    if current is None or not findings:
        return {**empty, "answer": "재계획할 배정안이 없습니다. 먼저 오늘 점검(조사·계획)을 실행하세요.",
                "plan": [], "version": None}

    versions = plan_versions(as_of) or [current["version"]]
    cur_v = max(versions + [current["version"]])
    limit = load_settings()["max_replan"]  # 설정 페이지에서 바꿀 수 있음 (기본 MAX_REPLANS)
    if cur_v - 1 >= limit:
        return {**empty, "answer": f"재계획 한도 초과: 오늘은 이미 {cur_v - 1}회 재계획했습니다 (하루 최대 {limit}회)."
                                   f" 현재 배정안 v{cur_v}을 유지합니다.", "plan": current["rows"], "version": cur_v}

    constraints = parse_constraints(reason, findings, current["rows"])
    # 이전 재계획에서 제외한 설비는 계속 제외 (사유가 사라지지 않음). 팀 가용 여부는 새 사유 기준.
    prev_reasons = {int(k): v for k, v in (current.get("defer_reason") or {}).items()}
    carried = {m: r for m, r in prev_reasons.items() if m not in constraints.exclude_machines}
    # 팀별 가능 시간도 이전 조건을 유지 (같은 팀은 새 조건이 우선, 오늘 투입하지 않는 팀은 무시)
    prev_windows = {t: w for t, w in (current.get("team_available") or {}).items()
                    if t in constraints.available_teams and t not in (constraints.team_available or {})}
    if carried or prev_windows:
        notes = []
        if carried:
            notes.append(f"이전 제외 유지: {', '.join(f'#{m}' for m in carried)}")
        if prev_windows:
            notes.append(f"이전 시간 조건 유지: {prev_windows}")
        constraints = PlanConstraints(
            available_teams=constraints.available_teams,
            exclude_machines=[*constraints.exclude_machines, *carried],
            defer_reason={**constraints.defer_reason, **carried},
            note=constraints.note + f" ({'; '.join(notes)})",
            team_available={**prev_windows, **(constraints.team_available or {})} or None,
        )
    new_v = cur_v + 1
    superseded = supersede_pending(as_of, new_v)  # 이전 버전의 미결정 초안은 등록 불가
    rows = rank([f["machine_id"] for f in findings], CREWS, as_of,
                available_teams=constraints.available_teams, exclude_machines=constraints.exclude_machines,
                defer_reason=constraints.defer_reason, version=new_v, team_available=constraints.team_available,
                meta={"reason": reason, "constraints": constraints.model_dump(), "previous_version": cur_v})
    new_ids = _ensure_drafts(as_of, findings, auto_filled=False)  # 초안도 코드가 작성 (배정 결과 그대로)
    new_ids += _ensure_purchase_drafts(as_of)  # 재고 0 부품 발주 누락 검사 (내일 우선으로 밀린 부품도)
    diff = _diff(current["rows"], rows)

    plan_ = load_plan(as_of)
    plan_["diff"] = diff
    save_plan(as_of, plan_)

    diff_text = "\n".join(f"#{d['machine_id']} {d['comp']} {d['task_type']}: {d['before']} -> {d['after']}"
                          + (" (변경)" if d["changed"] else "") for d in diff)
    diff_text += "\n" + schedule_summary(plan_)
    try:
        answer = _llm().invoke([
            ("system", SUMMARY_PROMPT),
            ("user", f"거절 사유: {reason}\n해석: {constraints.note}\n가능 팀: {constraints.available_teams or '없음'}"
                     f"\n팀별 가능 시간: {constraints.team_available or '제약 없음'}"
                     f"\n제외 설비: {constraints.defer_reason or '없음'}\n배정안 v{cur_v} -> v{new_v} 변경표:\n{diff_text}"
                     f"\n대체된 이전 초안: {superseded or '없음'}\n새 draft_id: {new_ids or '없음'}"),
        ]).text
    except Exception as e:
        answer = f"배정안 v{new_v}로 다시 계산했습니다 ({constraints.note}). 요약 작성 실패: {type(e).__name__}"

    drafts = [d for d in load_drafts(as_of).values() if d.get("version", 1) == new_v]
    return {"answer": answer, "constraints": constraints.model_dump(), "plan": rows, "drafts": drafts,
            "version": new_v, "diff": diff, "superseded": superseded}


@tool
def replan_maintenance(reason: str, runtime: ToolRuntime[Ctx]) -> str:
    """담당자가 거절하며 남긴 사유(팀 부족, 특정 설비 정비 불가 등)를 반영해 정비 배정안을 다시 만든다.
    점수·배정은 코드가 계산하고, 새 작업 지시서·발주 초안(draft_id 끝 -v{n})을 만든다. 이전 미결정 초안은 대체된다.

    언제 쓰나: submit 요청이 사유와 함께 거절됐을 때, 또는 사용자가 사유를 들어 재계획을 요청할 때.
    언제 쓰지 않나: 사유 없는 거절, 처음 계획 (ask_planner_agent 사용). 하루 최대 3회.

    Args:
        reason: 거절·재계획 사유 원문 (예: "2팀 오늘 교육이라 불가")
    """
    out = replan(runtime.context.as_of, reason)
    if out["version"] is None or not out["constraints"]:
        return out["answer"]
    pending = sorted(d["draft_id"] for d in out["drafts"] if d["status"] == "PENDING")
    return (f"{out['answer']}\n\n[배정안 v{out['version']}]\n{plan_table(out['plan'])}"
            f"\n\n새 draft_id (모두 submit_* 호출): {', '.join(pending) if pending else '없음 (등록할 초안 없음)'}")
