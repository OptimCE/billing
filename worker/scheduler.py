"""A once-a-day tick for the overdue sweep.

Fires at a fixed LOCAL time rather than "every N hours from process start". A
from-start interval re-runs on every deploy, and the sweep is user-visible:
`invoice.overdue` is TRANSACTIONAL and reaches the member's inbox. Re-running is
harmless (an already-OVERDUE invoice matches nothing), so this is hygiene rather
than correctness — but it is the difference between one notice and one per
deploy.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from zoneinfo import ZoneInfo

from core.config import settings
from worker.sweeps import try_sweep_overdue

logger = logging.getLogger(__name__)

_SETTLEMENT_TZ = ZoneInfo("Europe/Brussels")


def seconds_until_next_run(now: datetime.datetime, hour_local: int) -> float:
    """Seconds from ``now`` to the next occurrence of ``hour_local`` in Brussels.

    Pure, so the wrap-around and DST cases are unit-testable without waiting a
    day. ``now`` must be timezone-aware.
    """
    local_now = now.astimezone(_SETTLEMENT_TZ)
    target = local_now.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    if target <= local_now:
        target += datetime.timedelta(days=1)
    return (target - local_now).total_seconds()


async def run_overdue_scheduler(shutdown: asyncio.Event) -> None:
    """Sleep until the next scheduled hour, sweep, repeat, until shutdown."""
    if not settings.OVERDUE_SWEEP_ENABLED:
        logger.info("overdue scheduler disabled")
        return
    while not shutdown.is_set():
        delay = seconds_until_next_run(
            datetime.datetime.now(datetime.UTC), settings.OVERDUE_SWEEP_HOUR_LOCAL
        )
        logger.info("next overdue sweep in %.0fs", delay)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=delay)
        if shutdown.is_set():
            return
        try:
            await try_sweep_overdue()
        except Exception:
            # One failed sweep must not take the worker down: the next tick
            # retries, and the sweep is idempotent by construction.
            logger.exception("overdue sweep failed")
