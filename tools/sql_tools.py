"""데이터 에이전트용 SQL 도구 3개 + 기준 시각(as_of)으로 잘린 읽기 전용 연결.

- get_connection(as_of): pdm.db를 읽기 전용으로 열고, 시간이 있는 테이블 4개를 같은 이름의 TEMP VIEW로 덮는다.
  SQLite는 이름을 temp -> main 순서로 찾으므로 LLM이 'failures'라고 쓰면 as_of 이하 행만 보인다.
- 운영 DB(app.db)는 읽기 전용으로 ATTACH(ops)하고, 데모 기준 날짜로 자른 ops_ TEMP VIEW 5개로만 보여 준다.
  (실제 처리 시각 created_at 등이 아니라 as_of_date / datetime / as_of로 자른다. 알림·세션·앱 상태는 노출하지 않음)
- run_sql_guarded: SELECT/WITH 단일 문장만 실행 (가드 위반은 "차단됨: 이유", 실행 오류는 "쿼리 오류: ..." 문자열)
- as_of는 ToolRuntime[Ctx].context에서 꺼낸다. LLM 입력 인자에는 없다.
"""

import re
import sqlite3
import time
from pathlib import Path

import pandas as pd
from langchain.tools import ToolRuntime, tool

from store import app_db
from tools.context import Ctx

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
DB_PATH = ROOT_DIR / "data" / "pdm.db"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

TIMED_TABLES = ["telemetry", "errors", "maint", "failures"]
TABLE_DESCRIPTIONS = {
    "telemetry": "센서 시간별 기록 (datetime, machine_id, volt 전압, rotate 회전수, pressure 압력, vibration 진동)",
    "errors": "에러 발생 기록 (datetime, machine_id, error_id)",
    "maint": "부품 교체 기록 (datetime, machine_id, comp, by_failure: 1=사후 정비, 0=예방 정비)",
    "failures": "부품 고장 기록 (datetime, machine_id, comp)",
    "machines": "설비 정보 (machine_id, model, age 연식)",
    "parts_stock": "부품 재고 (comp, part_name, qty, safety_stock 안전 재고, lead_time_days 조달 일수)",
    "criticality": "설비 중요도 (machine_id, line 라인, criticality A/B/C, bottleneck 병목 여부 1/0)",
    "work_standards": "작업 표준 시간 (task_type replace/sensor_check/temporary_check, comp, duration_min 분, skill)",
    "teams": "정비 팀 (team, skill 전문 분야, shift_start, shift_end 근무 시간)",
}
# 운영 기록 뷰: 이름 -> (원본 테이블, 자르는 컬럼, 날짜로 자름(True)/시각으로 자름(False), 보여 줄 컬럼)
OPS_VIEWS = {
    "ops_work_orders": ("work_orders", "as_of_date", True,
                        ["wo_id", "as_of_date", "machine_id", "comp", "task_type", "team", "start", "end", "title",
                         "status", "approved_by", "approved_at", "started_at", "done_at", "created_at", "urgent",
                         "paused"]),
    "ops_purchase_orders": ("purchase_orders", "as_of_date", True,
                            ["po_id", "as_of_date", "comp", "qty", "order_date", "status", "approved_by", "approved_at",
                             "received_at", "created_at"]),
    "ops_work_reports": ("work_reports", "as_of_date", True,
                         ["wo_id", "machine_id", "as_of_date", "predicted_comp", "actual_cause_type", "actual_comp",
                          "action", "duration_min", "parts_used", "memo", "team", "reported_by", "reported_at",
                          "step_results", "deviation"]),
    "ops_maint_log": ("maint_log", "datetime", False, ["datetime", "machine_id", "comp", "source", "wo_id", "created_at"]),
    "ops_stock_ledger": ("stock_ledger", "as_of", False, ["comp", "delta", "reason", "ref_id", "as_of", "created_at"]),
}
TABLE_DESCRIPTIONS |= {
    "ops_work_orders": "작업 지시 (wo_id, as_of_date 지시 날짜, machine_id, comp, task_type 작업 종류: replace=교체 /"
                       " temporary_check=임시 점검 / sensor_check=센서 점검 / urgent_check=긴급 점검, team 담당 팀,"
                       " start/end 예정 시각, status: ASSIGNED=배정, IN_PROGRESS=작업 중, DONE=완료, done_at 완료 처리 시각,"
                       " urgent: 1=긴급 작업(경고로 자동 배정, wo_id가 UWO-로 시작, task_type은 urgent_check 또는"
                       " sensor_check) / 0=일반 작업, paused 긴급 때문에 중단된 구간)",
    "ops_purchase_orders": "부품 발주 (po_id, as_of_date 발주 승인 날짜, comp, qty 수량, order_date 희망 발주일,"
                           " status: REQUESTED=요청, RECEIVED=입고)",
    "ops_work_reports": "작업 완료 보고 (wo_id, machine_id, as_of_date 작업 날짜, predicted_comp 예측 부품,"
                        " actual_cause_type: 예측과 같음 / 다른 부품 / 센서·설치 문제 / 이상 없음, actual_comp 실제 부품,"
                        " action 조치, duration_min 소요 분, parts_used 사용 부품 수, team, reported_by,"
                        " is_virtual: 1이면 교육용 가상 메모(1~9월), 실제 운영 통계에서는 보통 제외)",
    "ops_maint_log": "작업자 보고 부품 교체 기록 (datetime 교체 시각, machine_id, comp, source, wo_id). 원본 maint와 별개",
    "ops_stock_ledger": "부품 재고 변동 (comp, delta 증감 수량, reason 사유: 발주 입고/작업 사용 등, ref_id 관련 번호, as_of 시각)",
}
MAX_ROWS = 10
QUERY_TIMEOUT_SEC = 10

