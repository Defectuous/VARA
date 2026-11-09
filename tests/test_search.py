import asyncio
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from vara.config import Settings
from vara.main import create_app
from vara.providers.base import Capabilities, Completed, Message, TextDelta
from vara.router import ModelRouter, PrivacyMode
from vara.search import planner
from vara.search.client import SearchFailed, SearxngClient, WebResult, WebSearch, search_context
from vara.search.service import SearxngService

SEARXNG_REPLY = {
    "query": "current us president",
    "results": [
        {"title": "Only Brave", "url": "https://a.example", "content": "a", "engines": ["brave"]},
        {
            "title": "President - The White House",
            "url": "https://whitehouse.example",
            "content": "  The   President  of the United States. ",
            "engines": ["bing", "brave", "google"],
            "publishedDate": "2026-10-01T00:00:00",
        },
        {"title": "", "url": "https://no-title.example", "engines": ["bing"]},
    ],
    "answers": [{"answer": "Donald Trump"}],
    "infoboxes": [{"infobox": "President of the United States", "content": "Head of state."}],
    "unresponsive_engines": [["google", "access denied"], ["duckduckgo", "CAPTCHA"]],
}


def searxng(handler) -> SearxngClient:
    return SearxngClient(
        "http://searxng",
        httpx.AsyncClient(base_url="http://searxng", transport=httpx.MockTransport(handler)),
    )


def test_results_are_parsed_and_agreed_ones_come_first() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=SEARXNG_REPLY)

    result = asyncio.run(searxng(handler).search("current us president", category="news"))

    assert seen == {"q": "current us president", "format": "json", "categories": "news"}
    assert [r.title for r in result.results] == ["President - The White House", "Only Brave"]
    top = result.results[0]
    assert top.engines == ["bing", "brave", "google"]
    assert top.snippet == "The President of the United States."
    assert result.answers == ["Donald Trump", "President of the United States: Head of state."]
    assert result.unresponsive == ["google", "duckduckgo"]


def test_an_unreachable_searxng_is_a_search_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(SearchFailed):
        asyncio.run(searxng(handler).search("anything"))


def test_search_context_numbers_results_and_names_agreeing_engines() -> None:
    search = WebSearch(
        "q",
        [
            WebResult(
                "Title", "https://x.example", "Snippet.", ["bing", "brave"], "2026-10-01T00:00"
            )
        ],
        answers=["42"],
    )

    context = search_context(search, "Thursday")

    assert 'Web search results for "q" (retrieved Thursday)' in context
    assert "- Direct answer: 42" in context
    assert "[1] Title (https://x.example; found by bing, brave, published 2026-10-01)" in context


@pytest.mark.parametrize(
    ("text", "needed"),
    [
        ("Who is the president of the United States?", True),
        ("what's the weather tomorrow", True),
        ("latest news about the Mars mission", True),
        ("search for the best pizza in Boston", True),
        ("can you access public data related to the S&P 500 and NASDAQ?", True),
        ("is the stock market bullish or bearish?", True),
        ("how is the Dow doing", True),
        ("what's bitcoin at", True),
        ("write me a short poem about rain", False),
        ("what is 12 times 7", False),
    ],
)
def test_the_word_check_only_fires_for_current_information(text: str, needed: bool) -> None:
    assert planner.might_need_search(text) is needed


@pytest.mark.parametrize(
    ("reply", "query"),
    [
        ("SEARCH: current US president", "current US president"),
        ('search: "weather Boston tomorrow"', "weather Boston tomorrow"),
        ("NO_SEARCH", None),
        ("I think you should search.", None),
    ],
)
def test_the_models_decision_is_parsed(reply: str, query: str | None) -> None:
    assert planner.parse_decision(reply) == query


def test_explicit_searches_skip_the_model() -> None:
    def ask_model(messages, system):
        raise AssertionError("the model should not be asked")

    query = asyncio.run(
        planner.plan_search("Look up the population of Canada?", "today", ask_model)
    )

    assert query == "the population of Canada"


class PlanningProvider:
    """Answers the search planner with SEARCH: ..., and records what each request was given."""

    name = "local"
    default_model = "local-model"
    capabilities = Capabilities(local=True, context_length=8192)

    def __init__(self, decision: str = "SEARCH: current US president") -> None:
        self.decision = decision
        self.systems: list[str | None] = []

    def request_body(
        self, messages: Sequence[Message], *, model=None, system=None, think=None
    ) -> dict:
        return {"model": model or self.default_model, "system": system, "messages": []}

    async def stream(self, messages: Sequence[Message], *, model=None, system=None, think=None):
        self.systems.append(system)
        planning = system is not None and system.startswith("You decide whether")
        for word in (self.decision if planning else "It is Donald Trump.").split(" "):
            yield TextDelta(word + " ")
        yield Completed(model=model or self.default_model, stop_reason="end_turn")


class FakeWeb:
    def __init__(self, fail: bool = False) -> None:
        self.queries: list[tuple[str, str]] = []
        self.fail = fail

    async def search(self, query: str, *, category: str = "general", limit: int = 6) -> WebSearch:
        self.queries.append((query, category))
        if self.fail:
            raise SearchFailed("Web search is unavailable")
        return WebSearch(
            query,
            [
                WebResult(
                    "The White House", "https://wh.example", "President Trump.", ["bing", "brave"]
                )
            ],
        )


