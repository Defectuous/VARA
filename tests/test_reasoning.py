import pytest

from vara.reasoning import should_reason


@pytest.mark.parametrize(
    "text",
    [
        "hi",
        "Thanks!",
        "good morning",
        "what time is it?",
        "What's the capital of France?",
        "Tell me a joke",
        "I think it's going to rain",
    ],
)
def test_small_talk_and_quick_facts_answer_straight_away(text: str) -> None:
    assert should_reason(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Why is the sky blue?",
        "Explain how DNS works",
        "Compare Postgres and SQLite for a home server",
        "What is 17 * 23?",
        "Help me debug this: ```x = [1, 2\nprint(x)```",
        "Should I use a VPN on my home network?",
        "Plan a three-day trip to Rome",
        "word " * 45,
    ],
)
def test_hard_or_long_questions_reason(text: str) -> None:
    assert should_reason(text) is True


def test_spoken_replies_reason_only_for_clearly_hard_questions() -> None:
    assert should_reason("Why is the sky blue?", spoken=True) is False
    assert should_reason("What is 17 times 23 plus 4 * 5?", spoken=True) is True
    assert should_reason("Plan my week step by step", spoken=True) is True


def test_mode_on_or_off_overrides_the_choice() -> None:
    assert should_reason("hi", mode="on") is True
    assert should_reason("Why is the sky blue?", mode="off") is False
