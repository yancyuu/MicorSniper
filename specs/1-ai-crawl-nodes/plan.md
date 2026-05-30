# Implementation Plan: Canvas RPA 收敛到 crawl4ai 并支持 AI 自动生成爬取节点图

**Branch**: `feature/canvas-rpa` | **Spec**: [spec.md](./spec.md) | **Date**: 2026-05-29

## Technical Context

| 项 | 取值 |
|---|---|
| 后端语言/框架 | Python 3.12 / FastAPI / uvicorn |
| 爬取引擎 | crawl4ai **0.8.6**(底层 Playwright/Chromium) |
| 前端 | React + @xyflow/react + Vite + TypeScript |
| 实时通道 | WebSocket(节点状态/截图/日志) |
| LLM Provider | 经 crawl4ai `LLMConfig`,沿用 `settings.LLM_PROVIDER` + `OPENAI_API_KEY` |
| 持久化 | 本地 JSON 文件:`data/{workflows,sessions,results,browser_profiles}` |
| 被移除 | `server/src/`(TypeScript + 原生 Playwright 后端) |

**关键 API 已实测可用**(crawl4ai 0.8.6):
- `JsonCssExtractionStrategy.generate_schema(html, query=..., llm_config=...) -> dict`
- `BrowserConfig(user_data_dir=..., cookies=..., storage_state=...)`
- `CrawlerRunConfig(session_id, js_only, wait_until, screenshot, extraction_strategy, js_code)`

## Constitution Check

仓库无 `.specify/memory/constitution.md`,无显式宪法约束 → 本计划无 gate 违例。沿用项目既有约定(见 `CLAUDE.md`):hybrid 浏览器自动化、AI Native 任务日志、`__` 嵌套环境变量配置。

## 架构概览(目标态)

```
frontend (画布)
  └─ POST /api/workflows/generate   ← 新:AI 生成节点图(URL+goal → nodes/edges+样例)
  └─ POST /api/workflows/run        → 返回 workflow_id
  └─ WS  /ws/workflows/{id}         → 节点状态/截图/日志
  └─ POST /api/workflows/{id}/stop  ← 新:真正中止
  └─ GET  /api/results/{run_id}     ← 新:取回/导出结果

server/app (唯一后端,FastAPI + crawl4ai)
  ├─ engine/runner.py   拓扑执行;入口找 context 节点绑定会话 profile;数据引用解析;可取消
  ├─ engine/nodes.py    节点实现(含 context 节点);generate 用 generate_schema
  ├─ engine/generate.py 新:抓页 → generate_schema → 组图 → 试跑一页
  ├─ store.py           +results 目录 + 轮转
  └─ routes/            workflows(+generate/+stop)、sessions、results(新)
```

## 设计决策(详见 research.md)

1. **会话绑定**:工作流执行用 `BrowserConfig(user_data_dir = data/browser_profiles/{session_name})` 复用登录持久化目录,取代硬编码 `run-{workflow_id}`。无 context 节点时退回临时 profile(保持 FR-2 基线可跑)。
2. **AI 生成**:`generate.py` 先抓页(需登录站点要求已选会话,FR-8),取 HTML → `generate_schema(html, query=goal)` → 拼 `context→crawl→css_extract`(检测到分页则加 `paginate`)→ 立即用 schema 试跑首页回填样例(FR-7)。
3. **翻页双执行修复**:runner 预先标记被 paginate "拥有"的子提取节点 ID,主拓扑循环跳过它们(只在 paginate 内执行)(FR-4)。
4. **节点数据引用**:执行每个节点前,用 `{{node_id.path}}` 模板对其 config 做解析(从 `node_results` 取值),泛化掉只认 `links` 的旧逻辑(FR-5)。
5. **结果持久化+轮转**:每次运行写 `data/results/{workflow_id}.json`;超过条数上限(默认 200)或总大小上限(默认 500MB)淘汰最旧(FR-6)。
6. **登录墙检测**:抓页后用启发式(最终 URL 跳转登录域/含密码框/标题关键词)判定;判为需登录且未选会话 → 返回明确提示(FR-8)。
7. **可中止**:`_RUNNING: dict[workflow_id, asyncio.Task]` 注册表 + 每节点间检查取消标志;stop 接口 cancel 任务(FR-9)。
8. **后端收敛**:删 `server/src`、`server/package.json`(TS);根 `package.json` `dev:server` 指向 `poetry run python main.py`;`main.py` `on_event` → lifespan(FR-1)。

## 实施阶段(按依赖/可独立验证排序)

- **Phase 0(地基/FR-1,2)**:删 TS 后端、修 dev 脚本、lifespan;验证最小 crawl 工作流端到端跑通。
- **Phase 1(FR-3)**:前端 context 节点 + runner 会话绑定。
- **Phase 2(FR-4,5,6,9)**:修翻页双执行、数据引用、结果持久化+轮转、可中止。
- **Phase 3(FR-7,8)**:`/generate` 端点 + 登录墙检测 + 试跑回填 + 前端生成入口与回填。

每阶段结束以对应 FR 的 Acceptance Criteria 验收。

## Artifacts

- [research.md](./research.md) — 决策与备选
- [data-model.md](./data-model.md) — 实体与字段
- [contracts/](./contracts/) — 接口契约
- [quickstart.md](./quickstart.md) — 本地启动与验收脚本

## 留待 tasks 阶段的默认值

- 轮转阈值:`RESULTS_MAX_RUNS=200`、`RESULTS_MAX_MB=500`(走 settings,可覆盖)。
- 数据引用语法:`{{node_id.dotted.path}}`,缺失解析为空并记 warning。
- 取消粒度:节点边界检查(不强杀进行中的单次 `arun`)。
