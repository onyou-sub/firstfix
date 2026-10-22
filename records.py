"""이력·보고서 화면용 기록 조회 (결과 파일만 읽는다. 에이전트·LLM을 import하지 않는다).

- decisions(): outputs/drafts/*.json 전체 -> 승인 내역 표 (처리자·처리 시각은 services.submit_decisions가 기록)
- report_days() / read_report(day): outputs/reports/handover_YYYYMMDD.md
- verification(days): outputs/predictions/*.json의 06시 고위험 vs 24시간 내 실제 고장 (evaluation.actual, 검증 전용)
- clear_day(as_of): 선택한 날짜의 outputs 결과 삭제 (설정 페이지의 데모 데이터 초기화)
"""

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from store import app_db

ROOT_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = ROOT_DIR / "outputs"
DRAFTS_DIR = OUTPUTS_DIR / "drafts"
REGISTERED_DIR = OUTPUTS_DIR / "registered"
PLANS_DIR = OUTPUTS_DIR / "plans"
REPORTS_DIR = OUTPUTS_DIR / "reports"
PREDICTIONS_DIR = OUTPUTS_DIR / "predictions"

STATUS_KO = {"APPROVED": "승인", "REJECTED": "거절", "SUPERSEDED": "대체됨", "PENDING": "대기"}
TYPE_KO = {"work_order": "작업 지시", "purchase_request": "발주"}
DECISION_COLUMNS = ["날짜", "초안 번호", "유형", "설비", "부품", "팀", "시간", "희망 발주일", "상태", "처리자", "처리 시각",
                    "재계획 버전",
                    "작업 상태", "완료 보고"]


def _day_of(path: Path) -> date | None:
    try:
        return datetime.strptime(path.stem[-8:], "%Y%m%d").date()
    except ValueError:
        return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def decisions() -> tuple[pd.DataFrame, dict[str, dict]]:
    """(승인 내역 표, {초안 번호: 원본 초안}). 표는 날짜·초안 번호 순."""
    rows, raw = [], {}
    orders = {w["wo_id"]: w for w in app_db.work_orders()}
    pos = {p["po_id"]: p for p in app_db.purchase_orders()}
    reports = {r["wo_id"]: r for r in app_db.work_reports()}
    for path in sorted(DRAFTS_DIR.glob("*.json")):
        day = _day_of(path)
        drafts = _read_json(path) or {}
        if day is None:
            continue
        for d in drafts.values():
            raw[d["draft_id"]] = d
            machines = [d["machine_id"]] if d.get("machine_id") is not None else d.get("for_machines", [])
            decided_at = d.get("decided_at") if d.get("decided_by") else None
            rows.append({
                "날짜": day, "초안 번호": d["draft_id"], "유형": TYPE_KO.get(d["type"], d["type"]),
                "설비": ", ".join(f"#{m}" for m in machines) or "-", "설비 목록": machines,
                "부품": d.get("comp") or "-", "팀": d.get("team") or "-",
                "시간": f"{d['start']}-{d['end']}" if d.get("start") else "-",
                "희망 발주일": (d.get("order_date") or str(day)) if d["type"] == "purchase_request" else "-",
                "상태": STATUS_KO.get(d["status"], d["status"]),
                "처리자": d.get("decided_by") or "-",
                "처리 시각": decided_at.replace("T", " ") if decided_at else "-",
                "재계획 버전": f"v{d.get('version', 1)}",
                "작업 상태": (app_db.WO_STATUS_KO[orders[d["draft_id"]]["status"]] if d["draft_id"] in orders
                          else {"REQUESTED": "발주 요청", "RECEIVED": "입고"}[pos[d["draft_id"]]["status"]]
                          if d["draft_id"] in pos else "-"),
                "완료 보고": (f"{reports[d['draft_id']]['actual_cause_type']} · {reports[d['draft_id']]['action']}"
                          if d["draft_id"] in reports else "-"),
            })
    df = pd.DataFrame(rows, columns=[*DECISION_COLUMNS, "설비 목록"])
    return df.sort_values(["날짜", "초안 번호"], ascending=[False, True]).reset_index(drop=True), raw


