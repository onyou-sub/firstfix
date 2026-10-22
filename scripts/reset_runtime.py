"""운영 기록 전체 초기화: 테스트하며 쌓인 결과·운영 DB·에이전트 상태를 지우고 처음 상태로 되돌린다.

1. 백업: 지울 대상 전부를 backups/{YYYYMMDD_HHMMSS}.zip에 저장 (Chroma field_notes는 문서·임베딩 JSON으로)
2. 삭제: outputs/ 아래 전부, evidence/ 아래 전부, data/app.db, data/checkpoints.db, data/memory.db,
   Chroma 컬렉션 "field_notes" (컬렉션만 지우고 "manuals"와 data/chroma 폴더는 그대로)
3. 보호: PROTECTED 경로는 지우지 않는다 (삭제 직전에 _guard가 다시 확인)
4. 다시 채우기: 빈 app.db 스키마 -> data/virtual/make_field_notes.py(있으면) -> field_notes 색인
   checkpoints.db·memory.db는 비워 두고, 에이전트가 처음 쓸 때 다시 만든다.

실행: uv run python scripts/reset_runtime.py         지울 목록을 보여 주고 "yes"를 입력하면 실행
      uv run python scripts/reset_runtime.py --yes   확인 없이 실행
설정 페이지의 "전체 초기화"도 reset_runtime()을 그대로 부른다.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

DATA_DIR = ROOT_DIR / "data"
BACKUP_DIR = ROOT_DIR / "backups"
CHROMA_DIR = DATA_DIR / "chroma"
OUTPUTS_DIR = ROOT_DIR / "outputs"
OUTPUT_SUBDIRS = ["predictions", "plans", "drafts", "registered", "reports"]
CLEAR_DIRS = [OUTPUTS_DIR, ROOT_DIR / "evidence"]  # 폴더는 두고 안의 내용만 지운다
DB_FILES = [DATA_DIR / "app.db", DATA_DIR / "checkpoints.db", DATA_DIR / "memory.db"]
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
FIELD_NOTES = "field_notes"
MANUALS = "manuals"
FIELD_NOTES_SCRIPT = DATA_DIR / "virtual" / "make_field_notes.py"

# 지우면 안 되는 경로 (원본 데이터·모델·평가 결과·매뉴얼·계정·벡터 DB 폴더)
PROTECTED = [
    DATA_DIR / "raw", DATA_DIR / "processed", DATA_DIR / "pdm.db", ROOT_DIR / "models", ROOT_DIR / "reports",
    DATA_DIR / "virtual" / "manuals", ROOT_DIR / ".streamlit" / "secrets.toml", CHROMA_DIR,
]

SERVER_NOTICE = "Streamlit 서버를 먼저 종료하세요. 서버가 실행 중이면 DB 파일이 잠겨 지워지지 않습니다."


class LockedError(RuntimeError):
    """DB 파일이 다른 프로세스(실행 중인 서버 등)에 잡혀 있어 지울 수 없음."""


def _rel(path: Path) -> str:
    return path.relative_to(ROOT_DIR).as_posix()


def _guard(path: Path) -> None:
    """지울 경로가 프로젝트 안이고, 보호 경로이거나 보호 경로를 품고 있지 않은지 확인한다."""
    p = path.resolve()
    if p == ROOT_DIR or ROOT_DIR not in p.parents:
        raise ValueError(f"프로젝트 밖이거나 프로젝트 루트라 지울 수 없음: {p}")
    for keep in PROTECTED:
        k = keep.resolve()
        if p == k or k in p.parents or p in k.parents:
            raise ValueError(f"보호 경로라 지울 수 없음: {_rel(p)} (보호: {_rel(k)})")


def _db_paths() -> list[Path]:
    """존재하는 DB 파일과 SQLite 부속 파일(-wal 등)."""
    return [q for p in DB_FILES for q in (p, *(p.with_name(p.name + s) for s in SQLITE_SIDECARS)) if q.exists()]


def _dir_files(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*") if p.is_file()) if d.exists() else []


# ---------------------------------------------------------------- Chroma (field_notes만)
def _chroma():
    import chromadb

    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def _collection_count(name: str) -> int | None:
    """컬렉션 문서 수 (없으면 None)."""
    if not CHROMA_DIR.exists():
        return None
    client = _chroma()
    if name not in {c.name if hasattr(c, "name") else c for c in client.list_collections()}:
        return None
    return client.get_collection(name).count()


def _field_notes_dump() -> dict | None:
    """백업용 field_notes 전체 (ids, documents, metadatas, embeddings)."""
    if _collection_count(FIELD_NOTES) is None:
        return None
    data = _chroma().get_collection(FIELD_NOTES).get(include=["documents", "metadatas", "embeddings"])
    emb = data.get("embeddings")
    return {"ids": data["ids"], "documents": data["documents"], "metadatas": data["metadatas"],
            "embeddings": [list(map(float, e)) for e in emb] if emb is not None else None}


# ---------------------------------------------------------------- 같은 프로세스가 잡고 있는 연결 정리
def _release_in_process() -> None:
    """이 프로세스(설정 페이지에서 부르면 Streamlit 서버)가 열어 둔 연결·캐시를 닫는다. 불러온 모듈만 건드린다."""
    mods = sys.modules
    if "memory.persist" in mods:
        mods["memory.persist"].close_all()
    if "memory.store" in mods:
        mods["memory.store"]._STORE = None
    for mod, fn in [("agents.supervisor", "build_supervisor"), ("agents.investigator", "build_investigator"),
                    ("agents.fanout", "build_fanout_graph"), ("store.field_notes", "get_store")]:
        if mod in mods:
            getattr(mods[mod], fn).cache_clear()  # 닫은 연결·지운 컬렉션을 붙잡은 객체를 버린다


def _check_unlocked(paths: list[Path]) -> None:
    """지우기 전에 이름 바꾸기로 잠김 확인 (Windows는 다른 프로세스가 연 파일의 이름을 바꿀 수 없다)."""
    for p in paths:
        tmp = p.with_name(p.name + ".reset-check")
        try:
            os.replace(p, tmp)
            os.replace(tmp, p)
        except PermissionError as e:
            raise LockedError(f"{_rel(p)} 파일이 사용 중입니다") from e


# ---------------------------------------------------------------- 계획·백업·삭제·다시 채우기
def plan() -> dict:
    """지울 대상 목록 {"files": [경로], "dirs": {폴더: 파일 수}, "dbs": [경로], "field_notes": 문서 수 | None}."""
    dirs = {_rel(d): len(_dir_files(d)) for d in CLEAR_DIRS}
    files = [p for d in CLEAR_DIRS for p in _dir_files(d)] + _db_paths()
    return {"files": files, "dirs": dirs, "dbs": [_rel(p) for p in _db_paths()],
            "field_notes": _collection_count(FIELD_NOTES)}


def backup(p: dict) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    path = BACKUP_DIR / f"{datetime.now():%Y%m%d_%H%M%S}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in p["files"]:
            zf.write(f, _rel(f))
        if (dump := _field_notes_dump()) is not None:
            zf.writestr(f"chroma/{FIELD_NOTES}.json", json.dumps(dump, ensure_ascii=False))
    return path


def _delete(p: dict) -> list[str]:
    deleted = []
    try:
        for d in CLEAR_DIRS:
            _guard(d)
            n = len(_dir_files(d))
            for child in d.iterdir() if d.exists() else []:
                _guard(child)
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            deleted.append(f"{_rel(d)}/ 아래 파일 {n}개")
        for f in _db_paths():
            _guard(f)
            f.unlink()
            deleted.append(_rel(f))
    except PermissionError as e:
        raise LockedError(f"삭제 중 파일이 사용 중입니다: {e.filename}") from e
    if p["field_notes"] is not None:
        try:
            _chroma().delete_collection(FIELD_NOTES)
        except Exception as e:
            if "locked" in str(e).lower():
                raise LockedError("Chroma DB가 사용 중입니다") from e
            raise
        deleted.append(f"Chroma 컬렉션 {FIELD_NOTES} (문서 {p['field_notes']}개)")
    return deleted


def _refill() -> tuple[list[str], list[str]]:
    """(다시 만든 것, 건너뛴 것)"""
    from store import app_db

    made, skipped = [], []
    OUTPUTS_DIR.mkdir(exist_ok=True)
    for sub in OUTPUT_SUBDIRS:
        (OUTPUTS_DIR / sub).mkdir(exist_ok=True)
    with app_db.connect():  # 빈 스키마 생성
        pass
    made.append(f"{_rel(app_db.DB_PATH)} (빈 스키마)")

    if FIELD_NOTES_SCRIPT.exists():
        subprocess.run([sys.executable, str(FIELD_NOTES_SCRIPT)], cwd=ROOT_DIR, check=True)
        made.append(f"{_rel(FIELD_NOTES_SCRIPT)} 실행 (1~9월 가상 현장 메모)")
    else:
        skipped.append(f"{_rel(FIELD_NOTES_SCRIPT)} 없음 -> 가상 현장 메모 생성 건너뜀")

    _chroma().get_or_create_collection(FIELD_NOTES)
    reports = app_db.work_reports()
    if reports:
        from store import field_notes

        field_notes.get_store.cache_clear()
        n = 0
        for r in reports:
            wo = app_db.get_work_order(r["wo_id"])
            if wo:
                field_notes.index_report(wo, r)
                n += 1
        made.append(f"Chroma 컬렉션 {FIELD_NOTES} (완료 보고 {n}건 색인)")
    else:
        made.append(f"Chroma 컬렉션 {FIELD_NOTES} (빈 컬렉션, 색인할 완료 보고 없음)")
    return made, skipped


def kept() -> list[str]:
    out = []
    for k in PROTECTED:
        if k == CHROMA_DIR:
            n = _collection_count(MANUALS)
            out.append(f"Chroma 컬렉션 {MANUALS} " + ("(없음)" if n is None else f"(문서 {n}개)"))
        else:
            out.append(_rel(k) + ("" if k.exists() else " (원래 없음)"))
    return out


def reset_runtime() -> dict:
    """전체 초기화. {"backup", "deleted", "recreated", "skipped", "kept"}. 파일이 잠겨 있으면 LockedError."""
    _release_in_process()
    p = plan()
    _check_unlocked(_db_paths())
    backup_path = backup(p)
    try:
        deleted = _delete(p)
    except LockedError as e:  # 일부만 지워졌을 수 있으므로 백업 위치를 함께 알린다
        raise LockedError(f"{e} (백업: {_rel(backup_path)})") from e
    recreated, skipped = _refill()
    recreated.append("data/checkpoints.db, data/memory.db는 에이전트가 처음 쓸 때 자동 생성")
    return {"backup": _rel(backup_path), "deleted": deleted, "recreated": recreated, "skipped": skipped,
            "kept": kept()}


# ---------------------------------------------------------------- CLI
def print_plan(p: dict) -> None:
    print("\n[지울 대상]")
    for d, n in p["dirs"].items():
        print(f"  - {d}/ 아래 전부 (파일 {n}개)")
    for db in p["dbs"]:
        print(f"  - {db}")
    print(f"  - Chroma 컬렉션 {FIELD_NOTES}: " + ("없음" if p["field_notes"] is None else f"문서 {p['field_notes']}개"))
    print("\n[지우지 않음]")
    for k in kept():
        print(f"  - {k}")
    print("\n지우기 전에 backups/에 zip으로 백업합니다.")


def print_summary(s: dict) -> None:
    print(f"\n{'=' * 80}\n초기화 완료 · 백업: {s['backup']}\n{'=' * 80}")
    for title, key in [("지운 것", "deleted"), ("다시 만든 것", "recreated"), ("건너뛴 것", "skipped"), ("남은 것", "kept")]:
        if s[key]:
            print(f"\n[{title}]")
            for line in s[key]:
                print(f"  - {line}")


def main(argv: list[str]) -> int:
    print(f"※ {SERVER_NOTICE}")
    try:
        p = plan()
    except sqlite3.OperationalError as e:
        print(f"\n목록을 읽지 못했습니다: {e}\n※ {SERVER_NOTICE}")
        return 1
    print_plan(p)
    if "--yes" not in argv:
        if input('\n실행하려면 "yes"를 입력하세요: ').strip() != "yes":
            print("취소했습니다.")
            return 1
    try:
        summary = reset_runtime()
    except (LockedError, sqlite3.OperationalError) as e:
        print(f"\n초기화 실패: {e}\n※ {SERVER_NOTICE}")
        return 1
    print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
