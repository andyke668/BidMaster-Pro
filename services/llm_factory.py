from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm_gateway.gateway import LLMGateway
from core.settings import get_settings

logger = logging.getLogger(__name__)

_gateway: LLMGateway | None = None
# 按 (来源, 配置, 模型) 缓存的智能体专用网关；配置变更（updated_at 变化）自动生成新实例
_agent_gateways: dict[tuple, LLMGateway] = {}


def get_llm_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        settings = get_settings()
        _gateway = LLMGateway({
            "providers": [
                {
                    "api_key": settings.llm_api_key,
                    "api_base": settings.llm_api_base,
                }
            ],
            "default_model": settings.llm_default_model,
            "fallback_models": [m.strip() for m in settings.llm_fallback_modes.split(",") if m.strip()],
            "max_retries": settings.llm_max_retries,
        })
    return _gateway


def reset_llm_gateway():
    global _gateway
    _gateway = None
    _agent_gateways.clear()


async def get_agent_gateway(db: AsyncSession, agent_name: str) -> LLMGateway:
    """按「智能体模型配置」解析该 Agent 实际使用的网关。

    AgentConfig.config["model"] 形如 "provider_id/model_name"：
    - 未配置模型 -> 默认网关（环境变量配置）；
    - provider_id 命中已启用的 LLMProviderConfig -> 用该配置的 api_base/api_key + 所选模型构建网关；
    - 未命中（如预设供应商未落库）-> 默认网关凭证 + 所选模型名覆盖。
    """
    from services.models import AgentConfig, LLMProviderConfig

    settings = get_settings()
    try:
        result = await db.execute(select(AgentConfig).where(AgentConfig.name == agent_name))
        agent = result.scalar_one_or_none()
    except Exception as e:
        logger.warning(f"[llm_factory] 读取 AgentConfig({agent_name}) 失败，回落默认网关: {e}")
        return get_llm_gateway()

    if agent is None or not agent.enabled:
        return get_llm_gateway()

    model = str((agent.config or {}).get("model") or "").strip()
    if not model:
        return get_llm_gateway()

    provider_id, _, model_name = model.partition("/")
    if not model_name:
        provider_id, model_name = "", provider_id

    cfg = None
    if provider_id:
        try:
            result = await db.execute(
                select(LLMProviderConfig)
                .where(
                    LLMProviderConfig.provider_id == provider_id,
                    LLMProviderConfig.enabled == True,
                )
                .order_by(LLMProviderConfig.is_default.desc(), LLMProviderConfig.created_at)
            )
            cfg = result.scalars().first()
        except Exception as e:
            logger.warning(f"[llm_factory] 读取 LLMProviderConfig({provider_id}) 失败，回落默认凭证: {e}")

    if cfg is not None and cfg.api_key:
        api_base = cfg.api_base or settings.llm_api_base
        cache_key = ("cfg", cfg.id, model_name, api_base, str(cfg.updated_at))
        cached = _agent_gateways.get(cache_key)
        if cached is not None:
            return cached
        gw = LLMGateway({
            "providers": [{"api_key": cfg.api_key, "api_base": api_base}],
            "default_model": model_name,
            "fallback_models": [],
            "max_retries": settings.llm_max_retries,
        })
        if len(_agent_gateways) > 128:
            _agent_gateways.clear()
        _agent_gateways[cache_key] = gw
        logger.info(f"[llm_factory] agent={agent_name} 使用供应商配置 provider={provider_id} model={model_name} base={api_base}")
        return gw

    cache_key = ("env", model_name, settings.llm_api_base)
    cached = _agent_gateways.get(cache_key)
    if cached is not None:
        return cached
    gw = LLMGateway({
        "providers": [{"api_key": settings.llm_api_key, "api_base": settings.llm_api_base}],
        "default_model": model_name,
        "fallback_models": [],
        "max_retries": settings.llm_max_retries,
    })
    if len(_agent_gateways) > 128:
        _agent_gateways.clear()
    _agent_gateways[cache_key] = gw
    logger.info(f"[llm_factory] agent={agent_name} 使用默认凭证 + 模型覆盖 model={model_name}")
    return gw