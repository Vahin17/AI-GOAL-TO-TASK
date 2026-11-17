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


@pytest.mark.asyncio
async def test_proactor_worker_cancellation_hang_marks_unhealthy_and_recovers(monkeypatch):
    import execution.browser.playwright_executor as pe
    monkeypatch.setattr(pe, "PLAYWRIGHT_TEARDOWN_TIMEOUT_SECONDS", 0.1)

    # Ensure clean worker
    pe._ProactorWorker.reset_instance()
    worker = pe._ProactorWorker.get()
    assert worker.is_healthy is True

    hang_event = asyncio.Event()

    async def hanging_coro():
        # Never finishes, simulating hung CDP/pipe transport
        await hang_event.wait()
        return "never"

    task = asyncio.create_task(worker.run(hanging_coro()))
    await asyncio.sleep(0.02)
    task.cancel()

    # 1. Cancellation propagates to caller within bounded timeout
    with pytest.raises(asyncio.CancelledError):
        await task

    # 2. Worker is marked unhealthy and diagnostics recorded
    assert worker.is_healthy is False
    assert worker.timeout_count >= 1
    assert "last_cancellation_error" in worker.diagnostics

    # 3. New operations are rejected on unhealthy worker
    dummy_coro = asyncio.sleep(0.01)
    try:
        with pytest.raises(RuntimeError, match="unhealthy"):
            await worker.run(dummy_coro)
    finally:
        dummy_coro.close()
        hang_event.set()

    # 4. Replacement worker recovers after reset_instance()
    pe._ProactorWorker.reset_instance()
    fresh_worker = pe._ProactorWorker.get()
    assert fresh_worker.is_healthy is True
    res = await fresh_worker.run(asyncio.sleep(0.01, result="recovered"))
    assert res == "recovered"


@pytest.mark.asyncio
async def test_playwright_executor_repeated_stop_is_safe():
    from execution.browser.playwright_executor import PlaywrightBrowserExecutor
    executor = PlaywrightBrowserExecutor()
    # Repeated calls before or after start must be safe and idempotent
    await executor.stop()
    await executor.stop()
    assert executor.is_running is False


