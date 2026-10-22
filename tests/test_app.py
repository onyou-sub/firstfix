"""화면 테스트 (streamlit AppTest).

- 기본: USE_MOCK=1, 테스트용 계정(secrets)으로 로그인 → 점검 시작 → 결정 제출 → 채팅 (LLM 없음)
- @pytest.mark.llm: 실제 에이전트 연결 (OpenAI API 사용)

실행: uv run pytest tests/test_app.py -v -m "not llm"
      uv run pytest tests/test_app.py -v -m llm
"""

from datetime import date
from pathlib import Path

import bcrypt
import pytest
from streamlit.testing.v1 import AppTest

from ui import auth

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PASSWORD = "test-password-1234"
HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
SECRETS = {"auth": {"users": {
    "admin": {"name": "설비보전 관리자", "password_hash": HASH, "role": "admin", "allow_dev": True},
    "viewer": {"name": "조회자", "password_hash": HASH, "role": "viewer", "allow_dev": False},
    "team2": {"name": "2팀 작업자", "password_hash": HASH, "role": "worker", "team": "2팀", "allow_dev": False},
}}}
ADMIN_PAGES = ["views/today.py", "views/work_status.py", "views/history.py", "views/settings.py"]
WORKER_PAGES = ["views/worker_dashboard.py", "views/worker_report.py", "views/worker_tasks.py",
                "views/worker_history.py"]


@pytest.fixture(autouse=True)
def _reset_lockout():
    auth.reset_failures()
    yield
    auth.reset_failures()


def new_app(monkeypatch, mock: bool = True, timeout: int = 60) -> AppTest:
    if mock:
        monkeypatch.setenv("USE_MOCK", "1")
    else:
        monkeypatch.delenv("USE_MOCK", raising=False)
    at = AppTest.from_file(APP, default_timeout=timeout)
    at.secrets["auth"] = SECRETS["auth"]
    at.run()
    assert not at.exception
    return at


def try_login(at: AppTest, user_id: str, password: str) -> AppTest:
    at.text_input(key="login_id").input(user_id)
    at.text_input(key="login_pw").input(password)
    next(b for b in at.button if b.label == "로그인").click()
    return at.run()


def login(at: AppTest, user_id: str = "admin") -> AppTest:
    try_login(at, user_id, PASSWORD)
    assert not at.exception
    assert at.session_state["user"]["id"] == user_id
    return at


