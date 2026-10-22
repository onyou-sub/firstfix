"""실시간 스트림 제어 (화면은 services를 통해 부른다. 생성기 루프는 별도 프로세스 scripts/stream_sim.py).

명령과 상태는 app.db app_state로 주고받는다:
  stream_running(bool), stream_machines(list[int]), stream_inject([{machine_id, kind, sensor, started_at}]),
  stream_heartbeat(마지막 tick 실제 시각), stream_pid, stream_started_at
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from store import app_db
from stream import SENSORS
from stream.generator import KINDS

ROOT_DIR = Path(__file__).resolve().parent.parent
SIM_SCRIPT = ROOT_DIR / "scripts" / "stream_sim.py"
LOG_PATH = ROOT_DIR / "outputs" / "stream_sim.log"
MAX_MACHINES = 5
STALE_SECONDS = 5  # heartbeat가 이보다 오래되면 "응답 없음"
STARTUP_SECONDS = 20  # 막 띄운 생성기가 첫 heartbeat를 쓰기까지 기다리는 시간 ("시작 중")


def _age(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return None


def stream_status() -> dict:
    """{"running", "heartbeat_age"(초 또는 None), "machines", "injections", "pid", "state": 실행 중/시작 중/응답 없음/정지}"""
    running = bool(app_db.get_state("stream_running", False))
    age = _age(app_db.get_state("stream_heartbeat"))
    started = _age(app_db.get_state("stream_started_at"))
    if not running:
        state = "정지"
    elif age is not None and age <= STALE_SECONDS:
        state = "실행 중"
    elif age is None and started is not None and started <= STARTUP_SECONDS:
        state = "시작 중"
    else:
        state = "응답 없음"
    return {"running": running, "heartbeat_age": age, "machines": app_db.get_state("stream_machines", []),
            "injections": app_db.get_state("stream_inject", []), "pid": app_db.get_state("stream_pid"),
            "state": state}


def stream_start(machine_ids: list[int], tick: float = 1.0, no_llm: bool = False, by: str | None = None) -> dict:
    """대상 설비(최대 5대)를 정하고 실행 명령을 남긴다. 생성기가 없으면(heartbeat 5초 이상 없음) 새로 띄운다.
    반환: stream_status() + {"spawned": bool} 또는 {"error"}"""
    ids = sorted({int(m) for m in machine_ids})
    if not ids or len(ids) > MAX_MACHINES or any(not 1 <= m <= 100 for m in ids):
        return {"error": f"설비는 1~{MAX_MACHINES}대, 번호 1~100이어야 합니다 (받은 값: {machine_ids})"}
    app_db.set_state("stream_machines", ids, by)
    app_db.set_state("stream_running", True, by)
    age = _age(app_db.get_state("stream_heartbeat"))
    spawned = False
    if age is None or age > STALE_SECONDS:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, str(SIM_SCRIPT), "--tick", str(tick)] + (["--no-llm"] if no_llm else [])
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with open(LOG_PATH, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(args, cwd=ROOT_DIR, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                    creationflags=flags)
        app_db.set_state("stream_pid", proc.pid, by)
        app_db.set_state("stream_heartbeat", None, by)
        app_db.set_state("stream_started_at", datetime.now().isoformat(timespec="milliseconds"), by)
        spawned = True
    return {**stream_status(), "spawned": spawned}


def stream_add_machine(machine_id: int, by: str | None = None) -> str:
    """긴급 배정된 설비를 실시간 대상에 넣는다: 생성기가 실행 중이고 대상이 MAX_MACHINES대 미만일 때만.
    (이미 있거나 꽉 찼거나 생성기가 멈춰 있으면 아무것도 바꾸지 않는다.) 결과를 문장으로 돌려준다."""
    m = int(machine_id)
    status = stream_status()
    if status["state"] != "실행 중":
        return f"#{m}: 실시간 생성기가 실행 중이 아니라 실시간 대상에 넣지 않았습니다."
    machines = [int(x) for x in status["machines"]]
    if m in machines:
        return f"#{m}: 이미 실시간 대상입니다."
    if len(machines) >= MAX_MACHINES:
        return f"#{m}: 실시간 대상이 {MAX_MACHINES}대로 꽉 차 있어 넣지 않았습니다."
    app_db.set_state("stream_machines", sorted([*machines, m]), by)
    return f"#{m}: 실시간 대상에 추가했습니다."


def stream_stop(by: str | None = None) -> dict:
    """정지 명령 (생성기는 다음 tick에 정리 후 스스로 종료한다). 남은 이상 주입도 지운다
    (다시 시작했을 때 예전 주입이 처음부터 다시 실행되지 않게)."""
    app_db.set_state("stream_running", False, by)
    app_db.set_state("stream_inject", [], by)
    return stream_status()


def stream_inject(machine_id: int, kind: str, sensor: str, by: str | None = None) -> dict:
    """이상 주입 (설비마다 하나: 새 주입이 이전 주입을 바꾼다)."""
    if kind not in KINDS:
        return {"error": f"주입 종류는 {', '.join(KINDS)} 중 하나여야 합니다 (받은 값: {kind!r})"}
    if sensor not in SENSORS:
        return {"error": f"센서는 {', '.join(SENSORS)} 중 하나여야 합니다 (받은 값: {sensor!r})"}
    items = [i for i in app_db.get_state("stream_inject", []) if int(i["machine_id"]) != int(machine_id)]
    items.append({"machine_id": int(machine_id), "kind": kind, "sensor": sensor,
                  "started_at": datetime.now().isoformat(timespec="milliseconds")})
    app_db.set_state("stream_inject", items, by)
    return {"machine_id": int(machine_id), "kind": kind, "sensor": sensor, "ok": True}


def stream_reset(machine_id: int, by: str | None = None) -> dict:
    """[정상 복귀]: 그 설비의 주입을 끝낸다."""
    items = app_db.get_state("stream_inject", [])
    keep = [i for i in items if int(i["machine_id"]) != int(machine_id)]
    app_db.set_state("stream_inject", keep, by)
    return {"machine_id": int(machine_id), "removed": len(items) - len(keep)}


def mark_repaired(machine_id: int, by: str | None = None) -> None:
    """긴급 작업 완료 보고 -> 생성기에 "이 설비 정비 끝" 신호 (app_state stream_repair = {설비: 시각}).
    생성기는 다음 tick에 그 설비의 이상 주입을 끝내고 값을 평소값으로 내려 보내며, 감지 창을 비운다.
    (주입이 남아 있으면 경고가 닫히자마자 같은 이상으로 새 경고가 생겨 다른 팀이 또 불려 간다)"""
    items = {str(k): v for k, v in (app_db.get_state("stream_repair", {}) or {}).items()}
    items[str(int(machine_id))] = datetime.now().isoformat(timespec="milliseconds")
    app_db.set_state("stream_repair", items, by)


def apply_repair(injections: list[dict], machine_id: int, last_values: dict, stamp: str) -> list[dict]:
    """정비 완료 처리 (순수 함수): 그 설비의 이상 주입을 지우고, 주입 센서의 현재 값이 있으면
    평소값으로 내려오는 recover 주입으로 바꾼다. 다른 설비 주입은 그대로."""
    m = int(machine_id)
    mine = next((i for i in injections if int(i["machine_id"]) == m), None)
    out = [i for i in injections if int(i["machine_id"]) != m]
    if mine and mine.get("kind") != "recover":
        start = (last_values or {}).get(mine["sensor"])
        if start is not None:
            out.append({"machine_id": m, "kind": "recover", "sensor": mine["sensor"], "from": float(start),
                        "started_at": stamp})
    return out


STATE_KO = {"정상": "정상", "이상": "이상 감지", "센서이상": "센서 이상"}


def stream_card(machine_id: int, as_of: str, seconds: int = 120) -> dict:
    """화면 카드 1장: {"window": 최근 seconds초, "base": {sensor: (mean, std)}, "state": detector.machine_state,
    "state_ko": 정상/이상 감지/센서 이상}. 판단은 생성기와 같은 감지 함수(최근 30초)."""
    from stream.baseline import baseline
    from stream.detector import machine_state

    window = stream_window(machine_id, seconds, as_of)
    base = baseline(machine_id, as_of)
    rows = [{s: (None if pd.isna(v) else float(v)) for s, v in r.items()} for r in window[SENSORS].to_dict("records")]
    state = machine_state(rows, base, SENSORS)
    return {"window": window, "base": base, "state": state, "state_ko": STATE_KO[state["state"]]}


COLUMNS = ["sim_ts", *SENSORS, "injected", "injected_sensor"]


def stream_window(machine_id: int, seconds: int = 120, as_of: str | None = None) -> pd.DataFrame:
    """차트용 최근 seconds초 원시값 (sim_ts 오름차순, 같은 sim_ts는 마지막에 쓴 행 하나만).
    as_of를 주면 그 기준 시각부터 하루 안의 행만 (다른 날 행이 섞여 x축이 벌어지지 않게).
    열: sim_ts, volt, rotate, pressure, vibration, injected, injected_sensor"""
    where, params = "machine_id = ?", [int(machine_id)]
    if as_of:
        start = pd.Timestamp(as_of)
        where += " AND sim_ts >= ? AND sim_ts < ?"
        params += [start.strftime("%Y-%m-%d %H:%M:%S"), (start + pd.Timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")]
    rows = app_db._rows(f"SELECT {', '.join(COLUMNS)} FROM live_stream WHERE {where}"
                        " ORDER BY sim_ts DESC, rowid DESC LIMIT ?", (*params, int(seconds) * 3))
    df = pd.DataFrame(rows, columns=COLUMNS).drop_duplicates("sim_ts", keep="first").head(int(seconds))
    df = df.iloc[::-1].reset_index(drop=True)
    df["sim_ts"] = pd.to_datetime(df["sim_ts"])
    return df


def resume_tick(conn, as_of: str) -> int:
    """생성기가 (다시) 시작할 때 이어 갈 tick: 그 기준 시각 이후 저장된 마지막 sim_ts + 1초 (없으면 0).
    재시작마다 0부터 시작하면 같은 시각이 다시 저장돼 차트 선이 되돌아가며 겹친다."""
    start = pd.Timestamp(as_of)
    end = start + pd.Timedelta(days=1)
    row = conn.execute("SELECT MAX(sim_ts) FROM live_stream WHERE sim_ts >= ? AND sim_ts < ?",
                       (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    if not row or not row[0]:
        return 0
    return int((pd.Timestamp(row[0]) - start).total_seconds()) + 1
