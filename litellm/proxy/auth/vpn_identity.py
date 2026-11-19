from hmac import compare_digest
import os
import re
from typing import Final

from starlette.requests import HTTPConnection


VPN_USER_ID: Final = re.compile(r"vpn-[0-9a-f]{32}")


def trusted_vpn_user(connection: HTTPConnection) -> str | None:
    secret: Final = os.getenv("LITELLM_VPN_PROXY_SECRET")
    supplied: Final = connection.headers.getlist("x-vpn-proxy-secret")
    users: Final = connection.headers.getlist("x-vpn-user-id")
    if not secret or len(supplied) != 1 or not compare_digest(secret.encode(), supplied[0].encode()):
        return None
    if len(users) != 1 or not VPN_USER_ID.fullmatch(users[0]):
        return None
    return users[0]


def vpn_identity_matches(connection: HTTPConnection, user_id: str | None) -> bool:
    if not os.getenv("LITELLM_VPN_PROXY_SECRET") or user_id is None or not VPN_USER_ID.fullmatch(user_id):
        return True
    return trusted_vpn_user(connection) == user_id
