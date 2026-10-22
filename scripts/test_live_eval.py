"""2단계 확인: 입력 직후 즉시 판단과 경고 (LLM 없음).

- 실제 data/app.db는 건드리지 않는다 (임시 app.db를 APP_DB_PATH로).
- 실행: uv run python scripts/test_live_eval.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

TMP_DIR = Path(tempfile.mkdtemp(prefix="fixfirst_eval_"))
os.environ["APP_DB_PATH"] = str(TMP_DIR / "app.db")

import services  # noqa: E402
from ml.predict import predict_df  # noqa: E402

AS_OF = "2015-11-12 06:00:00"
REPLACED = 87  # 2015-11-08 06:00 베어링 교체 (기준 시각 4일 전)
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"\n       {detail}" if detail else ""))
    results.append((name, ok))


def top(machine_id: int) -> str:
    p = predict_df(AS_OF).set_index("machine_id").loc[machine_id]
    return f"모델 top {p['top_comp']} {p['top_prob']:.3f} 고위험 {bool(p['high_risk'])}"


def count_alerts(machine_id: int) -> int:
    return sum(a["machine_id"] == machine_id for a in services.alert_history(limit=500))


def main() -> None:
    try:
        r = services.add_scenario(45, "bearing_vibration", AS_OF, created_by="admin")["alerts"][0]
        check("#45 베어링 진동 시나리오 -> 경고, 열림", r["level"] == "경고" and r["status"] == "열림" and r["is_new"],
              f"{r['level']}({r['source']}) {r['message']} | {top(45)}")

        r2 = services.add_scenario(45, "bearing_vibration", AS_OF, created_by="admin")["alerts"][0]
        check("같은 시나리오 한 번 더 -> 경고 1건 유지", not r2["is_new"] and r2["alert_id"] == r["alert_id"]
              and count_alerts(45) == 1, f"is_new {r2['is_new']}, #45 경고 {count_alerts(45)}건")

        normal = services.add_manual(46, "vibration", 41, "2015-11-12 01:00", "2015-11-12 06:00", "admin", as_of=AS_OF)
        a = normal["alerts"][0]
        check("정상 범위 직접 입력(진동 41) -> 경고 없음", a["level"] == "정상" and a["alert_id"] is None
              and count_alerts(46) == 0, f"{a['level']} {a['message']} | {top(46)}")

        high = services.add_manual(51, "vibration", 60, "2015-11-12 01:00", "2015-11-12 06:00", "admin", as_of=AS_OF)
        a = high["alerts"][0]
        check("모델은 낮지만 진동 60 6시간 -> 경고(규칙)", a["level"] == "경고" and a["source"] == "규칙",
              f"{a['level']}({a['source']}) {a['message']} | {top(51)}")

        a = services.add_scenario(REPLACED, "bearing_vibration", AS_OF, created_by="admin")["alerts"][0]
        check(f"최근 7일 안에 교체한 설비(#{REPLACED})에 시나리오 -> 사람 확인", a["level"] == "사람 확인",
              f"{a['level']}({a['source']}) {a['message']}")

        opened = services.open_alerts(AS_OF)
        check("open_alerts에 열린 경고 3건(#45, #51, #87)", sorted(x["machine_id"] for x in opened) == [45, 51, REPLACED],
              str([(x["machine_id"], x["level"]) for x in opened]))
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서):
#   uv run python scripts/test_live_eval.py