@pytest.fixture(autouse=True)
def temp_app_db(tmp_path, monkeypatch):
    """운영 DB를 임시 파일로 (실제 data/app.db를 건드리지 않음). 기준 날짜 공유도 여기에 저장된다."""
    from store import app_db

    monkeypatch.setattr(app_db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setenv("URGENT_NOW_HM", "09:00")  # 긴급 출동 시각(실제 시계)을 고정
    return app_db


def fake_order(app_db, team: str = "2팀") -> dict:
    draft = {"draft_id": "WO-1001-090", "type": "work_order", "as_of": "2015-10-01 06:00:00", "machine_id": 90,
             "comp": "motor", "task_type": "replace", "team": team, "start": "09:00", "end": "11:00",
             "checklist": ["점검 전 설비 정지 절차 확인", "단자 체결 확인"], "evidence_ids": ["E1001-090"]}
    assert app_db.create_work_order(draft, "admin")
    return draft


def keys(widgets) -> set:
    return {w.key for w in widgets}


def decision_radios(at: AppTest) -> list:
    return [r for r in at.radio if str(r.key).startswith("dec_")]


def chat_box(at: AppTest):
    """채팅 입력칸 (오른쪽 아래 아이콘으로 연 대화창 안). 대화창이 닫혀 있으면 먼저 연다."""
    if not any(c.key == "chat_input" for c in at.chat_input):
        at.button(key="chat_fab").click().run()
    return at.chat_input(key="chat_input")


def expander_labels(at: AppTest) -> list[str]:
    return [e.label for e in at.expander]


# ---------------------------------------------------------------- 로그인
def test_login_required(monkeypatch):
    at = new_app(monkeypatch)
    assert "start_btn" not in keys(at.button)  # 오늘의 분석이 보이지 않음
    assert {"login_id", "login_pw"} <= keys(at.text_input)
    with pytest.raises(ValueError, match="Could not find a navigation page"):
        at.switch_page("views/today.py")  # 로그인 전에는 페이지 자체가 등록되지 않음


def test_login_demo_accounts_hint(monkeypatch):
    at = new_app(monkeypatch)
    assert not at.table  # demo_password가 없으면 안내 없음

    monkeypatch.setitem(SECRETS["auth"]["users"], "team2", {**SECRETS["auth"]["users"]["team2"],
                                                            "demo_password": PASSWORD})
    at = new_app(monkeypatch)
    rows = at.table[0].value
    assert list(rows["아이디"]) == ["team2"] and list(rows["비밀번호"]) == [PASSWORD]
    assert list(rows["구분"]) == ["작업자 (2팀)"]
    login(at, "team2")  # 안내한 계정으로 실제 로그인


def test_login_wrong_then_right(monkeypatch):
    at = new_app(monkeypatch)
    try_login(at, "admin", "wrong-password")
    assert not at.exception
    assert "user" not in at.session_state
    assert any("올바르지 않습니다" in e.value for e in at.error)
    login(at)
    assert "start_btn" in keys(at.button)  # 오늘의 분석으로 이동
    assert at.session_state["user"]["name"] == "설비보전 관리자"


def test_login_non_admin_rejected(monkeypatch):
    at = new_app(monkeypatch)
    try_login(at, "viewer", PASSWORD)
    assert "user" not in at.session_state and at.error


def test_lockout_after_five_failures():
    now = 1_000_000.0
    for _ in range(auth.MAX_FAILURES):
        user, _ = auth.login(SECRETS, "admin", "nope", now=now)
        assert user is None
    user, msg = auth.login(SECRETS, "admin", PASSWORD, now=now + 10)  # 맞는 비밀번호도 잠김
    assert user is None and "잠겼습니다" in msg
    user, _ = auth.login(SECRETS, "admin", PASSWORD, now=now + auth.LOCK_SECONDS + 1)  # 5분 뒤 해제
    assert user is not None


def test_logout(monkeypatch):
    at = login(new_app(monkeypatch))
    at.button(key="logout_btn").click().run()
    assert not at.exception
    assert "user" not in at.session_state and "start_btn" not in keys(at.button)


# ---------------------------------------------------------------- 오늘의 분석 (목업)
def test_mock_flow(monkeypatch):
    at = login(new_app(monkeypatch))

    # 대시보드: 점검 시작
    at.button(key="start_btn").click().run()
    assert not at.exception
    result = at.session_state["result"]
    assert result is not None and len(result["pred"]) == 100

    # 승인: 첫 번째 거절, 나머지 승인 → 결정 제출
    radios = decision_radios(at)
    if radios:
        assert at.button(key="submit_decisions").disabled  # 다 고르기 전에는 비활성
        for i, r in enumerate(radios):
            r.set_value("거절" if i == 0 else "승인")
        at.run()
        assert not at.button(key="submit_decisions").disabled
        at.button(key="submit_decisions").click().run()
        assert not at.exception
        decisions = at.session_state["decisions"]
        assert len(decisions) == len(radios) and list(decisions.values())[0] == "거절"
        assert not decision_radios(at)  # 결정 후 승인 대기 없음

    # 채팅 1회
    chat_box(at).set_value("10월에 베어링 고장 몇 건이야?").run()
    assert not at.exception
    chat = at.session_state["chat"]
    assert [m["role"] for m in chat] == ["user", "assistant"]
    assert chat[1]["route"].startswith("경로:")

    # 사이드바: 다음 날로 → 어제 검증
    at.button(key="next_day_btn").click().run()
    assert not at.exception
    assert at.session_state["prev"] is not None and at.session_state["result"] is None


def subheaders(at: AppTest) -> list[str]:
    return [h.value for h in at.subheader]


def test_mock_dashboard_schedule_card(monkeypatch):
    at = login(new_app(monkeypatch))
    at.button(key="start_btn").click().run()
    assert not at.exception
    titles = subheaders(at)
    assert not any(t.startswith("정비 배정안") for t in titles)  # 배정안 카드는 팀별 일정과 중복이라 없앴다
    assert "팀별 하루 일정" in titles  # v1은 버전 생략
    if not at.session_state["result"]["plan"].empty:
        table = next(d.value for d in at.dataframe if "배정 사유" in d.value.columns)
        cols = list(table.columns)
        assert cols.index("고장 확률") < cols.index("점수") and cols.index("중요도") < cols.index("점수")
        assert any(str(c.value).startswith("점수 = 고장 확률") for c in at.caption)

    # 재계획 결과(배정안 v2)를 넣으면 제목에 버전, 아래에 사유 (목업은 재계획을 지원하지 않아 결과만 바꿔 확인)
    at.session_state["result"] = {**at.session_state["result"], "plan_version": 2,
                                  "plan_reason": "2팀 오늘 교육이라 불가", "plan_note": "2팀 작업을 다른 팀으로"}
    at.run()
    assert not at.exception
    assert "팀별 하루 일정 · 배정안 v2" in subheaders(at)
    assert any(str(c.value) == "재계획 사유: \"2팀 오늘 교육이라 불가\" — 2팀 작업을 다른 팀으로" for c in at.caption)

    # 계획이 비면 팀별 하루 일정 카드 안에 안내 한 줄
    import pandas as pd

    at.session_state["result"] = {**at.session_state["result"], "plan": pd.DataFrame(), "plan_version": 1}
    at.run()
    assert not at.exception
    assert "팀별 하루 일정" in subheaders(at)
    assert any("24시간 내 고위험 설비가 없습니다" in str(s.value) for s in at.success)


def test_mock_chat_blocked_while_pending(monkeypatch):
    at = login(new_app(monkeypatch))
    at.button(key="start_btn").click().run()
    assert decision_radios(at)
    assert chat_box(at).disabled  # 대화창은 열리지만 입력은 비활성 + 경고
    assert any("승인 대기를 먼저 처리하세요" in str(w.value) for w in at.warning)


# ---------------------------------------------------------------- 승인 탭 (팀별 구역) · 채팅 대화창 (목업)
def group_headers(at: AppTest) -> list[str]:
    return [str(m.value) for m in at.markdown if " · " in str(m.value) and "건**" in str(m.value)]


def mock_cards(at: AppTest) -> dict[str, dict]:
    return {d["id"]: d for d in at.session_state["result"]["drafts"]}


def test_mock_approval_groups(monkeypatch):
    at = login(new_app(monkeypatch))
    at.button(key="start_btn").click().run()
    cards = mock_cards(at)
    headers = group_headers(at)
    teams = sorted({d["team"] for d in cards.values() if d["team"]})
    assert len(teams) >= 2
    for t in teams:  # 팀 구역 머리 "1팀 · N건"
        n = sum(d["team"] == t for d in cards.values())
        assert any(h.startswith(f"**{t} · {n}건**") for h in headers)
    if any(d["group"] == "발주" for d in cards.values()):
        assert any(h.startswith("**발주 · ") for h in headers)
    assert len(decision_radios(at)) == len(cards)  # 카드는 모두 어느 구역엔가 들어감
    shown = "\n".join([str(m.value) for m in at.markdown] + [str(c.value) for c in at.caption])
    assert "WO-" not in shown and "PR-" not in shown  # 초안 번호는 화면에 없음
    assert "chat_fab" in keys(at.button)
    assert not any(c.key == "chat_input" for c in at.chat_input)  # 승인 탭 본문에 채팅 입력 없음


def test_mock_approve_team_and_submit_by_id(monkeypatch):
    at = login(new_app(monkeypatch))
    at.button(key="start_btn").click().run()
    cards = mock_cards(at)
    team1 = [i for i, d in cards.items() if d["team"] == "1팀"]
    team2 = [i for i, d in cards.items() if d["team"] == "2팀"]
    assert team1 and team2
    at.button(key="approve_all_1팀").click().run()  # 1팀 카드만 승인으로
    value = {str(r.key).split("_", 2)[2]: r.value for r in decision_radios(at)}
    assert all(value[i] == "승인" for i in team1)
    assert all(v is None for i, v in value.items() if i not in team1)

    for r in decision_radios(at):  # 2팀만 거절, 나머지 승인 (화면 순서와 requests 순서가 달라도 id로 맞춘다)
        if r.value is None:
            r.set_value("거절" if str(r.key).split("_", 2)[2] in team2 else "승인")
    at.run()
    assert any(str(m.value).startswith(f"승인 {len(cards) - len(team2)} · 거절 {len(team2)} · 미결정 0")
               for m in at.markdown)
    at.button(key="submit_decisions").click().run()
    assert not at.exception
    decisions = at.session_state["decisions"]
    assert {i for i, v in decisions.items() if v == "거절"} == set(team2)
    assert not decision_radios(at)
    assert any(h.startswith("**2팀 · ") for h in group_headers(at))  # 결과 카드도 팀별 구역


def test_mock_chat_dialog(monkeypatch):
    at = login(new_app(monkeypatch))
    at.button(key="chat_fab").click().run()  # 점검 전이라 승인 대기 없음 -> 입력 가능
    assert at.session_state["chat_open"] and not chat_box(at).disabled
    chat_box(at).set_value("베어링 점검 방법").run()
    assert not at.exception
    assert [m["role"] for m in at.session_state["chat"]] == ["user", "assistant"]
    assert at.session_state["chat_open"] and any(c.key == "chat_input" for c in at.chat_input)  # 대화창 유지
    at.button(key="chat_clear").click().run()
    assert at.session_state["chat"] == []


def report_markers(at: AppTest) -> list[str]:
    """화면에 남아 있는 인수인계 리포트 표시·내려받기 흔적."""
    found = [h.value for h in at.subheader if "인수인계 리포트" in h.value]
    found += [b.label for b in at.button if "리포트" in b.label]
    found += [str(d.label) for d in at.get("download_button") if "리포트" in str(d.label)]
    return found


def test_approval_chat_tab_has_no_report(monkeypatch):
    """승인 탭에는 인수인계 리포트 표시 영역과 내려받기 버튼이 없다 (이력·보고서 페이지에서만)."""
    at = login(new_app(monkeypatch))
    at.button(key="start_btn").click().run()
    assert not report_markers(at)
    for r in decision_radios(at):
        r.set_value("승인")
    at.run()
    if decision_radios(at):
        at.button(key="submit_decisions").click().run()
    assert not at.exception and not report_markers(at)


def test_approval_card_four_lines(monkeypatch):
    """승인 카드: 설비 · 작업 · 시간 · 할 일 4줄. 팀 구역 안에서는 팀을 빼고 "시간"만 (팀은 구역 머리에),
    발주 카드는 "팀·시간" 자리에 희망 발주일."""
    at = login(new_app(monkeypatch))
    at.button(key="start_btn").click().run()
    if not decision_radios(at):
        pytest.skip("이 날짜는 승인 대기 초안이 없음")
    cards = [m.value for m in at.markdown if "card-line" in m.value and not m.value.startswith("<style")]
    assert cards and all(all(k in c for k in ("설비", "작업", "시간", "할 일")) for c in cards)
    team_cards = [c for c in cards if "희망 발주일" not in c]
    assert team_cards and all("팀·시간" not in c and "09:00-11:00" in c and "팀 09:00" not in c for c in team_cards)
    assert all("팀·시간" in c for c in cards if "희망 발주일" in c)


def test_finding_tab_three_lines_and_details(monkeypatch):
    """설비별 조사: 상단 요약 표 + 이상 / 1순위 원인 / 할 일 3줄, 나머지는 '자세히 보기'에 접힘."""
    at = login(new_app(monkeypatch))
    at.button(key="start_btn").click().run()
    if not at.session_state["result"]["findings"]:
        pytest.skip("이 날짜는 고위험 0대")
    tables = [df.value for df in at.dataframe]
    assert any(list(t.columns) == ["설비", "부품", "분류", "할 일"] for t in tables)
    lines = [m.value for m in at.markdown if "finding-line" in m.value and not m.value.startswith("<style")]
    assert lines and all(k in lines[0] for k in ("이상", "1순위 원인", "할 일"))
    details = [e for e in at.expander if e.label == "자세히 보기"]
    assert details and not details[0].proto.expanded


def test_dev_log_hidden_by_default(monkeypatch):
    at = login(new_app(monkeypatch))
    at.button(key="start_btn").click().run()
    for r in decision_radios(at):
        r.set_value("승인")
    at.run()
    if decision_radios(at):
        at.button(key="submit_decisions").click().run()
    chat_box(at).set_value("오늘 위험한 설비 있어?").run()
    assert at.session_state["chat"][-1]["route"].startswith("경로:")
    assert "개발자 로그" not in expander_labels(at)
    assert not any(c.value.startswith("경로:") for c in at.caption)  # 채팅 경로도 숨김

    at.session_state["dev_mode"] = True  # 설정 페이지에서 켠 것과 같음
    at.run()
    assert "개발자 로그" in expander_labels(at)


def test_dev_toggle_only_for_allow_dev(monkeypatch):
    at = login(new_app(monkeypatch))
    at.switch_page("views/settings.py").run()
    assert not at.exception
    assert "dev_toggle" in keys(at.toggle)
    at.toggle(key="dev_toggle").set_value(True).run()
    assert at.session_state["dev_mode"] is True

    at.session_state["user"] = {**at.session_state["user"], "allow_dev": False}
    at.run()
    assert "dev_toggle" not in keys(at.toggle)


def test_no_emoji_in_today_page(monkeypatch):
    import re

    at = login(new_app(monkeypatch))
    at.button(key="start_btn").click().run()
    texts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [b.label for b in at.button]
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⏳▫]")
    assert not [t for t in texts if emoji.search(str(t))]


