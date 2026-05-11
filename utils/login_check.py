# -*- coding: utf-8 -*-
"""登录态校验：通过 agent.extract（AI 视觉理解）检测浏览器登录状态。"""

import asyncio
from dataclasses import dataclass

from agentbay import ExtractOptions
from utils.logger import logger

_PLATFORM_HOME_URLS = {
    "jd": "https://www.jd.com",
    "taobao": "https://www.taobao.com",
    "tmall": "https://www.tmall.com",
    "1688": "https://www.1688.com",
}

_PLATFORM_LOGIN_PROMPTS = {
    "jd": "请查看当前页面，判断京东网站是否处于已登录状态。已登录的标志是页面顶部显示用户名、'你好xxx'、或用户头像区域。未登录则显示'你好请登录'、'免费注册'等链接。",
    "taobao": "请查看当前页面，判断淘宝网站是否处于已登录状态。已登录的标志是页面顶部显示用户昵称、'亲，xxx'、或用户头像区域。未登录则显示'亲，请登录'、'免费注册'等链接。",
    "tmall": "请查看当前页面，判断天猫网站是否处于已登录状态。已登录的标志是页面顶部显示用户昵称或用户头像区域。未登录则显示'请登录'、'免费注册'等链接。",
    "1688": "请查看当前页面，判断1688网站是否处于已登录状态。已登录的标志是页面顶部显示公司名、用户名、或'已登录'提示。未登录则显示'请登录'、'免费注册'、'加入1688'等链接。",
}


@dataclass
class LoginCheckResult:
    logged_in: bool
    platform: str
    reason: str = ""


async def check_login_status(agent, platform: str, timeout: int = 30) -> LoginCheckResult:
    """导航到平台首页后用 agent.extract 检测登录态。

    Returns:
        LoginCheckResult: logged_in=True 表示已登录，False 表示未登录或检测失败。
    """
    home_url = _PLATFORM_HOME_URLS.get(platform)
    if not home_url:
        logger.warning(f"[login_check] Unknown platform '{platform}', skipping check")
        return LoginCheckResult(logged_in=True, platform=platform, reason="unknown platform, skip")

    prompt = _PLATFORM_LOGIN_PROMPTS.get(platform, f"请判断{platform}网站是否处于已登录状态。")
    instruction = f"{prompt} 请用JSON格式回答：{{\"logged_in\": true/false, \"reason\": \"简短说明\"}}"

    try:
        await agent.navigate(home_url)
        await asyncio.sleep(3)

        result = await agent.extract(
            ExtractOptions(
                instruction=instruction,
                schema=LoginCheckResult,
                use_vision=True,
                timeout=timeout,
            )
        )

        if hasattr(result, "logged_in") and isinstance(result.logged_in, bool):
            return LoginCheckResult(
                logged_in=result.logged_in,
                platform=platform,
                reason=getattr(result, "reason", ""),
            )

        # AI 返回了不确定结果
        logger.warning(f"[login_check] Ambiguous result for {platform}: {result}")
        return LoginCheckResult(logged_in=False, platform=platform, reason="检测结果不确定")

    except asyncio.TimeoutError:
        logger.warning(f"[login_check] Timeout checking login for {platform}")
        return LoginCheckResult(logged_in=False, platform=platform, reason="检测超时")
    except Exception as e:
        logger.warning(f"[login_check] Failed to check login for {platform}: {e}")
        return LoginCheckResult(logged_in=False, platform=platform, reason=str(e))


_PLATFORM_LABELS = {"jd": "京东", "taobao": "淘宝", "tmall": "天猫", "1688": "1688"}


def login_failed_message(platform: str) -> str:
    label = _PLATFORM_LABELS.get(platform, platform)
    return f"登录态已失效，请重新登录{label}"
