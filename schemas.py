"""에이전트 입출력 스키마."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

Comp = Literal["motor", "drive", "pump", "bearing"]
FindingLabel = Literal["예측 고장", "센서·설치 이상 가능성"]

# Finding 길이 제한 (화면에서 "무엇을 해야 하는지"가 바로 보이도록). 프롬프트·검증·말줄임이 모두 이 값을 쓴다.
LIMITS = {
    "action_summary": 40, "anomaly_summary": 60,
    "hypotheses": 3, "hypothesis.cause": 15, "hypothesis.evidence": 50,
    "steps_min": 3, "steps": 6, "step.action": 25, "step.criterion": 30, "step.if_abnormal": 25,
    "summary_sentences": 2,
    "field_tips": 3, "field_tip": 40, "parts_tools": 5, "parts_tool": 20,
    "est_minutes_min": 10, "est_minutes_max": 480,
}
SAFETY_WORDS = ("정지", "잠금", "안전", "보호구", "LOTO")  # 안전 조치 단계 판별 (첫 비안전 단계를 고를 때)


def sentences(text: str) -> list[str]:
    """한국어·영어 문장 나누기 (. ! ? 뒤 공백 기준)."""
    return [s for s in re.split(r"(?<=[.!?。])\s+", text.strip()) if s]


# ---------------------------------------------------------------- LLM이 내는 형식 (길이는 설명으로만 안내)
class HypothesisDraft(BaseModel):
    cause: str = Field(description=f"원인 후보, {LIMITS['hypothesis.cause']}자 이내 (예: \"윤활 부족\")")
    evidence: str = Field(description=f"그 근거 수치 한 가지, {LIMITS['hypothesis.evidence']}자 이내")


class StepDraft(BaseModel):
    action: str = Field(description=f"할 일 하나, {LIMITS['step.action']}자 이내 (예: \"하우징 온도 측정\")")
    criterion: str = Field(description=f"정상 판단 기준, {LIMITS['step.criterion']}자 이내 (매뉴얼·도구 결과 수치만)")
    if_abnormal: str = Field(description=f"기준을 벗어나면 할 일, {LIMITS['step.if_abnormal']}자 이내")


class FindingDraft(BaseModel):
    """조사 에이전트의 구조화 출력. 길이 제한은 설명으로 안내하고, 지켰는지는 코드가 Finding으로 검사한다."""

    machine_id: int = Field(description="조사한 설비 번호")
    comp: Comp = Field(description="조사한 부품 (입력으로 받은 예측 부품)")
    label: FindingLabel = Field(
        description='판정. 예측 부품을 최근 7일 안에 교체했다면 "센서·설치 이상 가능성", 아니면 "예측 고장"'
    )
    action_summary: str = Field(
        description=f"무엇을 해야 하는지 한 문장, {LIMITS['action_summary']}자 이내. 점검 순서 중 가장 중요한 1~2단계"
    )
    anomaly_summary: str = Field(
        description=f"이상 요약 {LIMITS['anomaly_summary']}자 이내: 어디가, 언제부터, 얼마나"
    )
    hypotheses: list[HypothesisDraft] = Field(description=f"원인 후보 최대 {LIMITS['hypotheses']}개 (가능성 높은 순)")
    steps: list[StepDraft] = Field(
        description=f"점검 순서 {LIMITS['steps_min']}~{LIMITS['steps']}개. 첫 단계는 설비 정지·잠금 같은 안전 조치"
    )
    field_tips: list[str] = Field(description=f"현장 요령 최대 {LIMITS['field_tips']}개, 각 {LIMITS['field_tip']}자 이내")
    est_minutes: int = Field(
        description=f"예상 점검 시간(분), {LIMITS['est_minutes_min']}~{LIMITS['est_minutes_max']}"
    )
    parts_tools: list[str] = Field(
        description=f"필요한 부품·공구 최대 {LIMITS['parts_tools']}개, 각 {LIMITS['parts_tool']}자 이내"
    )
    summary: str = Field(description=f"결론 {LIMITS['summary_sentences']}문장 이내")
    key_evidence: list[str] = Field(description="근거 최대 4개. 각 항목에 도구 결과의 수치를 포함")
    checklist: list[str] = Field(description="현장 점검 항목 최대 4개")
    manual_source: str = Field(description='참고한 매뉴얼 출처 (예: "manual_motor.md §1.1 전압 상승 시 점검"), 없으면 ""')


# ---------------------------------------------------------------- 저장·화면에 쓰는 형식 (길이 제한 강제)
def _s(n: int):
    return Annotated[str, StringConstraints(max_length=n)]


class Hypothesis(BaseModel):
    cause: _s(LIMITS["hypothesis.cause"])
    evidence: _s(LIMITS["hypothesis.evidence"])


class Step(BaseModel):
    action: _s(LIMITS["step.action"])
    criterion: _s(LIMITS["step.criterion"])
    if_abnormal: _s(LIMITS["step.if_abnormal"])


class Finding(BaseModel):
    """조사 에이전트가 설비 1대를 조사한 결론 (길이 제한을 지킨 최종본)."""

    machine_id: int
    comp: Comp
    label: FindingLabel
    action_summary: str = Field(max_length=LIMITS["action_summary"])
    anomaly_summary: str = Field(max_length=LIMITS["anomaly_summary"])
    hypotheses: list[Hypothesis] = Field(max_length=LIMITS["hypotheses"])
    steps: list[Step] = Field(min_length=LIMITS["steps_min"], max_length=LIMITS["steps"])
    field_tips: list[_s(LIMITS["field_tip"])] = Field(max_length=LIMITS["field_tips"])
    est_minutes: int = Field(ge=LIMITS["est_minutes_min"], le=LIMITS["est_minutes_max"])
    parts_tools: list[_s(LIMITS["parts_tool"])] = Field(max_length=LIMITS["parts_tools"])
    summary: str
    key_evidence: list[str]
    checklist: list[str]
    manual_source: str

    @field_validator("summary")
    @classmethod
    def _summary_sentences(cls, v: str) -> str:
        if len(sentences(v)) > LIMITS["summary_sentences"]:
            raise ValueError(f"summary는 {LIMITS['summary_sentences']}문장 이내여야 합니다 ({len(sentences(v))}문장)")
        return v


# ---------------------------------------------------------------- 옛 evidence 파일 호환
def is_safety_step(text: str) -> bool:
    return any(w in text for w in SAFETY_WORDS)


def ellipsis(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def finding_compat(f: dict) -> dict:
    """새 필드가 없는 옛 Finding(dict)에 기본값을 채운다 (읽기 전용, 파일은 바꾸지 않음).

    action_summary가 없으면 steps의 첫 비안전 단계 action -> (steps도 없으면) checklist의 첫 비안전 항목
    -> summary 첫 문장 순으로 대체하고, 길면 말줄임.
    """
    out = {"anomaly_summary": "", "hypotheses": [], "steps": [], "field_tips": [], "est_minutes": None,
           "parts_tools": [], **f}
    if not out.get("action_summary"):
        candidates = [s.get("action", "") for s in out["steps"]] + list(out.get("checklist") or [])
        first = next((c for c in candidates if c and not is_safety_step(c)), None)
        first = first or next(iter(sentences(out.get("summary") or "")), "")
        out["action_summary"] = ellipsis(first, LIMITS["action_summary"])
    return out


class PlanConstraints(BaseModel):
    """재계획 조건: 거절 사유(자연어)를 계획 도구가 쓸 수 있는 조건으로 바꾼 것."""

    available_teams: list[str] = Field(
        description='오늘 배정 가능한 팀 이름 순서 (예: ["1팀", "3팀"]). 사유에 팀 언급이 없으면 ["1팀", "2팀", "3팀"]'
    )
    exclude_machines: list[int] = Field(description="오늘 정비에서 빼야 하는 설비 번호")
    defer_reason: dict[int, str] = Field(description="제외 설비별 사유 (설비 번호 -> 짧은 사유)")
    note: str = Field(description="해석한 내용을 한 문장으로. 해석 불가하거나 모순이면 그 사실을 그대로 적는다")
    team_available: dict[str, list[str]] | None = Field(
        default=None,
        description='팀별 오늘 가능 시간 ["HH:MM", "HH:MM"] (예: {"2팀": ["13:00", "18:00"]}). 하루 종일 불가인 팀은 '
                    "available_teams에서 빼고 여기에는 넣지 않는다. 시간 제약이 없으면 null",
    )