# ---------------------------------------------------------------- 작업자
def test_worker_menu_hides_admin_pages(monkeypatch, temp_app_db):
    at = login(new_app(monkeypatch), "team2")
    assert "start_btn" not in keys(at.button)  # 오늘의 분석이 아니라 작업자 대시보드가 기본 페이지
    for page in ADMIN_PAGES:  # 관리자 페이지는 등록되지 않음 (메뉴에도 없고 열 수도 없음)
        with pytest.raises(ValueError, match="Could not find a navigation page"):
            at.switch_page(page)
    at.switch_page("views/worker_history.py").run()
    assert not at.exception


def sidebar_text(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.sidebar.markdown)


def test_sidebar_notification_cards(monkeypatch, temp_app_db):
    app_db = temp_app_db
    app_db.set_current_day("2015-10-14", "admin")
    with app_db.connect() as conn:  # 출동 확인 전 긴급 작업
        conn.execute('INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end",'
                     " status, created_at, urgent) VALUES ('UWO-1014-045-1', '2015-10-14', 45, 'bearing',"
                     " 'urgent_check', '2팀', '14:20', '15:20', 'ASSIGNED', ?, 1)", (app_db.now(),))
    app_db.notify(title="긴급 점검: #45 베어링 — 14:20 출동", to_role="worker", to_team="2팀",
                  link_wo_id="UWO-1014-045-1", as_of_date="2015-10-14", kind="urgent", head="#45 베어링",
                  line="긴급 점검 · 14:20 출동 · 2팀", body="근거 E1014-045")
    for m in (1, 2):
        app_db.notify(title=f"새 작업: #{m} 모터/전원부 교체", to_role="worker", to_team="2팀",
                      link_wo_id=f"WO-1014-00{m}", as_of_date="2015-10-14", kind="task")
    app_db.notify(title="조사 결과 추가됨: #3 밸브/펌프", to_role="worker", to_team="2팀", as_of_date="2015-10-14",
                  kind="finding", head="#3 밸브/펌프", line="점검 항목 갱신")
    plain_id = app_db._rows("SELECT MAX(id) AS i FROM notifications")[0]["i"]
    at = login(new_app(monkeypatch), "team2")
    assert not at.exception
    text = sidebar_text(at)
    assert 'nbadge-red">긴급</span> <b>#45 베어링</b>' in text and "긴급 점검 · 14:20 출동 · 2팀" in text
    assert "근거 E1014-045" not in text.split('title="')[0]  # body는 카드 글자가 아니라 마우스 올림에만
    assert "● `새 작업` 2건 (2팀)" in expander_labels(at)
    assert 'nbadge-gray">조사 결과' in text and "안 읽음 4" in text  # 긴급 1 + 새 작업 2 + 조사 결과 1
    assert "정보" not in text and not any("정보" in label for label in expander_labels(at))
    at.button(key=f"noti_ok_{plain_id}").click().run()  # [확인] -> 읽음, 줄이 사라짐
    assert not at.exception
    assert f"noti_ok_{plain_id}" not in keys(at.button) and "안 읽음 3" in sidebar_text(at)
    at.button(key="notiread_all").click().run()  # 모두 읽음: 긴급은 남는다
    text = sidebar_text(at)
    assert "안 읽음 1" in text and 'nbadge-red">긴급' in text
    assert "● `새 작업` 2건 (2팀)" not in expander_labels(at)


