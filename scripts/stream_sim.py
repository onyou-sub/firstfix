"""1초 실시간 스트림 생성기 (별도 프로세스, Streamlit 안에서 루프를 돌리지 않는다).

1초(--tick)마다:
  app_state에서 명령 읽기 -> 대상 설비(최대 5대)마다 값 생성·저장(live_stream) -> 센서별 감지(detect)
  -> 이상/센서이상이면 live_ops.evaluate_machine(..., stream_hint=...) (새 경고 -> handle_alert: 바로 배정 후 조사)
- 설비별 60초 쿨다운, 오늘 열린 경고만 중복으로 본다, 10초마다 배정 안 된 경고를 다시 처리한다.
- 같은 설비에 열린 경고가 있으면 감지는 계속하되 evaluate_machine을 다시 부르지 않는다.
- 60 tick마다 1시간 요약(live_telemetry, 배치 STREAM-YYYYMMDD) upsert, 10분보다 오래된 live_stream 삭제.
- 기준 시각은 app_state의 공유 기준 날짜(06:00). 바뀌면 평소값 캐시를 비운다.
- 시간은 tick 단위 모의 시각(sim_ts = 기준 시각 + 경과 tick초)이라 --tick 0.01로 빨리 돌려도 판단은 같다.
  다시 시작하면 tick은 저장된 마지막 sim_ts 다음부터 이어 간다 (같은 시각이 두 번 저장되지 않게).
- tick 단위로 예외를 잡아 로그만 남기고 루프는 계속한다. stream_running이 False가 되면 정리 후 종료.

실행: uv run python scripts/stream_sim.py --tick 1.0          (보통은 화면 [시작]이 띄운다)
      옵션: --tick 0.01 (테스트), --seed 0, --no-llm (조사 에이전트 대신 LLM 없는 가짜 조사), --max-ticks N
"""

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

import live_ops  # noqa: E402
from store import app_db  # noqa: E402
from stream import SENSORS, aggregate, baseline  # noqa: E402
from stream.control import apply_repair, resume_tick  # noqa: E402
from stream.detector import WINDOW, machine_state  # noqa: E402
from stream.generator import next_values  # noqa: E402

