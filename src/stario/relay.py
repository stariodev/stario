"""Tiny process-local pub/sub for dotted subjects and prefix wildcards.

- `publish` is synchronous and thread-safe — safe from worker threads, asyncio
  tasks, or sync code on the loop thread
- subscriptions bind to the asyncio loop that registered them; consume only there
- not a broker: no cross-process delivery, durability, replay, or global ordering
- use NATS, Redis, or another broker when you need multiprocess fanout or
  backpressure policy

**Subjects** (publish): exact, non-empty dotted strings — no wildcards.

**Patterns** (subscribe): exact subject, trailing `prefix.*`, or `*`.

**Threading**: `Relay.lock` protects the subscriber registry. Each subscription
has an `_inbox_lock` guarding its pending queue and waiter future so a worker
thread can `publish` while a handler awaits the next message on the owning loop.
A cross-loop publish appends under that lock and schedules one
`loop.call_soon_threadsafe` wake per burst, only while a `receive()` is blocked.

`RelaySubscription` is an opaque handle — enter with `async with`, then
consume with `receive()` or `async for`.
"""

from __future__ import annotations

from asyncio import AbstractEventLoop, Future, get_running_loop
from collections import deque
from collections.abc import AsyncIterator
from threading import Lock
from types import TracebackType
from typing import Self, cast

from stario.exceptions import StarioError, StarioRuntime

# Message: (subject, data)
type Msg[T] = tuple[str, T]


def _pattern_covers(broad: str, narrow: str) -> bool:
    # True if every subject matched by `narrow` is also matched by `broad`.
    # Used to collapse overlapping subscribe patterns into a minimal set.
    if broad == "*":
        return True
    if broad.endswith(".*"):
        # Compare against the "prefix." boundary (keep the trailing dot) so
        # "ab.*" does not spuriously cover "abc.x".
        return narrow.startswith(broad[:-1])
    return broad == narrow


def _valid_subject(value: str) -> bool:
    # Exact dotted subject: non-empty, no wildcards, no leading/trailing dots,
    # no empty segments.
    if not value or "*" in value:
        return False
    if value[0] == "." or value[-1] == ".":
        return False
    return ".." not in value


# Subjects up to this long keep their lookup keys in `Relay._keys`, at most
# `_KEYS_CACHE_SIZE` of them; a longer one is rare and recomputed each time.
_KEYS_CACHE_CHARS = 256
_KEYS_CACHE_SIZE = 1024


def _matching_patterns(subject: str, prefixes: tuple[int, ...]) -> tuple[str, ...]:
    """Registry keys that can match an exact subject: exact, `prefix.*`, then `*`.

    `prefixes` holds the lengths of the registered `prefix.` parts, longest
    first, so a subject costs one slice per registered prefix length rather
    than one per dot: a request-derived subject such as `room.a.a.a...`
    (thousands of dots) used to build every prefix of itself.
    """
    out = [subject]
    size = len(subject)
    for length in prefixes:
        if length < size and subject[length - 1] == ".":
            out.append(subject[:length] + "*")
    out.append("*")
    return tuple(out)


def _deduplicate_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """Validate and merge overlapping patterns."""
    kept: list[str] = []
    for pattern in dict.fromkeys(patterns):
        if not pattern:
            raise StarioError("subscription patterns must be non-empty")
        # Accept only "*", a trailing "prefix.*", or an exact dotted subject.
        valid = (
            pattern == "*"
            or (
                pattern.endswith(".*")
                and pattern.count("*") == 1
                and _valid_subject(pattern[:-2])
            )
            or _valid_subject(pattern)
        )
        if not valid:
            raise StarioError(
                "patterns must be exact dot-separated subjects, '*', or "
                "trailing 'prefix.*'",
            )
        # Drop this pattern if a kept one already covers it; otherwise drop any
        # kept patterns it subsumes. The result is a minimal, non-overlapping set,
        # so no single subject can match two of a subscription's patterns — that is
        # what makes delivery at-most-once without de-duplicating at publish time.
        if any(_pattern_covers(existing, pattern) for existing in kept):
            continue
        kept = [existing for existing in kept if not _pattern_covers(pattern, existing)]
        kept.append(pattern)
    return tuple(kept)


