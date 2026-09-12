"""SQLite 回归：并发初始化、用户统计时间窗口及历史费用快照。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from cost_control.store import StoreMixin


class _Store(StoreMixin):
    def __init__(self, data_dir):
        self.data_dir = data_dir

    def get_data_dir(self):
        return self.data_dir


@pytest_asyncio.fixture
async def store(tmp_path):
    host = _Store(tmp_path)
    yield host
    if host._engine is not None:
        await host._engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_first_writes_wait_for_complete_initialization(store):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = store._migrate_supplement_columns
    calls = 0

    async def pause_migration(conn):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await original(conn)

    store._migrate_supplement_columns = pause_migration
    first = asyncio.create_task(store.save_supplement({"umo": "first"}))
    await entered.wait()
    second = asyncio.create_task(store.save_supplement({"umo": "second"}))
    await asyncio.sleep(0)
    assert store._session_maker is None
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert calls == 1
    await store.init_store()
    assert calls == 1
    assert len(await store.query_supplements()) == 2


@pytest.mark.asyncio
async def test_failed_initialization_can_retry_without_publishing_broken_engine(store):
    original = store._migrate_supplement_columns

    async def fail(conn):
        raise RuntimeError("migration failed")

    store._migrate_supplement_columns = fail
    with pytest.raises(RuntimeError, match="migration failed"):
        await store.init_store()
    assert store._session_maker is None
    assert store._engine is None
    store._migrate_supplement_columns = original
    await store.save_supplement({"umo": "recovered"})
    assert len(await store.query_supplements()) == 1


@pytest.mark.asyncio
async def test_user_ranking_filters_window_empty_users_and_sorts_all_tokens(store):
    now = datetime.now(UTC)
    records = [
        {"user_id": "old", "token_input_other": 10000, "created_at": now - timedelta(days=2)},
        {"user_id": "input", "token_input_other": 100},
        {"user_id": "output", "token_input_other": 1, "token_output": 200},
        {"user_id": "cached", "token_input_cached": 500},
        {"user_id": "", "token_input_other": 9000},
        {"user_id": None, "token_input_other": 9000},
    ]
    for record in records:
        await store.save_supplement({"umo": "session", "created_at": now, **record})
    result = await store.query_user_token_totals(now - timedelta(days=1), limit=2)
    assert result == [("cached", 500), ("output", 201)]


@pytest.mark.asyncio
async def test_user_cost_uses_snapshot_even_if_current_price_changed_or_disappeared(store):
    now = datetime.now(UTC)
    await store.save_supplement(
        {
            "umo": "s",
            "user_id": "u",
            "provider_id": "p",
            "provider_model": "m",
            "token_input_other": 1_000_000,
            "cost_amount": 7.2,
            "currency_symbol": "CNY",
            "created_at": now,
        }
    )
    for pricing in ({}, {"user": {"p": {"mode": "per_token", "input": 20}}}):
        assert await store.query_user_cost_total(
            "u", now - timedelta(seconds=1), pricing, "USD", {"USD": 1, "CNY": 7.2}
        ) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_user_cost_per_request_counts_once_per_provider(store):
    now = datetime.now(UTC)
    for provider in ("p", "p", "q"):
        await store.save_supplement(
            {
                "umo": "s",
                "user_id": "u",
                "provider_id": provider,
                "request_id": "r",
                "cost_amount": 0,
                "created_at": now,
            }
        )
    pricing = {
        "user": {
            "p": {"mode": "per_request", "price": 2},
            "q": {"mode": "per_request", "price": 3},
        }
    }
    assert await store.query_user_cost_total("u", now - timedelta(seconds=1), pricing) == 5


@pytest.mark.asyncio
async def test_legacy_cache_table_migrates_snapshot_columns(store):
    import sqlite3

    with sqlite3.connect(store.data_dir / "supplement.db") as conn:
        conn.execute("""CREATE TABLE cache_events (
            id INTEGER PRIMARY KEY, umo TEXT, type TEXT, severity TEXT,
            detail TEXT, created_at DATETIME
        )""")
        conn.execute("""INSERT INTO cache_events (umo, type, created_at)
            VALUES ('old', 'context_reset', '2026-08-01 00:00:00')""")
    await store.init_store()
    await store.save_cache_event(
        {"umo": "new", "type": "context_reset", "before": {"count": 2}, "after": {"count": 0}}
    )
    rows = await store.query_cache_events()
    assert len(rows) == 2
    assert rows[0].before == {"count": 2}
    assert rows[1].before is None


@pytest.mark.asyncio
async def test_close_store_is_idempotent_and_prevents_late_reopening(store):
    await store.save_supplement({"umo": "saved"})
    await store.close_store()
    await store.close_store()
    assert store._engine is None
    assert store._session_maker is None
    with pytest.raises(RuntimeError, match="closed"):
        await store.save_supplement({"umo": "late-callback"})
