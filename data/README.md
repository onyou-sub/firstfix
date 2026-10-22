# 데이터: Microsoft Azure Predictive Maintenance

- 출처: Kaggle [arnabbiswas1/microsoft-azure-predictive-maintenance](https://www.kaggle.com/datasets/arnabbiswas1/microsoft-azure-predictive-maintenance)
- 설비 100대의 2015년 1년치 센서·오류·정비·고장 기록

> **고지**: 이 데이터는 실제 설비에서 수집한 것이 아니라 **합성(시뮬레이션) 데이터**입니다.
> 또한 comp1~4의 부품 이름(모터·구동부·펌프·베어링)과 error1~5의 오류 이름은
> 원본 데이터에 없는 정보이며, **프로젝트에서 붙인 가상 매핑**입니다.
> 매핑은 프로젝트 루트의 `config.py`에 있습니다.

## 폴더 구성

| 경로 | 내용 |
|---|---|
| `data/raw/` | Kaggle 원본 CSV 5개 (수정하지 않음, git 제외) |
| `data/processed/` | 컬럼명·값 이름 정리, 정렬한 CSV 5개 (git 제외) |
| `download.py` | 원본 내려받기 |
| `prepare.py` | 원본 → processed 변환 |
| `check_data.py` | processed 검증 (OK / FAIL 출력) |

## 정리 규칙 (`prepare.py`)

- 파일 이름에서 `PdM_` 접두어 제거 (`PdM_telemetry.csv` → `telemetry.csv`)
- 컬럼명 변경: `machineID` → `machine_id` (모든 파일), `errorID` → `error_id` (errors), `failure` → `comp` (failures)
- `maint.by_failure` 추가: 같은 `machine_id`·`datetime`·`comp`의 고장 기록이 failures에 있으면 1, 없으면 0
- `datetime`은 `YYYY-MM-DD HH:MM:SS` 문자열로 저장
- `machine_id`, `datetime` 순으로 정렬
- 값 이름 변경 (`config.py`). `model1`~`4`는 바꾸지 않음

| 원본 | processed 값 | 화면 표시 (`COMP_LABEL_KO` / `ERROR_LABEL_KO`) |
|---|---|---|
| comp1 | motor | 모터/전원부 |
| comp2 | drive | 구동부 |
| comp3 | pump | 밸브/펌프 |
| comp4 | bearing | 베어링 |
| error1 | overvoltage | 과전압 경고 |
| error2 | low_rpm | 회전수 저하 |
| error3 | torque | 토크 이상 |
| error4 | pressure | 압력 이상 |
| error5 | vibration | 진동 경고 |

## 파일별 컬럼

자료형은 `pd.read_csv`로 읽었을 때 기준입니다 (`str` = 문자열).

### telemetry.csv — 센서 측정값 (876,100행 = 100대 × 8,761시간)

| 컬럼 | 자료형 | 의미 |
|---|---|---|
| datetime | str | 측정 시각 (1시간 간격, 2015-01-01 06:00:00 ~ 2016-01-01 06:00:00) |
| machine_id | int64 | 설비 ID (1~100) |
| volt | float64 | 전압 (해당 1시간 평균) |
| rotate | float64 | 회전수 (해당 1시간 평균) |
| pressure | float64 | 압력 (해당 1시간 평균) |
| vibration | float64 | 진동 (해당 1시간 평균) |

### errors.csv — 오류 기록 (3,919행)

설비가 멈추지 않는 오류(경고성 이벤트)입니다.

| 컬럼 | 자료형 | 의미 |
|---|---|---|
| datetime | str | 오류 발생 시각 (정시 단위) |
| machine_id | int64 | 설비 ID |
| error_id | str | 오류 유형 (`overvoltage`, `low_rpm`, `torque`, `pressure`, `vibration`, 원본 `error1`~`error5`) |

### maint.csv — 정비(부품 교체) 기록 (3,286행)

정기 정비와 고장으로 인한 교체를 모두 포함합니다. 2014년 기록도 일부 있습니다.

| 컬럼 | 자료형 | 의미 |
|---|---|---|
| datetime | str | 부품 교체 시각 |
| machine_id | int64 | 설비 ID |
| comp | str | 교체한 부품 (`motor`, `drive`, `pump`, `bearing`, 원본 `comp1`~`comp4`) |
| by_failure | int64 | 고장 때문에 교체했으면 1, 아니면 0 (failures와 machine_id·datetime·comp가 일치하는지로 판단, 1인 행 743개) |

### failures.csv — 고장 기록 (761행)

고장으로 부품을 교체한 기록입니다.

| 컬럼 | 자료형 | 의미 |
|---|---|---|
| datetime | str | 고장(교체) 시각 |
| machine_id | int64 | 설비 ID |
| comp | str | 고장 난 부품 (`motor`, `drive`, `pump`, `bearing`, 원본 컬럼명 `failure`, 원본 값 `comp1`~`comp4`) |

### machines.csv — 설비 정보 (100행)

| 컬럼 | 자료형 | 의미 |
|---|---|---|
| machine_id | int64 | 설비 ID (1~100) |
| model | str | 설비 모델 (`model1`~`model4`) |
| age | int64 | 설비 사용 연수 (년) |

## 실행 방법 (프로젝트 루트에서)

```bash
uv run python data/download.py
uv run python data/prepare.py
uv run python data/check_data.py
```
