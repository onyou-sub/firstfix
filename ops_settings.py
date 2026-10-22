"""운영 설정 (화면 설정 페이지에서 수정, config/settings.json에 저장).

- 근무 시간, 점심시간, 작업 간 준비 시간(분), 하루 재계획 한도, 긴급 점검 시간(분), 근무 시간 외 긴급 처리
- 계획 코드(tools/plan_tools.schedule_tasks, agents/planner.replan)가 호출할 때마다 읽는다.
- 파일이 없거나 값이 잘못되면 config.py 기본값을 쓴다.
"""

import json
import re
from pathlib import Path

from config import LUNCH, MAX_REPLAN, PREP_MIN

ROOT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT_DIR / "config" / "settings.json"
DEFAULTS = {
    "work_start": "09:00", "work_end": "18:00",
    "lunch_start": LUNCH[0], "lunch_end": LUNCH[1],
    "prep_min": PREP_MIN, "max_replan": MAX_REPLAN,
    "urgent_check_min": 60,  # 긴급 점검 시간(분). 긴급 점검은 항상 자동 배정 (켜기/끄기 없음)
    # 근무 시간 밖에 경고가 나면: "now" 지금 바로 출동(근무 시간 외 출동 표시) / "next_shift" 다음 근무 시작에
    "urgent_off_hours": "now",
}
URGENT_OFF_HOURS = {"now": "지금 바로 출동 (근무 시간 외 출동)", "next_shift": "다음 근무 시작에 출동"}
_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def validate(values: dict) -> list[str]:
    """잘못된 점을 한국어 문장 목록으로 (비어 있으면 통과)."""
    errors = []
    times = ["work_start", "work_end", "lunch_start", "lunch_end"]
    for k in times:
        if not _HM.match(str(values.get(k, ""))):
            errors.append(f"시각 형식이 올바르지 않습니다: {values.get(k)!r} (HH:MM)")
    if errors:
        return errors
    ws, we, ls, le = (_minutes(values[k]) for k in times)
    if we <= ws:
        errors.append("근무 종료 시각은 시작 시각보다 늦어야 합니다.")
    if le <= ls:
        errors.append("점심 종료 시각은 점심 시작 시각보다 늦어야 합니다.")
    if ls < ws or le > we:
        errors.append("점심시간은 근무 시간 안에 있어야 합니다.")
    prep, limit = values.get("prep_min"), values.get("max_replan")
    if not isinstance(prep, int) or not 0 <= prep <= 120:
        errors.append("작업 간 준비 시간은 0~120분이어야 합니다.")
    if not isinstance(limit, int) or not 0 <= limit <= 10:
        errors.append("하루 재계획 한도는 0~10회여야 합니다.")
    urgent = values.get("urgent_check_min")
    if isinstance(urgent, bool) or not isinstance(urgent, int) or not 10 <= urgent <= 240:
        errors.append("긴급 점검 시간은 10~240분이어야 합니다.")
    if values.get("urgent_off_hours") not in URGENT_OFF_HOURS:
        errors.append(f"근무 시간 외 긴급 처리는 {', '.join(URGENT_OFF_HOURS)} 중 하나여야 합니다.")
    return errors


def load_settings() -> dict:
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULTS)
    values = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    return values if not validate(values) else dict(DEFAULTS)


def save_settings(values: dict) -> list[str]:
    """검증을 통과하면 저장하고 [], 아니면 저장하지 않고 오류 목록."""
    values = {k: values[k] for k in DEFAULTS if k in values}
    values = {**DEFAULTS, **values}
    errors = validate(values)
    if not errors:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    return errors
