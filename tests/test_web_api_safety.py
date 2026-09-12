"""Regression tests for configuration writes and destructive API request boundaries."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from cost_control.config import CONFIG_DEFAULTS, deep_merge
from cost_control.web_api import WebApiMixin


def test_partial_group_save_preserves_existing_settings():
    host = WebApiMixin()
    host.cfg = deep_merge(
        CONFIG_DEFAULTS,
        {
            "alerts": {"daily_report_time": "12:30", "daily_report_to": ["session"]},
            "schedule": {"retain_days": 45},
            "price_sync": {"cron": "0 5 * * *"},
        },
    )
    merged, error = host._validate_save_payload(
        {
            "alerts": {"enabled": False},
            "schedule": {"enable_daily_report": True},
            "price_sync": {"auto_enabled": True},
        }
    )
    assert error == ""
    assert merged["alerts"]["enabled"] is False
    assert merged["alerts"]["daily_report_time"] == "12:30"
    assert merged["alerts"]["daily_report_to"] == ["session"]
    assert merged["schedule"]["retain_days"] == 45
    assert merged["price_sync"]["cron"] == "0 5 * * *"
    assert host.cfg["alerts"]["enabled"] is True


@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_malformed_config_group_is_rejected(value):
    host = WebApiMixin()
    host.cfg = deep_merge(CONFIG_DEFAULTS)
    merged, error = host._validate_save_payload({"alerts": value})
    assert merged is None
    assert "alerts" in error


@pytest.mark.parametrize("modules", [[], ["unknown"], ["supplements", {}]])
async def test_purge_validates_all_modules_before_deleting(modules):
    host = WebApiMixin()
    host.purge_module = AsyncMock(return_value=1)
    app = Quart(__name__)
    async with app.test_request_context(
        "/", method="POST", json={"modules": modules, "confirm": "PURGE"}
    ):
        result = await host.api_action_purge()
    assert result["success"] is False
    host.purge_module.assert_not_awaited()


async def test_purge_deduplicates_modules_and_blocks_overlapping_requests():
    host = WebApiMixin()
    started, finish = asyncio.Event(), asyncio.Event()

    async def purge(module):
        started.set()
        await finish.wait()
        return 3

    host.purge_module = AsyncMock(side_effect=purge)
    app = Quart(__name__)

    async def request_purge():
        async with app.test_request_context(
            "/", method="POST", json={"modules": ["supplements"] * 2, "confirm": "PURGE"}
        ):
            return await host.api_action_purge()

    first = asyncio.create_task(request_purge())
    await started.wait()
    second = await request_purge()
    assert second["success"] is False
    finish.set()
    assert (await first)["data"]["results"] == {"supplements": 3}
    host.purge_module.assert_awaited_once_with("supplements")


async def test_rate_sync_disk_failure_does_not_apply_or_report_success(tmp_path, monkeypatch):
    from cost_control import exchange_rates, web_api

    host = WebApiMixin()
    host._data_dir = str(tmp_path)
    host.cfg = {"exchange_rates": {"USD": 1.0, "CNY": 7.0}}
    monkeypatch.setattr(
        exchange_rates, "sync_rates", AsyncMock(return_value=({"USD": 1.0}, "now", ""))
    )

    def fail_write(*args):
        raise OSError("disk full")

    monkeypatch.setattr(web_api, "save_plugin_config", fail_write)
    result = await host.api_action_sync_rates()
    assert result["success"] is False
    assert "disk full" in result["error"]
    assert host.cfg["exchange_rates"] == {"USD": 1.0, "CNY": 7.0}


async def test_session_aggregate_prices_provider_rows():
    host = WebApiMixin()
    host.cfg = {"currency_symbol": "USD", "exchange_rates": {"USD": 1.0}}
    host.get_pricing = lambda: {
        "user": {"provider": {"mode": "per_token", "input": 2.0}}, "defaults": {}
    }
    host.query_usage_grouped = AsyncMock(
        return_value=[{"key": "session", "token_input_other": 1_000_000, "count": 1}]
    )
    host.query_usage_cost_rows = AsyncMock(
        return_value=[{
            "provider_id": "provider", "provider_model": "model",
            "token_input_other": 1_000_000, "count": 1,
        }]
    )
    app = Quart(__name__)
    async with app.test_request_context("/?by=umo&provider=provider"):
        result = await host.api_records_aggregate()
    assert result["success"] is True
    assert result["data"]["groups"][0]["cost"] == 2.0
    assert host.query_usage_cost_rows.await_args.kwargs["umo"] == "session"
    assert host.query_usage_cost_rows.await_args.kwargs["provider"] == "provider"


async def test_compare_windows_do_not_double_count_boundary(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from cost_control import analytics

    boundary = datetime(2026, 9, 6, tzinfo=UTC)
    monkeypatch.setattr(
        analytics, "compare_windows",
        lambda *args: (boundary, boundary + timedelta(days=1),
                       boundary - timedelta(days=1), boundary),
    )
    host = WebApiMixin()
    host.cfg = {}
    host.context = SimpleNamespace(get_config=lambda: {})
    host.get_pricing = lambda: {}
    host.query_usage = AsyncMock(return_value={})
    host.query_usage_cost_rows = AsyncMock(return_value=[])
    result = await host.api_compare()
    assert result["success"] is True
    current, previous = host.query_usage.await_args_list
    assert current.kwargs["start"] == boundary
    assert previous.kwargs["end"] == boundary - timedelta(microseconds=1)


def test_date_only_end_excludes_next_midnight():
    from datetime import UTC, datetime

    assert WebApiMixin._parse_iso("2026-09-06", is_end=True) == datetime(
        2026, 9, 6, 23, 59, 59, 999999, tzinfo=UTC
    )


@pytest.mark.parametrize("entry", [
    None,
    {"mode": "invalid"},
    {"input": float("inf")},
    {"input": float("nan")},
    {"input": -1},
    {"input": "not-a-price"},
    {"input": True},
    {"mode": "per_turn"},
    {"mode": "per_request", "price": float("inf")},
    {"mode": "tiered_expr", "expr": "no_such_function()"},
    {"mode": "per_tier", "base": {"input": 1}, "service_tiers": {}},
    {"mode": "per_tier", "base": {"input": 1}, "context_tiers": [None]},
    {"mode": "per_tier", "base": {"input": 1},
     "context_tiers": [{"threshold_tokens": 1.5, "input": 2}]},
    {"mode": "per_tier", "base": {"input": 1},
     "context_tiers": [{"threshold_tokens": 100, "input": float("inf")}]},
    {"mode": "per_tier", "base": {"input": 1},
     "service_tiers": [{"match": "fast", "input_multiplier": float("inf")}]},
])
def test_invalid_pricing_write_is_rejected_without_deleting_existing_price(entry):
    host = WebApiMixin()
    host.cfg = {"pricing": {"provider": {"input": 2}}}
    merged, error = host._validate_save_payload({"pricing": {"provider": entry}})
    assert merged is None
    assert "pricing[provider]" in error
    assert host.cfg["pricing"] == {"provider": {"input": 2}}


def test_pricing_write_preserves_explicit_zero_prices_and_multipliers():
    host = WebApiMixin()
    host.cfg = {}
    merged, error = host._validate_save_payload({"pricing": {
        "free": {"mode": "per_turn", "price": 0},
        "tiered": {"mode": "per_tier", "base": {"input": 1},
                   "service_tiers": [{"match": "free", "input_multiplier": 0}]},
    }})
    assert error == ""
    assert merged["pricing"]["free"]["price"] == 0
    assert merged["pricing"]["tiered"]["service_tiers"][0]["input_multiplier"] == 0


def test_scheduled_override_rejects_nonfinite_price():
    host = WebApiMixin()
    host.cfg = {}
    merged, error = host._validate_save_payload({"pricing_schedules": {
        "provider": {"periods": [{
            "id": "free", "weekdays": [1], "all_day": True,
            "adjustment": {"type": "override", "rule": {"input": float("inf")}},
        }]},
    }})
    assert merged is None
    assert "pricing.input" in error


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1, 101, None, True])
def test_invalid_cluster_multiplier_write_is_rejected(value):
    host = WebApiMixin()
    host.cfg = {"pricing_multipliers": {"source": 2}}
    merged, error = host._validate_save_payload({"pricing_multipliers": {"source": value}})
    assert merged is None
    assert "pricing_multipliers[source]" in error
    assert host.cfg["pricing_multipliers"] == {"source": 2}