FORBIDDEN_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|pragma|attach|detach|vacuum)\b", re.IGNORECASE
)


def _norm_as_of(as_of: str) -> str:
    """'YYYY-MM-DD HH:MM:SS'로 정규화. 뷰 정의에 문자열로 들어가므로 Timestamp를 거쳐 형식을 강제한다."""
    return pd.Timestamp(as_of).strftime(DATETIME_FORMAT)


def _ops_views(conn: sqlite3.Connection, as_of: str) -> None:
    """app.db를 읽기 전용으로 ATTACH(ops)하고 기준 날짜로 자른 ops_ TEMP VIEW를 만든다. app.db가 없으면 건너뛴다."""
    path = Path(app_db.DB_PATH)
    if not path.exists():
        return
    conn.execute("ATTACH DATABASE ? AS ops", (f"{path.resolve().as_uri()}?mode=ro",))
    if isinstance(conn, _Conn):
        conn.ops_tables = frozenset(r[0] for r in conn.execute("SELECT name FROM ops.sqlite_master WHERE type = 'table'"))
    day = as_of[:10]
    for view, (table, col, by_day, wanted) in OPS_VIEWS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA ops.table_info({table})")}
        if not have:
            continue
        cols = ", ".join(f'"{c}"' for c in wanted if c in have)  # 옛 스키마에 없는 열은 빼고
        if view == "ops_work_reports":
            cols += f", CASE WHEN reported_by = '{app_db.VIRTUAL_REPORTER}' THEN 1 ELSE 0 END AS is_virtual"
        cut = day if by_day else as_of
        conn.execute(f"CREATE TEMP VIEW {view} AS SELECT {cols} FROM ops.{table} WHERE {col} <= '{cut}'")


def available_tables() -> list[str]:
    """지금 조회할 수 있는 테이블·뷰 이름 (app.db가 없으면 ops_ 뷰 제외)."""
    ops = Path(app_db.DB_PATH).exists()
    return [t for t in TABLE_DESCRIPTIONS if ops or t not in OPS_VIEWS]


