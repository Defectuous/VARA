"""The agent loop's API: start and follow runs, answer approvals, read the audit trail, and the pause
switch. Progress is also announced live on /v1/events (see vara/events.py)."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from vara.agent import (
    AgentManager,
    AgentPaused,
    NotWaiting,
    RunOptions,
    TooManyRuns,
    action_dict,
    run_dict,
)
from vara.coding.actions import CODING_TOOLS
from vara.db.models import ActionRecord, ActionStatus, AgentRun
from vara.reasoning import ReasoningMode, should_reason

agent = APIRouter(prefix="/v1/agent")


def _manager(request: Request) -> AgentManager:
    return request.app.state.agent


class RunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    conversation_id: int | None = None
    provider: str | None = None
    model: str | None = None
    confirm_cloud: bool = False
    client: str | None = Field(default=None, max_length=64)
    client_time: datetime | None = None
    client_timezone: str | None = Field(default=None, max_length=64)
    max_steps: int | None = Field(default=None, ge=1, le=20)
    reasoning: ReasoningMode | None = None
    # Wait for the run to finish (including any approvals) before answering. Otherwise the run is
    # returned as soon as it starts; follow it on /v1/events or GET /v1/agent/runs/{id}.
    wait: bool = False

    @field_validator("client_time")
    @classmethod
    def _has_offset(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("client_time must include its UTC offset")
        return value


def _run(request: Request, run_id: int) -> dict:
    with request.app.state.db.session() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        return run_dict(run, with_actions=True)


@agent.post("/runs")
async def start_run(body: RunRequest, request: Request) -> dict:
    manager = _manager(request)
    options = RunOptions(
        goal=body.goal,
        conversation_id=body.conversation_id,
        provider=body.provider,
        model=body.model,
        confirm_cloud=body.confirm_cloud,
        client=body.client,
        client_time=body.client_time,
        client_timezone=body.client_timezone,
        max_steps=body.max_steps,
        # The coding tools belong to the Python coding agent (chat with agent "python").
        tools={a.name for a in manager.registry.all()} - CODING_TOOLS,
        think=should_reason(
            body.goal, spoken=False, mode=body.reasoning or request.app.state.settings.reasoning
        ),
    )
    try:
        run_id = manager.start(options)
    except AgentPaused as exc:
        raise HTTPException(409, f"{exc}; resume it with POST /v1/agent/resume") from exc
    except TooManyRuns as exc:
        raise HTTPException(429, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if body.wait:
        await manager.wait(run_id)
    return _run(request, run_id)


@agent.get("/runs")
def list_runs(request: Request, limit: int = 20) -> dict:
    with request.app.state.db.session() as session:
        runs = session.scalars(
            select(AgentRun).order_by(AgentRun.id.desc()).limit(min(max(limit, 1), 200))
        )
        return {"runs": [run_dict(r) for r in runs]}


@agent.get("/runs/{run_id}")
def get_run(run_id: int, request: Request) -> dict:
    return _run(request, run_id)


@agent.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int, request: Request) -> dict:
    manager = _manager(request)
    _run(request, run_id)  # 404 for an unknown run
    if not manager.cancel(run_id):
        raise HTTPException(409, "That run isn't running")
    await manager.wait(run_id)
    return _run(request, run_id)


@agent.get("/approvals")
def pending_approvals(request: Request) -> dict:
    """The approval queue: actions waiting for your answer, oldest first."""
    with request.app.state.db.session() as session:
        waiting = session.scalars(
            select(ActionRecord)
            .where(ActionRecord.status == ActionStatus.AWAITING_APPROVAL)
            .order_by(ActionRecord.id)
        )
        return {"approvals": [action_dict(a) for a in waiting]}


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=500)


@agent.post("/approvals/{action_id}")
def decide(action_id: int, body: Decision, request: Request) -> dict:
    try:
        _manager(request).decide(action_id, body.decision == "approve", body.note)
    except NotWaiting as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": action_id, "decision": body.decision}


@agent.get("/actions")
def audit(
    request: Request,
    run_id: int | None = None,
    status: ActionStatus | None = None,
    action: str | None = None,
    limit: int = 50,
) -> dict:
    """The audit trail: every action any run asked for, newest first."""
    query = select(ActionRecord).order_by(ActionRecord.id.desc()).limit(min(max(limit, 1), 500))
    if run_id is not None:
        query = query.where(ActionRecord.run_id == run_id)
    if status is not None:
        query = query.where(ActionRecord.status == status)
    if action is not None:
        query = query.where(ActionRecord.action == action)
    with request.app.state.db.session() as session:
        return {"actions": [action_dict(a) for a in session.scalars(query)]}


@agent.get("/tools")
def tools(request: Request) -> dict:
    """Every action the agent knows, its risk level, and whether it can be used right now."""
    manager = _manager(request)
    ctx = request.app.state.agent_context(RunOptions(goal=""))
    return {
        "tools": [
            {
                "name": a.name,
                "description": a.description,
                "risk": a.risk,
                "needs_approval": a.risk.needs_approval,
                "available": a.available(ctx),
                "parameters": a.spec().parameters,
            }
            for a in manager.registry.all()
        ]
    }


@agent.get("/status")
def status(request: Request) -> dict:
    manager = _manager(request)
    with request.app.state.db.session() as session:
        waiting = session.scalars(
            select(ActionRecord.id).where(ActionRecord.status == ActionStatus.AWAITING_APPROVAL)
        ).all()
    return {"paused": manager.paused, "running": manager.running, "pending_approvals": len(waiting)}


@agent.post("/pause")
async def pause(request: Request) -> dict:
    """Stop every agent run now (pending approvals with them) and refuse new runs until resumed. The
    switch survives a restart."""
    manager = _manager(request)
    stopped = manager.pause()
    for run_id in stopped:
        await manager.wait(run_id)
    return {"paused": True, "stopped_runs": stopped}


@agent.post("/resume")
def resume(request: Request) -> dict:
    _manager(request).resume()
    return {"paused": False}
