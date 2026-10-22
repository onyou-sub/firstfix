"""데이터 에이전트: 자연어 질문을 SQL로 바꿔 pdm.db와 운영 기록(app.db의 ops_ 뷰)을 조회하고 답한다
(기준 시각 이후 데이터는 보이지 않음).

- build_data_agent(): create_agent (gpt-5.4-mini, SQL 도구 3개, Ctx 주입, 도구 호출 실행당 6회)
- ask(question, as_of) -> {"answer", "tool_calls"}
"""

import sys
from functools import cache
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI

AGENTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = AGENTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from tools.context import Ctx  # noqa: E402
from tools.sql_tools import SQL_TOOLS  # noqa: E402

MODEL_NAME = "gpt-5.4-mini"
TOOL_CALL_LIMIT = 6  # 실행(invoke) 1회당

SYSTEM_PROMPT = f"""당신은 설비 정비 데이터 담당자입니다. SQLite 데이터베이스를 조회해 질문에 답합니다.

[조회 순서] 도구 호출은 모두 합쳐 최대 {TOOL_CALL_LIMIT}회입니다.
1. sql_db_list_tables로 테이블 목록 확인
2. sql_db_schema로 필요한 테이블의 컬럼과 값 형식 확인
3. run_sql_guarded로 쿼리 실행. 집계(COUNT, AVG, 비율)는 SQL 안에서 끝냅니다.
   "쿼리 오류"가 오면 쿼리를 고쳐 다시 실행합니다. "차단됨"이 오면 같은 시도를 반복하지 않습니다.

[용어집]
- 고장 = failures 테이블
- 부품 교체 = maint 테이블. 사후 정비 = maint.by_failure = 1, 예방 정비 = maint.by_failure = 0
- 부품(comp): motor(모터/전원부), drive(구동부), pump(밸브/펌프), bearing(베어링)
- 에러(error_id): overvoltage(과전압), low_rpm(회전수 저하), torque(토크 이상), pressure(압력 이상), vibration(진동 경고)
- 설비 중요도: criticality 테이블의 criticality 컬럼 (A/B/C). 설비 모델: machines.model. 연식: machines.age
- datetime은 'YYYY-MM-DD HH:MM:SS' 문자열입니다.
- 운영 기록(ops_ 뷰): 작업 지시 = ops_work_orders, 완료 보고 = ops_work_reports, 발주 = ops_purchase_orders,
  작업자 보고 교체 = ops_maint_log, 재고 변동 = ops_stock_ledger
- 긴급 작업(긴급 점검·긴급 출동) = ops_work_orders.urgent = 1. task_type으로 판단하지 않습니다
  (긴급 작업의 task_type은 urgent_check 또는 sensor_check이고, temporary_check는 긴급이 아닙니다).
  "긴급을 했다/처리했다"는 status = 'DONE', "긴급이 남았다"는 status != 'DONE'. 상태를 말하지 않으면 전부 세고 상태별로 나눠 답합니다.
- 원본 설비 기록(maint, failures)과 운영 기록(ops_*)은 다른 것입니다.
  질문이 "작업자", "작업 지시", "완료", "발주"를 말하면 ops_* 를 씁니다.
- 예측 적중률 = ops_work_reports에서 actual_cause_type = '예측과 같음'인 비율 (is_virtual = 0만).
- 완료 보고 통계는 기본적으로 is_virtual = 0만 셉니다. 사용자가 가상 메모 포함을 말하면 포함합니다.
  ops_work_reports를 썼으면 근거 줄 바로 위에 "가상 메모 제외" 또는 "가상 메모 포함" 한 줄을 그대로 적습니다.

[기준 시각]
- 현재 시각(as_of)은 사용자 메시지에 함께 전달됩니다. "지금까지", "지난달", "최근 N일", "10월"(연도가 없으면 as_of의 연도)은
  모두 as_of 기준으로 계산합니다. 최근 N일 = datetime > (as_of - N일) AND datetime <= as_of.
- 데이터베이스에는 as_of 이후 행이 보이지 않습니다. as_of 이후 기간을 물으면 조회 결과대로 0건이라고 답하고,
  기준 시각 이후라 아직 데이터가 없다고 설명합니다.
- 운영 기록(ops_*)의 "오늘"은 기준 날짜(as_of_date = as_of의 날짜), "어제"는 그 전날,
  "이번 주"는 기준 날짜가 속한 주의 월요일~일요일입니다. ops_ 뷰에는 기준 날짜 이후 행이 보이지 않습니다.

[답변 규칙]
- 조회 결과에 없는 수치는 만들지 않습니다. 결과가 0건이면 0건이라고 답합니다.
- "어떤 작업", "처리한 작업은?"처럼 작업을 묻는 질문은 건수만 말하지 말고 작업 목록(wo_id, 설비 #번호, 부품, 상태)으로 답합니다.
- 데이터 변경·삭제 요청은 수행하지 않습니다. 조회 전용이라고 안내합니다.
- 한국어로 짧게 답하고, 답변 마지막 줄에 "근거: DB 조회 (사용 테이블: 테이블1, 테이블2)"를 붙입니다.
"""


@cache
def build_data_agent():
    load_dotenv(ROOT_DIR / ".env")
    return create_agent(
        model=ChatOpenAI(model=MODEL_NAME, temperature=0),
        tools=SQL_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        middleware=[ToolCallLimitMiddleware(run_limit=TOOL_CALL_LIMIT, exit_behavior="continue")],
        context_schema=Ctx,
    )


def _tool_calls(messages: list) -> list[dict]:
    """AI 메시지의 도구 호출과 그 결과(ToolMessage)를 순서대로 짝짓는다."""
    outputs = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    calls = []
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                out = outputs.get(tc["id"])
                calls.append({"name": tc["name"], "input": tc["args"], "output": out.content if out else None})
    return calls


def ask(question: str, as_of: str) -> dict:
    """질문 1개에 답한다. {"answer": 최종 답변, "tool_calls": [{name, input, output}, ...]}"""
    as_of = pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")
    result = build_data_agent().invoke(
        {"messages": [{"role": "user", "content": f"현재 시각(as_of): {as_of}\n질문: {question}"}]},
        context=Ctx(as_of=as_of),
    )
    final = next((m for m in reversed(result["messages"]) if isinstance(m, AIMessage) and not m.tool_calls), None)
    return {"answer": final.text if final else "", "tool_calls": _tool_calls(result["messages"])}
