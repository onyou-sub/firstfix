"""5단계 확인: 1초 스트림 생성기(별도 프로세스, --tick 0.01) -> 실시간 감지 -> 경고 -> 긴급 점검 배정 (LLM 없음).

- 실제 data/app.db는 건드리지 않는다 (임시 app.db를 APP_DB_PATH로, 생성기 프로세스도 같은 환경변수를 받는다).
- 생성기는 --no-llm으로 띄워 조사 에이전트 대신 가짜 조사를 쓴다.
- 실행: uv run python scripts/test_stream_e2e.py
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

TMP_DIR = Path(tempfile.mkdtemp(prefix="fixfirst_stream_"))
os.environ["APP_DB_PATH"] = str(TMP_DIR / "app.db")
os.environ["URGENT_NOW_HM"] = "09:00"  # 긴급 출동 시각(실제 시계)을 고정해 결과가 실행 시각에 따라 바뀌지 않게

import services  # noqa: E402
from store import app_db  # noqa: E402

AS_OF = "2015-11-12 06:00:00"
DRIFT_M, STUCK_M = 45, 12
TICK = 0.01
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"\n       {detail}" if detail else ""))
    results.append((name, ok))


def wait(cond, timeout: float = 60.0, every: float = 0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if v := cond():
            return v
        time.sleep(every)
    return None


def alerts(machine_id: int) -> list[dict]:
    return [a for a in services.alert_history(limit=500) if a["machine_id"] == machine_id]


def stream_rows() -> list[dict]:
    return app_db._rows("SELECT * FROM live_telemetry WHERE batch_id LIKE 'STREAM-%'")


def main() -> None:
    try:
        app_db.set_current_day(AS_OF[:10], "admin")
        st = services.stream_start([DRIFT_M, STUCK_M], tick=TICK, no_llm=True, by="admin")
        check("생성기 프로세스 시작", st.get("spawned") and not st.get("error"), str(st.get("pid")))
        ok = wait(lambda: services.stream_status()["state"] == "실행 중", timeout=60)
        check("heartbeat -> 상태 실행 중", bool(ok), str(services.stream_status()))

        # 1) #45 진동 서서히 상승 -> 실시간 경고 -> UWO 배정 + 팀·관리자 알림
        services.stream_inject(DRIFT_M, "drift", "vibration", by="admin")
        a = wait(lambda: next((x for x in alerts(DRIFT_M) if x["status"] == "점검 배정"), None), timeout=90)
        check("#45 drift -> source 실시간 경고 -> 점검 배정",
              bool(a) and a["level"] == "경고" and a["source"] == "실시간" and a["comp"] == "bearing",
              str(a and (a["level"], a["source"], a["status"], a["message"])))
        wo = app_db.get_work_order(a["wo_id"]) if a else None
        team_notes = [n for n in app_db.notifications_for({"role": "worker", "team": wo and wo["team"], "id": "-"}, 50)
                      if wo and n["link_wo_id"] == wo["wo_id"]]
        admin_notes = [n for n in app_db.notifications_for({"role": "admin", "team": None, "id": "-"}, 50)
                       if wo and n["link_wo_id"] == wo["wo_id"]]
        check("UWO 배정 + 팀·관리자 알림", bool(wo) and wo["wo_id"].startswith("UWO-") and wo["urgent"] == 1
              and len(team_notes) == 1 and len(admin_notes) == 1,
              str(wo and (wo["wo_id"], wo["team"], wo["start"], wo["task_type"])))
        errors = app_db._rows("SELECT * FROM live_errors WHERE machine_id = ?", (DRIFT_M,))
        check("drift 20초 시점 관련 에러(vibration) 기준 시각으로 기록",
              any(e["error_id"] == "vibration" and e["datetime"] == AS_OF for e in errors), str(errors))

        # 2) 같은 설비 계속 이상 -> 경고 1건 유지
        time.sleep(300 * TICK + 1)
        check("같은 설비 계속 이상 -> 경고 1건 유지", len(alerts(DRIFT_M)) == 1, f"{len(alerts(DRIFT_M))}건")

        # 3) #12 센서 멈춤 -> 사람 확인 -> sensor_check 배정
        services.stream_inject(STUCK_M, "stuck", "pressure", by="admin")
        b = wait(lambda: next((x for x in alerts(STUCK_M) if x["status"] == "점검 배정"), None), timeout=90)
        wo2 = app_db.get_work_order(b["wo_id"]) if b else None
        check("#12 stuck -> 사람 확인 -> sensor_check 배정",
              bool(b) and b["level"] == "사람 확인" and b["source"] == "실시간" and "멈춤" in b["message"]
              and wo2 and wo2["task_type"] == "sensor_check", str(b and (b["level"], b["message"], wo2 and wo2["task_type"])))

        # 4) 1시간 요약: live_telemetry에 STREAM 행이 기준 시각으로
        rows = wait(lambda: [r for r in stream_rows() if r["machine_id"] == DRIFT_M], timeout=30)
        check("live_telemetry에 STREAM 요약 행 (datetime = 기준 시각)",
              bool(rows) and all(r["datetime"] == AS_OF and r["batch_id"] == "STREAM-20151112" for r in rows),
              str(rows and {k: rows[0][k] for k in ("batch_id", "datetime", "vibration")}))
        win = services.stream_window(DRIFT_M, 120)
        steps = win["sim_ts"].diff().dropna().dt.total_seconds()
        check("stream_window 120초 (차트용, 1초 간격)", len(win) == 120 and bool((steps == 1).all()),
              f"{len(win)}행 {win['sim_ts'].iloc[0]:%H:%M:%S}~{win['sim_ts'].iloc[-1]:%H:%M:%S}")
    finally:
        services.stream_stop(by="admin")
        stopped = wait(lambda: services.stream_status()["pid"] is None, timeout=200)
        check("정지 명령 -> 생성기 정리 후 종료, 상태 정지", bool(stopped) and services.stream_status()["state"] == "정지")
        out = services.clear_live_data()
        check("입력 데이터 지우기 -> STREAM 요약·원시값 삭제", not stream_rows() and out["stream"] > 0
              and not app_db._rows("SELECT 1 FROM live_stream LIMIT 1"), str(out))
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서, LLM 없음):
#   uv run python scripts/test_stream_e2e.py
