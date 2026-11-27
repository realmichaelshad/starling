from app.storage import recent_strategy_instructions, save_strategy_message


def test_recent_user_chat_instructions_are_retrieved_locally_in_order():
    save_strategy_message("one", "user", "Keep my replies direct.")
    save_strategy_message("one", "assistant", "Got it.")
    save_strategy_message("two", "user", "Do not make the tone corporate.")

    assert recent_strategy_instructions() == [
        "Keep my replies direct.",
        "Do not make the tone corporate.",
    ]
