"""Supervisor: 사용자 요청을 받아 전문 에이전트·도구를 조율한다.

조립: create_agent + 미들웨어(input_guard, HITL 승인, 요약, 모델 호출 상한) + SqliteSaver + pack_result
- 대화 기록·승인 대기 위치는 data/checkpoints.db에 남아 서버를 다시 시작해도 같은 thread_id로 이어진다.
- run(message, as_of, thread_id) -> dict      : 한 턴 실행 (승인 대기면 status="pending")
- resume(thread_id, as_of, decisions) -> dict : 승인/거절 결정으로 이어서 실행
- chat_history(thread_id, as_of) -> list[dict] : 체크포인터에서 화면용 채팅 기록 복원
"""

import sys
from functools import cache
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentState,
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ModelRequest,
    SummarizationMiddleware,
    before_model,
    dynamic_prompt,
)
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime
from langgraph.types import Command

AGENTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = AGENTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.planner import _ensure_purchase_drafts, ask_planner_agent, replan_maintenance  # noqa: E402
from agents.report import ask_report_agent  # noqa: E402
from memory.persist import get_checkpointer  # noqa: E402
from memory.store import get_store  # noqa: E402
from config import CONTROL_WORDS, SUPERVISOR_MODEL_CALL_LIMIT, TOOL_NAMES as T  # noqa: E402
from tools.context import Ctx  # noqa: E402
from tools.investigate_tool import investigate_machines  # noqa: E402
from tools.memory_tools import get_engineer_feedback  # noqa: E402
from tools.plan_tools import (  # noqa: E402
    _stock, draft_purchase_request, expected_arrival, load_drafts, load_plan, purchase_qty, set_draft_status,
)
from tools.supervisor_tools import (  # noqa: E402
    ask_data_agent,
    predict_failure,
    submit_purchase_request,
    submit_work_order,
)

MODEL_NAME = "gpt-5.4-mini"
CONTROL_BLOCK_MESSAGE = "설비 직접 제어 요청은 처리할 수 없습니다. 이 시스템은 조회와 초안 작성만 합니다."
SUPERVISOR_TOOLS = [predict_failure, investigate_machines, ask_planner_agent, ask_data_agent,
                    ask_report_agent, submit_work_order, submit_purchase_request, replan_maintenance,
                    get_engineer_feedback, draft_purchase_request]
SUBMIT_TOOLS = {T["submit_wo"], T["submit_pr"]}
DAILY_MESSAGE = "오늘 점검을 시작해줘"  # [오늘 점검 시작] 버튼
RESTOCK_PREFIX = "재고가 0인 부품의 보충 발주 초안을 등록 요청해줘: "
SUBMIT_PREFIX = "계획 결과의 등록 대상 초안을 빠짐없이 등록 요청해줘: "
REPLAN_PREFIX = "정비 계획을 다시 세워줘. 재계획 사유: "  # [재계획 요청] 버튼
REPLAN_AGAIN_PREFIX = "방금 거절한 사유로 정비 계획을 다시 세워줘. 재계획 사유: "
AUTO_PREFIXES = (RESTOCK_PREFIX, SUBMIT_PREFIX, REPLAN_PREFIX, REPLAN_AGAIN_PREFIX)

assert {t.name for t in SUPERVISOR_TOOLS} == {T[k] for k in
        ["predict", "investigate", "planner", "data", "report", "submit_wo", "submit_pr", "replan", "feedback",
         "draft_pr"]}, "TOOL_NAMES와 도구 이름 불일치"

