"""The agent loop: the model plans in steps and asks for actions; the backend checks, approves, runs, and
records each one.

One run:

1. The goal (plus the conversation so far and relevant memories) goes to the model with the available
   tools (vara/actions).
2. If the model answers in text, the run is complete. If it asks for actions, each request is checked:
   an unknown tool or invalid arguments is refused and the model is told why; an action already done in
   this run is not repeated (its earlier result is reused); a read-only action runs; anything else waits
   in the approval queue until you approve or reject it (or it expires).
3. Results go back to the model, and the next step begins.

Limits: at most `agent_max_steps` model turns (then one last turn to answer from what it has), a time
limit for the whole run, and a time limit per action. A run can be cancelled, and the pause switch stops
every run and refuses new ones until it is turned off; it survives a restart.

Every request for an action is written to the audit trail (`action_records`) with the run, the step it
came from, the permission it ran under, and the result. Arguments and results are redacted before they
are stored or sent back to a model. Progress is announced on the live-events bus (vara/events.py).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from vara import store
from vara.actions.base import ActionContext, ActionFailed, ActionRegistry
from vara.api import ROUTING_ERRORS, fill_clock
from vara.db.models import (
    ActionRecord,
    ActionStatus,
    AgentControl,
    AgentRun,
    Conversation,
    RunStatus,
    utcnow,
)
from vara.logging import redact
from vara.providers.base import Completed, Message, TextDelta, ToolCall, ToolSpec
from vara.router import ModelRouter

log = logging.getLogger("vara.agent")

MAX_RESULT_CHARS = 8000  # of one action's result, as sent back to the model
# A change (anything not read-only) that succeeded this recently, with the same arguments, is not run
# again even from another run: a retried request shouldn't start the same thing twice.
DUPLICATE_WINDOW = timedelta(minutes=10)
PAUSED = "paused"

AGENT_INSTRUCTIONS = (
    "You can use tools. Call one when you need information you don't have or the user asks you to do "
    "something a tool can do; you'll get its result, then continue. Tools that change something "
    "(marked as needing approval) are shown to the user first and only run if they approve. If an action "
    "is declined, fails, or expires, tell the user plainly and don't try to get the same effect another "
    "way. Only say something was done when a tool result confirms it. When you have what you need, "
    "answer the user directly."
)
LIMIT_NOTE = (
    "You have used all the steps available for this request. Don't call any more tools: answer now "
    "from what you have, and say briefly what you couldn't finish."
)


class AgentPaused(Exception):
    """The pause switch is on: no new runs start until it's turned off."""


class TooManyRuns(Exception):
    pass


class NotWaiting(Exception):
    """The action isn't waiting for an approval (already decided, expired, or not found)."""


@dataclass
class RunOptions:
    goal: str
    conversation_id: int | None = None
    provider: str | None = None
    model: str | None = None
    confirm_cloud: bool = False
    client: str | None = None
    client_time: datetime | None = None
    client_timezone: str | None = None
    max_steps: int | None = None
    think: bool | None = False
    # Chat runs its messages through the agent (vara/api.py) and sets these: the chat's own system prompt
    # and history (instead of building them from the goal), the tools this request may use (None: all),
    # whether the run saves its answer to the conversation (chat saves the exchange itself), and a queue
    # that receives the run's events as they happen:
    #   ("event", RoutedEvent)        every streamed model event, from every step
    #   ("result", action, data)      an action that succeeded, with its ActionResult data
    #   ("error", exception)          the routing error that ended the run (privacy, no provider)
    #   ("end", run_dict, totals)     always last
    system: str | None = None
    messages: list[Message] | None = None
    tools: set[str] | None = None
    save: bool = True
    sink: asyncio.Queue | None = None


class Approvals:
    """Actions waiting for your answer, and the runs waiting on them. In memory: after a restart, any
    approval still open has expired (see AgentManager.recover)."""

    def __init__(self) -> None:
        self._waiting: dict[int, asyncio.Future] = {}

    async def wait(self, action_id: int, timeout: float) -> tuple[bool | None, str | None]:
        """(approved, note), or (None, None) when nobody answered in time."""
        future = asyncio.get_running_loop().create_future()
        self._waiting[action_id] = future
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            return None, None
        finally:
            self._waiting.pop(action_id, None)

    def decide(self, action_id: int, approved: bool, note: str | None) -> bool:
        future = self._waiting.get(action_id)
        if future is None or future.done():
            return False
        future.set_result((approved, note))
        return True


def _fingerprint(name: str, arguments: str) -> str:
    return hashlib.sha256(f"{name}\n{arguments}".encode()).hexdigest()


