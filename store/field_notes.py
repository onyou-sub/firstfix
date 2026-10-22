"""작업자 현장 기록 색인 (Chroma 컬렉션 "field_notes", 매뉴얼과 같은 data/chroma·임베딩).

- 완료 보고 1건 = 문서 1개 (id = 작업 지시 번호, 다시 색인하면 덮어씀)
- 본문: 설비·부품·원인·조치 한 줄 + "이상 발견" 단계와 측정값 + 지시와 달랐던 점 + 상세 메모 전체
- metadata.noted_ts: 기록이 생긴 데모 시각(기준 날짜 + 작업 종료 시각)의 epoch 초.
  검색은 noted_ts <= as_of 인 기록만 돌려준다 (미래 기록 차단).
- search_text(query, as_of, machine_id=None): 조사 에이전트 도구(search_field_notes)가 쓰는 문자열
"""

from functools import cache
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
CHROMA_DIR = ROOT_DIR / "data" / "chroma"
COLLECTION = "field_notes"
EMBEDDING_MODEL = "text-embedding-3-small"  # 매뉴얼 색인(data/virtual/build_index.py)과 같은 모델
ABNORMAL = "이상 발견"


@cache
def get_store():
    """검색·색인용 Chroma (프로세스 안에서 한 번만 연다). Chroma·OpenAI는 실제로 쓸 때만 불러온다."""
    from langchain_chroma import Chroma
    from langchain_openai import OpenAIEmbeddings

    load_dotenv(ROOT_DIR / ".env")
    return Chroma(collection_name=COLLECTION, embedding_function=OpenAIEmbeddings(model=EMBEDDING_MODEL),
                  persist_directory=str(CHROMA_DIR))


def _ts(text: str) -> int:
    return int(pd.Timestamp(text).timestamp())


def noted_at(wo: dict) -> str:
    """기록 시각 = 기준 날짜 + 작업 종료 시각 (교체 기록 maint_log와 같은 기준)."""
    return f"{wo['as_of_date']} {wo.get('end') or '18:00'}:00"


def note_text(wo: dict, report: dict) -> str:
    """색인 본문. 이상 발견 단계와 측정값, 메모 전체를 넣어 다음 조사에서 검색되게 한다."""
    head = (f"#{wo['machine_id']} {wo['comp']} {wo.get('title') or ''} ({wo.get('team') or '-'}, {wo['as_of_date']})"
            f" | 실제 원인: {report['actual_cause_type']} | 실제 부품: {report['actual_comp']}"
            f" | 조치: {report['action']} | 소요 {report['duration_min']}분 | 사용 부품 {report['parts_used']}개")
    lines = [head]
    abnormal = [s for s in report.get("step_results", []) if s.get("result") == ABNORMAL]
    if abnormal:
        lines.append("이상 발견 단계:")
        lines += [f"- {s['action']}: {s.get('observation') or '(측정값 없음)'}" for s in abnormal]
    measured = [s for s in report.get("step_results", []) if s.get("result") != ABNORMAL and s.get("observation")]
    if measured:
        lines.append("그 밖의 측정·관찰:")
        lines += [f"- {s['action']} ({s['result']}): {s['observation']}" for s in measured]
    if report.get("deviation"):
        lines.append(f"지시와 달랐던 점: {report['deviation']}")
    lines.append(f"상세 메모:\n{report.get('memo', '')}")
    return "\n".join(lines)


def index_report(wo: dict, report: dict) -> str:
    """완료 보고를 색인한다 (같은 작업 지시는 덮어씀). 문서 id."""
    from langchain_core.documents import Document

    at = noted_at(wo)
    doc = Document(page_content=note_text(wo, report), metadata={
        "wo_id": wo["wo_id"], "machine_id": int(wo["machine_id"]), "comp": wo["comp"],
        "actual_comp": report["actual_comp"], "cause_type": report["actual_cause_type"],
        "team": wo.get("team") or "", "as_of_date": wo["as_of_date"], "noted_at": at, "noted_ts": _ts(at),
    })
    store = get_store()
    store.delete(ids=[wo["wo_id"]])
    store.add_documents([doc], ids=[wo["wo_id"]])
    return wo["wo_id"]


def search(query: str, as_of: str, k: int = 3, machine_id: int | None = None) -> list:
    """as_of 이전 기록만 유사도 검색. machine_id를 주면 그 설비 기록만."""
    where = {"noted_ts": {"$lte": _ts(as_of)}}
    if machine_id is not None:
        where = {"$and": [where, {"machine_id": int(machine_id)}]}
    return get_store().similarity_search(query, k=k, filter=where)


def search_text(query: str, as_of: str, machine_id: int | None = None, k: int = 3) -> str:
    docs = search(query, as_of, k=k, machine_id=machine_id)
    if not docs:
        return "현장 기록 없음 (기준 시각 이전 작업자 완료 보고가 없거나 관련 기록을 찾지 못함)"
    return "\n\n".join(f"[현장 기록: {d.metadata['wo_id']}, {d.metadata['noted_at'][:16]}, #{d.metadata['machine_id']}"
                       f" {d.metadata['team']}]\n{d.page_content}" for d in docs)


def delete_day(as_of_date: str) -> int:
    """그날(기준 날짜) 작업 지시의 현장 기록을 지운다 (데모 초기화). 지운 수."""
    day = pd.Timestamp(as_of_date).strftime("%Y-%m-%d")
    store = get_store()
    ids = store.get(where={"as_of_date": day}).get("ids", [])
    if ids:
        store.delete(ids=ids)
    return len(ids)