class RelaySubscription[T]:
    """Opaque `Relay.subscribe` handle — enter before receiving messages."""

    __slots__ = (
        "_inbox_lock",
        "_items",
        "_latest",
        "_maxsize",
        "_relay",
        "_waiter",
        "_wake_pending",
        "active",
        "dropped",
        "generation",
        "loop",
        "patterns",
    )

    def __init__(
        self,
        relay: Relay[T],
        patterns: tuple[str, ...],
        *,
        maxsize: int | None = None,
        latest: bool = False,
    ) -> None:
        self._relay = relay
        self.patterns = patterns
        self._maxsize = maxsize
        self._latest = latest
        # Messages lost to bounded/latest inbox policy since registration.
        self.dropped = 0
        # Guards the inbox below so a worker-thread publish can hand a message to a
        # handler awaiting on the owning loop.
        self._inbox_lock = Lock()
        self.active = False
        # Bumped on register/unregister so in-flight deliveries after teardown are ignored.
        self.generation = 0
        # Messages delivered while no receive() is waiting. A deque in normal
        # mode; a subject-keyed dict in latest mode (insertion order = arrival).
        self._items: deque[Msg[T]] | dict[str, Msg[T]] = {} if latest else deque()
        # The loop that registered us; every delivery must land there.
        self.loop: AbstractEventLoop | None = None
        # The future a blocked receive() is awaiting, if any (at most one).
        self._waiter: Future[Msg[T]] | None = None
        # A cross-loop enqueue() already scheduled wake() on the owning loop.
        self._wake_pending = False

    def _register(self) -> None:
        loop = get_running_loop()
        with self._inbox_lock:
            if self.active:
                raise StarioRuntime("subscription is already active")
            self.generation += 1
            # Reused handle: start clean so a prior enter's messages never surface.
            self._items.clear()
            self.loop = loop
            self._waiter = None
            self._wake_pending = False
        # Join the registry while still inactive; publish skips until active is set.
        relay = self._relay
        with relay.lock:
            for pat in self.patterns:
                bucket = relay.subs.get(pat)
                if bucket is None:
                    bucket = relay.subs[pat] = {}
                    relay._count_prefix(pat, 1)
                bucket[self] = None
        with self._inbox_lock:
            self.active = True

    def invalidate_inbox(self) -> None:
        """Mark inactive, bump generation, and cancel any blocked receive."""
        with self._inbox_lock:
            self.active = False
            self.generation += 1
            waiter = self._waiter
            self._waiter = None
            loop = self.loop
        # Cancelling a Future schedules its callbacks via loop.call_soon, which
        # raises RuntimeError on a closed loop. A closed loop means the awaiting
        # coroutine is already gone (this happens when publish() reaps a dead
        # subscriber), so there is nothing to wake — skip the cancel.
        if (
            waiter is not None
            and not waiter.done()
            and loop is not None
            and not loop.is_closed()
        ):
            waiter.cancel()

    async def receive(self) -> Msg[T]:
        """Wait for the next message on an active subscription."""
        loop = get_running_loop()
        with self._inbox_lock:
            if not self.active:
                raise StarioRuntime("subscription is not active")
            if self.loop is not loop:
                raise StarioRuntime(
                    "subscription must be consumed on its owning loop",
                )
            # Drain buffered messages first; only block when the inbox is empty.
            if self._items:
                return self._popleft_locked()
            # Single-consumer handle: a second concurrent receive() is a usage bug.
            if self._waiter is not None:
                raise StarioRuntime("subscription already has a pending receive")
            waiter = loop.create_future()
            self._waiter = waiter
        try:
            return await waiter
        finally:
            # Clear our slot unless deliver already consumed it (or a newer receive
            # replaced it after a cancellation woke us).
            with self._inbox_lock:
                if self._waiter is waiter:
                    self._waiter = None

    def _append_locked(self, msg: Msg[T]) -> None:
        # Inbox policy lives here so deliver() and enqueue() share it.
        items = self._items
        if self._latest:
            latest_items = cast(dict[str, Msg[T]], items)
            subject = msg[0]
            if subject in latest_items:
                # Newer value for a buffered subject replaces it, latest at end.
                del latest_items[subject]
                self.dropped += 1
            elif self._maxsize is not None and len(latest_items) >= self._maxsize:
                del latest_items[next(iter(latest_items))]
                self.dropped += 1
            latest_items[subject] = msg
            return
        queue = cast(deque[Msg[T]], items)
        if self._maxsize is not None and len(queue) >= self._maxsize:
            queue.popleft()
            self.dropped += 1
        queue.append(msg)

    def _popleft_locked(self) -> Msg[T]:
        items = self._items
        if self._latest:
            latest_items = cast(dict[str, Msg[T]], items)
            return latest_items.pop(next(iter(latest_items)))
        return cast(deque[Msg[T]], items).popleft()

    def deliver(self, msg: Msg[T], generation: int) -> None:
        # Runs on the subscription's own loop (same-loop publishers call it
        # inline), so it may complete the waiter Future directly.
        with self._inbox_lock:
            # Stale delivery for a torn-down / re-registered subscription: drop it.
            if not self.active or self.generation != generation:
                return
            self._append_locked(msg)
            self._hand_off_locked()

    def enqueue(self, msg: Msg[T], generation: int) -> bool:
        """Buffer ``msg`` from any thread. True when the owning loop needs a wake.

        Only the Future hand-off is loop-affine; the deque is not. A
        cross-loop publisher appends under the inbox lock and hops to the
        loop only when a ``receive()`` is blocked and no hop is already in
        flight. A subscriber that keeps up costs its publisher no
        ``call_soon_threadsafe`` at all; a burst of N messages costs one.
        """
        with self._inbox_lock:
            if not self.active or self.generation != generation:
                return False
            self._append_locked(msg)
            if self._waiter is None or self._wake_pending:
                return False
            self._wake_pending = True
            return True

    def wake(self, generation: int) -> None:
        """Hand buffered messages to a blocked receive(); scheduled by a cross-loop enqueue()."""
        with self._inbox_lock:
            self._wake_pending = False
            if not self.active or self.generation != generation:
                return
            self._hand_off_locked()

    def _hand_off_locked(self) -> None:
        # Caller holds _inbox_lock and runs on the owning loop. Give the
        # oldest buffered message to a blocked receive(); a cancelled (done)
        # waiter leaves the message buffered for the next receive().
        waiter = self._waiter
        if waiter is None or not self._items:
            return
        self._waiter = None
        if not waiter.done():
            waiter.set_result(self._popleft_locked())

    async def __aenter__(self) -> Self:
        # Register on enter; unregister on exit — otherwise handles stay in
        # relay.subs and never get garbage-collected.
        self._register()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        # Idempotent: we may already have been reaped (e.g. publish dropped a dead
        # sub) before the `async with` block exits.
        if self.active:
            self._relay.drop_subscription(self)
        return False

    async def __aiter__(self) -> AsyncIterator[Msg[T]]:
        # A fresh async generator per `async for`; each step just awaits receive().
        if not self.active:
            raise StarioRuntime("subscription must be used with async with")
        while True:
            yield await self.receive()


