# -*- coding: utf-8 -*-
"""AgentBay act/extract/crawl orchestration for CLI mode."""

from typing import Any

from agentbay import (
    ActOptions,
    AsyncAgentBay,
    BrowserFingerprint,
    BrowserOption,
    BrowserScreen,
    ExtractOptions,
)

from config.settings import global_settings
from models.context import BrowserContext, ContextStatus
from models.task import Task

from .runtime import create_browser_session
from .schema import (
    AgentExtractResult,
    build_extract_instruction,
    extract_mode_kwargs,
    normalize_extract_result,
)


def _record_count(outputs: list[dict[str, Any]]) -> int:
    return sum(len(item.get("records") or []) for item in outputs if isinstance(item, dict))


def browser_option() -> BrowserOption:
    return BrowserOption(
        screen=BrowserScreen(width=1920, height=1080),
        solve_captchas=True,
        use_stealth=True,
        fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
    )


async def agent_recover_action(agent, task: Task, step: int, reason: str) -> None:
    """Generic Agent recovery action without site-specific DOM rules."""
    action = "检查当前页面状态，关闭遮挡弹窗；如果页面异常则刷新或返回可操作页面，然后继续围绕目标浏览。"
    try:
        await agent.act(ActOptions(action=action))
        await task.log_step(step, "Agent 自愈动作", {"reason": reason, "action": action}, {"status": "ok"}, "completed")
    except Exception as exc:
        await task.log_step(step, "Agent 自愈动作失败", {"reason": reason, "action": action}, {"error": str(exc)}, "failed")


async def agent_extract(agent, *, goal: str, timeout: int, mode: str):
    ok, payload = await agent.extract(
        ExtractOptions(
            instruction=build_extract_instruction(goal),
            schema=AgentExtractResult,
            timeout=timeout,
            **extract_mode_kwargs(mode),
        )
    )
    result = normalize_extract_result(payload) if ok and payload else {"ok": ok, "error": "extract returned empty payload"}
    return ok, payload, result


async def agent_crawl(
    ctx: BrowserContext,
    start_url: str,
    goal: str,
    max_steps: int,
    extract_timeout: int = 120,
    extract_mode: str = "both",
) -> dict[str, Any]:
    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
    session_result = await create_browser_session(agent_bay, ctx, kind="cli-agent-crawl", auto_upload=False)
    if not session_result.success or not session_result.session:
        raise RuntimeError(f"Failed to create session: {session_result.error_message}")
    session = session_result.session
    task = await Task.create(
        source="cli",
        source_id="craw-agent",
        task_type="agent_crawl",
        context_id=ctx.id,
        params={"start_url": start_url, "goal": goal, "max_steps": max_steps, "extract_mode": extract_mode},
    )
    outputs: list[dict[str, Any]] = []
    try:
        task.browser_url = session.resource_url or ""
        await task.start()
        await task.save()
        await session.browser.initialize(browser_option())
        agent = session.browser.agent
        await agent.navigate(start_url)
        await task.log_step(1, "打开起始页面", {"url": start_url, "goal": goal}, {}, "completed")

        for step in range(1, max_steps + 1):
            try:
                ok, payload, data = await agent_extract(agent, goal=goal, timeout=extract_timeout, mode=extract_mode)
            except Exception as exc:
                await task.log_step(
                    step + 1,
                    "Agent 抽取异常",
                    {"goal": goal, "timeout": extract_timeout},
                    {"error": str(exc)},
                    "failed",
                )
                await agent_recover_action(agent, task, step + 1_000, f"extract exception: {exc}")
                continue
            if not ok or payload is None:
                await task.log_step(step + 1, "Agent 抽取失败", {"goal": goal}, {"ok": ok}, "failed")
                await agent_recover_action(agent, task, step + 1_000, "extract failed")
                continue

            outputs.append(data)
            await task.log_step(step + 1, f"Agent 抽取第 {step} 轮", {"goal": goal}, data, "completed")
            if payload.done:
                break

            action = payload.next_action or "围绕目标继续浏览当前网站，优先打开可能包含所需信息的链接或区域。"
            try:
                await agent.act(ActOptions(action=action))
            except Exception as exc:
                await task.log_step(step + 1_000, "Agent 自愈动作", {"action": action}, {"error": str(exc)}, "failed")
                await agent_recover_action(agent, task, step + 2_000, str(exc))

        total = _record_count(outputs)
        result = {"task_id": str(task.id), "goal": goal, "rounds": len(outputs), "total": total, "success": total, "outputs": outputs}
        await task.complete(result)
        return result
    except Exception as exc:
        await task.fail(str(exc))
        raise
    finally:
        try:
            await agent_bay.delete(session, sync_context=False)
        finally:
            ctx.status = ContextStatus.LOGGED_IN.value
            await ctx.save()