class _Conn(sqlite3.Connection):
    """ops 원본 테이블 직접 읽기를 막는 authorizer를 가진 연결."""

    denied: str | None = None
    ops_tables: frozenset = frozenset()

    def authorize(self, action, arg1, arg2, db_name, view_name):
        # COUNT(*)처럼 열을 읽지 않으면 db_name이 None으로 오므로 테이블 이름으로도 확인한다
        raw_ops = db_name == "ops" or (db_name is None and arg1 in self.ops_tables)
        if action == sqlite3.SQLITE_READ and raw_ops and view_name not in OPS_VIEWS:
            self.denied = arg1
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def get_connection(as_of: str) -> sqlite3.Connection:
    """as_of 이하 데이터만 보이는 읽기 전용 연결.

    - mode=ro: main(pdm.db)에는 어떤 쓰기도 불가.
    - TEMP VIEW: 읽기 전용 연결에서도 temp 스키마(별도 임시 DB)에는 만들 수 있음을 확인했다.
      단, SQLite 뷰 정의에는 바인딩 파라미터(:as_of)를 쓸 수 없어서("parameters are not allowed in views")
      _norm_as_of로 형식을 고정한 문자열을 리터럴로 넣는다 (사용자 입력이 아니라 Ctx의 값).
    - app.db는 ?mode=ro로 ATTACH하고 ops_ 뷰(기준 날짜 이하)로만 노출한다 (_ops_views).
    - 뷰를 만든 뒤 PRAGMA query_only=ON으로 temp 쓰기까지 막는다 (가드가 뚫려도 2차 방어).
    - SQLite는 접두어 없는 이름을 temp -> main -> ops 순서로 찾으므로 'notifications', 'work_orders'만 써도
      ops 원본이 읽힌다. authorizer로 ops 읽기는 ops_ 뷰 안에서만 허용한다 (거부된 테이블은 conn.denied에 남김).
    """
    as_of = _norm_as_of(as_of)
    conn = sqlite3.connect(f"{DB_PATH.as_uri()}?mode=ro", uri=True, factory=_Conn)
    for table in TIMED_TABLES:
        conn.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} WHERE datetime <= '{as_of}'")
    _ops_views(conn, as_of)
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(conn.authorize)

    deadline = time.monotonic() + QUERY_TIMEOUT_SEC
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)  # 오래 걸리는 쿼리 중단
    return conn


def check_sql(query: str) -> str | None:
    """가드. 통과하면 None, 위반이면 "차단됨: 이유"."""
    q = query.strip()
    q = q.rstrip(";").strip() if q.endswith(";") else q
    if ";" in q:
        return "차단됨: 여러 문장(;)은 실행할 수 없습니다. SELECT 한 문장만 보내세요."
    if not re.match(r"^(select|with)\b", q, re.IGNORECASE):
        return "차단됨: SELECT 또는 WITH로 시작하는 조회만 실행할 수 있습니다."
    if m := FORBIDDEN_WORDS.search(q):
        return f"차단됨: 변경·관리 명령({m.group(1).upper()})은 실행할 수 없습니다. 이 도구는 조회 전용입니다."
    lowered = q.lower()
    if "main." in lowered or "sqlite_" in lowered:
        return "차단됨: main. 접두어나 sqlite_ 시스템 테이블은 쓸 수 없습니다. 테이블 이름만 쓰세요."
    if re.search(r"\bops\s*\.", lowered):
        return "차단됨: ops. 접두어(운영 원본 테이블)는 쓸 수 없습니다. ops_work_orders 같은 ops_ 뷰를 쓰세요."
    return None


def _md(columns: list[str], rows: list[tuple]) -> str:
    def fmt(v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, float):
            return f"{v:.4g}" if abs(v) < 1e6 else f"{v:,.0f}"
        return str(v)

    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def run_sql(query: str, as_of: str) -> str:
    """가드 통과 시 실행하고 최대 10행 표 문자열을 돌려준다."""
    if not isinstance(query, str) or not query.strip():
        return "차단됨: 빈 쿼리입니다."
    if blocked := check_sql(query):
        return blocked
    q = query.strip().rstrip(";")
    conn = get_connection(as_of)
    try:
        cur = conn.execute(q)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        if "interrupted" in str(e):
            return f"쿼리 오류: {QUERY_TIMEOUT_SEC}초 안에 끝나지 않아 중단했습니다. 조건이나 집계를 추가하세요."
        if getattr(conn, "denied", None):
            return (f"쿼리 오류: no such table: {conn.denied} (운영 기록은 ops_work_orders 같은 ops_ 뷰로만 조회할 수 있습니다."
                    " sql_db_list_tables로 이름을 확인하세요)")
        return f"쿼리 오류: {e}"
    finally:
        conn.close()
    if not rows:
        return "결과 0행"
    more = len(rows) > MAX_ROWS
    note = f"(상위 {MAX_ROWS}행만 표시, 더 있음 - 필요하면 집계나 LIMIT을 쓰세요)" if more else f"({len(rows)}행)"
    return f"{_md(columns, rows[:MAX_ROWS])}\n{note}"


