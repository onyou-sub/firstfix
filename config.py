# config.py
# 원본 값 -> 프로젝트 내부 이름 (data/processed/에 저장되는 값)
COMP_MAP = {"comp1": "motor", "comp2": "drive", "comp3": "pump", "comp4": "bearing"}
ERROR_MAP = {
    "error1": "overvoltage", "error2": "low_rpm", "error3": "torque",
    "error4": "pressure", "error5": "vibration",
}
# 화면 표시용 한글 이름
COMP_LABEL_KO = {"motor": "모터/전원부", "drive": "구동부", "pump": "밸브/펌프", "bearing": "베어링"}
ERROR_LABEL_KO = {
    "overvoltage": "과전압 경고", "low_rpm": "회전수 저하", "torque": "토크 이상",
    "pressure": "압력 이상", "vibration": "진동 경고",
}

# 정비 계획 (agents/planner.py, tools/plan_tools.py)
CREWS = 3  # 하루 투입 가능한 정비 팀 수
CRIT_WEIGHT = {"A": 3, "B": 2, "C": 1}  # 설비 중요도 가중치
NO_STOCK_FACTOR = 0.8  # 부품 재고가 0이면 점수에 곱함 (당장 교체가 어려움)
SENSOR_CHECK_FACTOR = 0.5  # 센서 점검(사람 확인) 작업은 점수에 곱함
LUNCH = ("12:00", "13:00")  # 이 시간과 겹치는 작업은 13:00 시작으로 미룸 (작업을 쪼개지 않음)
PREP_MIN = 30  # 같은 팀의 작업 사이 준비·이동 시간
SKILL_WAIT_LIMIT_MIN = 60  # 전문 팀이 가장 빠른 팀보다 이 시간 이내로 늦으면 전문 팀에 배정
MAX_REPLAN = 3  # 하루 재계획 최대 횟수

# Supervisor (agents/supervisor.py) - 도구 이름은 프롬프트·미들웨어·그래프 출력에서 이 상수만 쓴다
TOOL_NAMES = {
    # Supervisor 도구
    "predict": "predict_failure",
    "investigate": "investigate_machines",
    "planner": "ask_planner_agent",
    "data": "ask_data_agent",
    "report": "ask_report_agent",
    "submit_wo": "submit_work_order",
    "submit_pr": "submit_purchase_request",
    "replan": "replan_maintenance",
    "feedback": "get_engineer_feedback",
    # 조사 에이전트 도구
    "sensor_trend": "get_sensor_trend",
    "error_logs": "get_error_logs",
    "maint_history": "get_maintenance_history",
    "similar_failures": "find_similar_failures",
    "manual": "search_manual",
    "memory": "recall_machine_memory",
    "field_notes": "search_field_notes",
    # 계획 에이전트 도구
    "rank": "rank_maintenance",
    "stock": "check_parts_stock",
    "draft_wo": "draft_work_order",
    "draft_pr": "draft_purchase_request",
    # 데이터 에이전트 도구
    "sql_tables": "sql_db_list_tables",
    "sql_schema": "sql_db_schema",
    "sql_run": "run_sql_guarded",
    # 리포트 에이전트 도구
    "read_evidence": "read_evidence",
    "save_report": "save_report",
}
CONTROL_WORDS = ["정지", "멈춰", "재시작", "재가동", "셧다운", "전원 차단", "설정 변경", "shutdown", "restart"]
SUPERVISOR_MODEL_CALL_LIMIT = 15  # 실행(invoke) 1회당 모델 호출 상한
