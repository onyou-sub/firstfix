"""1단계 확인: 입력 데이터가 예측(build_features)과 조사 도구에 반영되는지 (LLM 없음).

- 실제 data/app.db는 건드리지 않는다: 임시 app.db를 APP_DB_PATH로 넘긴다 (import 전에 설정).
- 실행: uv run python scripts/test_live_input.py && uv run pytest tests/test_no_leak.py -v
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

TMP_DIR = Path(tempfile.mkdtemp(prefix="fixfirst_live_"))
os.environ["APP_DB_PATH"] = str(TMP_DIR / "app.db")

import services  # noqa: E402
from ml.features import build_features  # noqa: E402
from tools.data_tools import DB_PATH, sensor_trend_text  # noqa: E402

AS_OF = "2015-11-12 06:00:00"
TELEMETRY_ROWS = 876_100
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    results.append((name, ok))


def vib24(machine_id: int = 45) -> float:
    return float(build_features(AS_OF).set_index("machine_id").at[machine_id, "vibration_mean_24h"])


def raw_rows() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]


def main() -> None:
    try:
        before = vib24()
        out = services.add_scenario(45, "bearing_vibration", AS_OF, created_by="admin")
        check("시나리오 입력 저장", bool(out["batch_id"]) and out["machine_ids"] == [45] and out["rows"] > 0, str(out))

        after = vib24()
        check("#45 vibration_mean_24h 증가", after > before, f"{before:.2f} -> {after:.2f}")

        trend = sensor_trend_text(45, "vibration", 24, AS_OF)
        check("get_sensor_trend에 '입력 데이터 포함' 표시", f"입력 데이터 포함 (배치 {out['batch_id']})" in trend,
              trend.splitlines()[1])

        services.deactivate_batch(out["batch_id"])
        check("배치 끄면 입력 전 값으로 복귀", abs(vib24() - before) < 1e-9, f"{vib24():.6f} vs {before:.6f}")
        check("끈 뒤 '입력 데이터 포함' 표시 없음", "입력 데이터 포함" not in sensor_trend_text(45, "vibration", 24, AS_OF))

        csv = (b"datetime,machine_id,volt,rotate,pressure,vibration\n"
               b"2015-11-12 05:00:00,45,170,446,100,50\n"
               b"2015-11-12 07:00:00,45,170,446,100,50\n")
        res = services.add_csv(csv, AS_OF, created_by="admin")
        check("기준 시각 이후 CSV 행 거부 (전체 미저장)",
              res["batch_id"] is None and [r["row"] for r in res["rejected"]] == [3], str(res["rejected"]))

        check("원본 telemetry 행 수 그대로", raw_rows() == TELEMETRY_ROWS, f"{raw_rows():,}")
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    print(f"\n결과: {sum(ok for _, ok in results)}/{len(results)} 통과")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()

# 실행 방법 (프로젝트 루트에서):
#   uv run python scripts/test_live_input.py && uv run pytest tests/test_no_leak.py -v
