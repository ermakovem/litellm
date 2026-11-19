from asyncio import to_thread
from datetime import datetime
import os
from functools import lru_cache
from typing import Final, Mapping, cast

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.management_endpoints.subscription_quota import _ledger, shared_quota_enabled
from litellm.types.utils import StandardLoggingPayload


class ModelQuota(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str = Field(min_length=1)
    input_weight: float = Field(gt=0, allow_inf_nan=False)
    cached_input_weight: float = Field(ge=0, allow_inf_nan=False)
    output_weight: float = Field(gt=0, allow_inf_nan=False)


@lru_cache(maxsize=1)
def _models(config: str) -> dict[str, ModelQuota]:
    return TypeAdapter(dict[str, ModelQuota]).validate_json(config)


def _policy(model: object) -> ModelQuota | None:
    config: Final = os.getenv("LITELLM_SUBSCRIPTION_QUOTA_MODELS", "{}")
    return _models(config).get(model) if isinstance(model, str) else None


class SubscriptionQuotaLogger(CustomLogger):
    async def async_pre_call_hook(
        self, user_api_key_dict: UserAPIKeyAuth, cache: object, data: dict[str, object], call_type: str
    ) -> None:
        if shared_quota_enabled():
            return
        policy: Final = _policy(data.get("model"))
        if policy is None:
            return
        if not user_api_key_dict.user_id:
            raise HTTPException(403, "A user account is required")
        windows: Final = await to_thread(_ledger().usage, user_api_key_dict.user_id)
        matching: Final = [item for item in windows if item.source == policy.source]
        if not matching:
            raise HTTPException(403, "No active quota is assigned for this model")
        now: Final = datetime.now().timestamp()
        if any(window.resets_at <= now or window.observed_at < now - 180 for window in matching):
            raise HTTPException(503, "The subscription quota snapshot is stale")
        if any(window.remaining_percent <= 0 or window.provider_remaining_percent <= 0 for window in matching):
            raise HTTPException(429, "Subscription quota exhausted")

    async def async_log_success_event(
        self, kwargs: Mapping[str, object], response_obj: object, start_time: datetime, end_time: datetime
    ) -> None:
        if shared_quota_enabled():
            return
        raw: Final = kwargs.get("standard_logging_object")
        if not isinstance(raw, dict):
            return
        event: Final = cast(StandardLoggingPayload, raw)
        policy: Final = _policy(event.get("model_group") or event.get("model"))
        user_id: Final = event.get("metadata", {}).get("user_api_key_user_id")
        request_id: Final = event.get("litellm_call_id") or event.get("id")
        if not policy or not user_id or not request_id or event.get("cache_hit"):
            return
        response_usage: Final = getattr(response_obj, "usage", None)
        details: Final = getattr(response_usage, "prompt_tokens_details", None) or getattr(
            response_usage, "input_tokens_details", None
        )
        prompt_tokens: Final = event.get("prompt_tokens", 0)
        cached: Final = min(prompt_tokens, getattr(details, "cached_tokens", 0) or 0)
        weight: Final = (
            (prompt_tokens - cached) * policy.input_weight
            + cached * policy.cached_input_weight
            + event.get("completion_tokens", 0) * policy.output_weight
        )
        if weight > 0:
            await to_thread(_ledger().record, request_id, policy.source, user_id, event["endTime"], weight)


subscription_quota_callback = SubscriptionQuotaLogger()
