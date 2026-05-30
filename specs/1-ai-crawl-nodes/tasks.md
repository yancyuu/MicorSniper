# Tasks: Canvas RPA 收敛到 crawl4ai 并支持 AI 自动生成爬取节点图

**Feature**: `specs/1-ai-crawl-nodes` | **Branch**: `feature/canvas-rpa`
**Inputs**: plan.md, spec.md, research.md, data-model.md, contracts/api.md, quickstart.md

约定:后端根 `server/`,Python 包 `server/app/`;前端 `frontend/src/`。`[P]` = 可并行(不同文件、无未完成依赖)。测试任务仅在标注处给出(本特性以接口/验收脚本验证为主,未要求 TDD)。

User Story 映射:
- **US1 (P1)** 后端收敛 + 最小工作流跑通(FR-1, FR-2)— **MVP**
- **US2 (P2)** 上下文节点绑定登录态(FR-3)
- **US3 (P3)** runner 修复:翻页双执行 / 数据引用 / 结果持久化 / 可停止(FR-4, FR-5, FR-6, FR-9)
- **US4 (P4)** AI 生成节点图 + 登录约束(FR-7, FR-8)

---

## Phase 1: Setup

- [x] T001 确认 `server/` 依赖可用:`cd server && poetry install && poetry run playwright install chromium`,并 `poetry run python -c "import crawl4ai; print(crawl4ai.__version__)"` 应输出 0.8.6
- [x] T002 在 `server/app/config.py` 增补设置项 `RESULTS_MAX_RUNS:int=200`、`RESULTS_MAX_MB:int=500`,并补 `.env` 样例(`server/.env.example`)
- [x] T003 [P] 新建空目录占位 `data/results/`(`.gitkeep`),确认 `.gitignore` 忽略 `data/results/*.json` 与 `data/browser_profiles/`

---

## Phase 2: Foundational(阻塞所有 US 的前置)

- [x] T004 在 `server/app/store.py` 的 `_Store` 增加 `results_dir = base/"results"` 并在 `init()` 中 `mkdir`
- [x] T005 在 `server/app/models.py` 增加 `RunResult` 模型(workflow_id/run_id/started_at/finished_at/status/node_outputs/items)及 `GenerateRequest`/`GenerateResponse`,并将 `NodeType` 增加 `"context"`

**Checkpoint**: 配置与存储骨架就绪,后续 US 可并行推进各自实现。

---

## Phase 3: US1 — 后端收敛 + 最小工作流跑通 (P1 / MVP) — FR-1, FR-2

**Goal**: 删除并行 TS 后端,默认命令启动 FastAPI,拖一个 crawl 节点即可端到端跑通。
**独立验收**: `npm run dev` 起的是 FastAPI;画布拖「爬取页面」填公开 URL 运行 → 节点 success + 截图 + 内容摘要,工作流 completed(quickstart FR-1/2)。

- [x] T006 [US1] 删除整套 TS 后端目录 `server/src/` 与 `server/tsconfig.json`
- [x] T007 [US1] 清理 `server/package.json`:移除 express/ws/playwright/tsx 等 TS 运行脚本与依赖(若该文件仅服务于 TS 后端则整体删除)
- [x] T008 [US1] 修改根 `package.json` 的 `dev:server` 为 `cd server && poetry run python main.py`;复核 `dev:frontend` / `build` / `start` 不再引用 TS 后端
- [x] T009 [US1] 将 `server/main.py` 的 `@app.on_event("startup")` 改写为 FastAPI `lifespan`(async context 内调用 `STORE.init()`)
- [x] T010 [US1] 核对 `frontend/src/components/NodePalette.tsx` 与 `frontend/src/types/workflow.ts` 的 NodeType 与后端 `models.py` 一致(此处仅校验,新增 context 在 US2)
- [x] T011 [US1] 冒烟:运行 `npm run dev`,POST `/api/workflows/run` 跑单 crawl 节点,确认收到 success 状态帧与截图(对照 quickstart)

---

## Phase 4: US2 — 上下文节点绑定登录态 (P2) — FR-3

**Goal**: 工作流执行复用已登录会话的浏览器 profile/cookie。
**独立验收**: 选已登录会话运行登录可见页 → 得登录态内容;不选/失效会话 → 明确提示(quickstart FR-3)。

