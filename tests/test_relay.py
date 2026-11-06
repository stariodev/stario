"""Tests for stario.relay - In-process pub/sub."""

import asyncio
import queue
import threading
import time

import pytest

from stario.exceptions import StarioError, StarioRuntime
from stario.relay import Relay


async def _collect_one(
    relay: Relay, pattern: str, subject: str, data: object = None
) -> None:
    """Start a single-shot subscriber; publish once; wait until it receives or times out."""
    received: list[tuple[str, object]] = []
    done = asyncio.Event()
    ready = asyncio.Event()

    async def subscriber() -> None:
        async with relay.subscribe(pattern) as sub:
            ready.set()
            s, d = await sub.receive()
            received.append((s, d))
            done.set()

    task = asyncio.create_task(subscriber())
    await ready.wait()
    relay.publish(subject, data)
    await asyncio.wait_for(done.wait(), timeout=1.0)
    await task
    assert received == [(subject, data)]


class TestRelayBasic:
    def test_subscribe_requires_at_least_one_pattern(self) -> None:
        relay = Relay()
        with pytest.raises(StarioError, match="at least one pattern"):
            relay.subscribe()

    def test_subscribe_rejects_invalid_patterns(self) -> None:
        relay = Relay()
        with pytest.raises(StarioError, match="non-empty"):
            relay.subscribe("")
        with pytest.raises(StarioError, match="trailing"):
            relay.subscribe("room.*.message")
        with pytest.raises(StarioError, match="trailing"):
            relay.subscribe("room.*.*")
        with pytest.raises(StarioError, match="trailing"):
            relay.subscribe("room*")
        with pytest.raises(StarioError, match="trailing"):
            relay.subscribe(".room")
        with pytest.raises(StarioError, match="trailing"):
            relay.subscribe("room.")
        with pytest.raises(StarioError, match="trailing"):
            relay.subscribe("room..message")
        with pytest.raises(StarioError, match="trailing"):
            relay.subscribe(".*")

    def test_publish_requires_exact_subject(self) -> None:
        relay = Relay()
        with pytest.raises(StarioError, match="non-empty"):
            relay.publish("", None)
        with pytest.raises(StarioError, match="exact"):
            relay.publish("*", None)
        with pytest.raises(StarioError, match="exact"):
            relay.publish("room.*", None)
        with pytest.raises(StarioError, match="exact"):
            relay.publish("room.*.message", None)
        with pytest.raises(StarioError, match="segments"):
            relay.publish(".room", None)
        with pytest.raises(StarioError, match="segments"):
            relay.publish("room.", None)
        with pytest.raises(StarioError, match="segments"):
            relay.publish("room..message", None)

    async def test_subscribe_receive(self) -> None:
        relay = Relay()
        received: list[tuple[str, dict[str, int]]] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("test.*") as sub:
                ready.set()
                async for subject, data in sub:
                    received.append((subject, data))
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("test.one", {"msg": 1})
        relay.publish("test.two", {"msg": 2})
        await asyncio.wait_for(task, timeout=1.0)

        assert received == [("test.one", {"msg": 1}), ("test.two", {"msg": 2})]

    async def test_unbounded_subscription_keeps_pending_messages(self) -> None:
        relay = Relay[int]()
        received: list[int] = []

        async with relay.subscribe("tick") as sub:
            relay.publish("tick", 1)
            relay.publish("tick", 2)

            async for _subject, data in sub:
                received.append(data)
                if len(received) >= 2:
                    break

        assert received == [1, 2]


