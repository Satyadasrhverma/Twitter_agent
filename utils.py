"""
Shared utility helpers.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List

from rich.console import Console
from rich.live import Live
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console()


def chunk_list(lst: list, n: int) -> List[list]:
    """Split *lst* into *n* roughly equal sub-lists."""
    if n <= 0:
        raise ValueError("n must be > 0")
    size = max(1, len(lst) // n)
    chunks = [lst[i : i + size] for i in range(0, len(lst), size)]
    # If rounding created more chunks than workers, merge tail into last chunk
    while len(chunks) > n:
        chunks[-2].extend(chunks[-1])
        chunks.pop()
    return chunks


def format_uptime(started_at: datetime) -> str:
    """Return a human-readable uptime string from *started_at* (UTC)."""
    delta = datetime.now(timezone.utc) - started_at
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"


def build_status_table(scheduler: object) -> Table:
    """
    Build a Rich Table showing live monitoring stats.
    *scheduler* is typed as object to avoid circular import.
    """
    table = Table(title="X Monitor — Live Status", expand=False)
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="green")

    table.add_row("Uptime", format_uptime(scheduler.started_at))
    table.add_row("Users Monitored", str(len(scheduler._users)))  # type: ignore[attr-defined]
    table.add_row("Checks Completed", str(scheduler.checked_count))  # type: ignore[attr-defined]
    table.add_row("New Posts Detected", str(scheduler.new_posts_count))  # type: ignore[attr-defined]
    table.add_row("Errors", str(scheduler.error_count))  # type: ignore[attr-defined]
    table.add_row("Workers", str(len(scheduler._tasks)))  # type: ignore[attr-defined]

    return table


async def status_display_loop(scheduler: object, refresh_interval: float = 5.0) -> None:
    """Continuously re-render the status table in the terminal."""
    with Live(build_status_table(scheduler), refresh_per_second=1, console=console) as live:
        while True:
            await asyncio.sleep(refresh_interval)
            live.update(build_status_table(scheduler))
