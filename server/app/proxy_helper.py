"""KuaiDaili tunnel proxy helper — auto-fetch proxy via API."""
import time
import logging
import httpx
from app.config import settings

logger = logging.getLogger("proxy")

# Cache
_cached_proxy: str | None = None
_cached_at: float = 0
CACHE_TTL = 300  # 5 minutes


async def _get_token(client: httpx.AsyncClient) -> str | None:
    """Get secret_token from KDL API."""
    r = await client.post(
        "https://dev.kdlapi.com/api/get_secret_token",
        data={"secret_id": settings.KDL_SECRET_ID, "secret_key": settings.KDL_SECRET_KEY},
    )
    data = r.json()
    if data.get("code") != 0:
        logger.warning(f"KDL get_secret_token failed: {data}")
        return None
    return data["data"]["secret_token"]


async def get_proxy_url() -> str | None:
    """Get a usable HTTP proxy URL.

    Priority:
      1. KuaiDaili API (dynamic, auto-refresh)
      2. Static PROXY_URL from env
      3. None
    """
    global _cached_proxy, _cached_at

    # Static proxy takes precedence if configured
    if settings.PROXY_URL:
        return settings.PROXY_URL

    if not settings.KDL_SECRET_ID or not settings.KDL_SECRET_KEY:
        return None

    # Return cached proxy if fresh
    if _cached_proxy and (time.time() - _cached_at) < CACHE_TTL:
        return _cached_proxy

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token = await _get_token(client)
            if not token:
                return _cached_proxy

            # Get tunnel proxy address
            r = await client.get(
                "https://tps.kdlapi.com/api/gettps",
                params={
                    "secret_id": settings.KDL_SECRET_ID,
                    "signature": token,
                    "num": 1,
                    "format": "json",
                },
            )
            data = r.json()
            if data.get("code") != 0 or not data.get("data", {}).get("proxy_list"):
                logger.warning(f"KDL gettps failed: {data}")
                return _cached_proxy

            host_port = data["data"]["proxy_list"][0]

            # Get auth credentials
            r = await client.get(
                "https://dev.kdlapi.com/api/getproxyauthorization",
                params={
                    "secret_id": settings.KDL_SECRET_ID,
                    "signature": token,
                    "plaintext": 1,
                },
            )
            auth_data = r.json()
            if auth_data.get("code") != 0:
                logger.warning(f"KDL getproxyauthorization failed: {auth_data}")
                return _cached_proxy

            username = auth_data["data"]["username"]
            password = auth_data["data"]["password"]

            _cached_proxy = f"http://{username}:{password}@{host_port}"
            _cached_at = time.time()
            logger.info(f"KDL tunnel proxy: {_cached_proxy}")
            return _cached_proxy

    except Exception as e:
        logger.warning(f"KDL API error: {e}")
        return _cached_proxy
