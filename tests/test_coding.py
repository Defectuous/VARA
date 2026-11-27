"""The Python coding agent (vara/coding/): its projects folder, its tools, and chat with agent "python"."""

import asyncio
import shutil
import sys
import threading

import pytest

from tests.test_agent import call, pending, wait_for
from tests.test_chat_tools import ask, tools_client
from vara.actions.base import ActionContext, ActionFailed
from vara.coding import actions, runner
from vara.coding.workspace import ProjectError, Projects
from vara.config import Settings, get_settings
from vara.providers.base import ToolCall
from vara.providers.fake import FakeProvider

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git isn't installed")
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv isn't installed")


def go(coro):
    return asyncio.run(coro)


@pytest.fixture
def ctx() -> ActionContext:
    return ActionContext(db=None, settings=get_settings())


@pytest.fixture
def project(ctx) -> str:
    go(actions.create_project(ctx, actions.NewProject(name="demo", description="A demo")))
    return "demo"


def do(action, ctx, **params):
    model = next(a for a in actions.CODING_ACTIONS if a.run is action).params
    return go(action(ctx, model(**params)))


# ---- the projects folder ----


def test_a_new_project_has_its_files_and_shows_in_the_list(ctx, project) -> None:
    folder = ctx.settings.projects_dir / project

    assert {"pyproject.toml", "README.md", ".gitignore"} <= {p.name for p in folder.iterdir()}
    assert 'requires-python = ">=3.12"' in (folder / "pyproject.toml").read_text()
    listed = do(actions.list_projects, ctx)
    assert "demo: A demo" in listed.text
    with pytest.raises(ActionFailed, match="already exists"):
        do(actions.create_project, ctx, name="demo", description="again")


@pytest.mark.parametrize("name", ["Demo", "../x", "a b", "", "-x", "x" * 41])
def test_project_names_are_checked(ctx, name) -> None:
    with pytest.raises(ProjectError):
        Projects(ctx.settings.projects_dir).project(name)


@pytest.mark.parametrize(
    "path",
    ["../other/file.py", "..", "/etc/passwd", "C:/Windows/win.ini", ".git/config", ".venv/x"],
)
def test_paths_cannot_leave_the_project_or_touch_tool_folders(ctx, project, path) -> None:
    with pytest.raises(ActionFailed):
        do(actions.write_file, ctx, project=project, path=path, content="x")
    with pytest.raises(ActionFailed):
        do(actions.read_file, ctx, project=project, path=path)


def test_write_read_edit_and_search(ctx, project) -> None:
    do(
        actions.write_file,
        ctx,
        project=project,
        path="src/app.py",
        content="def f():\n    return 1\n",
    )

    read = do(actions.read_file, ctx, project=project, path="src/app.py")
    assert "    2      return 1" in read.text

    do(
        actions.edit_file,
        ctx,
        project=project,
        path="src/app.py",
        old_text="return 1",
        new_text="return 2",
    )
    assert "return 2" in do(actions.read_file, ctx, project=project, path="src/app.py").text
    assert (
        "src/app.py:2" in do(actions.search_code, ctx, project=project, pattern=r"return \d").text
    )

    ranged = do(
        actions.read_file, ctx, project=project, path="src/app.py", start_line=2, end_line=2
    )
    assert "def f" not in ranged.text and "return 2" in ranged.text


def test_an_edit_needs_text_that_appears_exactly_once(ctx, project) -> None:
    do(actions.write_file, ctx, project=project, path="a.py", content="x = 1\nx = 1\n")

    with pytest.raises(ActionFailed, match="appears 2 times"):
        do(actions.edit_file, ctx, project=project, path="a.py", old_text="x = 1", new_text="y")
    with pytest.raises(ActionFailed, match="isn't in"):
        do(actions.edit_file, ctx, project=project, path="a.py", old_text="nope", new_text="y")


@needs_git
def test_every_change_is_saved_and_the_latest_can_be_undone(ctx, project) -> None:
    do(actions.write_file, ctx, project=project, path="a.py", content="one\n")
    do(actions.write_file, ctx, project=project, path="a.py", content="two\n")

    undone = do(actions.undo_last_change, ctx, project=project)
    files = do(actions.list_files, ctx, project=project)

    assert "Rewrite a.py" in undone.text
    assert (ctx.settings.projects_dir / project / "a.py").read_text() == "one\n"
    assert "Revert" in files.text and "Create a.py" in files.text


def test_listing_skips_tool_folders(ctx, project) -> None:
    folder = ctx.settings.projects_dir / project
    (folder / ".venv" / "lib").mkdir(parents=True)
    (folder / ".venv" / "lib" / "x.py").write_text("")
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "a.pyc").write_bytes(b"\0")

    listed = do(actions.list_files, ctx, project=project).text

    assert "pyproject.toml" in listed
    assert ".venv" not in listed and "__pycache__" not in listed


@pytest.mark.parametrize("bad", ["-e .", "--index-url=http://evil", "git+https://x/y", "./local"])
def test_only_plain_package_names_can_be_installed(bad) -> None:
    with pytest.raises(ValueError):
        actions.PackageParams(project="demo", packages=[bad])


def test_package_names_with_versions_and_extras_are_fine() -> None:
    params = actions.PackageParams(project="demo", packages=["pandas>=2.2", "httpx[http2]", "rich"])
    assert params.packages == ["pandas>=2.2", "httpx[http2]", "rich"]


# ---- running code ----


