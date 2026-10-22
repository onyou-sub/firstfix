"""여러 설비를 조사 에이전트로 동시에 조사하는 LangGraph 그래프 (Send 기반 fan-out).

START --route_targets--> investigate × N (병렬, Send) --> END
                    \\--> END (조사 대상 0대)

- run_fanout(machine_ids, as_of) -> list[dict]: predict_df로 예측 부품·확률을 찾아 동시에 조사, machine_id 순 결과
- 설비 하나가 실패해도 나머지 조사는 계속된다 (status="error")
"""

import operator
import sys
from functools import cache
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

AGENTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = AGENTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.investigator import _recent_replacement, build_investigator, investigate_one  # noqa: E402
from ml.predict import predict_df  # noqa: E402
from tools.data_tools import COMP_SENSOR, SENSOR_MEAN, _error_rows, _sensor_rows  # noqa: E402

MAX_CONCURRENCY = 5
SENSOR_SHORT = {"volt": "전압", "rotate": "회전수", "pressure": "압력", "vibration": "진동"}
COMP_ERRORS = {"motor": ["overvoltage"], "drive": ["low_rpm", "torque"], "pump": ["pressure"], "bearing": ["vibration"]}
ERROR_DAYS = 7


class FanoutState(TypedDict):
    as_of: str
    targets: list[dict]  # [{"machine_id", "comp", "prob"}, ...]
    results: Annotated[list[dict], operator.add]  # 병렬 결과를 합치는 리듀서


class InvestigateInput(TypedDict):
    """Send로 investigate 노드 하나에 전달되는 입력."""
    as_of: str
    target: dict


def brief(machine_id: int, comp: str, as_of: str) -> str:
    """한 줄 근거를 DB에서 직접 계산 (LLM 문장을 자르지 않고 수치만): 짝 센서 24h 평균 %, 관련 에러 수, 최근 교체."""
    sensor = COMP_SENSOR[comp]
    values = _sensor_rows(machine_id, sensor, 24, as_of)["value"]
    parts = [f"{SENSOR_SHORT[sensor]} 24h 평균 {(values.mean() / SENSOR_MEAN[sensor] - 1) * 100:+.0f}%"
             if len(values) else f"{SENSOR_SHORT[sensor]} 기록 없음"]
    errors = _error_rows(machine_id, ERROR_DAYS, as_of)["error_id"].value_counts()
    parts.append(", ".join(f"{e} 에러 {int(errors.get(e, 0))}건" for e in COMP_ERRORS[comp]) + f"({ERROR_DAYS}일)")
    rule = _recent_replacement(machine_id, comp, as_of)
    if rule["recent"]:
        parts.append(f"{comp} 교체 {rule['days_since']:.1f}일 전")
    return ", ".join(parts)


def route_targets(state: FanoutState):
    if not state["targets"]:
        return END
    return [Send("investigate", {"as_of": state["as_of"], "target": t}) for t in state["targets"]]


def investigate(state: InvestigateInput) -> dict:
    t = state["target"]
    base = {"machine_id": t["machine_id"], "comp": t.get("comp")}
    try:
        finding, evidence_id = investigate_one(t["machine_id"], t["comp"], t["prob"], state["as_of"])
    except Exception as e:  # 한 설비 실패가 전체를 멈추지 않도록
        return {"results": [{**base, "status": "error", "error": f"{type(e).__name__}: {e}"[:300]}]}
    return {"results": [{
        **base,
        "label": finding.label,
        "summary": finding.summary,
        "key_evidence": finding.key_evidence,
        "brief": brief(t["machine_id"], t["comp"], state["as_of"]),  # 도구 한 줄 요약용
        "evidence_id": evidence_id,
        "status": "ok",
    }]}


@cache
def build_fanout_graph():
    g = StateGraph(FanoutState)
    g.add_node("investigate", investigate, input_schema=InvestigateInput)
    g.add_conditional_edges(START, route_targets, ["investigate", END])
    g.add_edge("investigate", END)
    return g.compile()


def make_targets(machine_ids: list[int], as_of: str) -> list[dict]:
    """predict_df에서 각 설비의 예측 부품(top_comp)과 확률(top_prob)을 찾는다."""
    if not machine_ids:
        return []
    pred = predict_df(as_of).set_index("machine_id")
    return [
        {"machine_id": int(m), "comp": pred.at[m, "top_comp"], "prob": float(pred.at[m, "top_prob"])}
        if m in pred.index else {"machine_id": int(m), "comp": None, "prob": None}  # 없는 설비 -> 조사에서 오류 처리
        for m in machine_ids
    ]


def _open_vector_stores() -> None:
    """매뉴얼·현장 기록 Chroma를 미리 연다 (캐시됨). 여러 스레드가 처음에 동시에 열면 Chroma 클라이언트 생성이 충돌해
    "Could not connect to tenant default_tenant", "'RustBindingsAPI' object has no attribute 'bindings'" 오류가 난다."""
    from build_index import get_store as manual_store  # data/virtual은 tools.data_tools가 sys.path에 넣는다

    from store import field_notes

    manual_store()
    field_notes.get_store()


def run_fanout(machine_ids: list[int], as_of: str) -> list[dict]:
    """설비들을 동시에 조사해 machine_id 순으로 결과를 돌려준다. 빈 목록이면 조사 없이 []."""
    targets = make_targets(machine_ids, as_of)
    if targets:
        build_investigator()  # 스레드들이 동시에 처음 만들지 않도록 미리 생성 (캐시됨)
        _open_vector_stores()
    out = build_fanout_graph().invoke(
        {"as_of": as_of, "targets": targets, "results": []},
        config={"max_concurrency": MAX_CONCURRENCY},
    )
    return sorted(out.get("results", []), key=lambda r: r["machine_id"])