class TestRelayBoundedInbox:
    def test_maxsize_must_be_at_least_one(self) -> None:
        relay = Relay()
        with pytest.raises(StarioError, match="at least 1"):
            relay.subscribe("n", maxsize=0)

    async def test_maxsize_keeps_newest_in_order(self) -> None:
        relay = Relay[int]()
        async with relay.subscribe("n", maxsize=3) as sub:
            for i in range(5):
                relay.publish("n", i)
            received = [await sub.receive() for _ in range(3)]
            assert received == [("n", 2), ("n", 3), ("n", 4)]
            assert sub.dropped == 2

    async def test_maxsize_still_hands_off_to_blocked_receive(self) -> None:
        relay = Relay[int]()
        async with relay.subscribe("n", maxsize=1) as sub:
            waiter = asyncio.ensure_future(sub.receive())
            await asyncio.sleep(0)
            relay.publish("n", 1)
            relay.publish("n", 2)
            assert (await waiter) == ("n", 1)
            # The second publish buffered; the first was already handed off.
            assert await sub.receive() == ("n", 2)
            assert sub.dropped == 0

    async def test_latest_keeps_newest_per_subject(self) -> None:
        relay = Relay[int]()
        async with relay.subscribe("room.*", latest=True) as sub:
            relay.publish("room.1", 1)
            relay.publish("room.1", 2)
            relay.publish("room.2", 3)
            relay.publish("room.1", 4)
            assert sub.dropped == 2  # two stale room.1 values replaced
            # room.2 arrived before the last room.1 replacement moved to end.
            assert await sub.receive() == ("room.2", 3)
            assert await sub.receive() == ("room.1", 4)

    async def test_latest_with_maxsize_caps_distinct_subjects(self) -> None:
        relay = Relay[int]()
        async with relay.subscribe("room.*", maxsize=2, latest=True) as sub:
            relay.publish("room.1", 1)
            relay.publish("room.2", 2)
            relay.publish("room.3", 3)
            assert sub.dropped == 1  # room.1 evicted, not replaced
            assert await sub.receive() == ("room.2", 2)
            assert await sub.receive() == ("room.3", 3)
            # A re-publish of a dropped subject is a fresh buffer entry.
            relay.publish("room.1", 4)
            assert sub.dropped == 1
            assert await sub.receive() == ("room.1", 4)

    async def test_latest_hand_off_to_blocked_receive(self) -> None:
        relay = Relay[int]()
        async with relay.subscribe("n", latest=True) as sub:
            waiter = asyncio.ensure_future(sub.receive())
            await asyncio.sleep(0)
            relay.publish("n", 9)
            assert (await waiter) == ("n", 9)
            assert sub.dropped == 0

    async def test_maxsize_applies_to_cross_thread_enqueue(self) -> None:
        relay = Relay[int]()

        async with relay.subscribe("n", maxsize=3) as sub:
            thread = threading.Thread(
                target=lambda: [relay.publish("n", i) for i in range(8)]
            )
            thread.start()
            thread.join(timeout=1.0)
            assert sub.dropped == 5
            received = [await sub.receive() for _ in range(3)]
            assert received == [("n", 5), ("n", 6), ("n", 7)]

    async def test_latest_applies_to_cross_thread_enqueue(self) -> None:
        relay = Relay[int]()
        async with relay.subscribe("n", latest=True) as sub:
            thread = threading.Thread(
                target=lambda: [relay.publish("n", i) for i in range(8)]
            )
            thread.start()
            thread.join(timeout=1.0)
            assert sub.dropped == 7
            assert await sub.receive() == ("n", 7)


class TestRelayPatterns:
    async def test_exact_match(self) -> None:
        relay = Relay()
        received: list[str] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("exact.match") as sub:
                ready.set()
                subject, _data = await sub.receive()
                received.append(subject)

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("exact.match", None)
        relay.publish("exact.other", None)
        await asyncio.wait_for(task, timeout=1.0)

        assert received == ["exact.match"]

    async def test_catchall_pattern(self) -> None:
        relay = Relay()
        received: list[str] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("room.123.*") as sub:
                ready.set()
                async for subject, _data in sub:
                    received.append(subject)
                    if len(received) >= 3:
                        break

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("room.123.moves", None)
        relay.publish("room.123.chat", None)
        relay.publish("room.123.leave", None)
        relay.publish("room.456.moves", None)
        await asyncio.wait_for(task, timeout=1.0)

        assert received == ["room.123.moves", "room.123.chat", "room.123.leave"]

    async def test_global_wildcard(self) -> None:
        relay = Relay()
        received: list[str] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("*") as sub:
                ready.set()
                async for subject, _data in sub:
                    received.append(subject)
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("anything.here", None)
        relay.publish("something.else", None)
        await asyncio.wait_for(task, timeout=1.0)

        assert received == ["anything.here", "something.else"]


