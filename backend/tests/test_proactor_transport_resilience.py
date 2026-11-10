import asyncio
import sys
import pytest
from execution.browser.playwright_executor import _ProactorWorker


@pytest.mark.asyncio
async def test_proactor_worker_cancellation_shielding():
    # Verify that cancelling the caller of _ProactorWorker.run() does NOT
    # leave the background task or pipe transport in an interrupted/corrupted state
    worker = _ProactorWorker.get()
    
    background_executed = False

    async def sample_background_coro():
        nonlocal background_executed
        await asyncio.sleep(0.1)
        background_executed = True
        return "completed_safely"

    # Schedule run in a task and cancel it immediately
    task = asyncio.create_task(worker.run(sample_background_coro()))
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Give background loop a moment to finish its shielded execution
    await asyncio.sleep(0.15)
    # The background coroutine finished safely despite caller cancellation
    assert background_executed is True


@pytest.mark.asyncio
async def test_proactor_worker_sequential_dispatch():
    # Verify multiple concurrent calls are handled safely by the worker
    worker = _ProactorWorker.get()

    async def quick_coro(val):
        await asyncio.sleep(0.01)
        return val * 2

    results = await asyncio.gather(
        worker.run(quick_coro(1)),
        worker.run(quick_coro(2)),
        worker.run(quick_coro(3)),
    )
    assert results == [2, 4, 6]

