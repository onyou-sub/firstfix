"""채팅(데이터 에이전트)이 보는 운영 기록 뷰에 긴급 작업 구분이 있는지 (LLM 없음, 임시 app.db).

긴급 작업은 task_type이 sensor_check일 수도 있어서 urgent 열로만 정확히 셀 수 있다.
예전에는 ops_work_orders에 urgent가 없어 "1팀 긴급 0건"처럼 틀린 답이 나왔다.

실행: uv run pytest tests/test_ops_views.py -v
"""
import pytest

from agents.data_agent import SYSTEM_PROMPT
from store import app_db
from tools import sql_tools


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "app.db")
    with app_db.connect() as conn:
        for wo_id, task, urgent in [("UWO-1018-001-1", "sensor_check", 1), ("UWO-1018-014-1", "urgent_check", 1),
                                    ("WO-1018-050", "temporary_check", 0), ("WO-1018-051", "replace", 0)]:
            conn.execute('INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, status,'
                         " created_at, urgent) VALUES (?, '2015-10-18', 1, 'motor', ?, '1팀', 'DONE', ?, ?)",
                         (wo_id, task, app_db.now(), urgent))


def test_urgent_column_visible_and_counts():
    conn = sql_tools.get_connection("2015-10-18 06:00:00")
    try:
        rows = conn.execute("SELECT wo_id FROM ops_work_orders WHERE team = '1팀' AND urgent = 1 ORDER BY wo_id").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["UWO-1018-001-1", "UWO-1018-014-1"]  # 센서 점검 긴급도 포함


def test_description_and_prompt_define_urgent():
    desc = sql_tools.TABLE_DESCRIPTIONS["ops_work_orders"]
    assert "urgent: 1=긴급 작업" in desc and "sensor_check" in desc and "urgent_check" in desc
    assert "긴급 작업(긴급 점검·긴급 출동) = ops_work_orders.urgent = 1" in SYSTEM_PROMPT
    assert "temporary_check는 긴급이 아닙니다" in SYSTEM_PROMPT