def test_programs_get_no_secrets_from_the_server(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("VIRTUAL_ENV", "/vara/.venv")
    monkeypatch.setenv("HARMLESS", "yes")

    env = runner.program_environment()

    assert "OPENAI_API_KEY" not in env and "GITHUB_TOKEN" not in env and "VIRTUAL_ENV" not in env
    assert env["HARMLESS"] == "yes" and env["PYTHONUNBUFFERED"] == "1"


def test_a_run_that_takes_too_long_is_stopped(tmp_path) -> None:
    outcome = go(
        runner.run(
            [sys.executable, "-c", "print('started', flush=True); import time; time.sleep(30)"],
            tmp_path,
            1.0,
        )
    )

    assert outcome.timed_out and outcome.seconds < 10
    assert "started" in outcome.output


def test_long_output_keeps_its_start_and_end(tmp_path) -> None:
    script = "print('FIRST'); [print('x' * 100) for _ in range(5000)]; print('LAST')"
    outcome = go(runner.run([sys.executable, "-c", script], tmp_path, 30.0))

    assert outcome.exit_code == 0
    assert outcome.output.startswith("FIRST") and outcome.output.rstrip().endswith("LAST")
    assert "bytes of output left out" in outcome.output
    assert len(outcome.output) < runner.HEAD_CHARS + runner.TAIL_CHARS + 200


@needs_uv
def test_run_python_and_run_tests_in_the_project(ctx, project) -> None:
    do(
        actions.write_file,
        ctx,
        project=project,
        path="add.py",
        content=(
            "import sys\n\ndef add(a: int, b: int) -> int:\n    return a + b\n\n"
            'if __name__ == "__main__":\n    print(add(int(sys.argv[1]), int(sys.argv[2])))\n'
        ),
    )
    do(
        actions.write_file,
        ctx,
        project=project,
        path="tests/test_add.py",
        content=(
            "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
            "from add import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
        ),
    )

    ran = do(actions.run_python, ctx, project=project, file="add.py", args=["2", "40"])
    tested = do(actions.run_tests, ctx, project=project)

    assert "exited with code 0" in ran.text and "42" in ran.text
    assert "exited with code 0" in tested.text and "1 passed" in tested.text


def test_run_python_only_runs_python_files_in_the_project(ctx, project) -> None:
    with pytest.raises(ActionFailed, match="no Python file"):
        do(actions.run_python, ctx, project=project, file="README.md")
    with pytest.raises(ActionFailed, match="no Python file"):
        do(actions.run_python, ctx, project=project, file="missing.py")


# ---- the agent in chat ----


def test_chat_never_gets_the_coding_tools() -> None:
    model = FakeProvider("local", script=["Hi."])
    with tools_client(model) as client:
        ask(client, "hello")

    assert not set(model.tools_offered[0]) & actions.CODING_TOOLS


def test_the_python_agent_gets_its_tools_and_instructions_and_edits_without_asking() -> None:
    model = FakeProvider(
        "local",
        script=[
            [ToolCall("call-1", "create_project", {"name": "hello", "description": "Says hello"})],
            [call("write_file", 2, project="hello", path="hello.py", content="print('hi')\n")],
            "Created hello.py.",
        ],
    )
    with tools_client(model) as client:
        reply = ask(client, "make me a hello world script", agent="python")

    offered = set(model.tools_offered[0])
    assert actions.CODING_TOOLS <= offered
    assert "watch_price" not in offered  # only its own tools and a few helpers
    assert "Python coding agent" in model.systems[0]
    assert reply["text"].strip() == "Created hello.py."
    assert [a["status"] for a in reply["actions"]] == ["succeeded", "succeeded"]
    assert (Settings().projects_dir / "hello" / "hello.py").read_text() == "print('hi')\n"


def test_running_code_waits_for_approval_and_a_refusal_runs_nothing() -> None:
    model = FakeProvider(
        "local",
        script=[
            [ToolCall("call-1", "create_project", {"name": "p", "description": "x"})],
            [call("run_python", 2, project="p", file="main.py")],
            "Okay, I won't run it.",
        ],
    )
    with tools_client(model) as client:
        result: dict = {}
        thread = threading.Thread(
            target=lambda: result.update(ask(client, "run it", agent="python"))
        )
        thread.start()
        [approval] = wait_for(lambda: pending(client))
        client.post(f"/v1/agent/approvals/{approval['id']}", json={"decision": "reject"})
        thread.join(5)

    assert approval["action"] == "run_python" and approval["risk"] == "run_code"
    assert result["actions"][1]["status"] == "rejected"


def test_reading_a_file_again_after_an_edit_sees_the_edit() -> None:
    """Coding tools are never answered from an earlier identical call."""
    read = {"project": "p", "path": "a.py"}
    model = FakeProvider(
        "local",
        script=[
            [ToolCall("call-1", "create_project", {"name": "p", "description": "x"})],
            [call("write_file", 2, project="p", path="a.py", content="one\n")],
            [call("read_file", 3, **read)],
            [call("write_file", 4, project="p", path="a.py", content="two\n")],
            [call("read_file", 5, **read)],
            "Done.",
        ],
    )
    with tools_client(model) as client:
        reply = ask(client, "go", agent="python")

    assert [a["status"] for a in reply["actions"]] == ["succeeded"] * 5
    assert "two" in model.calls[-1][-1].content


def test_general_agent_runs_dont_get_the_coding_tools() -> None:
    from tests.test_agent import make_client, run

    model = FakeProvider("local", script=["Hi."])
    with make_client(model) as client:
        run(client)

    assert "current_time" in model.tools_offered[0]
    assert not set(model.tools_offered[0]) & actions.CODING_TOOLS
