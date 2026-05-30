"""Smart generation — let LLM parse natural language into URL + goal + session."""
from __future__ import annotations
import json
import re
import httpx
from app.config import settings
from app.store import STORE
from app.engine.agent import agent_generate


async def _llm_parse(prompt: str, sessions: list[dict]) -> dict:
    """Ask LLM to extract url, goal, and matching session from NL input."""
    session_list = "\n".join(
        f"  - {s['name']} ({s['url']}, {s['cookie_count']} cookies)"
        for s in sessions
    ) or "  (无已登录网站)"

    system_msg = f"""你是一个爬虫任务解析助手。从用户的自然语言中提取:
1. url: 目标网站URL（根据描述推断，拼好搜索参数）
2. goal: 具体的采集目标描述
3. session: 如果目标网站在已登录列表中有匹配，填对应的name；否则填null

已登录网站:
{session_list}

常见网站: 淘宝→taobao.com, 京东→jd.com, 小红书→xiaohongshu.com, 抖音→douyin.com, B站→bilibili.com, 小米→xiaomi.com, 飞书→feishu.cn

严格返回JSON，不要其他文字:
{{"url": "...", "goal": "...", "session": "name或null}}"""

    base_url = settings.LLM_BASE_URL or "https://api.openai.com/v1"
    api_key = settings.OPENAI_API_KEY
    url = f"{base_url.rstrip('/')}/chat/completions"
    model = settings.LLM_PROVIDER.split("/", 1)[-1] if "/" in settings.LLM_PROVIDER else "qwen-turbo"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()

    # Extract JSON from response
    json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
    if not json_match:
        raise ValueError(f"LLM returned no JSON: {content[:200]}")

    return json.loads(json_match.group(0))


async def smart_generate(prompt: str) -> dict:
    """Parse NL → URL + goal + session via LLM, then generate node graph."""
    import logging
    logger = logging.getLogger("smart_generate")

    # Load available sessions
    stored = await STORE.list_sessions()
    sessions = [{"name": s.name, "url": s.url, "cookie_count": len(s.cookies)} for s in stored]

    # Let LLM parse everything
    try:
        parsed = await _llm_parse(prompt, sessions)
    except Exception as e:
        logger.warning(f"LLM parse failed: {e}")
        return {
            "error": True,
            "message": f"无法理解你的输入，请尝试更明确的描述（包含网站名称和要采集的内容）。\n\n错误: {e}",
        }

    url = parsed.get("url", "").strip()
    goal = parsed.get("goal", "").strip()
    session_name = parsed.get("session")

    if not url:
        return {
            "error": True,
            "message": f"无法识别目标网站。请明确指定，例如：\"爬取淘宝上手机壳的价格\"\n\n你的输入: {prompt}",
        }

    # Validate session exists
    if session_name and not any(s.name == session_name for s in stored):
        logger.warning(f"LLM picked session '{session_name}' but not found, ignoring")
        session_name = None

    logger.info(f"NL parse: {prompt!r} → url={url}, goal={goal}, session={session_name}")

    result = await agent_generate(url, goal or prompt, session_name)
    result["parsed_url"] = url
    result["parsed_goal"] = goal or prompt
    if session_name:
        result["matched_session"] = session_name
    return result
