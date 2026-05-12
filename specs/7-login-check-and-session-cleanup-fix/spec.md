# Feature Specification: 修复登录态校验误判与会话清理泄露

## Overview

修复 #6 "抓取任务增加登录态校验" 上线后引入的两个缺陷：
1. 登录态校验逻辑始终返回"未登录"，导致所有抓取任务在登录态正常的情况下也被立刻中止。
2. 校验失败后 `finally` 清理顺序异常，AgentBay 浏览器会话未被删除，造成云端资源泄露与配额占用。

## Problem Statement

### 缺陷 1：登录态校验永远误判为未登录

`utils/login_check.py` 中 `check_login_status` 与 AgentBay SDK 的 `agent.extract` API 接口不匹配，导致 AI 视觉理解实际上从未成功执行：

- **接入错误 A：Schema 类型不兼容**
  `LoginCheckResult` 被声明为 `@dataclass`，但 AgentBay SDK（`agentbay/_async/browser_agent.py:_execute_extract`）内部通过 `options.schema.model_json_schema()` 和 `options.schema.model_validate(...)` 调用 Pydantic 专有 API。dataclass 不支持这两个方法，调用即抛 `AttributeError`，被 SDK 包装成 `BrowserError`，再被 `login_check.py` 顶层 `except Exception` 捕获，返回 `logged_in=False, reason="..."`。

- **接入错误 B：返回值结构误用**
  `agent.extract` 的签名是 `Tuple[bool, T]`（成功标志 + 解析结果），但当前代码把整个 tuple 赋给 `result` 后直接 `hasattr(result, "logged_in")`，tuple 没有该属性，落入"AI 返回了不确定结果"分支，同样返回 `logged_in=False`。

两个错误叠加：即便修复 A，B 还会让逻辑继续误判。

### 缺陷 2：失败时 AgentBay 会话未关闭

所有抓取脚本（`jd_link_search.py` / `taobao_link_search.py` / `tmall_link_search.py` / `search_by_urls.py` / `product_detail_fetch.py`）的 `finally` 块都先关 Playwright `browser`，再删 AgentBay `session`：

```python
finally:
    try:
        if browser:
            cdp = await browser.new_browser_cdp_session()
            await cdp.send("Browser.close")
            await asyncio.sleep(0.5)
            await browser.close()
    except Exception as e:
        logger.warning(...)

    try:
        await asyncio.wait_for(agent_bay.delete(session, sync_context=False), timeout=30)
    except Exception:
        ...
```

关 browser 的代码块没有 timeout。当 `agent.extract` 调用失败（如缺陷 1 中的 AttributeError 路径）后，CDP/WebSocket 通道可能已经处于异常状态，`cdp.send("Browser.close")` 或 `browser.close()` 会无限期挂住，后续的 `agent_bay.delete()` 永远跑不到，AgentBay 云端会话因此持续存在，直到 AgentBay 自身的空闲回收策略生效（可能数分钟到数十分钟）。

## Goals

- 登录态校验在已登录的浏览器上下文中应正确返回 `logged_in=True`，AI extract 真实生效。
- 校验未通过（含校验过程异常）时，任务能正常失败，并在合理时间内（≤ 60 秒）释放 AgentBay 会话。
- 缺陷修复后不破坏现有 #6 spec 的所有验收条件。

## Non-Goals

- 不改变 AI 视觉理解的判断逻辑/提示词内容（除非为适配 Pydantic schema 必须微调）。
- 不调整批次编排、上下文锁定、断点续跑等周边逻辑。
- 不引入新的登录态自动恢复/重试机制（#6 已明确"未登录时由用户手动重登"）。

## User Scenarios & Testing

### Primary Scenario 1：登录态正常，校验通过

1. 用户已在某 `BrowserContext` 上完成登录，启动一个抓取任务。
2. 脚本进入登录态校验步骤，调用 `check_login_status`。
3. AI 视觉理解返回 `logged_in=True`。
4. 任务继续执行后续抓取流程。
5. 任务正常完成，AgentBay 会话在 `finally` 中被删除。

### Primary Scenario 2：登录态失效，任务中止且资源释放

1. 上下文 cookie 过期或被清空，抓取任务启动。
2. 登录态校验返回 `logged_in=False`。
3. 任务被标记为 `failed`，错误信息为 "登录态已失效，请重新登录 [平台]"。
4. `finally` 内 60 秒内删除 AgentBay 会话，AgentBay 控制台中该 session 状态变为已释放。

### Edge Cases

