from app.agent.history import append_history_turn, read_history, saved_context_question


def test_saved_overview_context_freezes_relative_dates():
    payload = {
        "player_profile": {"player": {"player_name": "Jalen Johnson"}},
        "semantic_evidence": {
            "metrics": [1],
            "scope": {
                "start": "2025-09-13",
                "end": "2026-09-12",
                "phases": ["Regular Season", "Playoffs"],
            },
        },
    }
    context = saved_context_question("performance past 12 months", payload)
    assert (
        context
        == "Jalen Johnson performance from 2025-09-13 through 2026-09-12 (Regular Season + Playoffs)"
    )
    assert saved_context_question("Original question", {}) == "Original question"


def test_history_search_paginates_beyond_tab_and_cache_limit(tmp_path):
    path = tmp_path / "history.jsonl"
    for index in range(32):
        append_history_turn(
            path,
            conversation_id=f"chat-{index}",
            request_id=f"r-{index}",
            question=f"Player {index} overview",
            provider="test",
            model="fixture",
            payload={"answer": "saved"},
        )
    first = read_history(path, limit=25)
    assert len(first["conversations"]) == 25
    assert first["next_offset"] == 25
    second = read_history(path, offset=25, limit=25)
    assert len(second["conversations"]) == 7
    assert second["next_offset"] is None
    assert any(
        item["conversation_id"] == "chat-0"
        for item in read_history(path, query="player 0")["conversations"]
    )
    assert (
        read_history(path, conversation_id="chat-0")["conversations"][0]["title"]
        == "Player 0 overview"
    )
    assert read_history(path, conversation_id="absent")["conversations"] == []


def test_history_keeps_opening_question_and_all_followups(tmp_path):
    path = tmp_path / "history.jsonl"
    for index in range(25):
        append_history_turn(
            path,
            conversation_id="chat",
            request_id=f"r-{index}",
            question="Opening question" if index == 0 else f"Follow-up {index}",
            provider="test",
            model="fixture",
            payload={"answer": str(index)},
        )
    chat = read_history(path, query="follow-up 24")["conversations"][0]
    assert chat["title"] == "Opening question"
    assert len(chat["turns"]) == 25
    assert chat["turns"][0]["payload"]["answer"] == "0"