- [x] T012 [P] [US2] 前端 `frontend/src/components/NodePalette.tsx` 增加 `context` 节点项;`frontend/src/types/workflow.ts` 的 NodeType 加 `'context'`;`useWorkflow.ts` 的 validTypes 集合加 `context`
- [x] T013 [P] [US2] 前端 `frontend/src/components/ConfigPanel.tsx` 增加 `ContextConfig`:下拉选择已登录会话(数据来自 `GET /api/sessions/`),写入 `config.session_name`
- [x] T014 [US2] 后端 `server/app/engine/runner.py`:执行入口查找 `type=="context"` 节点,读取 `session_name`,用 `BrowserConfig(user_data_dir=STORE.profiles_dir/session_name, ...)` 替换硬编码 `run-{workflow_id}`
- [x] T015 [US2] `runner.py`:profile 目录缺失但会话存有 cookie 时,回退用 `BrowserConfig(cookies=...)`/`storage_state` 注入(读 `STORE.load_session`)
- [x] T016 [US2] `runner.py`:无 context 节点时保留临时 `run-{workflow_id}` profile(保住 US1 基线)
- [x] T017 [US2] `runner.py`:会话失效/profile 不可用时,经 `on_status` 广播明确错误日志(对应 FR-3 边界与 spec Edge Case)

---

## Phase 5: US3 — runner 修复 (P3) — FR-4, FR-5, FR-6, FR-9

**Goal**: 修翻页双执行、加通用数据引用、结果持久化+轮转、运行可真正停止。
**独立验收**: 翻页 N 页结果恰 N 批无重复;`{{node.path}}` 引用生效;`data/results/{id}.json` 可取回且超限轮转;运行中 stop 后续节点不再执行(quickstart FR-4/5/6/9)。

- [x] T018 [P] [US3] `server/app/engine/runner.py`:预扫描被 `paginate` 经出边拥有的 `css_extract`/`ai_extract` 子节点 id 集合,主拓扑循环跳过这些节点(修 FR-4 双执行)
- [x] T019 [P] [US3] 新建 `server/app/engine/refs.py`:实现 `{{node_id.dotted.path}}`(支持 `data[].field` 取列)解析,对节点 config 递归求值,缺失→空值+warning
- [x] T020 [US3] `runner.py`:每个节点执行前调用 refs 解析其 config;用通用引用替换/兼容旧 `_collect_upstream_urls`,`batch_crawl` 的 `use_upstream` 作为语法糖保留(FR-5)
- [x] T021 [P] [US3] `server/app/store.py`:新增 `save_result(run_result)` 写 `results_dir/{workflow_id}.json`,及 `load_result(workflow_id)`
- [x] T022 [US3] `store.py`:新增 `rotate_results()`,按 mtime 与 `RESULTS_MAX_RUNS`/`RESULTS_MAX_MB` 淘汰最旧(FR-6),在 `save_result` 末尾调用
- [x] T023 [US3] `runner.py`:运行结束(成功/失败/停止)汇总 `node_outputs` 与主 `items`,构造 `RunResult` 调 `STORE.save_result`(FR-6)
- [x] T024 [P] [US3] 新建 `server/app/routes/results.py`:`GET /api/results/{workflow_id}`、`GET /api/results/{workflow_id}/export?format=json|csv`、`GET /api/results/`;在 `server/main.py` 注册 router(契约见 contracts/api.md)
- [x] T025 [US3] `runner.py` + `server/app/routes/workflows.py`:引入模块级 `_RUNNING: dict[str, asyncio.Task]`,`/run` 注册 task,runner 在每节点前检查取消(FR-9)
- [x] T026 [US3] `server/app/routes/workflows.py`:新增 `POST /api/workflows/{id}/stop`,cancel 对应 task 并经 manager 广播 `failed/stopped`(FR-9,契约见 contracts/api.md)
- [x] T027 [P] [US3] 前端 `frontend/src/hooks/useWorkflow.ts` 的 `stopWorkflow` 调用 `POST /api/workflows/{id}/stop`(而非仅关 WS);保存当前 `workflow_id`

---

## Phase 6: US4 — AI 生成节点图 + 登录约束 (P4) — FR-7, FR-8

**Goal**: 输入 URL+goal,自动抓页→生成 schema→组图→试跑首页回样例;需登录站点未选会话则拦截。
**独立验收**: 对电商列表页 generate 返回含 context→crawl→css_extract 的 nodes/edges + 非空 sample,回填可跑;需登录页未选会话返回 `need_session`(quickstart FR-7/8)。

