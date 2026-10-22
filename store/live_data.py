"""원본(pdm.db) + 사용자 입력 데이터(app.db live_*)를 합쳐 읽는다. 원본은 읽기만 한다.

- telemetry_frame(machine_ids, start, end) / errors_frame(machine_ids, start, end): start < datetime <= end
  · 켜진 배치(live_batches.active = 1)의 입력 행만 쓴다.
  · 같은 (machine_id, datetime)이면 입력 데이터가 원본을 덮어쓴다 (배치가 여럿이면 나중에 만든 배치가 우선).
  · 열은 원본과 같고 source("원본" / "입력"), batch_id(원본은 None)가 붙는다.
  · app.db나 live 테이블이 없으면 원본만 돌려준다 (기존 동작 그대로).
- 기준 시각 규칙: 호출하는 쪽이 end = as_of로 부르므로 입력 데이터도 datetime <= as_of만 보인다.
"""

import sqlite3
from pathlib import Path

import pandas as pd

from store import app_db

ROOT_DIR = Path(__file__).resolve().parent.parent
PDM_PATH = ROOT_DIR / "data" / "pdm.db"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
KEYS = ["machine_id", "datetime"]
SENSORS = ["volt", "rotate", "pressure", "vibration"]
SOURCE_RAW, SOURCE_INPUT = "원본", "입력"


def _ts(value) -> str:
    return pd.Timestamp(value).strftime(DATETIME_FORMAT)


def _where(machine_ids: list[int] | None, prefix: str = "") -> tuple[str, list]:
    sql, params = f" WHERE {prefix}datetime > ? AND {prefix}datetime <= ?", []
    if machine_ids is not None:
        ids = [int(m) for m in machine_ids]
        sql += f" AND {prefix}machine_id IN ({', '.join('?' * len(ids))})" if ids else " AND 0"
        params = ids
    return sql, params


def _read_raw(table: str, cols: list[str], machine_ids, start: str, end: str) -> pd.DataFrame:
    where, ids = _where(machine_ids)
    with sqlite3.connect(f"{PDM_PATH.as_uri()}?mode=ro", uri=True) as conn:
        return pd.read_sql_query(f"SELECT {', '.join(cols)} FROM {table}{where}", conn, params=[start, end, *ids])


def _read_input(table: str, cols: list[str], machine_ids, start: str, end: str) -> pd.DataFrame:
    """켜진 배치의 입력 행. app.db나 테이블이 없으면 빈 표."""
    empty = pd.DataFrame(columns=[*cols, "batch_id", "batch_created"])
    if not Path(app_db.DB_PATH).exists():
        return empty
    where, ids = _where(machine_ids, prefix="t.")
    sql = (f"SELECT {', '.join('t.' + c for c in cols)}, t.batch_id, b.created_at AS batch_created"
           f" FROM {table} t JOIN live_batches b ON b.batch_id = t.batch_id{where} AND b.active = 1")
    try:
        with sqlite3.connect(f"{Path(app_db.DB_PATH).resolve().as_uri()}?mode=ro", uri=True) as conn:
            return pd.read_sql_query(sql, conn, params=[start, end, *ids])
    except sqlite3.Error:  # 옛 app.db (live 테이블 없음)
        return empty


def _merge(raw: pd.DataFrame, inp: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    raw = raw.assign(source=SOURCE_RAW, batch_id=None)
    if inp.empty:
        out = raw
    else:
        # 같은 시각에 배치가 여럿이면 나중 배치의 행만 (에러는 한 시각에 여러 건일 수 있어 행이 아니라 배치를 고른다)
        winner = (inp.sort_values(["batch_created", "batch_id"], kind="stable")
                  .drop_duplicates(KEYS, keep="last")[[*KEYS, "batch_id"]])
        inp = (inp.merge(winner, on=[*KEYS, "batch_id"])
               .drop(columns="batch_created").assign(source=SOURCE_INPUT))
        covered = pd.MultiIndex.from_frame(inp[KEYS])
        raw = raw[~pd.MultiIndex.from_frame(raw[KEYS]).isin(covered)]
        out = pd.concat([raw, inp], ignore_index=True)
    out = out[[*cols, "source", "batch_id"]].sort_values(KEYS, kind="stable").reset_index(drop=True)
    out["datetime"] = pd.to_datetime(out["datetime"], format=DATETIME_FORMAT)
    return out


def telemetry_frame(machine_ids: list[int] | None, start, end) -> pd.DataFrame:
    """열: datetime, machine_id, volt, rotate, pressure, vibration, source, batch_id"""
    cols = ["datetime", "machine_id", *SENSORS]
    start, end = _ts(start), _ts(end)
    return _merge(_read_raw("telemetry", cols, machine_ids, start, end),
                  _read_input("live_telemetry", cols, machine_ids, start, end), cols)


def errors_frame(machine_ids: list[int] | None, start, end) -> pd.DataFrame:
    """열: datetime, machine_id, error_id, source, batch_id"""
    cols = ["datetime", "machine_id", "error_id"]
    start, end = _ts(start), _ts(end)
    return _merge(_read_raw("errors", cols, machine_ids, start, end),
                  _read_input("live_errors", cols, machine_ids, start, end), cols)


def input_batches(df: pd.DataFrame) -> list[str]:
    """합쳐 읽은 표에 섞인 입력 배치 번호 (없으면 [])."""
    if "batch_id" not in df.columns:
        return []
    return sorted({b for b in df["batch_id"].dropna()})