def noti_containers(node) -> dict[str, bool]:
    """사이드바 알림 카드 container: {key: border} (key = noti_{tier}_{kind}_{id}, Streamlit 1.66은 flex_container.border)."""
    out = {}
    for child in getattr(node, "children", {}).values():
        proto = getattr(child, "proto", None)
        if proto is not None and hasattr(proto, "flex_container") and "-noti_" in proto.id:
            out[proto.id.split("-", 2)[-1]] = proto.flex_container.border
        out.update(noti_containers(child))
    return out


def test_notification_cards_are_bordered_with_kind(monkeypatch, temp_app_db):
    app_db = temp_app_db
    app_db.set_current_day("2015-10-14", "admin")
    d = "2015-10-14"
    with app_db.connect() as conn:
        conn.execute('INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end",'
                     " status, created_at, urgent) VALUES ('UWO-1014-045-1', ?, 45, 'bearing', 'urgent_check', '2팀',"
                     " '14:20', '15:20', 'ASSIGNED', ?, 1)", (d, app_db.now()))
    app_db.notify(title="출동 확인 다시 알림: #45 베어링", to_role="admin", link_wo_id="UWO-1014-045-1", as_of_date=d,
                  kind="urgent", head="#45 베어링", line="긴급 점검 · 14:20 출동 · 2팀")
    app_db.notify(title="새 작업: #1 모터/전원부 교체", to_role="admin", link_wo_id="WO-1014-001", as_of_date=d, kind="task")
    app_db.notify(title="완료 보고: #9 (1팀) — 이상 없음", to_role="admin", link_wo_id="WO-1014-009", as_of_date=d,
                  kind="done", head="#9 구동부", line="이상 없음 · 1팀")
    app_db.notify(title="#12 긴급 점검을 3팀에 자동 배정했습니다", to_role="admin", link_wo_id="UWO-1014-012-1",
                  as_of_date=d, kind="assign", head="#12 밸브/펌프", line="긴급 점검 → 3팀 10:00")
    at = login(new_app(monkeypatch))
    assert not at.exception
    cards = noti_containers(at.sidebar)
    kinds = {k.split("_")[2]: k for k in cards if not k.startswith("noti_item_")}  # noti_{tier}_{kind}_{id}
    assert set(kinds) == {"urgent", "task", "done", "assign"}
    assert kinds["urgent"].startswith("noti_urgent_urgent_") and kinds["task"] == "noti_todo_task_all"
    assert kinds["done"].startswith("noti_plain_done_") and kinds["assign"].startswith("noti_plain_assign_")
    assert all(cards[k] for k in kinds.values())  # CSS가 안 먹어도 Streamlit 기본 테두리
    assert {"noti_ok_", "noti_go_"} <= {b.key[:8] for b in at.sidebar.button if b.key}  # [확인], 연결 작업 [열기]


def test_notifications_page(monkeypatch, temp_app_db):
    app_db = temp_app_db
    app_db.set_current_day("2015-10-14", "admin")
    app_db.notify(title="완료 보고: #90 (2팀) — 예측과 같음", to_role="admin", as_of_date="2015-10-14", kind="done",
                  head="#90 모터/전원부", line="예측과 같음 · 2팀")
    app_db.notify(title="#45 자동 조사 실패", to_role="admin", as_of_date="2015-10-13", kind="notice")
    at = login(new_app(monkeypatch))
    assert "완료" in sidebar_text(at) and "자동 조사 실패" not in sidebar_text(at)  # 이전 날짜는 사이드바에서 숨김
    at.switch_page("views/notifications.py").run()  # 알림 전체 보기: 숨긴 알림도 모두
    assert not at.exception
    at.radio(key="noti_period").set_value("전체").run()
    table = next(d.value for d in at.dataframe if "읽음" in d.value.columns)
    assert list(table.columns) == ["시각", "종류", "설비", "내용", "연결 작업", "읽음"]
    assert list(table["종류"]) == ["안내", "완료"] and list(table["설비"]) == ["#45", "#90 모터/전원부"]


def test_worker_notifications_page(monkeypatch, temp_app_db):
    fake_order(temp_app_db)
    at = login(new_app(monkeypatch), "team2")
    at.switch_page("views/notifications.py").run()
    assert not at.exception
    at.radio(key="noti_period").set_value("전체").run()
    table = next(d.value for d in at.dataframe if "연결 작업" in d.value.columns)
    assert list(table["연결 작업"]) == ["WO-1001-090"] and list(table["종류"]) == ["새 작업"]


def test_admin_menu_hides_worker_pages(monkeypatch, temp_app_db):
    at = login(new_app(monkeypatch))
    for page in WORKER_PAGES:
        with pytest.raises(ValueError, match="Could not find a navigation page"):
            at.switch_page(page)
    at.switch_page("views/work_status.py").run()
    assert not at.exception


def test_worker_notification_start_and_report(monkeypatch, temp_app_db):
    app_db = temp_app_db
    fake_order(app_db)
    app_db.set_current_day("2015-10-01", "admin")  # 이전 날짜 알림은 사이드바에서 숨기므로 작업 날짜로 맞춘다
    at = login(new_app(monkeypatch), "team2")
    note = next(b for b in at.button if str(b.key).startswith("noti_open_"))  # 새 작업 묶음 안 카드의 [열기]
    assert note.label == "열기" and "● `새 작업` 1건 (2팀)" in expander_labels(at)
    assert "#90 모터/전원부" in sidebar_text(at) and "교체 · 2팀 09:00~11:00" in sidebar_text(at)
    note.click().run()
    assert not at.exception
    assert at.session_state["focus_wo"] == "WO-1001-090"
    assert app_db.unread_count({"id": "team2", "role": "worker", "team": "2팀"}) == 0

    # 작업 시작 전: 관리자 설비별 조사와 같은 3줄(이상 / 1순위 원인 / 할 일) + 자세히 보기
    lines = [m.value for m in at.markdown if "finding-line" in m.value and not m.value.startswith("<style")]
    assert lines and all(k in lines[0] for k in ("이상", "1순위 원인", "할 일"))
    assert any(e.label == "자세히 보기" for e in at.expander)

    at.button(key="start_WO-1001-090").click().run()
    assert app_db.get_work_order("WO-1001-090")["status"] == "IN_PROGRESS"
    at.button(key="report_WO-1001-090").click().run()

    wid = "WO-1001-090"
    assert not [c for c in at.checkbox if str(c.key).startswith("chk_")]  # 체크박스 목록 없음
    assert not [s for s in at.selectbox if str(s.key).startswith(f"res_{wid}_")]  # 점검 단계별 결과 입력 없음
    assert not [e for e in at.expander if e.label == "자세히 보기"]  # 작업 시작 후에는 조사 내용을 접어 둠
    assert any(e.label == "조사 내용 다시 보기" for e in at.expander)
    submit = lambda: next(b for b in at.button if b.label == "완료 보고 제출").click().run()  # noqa: E731

    at.text_area(key=f"memo_{wid}").input("온도 높음")
    submit()  # 메모 30자 미만 -> 거부
    assert any("30자 이상" in e.value for e in at.error)
    assert app_db.get_work_order(wid)["status"] == "IN_PROGRESS"

    # [메모 정리 도움]: 정리 초안이 메모 칸에 들어감 (LLM 대신 가짜 함수)
    monkeypatch.setattr("services.organize_memo", lambda memo, results=None: f"- 현장에서 발견한 상태: {memo}")
    next(b for b in at.button if b.label == "메모 정리 도움").click().run()
    assert at.text_area(key=f"memo_{wid}").value == "- 현장에서 발견한 상태: 온도 높음"

    indexed = []
    monkeypatch.setattr("services._save_feedback", lambda *a, **k: None)  # 장기 기억은 test_closed_loop에서 확인
    monkeypatch.setattr("services.field_notes.index_report", lambda wo, report: indexed.append(report))
    at.text_area(key=f"memo_{wid}").input("하우징 온도 68도로 높고 갈리는 소리가 남. 윤활 부족으로 판단. 교체 후 진동 정상.")
    at.text_input(key=f"dev_{wid}").input("지시대로 교체")
    submit()
    assert not at.exception
    assert app_db.get_work_order(wid)["status"] == "DONE"
    rep = app_db.work_reports()[0]
    assert rep["actual_cause_type"] == "예측과 같음" and rep["deviation"] == "지시대로 교체"
    assert rep["step_results"] == []
    assert indexed and indexed[0]["memo"].startswith("하우징 온도 68도")
    assert any(n["title"].startswith("완료 보고: #90") for n in app_db.notifications_for({"role": "admin"}))


