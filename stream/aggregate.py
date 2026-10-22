"""1시간 요약: 설비별 최근 스트림 평균을 live_telemetry에 1행 upsert (60초마다, 생성기 루프가 부른다).

- 배치 "STREAM-{YYYYMMDD}"(live_batches kind "stream"), datetime = 기준 시각.
  그래서 예측 모델·조사 에이전트는 기존 시간 가드(datetime <= as_of)를 지키면서 스트림 결과를 본다.
- 스트림 중 관련 에러(drift 20초 시점)도 같은 배치로 live_errors에 기준 시각으로 남긴다 (record_error).
"""

import sqlite3

import pandas as pd

from store import app_db
from stream import SENSORS


def batch_id(as_of: str) -> str:
    return f"STREAM-{pd.Timestamp(as_of):%Y%m%d}"


def _ts(value) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def ensure_batch(conn: sqlite3.Connection, as_of: str) -> str:
    bid = batch_id(as_of)
    conn.execute('INSERT INTO live_batches (batch_id, machine_id, kind, description, start, "end", active, created_by,'
                 " created_at) VALUES (?, NULL, 'stream', '실시간 스트림 1시간 요약', ?, ?, 1, 'stream_sim', ?)"
                 " ON CONFLICT(batch_id) DO UPDATE SET active = 1", (bid, _ts(as_of), _ts(as_of), app_db.now()))
    return bid


def upsert_hourly(conn: sqlite3.Connection, machine_ids: list[int], as_of: str) -> int:
    """설비별 보관 중인 스트림(최근 10분) 평균 -> live_telemetry (batch, machine, datetime=as_of) 1행. 쓴 행 수."""
    bid, as_of = ensure_batch(conn, as_of), _ts(as_of)
    n = 0
    for m in machine_ids:
        row = conn.execute(f"SELECT COUNT(*), {', '.join(f'AVG({s})' for s in SENSORS)} FROM live_stream"
                           " WHERE machine_id = ?", (int(m),)).fetchone()
        if not row[0] or any(v is None for v in row[1:]):
            continue
        conn.execute("DELETE FROM live_telemetry WHERE batch_id = ? AND machine_id = ? AND datetime = ?", (bid, int(m), as_of))
        conn.execute("INSERT INTO live_telemetry (batch_id, datetime, machine_id, volt, rotate, pressure, vibration,"
                     " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (bid, as_of, int(m), *row[1:], app_db.now()))
        n += 1
    return n


def record_error(conn: sqlite3.Connection, machine_id: int, error_id: str, as_of: str) -> None:
    bid = ensure_batch(conn, as_of)
    conn.execute("INSERT INTO live_errors (batch_id, datetime, machine_id, error_id, created_at) VALUES (?, ?, ?, ?, ?)",
                 (bid, _ts(as_of), int(machine_id), error_id, app_db.now()))
