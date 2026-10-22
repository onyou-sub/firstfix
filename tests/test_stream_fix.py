"""실시간 모니터 버그 수정 확인 (LLM 없음, pdm.db 없이 가짜 평소값 사용).

- 예전 스키마 app.db에 새 열이 자동으로 추가된다 (alerts.investigated_at, live_stream.injected_sensor)
- 생성기를 다시 시작해도 sim_ts가 겹치지 않고 이어진다 (resume_tick)
- stream_window: sim_ts 오름차순, 중복 없음, 다른 날 행 제외
- 주입 센서만 표시, 정지하면 주입 명령이 지워진다, 1초 잡음은 0.3·std

실행: uv run pytest tests/test_stream_fix.py -v
"""

import sqlite3

import numpy as np
import pandas as pd
import pytest

from store import app_db
from stream import SENSORS
from stream import control
from stream.control import apply_repair
from stream.detector import machine_state
from stream.generator import STREAM_NOISE, next_values

AS_OF = "2015-11-12 06:00:00"
BASE = {"volt": (170.8, 15.0), "rotate": (446.6, 50.0), "pressure": (100.9, 10.0), "vibration": (40.4, 5.0)}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    monkeypatch.setattr(app_db, "DB_PATH", path)
    with app_db.connect():
        pass
    conn = sqlite3.connect(path, isolation_level=None)
    yield conn
    conn.close()


def _insert(conn, ts: str, vib: float, injected_sensor: str | None = None, machine_id: int = 45) -> None:
    conn.execute("INSERT INTO live_stream (sim_ts, wall_ts, machine_id, volt, rotate, pressure, vibration, injected,"
                 " injected_sensor) VALUES (?, 'w', ?, 170, 446, 100, ?, ?, ?)",
                 (ts, machine_id, vib, "drift" if injected_sensor else None, injected_sensor))


def _ts(sec: int, day: str = AS_OF) -> str:
    return (pd.Timestamp(day) + pd.Timedelta(seconds=sec)).strftime("%Y-%m-%d %H:%M:%S")