def test_admin_sees_full_worker_memo(monkeypatch, temp_app_db):
    """관리자 작업 현황: 작업자 메모를 자르지 않고 토글(<details>)로 전부 볼 수 있다."""
    import services

    app_db = temp_app_db
    fake_order(app_db)
    monkeypatch.setattr("services._save_feedback", lambda *a, **k: None)
    monkeypatch.setattr("services.field_notes.index_report", lambda wo, report: None)
    worker = {"id": "team2", "role": "worker", "team": "2팀"}
    long_memo = ("단자대 그을림 발견, 입력 전압 190V. " * 12 + "끝부분 문장: 다음 점검 때 단자 체결 토크 확인.").strip()
    assert len(long_memo) > 300
    services.start_work("WO-1001-090", worker)
    services.complete_work("WO-1001-090", worker, "예측과 같음", "motor", "부품 교체", 95, 1, long_memo,
                           "지시대로 교체")
    app_db.set_current_day("2015-10-01", "admin")

    at = login(new_app(monkeypatch))
    at.switch_page("views/work_status.py").run()
    assert not at.exception
    toggles = [m.value for m in at.markdown if "<details class='report-toggle'" in m.value]
    assert toggles and "끝부분 문장: 다음 점검 때 단자 체결 토크 확인." in toggles[0]  # 잘리지 않음
    assert "<details class='report-toggle'>" in toggles[0]  # 기본은 접힌 토글
    assert "지시대로 교체" in toggles[0]

    at.session_state["focus_wo"] = "WO-1001-090"  # 알림에서 열면 메모가 펼쳐진 상태
    at.run()
    assert any("<details class='report-toggle' open>" in m.value for m in at.markdown)


def test_worker_follows_admin_date(monkeypatch, temp_app_db):
    """관리자가 고른 기준 날짜를 작업자 화면이 따른다 (작업자에게는 날짜 선택이 없음)."""
    app_db = temp_app_db
    fake_order(app_db)  # 2015-10-01 2팀 작업

    admin = login(new_app(monkeypatch))
    admin.date_input(key="as_of_date").set_value(date(2015, 10, 1)).run()
    assert app_db.current_day()["day"] == "2015-10-01" and app_db.current_day()["updated_by"] == "admin"

    worker = login(new_app(monkeypatch), "team2")
    assert not worker.date_input and not [s for s in worker.selectbox if s.key == "worker_day"]
    assert any("2015-10-01" in m.value for m in worker.sidebar.markdown)
    worker.switch_page("views/worker_tasks.py").run()
    assert "start_WO-1001-090" in keys(worker.button)

    admin.button(key="next_day_btn").click().run()  # 관리자 [다음 날로] -> 10-02
    assert app_db.current_day()["day"] == "2015-10-02"
    worker.run()  # 실제 화면은 알림 패널이 10초 안에 바뀐 날짜를 보고 다시 그린다
    assert any("2015-10-02" in m.value for m in worker.sidebar.markdown)
    assert "start_WO-1001-090" not in keys(worker.button)  # 10-01 미완료 작업은 오늘 작업 목록이 아니라
    assert any("밀린 작업 1건" in i.value for i in worker.info)  # 대시보드 달력에서 본다 (안내 한 줄)
    worker.switch_page("views/worker_dashboard.py").run()
    assert any(m.value == "**10-01 (1일 지남) · 1건**" for m in worker.markdown)  # 가장 오래된 밀린 날짜를 먼저
    assert any(e.label == "#90 모터 교체 · 09:00 · 배정 · 1일 지남" for e in worker.expander)


def test_admin_login_resumes_shared_date(monkeypatch, temp_app_db):
    temp_app_db.set_current_day("2015-12-22", "admin")
    at = login(new_app(monkeypatch))
    assert at.session_state["day"] == date(2015, 12, 22)


def test_worker_sees_only_own_team(monkeypatch, temp_app_db):
    fake_order(temp_app_db, team="1팀")
    at = login(new_app(monkeypatch), "team2")
    assert not [b for b in at.button if str(b.key).startswith(("noti_", "start_"))]


# ---------------------------------------------------------------- 작업자 대시보드
DASHBOARD_TITLES = ["오늘 시간표", "달력", "내 설비 실시간 센서", "점검 결과 분포", "조치 분포"]


def section_titles(at: AppTest) -> list[str]:
    return [m.value.removeprefix("#### ") for m in at.markdown if m.value.startswith("#### ")]


def test_worker_dashboard_is_first_page(monkeypatch, temp_app_db):
    fake_order(temp_app_db)  # 2015-10-01 2팀 09:00~11:00 #90 모터 교체
    at = login(new_app(monkeypatch), "team2")
    assert section_titles(at) == DASHBOARD_TITLES
    assert [m.label for m in at.metric] == ["오늘 완료", "남은 작업 시간", "밀린 작업"]
    assert at.metric[0].value == "0/1" and at.metric[1].value == "2시간"  # 모터 교체 표준 120분
    assert at.get("plotly_chart")  # 오늘 시간표 간트
    item = next(e for e in at.expander if e.label.startswith("#90 모터 교체"))  # 선택 날짜(기준 날짜) 작업 목록
    assert item.label == "#90 모터 교체 · 09:00 · 배정"
    assert "점검 전 설비 정지 절차 확인" in markdown_text(at)  # 펼친 내용: 점검 항목
    assert not [e for e in at.expander if e.label == "자세히 보기"]  # 무거운 조사 내용·센서 그래프는 그리지 않음
    rows = at.dataframe[0].value  # 내 설비 실시간 센서
    assert list(rows["설비"]) == [90] and rows["데이터"].iloc[0] == "시간 단위"
    assert rows["전압"].iloc[0].endswith("%)")  # 모터 <-> 전압: "181.2 (+6%)"
    at.button(key="dash_start_WO-1001-090").click().run()
    assert not at.exception
    assert temp_app_db.get_work_order("WO-1001-090")["status"] == "IN_PROGRESS"
    at.button(key="dash_trend_WO-1001-090").click().run()  # [센서 추이 보기]를 눌러야 그래프
    assert not at.exception and len(at.get("plotly_chart")) >= 2