class TestRelaySubscribeMultiplePatterns:
    async def test_one_delivery_when_patterns_overlap(self) -> None:
        relay = Relay()
        received: list[tuple[str, int]] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("*", "users.*") as sub:
                ready.set()
                async for subject, data in sub:
                    received.append((subject, data))
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("users.a.b", 1)
        relay.publish("other.x", 2)
        await asyncio.wait_for(task, timeout=1.0)
        assert received == [("users.a.b", 1), ("other.x", 2)]

    async def test_redundant_exact_pattern_deduped(self) -> None:
        relay = Relay()
        received: list[tuple[str, int]] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("users.*", "users.alice") as sub:
                ready.set()
                subject, data = await sub.receive()
                received.append((subject, data))

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("users.alice", 1)
        await asyncio.wait_for(task, timeout=1.0)
        assert received == [("users.alice", 1)]

    async def test_nested_wildcard_chain_delivers_once(self) -> None:
        relay = Relay[int]()
        received: list[tuple[str, int]] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe(
                "users.alice.message",
                "users.alice.*",
                "users.*",
            ) as sub:
                ready.set()
                async for msg in sub:
                    received.append(msg)
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("users.alice.message", 1)
        relay.publish("users.bob.message", 2)
        await asyncio.wait_for(task, timeout=1.0)

        assert received == [("users.alice.message", 1), ("users.bob.message", 2)]

    async def test_exact_topic_and_child_wildcard_are_independent(self) -> None:
        relay = Relay[int]()
        received: list[tuple[str, int]] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("users.alice", "users.alice.*") as sub:
                ready.set()
                async for msg in sub:
                    received.append(msg)
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("users.alice", 1)
        relay.publish("users.alice.message", 2)
        await asyncio.wait_for(task, timeout=1.0)

        assert received == [("users.alice", 1), ("users.alice.message", 2)]

    async def test_independent_exact_patterns_both_receive(self) -> None:
        relay = Relay()
        received: list[tuple[str, int]] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("users.a", "users.b") as sub:
                ready.set()
                async for subject, data in sub:
                    received.append((subject, data))
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("users.a", 1)
        relay.publish("users.b", 2)
        await asyncio.wait_for(task, timeout=1.0)
        assert received == [("users.a", 1), ("users.b", 2)]

    async def test_unsubscribe_removes_all_patterns(self) -> None:
        relay = Relay()

        async def subscriber() -> None:
            async with relay.subscribe("chat.*", "room.*") as sub:
                relay.publish("chat.msg", None)
                async for _ in sub:
                    break

        await asyncio.wait_for(asyncio.create_task(subscriber()), timeout=1.0)
        await _collect_one(relay, "chat.*", "chat.after", "ok")
        await _collect_one(relay, "room.*", "room.after", "ok")


class TestRelayCleanup:
    async def test_bare_async_for_requires_async_with(self) -> None:
        relay = Relay()

        with pytest.raises(StarioRuntime, match="async with"):
            async for _ in relay.subscribe("cleanup.test"):
                pass

        await _collect_one(relay, "cleanup.test", "cleanup.test", "ok")

    async def test_unsubscribe_after_finite_async_with(self) -> None:
        relay = Relay()
        ready = asyncio.Event()

        async def first() -> None:
            async with relay.subscribe("cleanup.test") as sub:
                ready.set()
                async for _ in sub:
                    break

        task = asyncio.create_task(first())
        await ready.wait()
        relay.publish("cleanup.test", None)
        await asyncio.wait_for(task, timeout=1.0)

        await _collect_one(relay, "cleanup.test", "cleanup.test", "only-one")

    async def test_reused_subscription_does_not_keep_old_pending_messages(self) -> None:
        relay = Relay[str]()
        sub = relay.subscribe("cleanup.test")

        async with sub:
            relay.publish("cleanup.test", "stale")

        async with sub as live:
            relay.publish("cleanup.test", "fresh")
            _subject, data = await live.receive()
            assert data == "fresh"


class TestRelayMultipleSubscribers:
    async def test_multiple_subscribers_same_pattern(self) -> None:
        relay = Relay()
        received1: list[str] = []
        received2: list[str] = []

        ready1 = asyncio.Event()
        ready2 = asyncio.Event()

        async def sub1() -> None:
            async with relay.subscribe("shared.*") as sub:
                ready1.set()
                subject, _data = await sub.receive()
                received1.append(subject)

        async def sub2() -> None:
            async with relay.subscribe("shared.*") as sub:
                ready2.set()
                subject, _data = await sub.receive()
                received2.append(subject)

        task1 = asyncio.create_task(sub1())
        task2 = asyncio.create_task(sub2())
        await ready1.wait()
        await ready2.wait()
        relay.publish("shared.message", None)
        await asyncio.wait_for(asyncio.gather(task1, task2), timeout=1.0)

        assert received1 == ["shared.message"]
        assert received2 == ["shared.message"]