SUPERVISOR_PROMPT = f"""당신은 설비 정비 운영 Supervisor입니다. 직접 계산하지 않고 도구와 전문 에이전트에게 맡깁니다.

["오늘 점검을 시작해줘" 처리 순서]
1. {T['predict']}
2. 고위험이 0대면 {T['report']} 호출 후 "오늘 정비 대상 없음"으로 짧게 답하고 끝냅니다.
   (재고 0 부품의 보충 발주 초안은 코드가 따로 만들어 등록 요청을 지시합니다. 그때 등록 도구를 호출합니다.)
3. 고위험 설비 번호 전부로 {T['investigate']} (한 번에)
4. {T['planner']}
5. 계획 결과의 draft_id마다 등록 도구를 호출합니다. 모든 등록 호출을 한 번의 응답에서 함께 호출합니다.
   - WO-로 시작하면 {T['submit_wo']}, PR-로 시작하면 {T['submit_pr']}
   - 사람 승인 후에만 실행되며, 거절된 초안은 다시 등록하지 않습니다.
6. {T['report']}
7. 최종 답변: 고위험 대수, 팀 배정, 승인·거절 결과, 리포트 경로를 짧게

[거절과 재계획]
- 등록 요청이 거절되고 거절 메시지에 사유("with reason: 재계획 사유: ...")가 있으면 {T['replan']}(사유 원문)을 한 번 호출하고,
  결과의 "새 draft_id"마다 등록 도구를 다시 호출합니다 (한 번의 응답에서 함께). 새 draft_id가 없으면 등록하지 않습니다.
- 사유가 없는 거절은 재계획하지 않습니다. 그 초안은 다시 등록하지 않고 다음 단계({T['report']})로 갑니다.
- 사용자가 채팅으로 사유를 들어 재계획을 요청해도 {T['replan']}(사유)을 호출하고, 새 draft_id를 등록 요청합니다.
- {T['replan']}가 "재계획 한도 초과"를 돌려주면 다시 호출하지 말고 그대로 안내합니다.
- 재계획 뒤에도 등록 결정이 끝나면 {T['report']}를 호출해 리포트를 다시 저장합니다.

[엔지니어 피드백]
- "피드백", "현장 확인 결과", "엔지니어가 남긴 내용"을 물으면 {T['feedback']}(설비 번호, 없으면 비움)를 호출해 그대로 전합니다.
- 특정 설비가 왜 위험한지, 무엇을 점검할지 물으면 답하기 전에 {T['feedback']}(N)으로 피드백을 확인하고,
  있으면 "엔지니어 피드백: ..."으로 답에 반영합니다 (조사 결론보다 현장 확인 결과를 우선 참고).

[발주 요청]
- 사용자가 발주 요청을 올려 달라고 하면 {T['draft_pr']}(comp, qty, order_date, note)를 호출한 뒤,
  결과의 draft_id로 {T['submit_pr']}를 호출해 승인 대기로 올립니다 (사람 승인 후 등록).
- 발주 요청만 받았을 때는 {T['predict']}, {T['investigate']}, {T['planner']}를 부르지 않습니다 (점검·계획을 새로 세우지 않음).
  {T['draft_pr']} → {T['submit_pr']} 두 도구만 씁니다.
- 부품·수량을 말하지 않으면 아래 [오늘 재고 0 부품]의 부품과 권장 수량을 씁니다. 재고 0 부품이 없으면 발주하지 않고 그렇게 안내합니다.
- 사용자가 "내일", "모레", "다음 주 월요일", "10월 5일"처럼 날짜를 말하면 아래 [기준 시각]과 [날짜표]로 날짜를 계산해
  order_date="YYYY-MM-DD"로 넣습니다. 날짜를 직접 셈하지 말고 날짜표에서 고릅니다.
- 날짜를 말하지 않으면 order_date를 비우고(= 오늘), note에 "입고까지 리드타임 N일이 걸려 오늘 요청을 권장" 한 줄을 넣습니다.
  날짜를 말했으면 note에 "희망 발주일 요청: (사용자 표현)"만 적고 오늘 요청 권장 문구는 넣지 않습니다.
- 발주 요청을 만든 뒤에는 답변에 반드시 "부품, 수량, 희망 발주일, 상태(승인 대기)"를 적습니다.
  예: "pump(밸브/펌프 교체 키트) 2개, 희망 발주일 2015-10-02, 승인 대기 1건을 올렸습니다."
  날짜를 말하지 않았으면 리드타임 때문에 오늘 요청을 권장한다는 이유를 한 줄 덧붙입니다.

[그 밖의 질문]
- 통계·건수·비율·목록 질문은 {T['data']}에 질문을 그대로 넘깁니다.
  작업 지시·완료 보고·발주 통계(예: "어제 완료된 작업 몇 건?", "예측 적중률은?")도 {T['data']}로 보냅니다.
- "N번은 왜?", "1팀 설비는 왜 1순위?" 같은 질문은 대화에 남은 조사·계획 결과로 답합니다.
  이전 {T['investigate']} 결과에 "#N" 줄이 있거나 {T['planner']} 답변에 그 설비가 있으면 도구를 다시 부르지 않습니다.
  대화 어디에도 그 설비의 조사 결과가 없을 때만 {T['investigate']}([N])을 호출합니다.
  1순위 사유는 계획 결과의 점수 구성(확률 × 중요도 × 재고)과 조사 근거로 설명합니다.
- {T['planner']}는 점검 1회에 한 번만 호출합니다. 설명 질문 때문에 계획을 다시 세우지 않습니다.

[규칙]
- 도구 결과에 없는 수치는 만들지 않습니다. 설비 번호는 "#84"처럼 씁니다.
- 답변은 한국어로 간결하게 씁니다.
"""


