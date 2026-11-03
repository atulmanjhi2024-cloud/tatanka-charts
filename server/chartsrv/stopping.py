"""The background loops' waits between rounds (the quote feeds' polls, the daily refreshes, the watchdog): a stop that a
request swallowed ends the loop there.

httpx connects through anyio, and anyio's connect_tcp (a task group: the first address to connect cancels the other
attempts) takes a stop that lands in the same loop pass as its connection (Market.stop's task.cancel(): a restart in
place, the container stopping, a connection saved) for its own cancel, and swallows it (anyio 4.15). The request goes
on as if nothing happened: it answers, or fails and the loop's `except` takes the error. The task is left marked as
cancelling with nothing left to raise it: the loop slept out its wait and went on polling, and the stop waited for
it for good (a connection saved: the old feed polled on beside the one that replaced it)."""
import asyncio


def check():
    """Raises CancelledError if this task was asked to stop but runs on: a stop something swallowed."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError


async def pause(seconds: float):
    """asyncio.sleep(seconds), unless this task was asked to stop meanwhile."""
    check()
    await asyncio.sleep(seconds)
