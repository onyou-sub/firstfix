"""조사 에이전트: 고위험 설비 1대를 도구로 조사해 Finding을 만든다.

- build_investigator(): create_agent (gpt-5.4-mini, 도구 6개, Ctx 주입, Finding 구조화 출력, 도구 호출 실행당 6회)
- investigate_one(machine_id, comp, prob, as_of) -> (Finding, evidence_id)
  LLM 결과를 코드 규칙(최근 7일 내 교체)으로 한 번 더 확인하고, 조사 과정을 evidence/{YYYYMMDD}/{machine_id}.json에 남긴다.
- 길이 제한(schemas.LIMITS): 에이전트는 FindingDraft를 내고, 코드가 Finding 제한을 검사한다.
  넘치면 잘라내지 않고 "짧게 다시 작성"을 한 번 요청하고, 그래도 넘치는 필드만 말줄임 처리한다.
- 사실 확인(check_grounding): 원인 근거·이상 요약의 수치, 현장 요령의 날짜·WO 번호가 이번 도구 결과에 있는지 검사한다.
  어긋나면 "도구 결과 기준으로 고치거나 빼라"를 한 번 요청하고, 그래도 남은 원인·요령은 빼고 grounding_check에 기록한다.
  첫 단계가 안전 조치가 아니면 "설비 정지·잠금 확인"을 코드로 맨 앞에 넣는다.
"""

import json
import re
import sqlite3
import sys
from functools import cache
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

AGENTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = AGENTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

import progress  # noqa: E402
from memory.store import get_store, save_investigation  # noqa: E402
from schemas import LIMITS, Finding, FindingDraft, ellipsis, is_safety_step, sentences  # noqa: E402
from tools.context import Ctx  # noqa: E402
from tools.data_tools import COMP_SENSOR, DATA_TOOLS, DB_PATH, _maint_rows  # noqa: E402
from tools.memory_tools import recall_machine_memory, search_field_notes  # noqa: E402

EVIDENCE_DIR = ROOT_DIR / "evidence"
MODEL_NAME = "gpt-5.4-mini"
TOOL_CALL_LIMIT = 7  # 실행(invoke) 1회당 (현장 기록 검색 1회 포함)
RECENT_REPLACE_DAYS = 7
SENSOR_LABEL = "센서·설치 이상 가능성"
MAX_ITEMS = 4  # key_evidence, checklist 최대 개수