WEEKDAY_KO = "월화수목금토일"


def today_block(as_of: str) -> str:
    """기준 시각·날짜표·오늘 재고 0 부품 (매 모델 호출마다 코드가 계산해 시스템 프롬프트 끝에 붙인다)."""
    now = pd.Timestamp(as_of)
    day = now.normalize()
    next_monday = day + pd.Timedelta(days=7 - day.weekday())
    table = ", ".join(f"{d:%Y-%m-%d}({WEEKDAY_KO[d.weekday()]})" for d in pd.date_range(day, periods=15))
    stock = _stock(as_of)
    zero = [f"{c}({s['part_name']}): 재고 0, 권장 발주 수량 {purchase_qty(c, as_of)}, 입고 소요 {int(s['lead_time_days'])}일"
            for c, s in stock.items() if int(s["qty"]) == 0]
    return (f"\n[기준 시각] {now:%Y-%m-%d %H:%M} ({WEEKDAY_KO[day.weekday()]}요일). 오늘 = {day:%Y-%m-%d},"
            f" 내일 = {day + pd.Timedelta(days=1):%Y-%m-%d}, 모레 = {day + pd.Timedelta(days=2):%Y-%m-%d},"
            f" 다음 주 월요일 = {next_monday:%Y-%m-%d}"
            f"\n[날짜표] {table}"
            f"\n[오늘 재고 0 부품] {'; '.join(zero) or '없음'}")


@dynamic_prompt
def supervisor_prompt(request: ModelRequest) -> str:
    return SUPERVISOR_PROMPT + today_block(request.runtime.context.as_of)


@before_model(can_jump_to=["end"])
def input_guard(state: AgentState, runtime: Runtime[Ctx]) -> dict | None:
    """마지막 사람 메시지에 설비 직접 제어 단어가 있으면 모델을 부르지 않고 끝낸다."""
    last_human = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
    if last_human is None:
        return None
    text = last_human.text.lower()
    if any(word.lower() in text for word in CONTROL_WORDS):
        return {"messages": [AIMessage(CONTROL_BLOCK_MESSAGE)], "jump_to": "end"}
    return None


