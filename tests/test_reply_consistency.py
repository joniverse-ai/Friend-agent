"""Main Quest 08 후보 A(말과 행동 불일치) 고침을 지키는 시험. AI를 부르지 않는다."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app.agent import TOOL_RESULT_HEADER, tool_result_message  # noqa: E402
from app.persona import DEFAULT_CHARACTER  # noqa: E402
from eval_reply_consistency import judge, with_header  # noqa: E402


def test_tool_results_go_right_before_the_answer_with_ok_marks():
    from app.tools import ToolResult

    results = [ToolResult("create_reminder", True, "약속을 저장했습니다: 11월 10일 09:00 '치과'."),
               ToolResult("cancel_reminder", False, "'회의'에 해당하는 약속을 찾지 못했습니다.")]
    message = tool_result_message(results)
    assert message["role"] == "system" and message["content"].startswith(TOOL_RESULT_HEADER)
    assert "- 성공: 약속을 저장했습니다" in message["content"]
    assert "- 실패: '회의'에 해당하는" in message["content"]
    assert "앞의 대화에서 '할 수 없다'고 했더라도" in message["content"]
    assert tool_result_message([]) is None


def test_chat_turn_puts_tool_results_after_the_user_message(monkeypatch):
    seen = []

    class SpyLLM:
        can_reason = False
        model = "spy"

        def chat_stream(self, messages):
            seen.append(messages)
            yield "응, 저장했어!"

        def complete_text(self, messages):
            return "요약"

    from app import agent
    from app.store.memory_store import MemoryStore

    store = MemoryStore()
    store.save_character("u1", DEFAULT_CHARACTER)
    list(agent.run_turn("내일 아침 8시에 물 마시라고 알려 줘", "u1", SpyLLM(), store, False))
    # 답 생성 호출 = 사용자 말이 들어 있는 호출(화면 효과 고르기 같은 다른 호출은 건너뛴다)
    messages = next(m for m in seen if any(x["role"] == "user" and "물 마시라고" in x["content"] for x in m))
    assert messages[-2] == {"role": "user", "content": "내일 아침 8시에 물 마시라고 알려 줘"}
    assert messages[-1]["role"] == "system" and "- 성공: 약속을 저장했습니다" in messages[-1]["content"]
    assert "[도구 결과]" not in messages[0]["content"]  # 지시문 중간에는 더 이상 넣지 않는다


def test_judge_rules():
    assert judge("problem", "미안, 그건 기억할 수 없어.") == (False, "약속을 바꿨는데 못 한다고 말함")
    assert judge("problem", "약속을 지울 수는 없어!")[0] is False
    assert judge("problem", "응, 11월 3일 친구 약속 저장해 뒀어!")[0] is True
    assert judge("normal-other", "내일은 맑을 거야!")[0] is True
    assert judge("normal-fail", "응, 회의 약속 지웠어!")[0] is False
    assert judge("normal-fail", "회의 약속은 못 찾았어. 이름을 다시 알려 줄래?")[0] is True


def test_with_header_swaps_only_the_header():
    messages = [{"role": "system", "content": "규칙\n\n[도구 결과]\n- 약속을 저장했습니다"}, {"role": "user", "content": "x"}]
    out = with_header(messages, "[도구 결과]\n새 머리말")
    assert out[0]["content"] == "규칙\n\n[도구 결과]\n새 머리말\n- 약속을 저장했습니다"
    assert out[1] == messages[1]