def test_old_schema_gets_new_columns(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE alerts (alert_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, machine_id INTEGER NOT NULL, comp TEXT,
            prob REAL, level TEXT NOT NULL, source TEXT NOT NULL, batch_id TEXT, status TEXT NOT NULL, evidence_id TEXT,
            wo_id TEXT, message TEXT, investigating INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL);
        CREATE TABLE live_stream (sim_ts TEXT NOT NULL, wall_ts TEXT NOT NULL, machine_id INTEGER NOT NULL,
            volt REAL, rotate REAL, pressure REAL, vibration REAL, injected TEXT);
    """)
    old.close()
    monkeypatch.setattr(app_db, "DB_PATH", path)
    with app_db.connect() as conn:
        alerts = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
        stream = {r[1] for r in conn.execute("PRAGMA table_info(live_stream)")}
    assert "investigated_at" in alerts and "injected_sensor" in stream
    assert app_db._rows("SELECT COUNT(*) AS n FROM alerts WHERE investigated_at >= ?", ("2026",))[0]["n"] == 0


def test_resume_tick_continues(db):
    assert control.resume_tick(db, AS_OF) == 0
    for s in range(40):
        _insert(db, _ts(s), 40)
    assert control.resume_tick(db, AS_OF) == 40
    _insert(db, _ts(5, "2015-11-13 06:00:00"), 40)  # 다음 날 행은 이 기준 시각에 영향 없음
    assert control.resume_tick(db, AS_OF) == 40


def test_window_sorted_unique_same_day(db):
    for s in range(150):
        _insert(db, _ts(s), 40 + s * 0.01)
    _insert(db, _ts(149), 99)  # 같은 시각이 다시 저장된 경우 (예전 재시작 버그) -> 마지막 행만
    _insert(db, _ts(10, "2015-11-11 06:00:00"), 1)  # 다른 날
    w = control.stream_window(45, 120, AS_OF)
    assert len(w) == 120
    assert w["sim_ts"].is_monotonic_increasing and not w["sim_ts"].duplicated().any()
    assert (w["sim_ts"].diff().dropna().dt.total_seconds() == 1).all()
    assert w["vibration"].iloc[-1] == 99
    assert w["sim_ts"].min() >= pd.Timestamp(AS_OF)


def test_injected_sensor_only(db):
    for s in range(30):
        _insert(db, _ts(s), 40, injected_sensor="vibration" if s >= 10 else None)
    w = control.stream_window(45, 120, AS_OF)
    assert set(w["injected_sensor"].dropna()) == {"vibration"}
    rng = np.random.default_rng(0)
    v = next_values(45, BASE, {"kind": "drift", "sensor": "pressure", "elapsed": 5, "last": {}}, 0, rng)
    assert v["injected_sensor"] == "pressure"
    assert next_values(45, BASE, None, 0, rng)["injected_sensor"] is None


def test_noise_is_small():
    rng = np.random.default_rng(1)
    vib = [next_values(45, BASE, None, t, rng)["vibration"] for t in range(2000)]
    assert abs(np.std(vib) - STREAM_NOISE * BASE["vibration"][1]) < 0.2


def test_stop_clears_injections(db):
    control.stream_inject(45, "drift", "vibration", by="admin")
    assert app_db.get_state("stream_inject", [])
    control.stream_stop(by="admin")
    assert app_db.get_state("stream_inject", []) == []
    assert app_db.get_state("stream_running") is False


# ---- 정비 완료 -> 값 복귀 (다른 팀이 또 불려 가지 않게)

def test_recover_ramps_down_to_normal():
    rng = np.random.default_rng(2)
    mean, std = BASE["vibration"]
    vals = []
    for e in range(12):
        v = next_values(45, BASE, {"kind": "recover", "sensor": "vibration", "elapsed": e, "last": {}, "from": 72.0},
                        0, rng)
        vals.append((v["vibration"], v["done"], v["injected"]))
    assert vals[0][0] > mean + 5 * std  # 처음엔 정비 전 값 근처
    assert abs(vals[9][0] - mean) < 3 * std  # 10초 안에 평소값 근처
    assert vals[10][1] is True  # 10초 뒤 recover 끝
    assert all(inj is None for _, _, inj in vals)  # 이상 주입이 아니므로 주황 표시 없음


def test_apply_repair_replaces_injection():
    items = [{"machine_id": 45, "kind": "drift", "sensor": "vibration", "started_at": "a"},
             {"machine_id": 12, "kind": "step", "sensor": "volt", "started_at": "b"}]
    out = apply_repair(items, 45, {"vibration": 70.0}, "t1")
    assert {"machine_id": 12, "kind": "step", "sensor": "volt", "started_at": "b"} in out
    rec = [i for i in out if i["machine_id"] == 45]
    assert rec == [{"machine_id": 45, "kind": "recover", "sensor": "vibration", "from": 70.0, "started_at": "t1"}]
    assert apply_repair(items, 99, {}, "t2") == items  # 주입 없는 설비는 그대로


def test_mark_repaired_sets_signal(db):
    control.mark_repaired(45, by="test")
    first = app_db.get_state("stream_repair", {})["45"]
    control.mark_repaired(45, by="test")
    assert app_db.get_state("stream_repair", {})["45"] >= first


def test_no_new_alarm_after_repair():
    """drift로 경고 -> 정비 완료(창 비우고 recover) -> 이후 120초 동안 다시 '이상'이 나오지 않아야 한다."""
    rng = np.random.default_rng(5)
    window, last = [], {}
    inj = {"kind": "drift", "sensor": "vibration", "start": 0, "from": None}
    alarmed_at = None
    for t in range(200):
        state = {"kind": inj["kind"], "sensor": "vibration", "elapsed": t - inj["start"], "last": last,
                 "from": inj["from"]} if inj else None
        v = next_values(45, BASE, state, t, rng)
        if v["done"]:
            inj = None
        last = {s: v[s] for s in SENSORS}
        window = (window + [v])[-30:]
        st = machine_state(window, BASE, SENSORS)["state"]
        if alarmed_at is None and st == "이상":
            alarmed_at = t
            # 작업자가 바로 정비 완료했다고 가정 -> 생성기와 같은 처리
            items = apply_repair([{"machine_id": 45, "kind": "drift", "sensor": "vibration", "started_at": "x"}],
                                 45, last, "r")
            inj = {"kind": "recover", "start": t + 1, "from": items[0]["from"]}
            window = []
        elif alarmed_at is not None:
            assert st == "정상", f"정비 후 {t - alarmed_at}초에 다시 {st}"
    assert alarmed_at is not None