class Relay[T]:
    """In-process publish/subscribe registry.

    Use `async with relay.subscribe(...) as sub:`, then consume with
    `sub.receive()` or `async for msg in sub`. Nothing is queued before enter.

    Dead subscribers (closed event loop) are removed silently; `publish` does not
    report delivery failures.
    """

    __slots__ = ("_keys", "_prefix_counts", "_prefixes", "lock", "subs")

    def __init__(self) -> None:
        self.lock = Lock()
        # pattern -> subscriptions registered for it, in delivery order. A dict
        # keeps insertion order with O(1) add and remove, so N subscribers of
        # one pattern can leave without rebuilding the bucket N times.
        self.subs: dict[str, dict[RelaySubscription[T], None]] = {}
        # Registered `prefix.*` keys by the length of `prefix.`, and those
        # lengths longest first (see _matching_patterns). Guarded by `lock`.
        self._prefix_counts: dict[int, int] = {}
        self._prefixes: tuple[int, ...] = ()
        # subject -> its registry lookup keys for the current `_prefixes`.
        self._keys: dict[str, tuple[str, ...]] = {}

    def _count_prefix(self, pattern: str, delta: int) -> None:
        # Caller holds `lock`; `pattern` gained or lost its registry bucket.
        if pattern == "*" or not pattern.endswith(".*"):
            return
        length = len(pattern) - 1
        counts = self._prefix_counts
        count = counts.get(length, 0) + delta
        if count:
            counts[length] = count
        else:
            del counts[length]
        self._prefixes = tuple(sorted(counts, reverse=True))
        self._keys.clear()

    def drop_subscription(self, subscription: RelaySubscription[T]) -> None:
        # Wake/cancel any blocked receive first, then remove from the registry.
        subscription.invalidate_inbox()
        with self.lock:
            for pattern in subscription.patterns:
                subs = self.subs.get(pattern)
                if subs is None:
                    continue
                subs.pop(subscription, None)
                if not subs:
                    # Drop empty buckets so the registry doesn't accumulate dead keys.
                    del self.subs[pattern]
                    self._count_prefix(pattern, -1)

    def publish(self, subject: str, data: T) -> None:
        """Deliver `(subject, data)` to every subscriber whose pattern matches.

        Thread-safe from any OS thread. Delivery runs after the subscriber
        snapshot is taken, so `Relay.lock` is not held while `Future.set_result`
        runs. A same-loop delivery does hold that subscription's `_inbox_lock`
        for the `set_result` call; it only schedules the waiter's callbacks on
        the loop, so no other code runs under the lock. Does not await
        subscribers.
        """
        if not subject:
            raise StarioError("publish subject must be non-empty")
        if "*" in subject:
            raise StarioError(
                "publish subject must be exact; wildcards are only for subscribe()",
            )
        if not _valid_subject(subject):
            raise StarioError("publish subject segments must be non-empty")

        msg: Msg[T] = (subject, data)

        # Detect the caller's loop once (None when publishing from a plain thread).
        # Subscribers on this same loop can be delivered inline; subscribers on other
        # loops must be scheduled onto their loop with call_soon_threadsafe.
        try:
            caller_loop = get_running_loop()
        except RuntimeError:
            caller_loop = None

        # Snapshot matching subscribers under the lock, then deliver outside it so
        # set_result / call_soon_threadsafe never run while lock is held.
        immediate: list[tuple[RelaySubscription[T], int]] = []
        deferred: list[tuple[RelaySubscription[T], int, AbstractEventLoop]] = []
        dead: list[RelaySubscription[T]] = []

        with self.lock:
            keys = self._keys.get(subject)
            if keys is None:
                keys = _matching_patterns(subject, self._prefixes)
                if len(subject) <= _KEYS_CACHE_CHARS:
                    if len(self._keys) >= _KEYS_CACHE_SIZE:
                        self._keys.clear()
                    self._keys[subject] = keys
            for pattern in keys:
                subs = self.subs.get(pattern)
                if not subs:
                    continue
                # Patterns are de-duplicated at subscribe time, so each subscription
                # matches at most one of these keys — no per-subject de-dup needed.
                for subscription in subs:
                    # Inbox fields (active/loop/generation) are guarded by
                    # _inbox_lock, not lock. Reading them here is a lock-free
                    # snapshot; deliver re-validates under _inbox_lock, so a stale
                    # read only skips, or harmlessly schedules, one delivery.
                    if not subscription.active:
                        continue
                    loop = subscription.loop
                    if loop is None:
                        continue
                    generation = subscription.generation
                    if loop is caller_loop:
                        immediate.append((subscription, generation))
                    elif loop.is_closed():
                        dead.append(subscription)
                    else:
                        deferred.append((subscription, generation, loop))

        # Same-loop subscribers: deliver inline (we are already on their loop).
        for subscription, generation in immediate:
            subscription.deliver(msg, generation)

        # Subscribers whose loop has closed are unreachable — reap them.
        for subscription in dead:
            self.drop_subscription(subscription)

        # Cross-loop subscribers: buffer here, hop onto the owning loop only
        # when a blocked receive() needs waking (one hop per burst, not per
        # message).
        for subscription, generation, loop in deferred:
            if loop.is_closed():
                self.drop_subscription(subscription)
                continue
            if not subscription.enqueue(msg, generation):
                continue
            try:
                loop.call_soon_threadsafe(subscription.wake, generation)
            except RuntimeError:
                # Lost a race with loop shutdown; only swallow the closed-loop case.
                if not loop.is_closed():
                    raise
                self.drop_subscription(subscription)

    def subscribe(
        self,
        *patterns: str,
        maxsize: int | None = None,
        latest: bool = False,
    ) -> RelaySubscription[T]:
        """Subscribe to one or more patterns.

        Overlapping patterns are merged — each matching publish delivers at most once.

        The returned handle buffers nothing until entered with `async with`; do not
        use `async for` on the bare return value. Its inbox is unbounded by
        default: a consumer that falls behind a fast publisher grows memory
        without limit. Two opt-in bounds change that:

        - `maxsize=N` caps buffered messages; when full, the oldest buffered
          message is dropped for the new one.
        - `latest=True` keeps only the newest message per subject; a new
          message for a buffered subject replaces it (and moves it to the
          end). With `maxsize` the cap counts distinct subjects.

        Dropped and replaced messages count in `sub.dropped`. Use a broker
        (NATS, Redis) when you need real backpressure.
        """
        if not patterns:
            raise StarioError(
                "subscribe() requires at least one pattern",
                example='relay.subscribe("room.*")',
            )
        if maxsize is not None and maxsize < 1:
            raise StarioError(
                "subscribe(maxsize=) must be at least 1",
                context={"maxsize": maxsize},
            )

        return RelaySubscription(
            self,
            _deduplicate_patterns(tuple(patterns)),
            maxsize=maxsize,
            latest=latest,
        )
