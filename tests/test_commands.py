"""``/report`` 参数解析回归测试。

AstrBot 的 ``CommandFilter`` 只在局部变量剥离命令名并把参数解析进
``parsed_params``（以 kwargs 注入 handler），**不改写** ``event.message_str``
（仍含 "/report" 前缀）——旧的整串 ``message_str`` 比较永远不等于
daily/weekly/monthly，静默回退 daily。此文件锁定 kwargs 注入口径。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cost_control.commands import CommandsMixin


class _ReportHost(CommandsMixin):
    """最小宿主：记录 build_report 收到的 window，其余字段给空报表。"""

    def __init__(self) -> None:
        self.cfg = {}
        self.windows: list[str] = []

    async def build_report(self, window: str = "daily") -> dict:
        self.windows.append(window)
        return {
            "usage": {},
            "cost": 0.0,
            "cache_hit_rate": 0,
            "cache_samples": 0,
            "avg_injection": 0,
            "injection_samples": 0,
            "cost_by_model": [],
            "top_sessions": [],
        }


def _run(host: _ReportHost, window: str | None):
    """模拟 AstrBot 调度：event.message_str 带命令名，参数经 kwargs 注入。"""
    event = SimpleNamespace(
        message_str="/report " + (window or ""),
        plain_result=lambda s: s,
    )
    kwargs = {"window": window} if window is not None else {}

    async def _collect():
        return [out async for out in host.cmd_report(event, **kwargs)]

    return asyncio.run(_collect())


def test_report_window_from_kwargs_not_message_str():
    # 回归：message_str 是 "/report weekly"（带命令名），参数经 kwargs 到达。
    host = _ReportHost()
    result = _run(host, "weekly")
    assert host.windows == ["weekly"]
    assert "查询失败" not in result[0]


def test_report_window_normalizes_case_and_space():
    host = _ReportHost()
    _run(host, "  MONTHLY ")
    assert host.windows == ["monthly"]


def test_report_window_invalid_falls_back_daily():
    host = _ReportHost()
    _run(host, "yearly")  # 不在白名单 → daily
    assert host.windows == ["daily"]


def test_report_window_default_daily_without_kwargs():
    host = _ReportHost()
    _run(host, None)
    assert host.windows == ["daily"]


async def test_cost_preserves_valid_rows_and_combines_model_time_buckets():
    from datetime import UTC, datetime

    host = CommandsMixin()
    host.cfg = {"currency_symbol": "CNY", "exchange_rates": {"CNY": 7.0}}
    host._day_start = lambda: datetime(2026, 9, 1, tzinfo=UTC)
    host.query_usage = AsyncMock(return_value={"count": 8})
    host.get_pricing = lambda: {
        "user": {
            "paid": {"mode": "per_turn", "price": 1.0},
            "broken": {"mode": "tiered_expr", "expr": "p == 2 ? 1 / 0 : p"},
        }
    }
    host.query_usage_cost_rows = AsyncMock(
        return_value=[
            {"provider_id": "paid", "provider_model": "m1", "count": 1},
            {"provider_id": "paid", "provider_model": "m1", "count": 2},
            {"provider_id": "paid", "provider_model": "m2", "count": 4},
            {"provider_id": "broken", "provider_model": "m3", "count": 1, "token_input_other": 2},
        ]
    )
    event = SimpleNamespace(unified_msg_origin="session", plain_result=lambda text: text)
    outputs = [out async for out in host.cmd_cost(event)]
    text = outputs[0]
    assert "查询失败" not in text
    assert "¥49.0000" in text
    assert text.count("m1：") == 1
    assert "m1：3次 / ¥21.0000" in text
    assert text.index("m2：") < text.index("m1：")
    assert "金额仅含可计算部分" in text


async def test_budget_command_converts_global_and_override_thresholds():
    host = CommandsMixin()
    host.cfg = {
        "currency_symbol": "CNY",
        "exchange_rates": {"CNY": 7.0},
        "budgets_cost_currency": {"global_daily": "USD"},
        "budget_overrides": [
            {
                "target_type": "umo",
                "target_value": "session",
                "cost_limit": 3,
                "cost_currency": "USD",
            }
        ],
    }
    host.get_budgets = lambda: {}
    host.get_budgets_cost = lambda: {"global_daily": 2.0}
    host.check_budget = AsyncMock(return_value={"exceeded": False})
    event = SimpleNamespace(unified_msg_origin="session", plain_result=lambda text: text)
    outputs = [out async for out in host.cmd_budget(event)]
    assert "花费 ¥14.00" in outputs[0]
    assert "花费 ¥21.00" in outputs[0]
