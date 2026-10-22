"""하루 일정 배치(schedule_day) 확인 — LLM 없이 코드만.

- S1 (10-01): 모든 작업이 근무 시간 안, 점심과 겹치지 않음, 같은 팀 작업은 겹치지 않고 30분 이상 간격
- S2: 같은 입력 두 번 → 결과 완전히 같음 (결정적)
- S3: team_available {"2팀": ["13:00", "18:00"]} → 2팀 작업은 모두 13:00 이후 시작
- S4: 작업 10개짜리 가짜 입력 → 넘치는 작업은 "내일 우선"
- S5 (12-22): 13번 설비가 sensor_check 1시간 작업으로 일정에 들어감

실행: uv run python scripts/test_schedule.py
"""

import json
import sys
from pathlib import Path

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from config import LUNCH, PREP_MIN  # noqa: E402
from tools.plan_tools import (  # noqa: E402
    TOMORROW, _hm, load_findings, load_teams, schedule_day, schedule_tasks,
)

pd.set_option("display.width", 200)
pd.set_option("display.unicode.east_asian_width", True)
results: list[tuple[str, bool]] = []
COLS = ["machine_id", "comp", "task_type", "duration_min", "skill", "score", "team", "start", "end", "skill_match", "slot"]


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def header(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def show(result: dict) -> None:
    print(pd.DataFrame(result["rows"])[COLS].to_string(index=False))
    print(f"가동률: {result['utilization']} · 내일 우선: {result['tomorrow']} · 팀 가능 시간: {result['windows']}")


def violations(result: dict) -> list[str]:
    """근무 시간·점심·같은 팀 간격 규칙 위반 목록."""
    out = []
    lunch_s, lunch_e = _hm(LUNCH[0]), _hm(LUNCH[1])
    scheduled = [r for r in result["rows"] if r.get("team")]
    for r in scheduled:
        s, e = _hm(r["start"]), _hm(r["end"])
        ws, we = map(_hm, result["windows"][r["team"]])
        if s < ws or e > we:
            out.append(f"#{r['machine_id']} 근무 시간 밖 {r['start']}-{r['end']}")
        if s < lunch_e and e > lunch_s:
            out.append(f"#{r['machine_id']} 점심과 겹침 {r['start']}-{r['end']}")
        if e - s != r["duration_min"]:
            out.append(f"#{r['machine_id']} 작업 시간 불일치")
    by_team: dict[str, list] = {}
    for r in scheduled:
        by_team.setdefault(r["team"], []).append((_hm(r["start"]), _hm(r["end"]), r["machine_id"]))
    for team, items in by_team.items():
        items.sort()
        for (s1, e1, m1), (s2, e2, m2) in zip(items, items[1:]):
            if s2 < e1 + PREP_MIN:
                out.append(f"{team} #{m1}({e1 // 60:02d}:{e1 % 60:02d} 종료) → #{m2} 간격 {s2 - e1}분 < {PREP_MIN}분")
    return out


def candidates(as_of: str) -> list[int]:
    return [f["machine_id"] for f in load_findings(as_of)]


def s1_s2() -> None:
    as_of = "2015-10-01 06:00:00"
    header(f"S1: {as_of} 일정 규칙")
    ids = candidates(as_of)
    r1 = schedule_day(ids, as_of)
    show(r1)
    bad = violations(r1)
    scheduled = [r for r in r1["rows"] if r["team"]]
    check("S1: 작업이 일정에 들어감", len(scheduled) == len(ids), f"{len(scheduled)}/{len(ids)}건")
    check("S1: 09:00~18:00 안, 점심과 겹침 없음, 같은 팀 30분 이상 간격", not bad, "; ".join(bad) or "위반 없음")
    check("S1: 재고 0 부품은 임시 점검 + 발주 필요",
          all(r["task_type"] == "temporary_check" and r["needs_purchase"] for r in r1["rows"] if r["stock_qty"] == 0))

    header("S2: 결정적 (같은 입력 두 번)")
    r2 = schedule_day(ids, as_of)
    same = json.dumps(r1, sort_keys=True, default=str) == json.dumps(r2, sort_keys=True, default=str)
    check("S2: 두 번 실행 결과 완전히 같음", same)


def s3() -> None:
    as_of = "2015-10-01 06:00:00"
    window = {"2팀": ["13:00", "18:00"]}
    header(f"S3: team_available {window}")
    r = schedule_day(candidates(as_of), as_of, team_available=window)
    show(r)
    team2 = [x for x in r["rows"] if x["team"] == "2팀"]
    check("S3: 2팀 작업은 모두 13:00 이후 시작", all(_hm(x["start"]) >= _hm("13:00") for x in team2),
          f"2팀 {[(x['machine_id'], x['start']) for x in team2]}")
    check("S3: 2팀 가능 시간 13:00~18:00 반영", r["windows"]["2팀"] == ["13:00", "18:00"])
    check("S3: 일정 규칙 위반 없음", not violations(r), "; ".join(violations(r)) or "")

    # 2팀 작업이 실제로 생기는 큰 입력에서도 확인 (기계 작업이 많은 가짜 입력)
    fake = fake_tasks([("drive", 180, "기계")] * 4 + [("bearing", 90, "기계")] * 2)
    r_fake = schedule_tasks(fake, load_teams(), team_available=window)
    team2 = [x for x in r_fake["rows"] if x["team"] == "2팀"]
    check("S3: (가짜 기계 작업 6개) 2팀 작업이 있고 모두 13:00 이후", bool(team2) and all(
        _hm(x["start"]) >= _hm("13:00") for x in team2), f"2팀 {[(x['machine_id'], x['start']) for x in team2]}")


def fake_tasks(specs: list[tuple[str, int, str]]) -> list[dict]:
    """(comp, duration_min, skill) -> 작업 행. 점수는 앞에서부터 높게 (결정적)."""
    return [{"machine_id": 100 + i, "comp": comp, "task_type": "replace", "duration_min": d, "skill": skill,
             "score": round(3.0 - i * 0.1, 4), "stock_qty": 1, "label": "예측 고장", "needs_purchase": False,
             "team": None, "start": None, "end": None, "skill_match": None, "slot": None}
            for i, (comp, d, skill) in enumerate(specs)]


def s4() -> None:
    header("S4: 작업 10개 (넘치는 작업은 내일 우선)")
    specs = [("drive", 180, "기계"), ("motor", 120, "전기"), ("pump", 120, "유체"), ("drive", 180, "기계"),
             ("bearing", 90, "기계"), ("motor", 120, "전기"), ("drive", 180, "기계"), ("pump", 120, "유체"),
             ("drive", 180, "기계"), ("drive", 180, "기계")]
    r = schedule_tasks(fake_tasks(specs), load_teams())
    show(r)
    total = sum(d for _, d, _ in specs)
    lunch = _hm(LUNCH[1]) - _hm(LUNCH[0])
    capacity = sum(_hm(w[1]) - _hm(w[0]) - lunch for w in r["windows"].values())
    print(f"작업 합계 {total}분 / 가용 {capacity}분 (점심 제외)")
    tomorrow = [x for x in r["rows"] if x["slot"] == TOMORROW]
    check("S4: 넘치는 작업이 '내일 우선'", bool(tomorrow), f"내일 우선 {[x['machine_id'] for x in tomorrow]}")
    check("S4: 내일 우선 작업은 팀·시간 없음", all(x["team"] is None and x["start"] is None for x in tomorrow))
    check("S4: 일정에 들어간 작업은 규칙 위반 없음", not violations(r), "; ".join(violations(r)) or "")
    check("S4: 가동률 100% 이하", all(u <= 1 for u in r["utilization"].values()), str(r["utilization"]))


def s5() -> None:
    as_of = "2015-12-22 06:00:00"
    header(f"S5: {as_of} 13번 설비 센서 점검")
    r = schedule_day(candidates(as_of), as_of)
    show(r)
    row13 = next((x for x in r["rows"] if x["machine_id"] == 13), None)
    ok = (row13 is not None and row13["task_type"] == "sensor_check" and row13["duration_min"] == 60
          and row13["team"] is not None and _hm(row13["end"]) - _hm(row13["start"]) == 60)
    check("S5: 13번 sensor_check 1시간 작업이 일정에 들어감", ok, row13 and row13["slot"])
    check("S5: 슬롯에 '사람 확인' 표시", row13 is not None and row13["slot"].startswith("사람 확인"))


def main() -> None:
    s1_s2()
    s3()
    s4()
    s5()
    header("요약")
    for name, ok in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}")
    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, LLM 사용 안 함):
#   uv run python scripts/test_schedule.py
