"""계획 에이전트용 도구: 정비 우선순위 계산, 재고 확인, 작업 지시서·발주 요청서 초안.

- load_findings(as_of): 조사 결과(evidence JSON) 읽기 (도구 아님)
- rank_maintenance -> schedule_day: 순수 코드로 하루 일정을 팀별로 배치 (LLM은 계산하지 않음)
  · 작업: 예측 고장+재고 있음=replace, 예측 고장+재고 0=temporary_check(+발주), 센서·설치 이상=sensor_check
  · 점수 = prob × CRIT_WEIGHT[중요도] × (재고 0이면 NO_STOCK_FACTOR), sensor_check는 × SENSOR_CHECK_FACTOR
  · 그리디 배치: 점수 높은 작업부터, 팀별 다음 가능 시각(이전 종료 + 준비 시간), 전문 팀 우선(SKILL_WAIT_LIMIT_MIN 이내),
    점심시간과 겹치면 점심 뒤로 (근무·점심·준비 시간은 ops_settings 운영 설정), 근무 종료 전에 못 끝나면 "내일 우선". exclude_machines는 "제외(사유)"
  결과는 outputs/plans/{YYYYMMDD}_v{n}.json (버전별) + outputs/plans/{YYYYMMDD}.json (최신본)
- 초안은 outputs/drafts/{YYYYMMDD}.json에 draft_id를 키로 저장 (같은 id는 덮어쓰기 = 중복 생성 방지)
  v2 이상 배정안의 초안은 draft_id에 "-v{n}" 접미사, 새 버전이 생기면 이전 PENDING 초안은 SUPERSEDED
- 등록(submit)은 하지 않는다. 모든 초안은 status="PENDING".
"""

import json
import re
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
from langchain.tools import ToolRuntime, tool

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from config import (  # noqa: E402
    COMP_LABEL_KO, CREWS, CRIT_WEIGHT, NO_STOCK_FACTOR, SENSOR_CHECK_FACTOR, SKILL_WAIT_LIMIT_MIN,
)
from ops_settings import load_settings  # noqa: E402
from store import app_db  # noqa: E402
from schemas import finding_compat  # noqa: E402
from tools.context import Ctx  # noqa: E402

DB_PATH = ROOT_DIR / "data" / "pdm.db"
EVIDENCE_DIR = ROOT_DIR / "evidence"
PLANS_DIR = ROOT_DIR / "outputs" / "plans"
DRAFTS_DIR = ROOT_DIR / "outputs" / "drafts"

COMPS = ["motor", "drive", "pump", "bearing"]
SENSOR_LABEL = "센서·설치 이상 가능성"
HUMAN_CHECK = "사람 확인"
TOMORROW = "내일 우선"  # 오늘 근무 시간 안에 끝나지 않는 작업
EXCLUDED = "제외"  # slot = "제외(사유)"
SUPERSEDED = "SUPERSEDED"  # 새 배정안으로 대체되어 등록할 수 없는 초안
COMMON_SKILL = "공통"
TASK_LABEL = {"replace": "교체", "temporary_check": "임시 점검", "sensor_check": "센서 점검",
              "urgent_check": "긴급 점검"}  # urgent_check: 경고 자동 배정 (live_ops.dispatch_urgent, 계획에는 없음)
WO_TASKS = {"replace", "temporary_check"}  # 작업 지시서를 만드는 작업 (sensor_check는 사람 확인)
SAFETY_FIRST_STEP = "점검 전 설비 정지 절차 확인"


def formula() -> str:
    """점수·배치 규칙 설명 (운영 설정의 근무·점심·준비 시간을 호출 시점에 반영)."""
    cfg = load_settings()
    return (f"점수 = 확률 × 중요도 가중치(A {CRIT_WEIGHT['A']} / B {CRIT_WEIGHT['B']} / C {CRIT_WEIGHT['C']})"
            f" × 재고 계수(재고 0이면 {NO_STOCK_FACTOR}, 아니면 1), 센서 점검은 × {SENSOR_CHECK_FACTOR}. "
            f"점수 높은 작업부터 팀별로 배치 (근무 {cfg['work_start']}~{cfg['work_end']}, 작업 사이 준비 {cfg['prep_min']}분,"
            f" 점심 {cfg['lunch_start']}~{cfg['lunch_end']} 회피, "
            f"전문 팀이 가장 빠른 팀보다 {SKILL_WAIT_LIMIT_MIN}분 이내로 늦으면 전문 팀). "
            f"근무 종료 전에 못 끝나면 '{TOMORROW}'. '{SENSOR_LABEL}' 설비는 센서 점검({HUMAN_CHECK}).")

# 에이전트가 도구를 병렬로 호출해도 초안 파일을 안전하게 읽고 갱신 (읽기도 잠근다: 쓰는 중인 빈 파일을 읽지 않도록).
# 갱신 함수 안에서 load_drafts를 다시 부르므로 재진입 가능한 RLock
_drafts_lock = threading.RLock()


