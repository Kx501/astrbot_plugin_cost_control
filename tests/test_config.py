"""``config`` 模块纯函数 + 插件配置文件 IO 单测。"""

import os

from cost_control.config import (
    coerce_to_default_type,
    deep_merge,
    get_enabled_price_sources,
    get_price_sources,
    get_pricing,
    load_plugin_config,
    mark_migration_done,
    migration_done,
    normalize_price_sources,
    save_plugin_config,
    switches_from_config,
)

# ===== deep_merge =====


def test_deep_merge_nested():
    assert deep_merge({"a": 1, "b": {"x": 1}}, {"b": {"y": 2}}) == {"a": 1, "b": {"x": 1, "y": 2}}


def test_deep_merge_scalar_override():
    assert deep_merge({"a": 1}, {"a": 5}) == {"a": 5}


def test_deep_merge_nondict_replaces():
    assert deep_merge({"a": {"b": 1}}, {"a": 9}) == {"a": 9}


def test_deep_merge_multi_source_order():
    # 后者覆盖前者
    assert deep_merge({}, {"a": 1}, {"a": 2}) == {"a": 2}
    assert deep_merge({"a": {"x": 1}}, {"a": {"y": 2}}, {"a": {"x": 3}}) == {"a": {"x": 3, "y": 2}}


def test_deep_merge_base_nondict():
    assert deep_merge(5, {"a": 1}) == {"a": 1}


# ===== coerce_to_default_type =====


def test_coerce_bool():
    assert coerce_to_default_type(1, True) is True
    assert coerce_to_default_type(0, True) is False


def test_coerce_int():
    assert coerce_to_default_type("5", 0) == 5
    assert coerce_to_default_type(-3, 10) == 0  # 负数归 0
    assert coerce_to_default_type("abc", 7) == 7  # 非法回退默认


def test_coerce_float():
    assert coerce_to_default_type("1.5", 0.0) == 1.5
    assert coerce_to_default_type("x", 2.0) == 2.0


def test_coerce_str():
    assert coerce_to_default_type(123, "") == "123"


def test_coerce_list():
    assert coerce_to_default_type([1, 2], []) == [1, 2]
    assert coerce_to_default_type("nope", []) == []


def test_coerce_dict_fixed_keys_missing_backfill():
    assert coerce_to_default_type({"a": 1}, {"a": 0, "b": 0}) == {"a": 1, "b": 0}


def test_coerce_dict_none():
    assert coerce_to_default_type(None, {"a": 0}) == {"a": 0}


def test_coerce_dict_empty_accepts_any():
    out = coerce_to_default_type({"gpt-4o": {"input": 2.5}}, {})
    assert out == {"gpt-4o": {"input": 2.5}}


# ===== switches_from_config =====


def test_switches_from_config_extracts_only_enabled():
    # schema 只保留总开关 enabled；其余键（即便存在）一律不抽取，
    # 它们的值由插件自有 config.json 承载。
    raw = {
        "enabled": False,
        "alerts": {"enabled": True, "cooldown_seconds": 99},
        "cache_diag": {"detect_context_reset": False, "cache_hit_rate_alert_threshold": 50},
        "budgets": {"global_daily": 1000},
    }
    sw = switches_from_config(raw)
    assert sw == {"enabled": False}


def test_switches_from_config_empty():
    assert switches_from_config({}) == {}
    assert switches_from_config(None) == {}


# ===== 插件配置文件 IO =====


def test_plugin_config_round_trip(tmp_path):
    d = str(tmp_path)
    cfg = {
        "budgets": {"global_daily": 1000},
        "pricing": {"gpt-4o": {"input": 2.5}},
        "alerts": {"enabled": True},
    }
    save_plugin_config(d, cfg)
    assert load_plugin_config(d) == cfg
    # 文件确实写出
    assert os.path.exists(os.path.join(d, "config.json"))


def test_plugin_config_missing_returns_empty(tmp_path):
    assert load_plugin_config(str(tmp_path)) == {}


def test_plugin_config_overwrite(tmp_path):
    d = str(tmp_path)
    save_plugin_config(d, {"a": 1})
    save_plugin_config(d, {"a": 2, "b": 3})
    assert load_plugin_config(d) == {"a": 2, "b": 3}


# ===== 一次性迁移标记 =====


def test_migration_done_default_false():
    assert migration_done({}, "fix_mislabeled_cost_currency") is False
    assert migration_done(None, "fix_mislabeled_cost_currency") is False
    assert migration_done({"migrations": "bad-type"}, "fix_mislabeled_cost_currency") is False


def test_mark_migration_done_sets_and_persists(tmp_path):
    d = str(tmp_path)
    cfg: dict = {"budgets": {"global_daily": 1000}}
    save_plugin_config(d, cfg)

    assert migration_done(cfg, "fix_mislabeled_cost_currency") is False
    mark_migration_done(cfg, "fix_mislabeled_cost_currency")
    assert migration_done(cfg, "fix_mislabeled_cost_currency") is True
    # 其它配置不受影响
    assert cfg["budgets"] == {"global_daily": 1000}

    save_plugin_config(d, cfg)
    reloaded = load_plugin_config(d)
    assert migration_done(reloaded, "fix_mislabeled_cost_currency") is True
    # 再跑一次标记幂等
    mark_migration_done(cfg, "fix_mislabeled_cost_currency")
    assert migration_done(cfg, "fix_mislabeled_cost_currency") is True