@cache
def build_supervisor():
    """프로세스당 하나 (체크포인터 연결도 프로세스당 하나. 대화는 SQLite에 남아 새 프로세스에서도 resume·채팅이 이어진다)."""
    load_dotenv(ROOT_DIR / ".env")
    model = ChatOpenAI(model=MODEL_NAME, temperature=0)
    approval = {"allowed_decisions": ["approve", "reject"]}
    return create_agent(
        model=model,
        tools=SUPERVISOR_TOOLS,
        context_schema=Ctx,
        checkpointer=get_checkpointer(),  # data/checkpoints.db
        store=get_store(),  # 조사 에이전트와 같은 장기 기억 저장소
        middleware=[
            supervisor_prompt,  # SUPERVISOR_PROMPT + 기준 시각·날짜표·재고 0 부품
            input_guard,
            HumanInTheLoopMiddleware(
                interrupt_on={T["submit_wo"]: approval, T["submit_pr"]: approval},
                description_prefix="등록 승인 필요",
                interrupt_mode="batched",
            ),
            SummarizationMiddleware(model=model, trigger=("messages", 30), keep=("messages", 10)),
            # run_limit은 invoke 1회(첫 실행, resume 1회, 채팅 1회)마다 0부터 센다 (run_model_call_count는
            # UntrackedValue라 체크포인트에 남지 않음). 재계획 1회 = resume 1회 안에서 모델 약 3번
            # (replan -> submit -> HITL), 승인 후 resume에서 약 3번 (report -> 답변)이라 15면 재계획 3회까지 충분.
            # thread_limit은 대화 전체에 누적되므로 걸지 않는다 (같은 스레드에서 재계획·채팅이 이어짐).
            ModelCallLimitMiddleware(run_limit=SUPERVISOR_MODEL_CALL_LIMIT),
        ],
    )


def _config(thread_id: str, callbacks: list | None = None) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    if callbacks:  # 화면 진행 표시용 (services.py)
        config["callbacks"] = callbacks
    return config


def _requests(interrupts) -> list[dict]:
    """HITL interrupt -> [{"tool", "draft_id"}] (batched 모드: action_requests 순서 = decisions 순서)"""
    out = []
    for intr in interrupts:
        for req in (intr.value or {}).get("action_requests", []):
            out.append({"tool": req["name"], "draft_id": req["args"].get("draft_id")})
    return out


def pack_result(result) -> dict:
    """invoke(version="v2") 결과를 화면·테스트용 dict로."""
    if result.interrupts:
        return {"status": "pending", "requests": _requests(result.interrupts)}
    messages = result.value["messages"]
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    after = messages[last_human + 1:]
    used = [tc["name"] for m in after if isinstance(m, AIMessage) for tc in m.tool_calls]
    final = next((m for m in reversed(after) if isinstance(m, AIMessage) and not m.tool_calls), None)
    return {"status": "done", "answer": final.text if final else "", "used_tools": used}


def pending_answer(requests: list[dict], as_of: str) -> str:
    """승인 대기로 멈췄을 때 답변 (모델 답변이 없으므로 코드가 작성): 발주는 부품·수량·희망 발주일·상태를 반드시 적는다."""
    drafts = load_drafts(as_of)
    today = pd.Timestamp(as_of).strftime("%Y-%m-%d")
    prs = [drafts[r["draft_id"]] for r in requests if r["tool"] == T["submit_pr"] and r["draft_id"] in drafts]
    n_wo = sum(r["tool"] == T["submit_wo"] for r in requests)
    lines = []
    for d in prs:
        order = d.get("order_date") or today
        arrival = expected_arrival(order, d["lead_time_days"])
        lines.append(f"{d['comp']}({d['part_name']}) {d['qty']}개, 희망 발주일 {order}, 승인 대기")
        if order == today:
            lines.append(f"  입고까지 리드타임 {d['lead_time_days']}일이 걸려 오늘 요청을 권장합니다 (예상 입고 {arrival}).")
        else:
            lines.append(f"  리드타임 {d['lead_time_days']}일 → 예상 입고 {arrival[5:]}.")
    if len(prs) == 1 and not n_wo:
        first, *rest = lines
        return "\n".join([first.replace(", 승인 대기", ", 승인 대기 1건을 올렸습니다."), *(l.strip() for l in rest)])
    if n_wo:
        lines.append(f"작업 지시서 승인 대기 {n_wo}건")
    return f"승인 대기 {len(requests)}건을 올렸습니다.\n" + "\n".join(lines)