- **AI extract 调用抛异常**：登录态校验整体视为未通过，任务失败，会话仍要释放。
- **Playwright `browser.close()` 阻塞**：超时后跳过浏览器关闭，确保后续的 `agent_bay.delete()` 仍能执行。
- **AgentBay delete 自身超时/失败**：记录 warning，不影响任务最终状态写库。
- **校验过程中 navigate/extract 超时**：按未登录处理。

## Functional Requirements

### FR-1: `agent.extract` 返回值正确解构

**Description**: `check_login_status` 必须按 `Tuple[bool, T]` 解构 `agent.extract` 的返回值，并以 `bool` 标志决定后续分支。

**Acceptance Criteria**:
- 代码以 `success, payload = await agent.extract(...)`（或等价方式）解构。
- `success=False` 时按未登录处理并记录原因。
- `success=True` 时从 `payload.logged_in` 读取布尔结果。

### FR-2: extract schema 使用 Pydantic 模型

**Description**: 传给 `ExtractOptions(schema=...)` 的类型必须是 Pydantic `BaseModel` 子类，支持 `model_json_schema()` 与 `model_validate()`。

**Acceptance Criteria**:
- 为登录态校验定义一个 Pydantic schema（字段至少包含 `logged_in: bool` 与 `reason: str`）。
- 业务侧暴露的 `LoginCheckResult` 保留现有 dataclass 形态（或同样改 Pydantic），但确保 `check_login_status` 返回值字段名/语义不变，调用方无需改动。
- 提示词与 schema 字段一致，避免 AI 输出与 schema 字段不匹配。

### FR-3: 清理顺序保证会话一定释放

**Description**: 抓取脚本 `finally` 块必须保证：无论 Playwright 浏览器关闭是否成功/挂起，AgentBay 会话都能在合理超时内被删除。

**Acceptance Criteria**:
- 包裹 Playwright 关闭逻辑的代码具备总超时（建议 ≤ 10 秒），超时后放弃关闭并继续后续清理，记录 warning。
- AgentBay `agent_bay.delete(session, ...)` 的调用不被前序步骤阻塞，且自身带 timeout（沿用现有 30 秒或更短）。
- 任一清理步骤异常都不应抛出到 `finally` 之外。
- 覆盖以下脚本：`jd_link_search.py` / `taobao_link_search.py` / `tmall_link_search.py` / `search_by_urls.py` / `product_detail_fetch.py`。

### FR-4: 校验失败日志保留可诊断信息

**Description**: 当 extract 失败（schema 报错、超时、返回 success=False 等）时，任务日志应包含足够的失败原因，便于排查 SDK 接口变更或 AI 输出异常。

**Acceptance Criteria**:
- `logger.warning` 输出包含平台、失败类型与原始 exception/reason 字段。
- 任务 `log_step` 的 output 中 `reason` 字段非空。

## Success Criteria

- 在已登录上下文中跑任一抓取任务，登录态校验步骤成功通过（`logged_in=True`）。
- 在登录态故意失效的上下文中跑抓取任务，任务在合理时间（≤ 90 秒，包含 extract 默认轮询周期）内进入 `failed` 状态，且 AgentBay 控制台对应 session 在 60 秒内消失。
- 现有 #6 spec 的全部验收用例继续通过。
- `ruff check .` / `black --check .` 在改动文件上无新增告警。

## Key Entities

| Entity                | Description                                                   |
|-----------------------|---------------------------------------------------------------|
| LoginExtractSchema    | 供 `agent.extract` 使用的 Pydantic 模型，字段 `logged_in`、`reason` |
| LoginCheckResult      | `check_login_status` 对外返回的业务结果对象，字段保持向后兼容       |
| AgentBay Session 清理 | 保证 `agent_bay.delete(session)` 始终被调用的 `finally` 清理路径    |

## Assumptions

- AgentBay SDK 版本短期内不会再调整 `agent.extract` 的签名（仍为 `Tuple[bool, T]` 且要求 Pydantic schema）。
- 现有提示词在 schema 字段正确的前提下即可让 AI 输出可解析结果。
- `browser.close()` 偶发挂起属于已知现象，加超时即可绕过，不需要根因排查 Playwright/CDP 连接管理。

## Dependencies

- `agentbay` SDK（当前项目锁定版本）。
- `pydantic` v2（项目已使用 `pydantic-settings`，应已可用）。

## Open Questions

- 是否需要在登录态校验之外的其他 `agent.extract` 调用点（若有）做同样的 schema/返回值排查？当前 grep 未发现其他用法，但若后续新增需保持一致约定。