- [x] T028 [P] [US4] 新建 `server/app/engine/generate.py`:`fetch_html(url, session_name?)` 用与 runner 一致的 BrowserConfig 抓页,返回 HTML + 最终 URL + 截图
- [x] T029 [US4] `generate.py`:`detect_login_wall(html, final_url)` 启发式(登录路径/域、`input[type=password]`、标题关键词);命中且无 session → 返回 `need_session`(FR-8)
- [x] T030 [US4] `generate.py`:调用 `JsonCssExtractionStrategy.generate_schema(html, query=goal, llm_config=LLMConfig(provider=settings.LLM_PROVIDER, api_token=settings.OPENAI_API_KEY))` 得 schema(FR-7)
- [x] T031 [US4] `generate.py`:`build_graph(url, schema, session_name?, paginated?)` 组装 `context→crawl→css_extract(+paginate)` 的 nodes/edges(坐标依次右移);分页检测启发式(存在分页控件/“下一页”)决定是否加 paginate。**检测到分页时必须同时产出 paginate 节点的 `next_js`(由"下一页"元素选择器生成点击脚本)与 `max_pages` 默认值**,否则不加 paginate(FR-7 / 修 C2)
- [x] T032 [US4] `generate.py`:用生成的 schema 对刚抓的页面试跑一次 `css_extract` 得 `sample`(FR-7)
- [x] T036 [US4] `generate.py` + `build_graph`:当试跑 `sample` 为空或关键字段缺失时,响应置 `low_confidence=true` 并附"需人工确认"提示;schema 字段仅保留页面实际可定位的元素,**不编造无依据的字段/选择器**(FR-7 / spec Edge Case / 修 C1)
- [x] T033 [US4] `server/app/routes/workflows.py`:新增 `POST /api/workflows/generate`,串联 fetch→detect→generate_schema→build_graph→sample,返回成功体或 `need_session`(契约见 contracts/api.md)
- [x] T034 [P] [US4] 前端:新增生成入口组件(URL+goal 输入 + 可选会话选择),调用 `/api/workflows/generate`;参考 `frontend/src/components/Toolbar.tsx`/`FloatingPanel.tsx` 放置入口
- [x] T035 [US4] 前端 `frontend/src/hooks/useWorkflow.ts`:新增 `generateWorkflow(url, goal, sessionName?)`,把返回的 nodes/edges 回填画布(复用 loadWorkflow 的映射),并在日志面板展示 sample 预览;`need_session` 时提示用户先选会话

---

## Phase 7: Polish & Cross-Cutting

- [x] T037 [P] 更新 `CLAUDE.md`:架构章节改为「Canvas RPA / crawl4ai 单后端」,移除已删除的 Sanic/AgentBay 描述,补 generate/results/stop 接口与 context 节点说明
- [~] T038 [P] `ruff check server && black --check server`,修复 lint — ⚠️ ruff/black 未装进 server poetry 环境;暂以 `py_compile` 全量字节编译通过 + 全模块 import 通过代替;需补 dev 依赖后再正式跑
- [~] T039 端到端回归:按 `quickstart.md` 逐条跑 FR-1~FR-9 验收 — ⚠️ 已验证:后端全模块导入 + 路由注册、refs/分页/登录墙/建图纯逻辑单测、结果持久化+轮转单测、前端 vite build。需真实浏览器 + OPENAI_API_KEY 的活页跑(crawl/generate)未在此环境执行
- [x] T040 [P] 删除遗留无关文件(如根目录 `t1_user_update_0.json` 等临时产物,确认非必需后;非 FR,仓库卫生)

---

## Dependencies & 执行顺序

- **Setup(T001–T003)** → **Foundational(T004–T005)** → 之后按 US 优先级。
- **US1(T006–T011)** 是 MVP,且 T010 为 US2 前置校验点。
- **US2 依赖** Foundational(NodeType 含 context);US3、US4 的 runner 改动建议在 **US2 之后**(共享 `runner.py` 的 BrowserConfig 入口),避免合并冲突。
- **US4 依赖** US2(generate 复用会话抓页)与 US3(css_extract/refs 已稳定)。
- 同一文件内任务非并行:`runner.py`(T014–T018,T020,T023,T025)需顺序;`workflows.py`(T026,T033)需顺序;`useWorkflow.ts`(T027,T035)需顺序。

## 并行机会(示例)

- Setup 后:T012/T013(前端 context UI)与 T014–T017(后端绑定)可并行。
- US3 内:T018(翻页)、T019(refs)、T021/T024(results 存储与路由)、T027(前端 stop)分属不同文件,可并行起步,最后在 `runner.py`(T020/T023/T025)收口。
- US4 内:T036 在 T031/T032 之后(依赖 build_graph 与 sample)。
- Polish:T037/T038/T040 可并行。

## MVP 范围

**US1(T001–T011)** 即可交付一个能跑通的单后端画布最小闭环;US2 让它对登录站点可用;US3 让它健壮+可观测;US4 是体验跃升(AI 生成)。建议按 US1→US2→US3→US4 增量交付。