def filter_decisions(df: pd.DataFrame, start: date, end: date, status: str = "전체", kind: str = "전체",
                     machine: int | None = None) -> pd.DataFrame:
    out = df[(df["날짜"] >= start) & (df["날짜"] <= end)]
    if status != "전체":
        out = out[out["상태"] == status]
    if kind != "전체":
        out = out[out["유형"] == kind]
    if machine:
        out = out[out["설비 목록"].map(lambda ms: machine in ms)]
    return out.reset_index(drop=True)


def evidence_summaries(draft: dict) -> list[tuple[str, str]]:
    """초안의 근거 번호별 한 줄 요약 [(근거 번호, 요약)] (evidence 파일의 finding.summary)."""
    out = []
    day = pd.Timestamp(draft["as_of"]).strftime("%Y%m%d")
    for eid in draft.get("evidence_ids", []):
        machine = int(eid.split("-")[-1])
        ev = _read_json(ROOT_DIR / "evidence" / day / f"{machine}.json") or {}
        out.append((eid, (ev.get("finding") or {}).get("summary") or draft.get("summary") or "-"))
    return out


def report_days() -> list[date]:
    days = [_day_of(p) for p in REPORTS_DIR.glob("handover_*.md")]
    return sorted((d for d in days if d), reverse=True)


def read_report(day: date) -> str | None:
    path = REPORTS_DIR / f"handover_{day:%Y%m%d}.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def prediction_days() -> list[date]:
    return sorted(d for d in (_day_of(p) for p in PREDICTIONS_DIR.glob("*.json")) if d)


def verify_day(day: date) -> dict:
    """그날 06시 예측 고위험 설비 vs 그 뒤 24시간 실제 고장 (설비 단위)."""
    from evaluation.actual import actual_failures  # 검증 전용 (미래 데이터)

    data = _read_json(PREDICTIONS_DIR / f"{day:%Y%m%d}.json") or {}
    high = {int(r["machine_id"]) for r in data.get("rows", []) if r.get("high_risk")}
    as_of = data.get("as_of") or f"{day:%Y-%m-%d} 06:00:00"
    failed = {int(m) for m in actual_failures(as_of)["machine_id"]}
    return {"날짜": day, "고위험": len(high), "적중": len(high & failed),
            "헛경보": len(high - failed), "놓침": len(failed - high)}


def verification(start: date, end: date) -> pd.DataFrame:
    rows = [verify_day(d) for d in prediction_days() if start <= d <= end]
    return pd.DataFrame(rows, columns=["날짜", "고위험", "적중", "헛경보", "놓침"])


def clear_day(as_of: str) -> list[str]:
    """선택한 날짜의 outputs 결과(초안·등록·배정안 버전·리포트·예측)와 운영 DB 기록을 지운다. 지운 항목 목록."""
    day = pd.Timestamp(as_of).strftime("%Y%m%d")
    paths = [DRAFTS_DIR / f"{day}.json", REGISTERED_DIR / f"{day}.json", REPORTS_DIR / f"handover_{day}.md",
             PREDICTIONS_DIR / f"{day}.json", PLANS_DIR / f"{day}.json", *PLANS_DIR.glob(f"{day}_v*.json")]
    removed = []
    for path in paths:
        if path.exists():
            path.unlink()
            removed.append(str(path.relative_to(ROOT_DIR)))
    if n := app_db.clear_day(as_of):  # 운영 DB의 그날 작업 지시·발주·알림·완료 보고·교체·재고 변동
        removed.append(f"data/app.db 기록 {n}건")
    try:  # 그날 완료 보고의 현장 기록 색인 (Chroma)
        from store import field_notes

        if n := field_notes.delete_day(as_of):
            removed.append(f"현장 기록 색인 {n}건")
    except Exception:  # 색인을 열 수 없으면 (API 키 없음 등) 파일 결과만 지운다
        pass
    return removed


def work_report(wo_id: str) -> dict | None:
    """작업 지시의 완료 보고 (메모 전체 포함), 없으면 None."""
    return next((r for r in app_db.work_reports() if r["wo_id"] == wo_id), None)