def test_mark_migration_done_creates_block_when_missing():
    cfg: dict = {}
    mark_migration_done(cfg, "m1")
    mark_migration_done(cfg, "m2")
    assert cfg["migrations"] == {"m1": True, "m2": True}


def test_price_sources_backfill_public_defaults_for_partial_config():
    """动态 New API 源不能覆盖三种公共源的默认配置。"""
    sources = get_price_sources({"price_sources": {"newapi:gateway": {"enabled": True}}})
    assert set(sources) == {"modelsdev", "litellm", "openrouter", "newapi:gateway"}
    assert get_enabled_price_sources({"price_sources": {"newapi:gateway": {"enabled": True}}}) == [
        "modelsdev",
        "litellm",
        "openrouter",
        "newapi:gateway",
    ]


def test_normalize_price_sources_rejects_unknown_source_ids():
    normalized = normalize_price_sources(
        {
            "modelsdev": {"enabled": False},
            "newapi:gateway": {"enabled": True, "provider_id": "gateway"},
            "custom-url": {"enabled": True},
            "newapi:": {"enabled": True},
        }
    )
    assert normalized["modelsdev"]["enabled"] is False
    assert normalized["newapi:gateway"]["provider_id"] == "gateway"
    assert "custom-url" not in normalized
    assert "newapi:" not in normalized


def test_get_pricing_includes_normalized_cluster_multipliers():
    pricing = get_pricing(
        {
            "pricing_multipliers": {
                "openai-main": "1.25",
                "default-one": 1,
                "free": 0,
                "invalid-negative": -0.5,
            }
        }
    )
    assert pricing["multipliers"] == {"openai-main": 1.25, "free": 0.0}


def test_deep_merge_isolates_mutable_defaults_and_overrides():
    base = {"group": {"enabled": True}, "rows": []}
    override = {"price": {"input": 2}}
    merged = deep_merge(base, override)
    merged["group"]["enabled"] = False
    merged["rows"].append(1)
    merged["price"]["input"] = 9
    assert base == {"group": {"enabled": True}, "rows": []}
    assert override == {"price": {"input": 2}}
    assert deep_merge({}, 1, {"final": 2}) == {"final": 2}


def test_coerce_partial_group_preserves_nonzero_defaults():
    assert coerce_to_default_type({}, {"enabled": True, "time": "09:00", "rate": 100}) == {
        "enabled": True, "time": "09:00", "rate": 100,
    }
    assert coerce_to_default_type("false", True) is False
    assert coerce_to_default_type(float("inf"), 5) == 5
    assert coerce_to_default_type(float("nan"), 2.5) == 2.5


def test_service_tier_accepts_explicit_free_but_not_invalid_multiplier():
    rule = get_pricing({"pricing": {"p": {
        "mode": "per_tier", "base": {"input": 2}, "service_tiers": [
            {"match": "free", "input_multiplier": 0},
            {"match": "bad", "input_multiplier": float("inf")},
            {"match": "negative", "input_multiplier": -1},
        ],
    }}})["user"]["p"]
    assert rule["service_tiers"] == [{"match": "free", "input_multiplier": 0.0}]


def test_config_write_invalid_number_preserves_existing_file(tmp_path):
    import pytest

    save_plugin_config(str(tmp_path), {"rate": 1})
    with pytest.raises(ValueError):
        save_plugin_config(str(tmp_path), {"rate": float("nan")})
    assert load_plugin_config(str(tmp_path)) == {"rate": 1}
    assert list(tmp_path.iterdir()) == [tmp_path / "config.json"]


def test_concurrent_config_writes_use_distinct_temporary_files(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    replace = os.replace

    def simultaneous_replace(src, dest):
        barrier.wait(timeout=5)
        replace(src, dest)

    monkeypatch.setattr(os, "replace", simultaneous_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(save_plugin_config, str(tmp_path), {"value": n}) for n in (1, 2)]
        for task in tasks:
            task.result(timeout=10)
    assert load_plugin_config(str(tmp_path)) in ({"value": 1}, {"value": 2})


def test_malformed_rule_does_not_break_other_pricing_or_budget_rules():
    from cost_control.config import enabled_overrides, normalize_budget_override

    pricing = get_pricing({"pricing": {
        "bad": {"mode": "per_tier", "base": {"input": 2}, "context_tiers": 42},
        "good": {"mode": "per_turn", "price": 3},
    }})
    assert "bad" not in pricing["user"]
    assert pricing["user"]["good"]["price"] == 3
    rule = {"target_type": "umo", "target_value": "session", "token_limit": float("inf"),
            "cost_limit": float("inf"), "fallback_token_limit": float("inf")}
    normalized = normalize_budget_override(rule)
    assert normalized["token_limit"] == normalized["cost_limit"] == 0
    assert normalized["fallback_token_limit"] == 0
    assert enabled_overrides([{**rule, "enabled": "false"}]) == []
