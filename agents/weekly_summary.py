"""주간 보고서 맨 위 요약 문장 (3~4문장, "~했습니다"). 숫자는 summary_facts에 있는 것만 쓴다.

- summarize(facts): LLM 초안. 답에 나온 숫자가 하나라도 facts에 없으면(숫자를 바꾸거나 만들었으면) 템플릿 문장.
- LLM 오류·크레딧 소진도 템플릿. draft_summary는 {"text", "auto"(템플릿이면 True)}를 돌려준다 (화면에 "자동 문장").
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
MODEL_NAME = "gpt-5.4-mini"
NUMBER = re.compile(r"\d+(?:\.\d+)?")

PROMPT = """당신은 설비 정비 팀의 주간 보고서 요약을 쓰는 도우미입니다.
주어진 JSON 사실만 보고 관리자에게 올릴 요약을 3~4문장으로 씁니다.

[반드시 지킬 것]
- 주어진 숫자와 사실만 씁니다. 숫자를 바꾸거나, 더하거나 빼서 새 숫자를 만들거나, 반올림하지 않습니다.
- 값이 null인 항목은 언급하지 않습니다.
- 원인이나 이유를 추측하지 않습니다. 평가나 칭찬, 다짐도 쓰지 않습니다.
- 보고서 말투("~했습니다", "~입니다")로, 한 문단으로 씁니다. 제목·목록·이모지는 쓰지 않습니다."""


def _numbers(value) -> set[str]:
    """facts 안의 모든 숫자 (문자열 속 숫자 포함: "10/12~10/18" -> 10, 12, 18)."""
    if value is None or isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        return {str(value)}
    if isinstance(value, str):
        return set(NUMBER.findall(value))
    if isinstance(value, dict):
        return set().union(*(_numbers(v) | _numbers(k) for k, v in value.items())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_numbers(v) for v in value)) if value else set()
    return set()


def numbers_ok(text: str, facts: dict) -> bool:
    """답에 나온 숫자가 모두 facts 안에 있나."""
    allowed = _numbers(facts)
    return all(n in allowed for n in NUMBER.findall(text))


def template_summary(f: dict) -> str:
    """LLM 없이 만드는 요약 (숫자는 facts 그대로)."""
    team = f.get("팀") or "우리 팀"
    if not f.get("배정"):
        return f"이번 주({f.get('기간')}) {team}에 지시된 작업은 없었습니다."
    out = [f"이번 주 {team}은 작업 {f['배정']}건 중 {f['완료']}건({f['완료율(%)']}%)을 완료했습니다."]
    if f.get("긴급 출동"):
        ack = f.get("평균 출동 확인(분)")
        out.append(f"긴급 출동은 {f['긴급 출동']}건이며" + (f" 평균 {ack}분 안에 출동을 확인했습니다."
                                                         if ack is not None else " 아직 출동 확인 기록이 없습니다."))
    else:
        out.append("긴급 출동은 없었습니다.")
    if f.get("완료 보고"):
        hit = f.get("예측 적중률(%)")
        out.append(f"총 작업 시간은 {f['총 작업 시간']}이고, 완료 보고 {f['완료 보고']}건 중 {f['예측과 같음']}건"
                   f"({hit}%)이 예측과 같았습니다.")
    out.append(f"다음 주로 넘어가는 작업은 {f['이월']}건입니다." if f.get("이월") else "이월된 작업은 없습니다.")
    return " ".join(out)


def _llm(facts: dict) -> str:
    from dotenv import load_dotenv
    from langchain_openai import ChatOpenAI

    load_dotenv(ROOT_DIR / ".env")
    out = ChatOpenAI(model=MODEL_NAME, temperature=0).invoke(
        [("system", PROMPT), ("user", json.dumps(facts, ensure_ascii=False))])
    return out.text.strip()


def draft_summary(facts: dict, call=None) -> dict:
    """{"text", "auto": 템플릿 문장이면 True, "reason"}. call: 테스트용 가짜 LLM (facts -> 문자열)."""
    try:
        text = (call or _llm)(facts)
    except Exception as e:  # 오류·크레딧 소진
        return {"text": template_summary(facts), "auto": True, "reason": f"{type(e).__name__}: {e}"[:200]}
    if not text or not numbers_ok(text, facts):
        return {"text": template_summary(facts), "auto": True, "reason": "사실에 없는 숫자가 있어 자동 문장으로 대체"}
    return {"text": text, "auto": False, "reason": None}


def summarize(facts: dict, call=None) -> str:
    return draft_summary(facts, call)["text"]
