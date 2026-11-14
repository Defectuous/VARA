"""Answer-quality evals: ask the running backend a fixed set of questions and grade the replies.

`python -m vara eval` sends each case in `eval_cases.toml` to `/v1/chat` (the public API, like any
client), grades the reply with simple checks, saves the run under `data/evals/`, and compares the
score with the previous run of the same model, so a model or package update that makes answers
worse shows up as a regression instead of going unnoticed.
"""

from __future__ import annotations

import json
import re
import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

CASES_FILE = Path(__file__).with_name("eval_cases.toml")

# Every check a case may use. Text checks ignore case unless noted.
CHECKS = {
    "expect_all",  # every string must appear
    "expect_any",  # at least one string must appear
    "reject_any",  # none of these strings may appear
    "expect_regex",  # each pattern must match (re.IGNORECASE | re.MULTILINE)
    "reject_regex",  # no pattern may match
    "expect_number",  # this number must appear in the reply (commas ignored)
    "max_words",  # the whole reply has at most this many words
    "line_word_counts",  # the reply's non-empty lines have exactly these word counts
    "first_letters",  # the non-empty lines start with these letters, in order
    "no_markdown",  # no bold, headings, bullets, or code fences
    "expect_search",  # a web search did (true) or did not (false) run
    "expect_reasoning",  # the model reasoned (true) or didn't (false) before answering
}
REQUEST_FIELDS = {"web_search", "reasoning", "spoken"}
CASE_FIELDS = {"id", "category", "question", "note"} | REQUEST_FIELDS | CHECKS

WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
MARKDOWN = re.compile(r"\*\*|__|^#{1,6}\s|^\s*[-*+]\s|^\s*\d+\.\s|```", re.MULTILINE)


@dataclass
class Case:
    id: str
    category: str
    question: str
    request: dict[str, Any]
    checks: dict[str, Any]


@dataclass
class CaseResult:
    id: str
    category: str
    passed: bool
    failures: list[str]
    seconds: float
    reasoning: bool | None = None
    searched: bool = False
    unresponsive_engines: list[str] = field(default_factory=list)
    text: str = ""
    error: str | None = None


def load_cases(path: Path = CASES_FILE) -> list[Case]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cases, seen = [], set()
    for raw in data.get("case", []):
        unknown = set(raw) - CASE_FIELDS
        if unknown:
            raise ValueError(f"eval case {raw.get('id')!r} has unknown fields: {sorted(unknown)}")
        if raw["id"] in seen:
            raise ValueError(f"duplicate eval case id {raw['id']!r}")
        seen.add(raw["id"])
        cases.append(
            Case(
                id=raw["id"],
                category=raw["category"],
                question=raw["question"],
                request={k: raw[k] for k in REQUEST_FIELDS if k in raw},
                checks={k: raw[k] for k in CHECKS if k in raw},
            )
        )
    return cases


def _fill(value: str, now: datetime) -> str:
    """Placeholders for answers that depend on the day the eval runs."""
    return (
        value.replace("{month_name}", now.strftime("%B"))
        .replace("{day}", str(now.day))
        .replace("{year}", str(now.year))
    )


def grade(
    checks: dict[str, Any],
    text: str,
    *,
    searched: bool = False,
    reasoning: bool | None = None,
    now: datetime | None = None,
) -> list[str]:
    """What the reply got wrong, as readable reasons; an empty list means it passed."""
    now = now or datetime.now().astimezone()
    # Models often write curly quotes ("don’t"); compare them as straight ones.
    text = text.translate(QUOTES)
    lower = text.lower()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    failures: list[str] = []

    def strings(key: str) -> list[str]:
        return [_fill(s, now) for s in checks.get(key, [])]

    for s in strings("expect_all"):
        if s.lower() not in lower:
            failures.append(f"missing {s!r}")
    if "expect_any" in checks and not any(s.lower() in lower for s in strings("expect_any")):
        failures.append(f"none of {strings('expect_any')!r}")
    for s in strings("reject_any"):
        if s.lower() in lower:
            failures.append(f"contains {s!r}")
    for pattern in strings("expect_regex"):
        if not re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            failures.append(f"no match for /{pattern}/")
    for pattern in strings("reject_regex"):
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            failures.append(f"matches /{pattern}/")
    if "expect_number" in checks:
        wanted = float(checks["expect_number"])
        found = [float(n) for n in NUMBER.findall(text.replace(",", "").replace("−", "-"))]
        if wanted not in found:
            failures.append(f"number {checks['expect_number']} not in reply")
    if "max_words" in checks:
        words = len(WORD.findall(text))
        if words > checks["max_words"]:
            failures.append(f"{words} words (max {checks['max_words']})")
    if "line_word_counts" in checks:
        counts = [len(WORD.findall(line)) for line in lines]
        if counts != checks["line_word_counts"]:
            failures.append(f"line word counts {counts}, wanted {checks['line_word_counts']}")
    if "first_letters" in checks:
        letters = "".join(
            m.group(0)[0] for line in lines if (m := re.search(r"[A-Za-z]", line))
        ).upper()
        if letters != checks["first_letters"].upper():
            failures.append(f"lines start {letters!r}, wanted {checks['first_letters']!r}")
    if checks.get("no_markdown") and MARKDOWN.search(text):
        failures.append("uses Markdown")
    if "expect_search" in checks and searched != checks["expect_search"]:
        failures.append("didn't search the web" if checks["expect_search"] else "searched the web")
    if "expect_reasoning" in checks and reasoning != checks["expect_reasoning"]:
        failures.append(
            f"reasoning was {'on' if reasoning else 'off'}, "
            f"wanted {'on' if checks['expect_reasoning'] else 'off'}"
        )
    return failures


