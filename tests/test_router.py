import asyncio

import pytest

from vara.providers.base import Completed, Message, TextDelta
from vara.providers.fake import FakeProvider
from vara.router import (
    ConfirmationRequired,
    ModelRouter,
    NoProviderAvailable,
    PrivacyBlocked,
    PrivacyMode,
)

HELLO = [Message("user", "hello")]


def collect(router: ModelRouter, **kwargs) -> tuple[str, str]:
    async def run() -> tuple[str, str]:
        provider, text = "", ""
        async for routed in router.stream(HELLO, **kwargs):
            provider = routed.provider
            if isinstance(routed.event, TextDelta):
                text += routed.event.text
            else:
                assert isinstance(routed.event, Completed)
        return provider, text.strip()

    return asyncio.run(run())


def test_local_only_never_calls_a_cloud_provider() -> None:
    cloud = FakeProvider("cloud", local=False)
    router = ModelRouter([cloud], PrivacyMode.LOCAL_ONLY)

    with pytest.raises(NoProviderAvailable):
        collect(router)
    with pytest.raises(PrivacyBlocked):
        collect(router, provider="cloud")
    with pytest.raises(PrivacyBlocked):
        collect(router, provider="cloud", cloud_confirmed=True)
    assert cloud.calls == []


def test_local_only_uses_local_provider() -> None:
    router = ModelRouter(
        [FakeProvider("cloud", local=False), FakeProvider("local")], PrivacyMode.LOCAL_ONLY
    )

    assert collect(router) == ("local", "echo: hello")


def test_local_only_does_not_fall_back_to_cloud_when_local_fails() -> None:
    cloud = FakeProvider("cloud", local=False)
    router = ModelRouter([FakeProvider("local", fail=True), cloud], PrivacyMode.LOCAL_ONLY)

    with pytest.raises(NoProviderAvailable) as exc_info:
        collect(router)
    assert "local" in exc_info.value.errors
    assert cloud.calls == []


def test_ask_mode_requires_confirmation_for_cloud() -> None:
    cloud = FakeProvider("cloud", local=False)
    router = ModelRouter([cloud], PrivacyMode.ASK)

    with pytest.raises(ConfirmationRequired):
        collect(router)
    assert cloud.calls == []
    assert collect(router, cloud_confirmed=True) == ("cloud", "echo: hello")


def test_allowed_mode_falls_back_from_failed_local_to_cloud() -> None:
    router = ModelRouter(
        [FakeProvider("local", fail=True), FakeProvider("cloud", local=False)],
        PrivacyMode.ALLOWED,
    )

    assert collect(router) == ("cloud", "echo: hello")


def test_manual_choice_wins_and_does_not_fall_back() -> None:
    local = FakeProvider("local")
    router = ModelRouter(
        [local, FakeProvider("cloud", local=False, fail=True)], PrivacyMode.ALLOWED
    )

    with pytest.raises(NoProviderAvailable):
        collect(router, provider="cloud")
    assert local.calls == []


def test_unknown_provider_is_reported() -> None:
    router = ModelRouter([FakeProvider("local")], PrivacyMode.ALLOWED)

    with pytest.raises(NoProviderAvailable) as exc_info:
        collect(router, provider="nope")
    assert exc_info.value.errors == {"nope": "not configured"}
