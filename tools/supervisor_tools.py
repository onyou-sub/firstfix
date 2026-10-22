"""Supervisor 전용 도구: 고장 예측, 데이터 질문, 초안 등록(승인).

- predict_failure: predict_df(as_of) -> 고위험 설비 목록 (100대 결과는 outputs/predictions/{YYYYMMDD}.json)
- ask_data_agent: 데이터 에이전트 답변
- submit_work_order / submit_purchase_request: 초안을 APPROVED로 바꾸고 outputs/registered/{YYYYMMDD}.json에 기록
  (HumanInTheLoopMiddleware가 사람 승인 후에만 실행되게 한다. 이미 등록된 초안은 다시 기록하지 않음)
"""

import json
import sys
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
from langchain.tools import ToolRuntime, tool

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.data_agent import ask  # noqa: E402
from ml.predict import predict_df  # noqa: E402
from tools.context import Ctx  # noqa: E402
from store import app_db  # noqa: E402
from tools.plan_tools import load_drafts, set_draft_status  # noqa: E402

PREDICTIONS_DIR = ROOT_DIR / "outputs" / "predictions"
REGISTERED_DIR = ROOT_DIR / "outputs" / "registered"
_registered_lock = threading.Lock()


def _day(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y%m%d")


def registered_path(as_of: str) -> Path:
    return REGISTERED_DIR / f"{_day(as_of)}.json"


def load_registered(as_of: str) -> list[dict]:
    path = registered_path(as_of)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@tool
def predict_failure(runtime: ToolRuntime[Ctx]) -> str:
    """기준 시각의 설비 100대 고장 위험을 예측해 고위험 설비를 "#번호 부품 확률" 형식으로 한 줄씩 돌려준다.

    언제 쓰나: 오늘 점검을 시작할 때 가장 먼저. 결과의 고위험 설비 번호를 그대로 investigate_machines에 넣는다.
    언제 쓰지 않나: 통계·건수 질문(ask_data_agent), 이미 예측한 같은 대화 안의 재질문.
    """
    as_of = runtime.context.as_of
    df = predict_df(as_of)
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    (PREDICTIONS_DIR / f"{_day(as_of)}.json").write_text(json.dumps(
        {"as_of": as_of, "rows": df.to_dict("records")}, ensure_ascii=False, indent=2, default=str,
    ), encoding="utf-8")

    high = df[df["high_risk"]].sort_values("top_prob", ascending=False)
    if high.empty:
        return f"고위험 설비 0대 (기준 {as_of}, 100대 예측)"
    lines = [f"#{int(r.machine_id)} {r.top_comp} {r.top_prob:.4f}" for r in high.itertuples()]
    return f"고위험 설비 {len(lines)}대 (기준 {as_of}, 100대 예측)\n" + "\n".join(lines)


@tool
def ask_data_agent(question: str, runtime: ToolRuntime[Ctx]) -> str:
    """설비 데이터베이스에 대한 통계·건수·비율·목록 질문에 답한다 (기준 시각 이전 데이터만).

    언제 쓰나: "10월 베어링 고장 몇 건?", "사후 정비 비율은?", "A등급 설비 중 최근 고장 난 설비" 같은 질문.
    언제 쓰지 않나: 오늘 고위험 설비 예측(predict_failure), 특정 설비 원인 조사(investigate_machines).

    Args:
        question: 사용자의 질문 그대로
    """
    return ask(question, runtime.context.as_of)["answer"]


def _submit(draft_id: str, kind: str, prefix: str, as_of: str, engineer_id: str) -> str:
    draft_id = str(draft_id).strip()
    if not draft_id.startswith(prefix):
        return f"입력 오류: {kind} draft_id는 {prefix}로 시작해야 합니다 (받은 값: {draft_id!r})"
    draft = load_drafts(as_of).get(draft_id)
    if draft is None:
        return f"입력 오류: 초안 {draft_id}를 찾을 수 없습니다"
    if draft["status"] == "SUPERSEDED":
        return f"차단됨: {draft_id}는 새 배정안으로 대체된 초안이라 등록할 수 없습니다 ({draft.get('superseded_by', '')})"

    with _registered_lock:
        registered = load_registered(as_of)
        if draft["status"] == "APPROVED" or any(r["draft_id"] == draft_id for r in registered):
            return f"이미 등록됨: {draft_id} (중복 등록하지 않음)"
        set_draft_status(as_of, draft_id, "APPROVED")
        registered.append({
            "draft_id": draft_id, "type": draft["type"], "registered_at": datetime.now().isoformat(timespec="seconds"),
            "engineer_id": engineer_id, "as_of": as_of, "evidence_ids": draft.get("evidence_ids", []),
        })
        REGISTERED_DIR.mkdir(parents=True, exist_ok=True)
        registered_path(as_of).write_text(json.dumps(registered, ensure_ascii=False, indent=2), encoding="utf-8")
    # 운영 DB: 작업 지시(ASSIGNED) + 팀 알림 / 발주(REQUESTED) + 관리자 알림 (같은 draft_id는 한 번만)
    if draft["type"] == "work_order":
        app_db.create_work_order(draft, engineer_id)
    else:
        app_db.create_purchase_order(draft, engineer_id)
    return f"등록 완료: {draft_id} ({kind}, 승인자 {engineer_id})"


@tool
def submit_work_order(draft_id: str, runtime: ToolRuntime[Ctx]) -> str:
    """작업 지시서 초안(WO-...)을 등록한다. 실행 전 사람의 승인을 받는다.

    언제 쓰나: ask_planner_agent가 만든 작업 지시서 draft_id마다 한 번.
    언제 쓰지 않나: 발주 요청서(PR-...)는 submit_purchase_request.

    Args:
        draft_id: 작업 지시서 초안 ID (예: WO-1001-084)
    """
    ctx = runtime.context
    return _submit(draft_id, "작업 지시서", "WO-", ctx.as_of, ctx.engineer_id)


@tool
def submit_purchase_request(draft_id: str, runtime: ToolRuntime[Ctx]) -> str:
    """발주 요청서 초안(PR-...)을 등록한다. 실행 전 사람의 승인을 받는다.

    언제 쓰나: ask_planner_agent가 만든 발주 요청서 draft_id마다 한 번.
    언제 쓰지 않나: 작업 지시서(WO-...)는 submit_work_order.

    Args:
        draft_id: 발주 요청서 초안 ID (예: PR-1001-pump)
    """
    ctx = runtime.context
    return _submit(draft_id, "발주 요청서", "PR-", ctx.as_of, ctx.engineer_id)
