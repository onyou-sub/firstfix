"""설비별 과거 조사 기억 도구. memory.store의 장기 기억에서 기준 시각 이전 기록만 읽는다.

- search_field_notes: 작업자 완료 보고 색인(store.field_notes, Chroma)에서 기준 시각 이전 현장 기록 검색
"""

import sys
from pathlib import Path

from langchain.tools import ToolRuntime, tool

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from memory.store import feedback_machines, recall  # noqa: E402
from store import field_notes  # noqa: E402
from tools.context import Ctx  # noqa: E402

NO_MEMORY = "과거 조사 기록 없음"


def memory_text(machine_id: int, as_of: str, include_same_time: bool = False) -> str:
    if isinstance(machine_id, bool) or not isinstance(machine_id, int) or not 1 <= machine_id <= 100:
        return f"입력 오류: machine_id는 1~100 사이 정수여야 합니다 (받은 값: {machine_id!r})"
    records = recall(machine_id, as_of, include_same_time=include_same_time)
    if not records:
        return NO_MEMORY
    lines = []
    for r in records:
        head = f"{r['as_of'][:10]}: " + (f"{r['comp']} {r['label']}" if r.get("comp") else "조사 기록 없음(피드백만)")
        if r.get("evidence_id"):
            head += f" ({r['evidence_id']})"
        feedback = "; ".join(f"{f['text']} ({f['engineer_id']})" for f in r.get("feedback", []))
        lines.append(head + (f" / 피드백: {feedback}" if feedback else " / 피드백 없음"))
    return "\n".join(lines)


@tool
def recall_machine_memory(machine_id: int, runtime: ToolRuntime[Ctx]) -> str:
    """이 설비를 예전에 조사한 기록(이전 판정, 근거 ID)과 엔지니어 피드백을 최신순 최대 3건 돌려준다.

    언제 쓰나: 조사를 시작할 때 가장 먼저. 엔지니어 피드백(실제 원인, 현장 확인 결과)이 있으면 결론에 반영한다.
    언제 쓰지 않나: 센서·에러·교체 이력 같은 설비 데이터가 필요할 때 (데이터 조회 도구 사용).
    기준 시각 이후의 기록은 보이지 않는다.

    Args:
        machine_id: 설비 번호 1~100
    """
    return memory_text(machine_id, runtime.context.as_of)


@tool
def get_engineer_feedback(runtime: ToolRuntime[Ctx], machine_id: int | None = None) -> str:
    """엔지니어가 남긴 현장 피드백(실제 원인, 점검 결과)과 그 설비의 조사 기록을 돌려준다. 오늘 저장한 피드백도 포함한다.

    언제 쓰나: "피드백", "현장 확인 결과", "엔지니어 메모"를 물을 때, 또는 특정 설비가 왜 위험한지·어떻게 할지 답하기 전에
    그 설비에 엔지니어 피드백이 있는지 확인할 때. machine_id를 비우면 피드백이 있는 설비 전체를 보여준다.
    언제 쓰지 않나: 통계 질문(ask_data_agent), 새 조사가 필요할 때(investigate_machines).

    Args:
        machine_id: 설비 번호 1~100 (비우면 피드백이 있는 모든 설비)
    """
    as_of = runtime.context.as_of
    if machine_id is not None:
        text = memory_text(machine_id, as_of, include_same_time=True)
        return text if text.startswith("입력 오류") else f"[#{machine_id} 조사 기록·엔지니어 피드백]\n{text}"
    ids = feedback_machines(as_of)
    if not ids:
        return "엔지니어 피드백 없음"
    return "\n".join(f"[#{m}]\n{memory_text(m, as_of, include_same_time=True)}" for m in ids)


@tool
def search_field_notes(query: str, runtime: ToolRuntime[Ctx], machine_id: int | None = None) -> str:
    """작업자 완료 보고(현장 기록)에서 측정값·관찰·조치 결과를 검색한다. 기준 시각 이전 기록만 보인다.

    언제 쓰나: recall_machine_memory에 작업자 보고가 있어 측정값·상세 메모가 필요할 때, 또는 같은 부품의
    다른 설비에서 현장 작업자가 실제로 무엇을 발견했는지(소리, 열, 측정값, 원인 판단 근거) 확인할 때.
    언제 쓰지 않나: 센서·에러·교체 이력 같은 설비 데이터(데이터 조회 도구), 매뉴얼 점검 절차(search_manual).

    Args:
        query: 찾을 내용 (예: "베어링 하우징 온도 진동", "압력 센서 고정 볼트")
        machine_id: 이 설비 기록만 볼 때 설비 번호 1~100, 다른 설비 사례까지 볼 때는 비움
    """
    if not isinstance(query, str) or not query.strip():
        return "입력 오류: query는 비어 있지 않은 문자열이어야 합니다"
    if machine_id is not None and (isinstance(machine_id, bool) or not isinstance(machine_id, int)
                                   or not 1 <= machine_id <= 100):
        return f"입력 오류: machine_id는 1~100 사이 정수여야 합니다 (받은 값: {machine_id!r})"
    try:
        return field_notes.search_text(query.strip(), runtime.context.as_of, machine_id=machine_id)
    except Exception as e:  # 색인이 비었거나 임베딩 API 오류
        return f"현장 기록 검색 실패: {type(e).__name__}"
