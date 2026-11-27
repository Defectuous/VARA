"""The Python coding agent's tools. Reading is free; edits inside a project run without asking (each is a
git commit, so it can be undone); running code, tests, and installs are approved each time."""

import re
import shutil
from fnmatch import fnmatch

from pydantic import Field, field_validator

from vara.actions.base import (
    Action,
    ActionContext,
    ActionFailed,
    ActionResult,
    NoParams,
    Params,
    Risk,
)
from vara.coding import runner
from vara.coding.workspace import ProjectError, Projects

MAX_MATCHES = 200
_PACKAGE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-]*(\[[A-Za-z0-9._,\-]+\])?([<>=!~]=?[A-Za-z0-9.*+]+,?)*$"
)

CODING_INSTRUCTIONS = """\
You are V.A.R.A.'s Python coding agent. You write, fix, test, explain, and review Python, and use it to \
analyse data. You work only in V.A.R.A.'s projects folder ({root}): each project is a folder there with \
its own git history and uv environment (Python 3.12 or newer). You can't see or change anything else on \
this computer.

How to work:
- Start from what's there: list_projects, then list_files, read_file, and search_code before changing \
anything. Create a project with create_project only when the user wants something new.
- Change files with edit_file (an exact, unique piece of text replaced by new text) or write_file for new \
files and full rewrites. Keep changes focused on what was asked. Edits are saved without asking, and \
undo_last_change takes the latest one back.
- Write clear, idiomatic, typed Python: small functions, standard library first, pathlib, f-strings, \
dataclasses where they fit, and `if __name__ == "__main__":` for scripts. Put pytest tests in tests/ for \
anything with real logic.
- Check your work: check_code (ruff) and format_code, then run_tests or run_python. Running code, tests, \
or installing packages (add_packages) asks the user each time, so run when you expect it to work, not \
after every small edit. If a run fails, read the error, fix the cause, and run again.
- Programs get no keyboard input (stdin is closed): take arguments or read files instead. Each run is \
stopped after {timeout:g} seconds.
- For data analysis, the data file must be inside the project; if it isn't, tell the user to put it in the \
project's folder (give the full path). Write a script that prints the results, and save charts as PNG \
files in the project.
- To explain or review code, read it and answer; don't change files unless asked.
- If the user declines a run or an install, don't try to get the same effect another way.
- When you're done, say what you changed (files), how to run it, and what the tests showed. Only say tests \
pass or code works if a tool result in this conversation showed it."""


def _projects(ctx: ActionContext) -> Projects:
    return Projects(ctx.settings.projects_dir)


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise ActionFailed("uv isn't installed on this computer, so code can't be run")
    return uv


def _fail(exc: ProjectError) -> ActionFailed:
    return ActionFailed(str(exc))


def _saved_note(saved) -> str:
    return "" if saved.committed else f" ({saved.note})"


# ---- parameters ----


class ProjectParams(Params):
    project: str = Field(description="The project's name (its folder under the projects folder).")


class NewProject(Params):
    name: str = Field(description="Lowercase letters, digits, - and _, e.g. 'csv-report'.")
    description: str = Field(max_length=300, description="One sentence: what it's for.")


class PathParams(ProjectParams):
    path: str = Field(default=".", description="A path relative to the project folder.")


class ReadParams(ProjectParams):
    path: str = Field(description="The file, relative to the project folder.")
    start_line: int | None = Field(default=None, ge=1, description="First line to show (1-based).")
    end_line: int | None = Field(default=None, ge=1, description="Last line to show.")


class SearchParams(ProjectParams):
    pattern: str = Field(min_length=1, max_length=300, description="A Python regular expression.")
    glob: str = Field(default="*.py", description="Which files, e.g. '*.py' or '*'.")
    ignore_case: bool = False


class WriteParams(ProjectParams):
    path: str = Field(description="The file, relative to the project folder.")
    content: str = Field(description="The whole new content of the file.")


class EditParams(ProjectParams):
    path: str = Field(description="The file, relative to the project folder.")
    old_text: str = Field(
        min_length=1, description="Exact text to replace; must appear exactly once in the file."
    )
    new_text: str = Field(description="What replaces it.")


class FileParams(ProjectParams):
    path: str = Field(description="The file, relative to the project folder.")


class RunParams(ProjectParams):
    file: str = Field(description="The .py file to run, relative to the project folder.")
    args: list[str] = Field(default_factory=list, max_length=50, description="Its arguments.")


class TestParams(ProjectParams):
    paths: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Test files or folders; empty runs them all.",
    )


