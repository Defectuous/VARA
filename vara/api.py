from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from vara.providers.base import Completed, Message, ProviderError, TextDelta
from vara.router import ConfirmationRequired, ModelRouter, NoProviderAvailable, PrivacyBlocked

v1 = APIRouter(prefix="/v1")


@v1.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    system: str | None = None
    confirm_cloud: bool = False


class Usage(BaseModel):
    input_tokens: int | None
    output_tokens: int | None


class ChatResponse(BaseModel):
    provider: str
    model: str
    text: str
    stop_reason: str | None
    usage: Usage


def _router(request: Request) -> ModelRouter:
    return request.app.state.router


@v1.get("/providers")
def list_providers(request: Request) -> dict:
    router = _router(request)
    return {
        "privacy_mode": router.privacy_mode,
        "providers": [
            {"name": p.name, "default_model": p.default_model, "local": p.capabilities.local}
            for p in router.providers
        ],
    }


@v1.post("/chat")
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    messages = [Message(m.role, m.content) for m in body.messages]
    parts: list[str] = []
    provider = ""
    completed: Completed | None = None
    try:
        async for routed in _router(request).stream(
            messages,
            provider=body.provider,
            model=body.model,
            system=body.system,
            cloud_confirmed=body.confirm_cloud,
        ):
            provider = routed.provider
            if isinstance(routed.event, TextDelta):
                parts.append(routed.event.text)
            else:
                completed = routed.event
    except PrivacyBlocked as exc:
        raise HTTPException(403, str(exc)) from exc
    except ConfirmationRequired as exc:
        raise HTTPException(409, f"{exc}; resend with confirm_cloud=true") from exc
    except NoProviderAvailable as exc:
        raise HTTPException(503, {"message": str(exc), "errors": exc.errors}) from exc
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc

    if completed is None:
        raise HTTPException(502, f"{provider} ended without completing")
    return ChatResponse(
        provider=provider,
        model=completed.model,
        text="".join(parts),
        stop_reason=completed.stop_reason,
        usage=Usage(input_tokens=completed.input_tokens, output_tokens=completed.output_tokens),
    )
