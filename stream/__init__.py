"""1초 실시간 스트림: 평소값(baseline) -> 값 생성(generator) -> 실시간 감지(detector) -> 1시간 요약(aggregate).

1초 판단은 통계 규칙(코드)이 하고, 기존 예측 모델·조사 에이전트는 1시간 요약값(live_telemetry)으로 읽는다.
생성기 루프는 별도 프로세스 scripts/stream_sim.py, 화면 제어는 stream.control (services가 다시 내보냄).
"""

SENSORS = ["volt", "rotate", "pressure", "vibration"]
SENSOR_COMP = {"volt": "motor", "rotate": "drive", "pressure": "pump", "vibration": "bearing"}
DOWN_SENSORS = {"rotate"}  # 고장 전 아래 방향으로 움직이는 센서 (나머지는 위 방향)
COMP_ERRORS = {"motor": "overvoltage", "drive": "low_rpm", "pump": "pressure", "bearing": "vibration"}