def schema_text(tables: str, as_of: str) -> str:
    names = [t.strip() for t in str(tables).split(",") if t.strip()]
    tables_ok = available_tables()
    unknown = [t for t in names if t not in tables_ok]
    if not names or unknown:
        return f"입력 오류: 알 수 없는 테이블 {unknown or '(빈 입력)'}. 사용 가능: {', '.join(tables_ok)}"
    conn = get_connection(as_of)
    try:
        parts = []
        for t in names:  # t는 화이트리스트 이름만
            cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
            cur = conn.execute(f"SELECT * FROM {t} LIMIT 2")
            sample = cur.fetchall()
            col_text = ", ".join(f"{c[1]} {c[2]}" for c in cols)
            time_note = (" (기준 시각 이하 행만 보임)" if t in TIMED_TABLES
                         else " (기준 날짜 이하 행만 보임)" if t in OPS_VIEWS else "")
            parts.append(
                f"[{t}] {TABLE_DESCRIPTIONS[t]}{time_note}\n컬럼: {col_text}\n예시 2행:\n"
                + _md([d[0] for d in cur.description], sample)
            )
    finally:
        conn.close()
    return "\n\n".join(parts)


@tool
def sql_db_list_tables() -> str:
    """조회할 수 있는 테이블 14개(원본 설비 데이터 9개 + 운영 기록 ops_ 뷰 5개)의 이름과 한 줄 설명을 돌려준다.

    언제 쓰나: 질문에 답하기 전 가장 먼저, 어떤 테이블을 써야 할지 정할 때.
    언제 쓰지 않나: 이미 테이블과 컬럼을 알고 있을 때.
    """
    return "\n".join(f"- {name}: {TABLE_DESCRIPTIONS[name]}" for name in available_tables())


@tool
def sql_db_schema(tables: str, runtime: ToolRuntime[Ctx]) -> str:
    """테이블의 컬럼 목록(이름, 타입)과 예시 2행을 돌려준다.

    언제 쓰나: 쿼리를 쓰기 전에 정확한 컬럼 이름과 값 형식(예: datetime 문자열, comp 값)을 확인할 때.
    언제 쓰지 않나: 실제 집계 결과가 필요할 때 (run_sql_guarded 사용).

    Args:
        tables: 쉼표로 구분한 테이블 이름 (예: "failures, machines")
    """
    return schema_text(tables, runtime.context.as_of)


@tool
def run_sql_guarded(query: str, runtime: ToolRuntime[Ctx]) -> str:
    """SQLite SELECT 쿼리 한 문장을 실행하고 결과를 최대 10행 표로 돌려준다.

    언제 쓰나: 건수·비율·평균·목록 등 실제 데이터가 필요할 때. 집계는 SQL 안에서 끝낸다 (COUNT, AVG, GROUP BY).
    언제 쓰지 않나: 테이블 구조만 알고 싶을 때 (sql_db_schema 사용).
    규칙: SELECT/WITH 한 문장만 허용. 데이터 변경·삭제 명령, main.·ops. 접두어, sqlite_ 테이블은 차단된다.
    datetime은 'YYYY-MM-DD HH:MM:SS' 문자열이며 기준 시각 이후 행은 보이지 않는다.
    오류가 나면 "쿼리 오류: ..."가 오므로 쿼리를 고쳐 다시 시도한다.

    Args:
        query: 실행할 SELECT 쿼리
    """
    return run_sql(query, runtime.context.as_of)


SQL_TOOLS = [sql_db_list_tables, sql_db_schema, run_sql_guarded]
