"""Kaggle "Microsoft Azure Predictive Maintenance" 데이터를 data/raw/에 내려받는다.

- data/raw/에 5개 파일이 이미 있으면 아무것도 하지 않는다.
- ~/.kaggle/kaggle.json 인증이 있으면 kaggle API로 내려받고 압축을 푼다.
- 인증이 없으면 직접 받는 방법을 안내하고 종료한다.
"""

import os
import shutil
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
RAW_DIR = DATA_DIR / "raw"

DATASET = "arnabbiswas1/microsoft-azure-predictive-maintenance"
DATASET_URL = f"https://www.kaggle.com/datasets/{DATASET}"
FILES = [
    "PdM_telemetry.csv",
    "PdM_errors.csv",
    "PdM_maint.csv",
    "PdM_failures.csv",
    "PdM_machines.csv",
]


def missing_files() -> list[str]:
    return [name for name in FILES if not (RAW_DIR / name).is_file()]


def kaggle_json_path() -> Path:
    # kaggle은 KAGGLE_CONFIG_DIR이 지정되면 그 위치의 kaggle.json을 읽는다.
    config_dir = os.environ.get("KAGGLE_CONFIG_DIR")
    base = Path(config_dir) if config_dir else Path.home() / ".kaggle"
    return base / "kaggle.json"


def print_manual_guide() -> None:
    print("Kaggle에서 직접 받아 data/raw/에 5개 파일을 넣으세요.")
    print(f"  다운로드 URL: {DATASET_URL}")
    print(f"  넣을 위치: {RAW_DIR}")
    print(f"  파일: {', '.join(FILES)}")


def collect_nested_files() -> None:
    # 압축 안에 하위 폴더가 있으면 CSV를 data/raw/ 바로 아래로 옮긴다.
    for name in missing_files():
        found = next(RAW_DIR.rglob(name), None)
        if found is not None:
            shutil.move(str(found), RAW_DIR / name)


def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    if not missing_files():
        print(f"이미 5개 파일이 있습니다. 다운로드를 건너뜁니다: {RAW_DIR}")
        return 0

    if not kaggle_json_path().is_file():
        print(f"Kaggle 인증 파일이 없습니다: {kaggle_json_path()}")
        print_manual_guide()
        return 1

    # kaggle 패키지는 import 시점에 인증을 시도하므로 인증 파일 확인 뒤에 import한다.
    from kaggle.api.kaggle_api_extended import KaggleApi

    print(f"Kaggle API로 내려받는 중: {DATASET} -> {RAW_DIR}")
    try:
        api = KaggleApi()
        api.authenticate()
        api.dataset_download_files(DATASET, path=str(RAW_DIR), unzip=True, quiet=False)
    except Exception as exc:
        print(f"다운로드 실패: {exc}")
        print_manual_guide()
        return 1

    collect_nested_files()
    zip_path = RAW_DIR / f"{DATASET.split('/')[1]}.zip"
    if zip_path.exists():
        zip_path.unlink()

    missing = missing_files()
    if missing:
        print(f"다운로드 후에도 없는 파일: {', '.join(missing)}")
        print_manual_guide()
        return 1

    print("다운로드 완료:")
    for name in FILES:
        size_mb = (RAW_DIR / name).stat().st_size / 1_000_000
        print(f"  {name} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# 실행 방법 (프로젝트 루트에서):
#   uv run python data/download.py
