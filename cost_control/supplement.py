"""补充采集 Mixin。

在 ``on_llm_response`` 钩子里解析 ``LLMResponse.usage``（``TokenUsage``）与
``LLMResponse.raw_completion`` 中的 cache 字段（如 Anthropic 的
``cache_creation_input_tokens`` / ``cache_read_input_tokens``），补充原生
``ProviderStat`` 未记录的信息，交由 ``StoreMixin.save_supplement`` 写入独立库。

注意：``TokenUsage`` 仅含 ``input_other`` / ``input_cached`` / ``output``，
``cache_creation`` 需从 ``raw_completion`` 解析。不同 provider 的 raw 类型不同
（Anthropic Message / OpenAI ChatCompletion / OpenAI Response（Responses API）/
Google GenerateContentResponse），cache 字段命名各异，按 duck-typing 兼容，
解析失败降级为 None。

计费上下文（service_tier / 1h 缓存写标志）一律取**响应侧**：Anthropic
``usage.cache_creation.ephemeral_1h_input_tokens`` 与 OpenAI
``service_tier``。AstrBot 4.25.5 的 ``ProviderRequest`` 没有
``extra_body`` / ``headers`` 字段，请求侧提取链路是永不触发的死代码，已删除。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

# 生产环境经 root logger 汇入 astrbot/loguru；测试经 caplog 可捕获。
logger = logging.getLogger("cost_control.supplement")


def _field(value: Any, key: str, default: Any = None) -> Any:
    """兼容 SDK 对象与 JSON 字典，不要求供应商使用同一 SDK。"""
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _token_count(value: Any) -> int | None:
    try:
        count = int(value)
        return count if count >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _extract_cache(
    raw: Any,
) -> tuple[int | None, int | None, dict[str, Any] | None, int | None]:
    """提取缓存写、读、原始用量及 1h 写入细分，兼容对象和字典响应。

    DeepSeek 的 miss 属于普通输入，不能当作额外缓存写入收费。
    每个字段独立解析，坏字段不应掩盖其他有效用量。
    """
    usage = _field(raw, "usage")
    usage_meta = _field(raw, "usage_metadata")
    raw_usage = None
    source = usage if usage is not None else usage_meta
    try:
        if isinstance(source, dict):
            raw_usage = dict(source)
        elif source is not None and hasattr(source, "model_dump"):
            raw_usage = source.model_dump()
    except Exception:
        pass

    cache_creation = _token_count(_field(usage, "cache_creation_input_tokens"))
    cache_creation_1h = _token_count(
        _field(_field(usage, "cache_creation"), "ephemeral_1h_input_tokens")
    )
    cache_read = None
    for value in (
        _field(usage, "cache_read_input_tokens"),
        _field(_field(usage, "prompt_tokens_details"), "cached_tokens"),
        _field(_field(usage, "input_tokens_details"), "cached_tokens"),
        _field(usage, "prompt_cache_hit_tokens"),
        _field(usage_meta, "cached_content_token_count"),
    ):
        parsed = _token_count(value)
        if parsed is not None:
            cache_read = parsed
            break
    return cache_creation, cache_read, raw_usage, cache_creation_1h


def _normalize_usage(usage: Any, cache_read: int | None) -> tuple[int, int, int]:
    """补齐宿主未识别的缓存命中分量，保持 TokenUsage 总输入不变。"""
    other = _token_count(_field(usage, "input_other")) or 0
    cached = _token_count(_field(usage, "input_cached")) or 0
    output = _token_count(_field(usage, "output")) or 0
    # 例如 DeepSeek 的 hit 扩展没有被 AstrBot 映射到 input_cached。
    # 已有缓存分量时保留宿主结果，以免把多步聚合用量替换成单步 raw 用量。
    if cached == 0 and cache_read is not None and 0 <= cache_read <= other:
        cached, other = cache_read, other - cache_read
    return other, cached, output


def _safe_sender_id(event: Any) -> str | None:
    """从 ``AstrMessageEvent`` 读取发送者 user_id（健壮封装，绝不抛异常）。

    AstrBot 4.25.5 暴露 ``event.get_sender_id()``（返回 ``message_obj.sender.user_id``，
    平台统一处理为 str；不同平台可能是 QQ 号 / 微信 openid / 钉钉 staff_id）。读不到
    或抛异常一律返回 ``None``（按用户 override 在 user_id 为空时自然不会命中）。
    """
    try:
        fn = getattr(event, "get_sender_id", None)
        if not callable(fn):
            return None
        v = fn()
        if v is None:
            return None
        s = str(v).strip()
        return s or None
    except Exception:
        return None


def _read_request_id(event: Any) -> str | None:
    """读回 ``on_llm_request_head`` 挂到 event 上的用户请求 ID（健壮只读）。

    一次用户请求（pipeline）在 function-calling 多步场景下触发多次 LLM 调用，
    head 钩子为同一 event 生成一次 ``_cost_control_request_id``。读不到（插件
    中途加载、event 未经过 head、或 AstrBot 每次 clone 新 event）返回 ``None``。
    """
    try:
        rid = getattr(event, "_cost_control_request_id", None)
        if rid is None:
            return None
        s = str(rid).strip()
        return s or None
    except Exception:
        return None


def _extract_billing_context(
    raw: Any,
    cache_creation_1h: int | None = None,
) -> dict[str, Any]:
    """从**响应侧**提取计费上下文，归一化到 ``{"params": {...}}`` 形态。

    计费字段全部来自响应（权威且可得）：

    - ``service_tier``：OpenAI ``ChatCompletion.service_tier``（attr 或 dict）；
    - ``cache_ttl_1h``：Anthropic ``usage.cache_creation.ephemeral_1h_input_tokens
      > 0`` 时置 True（1h 缓存写，2× 价）。

    请求侧（``ProviderRequest.extra_body`` / ``.headers``）在 AstrBot 4.25.5
    并不存在这两个字段（已核对 ``core/provider/entities.py``），旧版从这里提取
    的链路是永不触发的死代码，已删除；``expr_eval.param()`` 读取口径不变。
    全异常吞掉返回 ``{}``。
    """
    try:
        params: dict[str, Any] = {}
        st = getattr(raw, "service_tier", None)
        if st is None and isinstance(raw, dict):
            st = raw.get("service_tier")
        if st is not None:
            s = str(st).strip()
            if s:
                params["service_tier"] = s
        if cache_creation_1h is not None and int(cache_creation_1h) > 0:
            params["cache_ttl_1h"] = True
        if not params:
            return {}
        out = {"params": params}
        logger.debug(
            "[cost_control] 计费上下文字段 params_keys=%s",
            sorted(params),
        )
        return out
    except Exception:
        return {}


def _active_agent_usage(event: Any) -> tuple[int, int, int] | None:
    """读取同一事件的内部 agent 累计用量；宿主接口不可用时保留响应采样。

    AstrBot 4.25.5 的 OnLLMResponse 在 agent 结束时仅提供最终一轮响应，
    累计值保存在活动 runner.stats。该注册表是宿主私有兼容路径，必须验证
    event 对象身份，不能仅凭 UMO 把并发请求或后续消息的用量归给当前用户。
    """
    try:
        from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS

        umo = str(getattr(event, "unified_msg_origin", None) or "")
        runner = _ACTIVE_AGENT_RUNNERS.get(umo)
        context = getattr(getattr(runner, "run_context", None), "context", None)
        if runner is None or getattr(context, "event", None) is not event:
            return None
        usage = getattr(getattr(runner, "stats", None), "token_usage", None)
        if usage is None:
            return None
        other = _token_count(_field(usage, "input_other"))
        cached = _token_count(_field(usage, "input_cached"))
        output = _token_count(_field(usage, "output"))
        if other is None or cached is None or output is None:
            return None
        return other, cached, output
    except Exception:
        return None


class SupplementMixin:
    """``on_llm_response`` 钩子补充采集 usage + cache 字段的 Mixin。"""

    # 由 ``Main`` 宿主提供（Mixin 不定义 ``__init__``）。
    context: Any

    async def collect_response(
        self,
        event: AstrMessageEvent,
        resp: Any,
    ) -> dict[str, Any]:
        """从 LLM 响应解析 usage 与 raw cache 字段，组装补充记录 dict。

        Args:
            event: 触发响应的 ``AstrMessageEvent``。
            resp: ``LLMResponse`` 对象，含 ``usage`` 与 ``raw_completion``。

        Returns:
            包含 umo / provider_id / provider_model / token 三类 /
            cache_creation / cache_read / raw_usage / response_id / created_at
            / cost_amount / currency_symbol 的补充记录 dict。
        """
        from .cost import TieredExprEvaluationError, compute_cost_with_currency

        usage = getattr(resp, "usage", None)
        raw = getattr(resp, "raw_completion", None)
        cache_creation, cache_read, raw_usage, cache_creation_1h = _extract_cache(raw)
        token_input_other, token_input_cached, token_output = _normalize_usage(usage, cache_read)
        total_usage = _active_agent_usage(event)
        response_usage = _normalize_usage(usage, None)
        has_prior_usage = total_usage is not None and sum(total_usage) > sum(response_usage)
        if total_usage is not None and has_prior_usage:
            # 仅补齐最终一轮可以确证的缓存分类差值，不能用最终 raw 的缓存量
            # 代替整个 agent 的累计缓存命中，也不修改宿主共享的 TokenUsage。
            cached_delta = token_input_cached - response_usage[1]
            token_input_other, token_input_cached, token_output = total_usage
            cached_delta = min(max(0, cached_delta), token_input_other)
            token_input_other -= cached_delta
            token_input_cached += cached_delta
            cache_read = token_input_cached

        umo = self._get_umo(event)
        conversation_id = self._get_conversation_id(event)
        provider_id, provider_model = await self._get_provider_info(umo, raw, event=event)
        response_id = getattr(resp, "id", None) or _field(raw, "id")
        user_id = _safe_sender_id(event)
        request_id = _read_request_id(event)

        # 计费上下文（service_tier / 1h 缓存判定），供 per_tier / tiered_expr 求值。
        # 全部来自响应侧（ProviderRequest 无 extra_body/headers 可提取，见
        # _extract_billing_context docstring）。
        billing_context = _extract_billing_context(raw, cache_creation_1h)
        if has_prior_usage:
            billing_context["collection"] = {
                "usage_scope": "agent_run",
                "raw_usage_scope": "final_response",
                "cache_creation_scope": "final_response",
            }
        bc_params = billing_context.get("params") if isinstance(billing_context, dict) else None
        bc_params = bc_params if isinstance(bc_params, dict) else {}
        # 1h 缓存写：优先 Anthropic 响应侧 ephemeral_1h 细分；provider 未返回
        # 细分但计费上下文标记了 1h 时，退化为整段 cache_creation 按 1h 计。
        cc1h = int(cache_creation_1h or 0)
        if cc1h <= 0 and bc_params.get("cache_ttl_1h"):
            cc1h = int(cache_creation or 0)

        # 固化原始货币成本金额与符号（展示时按当前汇率换算到主货币）。
        created = datetime.now(UTC)
        usage_dict = {
            "token_input_other": token_input_other,
            "token_input_cached": token_input_cached,
            "token_output": token_output,
            "cache_creation": cache_creation,
            "cache_creation_1h": cc1h,
            "billing_context": billing_context,
            "created_at": created,
        }
        raw_cost: float | None
        try:
            raw_cost, cur = compute_cost_with_currency(
                usage_dict, provider_id, provider_model, self.get_pricing()
            )
        except TieredExprEvaluationError:
            # 表达式失败不得固化为 0；保留 NULL 供后续修正规则后重算/回填。
            raw_cost, cur = None, "USD"
        except Exception as e:
            # 其余计算失败同样不得静默固化为 0 成本（资损防线）。
            logging.getLogger(__name__).warning(
                "[cost_control] 成本计算异常，保留 NULL 待回填: %s", e, exc_info=True
            )
            raw_cost, cur = None, "USD"
        # tiered_expr 命中的阶梯名（tier() 回填到 usage_dict），供审计与调试；
        # 求值失败时把失败类别固化进 billing_context.params（绝不静默归零）。
        matched_tier = usage_dict.pop("_matched_tier", None)
        pricing_period_id = usage_dict.pop("_pricing_period_id", None)
        pricing_period_name = usage_dict.pop("_pricing_period_name", None)
        expr_error = usage_dict.pop("_expr_error", None)
        if expr_error and isinstance(billing_context, dict):
            billing_context.setdefault("params", {})["expr_error"] = str(expr_error)

        return {
            "umo": umo,
            "provider_id": provider_id or "",
            "provider_model": provider_model,
            "conversation_id": conversation_id,
            "token_input_other": token_input_other,
            "token_input_cached": token_input_cached,
            "token_output": token_output,
            "cache_creation": cache_creation,
            "cache_read": cache_read,
            "raw_usage": raw_usage,
            "response_id": response_id,
            "request_id": request_id,
            "user_id": user_id,
            "billing_context": billing_context or None,
            "matched_tier": matched_tier,
            "pricing_period_id": pricing_period_id,
            "pricing_period_name": pricing_period_name,
            "cost_amount": round(raw_cost, 6) if raw_cost is not None else None,
            "currency_symbol": cur,
            "created_at": created,
        }

    def _get_umo(self, event: Any) -> str:
        return str(
            getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", None) or ""
        )

    def _get_conversation_id(self, event: Any) -> str | None:
        # on_llm_response 阶段 event 不直接暴露 conversation_id；留空，
        # 后续按 umo + created_at 与 ProviderStat 关联。
        cid = getattr(event, "conversation_id", None)
        return str(cid) if cid else None

    async def _get_provider_info(
        self,
        umo: str | None,
        raw: Any,
        *,
        event: Any = None,
    ) -> tuple[str | None, str | None]:
        captured = getattr(event, "_cost_control_provider", None)
        if captured is not None:
            provider_id, model = captured
            return provider_id, model or _field(raw, "model")
        provider_id: str | None = None
        model: str | None = None
        try:
            prov = self.context.get_using_provider(umo)
            if prov is not None:
                meta = prov.meta()
                provider_id = meta.id
                model = meta.model
        except Exception:
            pass
        if model is None and raw is not None:
            try:
                model = _field(raw, "model")
            except Exception:
                model = None
        return provider_id, model

    def record_request_provider(self, event: Any, req: Any) -> None:
        """在请求尾部固定 Provider/模型，防止等待响应期间切换模型导致记错账。"""
        try:
            prov = self.context.get_using_provider(self._get_umo(event))
            meta = prov.meta() if prov is not None else None
            model = getattr(req, "model", None) or getattr(meta, "model", None)
            event._cost_control_provider = (getattr(meta, "id", None), model)
        except Exception:
            event._cost_control_provider = None

    def ensure_request_id(self, event: Any) -> None:
        """为一次用户请求（pipeline）生成 request_id 并挂到 event（幂等、绝不抛异常）。

        在 ``on_llm_request_head``（最高优先级）调用：若 event 还没有
        ``_cost_control_request_id`` 则生成 ``cc_<16hex>`` 并 setattr。后续同 event
        的多次 LLM 调用（function-calling 多步）复用同一值，供 per_request 按请求计数。

        假设：AstrBot pipeline 在一次用户消息内复用同一 event 对象。若实际每次 clone
        新 event，request_id 退化为每次调用各一个（等同于 per_turn）——降级可接受。
        """
        try:
            if getattr(event, "_cost_control_request_id", None):
                return
            rid = f"cc_{uuid.uuid4().hex[:16]}"
            try:
                setattr(event, "_cost_control_request_id", rid)
            except Exception:
                pass
        except Exception:
            pass