class PackageParams(ProjectParams):
    packages: list[str] = Field(
        min_length=1, max_length=20, description="PyPI package names, optionally with versions."
    )

    @field_validator("packages")
    @classmethod
    def _plain_names(cls, packages: list[str]) -> list[str]:
        for package in packages:
            if not _PACKAGE.match(package.replace(" ", "")):
                raise ValueError(f'"{package}" isn\'t a plain package name (like "pandas>=2")')
        return [p.replace(" ", "") for p in packages]


# ---- reading ----


async def list_projects(ctx: ActionContext, _params) -> ActionResult:
    projects = _projects(ctx).all()
    root = ctx.settings.projects_dir.resolve()
    if not projects:
        return ActionResult(f"There are no projects yet (folder: {root}).", {"projects": []})
    lines = [f"Projects in {root}:"]
    lines += [
        f"- {p['name']}: {p['description'] or '(no description)'} ({p['files']} files)"
        for p in projects
    ]
    return ActionResult("\n".join(lines), {"projects": projects})


async def list_files(ctx: ActionContext, params: PathParams) -> ActionResult:
    try:
        files = _projects(ctx).files(params.project, params.path)
        history = await _projects(ctx).history(params.project, 5)
    except ProjectError as exc:
        raise _fail(exc) from exc
    if not files:
        return ActionResult(f"No files in {params.project}/{params.path}.")
    lines = [f"{path} ({size:,} bytes)" for path, size in files]
    if history:
        lines += ["", "Recent changes:", *history]
    return ActionResult("\n".join(lines), {"files": len(files)})


async def read_file(ctx: ActionContext, params: ReadParams) -> ActionResult:
    projects = _projects(ctx)
    try:
        if params.start_line or params.end_line:
            lines = projects.read_lines(params.project, params.path)
        else:
            lines = projects.read(params.project, params.path).splitlines()
    except ProjectError as exc:
        raise _fail(exc) from exc
    first = params.start_line or 1
    last = min(params.end_line or len(lines), len(lines))
    if not lines:
        return ActionResult(f"{params.path} is empty.")
    shown = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(first, last + 1))
    return ActionResult(f"{params.path} (lines {first}-{last} of {len(lines)}):\n{shown}")


async def search_code(ctx: ActionContext, params: SearchParams) -> ActionResult:
    try:
        pattern = re.compile(params.pattern, re.IGNORECASE if params.ignore_case else 0)
    except re.error as exc:
        raise ActionFailed(f"That isn't a valid regular expression: {exc}") from exc
    projects = _projects(ctx)
    matches: list[str] = []
    try:
        for path, size in projects.files(params.project):
            if size > 1_000_000 or not fnmatch(path.rsplit("/", 1)[-1], params.glob):
                continue
            try:
                lines = projects.read_lines(params.project, path)
            except ProjectError:
                continue  # binary
            for number, line in enumerate(lines, 1):
                if pattern.search(line):
                    matches.append(f"{path}:{number}: {line.strip()[:200]}")
                    if len(matches) >= MAX_MATCHES:
                        break
            if len(matches) >= MAX_MATCHES:
                break
    except ProjectError as exc:
        raise _fail(exc) from exc
    if not matches:
        return ActionResult(f"No matches for {params.pattern!r} in {params.glob} files.")
    more = f"\n(stopped at {MAX_MATCHES} matches)" if len(matches) >= MAX_MATCHES else ""
    return ActionResult("\n".join(matches) + more, {"matches": len(matches)})


# ---- workspace edits ----


async def create_project(ctx: ActionContext, params: NewProject) -> ActionResult:
    try:
        path = await _projects(ctx).create(params.name, params.description)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ActionResult(
        f"Created project {params.name} at {path.resolve()} with pyproject.toml, README.md, and "
        ".gitignore.",
        {"project": params.name},
    )


async def write_file(ctx: ActionContext, params: WriteParams) -> ActionResult:
    projects = _projects(ctx)
    try:
        created = projects.write(params.project, params.path, params.content)
        verb = "Create" if created else "Rewrite"
        saved = await projects.save(params.project, f"{verb} {params.path}")
    except ProjectError as exc:
        raise _fail(exc) from exc
    lines = params.content.count("\n") + (0 if params.content.endswith("\n") else 1)
    return ActionResult(f"{verb[:-1]}ed {params.path} ({lines} lines){_saved_note(saved)}.")


async def edit_file(ctx: ActionContext, params: EditParams) -> ActionResult:
    projects = _projects(ctx)
    try:
        text = projects.read(params.project, params.path)
        count = text.count(params.old_text)
        if count == 0:
            raise ActionFailed(
                f"That text isn't in {params.path}; read the file again and copy it exactly "
                "(including spaces and indentation)"
            )
        if count > 1:
            raise ActionFailed(
                f"That text appears {count} times in {params.path}; include more of the "
                "surrounding lines so it's unique"
            )
        projects.write(params.project, params.path, text.replace(params.old_text, params.new_text))
        saved = await projects.save(params.project, f"Edit {params.path}")
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ActionResult(f"Edited {params.path}{_saved_note(saved)}.")


