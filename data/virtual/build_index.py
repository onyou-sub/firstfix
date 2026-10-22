"""가상 매뉴얼(data/virtual/manuals/*.md)을 "##" 절 단위로 나눠 Chroma에 저장하고, 검색 함수를 제공한다.

- 저장 위치: data/chroma, 컬렉션 "manuals" (다시 실행하면 컬렉션을 지우고 새로 만든다)
- 임베딩: OpenAIEmbeddings(model="text-embedding-3-small"), .env의 OPENAI_API_KEY 사용
- metadata: source(파일명), section(절 번호와 제목) - 모두 문자열
- search_manual_raw(query, k=3): "[출처: 파일명 §절]\\n본문" 형식 문자열 (에이전트 도구 래핑은 다음 단계)
"""

import re
from functools import cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

VIRTUAL_DIR = Path(__file__).resolve().parent
ROOT_DIR = VIRTUAL_DIR.parent.parent
MANUALS_DIR = VIRTUAL_DIR / "manuals"
CHROMA_DIR = ROOT_DIR / "data" / "chroma"
COLLECTION = "manuals"
EMBEDDING_MODEL = "text-embedding-3-small"

SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)


def split_sections(path: Path) -> list[Document]:
    """'## ' 제목 하나 = 문서 하나. 문서 제목('# ')을 본문 앞에 붙여 어떤 부품 매뉴얼인지 검색에 반영한다."""
    text = path.read_text(encoding="utf-8")
    title_match = re.search(r"^# (.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem

    heads = list(SECTION_RE.finditer(text))
    docs = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        section = head.group(1).strip()
        body = text[head.end():end].strip()
        docs.append(Document(
            page_content=f"{title}\n## {section}\n{body}",
            metadata={"source": path.name, "section": section},
        ))
    return docs


def load_manual_docs() -> list[Document]:
    docs = [d for path in sorted(MANUALS_DIR.glob("*.md")) for d in split_sections(path)]
    if not docs:
        raise FileNotFoundError(f"{MANUALS_DIR}에 '## ' 절이 있는 마크다운이 없습니다")
    return docs


def get_embeddings() -> OpenAIEmbeddings:
    load_dotenv(ROOT_DIR / ".env")
    return OpenAIEmbeddings(model=EMBEDDING_MODEL)


def build_index(embeddings=None) -> Chroma:
    """컬렉션을 지우고 매뉴얼 전체를 다시 넣는다."""
    embeddings = embeddings or get_embeddings()
    store = Chroma(collection_name=COLLECTION, embedding_function=embeddings, persist_directory=str(CHROMA_DIR))
    store.delete_collection()
    store = Chroma(collection_name=COLLECTION, embedding_function=embeddings, persist_directory=str(CHROMA_DIR))

    docs = load_manual_docs()
    ids = [f"{d.metadata['source']}#{d.metadata['section'].split()[0]}" for d in docs]
    store.add_documents(docs, ids=ids)
    get_store.cache_clear()
    return store


@cache
def get_store() -> Chroma:
    """검색용 Chroma (프로세스 안에서 한 번만 연다)."""
    return Chroma(collection_name=COLLECTION, embedding_function=get_embeddings(), persist_directory=str(CHROMA_DIR))


def search_manual_docs(query: str, k: int = 3) -> list[Document]:
    return get_store().similarity_search(query, k=k)


def format_doc(doc: Document) -> str:
    # page_content 맨 앞의 문서 제목과 절 제목 줄은 출처 줄과 겹치므로 본문만 남긴다
    body = doc.page_content.split("\n", 2)[-1].strip()
    return f"[출처: {doc.metadata['source']} §{doc.metadata['section']}]\n{body}"


def search_manual_raw(query: str, k: int = 3) -> str:
    """매뉴얼 검색 결과 상위 k개를 "[출처: 파일명 §절]\\n본문" 형식으로 이어 붙인 문자열."""
    docs = search_manual_docs(query, k=k)
    if not docs:
        return "관련 매뉴얼을 찾지 못했습니다."
    return "\n\n".join(format_doc(d) for d in docs)


if __name__ == "__main__":
    docs = load_manual_docs()
    print(f"매뉴얼 {len({d.metadata['source'] for d in docs})}개, 절 {len(docs)}개를 임베딩합니다 ({EMBEDDING_MODEL})...")
    store = build_index()
    print(f"저장 완료: {CHROMA_DIR.relative_to(ROOT_DIR)} / 컬렉션 '{COLLECTION}' ({store._collection.count()}개)")
    for d in docs:
        print(f"  {d.metadata['source']:<20} §{d.metadata['section']}")

    query = "베어링 진동 상승 점검"
    print(f"\n검색 예시: '{query}'\n")
    print(search_manual_raw(query, k=1))

# 실행 순서 (프로젝트 루트에서):
#   1. uv run python data/virtual/make_virtual.py
#   2. uv run python data/virtual/build_index.py    # .env에 OPENAI_API_KEY 필요
