"""Real PostgreSQL checkouts must queue without consuming another pool slot."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AsyncExitStack, ExitStack

import pytest
from sqlalchemy import text

from src.core import scheduler
from src.core.config import get_settings
from src.core.database import make_api_engine
from src.email_monitor import email_monitor_manager as monitor


@pytest.fixture
def pool_engines(monkeypatch, async_engine, sync_engine):
    settings = get_settings().model_copy(
        update={
            "database_url": async_engine.url.render_as_string(hide_password=False),
            "database_url_sync": sync_engine.url.render_as_string(hide_password=False),
        }
    )
    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(monitor, "get_settings", lambda: settings)
    scheduled = scheduler.make_session_local().kw["bind"]
    # Exercise the actual factory without retaining a test engine in its cache.
    observed = monitor.session_factory.__wrapped__().kw["bind"]
    try:
        yield settings, scheduled, observed
    finally:
        observed.dispose()
        scheduled.dispose()


async def test_api_and_background_pools_leave_four_session_slots(pool_engines):
    settings, scheduled, observed = pool_engines
    api = make_api_engine(settings)
    try:
        async with AsyncExitStack() as api_stack:
            with ExitStack() as sync_stack:
                api_connections = [
                    await api_stack.enter_async_context(api.connect()) for _ in range(5)
                ]
                background = [sync_stack.enter_context(scheduled.connect()) for _ in range(5)]
                background.append(sync_stack.enter_context(observed.connect()))
                pids = {await c.scalar(text("SELECT pg_backend_pid()")) for c in api_connections}
                pids.update(c.scalar(text("SELECT pg_backend_pid()")) for c in background)
                assert len(pids) == 11
                assert 15 - len(pids) == 4

                # The old overflow created a sixth API connection immediately.
                # Cancellation must not consume a slot or poison the next request.
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(api.connect(), timeout=0.1)
                released_pid = await api_connections[-1].scalar(text("SELECT pg_backend_pid()"))
                await api_connections[-1].close()
                async with api.connect() as recovered:
                    assert await recovered.scalar(text("SELECT pg_backend_pid()")) == released_pid
                    assert await recovered.scalar(text("SELECT 1")) == 1
    finally:
        await api.dispose()


@pytest.mark.parametrize("component,size", [("scheduler", 5), ("monitor", 1)])
def test_background_checkout_waits_then_reuses_returned_connection(pool_engines, component, size):
    _, scheduled, observed = pool_engines
    engine = scheduled if component == "scheduler" else observed
    connections = [engine.connect() for _ in range(size)]
    released_pid = connections[-1].scalar(text("SELECT pg_backend_pid()"))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiter = executor.submit(engine.connect)
            try:
                with pytest.raises(FutureTimeout):
                    waiter.result(timeout=0.1)
            finally:
                # Always unblock the worker, including when the regression fires.
                connections[-1].close()
                recovered = waiter.result(timeout=5)
                try:
                    assert recovered.scalar(text("SELECT pg_backend_pid()")) == released_pid
                    assert recovered.scalar(text("SELECT 1")) == 1
                finally:
                    recovered.close()
    finally:
        for connection in connections:
            connection.close()
