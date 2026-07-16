"""Reconnect + startup-retry coverage for the NATS plumbing.

The actual reconnection behaviour lives inside the nats-py client and
can only be verified end-to-end with a real broker. What we *can* lock
in unit tests is:

* the surface we expose to the broker (the connect kwargs),
* the surface we expose to the worker (the startup retry loop),
* the publish-timeout so the API path cannot hang on a stalled broker.

Everything else (actual reconnection, durable consumer recovery) is
covered manually by the smoke test in the production roadmap's
verification section.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# init_nats — reconnect kwargs
# ---------------------------------------------------------------------------


async def test_init_nats_passes_reconnect_options(monkeypatch):
    from core.queue import init as nats_init

    mock_jetstream = MagicMock()
    mock_jetstream.add_stream = AsyncMock()
    mock_client = MagicMock()
    mock_client.jetstream = MagicMock(return_value=mock_jetstream)
    mock_client.drain = AsyncMock()
    mock_connect = AsyncMock(return_value=mock_client)

    monkeypatch.setattr(nats_init.nats, "connect", mock_connect)
    # Anchor the module globals to monkeypatch so init_nats's writes get
    # reverted at end of test — otherwise a half-mocked client would leak
    # to whichever test runs next.
    monkeypatch.setattr(nats_init, "_nats_client", nats_init._nats_client)
    monkeypatch.setattr(nats_init, "_jetstream", nats_init._jetstream)

    await nats_init.init_nats()

    mock_connect.assert_awaited_once()
    kwargs = mock_connect.await_args.kwargs
    assert kwargs["max_reconnect_attempts"] == -1
    assert kwargs["reconnect_time_wait"] == 2
    assert kwargs["connect_timeout"] == 5
    for cb in ("error_cb", "disconnected_cb", "reconnected_cb", "closed_cb"):
        assert callable(kwargs[cb]), f"{cb} must be wired to a coroutine"


# ---------------------------------------------------------------------------
# send_event — publish timeout
# ---------------------------------------------------------------------------


async def test_send_event_passes_publish_timeout():
    """Publish must use an explicit timeout so the API can't hang on a
    stalled broker. The default is 5 s; callers may override."""
    from core.queue.helper import Event, send_event

    mock_js = MagicMock()
    ack = MagicMock(stream="S", seq=1)
    mock_js.publish = AsyncMock(return_value=ack)

    await send_event(mock_js, "subject.test", Event(type="t", data={}))

    mock_js.publish.assert_awaited_once()
    kwargs = mock_js.publish.await_args.kwargs
    assert kwargs["timeout"] == 5.0


async def test_send_event_respects_caller_timeout():
    from core.queue.helper import Event, send_event

    mock_js = MagicMock()
    ack = MagicMock(stream="S", seq=1)
    mock_js.publish = AsyncMock(return_value=ack)

    await send_event(mock_js, "subject.test", Event(type="t", data={}), timeout=1.0)

    assert mock_js.publish.await_args.kwargs["timeout"] == 1.0


# ---------------------------------------------------------------------------
# Worker startup retry
# ---------------------------------------------------------------------------


async def test_connect_nats_with_retry_succeeds_after_two_failures(monkeypatch):
    """Two transient connect failures, then success. The retry loop must
    absorb the failures and return cleanly."""
    from worker import main as worker_main

    # Drop the backoff so the test doesn't actually sleep through retries.
    monkeypatch.setattr(worker_main, "_NATS_CONNECT_BASE_DELAY_SECONDS", 0)

    call_count = 0

    async def fake_init_nats():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("nats down")

    monkeypatch.setattr(worker_main, "init_nats", fake_init_nats)

    await worker_main._connect_nats_with_retry()

    assert call_count == 3


async def test_connect_nats_with_retry_gives_up_after_max_attempts(monkeypatch):
    from worker import main as worker_main

    monkeypatch.setattr(worker_main, "_NATS_CONNECT_BASE_DELAY_SECONDS", 0)

    call_count = 0

    async def always_fails():
        nonlocal call_count
        call_count += 1
        raise ConnectionError("nats permanently down")

    monkeypatch.setattr(worker_main, "init_nats", always_fails)

    with pytest.raises(ConnectionError):
        await worker_main._connect_nats_with_retry()

    assert call_count == worker_main._NATS_CONNECT_MAX_ATTEMPTS


async def test_connect_nats_with_retry_returns_immediately_on_success(monkeypatch):
    from worker import main as worker_main

    call_count = 0

    async def succeed():
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(worker_main, "init_nats", succeed)

    await worker_main._connect_nats_with_retry()

    assert call_count == 1