# ---------------------------------------------------------------- 공통
def _norm(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")


def _day(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y%m%d")


def _mmdd(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%m%d")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _md(df: pd.DataFrame) -> str:
    lines = ["| " + " | ".join(map(str, df.columns)) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(map(str, row)) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def load_findings(as_of: str) -> list[dict]:
    """evidence/{YYYYMMDD}/*.json 중 같은 as_of의 조사 결과. [{machine_id, comp, label, ..., prob, evidence_id}]"""
    as_of = _norm(as_of)
    folder = EVIDENCE_DIR / _day(as_of)
    findings = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        ev = json.loads(path.read_text(encoding="utf-8"))
        if ev.get("as_of") != as_of:
            continue
        findings.append({**finding_compat(ev["finding"]), "prob": ev["input"]["prob"], "evidence_id": ev["evidence_id"]})
    return sorted(findings, key=lambda f: f["machine_id"])


def _stock(as_of: str | None = None) -> dict[str, dict]:
    """부품 재고. as_of를 주면 parts_stock 기본 수량 + 운영 DB stock_ledger(as_of 이하) 합계 (0 미만은 0)."""
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM parts_stock", conn)
    stock = {r["comp"]: r for r in df.to_dict("records")}
    if as_of is not None:
        for comp, delta in app_db.stock_delta(as_of).items():
            if comp in stock:
                stock[comp] = {**stock[comp], "qty": max(0, int(stock[comp]["qty"]) + delta)}
    return stock


def _criticality() -> dict[int, dict]:
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM criticality", conn)
    return {int(r["machine_id"]): r for r in df.to_dict("records")}


# ---------------------------------------------------------------- 하루 일정 배치 (순수 코드)
def default_teams(crews: int = CREWS) -> list[str]:
    return [f"{i}팀" for i in range(1, crews + 1)]


def _hm(text: str) -> int:
    h, m = map(int, text.split(":"))
    return h * 60 + m


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def load_teams() -> list[dict]:
    """teams 테이블: [{"team", "skill", "shift_start", "shift_end"}] (팀 이름 순)."""
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM teams ORDER BY team", conn)
    return df.to_dict("records")


def load_work_standards() -> dict[tuple[str, str], dict]:
    """work_standards 테이블: {(task_type, comp): {"duration_min", "skill"}} (comp ""는 부품 무관)."""
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM work_standards", conn)
    return {(r["task_type"], r["comp"]): {"duration_min": int(r["duration_min"]), "skill": r["skill"]}
            for r in df.to_dict("records")}


TEAM_SLOT_RE = re.compile(r"^(\d+팀) (\d\d:\d\d)(?:-(\d\d:\d\d))?$")


def is_assigned(slot: str | None) -> bool:
    """작업 지시서 대상으로 팀 일정에 들어간 슬롯인지 ("1팀 09:00-11:00"). 사람 확인·내일 우선·제외는 False."""
    return bool(slot) and TEAM_SLOT_RE.match(slot) is not None


def slot_team(slot: str | None) -> str | None:
    """슬롯 문자열에서 팀 이름 ("1팀 09:00-11:00", "사람 확인 · 2팀 10:30-11:30" 모두)."""
    m = re.search(r"(\d+팀) \d\d:\d\d", slot or "")
    return m.group(1) if m else None


def build_tasks(candidates: list[int], as_of: str, exclude_machines: list[int] | None = None,
                defer_reason: dict[int, str] | None = None) -> list[dict]:
    """조사 결과·재고·중요도·작업 표준으로 설비별 작업을 만든다 (배치 전). 제외 설비는 task_type=None."""
    as_of = _norm(as_of)
    exclude = {int(m) for m in (exclude_machines or [])}
    reasons = {int(k): v for k, v in (defer_reason or {}).items()}
    findings = {f["machine_id"]: f for f in load_findings(as_of)}
    stock, crit, std = _stock(as_of), _criticality(), load_work_standards()

    rows = []
    for m in candidates:
        f, c = findings[m], crit[m]
        qty = int(stock[f["comp"]]["qty"])
        score = f["prob"] * CRIT_WEIGHT[c["criticality"]] * (NO_STOCK_FACTOR if qty == 0 else 1)
        if f["label"] == SENSOR_LABEL:
            task, s = "sensor_check", std[("sensor_check", "")]
            score *= SENSOR_CHECK_FACTOR
        elif qty == 0:
            task, s = "temporary_check", std[("temporary_check", "")]
        else:
            task, s = "replace", std[("replace", f["comp"])]
        row = {
            "machine_id": m, "comp": f["comp"], "prob": round(f["prob"], 4), "criticality": c["criticality"],
            "line": c["line"], "bottleneck": int(c["bottleneck"]), "stock_qty": qty, "score": round(score, 4),
            "label": f["label"], "evidence_id": f["evidence_id"],
            "task_type": task, "duration_min": s["duration_min"], "skill": s["skill"],
            "needs_purchase": task == "temporary_check",
            "team": None, "start": None, "end": None, "skill_match": None, "slot": None, "reason": None,
        }
        if m in exclude:
            row.update(task_type=None, duration_min=None, skill=None, needs_purchase=False, reason="제외 조건",
                       slot=f"{EXCLUDED}({reasons.get(m) or '사유 미기재'})")
        rows.append(row)
    return rows


def schedule_tasks(rows: list[dict], teams_info: list[dict], available_teams: list[str] | None = None,
                   team_available: dict[str, list[str]] | None = None, start_from: str | None = None,
                   occupied: dict[str, list[tuple[str, str]]] | None = None) -> dict:
    """작업 행에 팀·시작·종료를 배치한다 (그리디, 결정적, DB를 읽지 않는 순수 함수).

    rows: machine_id, score, task_type(None이면 제외), duration_min, skill이 있는 행. 결과는 rows를 갱신한다.
    근무 시간·점심·준비 시간은 운영 설정(ops_settings, 설정 페이지)에서 읽는다. 근무 시간은 모든 팀에 같게 적용.
    start_from("HH:MM"): 주어지면 모든 팀의 시작 가능 시각을 max(근무 시작, start_from)으로 (긴급 점검용).
    occupied: {팀: [("HH:MM", "HH:MM"), ...]} 이미 잡힌 작업 시간. 이 구간(+준비 시간)과 겹치지 않게 놓는다.
    반환: {"rows"(점수 순), "utilization": {팀: 비율}, "tomorrow": [설비], "windows": {팀: [시작, 종료]}}
    """
    info = {t["team"]: t for t in teams_info}
    teams = [t for t in (available_teams if available_teams is not None else list(info)) if t in info]
    cfg = load_settings()
    lunch_s, lunch_e = _hm(cfg["lunch_start"]), _hm(cfg["lunch_end"])
    lunch_text, prep_min = f"{cfg['lunch_start']}~{cfg['lunch_end']}", cfg["prep_min"]

    window = {}
    for t in teams:  # 근무 시간(운영 설정)과 오늘 가능 시간(team_available)의 교집합
        s, e = _hm(cfg["work_start"]), _hm(cfg["work_end"])
        if team_available and t in team_available:
            ws, we = team_available[t]
            s, e = max(s, _hm(ws)), min(e, _hm(we))
        if start_from:
            s = max(s, _hm(start_from))
        window[t] = (s, e)
    next_free = {t: window[t][0] for t in teams}
    busy = {t: 0 for t in teams}
    blocks = {t: sorted((_hm(a), _hm(b)) for a, b in (occupied or {}).get(t, [])) for t in teams}

    def fit(t: str, s: int, d: int) -> int:
        """s 이후 점심·이미 잡힌 작업과 겹치지 않는 가장 이른 시작 시각."""
        moved = True
        while moved:
            moved = False
            if s < lunch_e and s + d > lunch_s:  # 점심시간과 겹치면 점심 뒤로 (작업을 쪼개지 않음)
                s, moved = max(s, lunch_e), True
            for a, b in blocks[t]:
                if s < b + prep_min and s + d + prep_min > a:  # 기존 작업 앞뒤로 준비 시간
                    s, moved = b + prep_min, True
        return s

    for r in sorted((r for r in rows if r.get("task_type")), key=lambda r: (-r["score"], r["machine_id"])):
        d = r["duration_min"]
        options = {}
        for t in teams:
            s = fit(t, next_free[t], d)
            if s + d <= window[t][1]:
                options[t] = s
        if not options:
            r.update(team=None, start=None, end=None, skill_match=None, slot=TOMORROW,
                     reason=f"{d}분 작업이 어느 팀도 근무 종료 전에 끝나지 않음 → {TOMORROW}")
            continue
        earliest = min(options.values())
        chosen = next(t for t in teams if options.get(t) == earliest)  # 같은 시각이면 팀 순서
        if r["skill"] == COMMON_SKILL:
            reason = "공통 작업 → 가장 빠른 팀"
        else:
            skilled = [t for t in teams if t in options and info[t]["skill"] == r["skill"]]
            best = min(skilled, key=lambda t: (options[t], teams.index(t))) if skilled else None
            if best and options[best] - earliest <= SKILL_WAIT_LIMIT_MIN:
                chosen = best
                reason = f"{r['skill']} 전문 팀"
            elif best:
                reason = (f"{r['skill']} 전문 {best}은 {_fmt(options[best])}부터 가능({options[best] - earliest}분 늦음,"
                          f" 기준 {SKILL_WAIT_LIMIT_MIN}분 초과) → 가장 빠른 팀")
            else:
                reason = f"{r['skill']} 전문 팀이 오늘 불가 → 가장 빠른 팀"
        s = options[chosen]
        e = s + d
        if s != next_free[chosen]:
            nf = next_free[chosen]
            lunch_hit = nf < lunch_e and nf + d > lunch_s and s == lunch_e
            reason += (f", 점심({lunch_text})과 겹쳐 {_fmt(s)} 시작" if lunch_hit
                       else f", 이미 잡힌 작업을 피해 {_fmt(s)} 시작")
        if r["task_type"] == "temporary_check":
            reason += ", 재고 0 → 교체 대신 임시 점검 + 발주"
        r["reason"] = reason
        next_free[chosen] = e + prep_min
        busy[chosen] += d
        time_text = f"{chosen} {_fmt(s)}-{_fmt(e)}"
        r.update(team=chosen, start=_fmt(s), end=_fmt(e),
                 skill_match=r["skill"] == COMMON_SKILL or info[chosen]["skill"] == r["skill"],
                 slot=f"{HUMAN_CHECK} · {time_text}" if r["task_type"] == "sensor_check" else time_text)

    utilization = {}
    for t in teams:
        s, e = window[t]
        lunch_overlap = max(0, min(e, lunch_e) - max(s, lunch_s))
        avail = max(0, e - s - lunch_overlap)
        utilization[t] = round(busy[t] / avail, 3) if avail else 0.0
    ordered = sorted(rows, key=lambda r: (-r["score"], r["machine_id"]))
    return {
        "rows": ordered,
        "utilization": utilization,
        "tomorrow": [r["machine_id"] for r in ordered if r["slot"] == TOMORROW],
        "windows": {t: [_fmt(window[t][0]), _fmt(window[t][1])] for t in teams},
    }


def schedule_day(candidates: list[int], as_of: str, available_teams: list[str] | None = None,
                 exclude_machines: list[int] | None = None, defer_reason: dict[int, str] | None = None,
                 team_available: dict[str, list[str]] | None = None) -> dict:
    """후보 설비의 하루 정비 일정 (저장하지 않음). 반환 형식은 schedule_tasks와 같다."""
    rows = build_tasks(candidates, as_of, exclude_machines, defer_reason)
    return schedule_tasks(rows, load_teams(), available_teams, team_available)


def rank(candidates: list[int], crews: int, as_of: str, available_teams: list[str] | None = None,
         exclude_machines: list[int] | None = None, defer_reason: dict[int, str] | None = None,
         version: int | None = None, meta: dict | None = None,
         team_available: dict[str, list[str]] | None = None) -> list[dict]:
    """하루 일정을 배치하고 배정 파일에 저장한다. 배정표(행 목록, 점수 순)를 돌려준다.

    available_teams가 None이면 1팀~{crews}팀. version이 None이면 현재 최신 버전을 다시 계산(없으면 v1).
    """
    as_of = _norm(as_of)
    teams = default_teams(crews) if available_teams is None else list(available_teams)
    exclude = sorted({int(m) for m in (exclude_machines or [])})
    reasons = {int(k): v for k, v in (defer_reason or {}).items()}
    result = schedule_day(candidates, as_of, teams, exclude, reasons, team_available)

    if version is None:
        current = load_plan(as_of)
        version = current.get("version", 1) if current else 1
    save_plan(as_of, {
        "as_of": as_of, "version": version, "crews": crews, "available_teams": teams,
        "team_available": team_available or {}, "team_windows": result["windows"],
        "exclude_machines": exclude, "defer_reason": {str(k): v for k, v in reasons.items()},
        "formula": formula(), "created_at": _now(), "rows": result["rows"],
        "utilization": result["utilization"], "tomorrow": result["tomorrow"], **(meta or {}),
    })
    return result["rows"]


def save_plan(as_of: str, plan: dict) -> None:
    """버전 파일({day}_v{n}.json)과 최신본({day}.json)을 함께 저장."""
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    (PLANS_DIR / f"{_day(as_of)}_v{plan['version']}.json").write_text(text, encoding="utf-8")
    (PLANS_DIR / f"{_day(as_of)}.json").write_text(text, encoding="utf-8")


def load_plan(as_of: str, version: int | None = None) -> dict | None:
    """최신 배정안 (version을 주면 그 버전)."""
    name = f"{_day(as_of)}.json" if version is None else f"{_day(as_of)}_v{version}.json"
    path = PLANS_DIR / name
    if not path.exists():
        return None
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("as_of") != _norm(as_of):
        return None
    plan.setdefault("version", 1)
    return plan


def plan_versions(as_of: str) -> list[int]:
    """저장된 배정안 버전 번호 (outputs/plans/{day}_v{n}.json)."""
    pattern = re.compile(rf"^{_day(as_of)}_v(\d+)\.json$")
    found = [int(m.group(1)) for p in PLANS_DIR.glob(f"{_day(as_of)}_v*.json") if (m := pattern.match(p.name))]
    return sorted(found)


def next_steps(rows: list[dict], as_of: str | None = None) -> str:
    """배정표 다음에 할 일 (에이전트 안내용)."""
    assigned = [r for r in rows if is_assigned(r["slot"])]
    wo = ", ".join(f"#{r['machine_id']} {r['comp']}({TASK_LABEL[r['task_type']]})" for r in assigned) or "없음"
    no_stock = sorted({r["comp"] for r in assigned if r.get("needs_purchase")})
    pr = ", ".join(f"{c}(수량 {purchase_qty(c, as_of)})" for c in no_stock) or "없음 (재고 0인 배정 부품 없음)"
    skip = ", ".join(f"#{r['machine_id']}({r['slot']})" for r in rows if not is_assigned(r["slot"])) or "없음"
    return (f"다음 단계: 작업 지시서 초안 대상 {wo} / 발주 요청서 초안 대상 {pr} / "
            f"초안을 만들지 않는 설비 {skip}")


def plan_table(rows: list[dict]) -> str:
    """일정표 (점수 순위, 작업, 팀·시간, 전문 분야 일치)."""
    if not rows:
        return "배정 대상 없음 (조사된 고위험 설비 0대)"
    df = pd.DataFrame([{
        "순위": i, "설비": f"#{r['machine_id']}", "부품": r["comp"],
        "작업": TASK_LABEL.get(r.get("task_type"), "-") + (f" {r['duration_min']}분" if r.get("duration_min") else ""),
        "점수": f"{r['score']:.3f}", "중요도": r["criticality"] + (" (병목)" if r["bottleneck"] else ""),
        "재고": r["stock_qty"], "전문 분야": r.get("skill") or "-",
        "팀 일치": {True: "예", False: "아니오"}.get(r.get("skill_match"), "-"), "배정": r["slot"],
        "배정 사유(코드)": r.get("reason") or "-",
    } for i, r in enumerate(rows, 1)])
    return _md(df)


def schedule_summary(plan: dict) -> str:
    """팀별 가동률·가능 시간과 내일로 넘어간 작업 (에이전트·리포트 안내용)."""
    windows = plan.get("team_windows") or {}
    util = ", ".join(f"{t} {u:.0%} ({'~'.join(windows.get(t, ['-', '-']))})"
                     for t, u in (plan.get("utilization") or {}).items()) or "투입 가능한 팀 없음"
    tomorrow = ", ".join(f"#{m}" for m in plan.get("tomorrow") or []) or "없음"
    return f"팀별 가동률(작업 시간/가용 시간): {util} / 내일 우선: {tomorrow}"


# ---------------------------------------------------------------- 초안 저장 (draft_id 키, 덮어쓰기)
def _drafts_path(as_of: str) -> Path:
    return DRAFTS_DIR / f"{_day(as_of)}.json"


def load_drafts(as_of: str) -> dict[str, dict]:
    path = _drafts_path(as_of)
    with _drafts_lock:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_draft(as_of: str, draft: dict) -> dict:
    """draft_id를 키로 저장. 이미 있으면 덮어쓰되 최초 생성 시각(created_at)과
    이미 결정된 상태(APPROVED/REJECTED)는 유지한다 (계획을 다시 세워도 승인 결과가 되돌아가지 않도록)."""
    with _drafts_lock:
        drafts = load_drafts(as_of)
        old = drafts.get(draft["draft_id"])
        draft = {**draft, "created_at": old["created_at"] if old else _now(), "updated_at": _now()}
        if old and old.get("status") in ("APPROVED", "REJECTED", SUPERSEDED):
            draft["status"] = old["status"]
        drafts[draft["draft_id"]] = draft
        _write_drafts(as_of, drafts)
    return draft


def _write_drafts(as_of: str, drafts: dict) -> None:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    _drafts_path(as_of).write_text(json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")


def set_draft_status(as_of: str, draft_id: str, status: str) -> dict | None:
    """초안 상태 변경 (승인·거절). 없는 draft_id면 None."""
    with _drafts_lock:
        drafts = load_drafts(as_of)
        if draft_id not in drafts:
            return None
        drafts[draft_id] = {**drafts[draft_id], "status": status, "decided_at": _now()}
        _write_drafts(as_of, drafts)
        return drafts[draft_id]


def stamp_decisions(as_of: str, draft_ids: list[str], decided_by: str) -> list[str]:
    """사람이 결정한(PENDING이 아닌) 초안에 처리자·처리 시각(실제 현재 시각)을 남긴다. 기록한 draft_id 목록."""
    with _drafts_lock:
        drafts = load_drafts(as_of)
        stamped = [k for k in draft_ids if k in drafts and drafts[k]["status"] != "PENDING"
                   and not drafts[k].get("decided_by")]
        for k in stamped:
            drafts[k] = {**drafts[k], "decided_by": decided_by, "decided_at": _now()}
        if stamped:
            _write_drafts(as_of, drafts)
    return stamped


def supersede_pending(as_of: str, new_version: int) -> list[str]:
    """new_version보다 이전 버전의 PENDING 초안을 SUPERSEDED로 바꾼다 (등록 불가). 바뀐 draft_id 목록."""
    with _drafts_lock:
        drafts = load_drafts(as_of)
        changed = [k for k, d in drafts.items() if d["status"] == "PENDING" and d.get("version", 1) < new_version]
        for k in changed:
            drafts[k] = {**drafts[k], "status": SUPERSEDED, "decided_at": _now(), "superseded_by": f"v{new_version}"}
        if changed:
            _write_drafts(as_of, drafts)
    return changed


def _versioned(draft_id: str, version: int) -> str:
    return draft_id if version <= 1 else f"{draft_id}-v{version}"


def make_work_order(as_of: str, machine_id: int, comp: str, checklist: list[str], auto_filled: bool = False,
                    version: int | None = None) -> dict:
    as_of = _norm(as_of)
    f = next(x for x in load_findings(as_of) if x["machine_id"] == machine_id)
    plan = load_plan(as_of) or {"rows": [], "version": 1}
    version = version or plan["version"]
    row = next((r for r in plan["rows"] if r["machine_id"] == machine_id), {})
    task = row.get("task_type") or "replace"
    items = [c for c in checklist if c.strip() and c.strip() != SAFETY_FIRST_STEP]
    if task == "temporary_check":  # 재고 0: 교체 대신 점검·임시 조치, 부품 입고 후 교체
        items = [*items, "부품 재고 없음 - 교체하지 말고 점검·임시 조치 후 발주 입고 시 교체 일정 재등록"]
    return save_draft(as_of, {
        "draft_id": _versioned(f"WO-{_mmdd(as_of)}-{machine_id:03d}", version),
        "type": "work_order",
        "version": version,
        "status": "PENDING",
        "as_of": as_of,
        "machine_id": machine_id,
        "comp": comp,
        "slot": row.get("slot"),
        "team": row.get("team"),
        "start": row.get("start"),
        "end": row.get("end"),
        "task_type": task,
        "duration_min": row.get("duration_min"),
        "title": f"#{machine_id} {COMP_LABEL_KO[comp]}({comp}) {TASK_LABEL[task]}",
        "checklist": [SAFETY_FIRST_STEP, *items],
        "summary": f["summary"],
        "manual_source": f.get("manual_source", ""),
        "evidence_ids": [f["evidence_id"]],
        "auto_filled": auto_filled,
    })


def make_purchase_request(as_of: str, comp: str, qty: int, auto_filled: bool = False,
                          version: int | None = None, order_date: str | None = None, note: str = "") -> dict:
    """발주 요청서 초안. order_date(희망 발주일, YYYY-MM-DD)가 없으면 기준 시각 날짜(오늘)."""
    as_of = _norm(as_of)
    s = _stock(as_of)[comp]
    version = version or (load_plan(as_of) or {"version": 1})["version"]
    reasons = [f for f in load_findings(as_of) if f["comp"] == comp and f["label"] != SENSOR_LABEL]
    return save_draft(as_of, {
        "draft_id": _versioned(f"PR-{_mmdd(as_of)}-{comp}", version),
        "type": "purchase_request",
        "version": version,
        "status": "PENDING",
        "as_of": as_of,
        "comp": comp,
        "part_name": s["part_name"],
        "qty": qty,
        "current_qty": int(s["qty"]),
        "safety_stock": int(s["safety_stock"]),
        "lead_time_days": int(s["lead_time_days"]),
        "order_date": order_date or as_of[:10],
        "note": note,
        "for_machines": [f["machine_id"] for f in reasons],
        "evidence_ids": [f["evidence_id"] for f in reasons],
        "auto_filled": auto_filled,
    })


def expected_arrival(order_date: str, lead_time_days: int) -> str:
    """희망 발주일 + 입고 소요일 = 예상 입고일 (YYYY-MM-DD)."""
    return (pd.Timestamp(order_date) + pd.Timedelta(days=int(lead_time_days))).strftime("%Y-%m-%d")


def _purchase_orders(comp: str, as_of: str) -> tuple[dict | None, list[dict]]:
    """(입고 대기 중인 발주, 입고 예정일이 지났는데 입고 처리 안 된 발주들). 기준 날짜 이전 점검에서 승인된 발주만 본다.
    입고 대기 = 승인됨(REQUESTED) + 기준 날짜 < 희망 발주일 + 입고 소요일."""
    day = _norm(as_of)[:10]
    lead = int(_stock()[comp]["lead_time_days"])
    in_transit, overdue = None, []
    for po in app_db.purchase_orders():
        if po["comp"] != comp or po["status"] != "REQUESTED" or po["as_of_date"] >= day:
            continue
        arrival = expected_arrival(po.get("order_date") or po["as_of_date"], lead)
        po = {**po, "expected_arrival": arrival}
        if day < arrival:
            in_transit = in_transit or po
        else:
            overdue.append(po)
    return in_transit, overdue


def zero_stock_parts(as_of: str) -> list[dict]:
    """기준 시각 재고가 0인 부품과 발주 상태 [{comp, part_name, lead_time_days, in_transit, overdue}]."""
    out = []
    for comp, s in _stock(as_of).items():
        if int(s["qty"]) == 0:
            in_transit, overdue = _purchase_orders(comp, as_of)
            out.append({"comp": comp, "part_name": s["part_name"], "lead_time_days": int(s["lead_time_days"]),
                        "in_transit": in_transit, "overdue": overdue})
    return out


def purchase_qty(comp: str, as_of: str | None = None) -> int:
    """발주 수량 = 안전 재고 - 현재 재고 (최소 1)."""
    s = _stock(as_of)[comp]
    return max(1, int(s["safety_stock"]) - int(s["qty"]))


# ---------------------------------------------------------------- 도구
@tool
def rank_maintenance(candidates: list[int], runtime: ToolRuntime[Ctx], crews: int = CREWS,
                     available_teams: list[str] | None = None, exclude_machines: list[int] | None = None,
                     team_available: dict[str, list[str]] | None = None) -> str:
    """조사된 고위험 설비들의 정비 우선순위를 계산하고 팀을 배정한다 (계산은 코드가 한다).

    언제 쓰나: 계획을 세울 때 가장 먼저 한 번. 후보는 조사 결과가 있는 설비 번호를 그대로 넣는다.
    언제 쓰지 않나: 재고만 볼 때(check_parts_stock), 초안을 만들 때.
    점수 = 확률 × 중요도 가중치 × 재고 계수. "센서·설치 이상 가능성" 설비는 "사람 확인"으로 배정하지 않는다.

    Args:
        candidates: 후보 설비 번호 목록 (조사 결과가 있는 설비)
        crews: 투입 가능한 팀 수 (기본 3, available_teams를 주면 무시)
        available_teams: 배정 가능한 팀 이름 순서 (예: ["1팀", "3팀"]). 없으면 1팀~crews팀
        exclude_machines: 오늘 정비에서 뺄 설비 번호 ("제외"로 표시)
        team_available: 팀별 오늘 가능 시간 (예: {"2팀": ["13:00", "18:00"]})
    """
    as_of = runtime.context.as_of
    if not isinstance(crews, int) or isinstance(crews, bool) or not 1 <= crews <= 10:
        return f"입력 오류: crews는 1~10 사이 정수여야 합니다 (받은 값: {crews!r})"
    if not isinstance(candidates, list) or any(isinstance(m, bool) or not isinstance(m, int) for m in candidates):
        return f"입력 오류: candidates는 정수 목록이어야 합니다 (받은 값: {candidates!r})"
    if available_teams is not None and (not isinstance(available_teams, list)
                                        or any(not re.fullmatch(r"\d+팀", str(t)) for t in available_teams)):
        return f"입력 오류: available_teams는 \"1팀\" 같은 팀 이름 목록이어야 합니다 (받은 값: {available_teams!r})"
    ids = list(dict.fromkeys(candidates))
    known = {f["machine_id"] for f in load_findings(as_of)}
    if missing := [m for m in ids if m not in known]:
        return f"입력 오류: 조사 결과가 없는 설비 {missing}. 조사된 설비: {sorted(known)}"
    rows = rank(ids, crews, as_of, available_teams=available_teams, exclude_machines=exclude_machines,
                team_available=team_available)
    return f"{plan_table(rows)}\n{schedule_summary(load_plan(as_of))}\n{formula()}\n{next_steps(rows, as_of)}"


@tool
def check_parts_stock(comp: str, runtime: ToolRuntime[Ctx]) -> str:
    """부품 재고 수량, 안전 재고, 입고 소요일을 한 줄로 알려준다.

    언제 쓰나: 배정된 설비의 교체 부품을 바로 쓸 수 있는지, 발주가 필요한지 확인할 때.
    언제 쓰지 않나: 우선순위 계산(rank_maintenance)에는 재고가 이미 반영되어 있다.

    Args:
        comp: 부품 motor, drive, pump, bearing 중 하나
    """
    if comp not in COMPS:
        return f"입력 오류: comp는 {', '.join(COMPS)} 중 하나여야 합니다 (받은 값: {comp!r})"
    as_of = runtime.context.as_of  # 기본 재고 + 기준 시각 이전 입고·사용 기록
    s = _stock(as_of)[comp]
    qty, safety = int(s["qty"]), int(s["safety_stock"])
    status = (f'재고 없음 - 발주 필요: draft_purchase_request(comp="{comp}", qty={purchase_qty(comp, as_of)})를 호출하세요'
              if qty == 0 else "안전 재고 미만이지만 재고가 있어 발주하지 않음" if qty < safety else "재고 충분")
    return (f"{comp}({s['part_name']}): 재고 {qty}개, 안전 재고 {safety}개, 입고 소요 {int(s['lead_time_days'])}일"
            f" -> {status}")


@tool
def draft_work_order(machine_id: int, comp: str, checklist: list[str], runtime: ToolRuntime[Ctx]) -> str:
    """배정된 설비 1대의 작업 지시서 초안을 만든다 (등록하지 않음, status=PENDING). 같은 설비는 덮어쓴다.

    언제 쓰나: rank_maintenance 일정표에서 팀·시간이 배정된 교체·임시 점검 작업마다 한 번.
    언제 쓰지 않나: "사람 확인"(센서 점검)·"내일 우선"·"제외" 설비. 체크리스트는 조사 결과의 checklist를 그대로 넣는다
    (맨 앞의 "점검 전 설비 정지 절차 확인"은 코드가 자동으로 넣는다).

    Args:
        machine_id: 설비 번호
        comp: 교체·점검 부품 (조사 결과의 comp)
        checklist: 점검 항목 목록
    """
    as_of = runtime.context.as_of
    f = next((x for x in load_findings(as_of) if x["machine_id"] == machine_id), None)
    if f is None:
        return f"입력 오류: 설비 {machine_id}의 조사 결과가 없습니다"
    if comp != f["comp"]:
        return f"입력 오류: 설비 {machine_id}의 조사 부품은 {f['comp']}입니다 (받은 값: {comp!r})"
    if f["label"] == SENSOR_LABEL:
        return f"차단됨: 설비 {machine_id}는 '{SENSOR_LABEL}'이라 '{HUMAN_CHECK}' 대상입니다. 작업 지시서를 만들지 않습니다."
    plan = load_plan(as_of)
    if plan is None:
        return "입력 오류: 배정표가 없습니다. 먼저 rank_maintenance를 호출하세요."
    slot = next((r["slot"] for r in plan["rows"] if r["machine_id"] == machine_id), None)
    if not is_assigned(slot):
        return (f"차단됨: 설비 {machine_id}는 팀에 배정되지 않았습니다 (배정: {slot or '배정표에 없음'})."
                " 작업 지시서는 'N팀' 배정 설비만 만듭니다.")
    if not isinstance(checklist, list) or not all(isinstance(c, str) for c in checklist):
        return "입력 오류: checklist는 문자열 목록이어야 합니다"
    d = make_work_order(as_of, machine_id, comp, checklist)
    return f"작업 지시서 초안 {d['draft_id']} (PENDING, {TASK_LABEL[d['task_type']]} {d['slot']}, 근거 {', '.join(d['evidence_ids'])}, 점검 항목 {len(d['checklist'])}개)"


@tool
def draft_purchase_request(comp: str, qty: int, runtime: ToolRuntime[Ctx], order_date: str | None = None,
                           note: str = "") -> str:
    """부품 발주 요청서 초안을 만든다 (등록하지 않음, status=PENDING). 같은 날 같은 부품은 덮어쓴다.

    언제 쓰나: "예측 고장" 설비의 부품 재고가 0일 때. 수량 = 안전 재고 - 현재 재고 (최소 1).
    언제 쓰지 않나: 재고가 1개 이상일 때 (안전 재고 미만이어도 발주하지 않음, 호출하면 차단됨),
    "사람 확인" 설비만 해당하는 부품일 때.

    Args:
        comp: 부품 motor, drive, pump, bearing 중 하나
        qty: 발주 수량 (1~100)
        order_date: 희망 발주일 "YYYY-MM-DD" (기준 시각 날짜 이후). 사용자가 날짜를 말하지 않았으면 비운다 (= 오늘)
        note: 발주 메모 (예: 오늘 요청을 권장하는 이유)
    """
    if comp not in COMPS:
        return f"입력 오류: comp는 {', '.join(COMPS)} 중 하나여야 합니다 (받은 값: {comp!r})"
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 100:
        return f"입력 오류: qty는 1~100 사이 정수여야 합니다 (받은 값: {qty!r})"
    as_of = runtime.context.as_of
    today = _norm(as_of)[:10]
    if order_date in (None, ""):
        order_date = today
    try:
        order_date = pd.Timestamp(str(order_date)).strftime("%Y-%m-%d")
    except ValueError:
        return f"입력 오류: order_date는 YYYY-MM-DD 형식이어야 합니다 (받은 값: {order_date!r})"
    if order_date < today:
        return f"입력 오류: 희망 발주일 {order_date}은 기준 날짜 {today}보다 이전입니다. 오늘 이후 날짜만 가능합니다."
    stock = _stock(as_of)
    current = int(stock[comp]["qty"])
    if current > 0:  # 발주는 재고 0인 부품만 (안전 재고 미만이라도 재고가 있으면 오늘 교체 가능)
        zero = [c for c, s in stock.items() if int(s["qty"]) == 0]
        return (f"차단됨: {comp} 재고가 {current}개 있어 발주 대상이 아닙니다. 발주는 재고 0인 부품만 만듭니다."
                f" 오늘 재고 0인 부품: {', '.join(zero) or '없음'}")
    in_transit, _ = _purchase_orders(comp, as_of)
    if in_transit:  # 이미 승인된 발주가 입고 대기 중이면 중복 발주하지 않는다
        return (f"차단됨: {comp}는 이미 발주({in_transit['po_id']}, 희망 발주일 {in_transit['order_date']})가 승인되어"
                f" 입고 대기 중입니다 (입고 예정 {in_transit['expected_arrival']}). 새 발주를 만들지 않습니다.")
    version = (load_plan(as_of) or {"version": 1})["version"]
    existing = load_drafts(as_of).get(_versioned(f"PR-{_mmdd(as_of)}-{comp}", version))
    if existing and existing["status"] != "PENDING":  # 이미 결정된 초안은 고치지 않는다 (이력 보존)
        return (f"차단됨: 발주 초안 {existing['draft_id']}는 이미 {existing['status']} 상태라 다시 만들지 않습니다"
                f" (희망 발주일 {existing.get('order_date', today)}).")
    d = make_purchase_request(as_of, comp, qty, order_date=order_date, note=note)
    arrival = expected_arrival(order_date, d["lead_time_days"])
    return (f"발주 요청서 초안 {d['draft_id']} (PENDING, {d['part_name']} {qty}개, 희망 발주일 {order_date},"
            f" 입고 소요 {d['lead_time_days']}일 → 예상 입고 {arrival}, 근거 {', '.join(d['evidence_ids']) or '-'})")


PLAN_TOOLS = [rank_maintenance, check_parts_stock, draft_work_order, draft_purchase_request]
