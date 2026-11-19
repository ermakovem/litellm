from asyncio import to_thread
from dataclasses import asdict
from functools import lru_cache
import os
from pathlib import Path
import time
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.repositories.subscription_quota import QuotaLedger, Snapshot

router = APIRouter(prefix="/subscription-quota", tags=["Subscription quota"])
Auth = Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)]


@lru_cache(maxsize=1)
def _open_ledger(path: str) -> QuotaLedger:
    return QuotaLedger(Path(path))


def _ledger() -> QuotaLedger:
    path: Final = os.getenv("LITELLM_SUBSCRIPTION_QUOTA_DB")
    if not path:
        raise HTTPException(404, "Subscription quota is disabled")
    return _open_ledger(path)


class ShareInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str = Field(min_length=1, max_length=100)
    user_id: str = Field(min_length=1, max_length=128)
    percent: float = Field(ge=0, le=100, allow_inf_nan=False)


class SnapshotInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str = Field(min_length=1, max_length=100)
    window: str = Field(min_length=1, max_length=50)
    used_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    resets_at: int = Field(gt=0)
    observed_at: int = Field(gt=0)


def _admin(auth: UserAPIKeyAuth) -> None:
    if auth.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(403, "Proxy administrator required")


def shared_quota_enabled() -> bool:
    return os.getenv("LITELLM_SUBSCRIPTION_QUOTA_MODE") == "shared"


def _require_share_mode() -> None:
    if shared_quota_enabled():
        raise HTTPException(409, "This gateway uses shared subscription quota")


@router.get("/me")
async def my_quota(auth: Auth, response: Response):
    if not auth.user_id:
        raise HTTPException(403, "A user account is required")
    ledger: Final = _ledger()
    if shared_quota_enabled():
        response.headers["X-Subscription-Quota-Mode"] = "shared"
        windows_shared: Final = await to_thread(ledger.shared_usage)
        return [asdict(window) | {"shared": True} for window in windows_shared]
    windows: Final = await to_thread(ledger.usage, auth.user_id)
    return [
        {
            "source": window.source,
            "window": window.window,
            "remaining_percent": window.remaining_percent,
            "resets_at": window.resets_at,
            "observed_at": window.observed_at,
        }
        for window in windows
    ]


@router.get("/shares")
async def list_shares(auth: Auth):
    _admin(auth)
    _require_share_mode()
    ledger: Final = _ledger()
    rows: Final = await to_thread(ledger.shares)
    return [
        {
            "source": source,
            "user_id": user_id,
            "percent": percent,
            "windows": [asdict(window) for window in await to_thread(ledger.usage, user_id) if window.source == source],
        }
        for source, user_id, percent in rows
    ]


@router.put("/shares")
async def update_share(data: ShareInput, auth: Auth):
    _admin(auth)
    _require_share_mode()
    try:
        await to_thread(_ledger().set_share, data.source, data.user_id, data.percent)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return data


@router.put("/snapshot")
async def update_snapshot(data: SnapshotInput, auth: Auth):
    _admin(auth)
    if data.observed_at > time.time() + 5:
        raise HTTPException(422, "The observation time must not be in the future")
    try:
        await to_thread(
            _ledger().observe, data.source, data.window,
            Snapshot(data.used_percent, data.resets_at, data.observed_at),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": True}