SYSTEM_PROMPT = f"""당신은 설비 정비 조사 담당자입니다. 설비 1대만 조사하고, 그 결론을 Finding 형식으로 냅니다.

[출발점]
- 사용자 메시지로 받은 예측 부품과 고장 확률을 출발점으로 삼습니다. 다른 설비는 조사하지 않습니다.
- 부품과 짝인 센서: motor=volt, drive=rotate, pump=pressure, bearing=vibration

[조사 순서] 도구 호출은 모두 합쳐 최대 {TOOL_CALL_LIMIT}회입니다.
0. recall_machine_memory: 조사 시작 시 과거 조사 기록과 엔지니어 피드백을 먼저 확인합니다.
1. get_sensor_trend: 예측 부품과 짝인 센서의 추세 (hours=72)
2. get_error_logs: 최근 에러 (days=7)
3. get_maintenance_history: 부품별 마지막 교체일과 경과일
4. find_similar_failures: 근거가 부족할 때만, 같은 model·comp의 과거 고장 전 센서 수준과 비교
5. search_manual: 점검 절차와 판단 기준. 최근 교체가 있으면 "부품 교체 후에도 이상이 계속됨"으로 검색
6. search_field_notes: recall_machine_memory에 작업자 보고가 있으면 이 설비(machine_id)의 현장 기록에서
   측정값·관찰·조치 후 상태를 확인합니다. 원인이 불분명하면 machine_id 없이 같은 부품의 다른 설비 사례를 찾습니다.
   현장 기록의 측정값을 쓸 때는 "현장 기록(WO-...)"이라고 출처를 밝힙니다.

[교체 시점 해석] 최근 교체가 있으면 반드시 지킵니다.
- get_maintenance_history의 마지막 교체 시각과 센서·에러 기록의 시각을 비교합니다.
- 교체 시각 이전(같은 시각 포함) 기록은 "교체 전" 상태입니다. "교체 후에도", "교체 직후에도"라고 쓰지 않습니다.
- 교체 시각 이후 기록만 "교체 후"로 씁니다.
  · 24시간 평균은 (기준 시각 - 24시간, 기준 시각] 구간입니다. 교체가 그 구간 시작 이후라면 평균에 교체 전 값이 섞여 있습니다.
  · 교체가 기준 시각과 같으면 조회한 센서 평균·에러는 모두 교체 전 기록입니다.
  · 에러는 "마지막 발생" 시각과 "최근 24시간" 횟수로 교체 후 발생 여부를 판단합니다. 교체 후 0건이면 "교체 후 에러 없음"으로 씁니다.

[과거 기억]
- 엔지니어 피드백(실제 원인, 현장 확인 결과)이 있으면 결론과 checklist에 반영합니다.
  반영했으면 summary에 "과거 피드백 반영"이라고 적고 피드백 내용을 짧게 인용합니다.
- 과거 기록은 참고용입니다. 현재 판정과 수치는 이번 조사 도구 결과로 정합니다.

[판정 규칙]
- 예측 부품을 기준 시각으로부터 최근 {RECENT_REPLACE_DAYS}일 안에 교체했다면(경과일 {RECENT_REPLACE_DAYS}.0 이하)
  label은 반드시 "{SENSOR_LABEL}"입니다. 이 경우 재교체보다 센서 고정·배선·설치 상태 점검을 먼저 권합니다.
- 그 외에는 label을 "예측 고장"으로 합니다.

[작성 규칙] 현장 작업자가 "무엇을 해야 하는지" 바로 보도록 짧게 씁니다.
- 도구 결과에 없는 수치는 절대 만들지 않습니다. 수치는 도구 결과·매뉴얼의 값을 그대로 옮깁니다.
- 문장은 짧은 서술형으로 쓰고, 한 항목에는 한 가지 내용만 넣습니다.
- 수치는 "24h 평균 50.4(평소 대비 +25%)"처럼 짧게 씁니다.
- action_summary: {LIMITS['action_summary']}자 이내 한 문장. steps 중 가장 중요한 1~2단계를 한 문장으로 씁니다.
- anomaly_summary: {LIMITS['anomaly_summary']}자 이내. 어디가, 언제부터, 얼마나.
- hypotheses: 원인 후보 최대 {LIMITS['hypotheses']}개 (가능성 높은 순). cause {LIMITS['hypothesis.cause']}자, evidence {LIMITS['hypothesis.evidence']}자 이내.
- steps: {LIMITS['steps_min']}~{LIMITS['steps']}개. 첫 단계는 "설비 정지·잠금 확인" 같은 안전 조치.
  action {LIMITS['step.action']}자, criterion {LIMITS['step.criterion']}자, if_abnormal {LIMITS['step.if_abnormal']}자 이내.
  criterion은 매뉴얼·도구 결과의 기준만 씁니다. 기준 수치가 없으면 "평소 수준과 비교"처럼 씁니다.
- field_tips: 현장 요령 최대 {LIMITS['field_tips']}개, 각 {LIMITS['field_tip']}자 이내.
- est_minutes: 예상 점검 시간(분) {LIMITS['est_minutes_min']}~{LIMITS['est_minutes_max']}.
- parts_tools: 필요한 부품·공구 최대 {LIMITS['parts_tools']}개, 각 {LIMITS['parts_tool']}자 이내.
- summary: {LIMITS['summary_sentences']}문장 이내. 무엇이 왜 위험한지, 무엇을 먼저 할지.
- key_evidence: 최대 {MAX_ITEMS}개, 각 항목에 수치 하나 (예: "전압 24h 평균 186.2(평소 대비 +9.0%)").
- checklist: 현장 점검 항목 최대 {MAX_ITEMS}개. 매뉴얼 체크리스트를 근거로 짧게.
- manual_source: 참고한 매뉴얼 출처를 "파일명 §절" 형식으로. 매뉴얼을 쓰지 않았으면 "".
- 모든 문장은 한국어로 씁니다.

[좋은 예]
- action_summary: "하우징 온도·윤활 확인 후 이상 시 베어링 교체"
- anomaly_summary: "진동 72h 상승, 24h 평균 50.4(평소 대비 +25%)"
- step: action "하우징 온도 측정" / criterion "평소 수준과 비교" / if_abnormal "윤활 보충 후 재측정"
[나쁜 예] (길고, 한 항목에 여러 내용, 수치가 장황함)
- action_summary: "진동 센서의 최근 72시간 추세를 보면 평균이 크게 올랐으므로 하우징 온도, 윤활 상태, 축 정렬 상태 등을
  종합적으로 점검한 다음 필요하다면 베어링 교체를 검토하는 것이 바람직합니다"
"""

