"""조사 진행 기록 (화면 진행 막대용). 스레드 안전한 dict.

{as_of: {machine_id: {"step": 도구 호출 수, "done": bool, "error": str | None}}}
- investigate_one이 start / ToolCounter(도구 호출마다 step+1) / finish를 부른다.
- 에이전트·Streamlit 어느 쪽도 import하지 않는 독립 모듈 (순환 import 방지).
"""

import threading
from copy import deepcopy

import pandas as pd
from langchain_core.callbacks import BaseCallbackHandler

_lock = threading.Lock()
_PROGRESS: dict[str, dict[int, dict]] = {}


def _key(as_of: str) -> str:
    return pd.Timestamp(as_of).strftime("%Y-%m-%d %H:%M:%S")


def start(as_of: str, machine_id: int) -> None:
    with _lock:
        _PROGRESS.setdefault(_key(as_of), {})[int(machine_id)] = {"step": 0, "done": False, "error": None}


def step(as_of: str, machine_id: int) -> None:
    with _lock:
        rec = _PROGRESS.setdefault(_key(as_of), {}).setdefault(int(machine_id), {"step": 0, "done": False, "error": None})
        rec["step"] += 1


def finish(as_of: str, machine_id: int, error: str | None = None) -> None:
    with _lock:
        rec = _PROGRESS.setdefault(_key(as_of), {}).setdefault(int(machine_id), {"step": 0, "done": False, "error": None})
        rec["done"], rec["error"] = True, error


def get(as_of: str) -> dict[int, dict]:
    with _lock:
        return deepcopy(_PROGRESS.get(_key(as_of), {}))


def clear(as_of: str) -> None:
    with _lock:
        _PROGRESS.pop(_key(as_of), None)


class ToolCounter(BaseCallbackHandler):
    """조사 에이전트 1대의 도구 호출 수를 센다 (invoke config의 callbacks로 전달)."""

    def __init__(self, as_of: str, machine_id: int):
        self.as_of, self.machine_id = as_of, machine_id

    def on_tool_start(self, serialized, input_str, **kwargs) -> None:
        step(self.as_of, self.machine_id)
