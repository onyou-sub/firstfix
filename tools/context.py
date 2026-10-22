"""에이전트 실행 컨텍스트. 도구는 기준 시각(as_of)을 LLM 입력이 아니라 여기서 꺼낸다."""

from dataclasses import dataclass


@dataclass
class Ctx:
    as_of: str  # 'YYYY-MM-DD HH:MM:SS'. 이 시각 이후 데이터는 어떤 도구도 읽지 않는다.
    engineer_id: str = "eng01"