REWRITE_PROMPT = """당신은 설비 정비 조사 결과 편집자입니다. 아래 Finding JSON에서 [고칠 점]에 적힌 필드만 제한 이내로 짧게 다시 씁니다.
- 수치·판정·의미는 그대로 두고, 새 수치나 새 내용을 만들지 않습니다.
- 짧은 서술형, 한 항목에 한 가지 내용만. 수치는 "24h 평균 50.4(평소 대비 +25%)"처럼 짧게.
- 개수가 넘치면 덜 중요한 항목을 뺍니다. steps가 모자라면 checklist에서 단계로 옮깁니다.
- [고칠 점]에 없는 필드는 그대로 둡니다."""

GROUND_PROMPT = """당신은 설비 정비 조사 결과 검수자입니다. 아래 Finding JSON에서 [근거 미확인]에 적힌 항목을 [도구 결과] 기준으로 고칩니다.
- 수치·날짜·WO 번호는 [도구 결과]에 있는 값만 그대로 옮깁니다. 도구 결과로 확인할 수 없으면 그 항목(원인 후보·현장 요령)을 뺍니다.
- hypotheses의 evidence는 도구 결과의 수치나 기록을 10자 이상으로 적습니다.
- 길이 제한과 짧은 서술형은 그대로 지킵니다. [근거 미확인]에 없는 필드는 그대로 둡니다."""


@cache
def _llm() -> ChatOpenAI:
    load_dotenv(ROOT_DIR / ".env")
    return ChatOpenAI(model=MODEL_NAME, temperature=0)


@cache
def build_investigator():
    load_dotenv(ROOT_DIR / ".env")
    return create_agent(
        model=_llm(),
        tools=[*DATA_TOOLS, recall_machine_memory, search_field_notes],
        system_prompt=SYSTEM_PROMPT,
        middleware=[ToolCallLimitMiddleware(run_limit=TOOL_CALL_LIMIT, exit_behavior="continue")],
        context_schema=Ctx,
        response_format=FindingDraft,  # 길이 제한은 코드가 Finding으로 검사 (finalize_finding)
        store=get_store(),
    )


def _machine_info(machine_id: int) -> tuple[str, int]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT model, age FROM machines WHERE machine_id = ?", (machine_id,)).fetchone()
    if row is None:
        raise ValueError(f"machines에 없는 설비: {machine_id}")
    return row[0], int(row[1])


def _user_message(machine_id: int, comp: str, prob: float, as_of: str, model: str, age: int) -> str:
    return (
        f"기준 시각 {as_of}, 다음 설비 1대를 조사하세요.\n"
        f"- 설비 번호: {machine_id}\n"
        f"- 모델: {model}, 사용 연수: {age}년\n"
        f"- 예측 부품: {comp} (짝 센서: {COMP_SENSOR[comp]})\n"
        f"- 24시간 내 고장 확률: {prob:.4f}"
    )


def _tool_calls(messages: list) -> list[dict]:
    """AI 메시지의 도구 호출과 그 결과(ToolMessage)를 순서대로 짝짓는다."""
    outputs = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    calls = []
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        for tc in m.tool_calls:
            out = outputs.get(tc["id"])
            calls.append({
                "name": tc["name"],
                "input": tc["args"],
                "output": out.content if out is not None else None,
                "status": getattr(out, "status", None) if out is not None else "no_output",
            })
    return calls