def _pack(result, as_of: str) -> dict:
    out = pack_result(result)
    if out["status"] == "pending":
        out["answer"] = pending_answer(out["requests"], as_of)
    return out


def run(message: str, as_of: str, thread_id: str, callbacks: list | None = None, daily: bool = False) -> dict:
    """daily=True: "오늘 점검" 실행. 끝난 뒤 재고 0 부품 보충 발주를 코드가 확인한다 (고위험 0대인 날 포함)."""
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    agent = build_supervisor()
    result = agent.invoke(
        {"messages": [{"role": "user", "content": message}]},
        _config(thread_id, callbacks), context=Ctx(as_of=as_of), version="v2",
    )
    return _pack(_ensure_submissions(agent, result, thread_id, as_of, callbacks, daily=daily), as_of)


def _ensure_submissions(agent, result, thread_id: str, as_of: str, callbacks: list | None, daily: bool = False):
    """코드 보완: 계획·재계획 뒤 등록 요청 없이 끝났는데 PENDING 초안이 남아 있으면 등록 요청을 한 번 더 지시한다.
    daily(오늘 점검)이면 재고 0 부품 보충 발주 초안을 먼저 만든다 (고위험 0대라 계획을 건너뛴 날에도 발주가 올라가게)."""
    if result.interrupts:
        return result
    packed = pack_result(result)
    planned = {T["planner"], T["replan"], T["draft_pr"]} & set(packed["used_tools"])
    restock = _ensure_purchase_drafts(as_of) if daily else []
    submitted = SUBMIT_TOOLS & set(packed["used_tools"])
    pending = sorted(k for k, d in load_drafts(as_of).items() if d["status"] == "PENDING")
    if restock:  # 재고 보충 발주: 이번 실행에서 다른 등록 요청이 있었더라도 새로 만든 초안은 올린다
        pending = sorted(set(restock) & set(pending))
        message = RESTOCK_PREFIX + ", ".join(pending)
    elif not planned or submitted or not pending:
        return result
    else:
        message = SUBMIT_PREFIX + ", ".join(pending)
    if not pending:
        return result
    return agent.invoke({"messages": [{"role": "user", "content": message}]},
                        _config(thread_id, callbacks), context=Ctx(as_of=as_of), version="v2")


def pending_requests(thread_id: str) -> list[dict]:
    """이 스레드의 승인 대기 요청 [{"tool", "draft_id"}] (없으면 [])."""
    return _requests(build_supervisor().get_state(_config(thread_id)).interrupts)


