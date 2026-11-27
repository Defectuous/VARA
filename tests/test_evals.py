import json
from datetime import UTC, datetime

import httpx

from vara import evals

NOW = datetime(2026, 11, 12, 1, 5, tzinfo=UTC)


def test_case_file_loads_with_unique_ids_and_known_checks():
    cases = evals.load_cases()
    assert len(cases) >= 30
    assert len({c.id for c in cases}) == len(cases)
    assert all(c.checks for c in cases), "every case needs at least one check"


def test_unknown_case_field_is_rejected(tmp_path):
    path = tmp_path / "cases.toml"
    path.write_text('[[case]]\nid = "x"\ncategory = "c"\nquestion = "q"\nexpect_al = ["a"]\n')
    try:
        evals.load_cases(path)
    except ValueError as exc:
        assert "expect_al" in str(exc)
    else:
        raise AssertionError("a misspelled check must not be silently ignored")


def test_text_checks():
    assert evals.grade({"expect_all": ["Brewers", "five"]}, "The brewers won in Five games") == []
    assert evals.grade({"expect_all": ["Rays"]}, "Brewers") == ["missing 'Rays'"]
    assert evals.grade({"expect_any": ["a", "b"]}, "xyz") == ["none of ['a', 'b']"]
    assert evals.grade({"reject_any": ["syntax error"]}, "A Syntax Error on line 3") == [
        "contains 'syntax error'"
    ]
    assert evals.grade({"expect_regex": [r"\beast\b"]}, "You face East.") == []
    assert evals.grade({"reject_regex": [r"facing north"]}, "facing north") != []


def test_number_check_ignores_commas_and_needs_the_exact_number():
    assert evals.grade({"expect_number": 9368}, "The sum is 9,368.") == []
    assert evals.grade({"expect_number": 400}, "There are 4000 of them") != []
    assert evals.grade({"expect_number": -40}, "It is −40 °F") == []


def test_poem_checks():
    poem = (
        "Vast minds awaken in the quiet night\n"
        "Always listening, ready with an answer\n"
        "Reasoning softly through the tangled questions\n"
        "Answers arrive like dawn upon the hills\n"
    )
    checks = {"first_letters": "VARA", "line_word_counts": [7, 7, 7, 7]}
    assert evals.grade(checks, poem) == ["line word counts [7, 6, 6, 7], wanted [7, 7, 7, 7]"]
    assert evals.grade({"first_letters": "VARA"}, poem) == []


def test_length_markdown_search_and_reasoning_checks():
    assert evals.grade({"max_words": 3}, "one two three four") == ["4 words (max 3)"]
    assert evals.grade({"no_markdown": True}, "**Everest** is tallest") == ["uses Markdown"]
    assert evals.grade({"no_markdown": True}, "Mount Everest, at 8,849 metres.") == []
    assert evals.grade({"expect_search": True}, "x", searched=False) == ["didn't search the web"]
    assert evals.grade({"expect_reasoning": True}, "x", reasoning=False) == [
        "reasoning was off, wanted on"
    ]


def test_date_placeholders_use_the_day_of_the_run():
    checks = {"expect_all": ["{month_name}"], "expect_regex": [r"\b{day}(st|nd|rd|th)?\b"]}
    assert evals.grade(checks, "Today is Thursday, November 12th, 2026.", now=NOW) == []
    assert evals.grade(checks, "Today is November 11.", now=NOW) != []


def _fake_backend(replies: dict[str, dict], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "DELETE":
            seen.append(("delete", request.url.path))
            return httpx.Response(204)
        body = json.loads(request.content)
        seen.append(("chat", body))
        reply = replies[body["messages"][0]["content"]]
        return httpx.Response(200, json={"conversation_id": 7, **reply})

    return httpx.Client(base_url="http://vara", transport=httpx.MockTransport(handler))


def test_run_grades_saves_and_compares_with_the_previous_run(tmp_path):
    cases = [
        evals.Case("sum", "math", "17*23?", {}, {"expect_number": 391}),
        evals.Case("ws", "current", "World Series?", {"web_search": True}, {"expect_search": True}),
    ]
    seen: list = []
    replies = {
        "17*23?": {"text": "391", "reasoning": True},
        "World Series?": {"text": "Brewers", "web_search": {"unresponsive_engines": ["google"]}},
    }
    runner = evals.EvalRunner("http://vara", tmp_path, client=_fake_backend(replies, seen))
    runner.check_backend()

    first = [runner.run_case(c, "qwen3:14b") for c in cases]
    assert [r.passed for r in first] == [True, True]
    assert first[1].unresponsive_engines == ["google"]
    chat = seen[0][1]
    assert chat["model"] == "qwen3:14b" and chat["use_memory"] is False
    assert chat["web_search"] is False and seen[2][1]["web_search"] is True
    assert ("delete", "/v1/conversations/7") in seen, "test conversations are removed"
    assert runner.previous("qwen3:14b", {"sum", "ws"}) is None
    summary = runner.save("qwen3:14b", "abc123", first)
    assert summary["score"] == 100.0 and summary["by_category"] == {"current": "1/1", "math": "1/1"}
    assert (tmp_path / "runs" / summary["run_file"]).exists()

    replies["17*23?"] = {"text": "about 400", "reasoning": False}
    second = [runner.run_case(c, "qwen3:14b") for c in cases]
    previous = runner.previous("qwen3:14b", {"sum", "ws"})
    change = evals.compare(previous, runner.save("qwen3:14b", "def456", second))
    assert change == {"delta": -50.0, "regressed": ["sum"], "fixed": [], "model_changed": True}
    # A run of another model, or of a different set of cases, isn't compared.
    assert runner.previous("gemma4:12b", {"sum", "ws"}) is None
    assert runner.previous("qwen3:14b", {"sum"}) is None


def test_failed_request_is_a_failed_case(tmp_path):
    def handler(request):
        return httpx.Response(403, json={"detail": "blocked"})

    client = httpx.Client(base_url="http://vara", transport=httpx.MockTransport(handler))
    runner = evals.EvalRunner("http://vara", tmp_path, client=client)
    result = runner.run_case(evals.Case("x", "c", "q", {}, {"expect_any": ["a"]}), "m")
    assert not result.passed and result.failures == ["request failed"] and "blocked" in result.error


def test_curly_quotes_match_straight_ones():
    assert evals.grade({"expect_any": ["don't have"]}, "I don’t have access to that.") == []
