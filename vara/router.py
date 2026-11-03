from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from enum import StrEnum

from vara.providers.base import Completed, Message, ModelProvider, ProviderError, StreamEvent
from vara.usage import UsageTracker, estimate_cost


class PrivacyMode(StrEnum):
    LOCAL_ONLY = "local_only"  # no request ever leaves this machine
    ASK = "ask"  # cloud requests need explicit confirmation per request
    ALLOWED = "allowed"  # configured cloud providers may be used


class PrivacyBlocked(Exception):
    """The privacy mode forbids sending this request to a cloud provider."""


class ConfirmationRequired(Exception):
    """The privacy mode is `ask` and the request did not confirm cloud use."""


class NoProviderAvailable(Exception):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("No model provider could answer")


@dataclass(frozen=True)
class RoutedEvent:
    provider: str
    event: StreamEvent
    cost_usd: float | None = None  # set on the Completed event


class ModelRouter:
    """The only path to a model. Applies the privacy mode, then tries providers in order.

    A manual provider choice always wins and never falls back. Otherwise local providers are
    tried before cloud ones, moving on when a provider fails before producing any text.
    """

    def __init__(
        self,
        providers: Sequence[ModelProvider],
        privacy_mode: PrivacyMode,
        usage: UsageTracker | None = None,
    ) -> None:
        self._providers = {p.name: p for p in providers}
        self.privacy_mode = privacy_mode
        self.usage = usage or UsageTracker()

    @property
    def providers(self) -> list[ModelProvider]:
        return list(self._providers.values())

    def _permit(self, provider: ModelProvider, cloud_confirmed: bool) -> None:
        if provider.capabilities.local:
            return
        if self.privacy_mode is PrivacyMode.LOCAL_ONLY:
            raise PrivacyBlocked(
                f"Privacy mode 'local_only' blocks the cloud provider '{provider.name}'"
            )
        if self.privacy_mode is PrivacyMode.ASK and not cloud_confirmed:
            raise ConfirmationRequired(
                f"Sending this request to '{provider.name}' needs confirmation"
            )

    def _candidates(self, provider: str | None, cloud_confirmed: bool) -> list[ModelProvider]:
        if provider is not None:
            if provider not in self._providers:
                raise NoProviderAvailable({provider: "not configured"})
            chosen = self._providers[provider]
            self._permit(chosen, cloud_confirmed)
            return [chosen]

        ordered = sorted(self._providers.values(), key=lambda p: not p.capabilities.local)
        allowed = []
        for candidate in ordered:
            try:
                self._permit(candidate, cloud_confirmed)
            except PrivacyBlocked:
                continue
            except ConfirmationRequired:
                if not allowed:
                    # Nothing local to use: tell the caller confirmation would unlock an answer.
                    raise
                continue
            allowed.append(candidate)
        if not allowed:
            raise NoProviderAvailable({})
        return allowed

    def _record(self, provider: ModelProvider, done: Completed) -> float | None:
        cost = estimate_cost(
            done.model, done.input_tokens, done.output_tokens, local=provider.capabilities.local
        )
        self.usage.record(provider.name, done.model, done.input_tokens, done.output_tokens, cost)
        return cost

    def preview(
        self,
        messages: Sequence[Message],
        *,
        provider: str | None = None,
        model: str | None = None,
        system: str | None = None,
        cloud_confirmed: bool = False,
    ) -> list[dict]:
        """What each candidate would be sent, in the order they would be tried. Calls nothing."""
        return [
            {
                "provider": candidate.name,
                "local": candidate.capabilities.local,
                "request": candidate.request_body(messages, model=model, system=system),
            }
            for candidate in self._candidates(provider, cloud_confirmed)
        ]

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        provider: str | None = None,
        model: str | None = None,
        system: str | None = None,
        cloud_confirmed: bool = False,
    ) -> AsyncIterator[RoutedEvent]:
        errors: dict[str, str] = {}
        for candidate in self._candidates(provider, cloud_confirmed):
            started = False
            try:
                async for event in candidate.stream(messages, model=model, system=system):
                    started = True
                    if isinstance(event, Completed):
                        yield RoutedEvent(candidate.name, event, self._record(candidate, event))
                    else:
                        yield RoutedEvent(candidate.name, event)
                return
            except ProviderError as exc:
                if started:
                    raise
                errors[candidate.name] = str(exc)
        raise NoProviderAvailable(errors)