def _recent_replacement(machine_id: int, comp: str, as_of: str) -> dict:
    """코드 규칙: as_of 기준 comp의 마지막 교체가 7일 이내인지 (as_of 이하 기록만 사용)."""
    maint = _maint_rows(machine_id, as_of)
    rows = maint[maint["comp"] == comp]
    if rows.empty:
        return {"last_replaced": None, "days_since": None, "recent": False}
    last = rows["datetime"].max()
    days = (pd.Timestamp(as_of) - last).total_seconds() / 86400
    return {"last_replaced": str(last), "days_since": round(days, 2), "recent": days <= RECENT_REPLACE_DAYS}


# ---------------------------------------------------------------- 길이 제한 (검사 -> 다시 쓰기 1회 -> 말줄임)
def length_problems(d: dict) -> list[str]:
    """Finding 길이 제한을 어긴 곳을 한국어로 (비어 있으면 통과)."""
    out = []

    def text(name: str, value: str, limit: int) -> None:
        if len(value or "") > limit:
            out.append(f"{name}: {len(value)}자 -> {limit}자 이내")

    def count(name: str, items: list, hi: int, lo: int = 0) -> None:
        if not lo <= len(items) <= hi:
            out.append(f"{name}: {len(items)}개 -> {lo}~{hi}개" if lo else f"{name}: {len(items)}개 -> 최대 {hi}개")

    text("action_summary", d["action_summary"], LIMITS["action_summary"])
    text("anomaly_summary", d["anomaly_summary"], LIMITS["anomaly_summary"])
    count("hypotheses", d["hypotheses"], LIMITS["hypotheses"])
    for i, h in enumerate(d["hypotheses"]):
        text(f"hypotheses[{i}].cause", h["cause"], LIMITS["hypothesis.cause"])
        text(f"hypotheses[{i}].evidence", h["evidence"], LIMITS["hypothesis.evidence"])
    count("steps", d["steps"], LIMITS["steps"], LIMITS["steps_min"])
    for i, st in enumerate(d["steps"]):
        for k in ("action", "criterion", "if_abnormal"):
            text(f"steps[{i}].{k}", st[k], LIMITS[f"step.{k}"])
    count("field_tips", d["field_tips"], LIMITS["field_tips"])
    for i, t in enumerate(d["field_tips"]):
        text(f"field_tips[{i}]", t, LIMITS["field_tip"])
    count("parts_tools", d["parts_tools"], LIMITS["parts_tools"])
    for i, t in enumerate(d["parts_tools"]):
        text(f"parts_tools[{i}]", t, LIMITS["parts_tool"])
    if not LIMITS["est_minutes_min"] <= d["est_minutes"] <= LIMITS["est_minutes_max"]:
        out.append(f"est_minutes: {d['est_minutes']} -> {LIMITS['est_minutes_min']}~{LIMITS['est_minutes_max']}")
    if (n := len(sentences(d["summary"]))) > LIMITS["summary_sentences"]:
        out.append(f"summary: {n}문장 -> {LIMITS['summary_sentences']}문장 이내")
    return out


def _rewrite_shorter(d: dict, problems: list[str]) -> dict:
    """"짧게 다시 작성" 요청 1회. 판정·설비·근거 필드는 원래 값 그대로 둔다."""
    request = ("[고칠 점]\n" + "\n".join(f"- {p}" for p in problems)
               + "\n\n[Finding JSON]\n" + json.dumps(d, ensure_ascii=False, indent=1))
    draft = _llm().with_structured_output(FindingDraft).invoke([("system", REWRITE_PROMPT), ("user", request)])
    keep = ("machine_id", "comp", "label", "key_evidence", "checklist", "manual_source")
    return {**draft.model_dump(), **{k: d[k] for k in keep}}