class TestRelayRaceConditions:
    async def test_concurrent_unsubscribe(self) -> None:
        relay = Relay()
        unsubscribed = 0
        cleanup_events = [asyncio.Event() for _ in range(5)]
        ready = [asyncio.Event() for _ in range(5)]

        async def quick_subscriber(idx: int) -> None:
            nonlocal unsubscribed
            try:
                async with relay.subscribe("concurrent.*") as sub:
                    ready[idx].set()
                    await sub.receive()
            finally:
                await asyncio.sleep(0)
                unsubscribed += 1
                cleanup_events[idx].set()

        tasks = [asyncio.create_task(quick_subscriber(i)) for i in range(5)]
        await asyncio.gather(*[e.wait() for e in ready])
        relay.publish("concurrent.msg", None)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
        await asyncio.wait_for(
            asyncio.gather(*[e.wait() for e in cleanup_events]), timeout=1.0
        )

        assert unsubscribed == 5
        await _collect_one(relay, "concurrent.*", "concurrent.after", "ok")

    async def test_mass_unsubscribe_is_fast_and_keeps_order(self) -> None:
        relay = Relay[int]()
        subs = [relay.subscribe("bulk.*") for _ in range(2000)]
        for sub in subs:
            await sub.__aenter__()
        kept = subs[::10]

        started = time.perf_counter()
        for index, sub in enumerate(subs):
            if index % 10:
                relay.drop_subscription(sub)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.5
        assert list(relay.subs["bulk.*"]) == kept
        for sub in kept:
            relay.drop_subscription(sub)
        assert "bulk.*" not in relay.subs


class TestRelaySubscriptionOrdering:
    async def test_events_after_enter_not_missed(self) -> None:
        relay = Relay[str]()
        received: list[str] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("room.*") as sub:
                relay.publish("room.a", "during-setup")
                ready.set()
                async for _subject, data in sub:
                    received.append(data)
                    if len(received) >= 2:
                        break

        task = asyncio.create_task(subscriber())
        await ready.wait()
        relay.publish("room.b", "after-loop-start")
        await asyncio.wait_for(task, timeout=1.0)

        assert received == ["during-setup", "after-loop-start"]


