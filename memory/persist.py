"""재시작 후에도 남는 에이전트 상태 (SQLite 파일).

- get_checkpointer(): 대화 기록·승인 대기 위치 (SqliteSaver, data/checkpoints.db)
- get_sqlite_store(): 장기 기억 (SqliteStore, data/memory.db) - memory.store.get_store()가 사용
- 연결은 프로세스당 하나만 만들어 재사용한다. Streamlit은 여러 스레드에서 부르므로 check_same_thread=False
  (SqliteSaver·SqliteStore는 내부 lock으로 한 번에 한 스레드만 연결을 쓴다).
- close_all(): 연결을 닫는다 (scripts/reset_runtime.py가 파일을 지우기 전)
- 환경 변수 FIXFIRST_STATE_DIR로 저장 폴더를 바꿀 수 있다 (테스트가 실제 기록을 건드리지 않도록).
"""

import os
import sqlite3
import threading
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

ROOT_DIR = Path(__file__).resolve().parent.parent
CHECKPOINTS_FILE = "checkpoints.db"
MEMORY_FILE = "memory.db"

_lock = threading.Lock()
_CHECKPOINTER: SqliteSaver | None = None
_STORE: SqliteStore | None = None


def state_dir() -> Path:
    path = Path(os.environ.get("FIXFIRST_STATE_DIR") or ROOT_DIR / "data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_checkpointer() -> SqliteSaver:
    global _CHECKPOINTER
    with _lock:
        if _CHECKPOINTER is None:
            conn = sqlite3.connect(state_dir() / CHECKPOINTS_FILE, check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()
            _CHECKPOINTER = saver
    return _CHECKPOINTER


def open_sqlite_store(path: Path | str) -> SqliteStore:
    """SqliteStore 하나를 연다 (":memory:"면 테스트용 임시 저장소)."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)  # SqliteStore는 autocommit 연결 기준
    store = SqliteStore(conn)
    store.setup()
    return store


def get_sqlite_store() -> SqliteStore:
    global _STORE
    with _lock:
        if _STORE is None:
            _STORE = open_sqlite_store(state_dir() / MEMORY_FILE)
    return _STORE


def close_all() -> None:
    """열어 둔 연결을 닫고 다음 호출 때 다시 열게 한다 (전체 초기화가 DB 파일을 지우기 전에 부른다)."""
    global _CHECKPOINTER, _STORE
    with _lock:
        for obj in (_CHECKPOINTER, _STORE):
            if obj is not None:
                with obj.lock:
                    obj.conn.close()
        _CHECKPOINTER = _STORE = None