def action_dict(record: ActionRecord) -> dict:
    try:
        arguments = json.loads(record.arguments)
    except ValueError:
        arguments = record.arguments
    return {
        "id": record.id,
        "run_id": record.run_id,
        "step": record.step,
        "action": record.action,
        "risk": record.risk,
        "arguments": arguments,
        "description": record.description,
        "status": record.status,
        "permission": record.permission,
        "decided_at": _iso(record.decided_at),
        "decision_note": record.decision_note,
        "result": record.result,
        "error": record.error,
        "created_at": _iso(record.created_at),
        "started_at": _iso(record.started_at),
        "finished_at": _iso(record.finished_at),
    }


def run_dict(run: AgentRun, *, with_actions: bool = False) -> dict:
    data = {
        "id": run.id,
        "goal": run.goal,
        "status": run.status,
        "conversation_id": run.conversation_id,
        "client": run.client,
        "provider": run.provider,
        "model": run.model,
        "steps": run.steps,
        "max_steps": run.max_steps,
        "final_text": run.final_text,
        "error": run.error,
        "created_at": _iso(run.created_at),
        "finished_at": _iso(run.finished_at),
    }
    if with_actions:
        data["actions"] = [action_dict(a) for a in run.actions]
    return data


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class AgentManager:
    def __init__(
        self,
        *,
        db,
        router: ModelRouter,
        registry: ActionRegistry,
        settings,
        bus=None,
        context: Callable[[RunOptions], ActionContext],
    ) -> None:
        self.db = db
        self.router = router
        self.registry = registry
        self.settings = settings
        self.bus = bus
        self.approvals = Approvals()
        self._context = context
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancel_reasons: dict[int, str] = {}
        self._shutting_down = False

    # ---- controls ----

    def recover(self) -> None:
        """At startup: a run or approval left open by a stopped server can't continue."""
        with self.db.session() as session:
            now = utcnow()
            for run in session.scalars(
                select(AgentRun).where(AgentRun.status == RunStatus.RUNNING)
            ):
                run.status = RunStatus.INTERRUPTED
                run.error = "The server stopped while this was running"
                run.finished_at = now
            open_actions = select(ActionRecord).where(
                ActionRecord.status.in_([ActionStatus.AWAITING_APPROVAL, ActionStatus.RUNNING])
            )
            for record in session.scalars(open_actions):
                record.status = (
                    ActionStatus.EXPIRED
                    if record.status == ActionStatus.AWAITING_APPROVAL
                    else ActionStatus.CANCELLED
                )
                record.error = "The server stopped before this finished"
                record.finished_at = now

    @property
    def paused(self) -> bool:
        with self.db.session() as session:
            control = session.get(AgentControl, PAUSED)
            return control is not None and control.value == "true"

    def pause(self) -> list[int]:
        """Stop every run now and refuse new ones until `resume`."""
        with self.db.session() as session:
            session.merge(AgentControl(key=PAUSED, value="true"))
        stopped = list(self._tasks)
        for run_id in stopped:
            self.cancel(run_id, "Paused: all agent activity was stopped")
        log.info("Agent activity paused (%d runs stopped)", len(stopped))
        self._publish("agent_paused", {"paused": True, "stopped_runs": stopped})
        return stopped

    def resume(self) -> None:
        with self.db.session() as session:
            session.merge(AgentControl(key=PAUSED, value="false"))
        log.info("Agent activity resumed")
        self._publish("agent_paused", {"paused": False, "stopped_runs": []})

    @property
    def running(self) -> list[int]:
        return list(self._tasks)

    def start(self, options: RunOptions) -> int:
        if self.paused:
            raise AgentPaused("Agent activity is paused")
        if len(self._tasks) >= self.settings.agent_max_running:
            raise TooManyRuns(f"{len(self._tasks)} runs are already going; wait or cancel one")
        max_steps = options.max_steps or self.settings.agent_max_steps
        with self.db.session() as session:
            if options.conversation_id is not None and (
                session.get(Conversation, options.conversation_id) is None
            ):
                raise LookupError("Conversation not found")
            run = AgentRun(
                goal=options.goal,
                conversation_id=options.conversation_id,
                client=options.client,
                max_steps=max_steps,
            )
            session.add(run)
            session.flush()
            run_id = run.id
            started = run_dict(run)
        task = asyncio.create_task(self._run(run_id, options, max_steps))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(run_id, None))
        self._publish("agent_run_started", started)
        return run_id

    async def wait(self, run_id: int) -> None:
        if task := self._tasks.get(run_id):
            await asyncio.shield(task)

    def cancel(self, run_id: int, reason: str = "Cancelled") -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        self._cancel_reasons[run_id] = reason
        task.cancel()
        return True

    def decide(self, action_id: int, approved: bool, note: str | None = None) -> None:
        with self.db.session() as session:
            record = session.get(ActionRecord, action_id)
            if record is None or record.status != ActionStatus.AWAITING_APPROVAL:
                raise NotWaiting("This action isn't waiting for approval")
        if not self.approvals.decide(action_id, approved, note):
            raise NotWaiting("This action isn't waiting for approval")
        log.info("Action %d %s", action_id, "approved" if approved else "rejected")

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---- the loop ----

    def _publish(self, event: str, data: dict) -> None:
        if self.bus is not None:
            self.bus.publish(event, data)

    def _start(self, options: RunOptions) -> tuple[list[Message], str]:
        if options.messages is not None:
            system = options.system
            return list(options.messages), (
                f"{system}\n\n{AGENT_INSTRUCTIONS}" if system else AGENT_INSTRUCTIONS
            )
        settings = self.settings
        configured = "\n\n".join(p for p in (settings.system_prompt, settings.personality) if p)
        base = fill_clock(configured, options.client_time, options.client_timezone)
        with self.db.session() as session:
            history: list[Message] = []
            if options.conversation_id is not None:
                conversation = session.get(Conversation, options.conversation_id)
                if conversation is not None:
                    history = store.history(conversation)
            memories = store.retrieve_memories(session, options.goal)
            system = store.memory_prompt(base or None, memories)
        system = f"{system}\n\n{AGENT_INSTRUCTIONS}" if system else AGENT_INSTRUCTIONS
        return [*history, Message("user", options.goal)], system

    async def _run(self, run_id: int, options: RunOptions, max_steps: int) -> None:
        totals = {"provider": None, "model": None, "input": None, "output": None, "cost": None}
        try:
            ctx = self._context(options)
            allowed = [
                a
                for a in self.registry.available(ctx)
                if options.tools is None or a.name in options.tools
            ]
            names = {a.name for a in allowed}
            tools = [a.spec() for a in allowed]
            messages, system = self._start(options)
            async with asyncio.timeout(self.settings.agent_run_timeout_seconds):
                for step in range(1, max_steps + 1):
                    self._update_run(run_id, steps=step)
                    text, calls = await self._turn(
                        run_id, step, messages, system, tools, options, totals
                    )
                    if not calls:
                        self._finish(run_id, RunStatus.COMPLETED, options, totals, text=text)
                        return
                    messages.append(Message("assistant", text, tool_calls=tuple(calls)))
                    for call in calls:
                        result = await self._execute(run_id, step, call, ctx, names, options.sink)
                        messages.append(
                            Message("tool", result, tool_call_id=call.id, name=call.name)
                        )
                # Out of steps: one last turn to answer from what it has. Tools are still described
                # (some providers need them to read the earlier tool turns), but any call is ignored.
                text, _ignored = await self._turn(
                    run_id,
                    max_steps + 1,
                    messages,
                    f"{system}\n\n{LIMIT_NOTE}",
                    tools,
                    options,
                    totals,
                )
                self._finish(
                    run_id,
                    RunStatus.STOPPED,
                    options,
                    totals,
                    text=text,
                    error=f"Reached the limit of {max_steps} steps",
                )
        except TimeoutError:
            limit = self.settings.agent_run_timeout_seconds
            self._finish(
                run_id, RunStatus.STOPPED, options, totals, error=f"Ran out of time ({limit:g} s)"
            )
        except asyncio.CancelledError:
            if self._shutting_down:
                status, reason = RunStatus.INTERRUPTED, "The server stopped while this was running"
            else:
                status, reason = RunStatus.CANCELLED, self._cancel_reasons.pop(run_id, "Cancelled")
            self._finish(run_id, status, options, totals, error=reason)
        except ROUTING_ERRORS as exc:
            if options.sink is not None:
                options.sink.put_nowait(("error", exc))
            self._finish(run_id, RunStatus.FAILED, options, totals, error=str(exc))
        except Exception as exc:
            log.exception("Agent run %d failed", run_id)
            self._finish(
                run_id, RunStatus.FAILED, options, totals, error=f"{type(exc).__name__}: {exc}"
            )

    async def _turn(
        self,
        run_id: int,
        step: int,
        messages: list[Message],
        system: str,
        tools: list[ToolSpec],
        options: RunOptions,
        totals: dict,
    ) -> tuple[str, list[ToolCall]]:
        parts: list[str] = []
        calls: list[ToolCall] = []
        async for routed in self.router.stream(
            messages,
            provider=options.provider,
            model=options.model,
            system=system,
            think=options.think,
            cloud_confirmed=options.confirm_cloud,
            tools=tools or None,
        ):
            event = routed.event
            if options.sink is not None:
                options.sink.put_nowait(("event", routed))
            if isinstance(event, TextDelta):
                parts.append(event.text)
                self._publish(
                    "agent_delta",
                    {
                        "run_id": run_id,
                        "step": step,
                        "provider": routed.provider,
                        "text": event.text,
                    },
                )
            elif isinstance(event, ToolCall):
                calls.append(event)
            elif isinstance(event, Completed):
                totals["provider"], totals["model"] = routed.provider, event.model
                for key, value in (("input", event.input_tokens), ("output", event.output_tokens)):
                    if value is not None:
                        totals[key] = (totals[key] or 0) + value
                if routed.cost_usd is not None:
                    totals["cost"] = (totals["cost"] or 0.0) + routed.cost_usd
        self._update_run(run_id, provider=totals["provider"], model=totals["model"])
        return "".join(parts).strip(), calls

    async def _execute(
        self,
        run_id: int,
        step: int,
        call: ToolCall,
        ctx: ActionContext,
        allowed: set[str],
        sink: asyncio.Queue | None = None,
    ) -> str:
        """Check, approve, and run one requested action. Returns what the model is told."""
        action = self.registry.get(call.name) if call.name in allowed else None
        arguments = json.dumps(call.arguments, sort_keys=True, default=str)
        fingerprint = _fingerprint(call.name, arguments)
        with self.db.session() as session:
            record = ActionRecord(
                run_id=run_id,
                step=step,
                tool_call_id=call.id[:100],
                action=call.name[:60],
                risk=action.risk if action else None,
                arguments=redact(arguments),
                fingerprint=fingerprint,
                status=ActionStatus.RUNNING,
            )
            session.add(record)
            session.flush()
            record_id = record.id
        try:
            if action is None or not action.available(ctx):
                names = ", ".join(sorted(allowed)) or "none"
                return self._close(
                    record_id,
                    ActionStatus.INVALID,
                    error="Unknown or unavailable tool",
                    result=f'There is no tool named "{call.name}". Available tools: {names}.',
                )
            try:
                params = action.params.model_validate(call.arguments)
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
                    for e in exc.errors()
                )
                return self._close(
                    record_id,
                    ActionStatus.INVALID,
                    error=problems,
                    result=(
                        f"The arguments for {call.name} weren't valid ({problems}). Nothing was done; "
                        "correct them and try again if it's still needed."
                    ),
                )
            self._update_record(record_id, description=action.describe(params))

            if (previous := self._previous_result(run_id, fingerprint, action.risk)) is not None:
                return self._close(
                    record_id,
                    ActionStatus.DUPLICATE,
                    permission="duplicate",
                    result=f"{previous}\n(This was already done, so it wasn't done again.)",
                )

            if action.risk.needs_approval:
                self._update_record(record_id, status=ActionStatus.AWAITING_APPROVAL)
                self._publish("approval_requested", self._record_data(record_id))
                approved, note = await self.approvals.wait(
                    record_id, self.settings.agent_approval_timeout_seconds
                )
                if approved is None:
                    return self._close(
                        record_id,
                        ActionStatus.EXPIRED,
                        result="The user didn't answer the approval request in time, so this was not done.",
                    )
                if not approved:
                    reason = f" ({note})" if note else ""
                    return self._close(
                        record_id,
                        ActionStatus.REJECTED,
                        decided_at=utcnow(),
                        decision_note=note,
                        result=(
                            f"The user declined this{reason}, so it was not done and will not be. "
                            "Say so plainly; don't offer to do it anyway."
                        ),
                    )
                self._update_record(
                    record_id, decided_at=utcnow(), decision_note=note, permission="approved"
                )
            else:
                self._update_record(record_id, permission="read-only")

            self._update_record(record_id, status=ActionStatus.RUNNING, started_at=utcnow())
            self._publish("agent_action", self._record_data(record_id))
            try:
                async with asyncio.timeout(action.timeout):
                    outcome = await action.run(ctx, params)
            except TimeoutError:
                return self._close(
                    record_id,
                    ActionStatus.TIMED_OUT,
                    error=f"Took longer than {action.timeout:g} s",
                    result=f"{call.name} took too long and was stopped; it may not have finished.",
                )
            except ActionFailed as exc:
                message = redact(str(exc))
                return self._close(
                    record_id,
                    ActionStatus.FAILED,
                    error=message,
                    result=f"{call.name} failed: {message}",
                )
            except Exception as exc:
                log.exception("Action %s failed", call.name)
                return self._close(
                    record_id,
                    ActionStatus.FAILED,
                    error=redact(f"{type(exc).__name__}: {exc}"),
                    result=f"{call.name} failed unexpectedly.",
                )
            text = redact(outcome.text)
            if len(text) > MAX_RESULT_CHARS:
                text = text[:MAX_RESULT_CHARS] + "\n[cut short]"
            if sink is not None:
                sink.put_nowait(("result", call.name, outcome.data))
            return self._close(record_id, ActionStatus.SUCCEEDED, result=text)
        except asyncio.CancelledError:
            self._close(record_id, ActionStatus.CANCELLED, error="Cancelled", only_if_open=True)
            raise

    def _previous_result(self, run_id: int, fingerprint: str, risk) -> str | None:
        """The result of the same action with the same arguments, if it already succeeded in this run
        (or, for a change, in any run in the last few minutes)."""
        scope = ActionRecord.run_id == run_id
        if risk.needs_approval:
            scope = or_(scope, ActionRecord.finished_at >= utcnow() - DUPLICATE_WINDOW)
        with self.db.session() as session:
            return session.scalars(
                select(ActionRecord.result)
                .where(
                    ActionRecord.fingerprint == fingerprint,
                    ActionRecord.status == ActionStatus.SUCCEEDED,
                    scope,
                )
                .order_by(ActionRecord.id.desc())
                .limit(1)
            ).first()

    # ---- records ----

    def _update_record(self, record_id: int, **fields) -> None:
        with self.db.session() as session:
            record = session.get(ActionRecord, record_id)
            for key, value in fields.items():
                setattr(record, key, value)

    def _record_data(self, record_id: int) -> dict:
        with self.db.session() as session:
            return action_dict(session.get(ActionRecord, record_id))

    def _close(
        self, record_id: int, status: ActionStatus, *, only_if_open: bool = False, **fields
    ) -> str:
        open_states = {ActionStatus.RUNNING, ActionStatus.AWAITING_APPROVAL}
        with self.db.session() as session:
            record = session.get(ActionRecord, record_id)
            if only_if_open and record.status not in open_states:
                return record.result or ""
            record.status = status
            record.finished_at = utcnow()
            for key, value in fields.items():
                setattr(record, key, value)
            data = action_dict(record)
        self._publish("agent_action", data)
        return fields.get("result") or ""

    def _update_run(self, run_id: int, **fields) -> None:
        with self.db.session() as session:
            run = session.get(AgentRun, run_id)
            for key, value in fields.items():
                setattr(run, key, value)

    def _finish(
        self,
        run_id: int,
        status: RunStatus,
        options: RunOptions,
        totals: dict,
        *,
        text: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.db.session() as session:
            run = session.get(AgentRun, run_id)
            run.status = status
            run.final_text = text
            run.error = error
            run.finished_at = utcnow()
            if text and totals["provider"] and options.save:
                run.conversation_id = self._save_to_conversation(session, options, text, totals)
            data = run_dict(run)
        self._cancel_reasons.pop(run_id, None)
        log.info("Agent run %d %s%s", run_id, status, f": {error}" if error else "")
        self._publish("agent_run_finished", data)
        if options.sink is not None:
            options.sink.put_nowait(("end", data, dict(totals)))

    def link(self, run_id: int, conversation_id: int) -> None:
        """Record the conversation a chat run's exchange was saved to (chat saves it, not the run)."""
        self._update_run(run_id, conversation_id=conversation_id)

    @staticmethod
    def _save_to_conversation(
        session: Session, options: RunOptions, text: str, totals: dict
    ) -> int | None:
        """The goal and the final answer join the conversation, like any chat exchange."""
        conversation = None
        if options.conversation_id is not None:
            conversation = session.get(Conversation, options.conversation_id)
            if conversation is None:  # deleted while the run was going
                return None
        if conversation is None:
            conversation = Conversation(title=store.title_from(options.goal))
        summary = {
            "provider": totals["provider"],
            "model": totals["model"],
            "usage": {"input_tokens": totals["input"], "output_tokens": totals["output"]},
            "cost_usd": totals["cost"],
        }
        store.save_exchange(session, conversation, [Message("user", options.goal)], text, summary)
        return conversation.id
