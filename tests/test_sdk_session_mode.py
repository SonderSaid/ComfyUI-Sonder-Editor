"""The ``"sdk"`` editor host mode in the session registry (server/session_registry.py)."""

import asyncio

import pytest

from server import session_registry
from server.session_registry import claim_session, get_owner, heartbeat_session, release_session

PROJECT = "sdk-mode-project"
SDK_HOST = "sdk-host-1"
SECRET = "s3cret"


@pytest.fixture(autouse=True)
def resolver():
    session_registry.set_sdk_secret_resolver(lambda host_id: SECRET if host_id == SDK_HOST else None)
    yield
    session_registry.set_sdk_secret_resolver(None)

    async def cleanup():
        for host in (SDK_HOST, "plain-host"):
            await release_session(PROJECT, "cleanup", force=True, host_id=host)

    asyncio.run(cleanup())


def run(coro_fn):
    return asyncio.run(coro_fn())


def test_an_sdk_host_is_claimable_only_with_its_secret_whatever_the_mode():
    async def go():
        assert (await claim_session(PROJECT, "tab-a", "sdk", {"host_id": SDK_HOST}))["code"] == "invalid_sdk_session"
        wrong = await claim_session(PROJECT, "tab-a", "sdk", {"host_id": SDK_HOST}, sdk_secret="nope")
        assert wrong["code"] == "invalid_sdk_session"
        # Naming another mode does not dodge the check on an SDK host id.
        sneaky = await claim_session(PROJECT, "tab-x", "fullscreen", {"host_id": SDK_HOST})
        assert sneaky["code"] == "invalid_sdk_session"
        # "sdk" on a host id that is not an SDK session is refused too.
        stray = await claim_session(PROJECT, "tab-y", "sdk", {"host_id": "plain-host"}, sdk_secret=SECRET)
        assert stray["code"] == "invalid_sdk_session"

        ok = await claim_session(PROJECT, "tab-a", "sdk", {"host_id": SDK_HOST}, sdk_secret=SECRET)
        assert ok["ok"] and ok["owner"]["host_mode"] == "sdk" and ok["took_over"] is False

    run(go)


def test_a_claim_with_the_secret_takes_over_and_announces_it(monkeypatch):
    events = []
    monkeypatch.setattr(session_registry, "schedule_project_event", lambda pid, event: events.append(event))

    async def go():
        await claim_session(PROJECT, "tab-a", "sdk", {"host_id": SDK_HOST}, sdk_secret=SECRET)
        second = await claim_session(PROJECT, "tab-b", "sdk", {"host_id": SDK_HOST}, sdk_secret=SECRET)
        assert second["ok"] and second["took_over"] is True
        assert (await get_owner(PROJECT, host_id=SDK_HOST))["session_id"] == "tab-b"
        # The displaced tab's heartbeat is refused, so it shows "Opened in another tab".
        assert (await heartbeat_session(PROJECT, "tab-a", host_id=SDK_HOST))["code"] == "locked"

    run(go)
    assert events[-1]["type"] == "session_changed" and events[-1]["reason"] == "taken_over"


def test_a_stale_sdk_owner_is_orphaned_not_released(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(session_registry, "_now", lambda: now[0])

    async def go():
        await claim_session(PROJECT, "tab-a", "sdk", {"host_id": SDK_HOST}, sdk_secret=SECRET)
        now[0] += session_registry.SESSION_TTL_SECONDS + 5
        owner = await get_owner(PROJECT, host_id=SDK_HOST)
        assert owner["status"] == "orphaned"
        # A background tab that comes back reactivates its own claim.
        back = await heartbeat_session(PROJECT, "tab-a", host_id=SDK_HOST)
        assert back["ok"] and back["owner"]["status"] == "active"

    run(go)


def test_ordinary_hosts_are_unchanged():
    async def go():
        first = await claim_session(PROJECT, "fs-1", "fullscreen", {"host_id": "plain-host"})
        assert first["ok"]
        locked = await claim_session(PROJECT, "fs-2", "fullscreen", {"host_id": "plain-host"})
        assert locked["code"] == "locked"
        tab = await claim_session(PROJECT, "tab-1", "tab", {"host_id": "plain-host"})
        assert tab["code"] == "invalid_handoff"

    run(go)
