"""막힌 경고 풀기 (한 번 실행용).

1. 조사에 실패한 경고(자동 조사 실패)는 시간당 조사 한도 계산에서 뺀다 (investigated_at 비우기)
2. 기준 날짜가 아닌 날짜의 열림/대기/조사 중 경고는 "종료"로 닫는다 (배정된 작업 지시는 그대로)
3. 오늘 기준 날짜의 "대기" 경고를 다시 처리한다 (조사 → 긴급 점검 배정, 조사 에이전트 LLM 사용)

실행: uv run python scripts/unblock_alerts.py
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import live_ops  # noqa: E402
from store import app_db  # noqa: E402


def main() -> None:
    cur = app_db.current_day()
    day = cur["day"] if cur else None
    print(f"기준 날짜: {day}")
    with app_db._lock, app_db.connect() as conn:
        n_failed = conn.execute("UPDATE alerts SET investigated_at = NULL"
                                " WHERE investigated_at IS NOT NULL AND message LIKE '%자동 조사 실패%'").rowcount
        n_stale = 0
        if day:
            n_stale = conn.execute(
                "UPDATE alerts SET status = '종료', investigating = 0,"
                " message = COALESCE(message, '') || ' · 기준 날짜 변경으로 종료', updated_at = ?"
                " WHERE substr(as_of, 1, 10) != ? AND status IN ('열림', '대기', '조사 중')",
                (app_db.now(), day)).rowcount
    print(f"1. 실패한 조사 {n_failed}건을 한도 계산에서 제외")
    print(f"2. 이전 날짜 열린 경고 {n_stale}건 종료")
    print(f"   지금 최근 1시간 조사 수: {live_ops._investigations_last_hour()} / {live_ops.MAX_INVESTIGATIONS_PER_HOUR}")

    waiting = app_db._rows("SELECT alert_id, machine_id FROM alerts WHERE status = '대기'"
                           + (" AND substr(as_of, 1, 10) = ?" if day else "") + " ORDER BY created_at",
                           (day,) if day else ())
    print(f"3. 대기 경고 {len(waiting)}건 다시 처리 (건당 수십 초 걸릴 수 있음)")
    for a in waiting:
        out = live_ops.handle_alert(a["alert_id"])
        detail = out.get("wo_id") or out.get("error") or ""
        print(f"   #{a['machine_id']} {a['alert_id']} -> {out.get('status')} {detail}")
    print("끝")


if __name__ == "__main__":
    main()
