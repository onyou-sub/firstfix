"""화면(app.py)과 에이전트 사이 연결층. Streamlit 코드는 넣지 않는다.

- get_supervisor(): Supervisor 1회 생성 (app에서 @st.cache_resource로 감싼다)
- start_daily_check / get_job: "오늘 점검을 시작해줘"를 백그라운드 스레드로 실행하고 상태·진행 이벤트를 조회
- load_day(as_of): 결과 파일들을 화면용 데이터로 모은다 (목업 ss.result와 같은 키)
- submit_decisions / chat: 승인 결정 제출, 채팅
- verify(prev_as_of): 전날 예측 vs 실제 고장 (검증 전용, evaluation.actual만 미래를 읽음)
"""

import json
import sqlite3
import threading
import time
import traceback
from pathlib import Path

import pandas as pd
from langchain_core.callbacks import BaseCallbackHandler

import progress
from agents.supervisor import (
    CONTROL_BLOCK_MESSAGE, DAILY_MESSAGE, REPLAN_PREFIX, build_supervisor, chat_history as _chat_history,
    pending_requests, resume, run,
)
from config import COMP_LABEL_KO, TOOL_NAMES as T
from schemas import finding_compat
from evaluation.actual import actual_failures
from memory.store import recall, save_feedback as _save_feedback
from ml.predict import predict_df
from store import app_db, field_notes
import live_ops
from live_ops import (  # noqa: F401  입력 데이터 → 즉시 판단 → 긴급 점검 (화면은 services로 부른다)
    SCENARIO_KO, SCENARIOS, add_csv, add_manual, add_scenario, alert_history, alert_progress, clear_live_data,
    csv_example, deactivate_batch, dispatch_urgent, evaluate_machine, get_alert, handle_alert, list_batches,
    live_version, open_alerts, scenario_frames, scenario_preview, urgent_orders,
)
from stream.control import (  # noqa: F401  1초 실시간 스트림 제어 (생성기는 별도 프로세스 scripts/stream_sim.py)
    stream_add_machine, stream_card, stream_inject, stream_reset, stream_start, stream_status, stream_stop, stream_window,
)
from tools.data_tools import COMP_SENSOR, SENSOR_MEAN, sensor_series
from tools.plan_tools import (
    DRAFTS_DIR, PLANS_DIR, TASK_LABEL, _purchase_orders, _stock, expected_arrival, load_drafts, load_findings, load_plan,
    stamp_decisions, zero_stock_parts,
)
from tools.report_tools import report_path
from tools.supervisor_tools import PREDICTIONS_DIR, registered_path

ROOT_DIR = Path(__file__).resolve().parent
EVIDENCE_DIR = ROOT_DIR / "evidence"
DB_PATH = ROOT_DIR / "data" / "pdm.db"
START_MESSAGE = DAILY_MESSAGE
TYPE_KO = {"work_order": "작업 지시서", "purchase_request": "발주 요청"}

# Supervisor 도구 -> 화면에 보여줄 담당 (실행 로그용)
AGENT_OF_TOOL = {
    T["predict"]: "ML",
    T["investigate"]: "Investigator",
    T["planner"]: "Planner",
    T["data"]: "Data Agent",
    T["report"]: "Report Agent",
    T["submit_wo"]: "Supervisor",
    T["submit_pr"]: "Supervisor",
    T["replan"]: "Planner",
}

_jobs_lock = threading.Lock()
_JOBS: dict[str, dict] = {}