async def delete_file(ctx: ActionContext, params: FileParams) -> ActionResult:
    projects = _projects(ctx)
    try:
        projects.delete(params.project, params.path)
        saved = await projects.save(params.project, f"Delete {params.path}")
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ActionResult(f"Deleted {params.path}{_saved_note(saved)}.")


async def undo_last_change(ctx: ActionContext, params: ProjectParams) -> ActionResult:
    try:
        undone = await _projects(ctx).undo(params.project)
    except ProjectError as exc:
        raise _fail(exc) from exc
    return ActionResult(f'Undid "{undone}" in {params.project}.')


async def format_code(ctx: ActionContext, params: PathParams) -> ActionResult:
    projects = _projects(ctx)
    try:
        target = projects.resolve(params.project, params.path)
        cwd = projects.project(params.project)
    except ProjectError as exc:
        raise _fail(exc) from exc
    where = projects.relative(params.project, target)
    timeout = ctx.settings.coding_command_timeout_seconds
    fixed = await runner.run([_uv(), "tool", "run", "ruff", "check", "--fix", where], cwd, timeout)
    formatted = await runner.run([_uv(), "tool", "run", "ruff", "format", where], cwd, timeout)
    saved = await projects.save(params.project, f"Format {where}")
    return ActionResult(
        f"ruff check --fix:\n{fixed.output.strip()}\n\nruff format:\n{formatted.output.strip()}"
        f"{_saved_note(saved)}"
    )


# ---- running ----


def _report(what: str, outcome: runner.Outcome) -> ActionResult:
    if outcome.timed_out:
        status = f"was stopped after {outcome.seconds:.0f} s (too long)"
    else:
        status = f"exited with code {outcome.exit_code} after {outcome.seconds:.1f} s"
    output = outcome.output.strip() or "(no output)"
    return ActionResult(
        f"{what} {status}.\nOutput:\n{output}",
        {"exit_code": outcome.exit_code, "seconds": round(outcome.seconds, 1)},
    )


async def check_code(ctx: ActionContext, params: PathParams) -> ActionResult:
    projects = _projects(ctx)
    try:
        target = projects.resolve(params.project, params.path)
        cwd = projects.project(params.project)
    except ProjectError as exc:
        raise _fail(exc) from exc
    where = projects.relative(params.project, target)
    outcome = await runner.run(
        [_uv(), "tool", "run", "ruff", "check", "--output-format", "concise", where],
        cwd,
        ctx.settings.coding_command_timeout_seconds,
    )
    return _report("ruff check", outcome)


async def run_python(ctx: ActionContext, params: RunParams) -> ActionResult:
    projects = _projects(ctx)
    try:
        script = projects.resolve(params.project, params.file)
        cwd = projects.project(params.project)
    except ProjectError as exc:
        raise _fail(exc) from exc
    if script.suffix != ".py" or not script.is_file():
        raise ActionFailed(f"There is no Python file {params.file} in {params.project}")
    relative = projects.relative(params.project, script)
    outcome = await runner.run(
        [_uv(), "run", "-q", "python", relative, *params.args],
        cwd,
        ctx.settings.coding_command_timeout_seconds,
    )
    return _report(f"python {relative}", outcome)


async def run_tests(ctx: ActionContext, params: TestParams) -> ActionResult:
    projects = _projects(ctx)
    try:
        cwd = projects.project(params.project)
        paths = [
            projects.relative(params.project, projects.resolve(params.project, p))
            for p in params.paths
        ]
    except ProjectError as exc:
        raise _fail(exc) from exc
    outcome = await runner.run(
        [_uv(), "run", "-q", "--with", "pytest", "pytest", "-q", "-p", "no:cacheprovider", *paths],
        cwd,
        ctx.settings.coding_command_timeout_seconds,
    )
    if outcome.exit_code == 5:  # pytest: no tests collected
        return ActionResult("pytest found no tests to run.", {"exit_code": 5})
    return _report("pytest", outcome)


async def add_packages(ctx: ActionContext, params: PackageParams) -> ActionResult:
    projects = _projects(ctx)
    try:
        cwd = projects.project(params.project)
    except ProjectError as exc:
        raise _fail(exc) from exc
    outcome = await runner.run(
        [_uv(), "add", "-q", *params.packages],
        cwd,
        max(ctx.settings.coding_command_timeout_seconds, 300.0),
    )
    if outcome.exit_code == 0:
        saved = await projects.save(params.project, f"Add {', '.join(params.packages)}")
        return ActionResult(f"Added {', '.join(params.packages)}{_saved_note(saved)}.")
    return _report("uv add", outcome)