def _ellipsize(d: dict) -> dict:
    """다시 써도 넘치는 필드만 말줄임 (개수는 앞에서부터, 글자는 끝을 …로). steps가 모자라면 checklist로 채운다."""
    d = json.loads(json.dumps(d, ensure_ascii=False))
    lim = LIMITS
    d["action_summary"] = ellipsis(d["action_summary"], lim["action_summary"])
    d["anomaly_summary"] = ellipsis(d["anomaly_summary"], lim["anomaly_summary"])
    d["hypotheses"] = [{"cause": ellipsis(h["cause"], lim["hypothesis.cause"]),
                        "evidence": ellipsis(h["evidence"], lim["hypothesis.evidence"])}
                       for h in d["hypotheses"][:lim["hypotheses"]]]
    steps = d["steps"][:lim["steps"]]
    for item in d["checklist"]:
        if len(steps) >= lim["steps_min"]:
            break
        steps.append({"action": item, "criterion": "매뉴얼 기준 확인", "if_abnormal": "담당자에게 보고"})
    d["steps"] = [{k: ellipsis(st[k], lim[f"step.{k}"]) for k in ("action", "criterion", "if_abnormal")}
                  for st in steps]
    d["field_tips"] = [ellipsis(t, lim["field_tip"]) for t in d["field_tips"][:lim["field_tips"]]]
    d["parts_tools"] = [ellipsis(t, lim["parts_tool"]) for t in d["parts_tools"][:lim["parts_tools"]]]
    d["est_minutes"] = min(max(int(d["est_minutes"]), lim["est_minutes_min"]), lim["est_minutes_max"])
    if len(sentences(d["summary"])) > lim["summary_sentences"]:
        d["summary"] = " ".join(sentences(d["summary"])[:lim["summary_sentences"]])
    return d


def finalize_finding(d: dict, rewrite=None) -> tuple[Finding, dict]:
    """FindingDraft(dict) -> 길이 제한을 지킨 Finding.
    (Finding, 기록 {"problems", "rewritten", "after_rewrite", "ellipsized"}). rewrite는 테스트용 대체 함수."""
    rewrite = rewrite or _rewrite_shorter
    log = {"problems": length_problems(d), "rewritten": False, "after_rewrite": [], "ellipsized": False}
    if log["problems"]:
        try:
            d = rewrite(d, log["problems"])
            log["rewritten"] = True
        except Exception as e:  # 다시 쓰기 실패 -> 말줄임으로 진행
            log["rewrite_error"] = f"{type(e).__name__}: {e}"
        log["after_rewrite"] = length_problems(d)
        if log["after_rewrite"]:
            d = _ellipsize(d)
            log["ellipsized"] = True
    try:
        return Finding.model_validate(d), log
    except ValidationError:  # 검사표에 없는 위반이 남아 있으면 말줄임 후 한 번 더
        log["ellipsized"] = True
        return Finding.model_validate(_ellipsize(d)), log


