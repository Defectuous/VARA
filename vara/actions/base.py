"""Typed actions.

A model never executes anything. It can only ask for an action by name with arguments; the agent loop
looks the action up here, checks the arguments against the action's Pydantic model, and either runs it
(read-only actions) or waits for your approval first (everything else). Every request is recorded in the
audit trail, whatever happens to it.

Each action declares a risk level:

- read_only: looks things up; changes nothing. Runs without asking.
- local_change: changes V.A.R.A.'s own data on this PC (for example, starts a price watch).
- external_effect: does something outside this PC (sends, posts, books).
- financial: involves money.

Everything above read_only needs your approval each time, until narrower rules exist (Phase 13).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from vara.providers.base import ToolSpec


class Risk(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_CHANGE = "local_change"
    EXTERNAL_EFFECT = "external_effect"
    FINANCIAL = "financial"

    @property
    def needs_approval(self) -> bool:
        return self is not Risk.READ_ONLY


class Params(BaseModel):
    """Base for action arguments: unknown fields are an error, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


class NoParams(Params):
    pass


@dataclass
class ActionContext:
    """What an action may use. Anything that's None (web search turned off, say) makes the actions that
    need it unavailable."""

    db: Any
    settings: Any
    web: Any = None
    markets: Any = None
    deals: Any = None
    weather: Any = None  # the weather watch, when a location is set
    now: datetime | None = None  # the asking device's clock, when it sent one
    timezone: str | None = None


@dataclass
class ActionResult:
    text: str  # what the model is told
    data: dict = field(default_factory=dict)


class ActionFailed(Exception):
    """An action couldn't do its job; the message is told to the model."""


def _describe_default(params: BaseModel) -> str:
    return str(params.model_dump())


@dataclass(frozen=True)
class Action:
    name: str
    description: str  # for the model: what it does and when to use it
    params: type[Params]
    risk: Risk
    run: Callable[[ActionContext, Any], Awaitable[ActionResult]]
    # What it will do with these arguments, in words, for the approval prompt.
    describe: Callable[[Any], str] = _describe_default
    timeout: float = 30.0
    needs: str | None = None  # an ActionContext field that must be set, e.g. "web"

    def available(self, ctx: ActionContext) -> bool:
        return self.needs is None or getattr(ctx, self.needs) is not None

    def spec(self) -> ToolSpec:
        schema = self.params.model_json_schema()
        schema.pop("title", None)
        schema.setdefault("properties", {})
        return ToolSpec(self.name, self.description, schema)


class ActionRegistry:
    def __init__(self, actions: list[Action] | None = None) -> None:
        self._actions: dict[str, Action] = {}
        for action in actions or []:
            self.register(action)

    def register(self, action: Action) -> None:
        if action.name in self._actions:
            raise ValueError(f"An action named {action.name!r} is already registered")
        self._actions[action.name] = action

    def get(self, name: str) -> Action | None:
        return self._actions.get(name)

    def all(self) -> list[Action]:
        return list(self._actions.values())

    def available(self, ctx: ActionContext) -> list[Action]:
        return [a for a in self._actions.values() if a.available(ctx)]
