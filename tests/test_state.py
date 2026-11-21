"""The duplicate guard and the per-symbol locks."""

from __future__ import annotations

import threading

from conftest import FakeClock

from tvbot.state import DedupeCache, SymbolLocks


def make(window: int = 30):
    clock = FakeClock(1000.0)
    return DedupeCache(window, clock), clock


def test_first_reserve_wins_and_an_identical_one_is_refused_while_running():
    cache, _ = make()
    assert cache.reserve("a") is True
    assert cache.reserve("a") is False


def test_different_keys_do_not_interfere():
    cache, _ = make()
    assert cache.reserve("a") is True
    assert cache.reserve("b") is True


def test_a_kept_entry_blocks_for_the_window_counted_from_finish():
    cache, clock = make(30)
    assert cache.reserve("a")
    clock.advance(20)  # the request ran for 20 seconds
    cache.finish("a", keep=True)
    clock.advance(29)
    assert cache.reserve("a") is False
    clock.advance(1)
    assert cache.reserve("a") is True


def test_a_running_entry_never_expires():
    cache, clock = make(30)
    assert cache.reserve("a")
    clock.advance(30 * 100)
    assert cache.reserve("a") is False
    cache.finish("a", keep=True)
    assert cache.reserve("a") is False


def test_finish_without_keep_frees_the_key_at_once():
    cache, _ = make()
    assert cache.reserve("a")
    cache.finish("a", keep=False)
    assert cache.reserve("a") is True


def test_finishing_an_unknown_key_is_harmless():
    cache, _ = make()
    cache.finish("never-reserved", keep=True)
    cache.finish("never-reserved", keep=False)
    assert cache.reserve("never-reserved") is True


def test_window_zero_turns_the_guard_off():
    cache, _ = make(0)
    assert cache.reserve("a") is True
    assert cache.reserve("a") is True
    cache.finish("a", keep=True)
    assert cache.reserve("a") is True


def test_expired_entries_are_dropped_so_the_cache_stays_small():
    cache, clock = make(10)
    for index in range(50):
        assert cache.reserve(f"k{index}")
        cache.finish(f"k{index}", keep=True)
    clock.advance(10)
    assert cache.reserve("fresh")
    assert len(cache._entries) == 1


def test_only_one_of_many_simultaneous_reservations_wins():
    cache, _ = make()
    barrier = threading.Barrier(32)
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=10)
        won = cache.reserve("same-body")
        with lock:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 32
    assert results.count(True) == 1


def test_symbol_locks_return_one_lock_per_pair():
    locks = SymbolLocks()
    assert locks.get("alpaca", "AAPL") is locks.get("alpaca", "AAPL")
    assert locks.get("alpaca", "AAPL") is not locks.get("alpaca", "MSFT")
    assert locks.get("alpaca", "AAPL") is not locks.get("tradier", "AAPL")


def test_a_symbol_lock_excludes_while_held():
    locks = SymbolLocks()
    lock = locks.get("alpaca", "AAPL")
    with lock:
        assert locks.get("alpaca", "AAPL").acquire(blocking=False) is False
        assert locks.get("alpaca", "MSFT").acquire(blocking=False) is True
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_concurrent_lookups_get_the_same_lock():
    locks = SymbolLocks()
    barrier = threading.Barrier(16)
    found: list[object] = []
    guard = threading.Lock()

    def worker():
        barrier.wait(timeout=10)
        lock = locks.get("schwab", "SPY")
        with guard:
            found.append(lock)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(found) == 16
    assert all(lock is found[0] for lock in found)
