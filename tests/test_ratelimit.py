import asyncio
import time

import pytest

from backend.ratelimit import Limit, Priority, SlidingWindowLimiter, _Bucket

TOL = 0.03  # asyncio timers on Windows have ~15 ms resolution


# ------------------------------------------------------------ pure window math
def test_bucket_sliding_window_wait():
    b = _Bucket("x", Limit(3, 10.0), margin=0.0)
    for t in (0.0, 1.0, 2.0):
        assert b.wait_time(t, Priority.NORMAL) == 0
        b.record(t)
    assert b.wait_time(2.5, Priority.NORMAL) == pytest.approx(7.5)   # waits for the t=0 call to leave
    assert b.wait_time(10.0, Priority.NORMAL) == 0
    b.record(10.0)
    assert b.wait_time(10.5, Priority.NORMAL) == pytest.approx(0.5)  # t=1 leaves at 11


def test_bucket_safety_margin_extends_window():
    b = _Bucket("x", Limit(1, 30.0), margin=0.25)
    b.record(0.0)
    assert b.wait_time(29.0, Priority.NORMAL) == pytest.approx(1.25)


def test_bucket_min_interval():
    b = _Bucket("x", Limit(100, 30.0, min_interval_sec=0.04), margin=0.0)
    b.record(1.0)
    assert b.wait_time(1.01, Priority.ORDER) == pytest.approx(0.03)
    assert b.wait_time(1.05, Priority.ORDER) == 0


def test_reserve_keeps_capacity_for_orders():
    b = _Bucket("snap", Limit(5, 10.0, reserve_high=2), margin=0.0)
    for t in (0.0, 0.1, 0.2):
        b.record(t)
    assert b.wait_time(0.3, Priority.POLL) > 0        # polling may only use 3 of 5
    assert b.wait_time(0.3, Priority.ORDER) == 0      # orders still go out
    b.record(0.3)
    b.record(0.4)
    assert b.wait_time(0.5, Priority.ORDER) > 0       # now really full


def test_unlimited_is_counted():
    b = _Bucket("x", Limit(None, 30.0), margin=0.0)
    for i in range(50):
        assert b.wait_time(i * 0.01, Priority.NORMAL) == 0
        b.record(i * 0.01)
    assert b.used_in_window(1.0) == 50


def test_limit_validation():
    with pytest.raises(ValueError):
        Limit(0, 30)
    with pytest.raises(ValueError):
        Limit(5, 30, reserve_high=5)


def test_from_config_aliases_and_defaults():
    lim = SlidingWindowLimiter.from_config({
        "place_order": {"max_calls": 15, "window_sec": 30, "min_interval_sec": 0.02},
        "place_combo_order": {"bucket": "place_order"},
        "get_market_snapshot": {"max_calls": 60, "window_sec": 30, "max_codes": 400, "reserve_high": 6},
    }, {"safety_margin_sec": 0.25})
    assert lim.bucket_name("place_combo_order") == "place_order"
    usage = lim.usage()
    assert usage["place_order"]["max_calls"] == 15
    assert usage["place_order"]["shared_by"] == ["place_combo_order"]
    assert usage["get_market_snapshot"]["max_calls"] == 60


# ----------------------------------------------------------------- async queue
def test_never_exceeds_limit_in_any_window():
    async def main():
        lim = SlidingWindowLimiter({"ep": Limit(4, 0.5)})
        stamps = []

        async def one():
            await lim.acquire("ep")
            stamps.append(time.monotonic())

        t0 = time.monotonic()
        await asyncio.gather(*(one() for _ in range(12)))
        return t0, sorted(stamps)

    t0, stamps = asyncio.run(main())
    assert len(stamps) == 12
    for i in range(len(stamps) - 4):
        assert stamps[i + 4] - stamps[i] >= 0.5 - TOL, "5 calls inside one window"
    assert stamps[-1] - t0 >= 1.0 - TOL                 # 12 calls / 4 per 0.5 s => >= 2 full windows


def test_queued_request_waits_instead_of_failing():
    async def main():
        lim = SlidingWindowLimiter({"ep": Limit(2, 0.4)})
        t0 = time.monotonic()
        await lim.acquire("ep")
        await lim.acquire("ep")
        await lim.acquire("ep")       # must wait for the first call to leave the window
        return time.monotonic() - t0

    assert asyncio.run(main()) >= 0.4 - TOL


def test_priority_orders_overtake_polling():
    async def main():
        lim = SlidingWindowLimiter({"ep": Limit(1, 0.3)})
        await lim.acquire("ep")
        order: list[str] = []

        async def req(name, prio):
            await lim.acquire("ep", prio)
            order.append(name)

        a = asyncio.create_task(req("poll-1", Priority.POLL))
        b = asyncio.create_task(req("poll-2", Priority.POLL))
        await asyncio.sleep(0.05)
        c = asyncio.create_task(req("order", Priority.ORDER))
        await asyncio.gather(a, b, c)
        return order

    assert asyncio.run(main()) == ["order", "poll-1", "poll-2"]


def test_shared_bucket_budget():
    async def main():
        lim = SlidingWindowLimiter.from_config({
            "place_order": {"max_calls": 2, "window_sec": 0.4},
            "place_combo_order": {"bucket": "place_order"}})
        t0 = time.monotonic()
        await lim.acquire("place_order")
        await lim.acquire("place_combo_order")
        await lim.acquire("place_combo_order")   # third call in the shared bucket waits
        return time.monotonic() - t0, lim.usage()["place_order"]["total"]

    elapsed, total = asyncio.run(main())
    assert elapsed >= 0.4 - TOL
    assert total == 3


def test_cancelled_waiter_does_not_block_others():
    async def main():
        lim = SlidingWindowLimiter({"ep": Limit(1, 0.3)})
        await lim.acquire("ep")
        x = asyncio.create_task(lim.acquire("ep", Priority.ORDER))
        y = asyncio.create_task(lim.acquire("ep", Priority.POLL))
        await asyncio.sleep(0.05)
        x.cancel()
        t0 = time.monotonic()
        await y
        return time.monotonic() - t0, x.cancelled()

    waited, cancelled = asyncio.run(main())
    assert cancelled
    assert waited < 0.4


def test_usage_reports_queue_and_window():
    async def main():
        lim = SlidingWindowLimiter({"ep": Limit(1, 0.5)})
        await lim.acquire("ep")
        t = asyncio.create_task(lim.acquire("ep"))
        await asyncio.sleep(0.05)
        u = lim.usage()["ep"]
        await t
        return u

    u = asyncio.run(main())
    assert u["used"] == 1 and u["queued"] == 1 and u["max_calls"] == 1