def resume(thread_id: str, as_of: str, decisions: list[str], reason: str | None = None,
           callbacks: list | None = None, engineer_id: str | None = None) -> dict:
    """대기 중인 승인 요청에 결정을 내리고 이어서 실행한다. decisions: 요청 순서대로 "approve" / "reject".

    reason이 있으면 decisions와 상관없이 대기 중인 요청을 모두 reject하고, 거절 메시지에 사유를 담는다
    (HITL은 "User rejected the tool call ... with reason: {message}"로 모델에 전달 -> Supervisor가 replan_maintenance 호출).
    """
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    ctx = Ctx(as_of=as_of, engineer_id=engineer_id) if engineer_id else Ctx(as_of=as_of)  # 승인자 = 결정 제출자
    agent = build_supervisor()
    pending = _requests(agent.get_state(_config(thread_id)).interrupts)
    if reason:
        decisions = ["reject"] * len(pending)
    if len(decisions) != len(pending):
        raise ValueError(f"결정 {len(decisions)}개, 승인 요청 {len(pending)}개: 개수가 같아야 합니다")
    if bad := [d for d in decisions if d not in ("approve", "reject")]:
        raise ValueError(f"결정은 approve/reject만 가능합니다: {bad}")

    rejected = [r["draft_id"] for r, d in zip(pending, decisions)
                if d == "reject" and r["tool"] in SUBMIT_TOOLS and r["draft_id"]]
    if not reason:
        # 거절된 등록 도구는 실행되지 않으므로 초안 상태를 여기서 REJECTED로 바꾼다
        for draft_id in rejected:
            set_draft_status(as_of, draft_id, "REJECTED")
    # 사유 있는 거절은 PENDING으로 두고 재계획(replan)이 SUPERSEDED로 바꾼다.
    # 사유 없는 거절은 message를 비워 둔다 (모델이 재계획 사유로 오해하지 않도록 HITL 기본 문구 사용)
    hitl = [{"type": "approve"} if d == "approve"
            else ({"type": "reject", "message": f"재계획 사유: {reason} → 이 사유 원문으로 {T['replan']}를 호출하세요."}
                  if reason else {"type": "reject"})
            for d in decisions]
    version_before = (load_plan(as_of) or {}).get("version")
    result = agent.invoke(
        Command(resume={"decisions": hitl}), _config(thread_id, callbacks), context=ctx, version="v2",
    )
    if not reason:
        result = _ensure_submissions(agent, result, thread_id, as_of, callbacks)
    if reason and result.interrupts == () and (load_plan(as_of) or {}).get("version") == version_before:
        # 코드 보완: 거절 메시지만으로 재계획하지 않은 경우 같은 스레드에서 명시적으로 재계획을 요청한다
        result = agent.invoke(
            {"messages": [{"role": "user", "content": REPLAN_AGAIN_PREFIX + reason}]},
            _config(thread_id, callbacks), context=ctx, version="v2",
        )
    if reason:  # 그래도 재계획되지 않아 아직 PENDING이면 거절로 정리
        drafts = load_drafts(as_of)
        for draft_id in rejected:
            if drafts.get(draft_id, {}).get("status") == "PENDING":
                set_draft_status(as_of, draft_id, "REJECTED")
    return _pack(result, as_of)


def _is_auto(message: HumanMessage) -> bool:
    """코드가 보낸 메시지·요약 메시지 (사용자가 채팅창에 쓴 질문이 아님)."""
    if message.additional_kwargs.get("lc_source") == "summarization":
        return True
    text = message.text.strip()
    return text == DAILY_MESSAGE or text.startswith(AUTO_PREFIXES)


def _all_messages(thread_id: str) -> list:
    """요약 미들웨어가 오래된 메시지를 지워도 화면 기록은 남도록, 체크포인트 이력 전체에서 메시지를 id 기준으로 모은다."""
    agent = build_supervisor()
    seen, out = set(), []
    for snap in reversed(list(agent.get_state_history(_config(thread_id)))):  # 오래된 체크포인트부터
        for m in snap.values.get("messages", []):
            key = m.id or id(m)
            if key not in seen:
                seen.add(key)
                out.append(m)
    return out


def chat_history(thread_id: str, as_of: str) -> list[dict]:
    """채팅 화면용 기록 [{"role": "user"|"assistant", "content"}] (사용자 질문과 그 질문에 대한 마지막 답변).

    답변 없이 승인 대기로 멈춘 마지막 질문은 run()과 같은 승인 대기 안내문을 답변으로 붙인다.
    """
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    turns: list[dict] = []
    current = None  # 지금 답변을 모으는 사용자 질문 (코드 메시지 뒤의 답변은 버린다)
    for m in _all_messages(thread_id):
        if isinstance(m, HumanMessage):
            current = None if _is_auto(m) else {"question": m.text, "answer": None}
            if current:
                turns.append(current)
        elif current and isinstance(m, AIMessage) and not m.tool_calls and m.text.strip():
            current["answer"] = m.text
    if turns and turns[-1]["answer"] is None and (requests := pending_requests(thread_id)):
        turns[-1]["answer"] = pending_answer(requests, as_of)
    out = []
    for t in turns:
        out.append({"role": "user", "content": t["question"]})
        if t["answer"] is not None:
            out.append({"role": "assistant", "content": t["answer"]})
    return out