MAX_MACHINES = 5
KEEP_MINUTES = 10
EVERY_TICKS = 60
DEFAULT_DAY = "2015-10-01"
ALERT_JOIN_SECONDS = 180
ALERT_COOLDOWN_TICKS = 60  # 설비별: 경고 판단을 넘긴 뒤 60초 안에는 다시 넘기지 않는다 (경고 반복 생성 방지)
RETRY_EVERY_TICKS = 10  # 배정이 안 된 채 멈춘 오늘 경고를 10초마다 다시 처리  # 종료할 때 진행 중인 자동 조사·배정을 기다리는 최대 시간


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Sim:
    def __init__(self, tick_seconds: float, seed: int):
        self.tick_seconds = tick_seconds
        self.rng = np.random.default_rng(seed)
        with app_db.connect():  # 스키마 생성 (한 번)
            pass
        self.conn = sqlite3.connect(app_db.DB_PATH, timeout=10, isolation_level=None)  # 매 tick 자동 커밋
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.tick = 0
        self.as_of: str | None = None
        self.windows: dict[int, deque] = {}
        self.last: dict[int, dict] = {}
        self.inject_start: dict[int, tuple[tuple, int]] = {}  # 설비 -> (주입 키, 시작 tick)
        self.last_eval_tick: dict[int, int] = {}  # 설비 -> 마지막으로 evaluate_machine을 부른 tick (쿨다운)
        self.skip_logged: set[str] = set()  # "열린 경고 때문에 건너뜀" 로그를 경고마다 한 번만
        # 이미 처리한 정비 완료 신호 (재시작 때 예전 신호를 다시 적용하지 않게 현재 값으로 시작)
        self.repair_seen: dict[int, str] = {int(k): v for k, v in (self.get("stream_repair", {}) or {}).items()}

    # ---- app_state (자기 연결로 직접: app_db.connect는 매번 스키마를 실행해 1초 루프에 무겁다)
    def get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return row[0]

    def put(self, key: str, value) -> None:
        self.conn.execute("INSERT INTO app_state (key, value, updated_by, updated_at) VALUES (?, ?, 'stream_sim', ?)"
                          " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,"
                          " updated_at = excluded.updated_at", (key, json.dumps(value, ensure_ascii=False), app_db.now()))

    def current_as_of(self) -> str:
        row = self.conn.execute("SELECT value FROM app_state WHERE key = 'current_day'").fetchone()
        return f"{row[0] if row else DEFAULT_DAY} {app_db.AS_OF_HOUR}"

    # ---- 한 tick
    def step(self) -> None:
        as_of = self.current_as_of()
        if as_of != self.as_of:  # 기준 시각이 바뀌면 평소값·창을 새로
            baseline.clear_cache()
            self.as_of, self.tick = as_of, resume_tick(self.conn, as_of)
            self.windows.clear()
            self.last.clear()
            self.inject_start.clear()
            self.last_eval_tick.clear()
            closed = live_ops.close_stale_alerts(as_of)
            log(f"기준 시각 {as_of} (tick {self.tick}부터)" + (f", 이전 날짜 열린 경고 {closed}건 종료" if closed else ""))
        machines = [int(m) for m in self.get("stream_machines", [])][:MAX_MACHINES]
        self.apply_repairs()
        injections = {int(i["machine_id"]): i for i in self.get("stream_inject", [])}
        sim_ts = (pd.Timestamp(as_of) + pd.Timedelta(seconds=self.tick)).strftime("%Y-%m-%d %H:%M:%S")
        wall = datetime.now().isoformat(timespec="milliseconds")
        finished = []
        for m in machines:
            base = baseline.baseline(m, as_of)
            inj = injections.get(m)
            state = None
            if inj:
                key = (inj["kind"], inj["sensor"], inj.get("started_at"))
                if self.inject_start.get(m, (None,))[0] != key:
                    self.inject_start[m] = (key, self.tick)
                state = {"kind": inj["kind"], "sensor": inj["sensor"], "elapsed": self.tick - self.inject_start[m][1],
                         "last": self.last.get(m, {}), "from": inj.get("from")}
            v = next_values(m, base, state, self.tick, self.rng)
            if v["done"]:
                finished.append(m)
            if v["error"]:
                aggregate.record_error(self.conn, m, v["error"], as_of)
                log(f"#{m} 관련 에러 기록: {v['error']}")
            self.conn.execute("INSERT INTO live_stream (sim_ts, wall_ts, machine_id, volt, rotate, pressure, vibration,"
                              " injected, injected_sensor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (sim_ts, wall, m, *(v[s] for s in SENSORS), v["injected"], v["injected_sensor"]))
            self.last[m] = {**self.last.get(m, {}), **{s: v[s] for s in SENSORS if v[s] is not None}}
            window = self.windows.setdefault(m, deque(maxlen=WINDOW))
            window.append(v)
            self.judge(m, list(window), base, as_of)
        if finished:  # 끝난 주입(180초 경과, spike 2초)은 명령 목록에서 지운다
            self.put("stream_inject", [i for i in self.get("stream_inject", []) if int(i["machine_id"]) not in finished])
        if self.tick % RETRY_EVERY_TICKS == RETRY_EVERY_TICKS - 1:
            if retried := live_ops.retry_stuck_alerts(as_of):
                log(f"배정 안 된 경고 다시 처리: {', '.join(retried)}")
        self.put("stream_heartbeat", wall)
        self.put("stream_pid", os.getpid())
        if self.tick % EVERY_TICKS == EVERY_TICKS - 1:
            aggregate.upsert_hourly(self.conn, machines, as_of)
            cutoff = (datetime.now() - timedelta(minutes=KEEP_MINUTES)).isoformat(timespec="milliseconds")
            self.conn.execute("DELETE FROM live_stream WHERE wall_ts < ?", (cutoff,))
        self.tick += 1

    def apply_repairs(self) -> None:
        """긴급 작업 완료 보고(stream_repair 신호) -> 그 설비의 이상 주입을 끝내고 값이 평소로 내려오게(recover),
        감지 창을 비워 정비 전 값으로 다시 경고가 나지 않게 한다."""
        repairs = {int(k): v for k, v in (self.get("stream_repair", {}) or {}).items()}
        new = {m: t for m, t in repairs.items() if self.repair_seen.get(m) != t}
        if not new:
            return
        items = self.get("stream_inject", [])
        for m, stamp in new.items():
            items = apply_repair(items, m, self.last.get(m, {}), stamp)
            self.windows.pop(m, None)
            self.inject_start.pop(m, None)
            self.last_eval_tick.pop(m, None)  # 정비 후 새 이상은 바로 판단
            self.repair_seen[m] = stamp
            log(f"#{m} 정비 완료 -> 평소값으로 복귀")
        self.put("stream_inject", items)

    def judge(self, machine_id: int, window: list[dict], base: dict, as_of: str) -> None:
        s = machine_state(window, base, SENSORS)
        if s["state"] == "정상":
            return
        if (opened := live_ops._open_alert(machine_id, as_of)) is not None:  # 오늘 열린 경고가 있으면 감지만 계속
            if opened["alert_id"] not in self.skip_logged:
                self.skip_logged.add(opened["alert_id"])
                log(f"#{machine_id} {s['sensor']} {s['state']} - 열린 경고 {opened['alert_id']}({opened['status']})가 있어 새 경고 없음")
            return
        last = self.last_eval_tick.get(machine_id)
        if last is not None and self.tick - last < ALERT_COOLDOWN_TICKS:  # 쿨다운 중
            return
        self.last_eval_tick[machine_id] = self.tick
        hint = {"sensor": s["sensor"], "comp": s["comp"], "state": s["state"], "z": s["z"]}
        out = live_ops.evaluate_machine(machine_id, as_of, batch_id=aggregate.batch_id(as_of), stream_hint=hint)
        log(f"#{machine_id} {s['sensor']} {s['state']} (z {s['z'] or 0:+.1f}, {s['count']}초) -> {out['level']}"
            f" {out.get('alert_id')} 새 경고 {out['is_new']}")
        live_ops.on_new_alerts([out])

    def run(self, max_ticks: int | None = None) -> None:
        log(f"시작 pid {os.getpid()} tick {self.tick_seconds}s, app.db {app_db.DB_PATH}")
        n = 0
        while True:
            t0 = time.monotonic()
            try:
                if not self.get("stream_running", False):
                    break
                self.step()
            except Exception:
                log("tick 오류 (루프는 계속):\n" + traceback.format_exc())
            n += 1
            if max_ticks is not None and n >= max_ticks:
                break
            time.sleep(max(0.0, self.tick_seconds - (time.monotonic() - t0)))
        self.shutdown()

    def shutdown(self) -> None:
        for t in [t for t in threading.enumerate() if t.name.startswith("alert-")]:  # 진행 중인 자동 조사·배정 마무리
            t.join(timeout=ALERT_JOIN_SECONDS)
        try:
            if self.as_of:
                aggregate.upsert_hourly(self.conn, [int(m) for m in self.get("stream_machines", [])], self.as_of)
            self.put("stream_pid", None)
            self.put("stream_heartbeat", None)
        finally:
            self.conn.close()
        log("종료")


def main() -> None:
    p = argparse.ArgumentParser(description="1초 실시간 센서 스트림 생성기")
    p.add_argument("--tick", type=float, default=1.0, help="tick 간격(초). 테스트는 0.01")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--no-llm", action="store_true", help="조사 에이전트 대신 LLM 없는 가짜 조사")
    p.add_argument("--max-ticks", type=int, default=None)
    args = p.parse_args()
    if args.no_llm:
        live_ops.INVESTIGATE = live_ops.mock_investigate
    Sim(args.tick, args.seed).run(args.max_ticks)


if __name__ == "__main__":
    main()
