"""운영 DB (data/app.db): 작업 지시, 발주, 알림, 완료 보고, 작업자 교체 기록, 재고 변동.

- data/pdm.db(원본, 읽기 전용)는 건드리지 않는다. 운영 중에 생기는 기록만 여기에 쌓는다.
- 시각은 두 가지를 함께 저장한다: 데모 기준 시각(as_of_date / datetime / as_of)과 실제 처리 시각(created_at 등).
- 에이전트 도구는 기준 시각 이하 기록만 읽는다 (maint_rows, stock_delta).
- 데이터 에이전트(SQL)는 tools/sql_tools.py의 ops_ 뷰(기준 날짜로 자른 읽기 전용)로만 읽는다.
- 환경변수 APP_DB_PATH가 있으면 그 경로를 쓴다 (테스트용 임시 DB).
"""

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ["APP_DB_PATH"]) if os.environ.get("APP_DB_PATH") else ROOT_DIR / "data" / "app.db"
DRAFTS_DIR = ROOT_DIR / "outputs" / "drafts"
REGISTERED_DIR = ROOT_DIR / "outputs" / "registered"

WO_STATUS = ("ASSIGNED", "IN_PROGRESS", "DONE")
WO_STATUS_KO = {"ASSIGNED": "배정", "IN_PROGRESS": "진행 중", "DONE": "완료"}
PO_STATUS = ("REQUESTED", "RECEIVED")
CAUSE_TYPES = ["예측과 같음", "다른 부품", "센서·설치 문제", "이상 없음"]
ACTIONS = ["부품 교체", "조정·청소", "센서 재고정", "임시 조치", "조치 없음"]
WORKER_SOURCE = "작업자 보고"
VIRTUAL_REPORTER = "virtual"  # 교육용 가상 메모(1~9월)의 work_reports.reported_by 값 (data/virtual/make_field_notes.py가 써야 함)

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    wo_id TEXT PRIMARY KEY, as_of_date TEXT NOT NULL, machine_id INTEGER NOT NULL, comp TEXT NOT NULL,
    task_type TEXT, team TEXT, start TEXT, "end" TEXT, checklist TEXT, evidence_id TEXT, title TEXT,
    status TEXT NOT NULL DEFAULT 'ASSIGNED', approved_by TEXT, approved_at TEXT, started_at TEXT, done_at TEXT,
    created_at TEXT NOT NULL,
    paused TEXT  -- 긴급 점검으로 중단된 구간 "HH:MM~HH:MM" (진행 중 작업, 남은 시간은 긴급 뒤로)
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id TEXT PRIMARY KEY, as_of_date TEXT NOT NULL, comp TEXT NOT NULL, qty INTEGER NOT NULL, order_date TEXT,
    status TEXT NOT NULL DEFAULT 'REQUESTED', approved_by TEXT, approved_at TEXT, received_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, to_role TEXT, to_team TEXT, to_user TEXT, title TEXT NOT NULL,
    body TEXT, link_wo_id TEXT, as_of_date TEXT, created_at TEXT NOT NULL, read_at TEXT, kind TEXT,
    head TEXT, line TEXT  -- 카드 1줄("#45 베어링") / 2줄("긴급 점검 · 14:20 출동 · 2팀")
);
-- 작업자 주간 보고서 제출 (store/weekly_report.py). 같은 팀·주는 덮어쓴다 (status "수정 제출")
CREATE TABLE IF NOT EXISTS weekly_reports (
    team TEXT NOT NULL, week_start TEXT NOT NULL, author TEXT, summary TEXT, opinion TEXT, html_path TEXT,
    status TEXT NOT NULL, submitted_at TEXT NOT NULL, PRIMARY KEY (team, week_start)
);
-- 사람별 읽음 (팀 알림을 팀원 한 명이 읽어도 다른 팀원에게는 안 읽음). notifications.read_at은 호환용
CREATE TABLE IF NOT EXISTS notification_reads (
    notification_id INTEGER NOT NULL, user_id TEXT NOT NULL, read_at TEXT NOT NULL,
    PRIMARY KEY (notification_id, user_id)
);
CREATE TABLE IF NOT EXISTS work_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, wo_id TEXT NOT NULL UNIQUE, machine_id INTEGER NOT NULL,
    as_of_date TEXT NOT NULL, predicted_comp TEXT, actual_cause_type TEXT NOT NULL, actual_comp TEXT,
    action TEXT NOT NULL, duration_min INTEGER, parts_used INTEGER, checklist_done TEXT, memo TEXT,
    team TEXT, reported_by TEXT NOT NULL, reported_at TEXT NOT NULL, step_results TEXT, deviation TEXT
);
CREATE TABLE IF NOT EXISTS maint_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, datetime TEXT NOT NULL, machine_id INTEGER NOT NULL, comp TEXT NOT NULL,
    source TEXT NOT NULL, wo_id TEXT UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_by TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    user_id TEXT NOT NULL, as_of_date TEXT NOT NULL, thread_id TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, as_of_date)
);
CREATE TABLE IF NOT EXISTS stock_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, comp TEXT NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL,
    ref_id TEXT, as_of TEXT NOT NULL, created_at TEXT NOT NULL
);
-- 사용자가 올린 입력 데이터 (원본 pdm.db와 분리, 배치 단위로 끄거나 지울 수 있음). store/live_data.py가 원본과 합쳐 읽는다.
CREATE TABLE IF NOT EXISTS live_batches (
    batch_id TEXT PRIMARY KEY, machine_id INTEGER, kind TEXT NOT NULL, description TEXT, start TEXT, "end" TEXT,
    active INTEGER NOT NULL DEFAULT 1, created_by TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_telemetry (
    batch_id TEXT NOT NULL, datetime TEXT NOT NULL, machine_id INTEGER NOT NULL,
    volt REAL, rotate REAL, pressure REAL, vibration REAL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_telemetry_machine_datetime ON live_telemetry (machine_id, datetime);
CREATE TABLE IF NOT EXISTS live_errors (
    batch_id TEXT NOT NULL, datetime TEXT NOT NULL, machine_id INTEGER NOT NULL, error_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_errors_machine_datetime ON live_errors (machine_id, datetime);
-- 즉시 판단 결과 (live_ops.evaluate_machine). 정상은 저장하지 않는다.
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, machine_id INTEGER NOT NULL, comp TEXT, prob REAL,
    level TEXT NOT NULL, source TEXT NOT NULL, batch_id TEXT, status TEXT NOT NULL, evidence_id TEXT, wo_id TEXT,
    message TEXT, investigating INTEGER NOT NULL DEFAULT 0, investigated_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_machine ON alerts (machine_id, status);
-- 1초 실시간 스트림 원시값 (scripts/stream_sim.py가 쓰고 최근 10분만 보관). sim_ts = 기준 시각 + 경과 초
CREATE TABLE IF NOT EXISTS live_stream (
    sim_ts TEXT NOT NULL, wall_ts TEXT NOT NULL, machine_id INTEGER NOT NULL,
    volt REAL, rotate REAL, pressure REAL, vibration REAL, injected TEXT, injected_sensor TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_stream_machine_wall ON live_stream (machine_id, wall_ts);
"""

_lock = threading.Lock()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _date(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y-%m-%d")


def _ts(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def connect(path: Path | None = None):
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # 화면 프로세스와 스트림 생성기 프로세스가 동시에 읽고 쓴다
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn) -> None:
    """이전 버전 DB에 새 열 추가 (purchase_orders.order_date = 희망 발주일,
    work_reports.step_results = 점검 단계별 결과 JSON, work_reports.deviation = 지시와 달랐던 점,
    work_orders.urgent = 긴급 점검 자동 배정 작업)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(purchase_orders)")}
    if "order_date" not in cols:
        conn.execute("ALTER TABLE purchase_orders ADD COLUMN order_date TEXT")
        conn.execute("UPDATE purchase_orders SET order_date = as_of_date WHERE order_date IS NULL")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(work_reports)")}
    for col in ("step_results", "deviation"):
        if col not in cols:
            conn.execute(f"ALTER TABLE work_reports ADD COLUMN {col} TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(work_orders)")}
    if "urgent" not in cols:  # 긴급 점검(UWO) 표시. 기존 행은 0
        conn.execute("ALTER TABLE work_orders ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0")
    _add_missing_columns(conn)
    # 종류가 없거나 예전 종류 이름(info 등)인 옛 알림, head/line이 없는 알림은 제목으로 채운다
    old = conn.execute(f"SELECT id, title, kind, head FROM notifications WHERE kind IS NULL OR head IS NULL"
                       f" OR kind NOT IN ({', '.join('?' * len(KINDS))})", KINDS).fetchall()
    for i, t, k, h in old:
        kind = k if k in KINDS and not (k == "urgent" and "자동 배정했습니다" in (t or "")) else classify_kind(t)
        head, line = split_title(t)
        conn.execute("UPDATE notifications SET kind = ?, head = COALESCE(head, ?), line = COALESCE(line, ?)"
                     " WHERE id = ?", (kind, head, line if h is None else None, i))


_SCHEMA_COLUMNS: dict[str, list[tuple]] | None = None


def _schema_columns() -> dict[str, list[tuple]]:
    """SCHEMA가 정의하는 테이블별 열 (name, type, notnull, default). 메모리 DB에 한 번 만들어 읽는다."""
    global _SCHEMA_COLUMNS
    if _SCHEMA_COLUMNS is None:
        mem = sqlite3.connect(":memory:")
        try:
            mem.executescript(SCHEMA)
            tables = [r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type = 'table'"
                                                " AND name NOT LIKE 'sqlite_%'")]
            _SCHEMA_COLUMNS = {t: [(r[1], r[2], r[3], r[4]) for r in mem.execute(f"PRAGMA table_info({t})")]
                               for t in tables}
        finally:
            mem.close()
    return _SCHEMA_COLUMNS


def _add_missing_columns(conn) -> None:
    """예전 버전 app.db에 SCHEMA의 새 열이 없으면 추가한다 (예: alerts.investigated_at, live_stream.injected_sensor).
    CREATE TABLE IF NOT EXISTS는 이미 있는 테이블의 열을 바꾸지 않으므로, 열을 추가할 때마다 여기서 자동으로 맞춘다.
    NOT NULL인데 기본값이 없는 열은 ALTER로 붙일 수 없어 NOT NULL을 빼고 추가한다."""
    for table, cols in _schema_columns().items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ctype, notnull, default in cols:
            if name in have:
                continue
            spec = f'"{name}" {ctype}'.rstrip()
            if default is not None:
                spec += f" {'NOT NULL ' if notnull else ''}DEFAULT {default}"
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {spec}")


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _wo(row: dict) -> dict:
    return {**row, "checklist": json.loads(row["checklist"] or "[]")}


# ---------------------------------------------------------------- 승인 → 작업 지시·발주·알림
def wo_title(d: dict) -> str:
    from config import COMP_LABEL_KO  # 순환 import 방지

    task = {"replace": "교체", "temporary_check": "임시 점검", "sensor_check": "센서 점검"}.get(d.get("task_type"), "작업")
    return f"#{d['machine_id']} {COMP_LABEL_KO.get(d['comp'], d['comp'])} {task}"


def create_work_order(draft: dict, approved_by: str) -> bool:
    """승인된 작업 지시서 초안 -> work_orders(ASSIGNED) + 해당 팀 알림. 이미 있으면 아무것도 하지 않고 False."""
    created = now()
    with _lock, connect() as conn:
        cur = conn.execute(
            'INSERT OR IGNORE INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end",'
            " checklist, evidence_id, title, status, approved_by, approved_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ASSIGNED', ?, ?, ?)",
            (draft["draft_id"], _date(draft["as_of"]), draft["machine_id"], draft["comp"], draft.get("task_type"),
             draft.get("team"), draft.get("start"), draft.get("end"),
             json.dumps(draft.get("checklist", []), ensure_ascii=False), (draft.get("evidence_ids") or [None])[0],
             wo_title(draft), approved_by, created, created),
        )
        if cur.rowcount == 0:
            return False
        when = f" ({draft['team']} {draft['start']}~{draft['end']})" if draft.get("team") else ""
        from config import COMP_LABEL_KO  # 순환 import 방지

        task = wo_title(draft).split(" ", 2)[-1]  # "#90 모터/전원부 교체" -> "교체"
        slot = f"{draft['team']} {draft['start']}~{draft['end']}" if draft.get("team") else "시간 미정"
        _notify(conn, title=f"새 작업: {wo_title(draft)}{when}", body=f"작업 지시 {draft['draft_id']} · 승인 {approved_by}",
                to_role="worker", to_team=draft.get("team"), link_wo_id=draft["draft_id"], as_of_date=_date(draft["as_of"]),
                kind="task", head=f"#{draft['machine_id']} {COMP_LABEL_KO.get(draft['comp'], draft['comp'])}",
                line=f"{task} · {slot}")
    return True


def _order_date(draft: dict) -> str:
    """희망 발주일 (예전 초안에는 없으므로 기준 날짜)."""
    return draft.get("order_date") or _date(draft["as_of"])


def create_purchase_order(draft: dict, approved_by: str) -> bool:
    """승인된 발주 요청 초안 -> purchase_orders(REQUESTED) + 관리자 알림. 이미 있으면 False."""
    created = now()
    with _lock, connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO purchase_orders (po_id, as_of_date, comp, qty, order_date, status, approved_by,"
            " approved_at, created_at) VALUES (?, ?, ?, ?, ?, 'REQUESTED', ?, ?, ?)",
            (draft["draft_id"], _date(draft["as_of"]), draft["comp"], int(draft["qty"]), _order_date(draft),
             approved_by, created, created),
        )
        if cur.rowcount == 0:
            return False
        _notify(conn, title=f"발주 승인: {draft['comp']} {draft['qty']}개 (희망 발주일 {_order_date(draft)})",
                body=f"발주 {draft['draft_id']} · 승인 {approved_by}",
                to_role="admin", as_of_date=_date(draft["as_of"]), kind="stock",  # 입고 처리 필요
                head=f"{draft['comp']} {draft['qty']}개", line=f"발주 승인 · 희망 발주일 {_order_date(draft)} · 입고 처리 필요")
    return True


def receive_purchase_order(po_id: str, as_of: str) -> bool:
    """발주 입고: RECEIVED + 재고 +qty (기준 시각 as_of로 기록)."""
    with _lock, connect() as conn:
        po = conn.execute("SELECT * FROM purchase_orders WHERE po_id = ?", (po_id,)).fetchone()
        if po is None or po["status"] == "RECEIVED":
            return False
        conn.execute("UPDATE purchase_orders SET status = 'RECEIVED', received_at = ? WHERE po_id = ?", (now(), po_id))
        conn.execute("INSERT INTO stock_ledger (comp, delta, reason, ref_id, as_of, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (po["comp"], int(po["qty"]), "발주 입고", po_id, _ts(as_of), now()))
    return True


# ---------------------------------------------------------------- 알림
# 종류 -> 배지 이름 -> 모양 단계(tier, 화면 글자로는 나오지 않음)
#   urgent 긴급(처리해야 사라짐) / todo 새 작업·승인 대기·입고(종류·팀별 묶음) / plain 완료·조사 결과·배정·안내(한 줄씩)
KINDS = ("urgent", "task", "approval", "stock", "done", "finding", "assign", "notice")
KIND_LABEL = {"urgent": "긴급", "task": "새 작업", "approval": "승인 대기", "stock": "입고", "done": "완료",
              "finding": "조사 결과", "assign": "배정", "notice": "안내"}
KIND_TIER = {"urgent": "urgent", "task": "todo", "approval": "todo", "stock": "todo",
             "done": "plain", "finding": "plain", "assign": "plain", "notice": "plain"}
TIERS = ("urgent", "todo", "plain")
_KIND_RULES = [  # 옛 알림(종류 없음)의 제목 -> 종류. 위에서부터 먼저 맞는 것 (접두어 또는 포함)
    ("긴급 점검:", "urgent"), ("센서 점검:", "urgent"), ("*다시 알림", "urgent"), ("*자동 배정했습니다", "assign"),
    ("새 작업:", "task"), ("재고 0 발주 승인 대기", "approval"), ("*승인 대기", "approval"), ("발주 승인:", "stock"),
    ("완료 보고:", "done"), ("*조사 결과", "finding"),
]
# head로 뽑을 부품 이름 ("#45 베어링"의 "베어링")
_COMP_WORDS = {"모터/전원부", "구동부", "밸브/펌프", "베어링", "motor", "drive", "pump", "bearing"}


def classify_kind(title: str | None) -> str:
    """제목으로 종류를 정한다 (kind 없이 저장된 옛 알림용). 맞는 규칙이 없으면 안내."""
    t = str(title or "")
    for rule, kind in _KIND_RULES:
        if (rule[1:] in t) if rule.startswith("*") else t.startswith(rule):
            return kind
    return "notice"


def split_title(title: str | None) -> tuple[str, str]:
    """제목 -> (head, line). head = "#45 베어링"(설비 번호 + 부품, 없으면 대상), line = 나머지를 " · "로 이은 한 줄.
    "(근거 ...)" 같은 부가 정보는 버린다 (body에만 둔다).
    예: "긴급 점검: #45 베어링 — 09:00 출동 (근거 E1)" -> ("#45 베어링", "긴급 점검 · 09:00 출동")"""
    t = re.sub(r"\s*\(근거[^)]*\)", "", str(title or "")).strip()
    m = re.search(r"#\d+(?:\s+(\S+))?", t)
    if m:
        head = m.group(0) if m.group(1) in _COMP_WORDS else m.group(0).split()[0]
        end = m.start() + len(head)
        rest = t[:m.start()] + " — " + t[end:]  # 라벨 "긴급 점검:" 뒤 콜론은 아래에서 구분자로
    elif ": " in t:
        label, rest_ = t.split(": ", 1)
        head = re.sub(r"\s*\([^)]*\)", "", rest_).strip() or label.strip()
        rest = label + "".join(f" — {x}" for x in re.findall(r"\(([^)]*)\)", rest_))
        return head, " · ".join(p.strip() for p in re.split(r":\s|:$|—", rest) if p.strip())
    else:
        return t, ""
    rest = re.sub(r"\(([^)]*)\)", r" — \1", rest)
    return head, " · ".join(p.strip() for p in re.split(r":\s|:$|—", rest) if p.strip())


def _notify(conn, title: str, body: str = "", to_role: str | None = None, to_team: str | None = None,
            to_user: str | None = None, link_wo_id: str | None = None, as_of_date: str | None = None,
            kind: str | None = None, head: str | None = None, line: str | None = None) -> None:
    """알림 1건. 카드 1줄 head("#45 베어링"), 2줄 line("긴급 점검 · 14:20 출동 · 2팀"). 없으면 title에서 뽑는다.
    근거 번호 같은 부가 정보는 body로만 (카드에는 안 보이고 마우스를 올리거나 [열기] 뒤에서 보인다)."""
    kind = kind if kind in KINDS else classify_kind(title)
    if head is None or line is None:
        h, ln = split_title(title)
        head, line = head if head is not None else h, line if line is not None else ln
    conn.execute("INSERT INTO notifications (to_role, to_team, to_user, title, body, link_wo_id, as_of_date, created_at,"
                 " kind, head, line) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (to_role, to_team, to_user, title, body, link_wo_id, as_of_date, now(), kind, head, line))


def notify(**kwargs) -> None:
    with _lock, connect() as conn:
        _notify(conn, **kwargs)


def _audience(user: dict) -> tuple[str, tuple]:
    return ("((n.to_role = ? AND (n.to_team IS NULL OR n.to_team = ?)) OR n.to_user = ?)",
            (user.get("role"), user.get("team"), user.get("id")))


def _user_rows(user: dict, extra: str = "", params: tuple = (), limit: int | None = None) -> list[dict]:
    """그 사람이 받는 알림 (최신순). read_at = 그 사람이 읽은 시각 (사람별), 연결 작업의 상태·출동 시각을 함께.
    각 행에 label(배지 이름)·tier(모양 단계)를 붙인다."""
    where, aud = _audience(user)
    rows = _rows(
        "SELECT n.id, n.to_role, n.to_team, n.to_user, n.title, n.body, n.link_wo_id, n.as_of_date, n.created_at,"
        " n.kind, n.head, n.line, r.read_at, w.status AS wo_status, w.started_at AS wo_started_at"
        " FROM notifications n LEFT JOIN notification_reads r ON r.notification_id = n.id AND r.user_id = ?"
        f" LEFT JOIN work_orders w ON w.wo_id = n.link_wo_id WHERE {where}{extra} ORDER BY n.id DESC"
        + (" LIMIT ?" if limit else ""), (str(user.get("id")), *aud, *params, *((limit,) if limit else ())))
    for r in rows:
        r["kind"] = r["kind"] if r["kind"] in KINDS else classify_kind(r["title"])
        if r["head"] is None:
            r["head"], r["line"] = split_title(r["title"])
        r["label"], r["tier"] = KIND_LABEL[r["kind"]], KIND_TIER[r["kind"]]
    return rows


def notifications_for(user: dict, limit: int = 10) -> list[dict]:
    """최신 알림 (read_at은 그 사람 기준)."""
    return _user_rows(user, limit=limit)


def unread_count(user: dict) -> int:
    """그 사람이 아직 읽지 않은 알림 수 (숨김 규칙 없이 전부)."""
    return sum(r["read_at"] is None for r in _user_rows(user))


def mark_read_many(ids, user: dict) -> int:
    """그 사람 기준 읽음 (받는 사람이 아닌 알림은 무시). 새로 읽음 처리한 수."""
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    where, aud = _audience(user)
    stamp = now()
    with _lock, connect() as conn:
        mine = [r[0] for r in conn.execute(
            f"SELECT n.id FROM notifications n WHERE n.id IN ({', '.join('?' * len(ids))}) AND {where}", (*ids, *aud))]
        n = conn.executemany("INSERT OR IGNORE INTO notification_reads (notification_id, user_id, read_at)"
                             " VALUES (?, ?, ?)", [(i, str(user.get("id")), stamp) for i in mine]).rowcount
        conn.executemany("UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",  # 호환용 (첫 읽음)
                         [(stamp, i) for i in mine])
    return n


def mark_read(notification_id: int, user: dict) -> None:
    mark_read_many([notification_id], user)


def mark_all_read(user: dict) -> int:
    """안 읽은 알림을 모두 읽음. 긴급은 읽음으로 사라지지 않으므로(출동 확인 후 사라짐) 건드리지 않는다."""
    return mark_read_many([r["id"] for r in _user_rows(user) if r["read_at"] is None and r["kind"] != "urgent"], user)


def _handled(r: dict) -> bool:
    """긴급 알림이 처리됐나: 연결 작업을 출동 확인(시작)했거나 완료. 연결 작업이 없으면 읽음으로 처리."""
    if r["link_wo_id"] and r["wo_status"] is not None:
        return bool(r["wo_started_at"]) or r["wo_status"] != "ASSIGNED"
    return r["read_at"] is not None


def _card(items: list[dict]) -> dict:
    """같은 작업 알림 묶음 -> 카드 1장: 가장 최신 알림 내용 + "외 N건", 시각도 최신 기준.
    종류는 묶음에서 가장 급한 것 (긴급 > 할 일 > 그 밖)."""
    top = items[0]
    kind = min((i["kind"] for i in items), key=lambda k: (TIERS.index(KIND_TIER[k]), k != top["kind"]))
    more = len(items) - 1
    return {"id": top["id"], "ids": [i["id"] for i in items], "kind": kind, "label": KIND_LABEL[kind],
            "tier": KIND_TIER[kind], "head": top["head"], "line": top["line"], "more": more,
            "extra": f"외 {more}건" if more else "", "title": top["title"] + (f" 외 {more}건" if more else ""),
            "body": top["body"], "link_wo_id": top["link_wo_id"], "created_at": top["created_at"],
            "to_team": top["to_team"]}


def _cards(rows: list[dict]) -> list[dict]:
    """link_wo_id가 같은 알림을 한 장으로 (최신순 유지)."""
    by_wo: dict[str, list[dict]] = {}
    order: list[list[dict]] = []
    for r in rows:
        if not r["link_wo_id"]:
            order.append([r])
            continue
        if r["link_wo_id"] not in by_wo:
            by_wo[r["link_wo_id"]] = []
            order.append(by_wo[r["link_wo_id"]])
        by_wo[r["link_wo_id"]].append(r)
    return [_card(items) for items in order]


def inbox(user: dict, day: str | None = None) -> dict:
    """사이드바 알림 (화면은 이것만 부른다). {"urgent": [카드], "todo_groups": [묶음], "plain": [카드], "unread": N}

    - 보이는 것 = 내가 안 읽은 알림 전부 (개수 제한 없음). 읽으면 사라진다.
    - 긴급은 예외: 읽어도 남고 연결 작업이 처리돼야(출동 확인·시작 또는 완료) 사라진다. 이전 날짜여도 남는다.
    - 기준 날짜(day)보다 이전 날짜 알림은 숨긴다.
    - 같은 작업(link_wo_id) 알림은 카드 한 장 ("외 N건", ids에 묶인 알림 전부).
    - 새 작업·승인 대기·입고는 (종류, 팀)마다 묶음 {"kind", "team", "label", "count", "items", "ids", "title"}.
    - 완료·조사 결과·배정·안내는 묶지 않고 카드 한 장씩, 최신이 위.
    - unread = 긴급 카드 수 + 묶음 안 카드 수 + plain 카드 수."""
    day = _date(day) if day else None
    visible = []
    for r in _user_rows(user):
        if r["kind"] == "urgent":
            if not _handled(r):
                visible.append(r)
        elif r["read_at"] is None and not (day and r["as_of_date"] and r["as_of_date"] < day):
            visible.append(r)
    urgent, plain, groups = [], [], {}
    for card in _cards(visible):
        if card["tier"] == "urgent":
            urgent.append(card)
        elif card["tier"] == "todo":
            groups.setdefault((card["kind"], card["to_team"]), []).append(card)
        else:
            plain.append(card)
    todo_groups = []
    for (kind, team), cards in sorted(groups.items(), key=lambda kv: (KINDS.index(kv[0][0]), kv[0][1] or "")):
        todo_groups.append({"kind": kind, "team": team, "label": KIND_LABEL[kind], "count": len(cards),
                            "items": cards, "ids": [i for c in cards for i in c["ids"]],
                            "title": f"{len(cards)}건" + (f" ({team})" if team else "")})
    unread = len(urgent) + sum(g["count"] for g in todo_groups) + len(plain)
    return {"urgent": urgent, "todo_groups": todo_groups, "plain": plain, "unread": unread}


def notification_history(user: dict, day: str | None = None, kind: str | None = None, unread_only: bool = False,
                         limit: int = 200, since: str | None = None) -> list[dict]:
    """전체 보기용: 사이드바에서 사라진 알림(읽음, 이전 날짜, 처리된 긴급)도 모두. day = 그 기준 날짜만,
    since = 그 날짜 이후. 각 행에 "read"(그 사람이 읽었나)를 붙인다."""
    extra, params = "", []
    if day:
        extra, params = extra + " AND n.as_of_date = ?", [*params, _date(day)]
    if since:
        extra, params = extra + " AND n.as_of_date >= ?", [*params, _date(since)]
    rows = _user_rows(user, extra, tuple(params))
    rows = [r for r in rows if (kind is None or r["kind"] == kind) and not (unread_only and r["read_at"])]
    return [{**r, "read": r["read_at"] is not None} for r in rows[:limit]]


# ---------------------------------------------------------------- 작업 지시 조회·상태
def get_work_order(wo_id: str) -> dict | None:
    rows = _rows("SELECT * FROM work_orders WHERE wo_id = ?", (wo_id,))
    return _wo(rows[0]) if rows else None


def work_orders(as_of_date: str | None = None, team: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM work_orders WHERE 1 = 1", []
    if as_of_date:
        sql, params = sql + " AND as_of_date = ?", [*params, _date(as_of_date)]
    if team:
        sql, params = sql + " AND team = ?", [*params, team]
    return [_wo(r) for r in _rows(sql + ' ORDER BY as_of_date, start, team, wo_id', tuple(params))]


def work_order_dates(team: str | None = None) -> list[str]:
    sql = "SELECT DISTINCT as_of_date FROM work_orders" + (" WHERE team = ?" if team else "") + " ORDER BY as_of_date"
    return [r["as_of_date"] for r in _rows(sql, (team,) if team else ())]


def set_started(wo_id: str) -> None:
    with _lock, connect() as conn:
        conn.execute("UPDATE work_orders SET status = 'IN_PROGRESS', started_at = ? WHERE wo_id = ? AND status = 'ASSIGNED'",
                     (now(), wo_id))


def save_report(wo: dict, report: dict, maint_datetime: str | None, parts_comp: str | None) -> int:
    """완료 보고 저장(한 트랜잭션): work_reports + work_orders DONE + (교체면) maint_log + (사용 부품) stock_ledger."""
    t = now()
    with _lock, connect() as conn:
        cur = conn.execute(
            "INSERT INTO work_reports (wo_id, machine_id, as_of_date, predicted_comp, actual_cause_type, actual_comp,"
            " action, duration_min, parts_used, checklist_done, memo, team, reported_by, reported_at, step_results,"
            " deviation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (wo["wo_id"], wo["machine_id"], wo["as_of_date"], wo["comp"], report["actual_cause_type"],
             report["actual_comp"], report["action"], int(report["duration_min"]), int(report["parts_used"]),
             json.dumps(report.get("checklist_done", []), ensure_ascii=False), report.get("memo", ""), wo["team"],
             report["reported_by"], t, json.dumps(report.get("step_results", []), ensure_ascii=False),
             report.get("deviation", "")),
        )
        conn.execute("UPDATE work_orders SET status = 'DONE', done_at = ? WHERE wo_id = ?", (t, wo["wo_id"]))
        if maint_datetime:
            conn.execute("INSERT OR IGNORE INTO maint_log (datetime, machine_id, comp, source, wo_id, created_at)"
                         " VALUES (?, ?, ?, ?, ?, ?)",
                         (maint_datetime, wo["machine_id"], report["actual_comp"], WORKER_SOURCE, wo["wo_id"], t))
        if parts_comp and int(report["parts_used"]) > 0:
            conn.execute("INSERT INTO stock_ledger (comp, delta, reason, ref_id, as_of, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                         (parts_comp, -int(report["parts_used"]), "작업 사용", wo["wo_id"],
                          maint_datetime or f"{wo['as_of_date']} {wo['end'] or '18:00'}:00", t))
        return cur.lastrowid


def work_reports(team: str | None = None, as_of_date: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM work_reports WHERE 1 = 1", []
    if team:
        sql, params = sql + " AND team = ?", [*params, team]
    if as_of_date:
        sql, params = sql + " AND as_of_date = ?", [*params, _date(as_of_date)]
    rows = _rows(sql + " ORDER BY as_of_date DESC, reported_at DESC", tuple(params))
    return [{**r, "checklist_done": json.loads(r["checklist_done"] or "[]"),
             "step_results": json.loads(r.get("step_results") or "[]")} for r in rows]


def purchase_orders(as_of_date: str | None = None) -> list[dict]:
    if as_of_date:
        return _rows("SELECT * FROM purchase_orders WHERE as_of_date = ? ORDER BY po_id", (_date(as_of_date),))
    return _rows("SELECT * FROM purchase_orders ORDER BY as_of_date, po_id")


# ---------------------------------------------------------------- 에이전트 도구용 (기준 시각 이하만)
def maint_rows(machine_id: int, as_of: str) -> pd.DataFrame:
    """작업자 보고 교체 기록 (datetime <= as_of). 열: datetime, comp, source"""
    rows = _rows("SELECT datetime, comp, source FROM maint_log WHERE machine_id = ? AND datetime <= ? ORDER BY datetime",
                 (int(machine_id), _ts(as_of)))
    df = pd.DataFrame(rows, columns=["datetime", "comp", "source"])
    df["datetime"] = pd.to_datetime(df["datetime"], format="%Y-%m-%d %H:%M:%S")
    return df


def stock_delta(as_of: str) -> dict[str, int]:
    """부품별 재고 변동 합계 (as_of 이하 기록만)."""
    rows = _rows("SELECT comp, SUM(delta) AS d FROM stock_ledger WHERE as_of <= ? GROUP BY comp", (_ts(as_of),))
    return {r["comp"]: int(r["d"]) for r in rows}


def stock_ledger() -> list[dict]:
    return _rows("SELECT * FROM stock_ledger ORDER BY id")


def maint_log() -> list[dict]:
    return _rows("SELECT * FROM maint_log ORDER BY datetime")


# ---------------------------------------------------------------- 공유 기준 날짜 (관리자가 정하고 작업자가 따른다)
def set_current_day(day: str, updated_by: str | None = None) -> None:
    """관리자가 고른 데모 기준 날짜(YYYY-MM-DD)를 저장한다. 작업자 화면은 이 날짜를 따른다."""
    with _lock, connect() as conn:
        conn.execute("INSERT INTO app_state (key, value, updated_by, updated_at) VALUES ('current_day', ?, ?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,"
                     " updated_at = excluded.updated_at", (_date(day), updated_by, now()))


def set_state(key: str, value, updated_by: str | None = None) -> None:
    """app_state에 JSON 값을 저장한다 (스트림 명령 등)."""
    with _lock, connect() as conn:
        conn.execute("INSERT INTO app_state (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,"
                     " updated_at = excluded.updated_at", (key, json.dumps(value, ensure_ascii=False), updated_by, now()))


def get_state(key: str, default=None):
    rows = _rows("SELECT value FROM app_state WHERE key = ?", (key,))
    if not rows:
        return default
    try:
        return json.loads(rows[0]["value"])
    except ValueError:
        return rows[0]["value"]


def current_day() -> dict | None:
    """{"day": "YYYY-MM-DD", "updated_by", "updated_at"} 또는 None (관리자가 아직 날짜를 정하지 않음)."""
    rows = _rows("SELECT value AS day, updated_by, updated_at FROM app_state WHERE key = 'current_day'")
    return rows[0] if rows else None


def open_orders_before(day: str, team: str) -> list[dict]:
    """기준 날짜 이전에 지시됐지만 아직 완료되지 않은 우리 팀 작업 (날짜가 넘어가도 사라지지 않게)."""
    return [_wo(r) for r in _rows("SELECT * FROM work_orders WHERE team = ? AND as_of_date < ? AND status != 'DONE'"
                                  ' ORDER BY as_of_date, start, wo_id', (team, _date(day)))]


# ---------------------------------------------------------------- 화면 세션 (사용자·기준 날짜 -> 에이전트 thread_id)
def session_thread(user_id: str, as_of_date: str, new_thread_id: str) -> str:
    """이 사용자의 기준 날짜 thread_id. 없으면 new_thread_id를 저장해 돌려준다 (재접속해도 같은 대화를 이어 씀)."""
    day = _date(as_of_date)
    with _lock, connect() as conn:
        conn.execute("INSERT OR IGNORE INTO sessions (user_id, as_of_date, thread_id, created_at) VALUES (?, ?, ?, ?)",
                     (user_id, day, new_thread_id, now()))
        return conn.execute("SELECT thread_id FROM sessions WHERE user_id = ? AND as_of_date = ?",
                            (user_id, day)).fetchone()["thread_id"]



# ---------------------------------------------------------------- 마이그레이션·초기화
def migrate_registered() -> dict[str, int]:
    """기존 outputs/registered/*.json 등록 기록 -> work_orders / purchase_orders (알림은 만들지 않음, 여러 번 실행해도 안전)."""
    counts = {"work_orders": 0, "purchase_orders": 0}
    for path in sorted(REGISTERED_DIR.glob("*.json")):
        drafts_path = DRAFTS_DIR / path.name
        drafts = json.loads(drafts_path.read_text(encoding="utf-8")) if drafts_path.exists() else {}
        for reg in json.loads(path.read_text(encoding="utf-8")):
            d = drafts.get(reg["draft_id"])
            if d is None or d.get("status") != "APPROVED":
                continue
            approver = d.get("decided_by") or reg.get("engineer_id") or "-"
            approved_at = reg.get("registered_at") or now()
            with _lock, connect() as conn:
                if d["type"] == "work_order":
                    cur = conn.execute(
                        'INSERT OR IGNORE INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end",'
                        " checklist, evidence_id, title, status, approved_by, approved_at, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ASSIGNED', ?, ?, ?)",
                        (d["draft_id"], _date(d["as_of"]), d["machine_id"], d["comp"], d.get("task_type"), d.get("team"),
                         d.get("start"), d.get("end"), json.dumps(d.get("checklist", []), ensure_ascii=False),
                         (d.get("evidence_ids") or [None])[0], wo_title(d), approver, approved_at, now()))
                    counts["work_orders"] += cur.rowcount
                else:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO purchase_orders (po_id, as_of_date, comp, qty, order_date, status,"
                        " approved_by, approved_at, created_at) VALUES (?, ?, ?, ?, ?, 'REQUESTED', ?, ?, ?)",
                        (d["draft_id"], _date(d["as_of"]), d["comp"], int(d["qty"]), _order_date(d), approver,
                         approved_at, now()))
                    counts["purchase_orders"] += cur.rowcount
    return counts


def clear_day(as_of: str) -> int:
    """기준 날짜의 운영 기록(작업 지시·발주·알림·완료 보고·교체 기록·재고 변동)을 지운다. 지운 행 수."""
    day = _date(as_of)
    with _lock, connect() as conn:
        wo_ids = [r["wo_id"] for r in conn.execute("SELECT wo_id FROM work_orders WHERE as_of_date = ?", (day,))]
        po_ids = [r["po_id"] for r in conn.execute("SELECT po_id FROM purchase_orders WHERE as_of_date = ?", (day,))]
        ids = wo_ids + po_ids
        n = 0
        if ids:
            marks = ",".join("?" * len(ids))
            n += conn.execute(f"DELETE FROM stock_ledger WHERE ref_id IN ({marks})", ids).rowcount
            n += conn.execute(f"DELETE FROM maint_log WHERE wo_id IN ({marks})", ids).rowcount
        n += conn.execute("DELETE FROM work_reports WHERE as_of_date = ?", (day,)).rowcount
        conn.execute("DELETE FROM notification_reads WHERE notification_id IN"
                     " (SELECT id FROM notifications WHERE as_of_date = ?)", (day,))
        n += conn.execute("DELETE FROM notifications WHERE as_of_date = ?", (day,)).rowcount
        n += conn.execute("DELETE FROM work_orders WHERE as_of_date = ?", (day,)).rowcount
        n += conn.execute("DELETE FROM purchase_orders WHERE as_of_date = ?", (day,)).rowcount
    return n


if __name__ == "__main__":
    print("registered -> app.db:", migrate_registered())


# ---------------------------------------------------------------- 완료 보고 -> 장기 기억 문장
AS_OF_HOUR = "06:00:00"  # 데모 기준 시각은 매일 06시 (작업 지시의 as_of)


def report_as_of(as_of_date: str) -> str:
    return f"{as_of_date} {AS_OF_HOUR}"


MEMO_CORE_CHARS = 120  # 장기 기억 문장에 넣는 메모 길이 (전체 메모는 현장 기록 색인에)


def memo_core(memo: str, limit: int = MEMO_CORE_CHARS) -> str:
    """메모 핵심: 줄바꿈·머리표를 한 줄로 합치고 앞부분만 (길면 …)."""
    text = " ".join(line.strip(" -·*•\t") for line in (memo or "").splitlines() if line.strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def feedback_sentence(r: dict) -> str:
    """예: "작업자 보고(2팀): 예측과 같음 — 베어링 교체 완료, 95분, 이상 발견: 하우징 온도 측정(68도, 평소보다 높음),
    메모: 하우징 온도 높았음 ..." """
    from config import COMP_LABEL_KO

    comp = COMP_LABEL_KO.get(r.get("actual_comp"), r.get("actual_comp") or "")
    action = {"부품 교체": f"{comp} 교체 완료", "조치 없음": "조치 없음"}.get(r["action"], f"{comp} {r['action']} 완료")
    abnormal = [s for s in r.get("step_results") or [] if s.get("result") == "이상 발견"]
    found = ("이상 발견: " + "; ".join(f"{s['action']}({s['observation']})" if s.get("observation") else s["action"]
                                     for s in abnormal) + ", ") if abnormal else ""
    memo = f"메모: {memo_core(r['memo'])}" if r.get("memo") else ""
    tail = f", {found}{memo}".rstrip(", ")
    return f"작업자 보고({r.get('team') or '-'}): {r['actual_cause_type']} — {action}, {r['duration_min']}분{tail}"
