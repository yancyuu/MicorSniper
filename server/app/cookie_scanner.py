"""Auto-detect logged-in sites from the user's browser cookies."""
import logging
import re

logger = logging.getLogger("cookie_scanner")

# Auth cookie name patterns — if any cookie name matches, likely logged in
_AUTH_PATTERNS = re.compile(
    r"(token|session|sess|auth|sid|login|user_id|uid|pass|account|csrf|jwt|refresh)",
    re.IGNORECASE,
)

# Domains to skip (not useful for crawling)
_SKIP_DOMAINS = {
    "google.com", "youtube.com", "bing.com", "doubleclick.net",
    "googleadservices.com", "facebook.com", "twitter.com",
    "hm.baidu.com", "baidu.com", "sensorsdata.cn",
}

# Nice labels for common Chinese sites
_LABELS: dict[str, str] = {
    "taobao.com": "淘宝",
    "tmall.com": "天猫",
    "xiaohongshu.com": "小红书",
    "jd.com": "京东",
    "douyin.com": "抖音",
    "bilibili.com": "B站",
    "weibo.com": "微博",
    "pinduoduo.com": "拼多多",
    "feishu.cn": "飞书",
    "aliyun.com": "阿里云",
    "xiaomi.com": "小米",
    "bigmodel.cn": "智谱AI",
    "kuaidaili.com": "快代理",
    "picsart.com": "Picsart",
}


def _root_domain(domain: str) -> str:
    """Extract root domain: '.www.taobao.com' → 'taobao.com'."""
    d = domain.lstrip(".")
    parts = d.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return d


def _is_logged_in(cookies: list[dict]) -> bool:
    """Heuristic: does this set of cookies indicate a logged-in session?"""
    if len(cookies) < 3:
        return False

    # Check for auth-pattern cookie names
    for c in cookies:
        name = c.get("name", "")
        if _AUTH_PATTERNS.search(name):
            return True

    # Check for httpOnly cookies (usually set by server after auth)
    if any(c.get("httpOnly") for c in cookies):
        return True

    return False


def scan_browser_cookies() -> list[dict]:
    """Scan browser cookies and detect logged-in sites.

    Returns a list of detected sessions:
    [{name, url, label, cookie_count, auth_cookies_found}]
    """
    try:
        import rookiepy
    except ImportError:
        logger.warning("rookiepy not installed, cannot scan browser cookies")
        return []

    all_cookies: list[dict] = []
    # Try Chrome first, then Brave, then Edge
    for browser_fn in [rookiepy.chrome, rookiepy.brave, rookiepy.edge]:
        try:
            cookies = browser_fn()
            if cookies:
                all_cookies.extend(cookies)
        except Exception:
            pass

    if not all_cookies:
        return []

    # Group cookies by root domain
    domain_cookies: dict[str, list[dict]] = {}
    for c in all_cookies:
        d = c.get("domain", "")
        root = _root_domain(d)
        domain_cookies.setdefault(root, []).append(c)

    # Detect logged-in sites
    detected = []
    for root, cookies in domain_cookies.items():
        if root in _SKIP_DOMAINS:
            continue
        if not _is_logged_in(cookies):
            continue

        # Find auth cookie names for display
        auth_found = [
            c.get("name", "") for c in cookies
            if _AUTH_PATTERNS.search(c.get("name", ""))
        ]

        label = _LABELS.get(root, root)
        name = root.replace(".", "-")

        detected.append({
            "name": name,
            "url": f"https://www.{root}",
            "label": label,
            "cookie_count": len(cookies),
            "auth_cookies_found": auth_found[:5],
            "cookies": cookies,
        })

    detected.sort(key=lambda s: s["cookie_count"], reverse=True)
    logger.info(f"Detected {len(detected)} logged-in sites: {[s['label'] for s in detected]}")
    return detected
