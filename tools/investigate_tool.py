"""고위험 설비 여러 대를 동시에 조사하는 도구 (Supervisor가 쓸 예정). 내부에서 agents.fanout.run_fanout을 호출한다."""

import sys
from pathlib import Path

from langchain.tools import ToolRuntime, tool

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.fanout import run_fanout  # noqa: E402
from tools.context import Ctx  # noqa: E402

MAX_MACHINES = 10


def format_results(results: list[dict]) -> str:
    """설비당 한 줄: "#45 bearing | 예측 고장 | 진동 24h 평균 +25%, vibration 에러 1건(7일) | evidence=E1001-045"
    / 오류는 "#45 | 조사 실패 | 사유". 근거 칸은 fanout.brief()가 DB에서 계산한 수치다."""
    lines = []
    for r in results:
        if r.get("status") != "ok":
            lines.append(f"#{r['machine_id']} | 조사 실패 | {r.get('error', '알 수 없는 오류')}")
            continue
        lines.append(f"#{r['machine_id']} {r['comp']} | {r['label']} | {r.get('brief', '-')} | evidence={r['evidence_id']}")
    return "\n".join(lines)


def validate_ids(machine_ids) -> tuple[list[int], str | None]:
    """중복 제거(순서 유지) 후 검증. (정리된 목록, 오류 문자열 또는 None)"""
    if not isinstance(machine_ids, list) or any(isinstance(m, bool) or not isinstance(m, int) for m in machine_ids):
        return [], f"입력 오류: machine_ids는 정수 목록이어야 합니다 (받은 값: {machine_ids!r})"
    ids = list(dict.fromkeys(machine_ids))
    if bad := [m for m in ids if not 1 <= m <= 100]:
        return [], f"입력 오류: 설비 번호는 1~100 사이여야 합니다 (범위 밖: {bad})"
    if len(ids) > MAX_MACHINES:
        return [], f"입력 오류: 한 번에 최대 {MAX_MACHINES}대까지 조사할 수 있습니다 (받은 수: {len(ids)}대)"
    return ids, None


@tool
def investigate_machines(machine_ids: list[int], runtime: ToolRuntime[Ctx]) -> str:
    """고위험으로 예측된 설비들의 원인을 동시에 조사할 때 사용. predict_failure 결과의 고위험 설비 번호를 그대로 넣는다.
    통계 질문에는 사용하지 않는다.

    설비마다 조사 에이전트가 센서 추세·에러·정비 이력·매뉴얼을 확인하고, 설비당 한 줄
    "#번호 부품 | 판정 | 핵심 근거 | evidence=증거ID"로 돌려준다. 한 번에 최대 10대.

    Args:
        machine_ids: 조사할 설비 번호 목록 (1~100, 최대 10대)
    """
    ids, err = validate_ids(machine_ids)
    if err:
        return err
    if not ids:
        return "조사 대상 없음 (고위험 설비 0대)"
    return format_results(run_fanout(ids, runtime.context.as_of))