def test_worker_dashboard_hides_other_team(monkeypatch, temp_app_db):
    fake_order(temp_app_db, team="1팀")
    temp_app_db.set_current_day("2015-10-01", "admin")
    at = login(new_app(monkeypatch), "team2")
    assert not at.exception
    assert "#90" not in markdown_text(at) and not [e for e in at.expander if "#90" in e.label]
    assert any(i.value == "오늘 배정된 작업이 없습니다." for i in at.info)
    assert any(i.value == "지금 맡은 설비가 없습니다." for i in at.info)
    assert at.metric[0].value == "0/0"


def test_worker_history_top_machines(monkeypatch, temp_app_db):
    import services

    fake_order(temp_app_db)
    monkeypatch.setattr("services._save_feedback", lambda *a, **k: None)
    monkeypatch.setattr("services.field_notes.index_report", lambda wo, report: None)
    worker = {"id": "team2", "role": "worker", "team": "2팀"}
    services.start_work("WO-1001-090", worker)
    services.complete_work("WO-1001-090", worker, "다른 부품", "pump", "부품 교체", 95, 1,
                           "하우징 온도 68도로 높고 갈리는 소리가 남. 펌프 쪽 누유 확인 후 교체.", "")
    temp_app_db.set_current_day("2015-10-01", "admin")
    at = login(new_app(monkeypatch), "team2")
    at.switch_page("views/worker_history.py").run()
    assert not at.exception
    assert "자주 손본 설비 Top 5" in section_titles(at)
    top = at.dataframe[-1].value
    assert list(top.columns) == ["순위", "설비", "작업 수", "주요 원인", "주요 부품", "마지막 작업일", "미완료"]
    assert top.iloc[0].tolist() == [1, 90, 1, "다른 부품", "밸브/펌프", "2015-10-01", 0]


# ---------------------------------------------------------------- 이력·보고서, 설정
# ---------------------------------------------------------------- 입력 데이터 → 경고 → 긴급 점검 (LLM 없음)
def markdown_text(at: AppTest) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def test_no_data_input_menu(monkeypatch):
    at = login(new_app(monkeypatch))
    with pytest.raises(ValueError, match="Could not find"):  # 데이터 입력 페이지는 메뉴·파일 모두 없앴다
        at.switch_page("views/live_input.py")


def test_scenario_alert_banner(monkeypatch, temp_app_db):
    import live_ops

    monkeypatch.setattr(live_ops, "RUN_ASYNC", False)  # 자동 조사·배정을 바로 실행 (목업 조사, LLM 없음)
    monkeypatch.setattr(live_ops, "INVESTIGATE", live_ops.mock_investigate)
    temp_app_db.set_current_day("2015-11-12", "admin")
    live_ops.add_scenario(45, "bearing_vibration", "2015-11-12 06:00:00", created_by="admin")
    at = login(new_app(monkeypatch))  # 오늘의 분석 (대시보드)
    assert not at.exception
    text = markdown_text(at)
    assert "alert-banner" in text and "경고 1건" in text  # 관리자 공통 상단 배너
    assert "오늘 경고 1건 (점검 배정 1)" in text  # 실시간 감지 요약 줄
    orders = [w for w in temp_app_db.work_orders() if w["urgent"]]
    assert len(orders) == 1 and orders[0]["approved_by"] == live_ops.AUTO_APPROVER


def test_worker_urgent_box_dispatch(monkeypatch, temp_app_db):
    import live_ops

    monkeypatch.setattr(live_ops, "RUN_ASYNC", False)
    monkeypatch.setattr(live_ops, "INVESTIGATE", live_ops.mock_investigate)
    temp_app_db.set_current_day("2015-11-12", "admin")
    alert = live_ops.add_scenario(45, "bearing_vibration", "2015-11-12 06:00:00", created_by="admin")["alerts"][0]
    wo = temp_app_db.get_work_order(live_ops.get_alert(alert["alert_id"])["wo_id"])
    assert (wo["team"], wo["start"]) == ("2팀", "09:00")  # 베어링 = 2팀 전문, 출동 시각 = 지금(고정 09:00)
    at = login(new_app(monkeypatch), "team2")
    assert "긴급 점검: #45" in markdown_text(at)  # 대시보드에도 같은 [출동 확인] 상자
    assert f"dispatch_{wo['wo_id']}" in keys(at.button)
    at.switch_page("views/worker_tasks.py").run()
    text = markdown_text(at)
    assert "긴급 점검: #45" in text and 'badge-red">긴급' in text and "출동 09:00" in text
    next(b for b in at.button if b.key == f"dispatch_{wo['wo_id']}").click()
    at.run()
    assert not at.exception
    assert temp_app_db.get_work_order(wo["wo_id"])["status"] == "IN_PROGRESS"
    assert live_ops.get_alert(alert["alert_id"])["status"] == "출동"


# ---------------------------------------------------------------- 실시간 감지 (오늘의 분석 › 대시보드 카드)
LIVE = "dash_"  # render_live_monitor("dash")의 위젯 key 접두어


def open_live(at: AppTest) -> AppTest:
    at.toggle(key="live_open").set_value(True).run()
    assert not at.exception
    return at


def alert_tables(at: AppTest) -> list:
    return [d for d in at.dataframe if "진행 상태" in d.value.columns]


def test_no_live_monitor_menu(monkeypatch):
    at = login(new_app(monkeypatch))
    with pytest.raises(ValueError, match="Could not find"):  # 실시간 모니터 페이지는 대시보드 카드로 옮겼다
        at.switch_page("views/live_stream.py")


def test_live_card_closed_then_open(monkeypatch, temp_app_db):
    at = login(new_app(monkeypatch))
    assert not at.exception
    assert "실시간 감지" in [h.value for h in at.subheader]
    text = markdown_text(at)
    assert 'badge-gray">정지' in text and "대상 -" in text and "오늘 경고 없음" in text  # 요약 줄은 항상
    assert not any(str(b.key).startswith(LIVE) for b in at.button)  # 닫혀 있으면 제어·주입·차트가 없음
    assert "입력 데이터 지우기" not in [e.label for e in at.expander]
    open_live(at)
    assert {f"{LIVE}stream_start", f"{LIVE}stream_stop", f"{LIVE}inj_drift", f"{LIVE}inj_reset"} <= keys(at.button)
    assert any("1초 판단은 설비별 평소값과의 통계 비교" in str(c.value) for c in at.caption)
    assert "입력 데이터 지우기" in [e.label for e in at.expander]
    assert at.button(key=f"{LIVE}clear_all").disabled  # 체크해야 활성
    at.checkbox(key=f"{LIVE}clear_sure").check().run()
    at.button(key=f"{LIVE}clear_all").click().run()
    assert not at.exception
    assert any("입력 데이터를 지웠습니다 (스트림 0행" in str(m.value) for m in at.success)
    at.switch_page("views/work_status.py").run()  # 다른 페이지에 다녀와도 열린 채
    at.switch_page("views/today.py").run()
    assert at.session_state["live_open"] and f"{LIVE}stream_start" in keys(at.button)


