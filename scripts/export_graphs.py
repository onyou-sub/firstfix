"""에이전트 그래프를 Mermaid로 출력한다 (docs/).

- graph_supervisor.mmd : Supervisor create_agent 그래프 (미들웨어 노드 포함)
- graph_fanout.mmd     : 병렬 조사 그래프 (Send fan-out)
- graph_system.mmd     : Supervisor -> 도구 -> 전문 에이전트 -> 하위 도구 (컴파일된 에이전트의 실제 도구 목록에서 생성)
- graphs.md            : 세 그래프를 ```mermaid 블록으로 묶은 문서 (VS Code·GitHub에서 바로 보기)
- *.png                : draw_mermaid_png 시도 (mermaid.ink 네트워크 필요, 실패하면 건너뜀)
"""

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from agents.data_agent import build_data_agent  # noqa: E402
from agents.fanout import build_fanout_graph  # noqa: E402
from agents.investigator import build_investigator  # noqa: E402
from agents.planner import build_planner  # noqa: E402
from agents.report import build_report_agent  # noqa: E402
from agents.supervisor import build_supervisor  # noqa: E402
from config import TOOL_NAMES as T  # noqa: E402
from langchain_core.runnables.graph_mermaid import draw_mermaid_png  # noqa: E402

DOCS_DIR = ROOT_DIR / "docs"

# Supervisor 도구 -> 그 도구가 부르는 전문 에이전트 (도구 목록은 컴파일된 에이전트에서 읽는다)
SUB_AGENTS = {
    T["investigate"]: ("조사 에이전트 ×N<br/>(병렬 fan-out)", build_investigator),
    T["planner"]: ("계획 에이전트", build_planner),
    T["data"]: ("데이터 에이전트", build_data_agent),
    T["report"]: ("리포트 에이전트", build_report_agent),
}
# 에이전트가 아닌 도구가 쓰는 대상 (설명용)
BACKENDS = {
    T["predict"]: "LightGBM 모델<br/>models/lgbm.pkl",
    T["submit_wo"]: "사람 승인(HITL) 후<br/>outputs/registered",
    T["submit_pr"]: "사람 승인(HITL) 후<br/>outputs/registered",
    T["replan"]: "조건 해석(LLM) → rank 재계산(코드)<br/>→ 초안 v2+ (outputs/plans·drafts)",
    T["feedback"]: "장기 기억<br/>(SqliteStore, 엔지니어 피드백)",
}


def tool_names(agent) -> list[str]:
    """컴파일된 create_agent 그래프의 ToolNode에서 실제 도구 이름을 읽는다."""
    return list(agent.nodes["tools"].bound.tools_by_name)


def system_mermaid() -> str:
    lines = ["flowchart TD", '    U(["사용자"]) --> S{{"Supervisor"}}']
    for name in tool_names(build_supervisor()):
        tid = f"t_{name}"
        lines.append(f'    S --> {tid}["{name}"]')
        if name in SUB_AGENTS:
            label, build = SUB_AGENTS[name]
            aid = f"a_{name}"
            lines.append(f'    {tid} --> {aid}(["{label}"])')
            for sub in tool_names(build()):
                lines.append(f'    {aid} --> {aid}_{sub}["{sub}"]')
        elif name in BACKENDS:
            lines.append(f'    {tid} --> b_{name}[("{BACKENDS[name]}")]')
    lines += [
        "    classDef hitl fill:#ffe9d6,stroke:#d9822b",
        *[f"    class t_{n} hitl" for n in (T["submit_wo"], T["submit_pr"])],
    ]
    return "\n".join(lines) + "\n"


def save_png(graph, path: Path) -> None:
    """graph: 컴파일된 그래프의 get_graph() 결과 또는 Mermaid 문자열."""
    try:
        png = draw_mermaid_png(graph) if isinstance(graph, str) else graph.draw_mermaid_png()
        path.write_bytes(png)
        print(f"  PNG 저장: {path.relative_to(ROOT_DIR)}")
    except Exception as e:  # mermaid.ink 접속 실패 등
        print(f"  PNG 건너뜀 ({path.name}): {type(e).__name__} - 네트워크 연결 시 다시 실행하거나 graphs.md를 보세요")


def main() -> None:
    DOCS_DIR.mkdir(exist_ok=True)
    supervisor = build_supervisor().get_graph()
    fanout = build_fanout_graph().get_graph()
    graphs = {
        "graph_supervisor": ("Supervisor (create_agent + 미들웨어)", supervisor.draw_mermaid()),
        "graph_fanout": ("병렬 조사 (Send fan-out)", fanout.draw_mermaid()),
        "graph_system": ("전체 구조 (Supervisor → 도구 → 전문 에이전트 → 하위 도구)", system_mermaid()),
    }
    for name, (_, text) in graphs.items():
        (DOCS_DIR / f"{name}.mmd").write_text(text, encoding="utf-8")
        print(f"저장: docs/{name}.mmd")

    md = ["# 에이전트 그래프", "", "`uv run python scripts/export_graphs.py`로 코드에서 생성한 그림입니다. 직접 고치지 마세요.", ""]
    for name, (title, text) in graphs.items():
        md += [f"## {title}", "", f"`docs/{name}.mmd`", "", "```mermaid", text.strip(), "```", ""]
    (DOCS_DIR / "graphs.md").write_text("\n".join(md), encoding="utf-8")
    print("저장: docs/graphs.md")

    print("PNG 저장 시도 (mermaid.ink):")
    save_png(supervisor, DOCS_DIR / "graph_supervisor.png")
    save_png(fanout, DOCS_DIR / "graph_fanout.png")
    save_png(graphs["graph_system"][1], DOCS_DIR / "graph_system.png")


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서):
#   uv run python scripts/export_graphs.py