def web_client(provider, web, **settings) -> TestClient:
    router = ModelRouter([provider], PrivacyMode.LOCAL_ONLY)
    settings.setdefault("web_search", "auto")
    return TestClient(create_app(Settings(**settings), router=router, web_search=web))


ASK = {"messages": [{"role": "user", "content": "Who is the president of the United States?"}]}


def test_a_current_events_question_is_searched_and_answered_from_the_results() -> None:
    provider, web = PlanningProvider(), FakeWeb()

    response = web_client(provider, web).post("/v1/chat", json=ASK)

    assert response.status_code == 200
    assert web.queries == [("current US president", "general")]
    answer_system = provider.systems[-1]
    assert 'Web search results for "current US president"' in answer_system
    assert "[1] The White House (https://wh.example; found by bing, brave)" in answer_system
    assert response.json()["web_search"]["results"] == [
        {"title": "The White House", "url": "https://wh.example", "engines": ["bing", "brave"]}
    ]


def test_no_search_when_the_model_says_it_is_not_needed() -> None:
    provider, web = PlanningProvider(decision="NO_SEARCH"), FakeWeb()

    response = web_client(provider, web).post("/v1/chat", json=ASK)

    assert web.queries == []
    assert response.json()["web_search"] is None


def test_ordinary_messages_do_not_even_ask_the_planner() -> None:
    provider, web = PlanningProvider(), FakeWeb()

    web_client(provider, web).post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "write a haiku"}]}
    )

    assert len(provider.systems) == 1  # just the answer
    assert web.queries == []


def test_a_follow_up_is_planned_with_the_earlier_question() -> None:
    seen: list[str] = []

    async def ask_model(messages, system):
        seen.append(messages[-1].content)
        yield TextDelta("SEARCH: NASDAQ composite today")
        yield Completed(model="m", stop_reason="end_turn")

    history = [
        Message("user", "Is the stock market up today?"),
        Message("assistant", "According to Yahoo Finance, the S&P 500 closed up 0.3%."),
    ]
    query = asyncio.run(planner.plan_search("and the other one?", "today", ask_model, history))

    assert query == "NASDAQ composite today"
    assert "Is the stock market up today?" in seen[0]
    assert seen[0].endswith("Latest message: and the other one?")


def test_the_model_is_told_it_can_search_even_when_no_search_ran() -> None:
    provider, web = PlanningProvider(), FakeWeb()

    web_client(provider, web).post(
        "/v1/chat", json={"messages": [{"role": "user", "content": "write a haiku"}]}
    )

    assert "Never say you can't access the internet" in provider.systems[-1]


def test_a_failed_search_still_answers() -> None:
    provider, web = PlanningProvider(), FakeWeb(fail=True)

    response = web_client(provider, web).post("/v1/chat", json=ASK)

    assert response.status_code == 200
    assert response.json()["web_search"] is None
    assert "Web search results" not in (provider.systems[-1] or "")


def test_requests_can_opt_out_and_settings_can_turn_search_off() -> None:
    provider, web = PlanningProvider(), FakeWeb()
    web_client(provider, web).post("/v1/chat", json={**ASK, "web_search": False})
    off = web_client(PlanningProvider(), None, web_search="off")

    assert web.queries == []
    assert off.app.state.web is None
    assert off.get("/v1/web/search", params={"q": "x"}).status_code == 503


# The daily update: a local git repository stands in for GitHub.


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    git("add", ".", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def module(tmp_path: Path) -> tuple[SearxngService, Path]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "-q", "-b", "master", cwd=upstream)
    commit(upstream, "v1", {"requirements.txt": "flask\n", "searx/webapp.py": "# v1\n"})
    service = SearxngService(tmp_path / "searxng")
    git("clone", "-q", str(upstream), str(service.src), cwd=tmp_path)
    service.python.parent.mkdir(parents=True)
    service.python.write_text("")  # stands in for the module's Python environment
    return service, upstream


def test_update_check_when_already_current(module) -> None:
    service, _ = module

    status = service.check_for_update(smoke_test=lambda: True)

    assert status.result == "up to date"
    assert not service.update_due()
    assert json.loads(service.status_path.read_text())["result"] == "up to date"


def test_a_new_upstream_commit_is_applied_when_search_still_works(module) -> None:
    service, upstream = module
    new = commit(upstream, "v2", {"searx/webapp.py": "# v2\n"})

    status = service.check_for_update(smoke_test=lambda: True)

    assert status.result == "updated"
    assert service.current_commit() == new
    assert status.history[-1]["result"] == "updated"


def test_a_broken_update_is_rolled_back(module) -> None:
    service, upstream = module
    before = service.current_commit()
    commit(upstream, "v2 broken", {"searx/webapp.py": "# broken\n"})

    status = service.check_for_update(smoke_test=lambda: False)

    assert status.result.startswith("rolled back")
    assert service.current_commit() == before
    assert (service.src / "searx" / "webapp.py").read_text() == "# v1\n"


def test_status_endpoint_reports_the_module(module, tmp_path) -> None:
    client = TestClient(
        create_app(Settings(data_dir=tmp_path / "data", web_search="auto"), web_search=FakeWeb())
    )

    status = client.get("/v1/web/status").json()

    assert status["enabled"] is True
    assert status["installed"] is False
    assert status["update"]["result"] == "never checked"