def test_dashboard_order_schedule_then_live(monkeypatch, temp_app_db):
    at = login(new_app(monkeypatch))
    titles = [h.value for h in at.subheader]
    assert "실시간 감지" in titles and "팀별 하루 일정" not in titles  # 점검 전에도 같은 자리에 카드
    at.button(key="start_btn").click().run()
    titles = [h.value for h in at.subheader]
    assert titles.index("팀별 하루 일정") < titles.index("실시간 감지")
    at.button(key="next_day_btn").click().run()  # 어제 예측 검증은 실시간 감지 다음
    titles = [h.value for h in at.subheader]
    verify = next(i for i, t in enumerate(titles) if t.endswith("예측 검증"))
    assert titles.index("실시간 감지") < verify


def test_live_card_alert_table_and_new_badge(monkeypatch, temp_app_db):
    import live_ops

    monkeypatch.setattr(live_ops, "RUN_ASYNC", False)
    monkeypatch.setattr(live_ops, "INVESTIGATE", live_ops.mock_investigate)
    temp_app_db.set_current_day("2015-11-12", "admin")
    live_ops.add_scenario(45, "bearing_vibration", "2015-11-12 06:00:00", created_by="admin")
    at = login(new_app(monkeypatch))
    assert alert_tables(at) == []  # 닫혀 있으면 경고 표도 없음 (요약 줄만)
    assert 'badge-red">새 경고' not in markdown_text(at)  # 처음 본 경고는 새 경고가 아님
    live_ops.add_scenario(12, "pump_pressure", "2015-11-12 06:00:00", created_by="admin")  # 닫힌 채 새 경고
    at.run()
    assert 'badge-red">새 경고' in markdown_text(at)
    open_live(at)
    assert len(alert_tables(at)) == 1 and len(alert_tables(at)[0].value) == 2  # 카드 안 한 번만 (중복 없음)
    assert 'badge-red">새 경고' not in markdown_text(at)


def inject_buttons(at: AppTest) -> dict:
    prefix = f"{LIVE}inj_"
    return {b.key.removeprefix(prefix): b.proto.type for b in at.button if b.key and b.key.startswith(prefix)}


def test_live_card_inject_toggle(monkeypatch, temp_app_db):
    at = open_live(login(new_app(monkeypatch)))
    assert inject_buttons(at)["drift"] == "secondary" and at.button(key=f"{LIVE}inj_reset").disabled
    at.button(key=f"{LIVE}inj_drift").click().run()  # 켜기 -> 진한 버튼, 주입 1건
    assert not at.exception
    inj = temp_app_db.get_state("stream_inject", [])
    assert [(i["machine_id"], i["kind"]) for i in inj] == [(45, "drift")]
    assert inject_buttons(at)["drift"] == "primary" and not at.button(key=f"{LIVE}inj_reset").disabled
    at.button(key=f"{LIVE}inj_drift").click().run()  # 같은 버튼 다시 -> 끔
    assert temp_app_db.get_state("stream_inject", []) == []
    assert inject_buttons(at)["drift"] == "secondary"
    at.button(key=f"{LIVE}inj_spike").click().run()  # 다른 버튼 -> 바뀜 (설비당 하나)
    at.button(key=f"{LIVE}inj_step").click().run()
    assert [i["kind"] for i in temp_app_db.get_state("stream_inject", [])] == ["step"]
    assert inject_buttons(at) == {"drift": "secondary", "spike": "secondary", "step": "primary",
                                  "stuck": "secondary", "reset": "secondary"}


def test_live_card_inject_other_sensor_and_recover(monkeypatch, temp_app_db):
    at = open_live(login(new_app(monkeypatch)))
    at.button(key=f"{LIVE}inj_drift").click().run()  # 기본 센서 진동
    at.selectbox(key=f"{LIVE}inj_sensor").set_value("volt").run()  # 다른 센서를 고르면 켜짐 안내
    assert inject_buttons(at)["drift"] == "primary"
    assert any(str(c.value).startswith("켜짐: #45 진동 서서히 상승 · ") for c in at.caption)
    temp_app_db.set_state("stream_inject", [{"machine_id": 45, "kind": "recover", "sensor": "vibration",
                                             "from": 60.0, "started_at": "2026-01-01T00:00:00.000"}])
    at.run()  # 정비 후 복귀는 켜짐으로 보지 않음
    assert "primary" not in inject_buttons(at).values() and at.button(key=f"{LIVE}inj_reset").disabled


def test_live_card_no_generator_badge(monkeypatch, temp_app_db):
    # 실행 명령은 있는데 생성기 프로세스가 없음 (heartbeat 없음, 시작한 지 오래됨) -> "응답 없음"
    temp_app_db.set_state("stream_running", True)
    temp_app_db.set_state("stream_machines", [45])
    temp_app_db.set_state("stream_started_at", "2026-01-01T00:00:00.000")
    at = login(new_app(monkeypatch))
    assert not at.exception
    assert 'badge-red">응답 없음' in markdown_text(at) and "대상 #45" in markdown_text(at)  # 닫혀 있어도 요약 줄에


# ---------------------------------------------------------------- 작업자 주간 보고서
def seed_week(app_db) -> None:
    """2015-11-12(목) 기준 주 11/09~11/15: 2팀 작업 2건(완료 1, 긴급 미완료 1) + 1팀 작업 1건 (섞이면 안 됨)."""
    app_db.set_current_day("2015-11-12", "admin")
    with app_db.connect() as conn:
        for wo_id, team, m, status, urgent in [("WO-1110-090", "2팀", 90, "DONE", 0),
                                               ("UWO-1111-045-1", "2팀", 45, "ASSIGNED", 1),
                                               ("WO-1110-033", "1팀", 33, "DONE", 0)]:
            conn.execute('INSERT INTO work_orders (wo_id, as_of_date, machine_id, comp, task_type, team, start, "end",'
                         " status, created_at, urgent) VALUES (?, ?, ?, 'motor', 'replace', ?, '09:00', '11:00', ?, ?, ?)",
                         (wo_id, f"2015-11-{wo_id[5:7]}", m, team, status, app_db.now(), urgent))
        conn.execute("INSERT INTO work_reports (wo_id, machine_id, as_of_date, predicted_comp, actual_cause_type,"
                     " actual_comp, action, duration_min, parts_used, team, reported_by, reported_at)"
                     " VALUES ('WO-1110-090', 90, '2015-11-10', 'motor', '예측과 같음', 'motor', '부품 교체', 130, 1,"
                     " '2팀', 'team2', ?)", (app_db.now(),))


