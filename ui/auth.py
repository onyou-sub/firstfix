"""로그인 검증 (Streamlit 화면 코드 없음).

- 계정은 .streamlit/secrets.toml의 [auth.users.<아이디>]에서 읽는다 (name, password_hash, role, allow_dev).
- 비밀번호는 bcrypt 해시와 비교한다. 코드에 비밀번호를 두지 않는다.
- 관리자(role="admin")와 팀 작업자(role="worker", team 필수) 계정만 로그인할 수 있다.
- 같은 아이디로 MAX_FAILURES회 연속 실패하면 LOCK_SECONDS 동안 잠근다 (서버 프로세스 기준, 새로고침해도 유지).
"""

import threading
import time
from collections.abc import Mapping

import bcrypt

MAX_FAILURES = 5
LOCK_SECONDS = 5 * 60
_lock = threading.Lock()
_FAILURES: dict[str, dict] = {}  # 아이디 -> {"count", "locked_until"}


def _users(secrets: Mapping) -> Mapping:
    try:
        return secrets["auth"]["users"]
    except (KeyError, FileNotFoundError, TypeError):
        return {}


def locked_seconds(user_id: str, now: float | None = None) -> int:
    """남은 잠금 시간(초). 잠기지 않았으면 0."""
    now = time.time() if now is None else now
    with _lock:
        rec = _FAILURES.get(user_id.strip())
        return max(0, int(rec["locked_until"] - now + 0.999)) if rec else 0


def login(secrets: Mapping, user_id: str, password: str, now: float | None = None) -> tuple[dict | None, str]:
    """(사용자 정보 또는 None, 안내 문구). 성공하면 연속 실패 기록을 지운다."""
    now = time.time() if now is None else now
    user_id = user_id.strip()
    if not user_id or not password:
        return None, "아이디와 비밀번호를 입력하세요."
    wait = locked_seconds(user_id, now)
    if wait:
        return None, f"로그인 시도가 {MAX_FAILURES}회 연속 실패해 잠겼습니다. {wait // 60}분 {wait % 60}초 뒤에 다시 시도하세요."

    account = _users(secrets).get(user_id)
    ok = False
    if account is not None and (account.get("role") == "admin" or (account.get("role") == "worker" and account.get("team"))):
        try:
            ok = bcrypt.checkpw(password.encode("utf-8"), str(account["password_hash"]).encode("utf-8"))
        except (KeyError, ValueError):
            ok = False
    with _lock:
        if ok:
            _FAILURES.pop(user_id, None)
        else:
            rec = _FAILURES.setdefault(user_id, {"count": 0, "locked_until": 0.0})
            if rec["locked_until"] and rec["locked_until"] <= now:  # 잠금이 끝났으면 다시 셈
                rec.update(count=0, locked_until=0.0)
            rec["count"] += 1
            if rec["count"] >= MAX_FAILURES:
                rec["locked_until"] = now + LOCK_SECONDS
                return None, f"{MAX_FAILURES}회 연속 실패해 {LOCK_SECONDS // 60}분 동안 로그인할 수 없습니다."
            left = MAX_FAILURES - rec["count"]
            return None, f"아이디 또는 비밀번호가 올바르지 않습니다. (잠금까지 {left}회 남음)"
    return {"id": user_id, "name": account.get("name", user_id), "role": account.get("role"),
            "allow_dev": bool(account.get("allow_dev", False)), "team": account.get("team")}, "로그인했습니다."


def reset_failures() -> None:
    """테스트용: 실패 기록 초기화."""
    with _lock:
        _FAILURES.clear()