def _norm(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")


def _day(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y%m%d")


def get_supervisor():
    return build_supervisor()


# ---------------------------------------------------------------- 실행 이벤트 (실행 로그·단계 표시)
class SupervisorEvents(BaseCallbackHandler):
    """Supervisor 도구의 시작·끝을 job 이벤트로 남긴다 (하위 에이전트 도구는 무시)."""

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.names: dict = {}

    def on_tool_start(self, serialized, input_str, *, run_id=None, **kwargs) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name")
        if name in AGENT_OF_TOOL:
            self.names[run_id] = name
            _event(self.thread_id, AGENT_OF_TOOL[name], f"`{name}` 시작", stage=name)

    def on_tool_end(self, output, *, run_id=None, **kwargs) -> None:
        name = self.names.pop(run_id, None)
        if name:
            text = getattr(output, "content", output)
            first = str(text).strip().splitlines()[0][:120] if str(text).strip() else ""
            _event(self.thread_id, AGENT_OF_TOOL[name], f"`{name}` 완료 — {first}")


def _job(thread_id: str) -> dict:
    return _JOBS.setdefault(thread_id, {"running": False, "result": None, "error": None,
                                        "as_of": None, "stage": None, "events": []})


def _event(thread_id: str, agent: str, msg: str, stage: str | None = None) -> None:
    with _jobs_lock:
        job = _job(thread_id)
        job["events"].append({"time": time.strftime("%H:%M:%S"), "agent": agent, "msg": msg})
        if stage:
            job["stage"] = stage


# ---------------------------------------------------------------- 오늘 점검 (백그라운드)
def reset_day_outputs(as_of: str) -> None:
    """같은 날짜를 처음부터 다시 점검할 때 이전 초안·등록·리포트·배정표를 지운다 (생성물만 대상)."""
    day = _day(as_of)
    paths = [DRAFTS_DIR / f"{day}.json", registered_path(as_of), report_path(as_of), PLANS_DIR / f"{day}.json",
             *PLANS_DIR.glob(f"{day}_v*.json")]
    for path in paths:
        if path.exists():
            path.unlink()


def start_daily_check(as_of: str, thread_id: str, reset: bool = True) -> None:
    """백그라운드 스레드로 '오늘 점검을 시작해줘'를 실행하고 즉시 돌아온다."""
    as_of = _norm(as_of)
    with _jobs_lock:
        job = _job(thread_id)
        if job["running"]:
            raise RuntimeError("이미 점검이 진행 중입니다")
        job.update(running=True, result=None, error=None, as_of=as_of, stage="시작", events=[])
    if reset:
        reset_day_outputs(as_of)
    progress.clear(as_of)
    _event(thread_id, "Supervisor", f"버튼 메시지 수신: '{START_MESSAGE}'")

    def work():
        try:
            result = run(START_MESSAGE, as_of, thread_id, callbacks=[SupervisorEvents(thread_id)], daily=True)
            if result["status"] == "pending":
                _event(thread_id, "Guard", f"submit_* 호출 → HITL로 멈춤 (승인 대기 {len(result['requests'])}건)",
                       stage="승인 대기")
            else:
                _event(thread_id, "Supervisor", "점검 완료", stage="완료")
            with _jobs_lock:
                _job(thread_id).update(result=result)
        except Exception as e:
            with _jobs_lock:
                _job(thread_id).update(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc())
            _event(thread_id, "Supervisor", f"오류: {type(e).__name__}: {e}", stage="오류")
        finally:
            with _jobs_lock:
                _job(thread_id)["running"] = False

    threading.Thread(target=work, daemon=True, name=f"daily-check-{thread_id}").start()


def get_job(thread_id: str) -> dict:
    """{"running", "result"(pack_result 또는 None), "error", "stage", "events", "progress"}"""
    with _jobs_lock:
        job = dict(_job(thread_id))
        job["events"] = list(job["events"])
    job["progress"] = progress.get(job["as_of"]) if job["as_of"] else {}
    return job


# ---------------------------------------------------------------- 화면용 데이터
def _criticality() -> dict[int, str]:
    with sqlite3.connect(DB_PATH) as conn:
        return dict(conn.execute("SELECT machine_id, criticality FROM criticality").fetchall())


def _manual_excerpt(tool_calls: list[dict], source: str) -> str:
    """search_manual 결과에서 출처와 같은 절의 본문 앞부분 (절 번호까지 일치 > 같은 파일 > 첫 결과)."""
    blocks = []
    for c in tool_calls:
        if c["name"] == "search_manual" and c.get("output"):
            for b in c["output"].split("\n\n[출처: "):
                head, _, body = b.partition("\n")
                blocks.append((head, body.strip()))
    if not blocks:
        return ""
    exact = " ".join(source.split()[:2])  # "manual_pump.md §3.1"
    file = source.split()[0] if source else ""
    for want in [exact, file]:
        hit = next((body for head, body in blocks if want and want in head), None)
        if hit:
            return hit[:600]
    return blocks[0][1][:600]


def _finding_view(ev: dict, crit: dict[int, str]) -> dict:
    f = finding_compat(ev["finding"])  # 새 필드가 없는 옛 근거 파일도 같은 형식으로
    comp, sensor = f["comp"], COMP_SENSOR[f["comp"]]
    calls = ev.get("tool_calls", [])
    days = (ev.get("rule_check") or {}).get("days_since")
    return {
        "machine_id": f["machine_id"], "comp": comp, "comp_name": COMP_LABEL_KO[comp], "sensor": sensor,
        "prob": float(ev["input"]["prob"]), "criticality": crit.get(f["machine_id"], "-"), "label": f["label"],
        "summary": f["summary"], "key_evidence": f["key_evidence"], "checklist": f["checklist"],
        "days_since": None if days is None else round(days, 1), "base": SENSOR_MEAN[sensor],
        "tool_calls": [(c["name"], json.dumps(c["input"], ensure_ascii=False), c.get("output") or "") for c in calls],
        "manual": (f.get("manual_source") or "", _manual_excerpt(calls, f.get("manual_source") or "")),
        "evidence_id": ev["evidence_id"], "rule_override": ev.get("rule_override", False),
        "grounding_check": ev.get("grounding_check"),
        **{k: f[k] for k in ("action_summary", "anomaly_summary", "hypotheses", "steps", "field_tips", "est_minutes",
                             "parts_tools")},
    }


def load_day(as_of: str) -> dict:
    """결과 파일 -> {"pred", "findings", "plan", "drafts"} (목업 ss.result와 같은 키). 예측 전이면 pred=None."""
    as_of = _norm(as_of)
    crit = _criticality()

    pred_path = PREDICTIONS_DIR / f"{_day(as_of)}.json"
    pred = None
    if pred_path.exists():
        rows = json.loads(pred_path.read_text(encoding="utf-8"))["rows"]
        pred = pd.DataFrame(rows).rename(columns={"top_comp": "comp", "top_prob": "prob"})
        pred["criticality"] = pred["machine_id"].map(crit)
        pred = pred.sort_values("machine_id").reset_index(drop=True)

    findings = []
    for f in load_findings(as_of):
        path = EVIDENCE_DIR / _day(as_of) / f"{f['machine_id']}.json"
        view = _finding_view(json.loads(path.read_text(encoding="utf-8")), crit)
        findings.append({**view, "notes": machine_notes(f["machine_id"], as_of),
                         "reports": machine_reports(f["machine_id"], as_of)})

    plan_file = load_plan(as_of) or {"rows": [], "version": 1}
    plan_rows = plan_file["rows"]
    plan = pd.DataFrame([{
        "machine_id": r["machine_id"], "comp": r["comp"], "comp_name": COMP_LABEL_KO[r["comp"]], "prob": r["prob"],
        "criticality": r["criticality"], "stock": r["stock_qty"], "score": r["score"], "label": r["label"],
        "slot": r["slot"], "evidence_id": r["evidence_id"],
        "team": r.get("team"), "start": r.get("start"), "end": r.get("end"),
        "task_type": r.get("task_type"), "task": TASK_LABEL.get(r.get("task_type"), "-"),
        "duration_min": r.get("duration_min"), "skill": r.get("skill"), "skill_match": r.get("skill_match"),
        "needs_purchase": r.get("needs_purchase", False), "reason": r.get("reason") or "-",
    } for r in plan_rows])

    actions = {f["machine_id"]: f["action_summary"] for f in findings}
    drafts = []
    for d in sorted(load_drafts(as_of).values(), key=lambda d: d["draft_id"]):
        order = notice = None  # 발주만: 희망 발주일, 오늘보다 늦으면 예상 입고 안내
        if d["type"] == "work_order":
            title = f"설비 #{d['machine_id']} {COMP_LABEL_KO[d['comp']]}({d['comp']}) 점검 — {d['slot']}"
            body = "체크리스트: " + " ".join(f"{i}) {c}" for i, c in enumerate(d["checklist"][:8], 1))
            card = {"machine": f"#{d['machine_id']} {COMP_LABEL_KO[d['comp']]}",
                    "task": TASK_LABEL.get(d.get("task_type"), "작업"),
                    "when": f"{d['team']} {d['start']}-{d['end']}" if d.get("team") else d.get("slot") or "-",
                    "todo": actions.get(d["machine_id"], "-"),
                    # 승인 탭 구역: 팀이 있으면 팀, 일정에 못 들어간 작업(내일 우선 등)은 미배정
                    "team": d.get("team") or None, "start": d.get("start") or None,
                    "group": d.get("team") or "미배정"}
        else:
            title = f"{COMP_LABEL_KO[d['comp']]}({d['comp']}) {d['qty']}개 발주 — 재고 {d['current_qty']}"
            order = d.get("order_date") or as_of[:10]
            body = (f"희망 발주일 {order} · 입고 소요 {d['lead_time_days']}일 · 대상 설비 "
                    f"{', '.join(f'#{m}' for m in d['for_machines']) or '-'}")
            arrival = expected_arrival(order, d["lead_time_days"])[5:]
            if order > as_of[:10]:  # 오늘보다 늦게 발주하면 입고도 그만큼 늦어진다
                notice = f"리드타임 {d['lead_time_days']}일 → 예상 입고 {arrival}"
            card = {"machine": (", ".join(f"#{m}" for m in d["for_machines"]) or "-") + f" {COMP_LABEL_KO[d['comp']]}",
                    "task": f"발주 {d['part_name']} {d['qty']}개", "when": f"희망 발주일 {order}",
                    "todo": notice or f"오늘 발주 → 예상 입고 {arrival}",
                    "team": None, "start": None, "group": "발주"}
        drafts.append({"id": d["draft_id"], "type": TYPE_KO[d["type"]], "title": title,
                       "body": f"{body} · 근거 번호 {', '.join(d['evidence_ids'])}", "status": d["status"],
                       "version": d.get("version", 1), "order_date": order, "notice": notice, **card})
    prev_v, new_v = plan_file.get("previous_version", "-"), plan_file["version"]
    diff = pd.DataFrame([{
        "설비": d["machine_id"], "부품": d["comp"], "작업": d.get("task_type", "-"),
        f"이전 팀 (v{prev_v})": d.get("before_team", "-"), f"이전 시간 (v{prev_v})": d.get("before_time", "-"),
        f"새 팀 (v{new_v})": d.get("after_team", "-"), f"새 시간 (v{new_v})": d.get("after_time", "-"),
        f"이전 배정 (v{prev_v})": d["before"], f"새 배정 (v{new_v})": d["after"], "변경": d["changed"],
    } for d in plan_file.get("diff", [])])
    return {"pred": pred, "findings": findings, "plan": plan, "drafts": drafts,
            "plan_version": plan_file["version"], "plan_reason": plan_file.get("reason"),
            "plan_note": (plan_file.get("constraints") or {}).get("note"), "diff": diff,
            "utilization": plan_file.get("utilization") or {}, "tomorrow": plan_file.get("tomorrow") or [],
            "team_windows": plan_file.get("team_windows") or {}}


WORKER_REPORT_PREFIX = "작업자 보고("  # app_db.feedback_sentence 문장 머리


def machine_notes(machine_id: int, as_of: str, limit: int = 5) -> list[str]:
    """과거 현장 메모: 장기 기억의 엔지니어 피드백 (기준 시각까지, 최신순).
    작업자 완료 보고는 메모가 잘린 요약 문장이라 빼고, machine_reports(메모 전체)로 따로 보여 준다."""
    notes = []
    for rec in recall(machine_id, as_of, limit=10, include_same_time=True):
        for fb in reversed(rec.get("feedback", [])):
            if not fb["text"].startswith(WORKER_REPORT_PREFIX):
                notes.append(f"{rec['as_of'][:10]} {fb['text']}")
    return notes[:limit]


def machine_reports(machine_id: int, as_of: str) -> list[dict]:
    """이 설비의 작업자 완료 보고 전체 (작업 지시 날짜가 기준 날짜 이하, 최신순, 메모를 자르지 않음)."""
    day = pd.Timestamp(as_of).strftime("%Y-%m-%d")
    return [r for r in app_db.work_reports() if r["machine_id"] == int(machine_id) and r["as_of_date"] <= day]


def sensor_trend(machine_id: int, sensor: str, hours: int, as_of: str) -> pd.DataFrame:
    """차트용 센서 시계열 (as_of 이하만)."""
    return sensor_series(machine_id, sensor, hours, _norm(as_of))


def get_report(as_of: str) -> str | None:
    path = report_path(as_of)
    return path.read_text(encoding="utf-8") if path.exists() else None


def save_feedback(machine_id: int, as_of: str, text: str, engineer_id: str = "eng01") -> dict:
    if not text.strip():
        raise ValueError("피드백 내용을 입력하세요")
    return _save_feedback(machine_id, _norm(as_of), text.strip(), engineer_id)


# ---------------------------------------------------------------- 승인·채팅
def submit_decisions(thread_id: str, as_of: str, decisions: list[str], decided_by: str | None = None) -> dict:
    """대기 중인 승인 요청에 결정(요청 순서대로 "approve"/"reject")을 제출하고 이어서 실행한다.
    결정한 초안에는 처리자(decided_by, 로그인 아이디)와 처리 시각(decided_at, 실제 현재 시각)을 남긴다."""
    as_of = _norm(as_of)
    _event(thread_id, "Guard", "결정 제출: " + ", ".join(decisions))
    ids = [r["draft_id"] for r in pending_requests(thread_id)]
    result = resume(thread_id, as_of, decisions, callbacks=[SupervisorEvents(thread_id)], engineer_id=decided_by)
    if decided_by:
        stamp_decisions(as_of, ids, decided_by)
    with _jobs_lock:
        _job(thread_id).update(result=result)
    return result


def request_replan(thread_id: str, as_of: str, reason: str, decided_by: str | None = None) -> dict:
    """거절 사유로 재계획. 승인 대기 중이면 모든 요청을 사유와 함께 거절(resume), 아니면 채팅으로 재계획 요청."""
    as_of, reason = _norm(as_of), reason.strip()
    if not reason:
        raise ValueError("재계획 사유를 입력하세요")
    _event(thread_id, "Guard", f"재계획 요청: {reason}")
    ids = [r["draft_id"] for r in pending_requests(thread_id)]
    if ids:
        result = resume(thread_id, as_of, [], reason=reason, callbacks=[SupervisorEvents(thread_id)])
        if decided_by:
            stamp_decisions(as_of, ids, decided_by)
    else:
        result = run(REPLAN_PREFIX + reason, as_of, thread_id,
                     callbacks=[SupervisorEvents(thread_id)])
    with _jobs_lock:
        _job(thread_id).update(result=result)
    return result


def has_thread(thread_id: str) -> bool:
    """체크포인터에 이 스레드의 대화가 있는지 (재접속 시 화면 복원 여부)."""
    return build_supervisor().get_state({"configurable": {"thread_id": thread_id}}).created_at is not None


def chat_history(thread_id: str, as_of: str) -> list[dict]:
    """체크포인터의 대화 기록 -> 채팅 화면 메시지 [{"role", "content"}]."""
    return _chat_history(thread_id, _norm(as_of))


def chat(thread_id: str, as_of: str, text: str) -> dict:
    """{"answer", "used_tools", "status", "blocked", "requests"(pending일 때)}"""
    result = run(text, _norm(as_of), thread_id, callbacks=[SupervisorEvents(thread_id)])
    if result["status"] == "pending":
        with _jobs_lock:
            _job(thread_id).update(result=result)
        return {"answer": result.get("answer") or f"승인이 필요한 요청 {len(result['requests'])}건이 생겼습니다.",
                "used_tools": [],
                "status": "pending", "blocked": False, "requests": result["requests"]}
    return {"answer": result["answer"], "used_tools": result["used_tools"], "status": "done",
            "blocked": result["answer"].strip() == CONTROL_BLOCK_MESSAGE and not result["used_tools"]}


# ---------------------------------------------------------------- 전날 검증
def verify(prev_as_of: str) -> pd.DataFrame:
    """전날 06시 예측 고위험 vs 그 뒤 24시간 실제 고장. 열: 설비, 예측, 실제, 판정"""
    prev_as_of = _norm(prev_as_of)
    pred_path = PREDICTIONS_DIR / f"{_day(prev_as_of)}.json"
    if pred_path.exists():
        pred = pd.DataFrame(json.loads(pred_path.read_text(encoding="utf-8"))["rows"])
    else:
        pred = predict_df(prev_as_of)
    high = {int(r.machine_id): r.top_comp for r in pred[pred["high_risk"]].itertuples()}
    actual = actual_failures(prev_as_of).groupby("machine_id")["comp"].apply(lambda s: ", ".join(sorted(set(s))))
    rows = []
    for m in sorted(set(high) | set(actual.index)):
        hit, failed = m in high, m in actual.index
        rows.append({
            "설비": m,
            "예측": f"고위험 ({high[m]})" if hit else "-",
            "실제": f"고장 ({actual[m]})" if failed else "-",
            "판정": "적중" if hit and failed else "헛경보" if hit else "놓침",
        })
    return pd.DataFrame(rows, columns=["설비", "예측", "실제", "판정"])


# ---------------------------------------------------------------- 작업자: 작업 시작·완료 보고 (폐쇄 루프)
def _own_team_order(wo_id: str, user: dict) -> dict:
    """작업 지시를 읽고, 같은 팀 작업자만 처리할 수 있는지 서버 측에서 확인한다."""
    wo = app_db.get_work_order(wo_id)
    if wo is None:
        raise ValueError(f"작업 지시 {wo_id}를 찾을 수 없습니다")
    if user.get("role") != "worker" or not user.get("team") or user.get("team") != wo["team"]:
        raise PermissionError(f"{wo['team']} 작업은 {wo['team']} 작업자만 처리할 수 있습니다")
    return wo


def start_work(wo_id: str, user: dict) -> dict:
    """[작업 시작] ASSIGNED -> IN_PROGRESS."""
    wo = _own_team_order(wo_id, user)
    if wo["status"] != "ASSIGNED":
        raise ValueError(f"이미 {app_db.WO_STATUS_KO[wo['status']]} 상태인 작업입니다")
    app_db.set_started(wo_id)
    live_ops.on_work_started(wo_id)  # 긴급 점검이면 경고 상태 "출동"
    return app_db.get_work_order(wo_id)


STEP_RESULTS = ["정상", "이상 발견", "확인 못함"]
MIN_MEMO_CHARS = 30
MEMO_GUIDE = ("- 현장에서 발견한 상태 (소리, 냄새, 열, 외관, 측정값)\n- 원인이라고 판단한 근거\n"
              "- 조치 후 상태 (재가동 후 진동·전압 등)\n- 다음 점검 때 주의할 점")


def work_steps(wo: dict) -> list[dict]:
    """완료 보고에서 결과를 적을 점검 단계 [{"action", "criterion"}].
    작업 지시에 steps가 있으면 그것, 없으면 그 설비 조사 결과의 steps, 그것도 없으면 작업 지시 체크리스트."""
    steps = wo.get("steps") or evidence_brief(wo["as_of_date"], wo["machine_id"]).get("steps") or []
    if steps:
        return [{"action": st["action"], "criterion": st.get("criterion", "")} for st in steps]
    return [{"action": c, "criterion": ""} for c in wo.get("checklist") or []]


def complete_work(wo_id: str, user: dict, actual_cause_type: str, actual_comp: str, action: str,
                  duration_min: int, parts_used: int, memo: str, deviation: str = "",
                  step_results: list[dict] | None = None) -> dict:
    """[완료 보고] 저장하고 다음 판단에 반영한다.

    memo: 상세 메모 (필수, 30자 이상). 측정값·관찰도 메모에 적는다 (화면에서는 단계별 결과 입력을 받지 않음).
    step_results: (선택) work_steps(wo) 순서대로 [{"result": 정상/이상 발견/확인 못함, "observation": 측정값·관찰}].
        주면 단계 수가 맞아야 하고, 모두 "확인 못함"이면 거부.
    - work_reports 저장 (step_results JSON, memo, 지시와 달랐던 점), work_orders.status = DONE
    - 현장 기록 색인(field_notes): "이상 발견" 단계와 측정값, 메모 전체 -> 다음 조사의 search_field_notes
    - 장기 기억: save_feedback(설비, 그날 기준 시각, "작업자 보고(2팀): ... 이상 발견: ..., 메모: ...")
    - 부품 교체면 maint_log (datetime = 기준 날짜 + 작업 종료 시각) -> get_maintenance_history·최근 교체 규칙에 반영
    - 사용 부품 수량만큼 stock_ledger -qty -> check_parts_stock·배정 재고에 반영
    - 관리자 알림 "완료 보고: #45 (2팀) — 예측과 같음"
    """
    wo = _own_team_order(wo_id, user)
    if wo["status"] != "IN_PROGRESS":
        raise ValueError("작업 시작 후에만 완료 보고를 할 수 있습니다" if wo["status"] == "ASSIGNED"
                         else "이미 완료 보고된 작업입니다")
    if actual_cause_type not in app_db.CAUSE_TYPES:
        raise ValueError(f"실제 원인 유형은 {app_db.CAUSE_TYPES} 중 하나여야 합니다")
    if action not in app_db.ACTIONS:
        raise ValueError(f"조치 내용은 {app_db.ACTIONS} 중 하나여야 합니다")
    if actual_comp not in COMP_LABEL_KO:
        raise ValueError(f"실제 고장 부품은 {list(COMP_LABEL_KO)} 중 하나여야 합니다")
    if not 1 <= int(duration_min) <= 24 * 60:
        raise ValueError("실제 소요 시간은 1~1440분이어야 합니다")
    if not 0 <= int(parts_used) <= 20:
        raise ValueError("사용 부품 수량은 0~20개여야 합니다")
    step_results = step_results or []
    steps = work_steps(wo) if step_results else []
    if step_results and len(step_results) != len(steps):
        raise ValueError(f"점검 단계 {len(steps)}개마다 결과를 하나씩 적어야 합니다 (받은 결과 {len(step_results)}개)")
    if any(r.get("result") not in STEP_RESULTS for r in step_results):
        raise ValueError(f"단계 결과는 {STEP_RESULTS} 중에서 골라야 합니다")
    if step_results and all(r["result"] == "확인 못함" for r in step_results):
        raise ValueError("모든 단계가 '확인 못함'입니다. 확인한 단계의 결과를 적어 주세요")
    memo = (memo or "").strip()
    if len(memo) < MIN_MEMO_CHARS:
        raise ValueError(f"상세 메모를 {MIN_MEMO_CHARS}자 이상 적어 주세요 (지금 {len(memo)}자). "
                         "발견한 상태, 원인 판단 근거, 조치 후 상태, 다음 점검 주의점을 적으면 다음 조사에 쓰입니다")

    results = [{"action": st["action"], "criterion": st["criterion"], "result": r["result"],
                "observation": (r.get("observation") or "").strip()} for st, r in zip(steps, step_results)]
    report = {"actual_cause_type": actual_cause_type, "actual_comp": actual_comp, "action": action,
              "duration_min": int(duration_min), "parts_used": int(parts_used), "checklist_done": [],
              "step_results": results, "memo": memo, "deviation": (deviation or "").strip(),
              "reported_by": user["id"], "team": wo["team"]}
    end = wo["end"] or "18:00"
    maint_dt = f"{wo['as_of_date']} {end}:00" if action == "부품 교체" else None
    report_id = app_db.save_report(wo, report, maint_dt, actual_comp)
    live_ops.on_work_done(wo_id)  # 긴급 점검이면 경고 상태 "점검 완료"

    try:  # 색인 실패(임베딩 API 오류 등)가 완료 보고 자체를 막지 않게
        field_notes.index_report(wo, report)
        indexed, index_error = True, None
    except Exception as e:
        indexed, index_error = False, f"{type(e).__name__}: {e}"

    stock_out = _stock_out_alert(wo, report)

    sentence = app_db.feedback_sentence(report)
    _save_feedback(wo["machine_id"], app_db.report_as_of(wo["as_of_date"]), sentence, user["id"])
    app_db.notify(title=f"완료 보고: #{wo['machine_id']} ({wo['team']}) — {actual_cause_type}",
                  body=sentence, to_role="admin", link_wo_id=wo_id, as_of_date=wo["as_of_date"], kind="done",
                  head=f"#{wo['machine_id']} {COMP_LABEL_KO.get(wo['comp'], wo['comp'])}",
                  line=f"{actual_cause_type} · {wo['team']}")
    return {"report_id": report_id, "sentence": sentence, "maint_datetime": maint_dt, "indexed": indexed,
            "index_error": index_error, "stock_out": stock_out, "work_order": app_db.get_work_order(wo_id)}


def _stock_out_alert(wo: dict, report: dict) -> bool:
    """완료 보고로 부품을 쓴 뒤 재고가 0이 되면 관리자에게 바로 알린다 (발주 요청은 다음 점검에서 자동으로 올라감)."""
    if int(report["parts_used"]) <= 0:
        return False
    comp = report["actual_comp"]
    used_at = f"{wo['as_of_date']} {wo['end'] or '18:00'}:00"
    if int(_stock(used_at)[comp]["qty"]) > 0:
        return False
    if _purchase_orders(comp, used_at)[0] is not None:  # 이미 발주가 진행 중(입고 대기)이면 새 알림을 만들지 않는다
        return True
    next_day = (pd.Timestamp(wo["as_of_date"]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    app_db.notify(title=f"재고 0: {COMP_LABEL_KO[comp]} ({comp})",
                  body=f"#{wo['machine_id']} 작업({wo['wo_id']})에서 마지막 부품을 썼습니다."
                       f" 다음 점검({next_day})에서 보충 발주 요청이 자동으로 올라갑니다.",
                  to_role="admin", link_wo_id=wo["wo_id"], as_of_date=wo["as_of_date"], kind="notice",
                  head=f"{COMP_LABEL_KO[comp]} 재고 0", line=f"#{wo['machine_id']} 작업에서 마지막 부품 사용 · {next_day} 보충 발주")
    return True


STOCK_ACK_KEY = "stock_ack"  # app_state: {comp: 확인한 상태 키} — 상태가 바뀌면 다시 보인다


def stock_alerts(as_of: str) -> list[dict]:
    """대시보드용 재고 0 부품 상태 [{"comp", "part_name", "status", "level", "key", "action_needed", "acked"}].

    level: warning/info/error. key = 상태 키 ("none", "pending:{draft_id}", "rejected:{draft_id}",
    "approved:{draft_id}", "in_transit:{po_id}", 입고 예정일이 지난 발주가 있으면 "|overdue:{po_id,...}"가 붙음).
    action_needed: 조치가 필요한가 (요청 없음 / 거절 / 승인 대기 / 입고 예정일 지남). 발주 진행 중·오늘 승인됨은 False.
    acked: 관리자가 이 상태 그대로를 [확인]했나 (stock_ack). 화면은 action_needed이고 acked가 아닌 것만 보여 준다."""
    as_of = _norm(as_of)
    drafts = load_drafts(as_of)
    acks = app_db.get_state(STOCK_ACK_KEY, {}) or {}
    out = []
    for z in zero_stock_parts(as_of):
        comp = z["comp"]
        prs = sorted((d for d in drafts.values() if d["type"] == "purchase_request" and d["comp"] == comp
                      and d["status"] != "SUPERSEDED"), key=lambda d: d.get("version", 1))
        pr = prs[-1] if prs else None
        if z["in_transit"]:
            status, level = (f"발주 진행 중 ({z['in_transit']['po_id']}, 입고 예정 {z['in_transit']['expected_arrival']})",
                             "info")
            key, action = f"in_transit:{z['in_transit']['po_id']}", False
        elif pr and pr["status"] == "PENDING":
            status, level = f"보충 발주 승인 대기 ({pr['draft_id']}, {pr['qty']}개)", "warning"
            key, action = f"pending:{pr['draft_id']}", True
        elif pr and pr["status"] == "APPROVED":
            status, level = f"오늘 발주 승인됨 ({pr['draft_id']}, 입고 {z['lead_time_days']}일 소요)", "info"
            key, action = f"approved:{pr['draft_id']}", False
        elif pr and pr["status"] == "REJECTED":
            status, level = f"발주 거절됨 ({pr['draft_id']}) - 재고 0 그대로", "error"
            key, action = f"rejected:{pr['draft_id']}", True
        else:
            status, level = "발주 요청 없음 - 오늘 점검을 실행하면 보충 발주가 올라갑니다", "error"
            key, action = "none", True
        if z["overdue"]:  # 발주 진행 중이어도 입고 예정일이 지났으면 확인이 필요하다
            status += " · 입고 예정일이 지난 발주: " + ", ".join(
                f"{o['po_id']}({o['expected_arrival']})" for o in z["overdue"]) + " (입고 처리 확인)"
            key += "|overdue:" + ",".join(o["po_id"] for o in z["overdue"])
            level = "warning" if level == "info" else level
            action = True
        out.append({"comp": comp, "part_name": z["part_name"], "status": status, "level": level, "key": key,
                    "action_needed": action, "acked": acks.get(comp) == key})
    return out


def stock_card(as_of: str) -> dict:
    """대시보드 "재고 0 부품" 카드에 그릴 것: {"show": 조치가 필요하고 아직 확인하지 않은 항목, "in_progress": 발주 진행 중
    (입고 대기·오늘 승인됨) 건수}. show가 비면 카드를 그리지 않는다."""
    alerts = stock_alerts(as_of)
    return {"show": [a for a in alerts if a["action_needed"] and not a["acked"]],
            "in_progress": sum(not a["action_needed"] for a in alerts)}


def ack_stock_alert(comp: str, key: str, by: str | None = None) -> None:
    """재고 0 항목 [확인]: 그 부품의 지금 상태 키를 기록한다 (같은 상태면 계속 숨김, 상태가 바뀌면 다시 보임)."""
    acks = dict(app_db.get_state(STOCK_ACK_KEY, {}) or {})
    acks[comp] = key
    app_db.set_state(STOCK_ACK_KEY, acks, by)


ORGANIZE_PROMPT = """당신은 설비 정비 작업자의 완료 보고 메모를 정리하는 도우미입니다.
작업자가 쓴 메모와 단계별 결과를 아래 4개 항목으로만 정리합니다.

- 현장에서 발견한 상태 (소리, 냄새, 열, 외관):
- 원인이라고 판단한 근거:
- 조치 후 상태 (재가동 후 진동·전압 등):
- 다음 점검 때 주의할 점:

[반드시 지킬 것]
- 작업자가 쓰지 않은 사실, 수치, 원인, 조치를 절대 추가하지 않습니다. 추측하지 않습니다.
- 메모와 단계별 결과에 있는 내용만 옮기고, 문장만 짧게 다듬습니다. 수치는 그대로 씁니다.
- 해당 내용이 없는 항목은 "(기록 없음 - 작업자가 채워 주세요)"라고 씁니다.
- 위 4줄 형식 그대로, 다른 말(인사, 설명)은 붙이지 않습니다."""


def organize_memo(memo: str, step_results: list[dict] | None = None) -> str:
    """[메모 정리 도움] 짧은 메모를 4개 항목 형식의 초안으로 (작업자가 확인·수정 후 제출). LLM 호출."""
    from dotenv import load_dotenv
    from langchain_openai import ChatOpenAI

    if not (memo or "").strip() and not any((r.get("observation") or "").strip() for r in step_results or []):
        raise ValueError("정리할 메모나 측정값이 없습니다. 먼저 짧게라도 적어 주세요")
    load_dotenv(ROOT_DIR / ".env")
    steps = "\n".join(f"- {r.get('action', '')}: {r.get('result') or '(미선택)'}"
                      + (f", {r['observation']}" if r.get("observation") else "") for r in step_results or [])
    out = ChatOpenAI(model="gpt-5.4-mini", temperature=0).invoke([
        ("system", ORGANIZE_PROMPT),
        ("user", f"[작업자 메모]\n{memo.strip() or '(없음)'}\n\n[단계별 결과]\n{steps or '(없음)'}"),
    ])
    return out.text.strip()


def finding_view(as_of_date: str, machine_id: int) -> dict | None:
    """작업자 작업 카드용 조사 내용 (관리자 설비별 조사 탭과 같은 형식 + 과거 현장 메모). 근거 파일이 없으면 None."""
    path = EVIDENCE_DIR / pd.Timestamp(as_of_date).strftime("%Y%m%d") / f"{machine_id}.json"
    if not path.exists():
        return None
    view = _finding_view(json.loads(path.read_text(encoding="utf-8")), _criticality())
    as_of = f"{pd.Timestamp(as_of_date):%Y-%m-%d} 06:00:00"
    return {**view, "notes": machine_notes(machine_id, as_of), "reports": machine_reports(machine_id, as_of)}


def evidence_brief(as_of_date: str, machine_id: int) -> dict:
    """작업자 화면용 조사 결론: summary와 key_evidence만 (근거 파일)."""
    path = EVIDENCE_DIR / pd.Timestamp(as_of_date).strftime("%Y%m%d") / f"{machine_id}.json"
    if not path.exists():
        return {"summary": "", "key_evidence": [], "label": None, "action_summary": "", "steps": []}
    f = finding_compat(json.loads(path.read_text(encoding="utf-8")).get("finding", {}))
    return {"summary": f.get("summary", ""), "key_evidence": f.get("key_evidence", []), "label": f.get("label"),
            "action_summary": f["action_summary"], "steps": f["steps"]}
