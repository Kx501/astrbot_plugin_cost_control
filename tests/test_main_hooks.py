"""真实入口的总开关、平台限制与 event 归因回归，不连接运行中的 AstrBot。"""

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture(scope="module")
def main_class():
    # 入口由 AstrBot 作为插件包加载。复用已导入的 Mixin，避免重复注册 SQLModel 表。
    root = Path(__file__).resolve().parents[1]
    package = ModuleType("_cost_control_hook_tests")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    sys.modules[f"{package.__name__}.cost_control"] = importlib.import_module("cost_control")
    for path in (root / "cost_control").glob("*.py"):
        if path.stem != "__init__":
            module = importlib.import_module(f"cost_control.{path.stem}")
            sys.modules[f"{package.__name__}.cost_control.{path.stem}"] = module
    spec = importlib.util.spec_from_file_location(f"{package.__name__}.main", root / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module.Main
    for key in list(sys.modules):
        if key == package.__name__ or key.startswith(package.__name__ + "."):
            del sys.modules[key]


@pytest.mark.parametrize("cfg", [
    {"enabled": False},
    {"enabled": True, "platforms": ["telegram"]},
])
async def test_disabled_or_excluded_platform_skips_all_request_hooks(main_class, cfg):
    host = main_class.__new__(main_class)
    host.cfg = cfg
    host.check_budget = AsyncMock()
    host.collect_response = AsyncMock()
    host.run_cache_diag = AsyncMock()
    host.record_initial_context = Mock()
    event = SimpleNamespace(get_platform_name=lambda: "aiocqhttp")
    req = SimpleNamespace()
    await host.on_llm_request_head(event, req)
    await host.on_llm_request_tail(event, req)
    await host.on_llm_response(event, SimpleNamespace())
    host.check_budget.assert_not_awaited()
    host.collect_response.assert_not_awaited()
    host.run_cache_diag.assert_not_awaited()
    host.record_initial_context.assert_not_called()


async def test_response_attribution_uses_its_event_not_latest_session(main_class):
    host = main_class.__new__(main_class)
    host.cfg = {}
    host.collect_response = AsyncMock(side_effect=lambda event, resp: {"umo": "shared"})
    host.save_supplement = AsyncMock()
    host.check_hit_rate = Mock(return_value=(-1, False))
    host.consume_last_injection = Mock(return_value={"injected_total": 999, "final": {}})
    sampled = SimpleNamespace(_cost_control_injection={"injected_total": 12, "final": {"user": 3}})
    unsampled = SimpleNamespace(_cost_control_injection=None)
    await host.on_llm_response(sampled, SimpleNamespace())
    await host.on_llm_response(unsampled, SimpleNamespace())
    first, second = [call.args[0] for call in host.save_supplement.await_args_list]
    assert first["injection_total"] == 12
    assert "injection_total" not in second
    host.consume_last_injection.assert_not_called()


async def test_termination_closes_store_even_when_cron_cleanup_fails(main_class):
    host = main_class.__new__(main_class)
    host.unregister_cron = AsyncMock(side_effect=RuntimeError("cron unavailable"))
    host.close_store = AsyncMock()
    await host.terminate()
    host.unregister_cron.assert_awaited_once()
    host.close_store.assert_awaited_once()