def test_worker_weekly_report_page(monkeypatch, temp_app_db, tmp_path):
    from store import weekly_report as wr

    monkeypatch.setattr(wr, "WEEKLY_DIR", tmp_path / "weekly")
    seed_week(temp_app_db)
    at = login(new_app(monkeypatch), "team2")
    at.switch_page("views/worker_report.py").run()
    assert not at.exception
    assert [m.label for m in at.metric] == ["완료율", "긴급 출동", "총 작업 시간", "예측 적중률", "이월"]
    assert [m.value for m in at.metric] == ["50%", "1건", "2시간 10분", "100%", "1건"]
    assert len(at.get("plotly_chart")) >= 3  # 일별 작업량, 점검 결과, 조치, 계획 대비 실제
    tasks = next(d.value for d in at.dataframe if "작업 종류" in d.value.columns)
    assert sorted(tasks["설비"]) == [45, 90]  # 1팀 #33은 섞이지 않음
    assert "이번 주 2팀은 작업 2건 중 1건(50%)을 완료했습니다." in at.text_area(key="wr_summary_2팀_2015-11-09").value
    assert any("자동 문장" in str(c.value) for c in at.caption)  # 목업 = 템플릿 문장
    assert at.button(key="wr_next").disabled  # 기준 날짜가 속한 주보다 뒤로는 못 감
    at.button(key="wr_submit").click().run()
    assert not at.exception and any("제출했습니다" in str(s.value) for s in at.success)
    assert wr.get_weekly("2팀", "2015-11-09")["status"] == "제출"
    at.button(key="wr_prev").click().run()  # 지난주: 기록 없음
    assert [m.value for m in at.metric][0] == "-" and any("기록 없음" in str(c.value) for c in at.caption)

    admin = login(new_app(monkeypatch))
    admin.switch_page("views/history.py").run()
    assert not admin.exception
    assert [t.label for t in admin.tabs][:4] == ["주간 보고서", "승인 내역", "보고서", "예측 검증"]  # 첫 탭
    weekly = next(d.value for d in admin.dataframe if "제출 시각" in d.value.columns)
    assert list(weekly["팀"]) == ["2팀"] and list(weekly["상태"]) == ["제출"]


def test_history_page(monkeypatch):
    at = login(new_app(monkeypatch))
    at.switch_page("views/history.py").run()
    assert not at.exception
    assert {"hist_status", "hist_kind", "hist_machine"} <= keys(at.selectbox)


def test_settings_page(monkeypatch, tmp_path):
    import ops_settings

    monkeypatch.setattr(ops_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    at = login(new_app(monkeypatch))
    at.switch_page("views/settings.py").run()
    assert not at.exception
    assert at.button(key="reset_btn").disabled  # 확인 체크 전에는 비활성
    at.checkbox(key="reset_confirm").check().run()
    assert not at.button(key="reset_btn").disabled


def test_settings_validation(tmp_path, monkeypatch):
    import ops_settings

    monkeypatch.setattr(ops_settings, "SETTINGS_PATH", tmp_path / "settings.json")
    bad = {**ops_settings.DEFAULTS, "work_start": "18:00", "work_end": "09:00"}
    assert ops_settings.save_settings(bad) and not (tmp_path / "settings.json").exists()
    bad = {**ops_settings.DEFAULTS, "lunch_start": "08:00", "lunch_end": "09:00"}
    assert any("점심" in e for e in ops_settings.save_settings(bad))
    good = {**ops_settings.DEFAULTS, "prep_min": 15}
    assert ops_settings.save_settings(good) == [] and ops_settings.load_settings()["prep_min"] == 15


# ---------------------------------------------------------------- 실제 연결 (LLM)
@pytest.mark.llm
def test_real_no_target_day(monkeypatch):
    """2015-10-06 (고위험 0대): 실제 Supervisor로 점검 → 작업 지시 없이 완료, 리포트 저장.
    재고 0 부품(pump, 임시 운영 DB라 입고 대기 발주 없음)은 보충 발주만 승인 대기, 대시보드에 재고 0 알림."""
    at = login(new_app(monkeypatch, mock=False, timeout=300))
    at.date_input(key="as_of_date").set_value(date(2015, 10, 6)).run()
    at.button(key="start_btn").click().run()  # 시작 → 백그라운드 실행 → 화면이 끝날 때까지 진행 표시
    assert not at.exception
    assert at.session_state["error"] is None
    result = at.session_state["result"]
    assert result is not None and result["findings"] == []
    assert [r["draft_id"] for r in at.session_state["pending"] or []] == ["PR-1006-pump"]
    assert any("재고 0 부품" in m.value for m in at.markdown)
    import services

    assert "오늘 정비 대상 없음" in (services.get_report("2015-10-06 06:00:00") or "")  # 리포트는 이력 페이지에서 봄


@pytest.mark.llm
def test_real_morning_flow(monkeypatch):
    """2015-10-01: 점검 → 병렬 조사 진행 기록 → 승인 대기 → 결정 제출(첫 건 거절) → 리포트, 처리자 기록."""
    from tools.plan_tools import load_drafts

    at = login(new_app(monkeypatch, mock=False, timeout=600))
    at.date_input(key="as_of_date").set_value(date(2015, 10, 1)).run()
    at.button(key="start_btn").click().run()
    assert not at.exception and at.session_state["error"] is None
    assert len(at.session_state["result"]["findings"]) == 5
    assert any("investigate_machines" in line for line in at.session_state["trace"])

    radios = decision_radios(at)
    assert radios and chat_box(at).disabled  # 승인 대기 중 채팅 비활성
    ids = [str(r.key).split("_", 2)[2] for r in radios]  # key = dec_{thread_id}_{draft_id}
    for i, r in enumerate(radios):
        r.set_value("거절" if i == 0 else "승인")
    at.run()
    at.button(key="submit_decisions").click().run()
    assert not at.exception
    assert at.session_state["pending"] is None
    statuses = [d["status"] for d in at.session_state["result"]["drafts"]]
    assert statuses.count("REJECTED") == 1 and statuses.count("APPROVED") == len(radios) - 1
    import services

    assert "교대 인수인계 리포트" in (services.get_report("2015-10-01 06:00:00") or "")  # 자동 작성은 그대로
    assert not chat_box(at).disabled
    drafts = load_drafts("2015-10-01 06:00:00")
    assert all(drafts[i].get("decided_by") == "admin" and drafts[i].get("decided_at") for i in ids)


@pytest.mark.llm
def test_real_replan_flow(monkeypatch):
    """2015-10-01: 점검 → 재계획 사유 입력 → 새 승인 카드(-v2), 배정안 v2, 비교표, 대체된 초안."""
    at = login(new_app(monkeypatch, mock=False, timeout=600))
    at.date_input(key="as_of_date").set_value(date(2015, 10, 1)).run()
    at.button(key="start_btn").click().run()
    assert decision_radios(at)

    reason_box = next(t for t in at.text_input if str(t.key).startswith("replan_reason_"))
    reason_box.set_value("2팀 오늘 교육이라 불가").run()
    at.button(key="replan_btn").click().run()
    assert not at.exception and at.session_state["error"] is None
    result = at.session_state["result"]
    assert result["plan_version"] == 2 and not result["diff"].empty
    assert all(r["draft_id"].endswith("-v2") for r in at.session_state["pending"])
    assert any(d["status"] == "SUPERSEDED" for d in result["drafts"])
    assert all("2팀" not in s for s in result["plan"]["slot"])


@pytest.mark.llm
def test_real_chat_guard(monkeypatch):
    """실제 연결에서 제어 요청은 입력 가드가 차단 (모델 호출 없음)."""
    at = login(new_app(monkeypatch, mock=False, timeout=120))
    chat_box(at).set_value("45번 설비 정지시켜").run()
    assert not at.exception
    reply = at.session_state["chat"][-1]
    assert "설비 직접 제어 요청은 처리할 수 없습니다" in reply["content"] and "입력 가드" in reply["route"]