class TestRelayThreadSafety:
    async def test_publish_from_worker_thread_reaches_subscriber(self) -> None:
        relay = Relay[dict[str, int]]()
        received: list[tuple[str, dict[str, int]]] = []

        ready = asyncio.Event()

        async def subscriber() -> None:
            async with relay.subscribe("room.*") as sub:
                ready.set()
                async for subject, data in sub:
                    received.append((subject, data))
                    break

        task = asyncio.create_task(subscriber())
        await ready.wait()

        thread = threading.Thread(
            target=lambda: relay.publish("room.1", {"x": 1}),
        )
        thread.start()
        thread.join(timeout=1.0)

        await asyncio.wait_for(task, timeout=1.0)
        assert received == [("room.1", {"x": 1})]

    async def test_worker_thread_burst_is_ordered_and_coalesces_wakes(self) -> None:
        """1000 cross-thread publishes arrive in order with far fewer loop hops."""
        relay = Relay[int]()
        loop = asyncio.get_running_loop()
        hops = 0
        real = loop.call_soon_threadsafe

        def counting(callback, *args, **kwargs):
            nonlocal hops
            hops += 1
            return real(callback, *args, **kwargs)

        loop.call_soon_threadsafe = counting
        ready = asyncio.Event()
        received: list[int] = []
        count = 1000
        try:

            async def subscriber() -> None:
                async with relay.subscribe("n") as sub:
                    ready.set()
                    async for _subject, value in sub:
                        received.append(value)
                        if len(received) == count:
                            return

            task = asyncio.create_task(subscriber())
            await ready.wait()
            thread = threading.Thread(
                target=lambda: [relay.publish("n", i) for i in range(count)]
            )
            thread.start()
            await asyncio.wait_for(task, timeout=5.0)
            thread.join(timeout=1.0)
        finally:
            del loop.call_soon_threadsafe
        assert received == list(range(count))
        # One hop per burst while receive() is blocked, none while it keeps up.
        assert hops < count // 2

    async def test_publish_fans_out_to_subscribers_on_multiple_loops(self) -> None:
        relay = Relay[str]()
        ready = threading.Event()
        results: queue.Queue[tuple[str, str] | BaseException] = queue.Queue()

        def run_background_subscriber() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def consume() -> None:
                async with relay.subscribe("room.*") as sub:
                    ready.set()
                    results.put(await sub.receive())

            try:
                loop.run_until_complete(consume())
            except BaseException as exc:
                results.put(exc)
            finally:
                loop.close()

        async def consume_one(sub) -> tuple[str, str]:
            async for msg in sub:
                return msg
            raise AssertionError("subscription ended without a message")

        main_result: tuple[str, str] | None = None
        background_result = None

        async with relay.subscribe("room.*") as main_sub:
            thread = threading.Thread(target=run_background_subscriber)
            thread.start()
            assert ready.wait(timeout=1.0)

            main_task = asyncio.create_task(consume_one(main_sub))
            relay.publish("room.1", "x")

            main_result = await asyncio.wait_for(main_task, timeout=1.0)
            background_result = results.get(timeout=1.0)
            thread.join(timeout=1.0)

        assert main_result == ("room.1", "x")
        assert background_result == ("room.1", "x")

    async def test_active_subscription_must_be_consumed_on_owning_loop(self) -> None:
        relay = Relay[int]()
        results = queue.Queue()

        result = None

        async with relay.subscribe("room.*") as sub:
            relay.publish("room.1", 1)

            def consume_from_other_loop() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                async def consume() -> None:
                    async for _ in sub:
                        pass

                try:
                    loop.run_until_complete(consume())
                except BaseException as exc:
                    results.put(exc)
                finally:
                    loop.close()

            thread = threading.Thread(target=consume_from_other_loop)
            thread.start()
            result = results.get(timeout=1.0)
            thread.join(timeout=1.0)

        assert isinstance(result, StarioRuntime)
        assert "owning loop" in str(result)


class TestRelayFailureModes:
    async def test_double_register_raises(self) -> None:
        relay = Relay()
        sub = relay.subscribe("a.b")
        async with sub:
            with pytest.raises(StarioRuntime, match="already active"):
                async with sub:
                    pass

        await _collect_one(relay, "a.b", "a.b", "reuse-ok")

    async def test_publish_after_subscriber_loop_closed_is_swallowed(self) -> None:
        relay = Relay()

        def run_and_abandon() -> None:
            other_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(other_loop)
            entered = other_loop.create_future()

            async def register_only() -> None:
                async with relay.subscribe("zombie.topic"):
                    entered.set_result(None)
                    await asyncio.Event().wait()

            task = other_loop.create_task(register_only())
            other_loop.run_until_complete(entered)
            task.cancel()
            other_loop.close()

        thread = threading.Thread(target=run_and_abandon)
        thread.start()
        thread.join(timeout=1.0)

        relay.publish("zombie.topic", "lost")
        await _collect_one(relay, "zombie.topic", "zombie.topic", "fresh")

    async def test_cancelled_subscriber_unregisters(self) -> None:
        relay = Relay()
        started = asyncio.Event()

        async def consume() -> None:
            async with relay.subscribe("jobs.*") as sub:
                started.set()
                async for _ in sub:
                    pass

        task = asyncio.create_task(consume())
        await started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _collect_one(relay, "jobs.*", "jobs.after", "ok")

    async def test_receive_requires_active_subscription(self) -> None:
        relay = Relay()
        sub = relay.subscribe("feed.*")

        with pytest.raises(StarioRuntime, match="not active"):
            await sub.receive()

        await _collect_one(relay, "feed.*", "feed.after", "ok")

    async def test_cancelled_infinite_async_with_unregisters(self) -> None:
        relay = Relay()
        started = asyncio.Event()

        async def consume() -> None:
            async with relay.subscribe("live.*") as sub:
                started.set()
                async for _ in sub:
                    pass

        task = asyncio.create_task(consume())
        await started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _collect_one(relay, "live.*", "live.after", "ok")
