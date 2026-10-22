"""장기 기억 저장소 (설비별 조사 기록·엔지니어 피드백, 엔지니어 선호).

[저장 방식] langgraph SqliteStore (data/memory.db, memory.persist.get_sqlite_store)
- 이유: langgraph-checkpoint-sqlite(체크포인터용으로 설치)에 SqliteStore가 함께 들어 있어, InMemoryStore와
  같은 BaseStore API(get/put/search)를 그대로 쓴다. 함수·도구(recall_machine_memory)·에이전트의 store= 인자를
  바꾸지 않고 저장 위치만 파일로 옮길 수 있으므로, app.db에 machine_memory 테이블을 따로 만들지 않았다.
- 재시작해도 기억이 남는다. 연결은 프로세스당 하나 (조사 에이전트·Supervisor가 같은 객체를 공유).

- 설비 기억: namespace ("machine", "<machine_id>"), key = 조사 기준 시각(as_of)
- recall은 현재 as_of보다 "이전" 기록만 돌려준다 (미래 기억 차단) - 저장 방식과 상관없이 recall에서 거른다.
"""

import hashlib
from datetime import datetime

import pandas as pd
from langgraph.store.base import BaseStore

from memory.persist import get_sqlite_store, open_sqlite_store

_STORE: BaseStore | None = None
SEARCH_LIMIT = 10_000  # search 기본 limit(10)으로 기록이 잘리지 않도록


def get_store() -> BaseStore:
    global _STORE
    if _STORE is None:
        _STORE = get_sqlite_store()
        _replay_work_reports(_STORE)
    return _STORE


def _replay_work_reports(store: BaseStore) -> None:
    """작업자 완료 보고(운영 DB)를 피드백으로 넣는다. 기억 파일이 생기기 전 보고도 남도록 시작할 때마다 확인하며,
    같은 설비·시각에 같은 문장·작성자의 피드백이 이미 있으면 다시 넣지 않는다."""
    try:
        from store import app_db

        reports = app_db.work_reports()
    except Exception:  # 운영 DB가 없거나 읽을 수 없으면 기억만 비어 있게 둔다
        return
    for r in sorted(reports, key=lambda r: r["reported_at"]):
        as_of = _norm(app_db.report_as_of(r["as_of_date"]))
        text = app_db.feedback_sentence(r)
        old = store.get(_machine_ns(r["machine_id"]), as_of)
        value = dict(old.value) if old else {
            "as_of": as_of, "comp": None, "label": None, "summary": "", "evidence_id": "", "feedback": [],
        }
        if any(f["text"] == text and f["engineer_id"] == r["reported_by"] for f in value.get("feedback", [])):
            continue
        value["feedback"] = [*value.get("feedback", []), {
            "text": text, "engineer_id": r["reported_by"], "as_of": as_of, "saved_at": r["reported_at"],
        }]
        store.put(_machine_ns(r["machine_id"]), as_of, value)


def reset_store() -> None:
    """테스트용: 실제 기억 파일 대신 빈 임시 저장소(SQLite 메모리 DB)로 바꾼다."""
    global _STORE
    _STORE = open_sqlite_store(":memory:")


def _norm(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")


def _machine_ns(machine_id: int) -> tuple[str, str]:
    return ("machine", str(int(machine_id)))


# ---------------------------------------------------------------- 설비 기억
def save_investigation(machine_id: int, as_of: str, finding: dict, evidence_id: str) -> dict:
    """조사 결과 저장. 같은 as_of로 다시 조사하면 덮어쓰되, 이미 달린 피드백은 유지한다."""
    store, as_of = get_store(), _norm(as_of)
    old = store.get(_machine_ns(machine_id), as_of)
    value = {
        "as_of": as_of,
        "comp": finding.get("comp"),
        "label": finding.get("label"),
        "summary": finding.get("summary", ""),
        "evidence_id": evidence_id,
        "feedback": old.value.get("feedback", []) if old else [],
    }
    store.put(_machine_ns(machine_id), as_of, value)
    return value


def save_feedback(machine_id: int, as_of: str, text: str, engineer_id: str) -> dict:
    """key=as_of 조사 기록에 엔지니어 피드백을 추가한다 (기록이 없으면 피드백만 있는 기록을 새로 만든다)."""
    store, as_of = get_store(), _norm(as_of)
    old = store.get(_machine_ns(machine_id), as_of)
    value = dict(old.value) if old else {
        "as_of": as_of, "comp": None, "label": None, "summary": "", "evidence_id": "", "feedback": [],
    }
    value["feedback"] = [*value.get("feedback", []), {
        "text": text, "engineer_id": engineer_id, "as_of": as_of,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }]
    store.put(_machine_ns(machine_id), as_of, value)
    return value


def recall(machine_id: int, as_of: str, limit: int = 3, include_same_time: bool = False) -> list[dict]:
    """현재 as_of보다 이전에 저장된 기록만 최신순으로 최대 limit개.

    include_same_time=True면 as_of와 같은 시각의 기록도 포함한다 (오늘 저장한 피드백을 채팅에서 볼 때).
    조사 에이전트는 기본값(False)을 써서 지금 조사 중인 자기 결과가 과거 기억으로 섞이지 않게 한다.
    """
    now = pd.Timestamp(as_of)
    items = get_store().search(_machine_ns(machine_id), limit=SEARCH_LIMIT)
    keep = (lambda t: t <= now) if include_same_time else (lambda t: t < now)
    past = [i.value for i in items if keep(pd.Timestamp(i.value["as_of"]))]
    return sorted(past, key=lambda v: v["as_of"], reverse=True)[:limit]


def feedback_machines(as_of: str) -> list[int]:
    """as_of 이하 기록 중 엔지니어 피드백이 있는 설비 번호."""
    now = pd.Timestamp(as_of)
    items = get_store().search(("machine",), limit=SEARCH_LIMIT)
    return sorted({int(i.namespace[1]) for i in items
                   if i.value.get("feedback") and pd.Timestamp(i.value["as_of"]) <= now})


# ---------------------------------------------------------------- 엔지니어 선호 (선택)
def _engineer_ns(engineer_id: str) -> tuple[str, str]:
    return ("engineer", engineer_id)


def save_preference(engineer_id: str, text: str) -> None:
    """엔지니어 선호 문장 저장 (같은 문장은 한 번만)."""
    key = "pref-" + hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:12]
    get_store().put(_engineer_ns(engineer_id), key, {
        "text": text.strip(), "saved_at": datetime.now().isoformat(timespec="seconds"),
    })


def get_preferences(engineer_id: str) -> list[str]:
    items = get_store().search(_engineer_ns(engineer_id), limit=SEARCH_LIMIT)
    return [i.value["text"] for i in sorted(items, key=lambda i: i.value["saved_at"])]