class EvalRunner:
    def __init__(self, base_url: str, results_dir: Path, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=base_url, timeout=900.0)
        self.results_dir = results_dir

    def check_backend(self) -> None:
        try:
            self.client.get("/health", timeout=5.0).raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                "V.A.R.A. isn't running; start it with .\\start-vara.ps1 and try again"
            ) from exc

    def run_case(self, case: Case, model: str, *, keep: bool = False) -> CaseResult:
        now = datetime.now().astimezone()
        body = {
            "messages": [{"role": "user", "content": case.question}],
            "provider": "ollama",
            "model": model,
            "use_memory": False,  # your memories would make results differ from run to run
            "web_search": False,
            "client": "vara-eval",
            "client_time": now.isoformat(),
            **case.request,
        }
        start = time.monotonic()
        try:
            response = self.client.post("/v1/chat", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            detail = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
            return CaseResult(
                id=case.id,
                category=case.category,
                passed=False,
                failures=["request failed"],
                seconds=round(time.monotonic() - start, 1),
                error=detail[:500],
            )
        data = response.json()
        seconds = round(time.monotonic() - start, 1)
        search = data.get("web_search") or {}
        if not keep:
            # The exchange was only a test; don't leave it in the conversation history.
            self.client.delete(f"/v1/conversations/{data['conversation_id']}")
        failures = grade(
            case.checks,
            data["text"],
            searched=bool(search),
            reasoning=data.get("reasoning"),
            now=now,
        )
        return CaseResult(
            id=case.id,
            category=case.category,
            passed=not failures,
            failures=failures,
            seconds=seconds,
            reasoning=data.get("reasoning"),
            searched=bool(search),
            unresponsive_engines=list(search.get("unresponsive") or []),
            text=data["text"],
        )

    def model_digest(self, ollama_url: str, model: str) -> str | None:
        """The model's Ollama digest, so the history shows when the model itself changed."""
        try:
            tags = httpx.get(f"{ollama_url}/api/tags", timeout=5.0).json()
        except (httpx.HTTPError, ValueError):
            return None
        for m in tags.get("models", []):
            if m.get("name") == model:
                return (m.get("digest") or "")[:12] or None
        return None

    def save(self, model: str, digest: str | None, results: list[CaseResult]) -> dict:
        """Write the full run and append its summary to history.jsonl; return the summary."""
        at = datetime.now().astimezone()
        slug = re.sub(r"[^A-Za-z0-9.-]+", "_", model)
        runs = self.results_dir / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        run_file = runs / f"{at:%Y%m%d-%H%M%S}-{slug}.json"
        by_category: dict[str, list[int]] = {}
        for r in results:
            tally = by_category.setdefault(r.category, [0, 0])
            tally[0] += r.passed
            tally[1] += 1
        passed = sum(r.passed for r in results)
        summary = {
            "at": at.isoformat(timespec="seconds"),
            "model": model,
            "digest": digest,
            "passed": passed,
            "total": len(results),
            "score": round(100 * passed / len(results), 1) if results else 0.0,
            "by_category": {k: f"{p}/{t}" for k, (p, t) in sorted(by_category.items())},
            "results": {r.id: r.passed for r in results},
            "run_file": run_file.name,
        }
        run_file.write_text(
            json.dumps(
                {**summary, "cases": [asdict(r) for r in results]}, indent=2, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        with (self.results_dir / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        return summary

    def previous(self, model: str, case_ids: set[str]) -> dict | None:
        """The latest earlier run of this model that covered the same cases."""
        history = self.results_dir / "history.jsonl"
        if not history.exists():
            return None
        latest = None
        for line in history.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("model") == model and set(entry.get("results", {})) == case_ids:
                latest = entry
        return latest


def compare(previous: dict | None, current: dict) -> dict:
    """What changed since the previous run: score delta, newly failing and newly passing cases."""
    if previous is None:
        return {"delta": None, "regressed": [], "fixed": []}
    before, now = previous["results"], current["results"]
    return {
        "delta": round(current["score"] - previous["score"], 1),
        "regressed": sorted(k for k, ok in now.items() if before.get(k) and not ok),
        "fixed": sorted(k for k, ok in now.items() if ok and before.get(k) is False),
        "model_changed": previous.get("digest") != current.get("digest"),
    }