# ---------------------------------------------------------------- 사실 확인 (검사 -> 고쳐 쓰기 1회 -> 근거 없는 항목 빼기)
NUM_RE = re.compile(r"(?<![\d.])[+-]?\d+(?:,\d{3})*(?:\.\d+)?%?")
DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b|(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
WO_RE = re.compile(r"WO-\d+(?:-\d+)*", re.IGNORECASE)
MEMORY_TOOLS = ("search_field_notes", "recall_machine_memory")
MIN_EVIDENCE_CHARS = 10
SAFETY_STEP = {"action": "설비 정지·잠금 확인", "criterion": "잠금 태그 부착", "if_abnormal": "작업 중지"}


def _numbers(text: str) -> list[float]:
    return [abs(float(m.group().rstrip("%").replace(",", ""))) for m in NUM_RE.finditer(text or "")]


def _dates(text: str) -> set[tuple]:
    """날짜 -> {(월, 일), (연, 월, 일)} (YYYY-MM-DD는 둘 다, MM/DD는 (월, 일)만)."""
    out = set()
    for m in DATE_RE.finditer(text or ""):
        if m.group(1):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            out |= {(mo, d), (y, mo, d)}
        else:
            out.add((int(m.group(4)), int(m.group(5))))
    return out


def _outputs(tool_calls: list[dict], names: tuple[str, ...] | None = None) -> str:
    return "\n".join(str(c.get("output") or "") for c in tool_calls if names is None or c.get("name") in names)


def _grounding_issues(d: dict, tool_calls: list[dict]) -> list[tuple[str, int | None, str]]:
    """(필드, 순번, 문제) 목록. 순번은 빼낼 수 있는 항목(hypotheses·field_tips)의 위치."""
    seen = {round(n, 1) for n in _numbers(_outputs(tool_calls))}
    memory = _outputs(tool_calls, MEMORY_TOOLS)
    memory_dates, memory_wos = _dates(memory), {w.upper() for w in WO_RE.findall(memory)}
    out = []

    def numbers(field: str, idx: int | None, text: str) -> None:
        missing = [m.group() for m in NUM_RE.finditer(text or "") if round(_numbers(m.group())[0], 1) not in seen]
        if missing:
            out.append((field, idx, f"{field if idx is None else f'{field}[{idx}]'}: 도구 결과에 없는 수치 {', '.join(missing)}"))

    numbers("anomaly_summary", None, d["anomaly_summary"])
    for i, h in enumerate(d["hypotheses"]):
        if len((h["evidence"] or "").strip()) < MIN_EVIDENCE_CHARS:
            out.append(("hypotheses", i, f"hypotheses[{i}].evidence: 근거가 비었거나 {MIN_EVIDENCE_CHARS}자 미만"))
        else:
            numbers("hypotheses", i, h["evidence"])
    for i, tip in enumerate(d["field_tips"]):
        missing = [m.group() for m in DATE_RE.finditer(tip) if not _dates(m.group()) & memory_dates]
        missing += [w for w in WO_RE.findall(tip) if w.upper() not in memory_wos]
        if missing:
            out.append(("field_tips", i, f"field_tips[{i}]: 현장 기록·과거 기억에 없는 날짜/WO {', '.join(missing)}"))
    return out


def check_grounding(finding: dict, tool_calls: list[dict]) -> list[str]:
    """Finding(dict)의 수치·날짜·WO 번호가 이번 조사 도구 결과에 있는지, 원인 근거가 충분한지, 첫 단계가 안전 조치인지.
    문제 목록을 한국어로 (비어 있으면 통과)."""
    out = [msg for _, _, msg in _grounding_issues(finding, tool_calls)]
    steps = finding.get("steps") or []
    if not steps or not is_safety_step(steps[0]["action"]):
        out.append("steps[0]: 안전 조치(정지·잠금·안전)가 아님")
    return out


def _ensure_safety_first(d: dict) -> tuple[dict, bool]:
    """첫 단계가 안전 조치가 아니면 SAFETY_STEP을 맨 앞에 넣는다 (최대 개수는 유지)."""
    if d["steps"] and is_safety_step(d["steps"][0]["action"]):
        return d, False
    return {**d, "steps": [dict(SAFETY_STEP), *d["steps"]][:LIMITS["steps"]]}, True


def _rewrite_grounded(d: dict, problems: list[str], tool_calls: list[dict]) -> dict:
    """"근거 없는 항목을 도구 결과 기준으로 고치거나 빼라" 요청 1회. 판정·설비·근거 필드는 원래 값 그대로 둔다."""
    results = "\n\n".join(f"## {c['name']} {json.dumps(c.get('input'), ensure_ascii=False)}\n{str(c.get('output') or '')[:3000]}"
                          for c in tool_calls)
    request = ("[근거 미확인]\n" + "\n".join(f"- {p}" for p in problems)
               + "\n\n[도구 결과]\n" + results
               + "\n\n[Finding JSON]\n" + json.dumps(d, ensure_ascii=False, indent=1))
    draft = _llm().with_structured_output(FindingDraft).invoke([("system", GROUND_PROMPT), ("user", request)])
    keep = ("machine_id", "comp", "label", "key_evidence", "checklist", "manual_source")
    return {**draft.model_dump(), **{k: d[k] for k in keep}}


def ground_finding(d: dict, tool_calls: list[dict], rewrite=None) -> tuple[dict, dict]:
    """FindingDraft(dict) -> 근거를 확인한 dict.
    (dict, 기록 {"problems", "rewritten", "after_rewrite", "removed", "unresolved", "safety_inserted"}).
    rewrite는 테스트용 대체 함수 (d, problems, tool_calls) -> d."""
    rewrite = rewrite or _rewrite_grounded
    d, safety_inserted = _ensure_safety_first(d)
    log = {"problems": check_grounding(d, tool_calls), "rewritten": False, "after_rewrite": [],
           "removed": [], "unresolved": [], "safety_inserted": safety_inserted}
    if not log["problems"]:
        return d, log
    try:
        d = rewrite(d, log["problems"], tool_calls)
        log["rewritten"] = True
    except Exception as e:  # 고쳐 쓰기 실패 -> 근거 없는 항목 빼기로 진행
        log["rewrite_error"] = f"{type(e).__name__}: {e}"
    d, again = _ensure_safety_first(d)
    log["safety_inserted"] |= again
    issues = _grounding_issues(d, tool_calls)
    log["after_rewrite"] = [msg for _, _, msg in issues]
    drop = {(f, i) for f, i, _ in issues if f in ("hypotheses", "field_tips")}
    for field in ("hypotheses", "field_tips"):
        log["removed"] += [{"field": field, "item": x} for i, x in enumerate(d[field]) if (field, i) in drop]
    d = {**d, **{field: [x for i, x in enumerate(d[field]) if (field, i) not in drop]
                 for field in ("hypotheses", "field_tips")}}
    log["unresolved"] = [msg for f, _, msg in issues if f not in ("hypotheses", "field_tips")]  # 이상 요약은 빼지 않고 기록만
    return d, log


def investigate_one(machine_id: int, comp: str, prob: float, as_of: str) -> tuple[Finding, str]:
    """설비 1대 조사. (Finding, evidence_id)를 돌려주고 evidence JSON을 저장한다."""
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    progress.start(as_of, machine_id)  # 화면 진행 막대용 (도구 호출 수)
    try:
        model, age = _machine_info(machine_id)
        agent = build_investigator()
        result = agent.invoke(
            {"messages": [{"role": "user", "content": _user_message(machine_id, comp, prob, as_of, model, age)}]},
            {"callbacks": [progress.ToolCounter(as_of, machine_id)]},
            context=Ctx(as_of=as_of),
        )
    except Exception as e:
        progress.finish(as_of, machine_id, error=f"{type(e).__name__}: {e}")
        raise
    draft: FindingDraft | None = result.get("structured_response")
    if draft is None:
        raise RuntimeError(f"설비 {machine_id}: 에이전트가 Finding을 돌려주지 않았습니다")

    # 입력과 다른 설비·부품으로 답하거나 근거·체크리스트 항목이 넘치는 경우를 코드로 고정
    llm_finding = draft.model_dump()
    tool_calls = _tool_calls(result["messages"])
    fixed = {**llm_finding, "machine_id": machine_id, "comp": comp,
             "key_evidence": draft.key_evidence[:MAX_ITEMS], "checklist": draft.checklist[:MAX_ITEMS]}
    grounded, grounding_check = ground_finding(fixed, tool_calls)  # 사실 확인 -> 길이 제한 순서
    finding, length_check = finalize_finding(grounded)

    # 코드 규칙으로 한 번 더 확인: 최근 7일 내 교체면 label 강제
    rule = _recent_replacement(machine_id, comp, as_of)
    rule_override = rule["recent"] and finding.label != SENSOR_LABEL
    if rule_override:
        finding = finding.model_copy(update={"label": SENSOR_LABEL})

    ts = pd.Timestamp(as_of)
    evidence_id = f"E{ts:%m%d}-{machine_id:03d}"
    evidence = {
        "evidence_id": evidence_id,
        "as_of": as_of,
        "input": {"machine_id": machine_id, "comp": comp, "prob": prob, "model": model, "age": age},
        "tool_calls": tool_calls,
        "llm_finding": llm_finding,
        "finding": finding.model_dump(),
        "rule_check": rule,
        "rule_override": rule_override,
        "length_check": length_check,
        "grounding_check": grounding_check,
    }
    path = EVIDENCE_DIR / f"{ts:%Y%m%d}" / f"{machine_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    save_investigation(machine_id, as_of, finding.model_dump(), evidence_id)  # 장기 기억에 자동 저장
    progress.finish(as_of, machine_id)
    return finding, evidence_id