def _in(params) -> str:
    return f" in {params.project}"


CODING_ACTIONS = [
    Action(
        "list_projects",
        "List the Python projects in V.A.R.A.'s projects folder.",
        NoParams,
        Risk.READ_ONLY,
        list_projects,
        describe=lambda _p: "List the coding projects.",
        repeatable=True,
    ),
    Action(
        "list_files",
        "List the files in a project (or a folder of it), with sizes and its recent changes.",
        PathParams,
        Risk.READ_ONLY,
        list_files,
        describe=lambda p: f"List files in {p.project}/{p.path}.",
        repeatable=True,
    ),
    Action(
        "read_file",
        "Read a text file in a project, with line numbers; optionally just a range of lines.",
        ReadParams,
        Risk.READ_ONLY,
        read_file,
        describe=lambda p: f"Read {p.path}{_in(p)}.",
        repeatable=True,
    ),
    Action(
        "search_code",
        "Search a project's files for a regular expression; shows each matching line.",
        SearchParams,
        Risk.READ_ONLY,
        search_code,
        describe=lambda p: f"Search {p.glob} files{_in(p)} for {p.pattern!r}.",
        repeatable=True,
    ),
    Action(
        "check_code",
        "Lint a project (or a file or folder of it) with ruff. Doesn't run the code.",
        PathParams,
        Risk.READ_ONLY,
        check_code,
        describe=lambda p: f"Lint {p.path}{_in(p)} with ruff.",
        timeout=600.0,
        repeatable=True,
    ),
    Action(
        "create_project",
        "Create a new Python project: a folder with pyproject.toml, README.md, and its own git history.",
        NewProject,
        Risk.WORKSPACE_EDIT,
        create_project,
        describe=lambda p: f"Create the project {p.name}.",
    ),
    Action(
        "write_file",
        "Create a file in a project, or replace its whole content. Saved to the project's history.",
        WriteParams,
        Risk.WORKSPACE_EDIT,
        write_file,
        describe=lambda p: f"Write {p.path}{_in(p)}.",
        repeatable=True,
    ),
    Action(
        "edit_file",
        "Replace one exact, unique piece of text in a project file. Saved to the project's history.",
        EditParams,
        Risk.WORKSPACE_EDIT,
        edit_file,
        describe=lambda p: f"Edit {p.path}{_in(p)}.",
        repeatable=True,
    ),
    Action(
        "delete_file",
        "Delete a file from a project. Saved to the project's history, so it can be undone.",
        FileParams,
        Risk.WORKSPACE_EDIT,
        delete_file,
        describe=lambda p: f"Delete {p.path}{_in(p)}.",
        repeatable=True,
    ),
    Action(
        "undo_last_change",
        "Undo the latest change to a project (an edit, a format, or an install).",
        ProjectParams,
        Risk.WORKSPACE_EDIT,
        undo_last_change,
        describe=lambda p: f"Undo the latest change{_in(p)}.",
        repeatable=True,
    ),
    Action(
        "format_code",
        "Fix what ruff can fix automatically and format the code with ruff. Doesn't run the code.",
        PathParams,
        Risk.WORKSPACE_EDIT,
        format_code,
        describe=lambda p: f"Format {p.path}{_in(p)} with ruff.",
        timeout=600.0,
        repeatable=True,
    ),
    Action(
        "run_python",
        "Run a Python file in a project's environment and get its output. The user approves each run.",
        RunParams,
        Risk.RUN_CODE,
        run_python,
        describe=lambda p: f"Run python {p.file} {' '.join(p.args)}".rstrip() + f"{_in(p)}.",
        timeout=900.0,
        repeatable=True,
    ),
    Action(
        "run_tests",
        "Run a project's pytest tests and get the results. The user approves each run.",
        TestParams,
        Risk.RUN_CODE,
        run_tests,
        describe=lambda p: (
            f"Run the tests{_in(p)}" + (f" ({', '.join(p.paths)})." if p.paths else ".")
        ),
        timeout=900.0,
        repeatable=True,
    ),
    Action(
        "add_packages",
        "Install PyPI packages into a project (uv add). The user approves each install.",
        PackageParams,
        Risk.RUN_CODE,
        add_packages,
        describe=lambda p: f"Install {', '.join(p.packages)}{_in(p)} from PyPI.",
        timeout=900.0,
        repeatable=True,
    ),
]

CODING_TOOLS = frozenset(a.name for a in CODING_ACTIONS)
# Other tools the coding agent may use alongside its own (when they're available).
CODING_HELPERS = frozenset({"web_search", "current_time", "search_memory"})


def coding_instructions(settings) -> str:
    return CODING_INSTRUCTIONS.format(
        root=settings.projects_dir.resolve(), timeout=settings.coding_command_timeout_seconds
    )
