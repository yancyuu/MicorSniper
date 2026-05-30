# Research: Canvas RPA 收敛 + AI 生成节点图

**Date**: 2026-05-29 | crawl4ai 实测版本 **0.8.6**

## R-1: 工作流如何绑定登录态会话

- **Decision**: 执行时用 `BrowserConfig(user_data_dir = data/browser_profiles/{session_name})` 直接复用登录得到的持久化 profile;若该 profile 不存在但 session 存了 cookie,则退回 `storage_state`/`cookies` 注入。无 context 节点时用临时 `run-{workflow_id}` profile(保 FR-2)。
- **Rationale**: 登录流程(`sessions.py`)本就用 `launch_persistent_context(browser_profiles/{name})` 落盘,直接复用同一目录最忠实、零额外转换;crawl4ai 0.8.6 的 `BrowserConfig` 已实测支持 `user_data_dir`/`cookies`/`storage_state`。
- **Alternatives**: 仅注入 cookie(丢 localStorage/指纹,部分站点失效);每次新建 profile(=当前 bug,登录态丢失)。

## R-2: AI 生成提取 schema

- **Decision**: `JsonCssExtractionStrategy.generate_schema(html=<抓到的页面HTML>, query=<用户目标>, llm_config=LLMConfig(provider=settings.LLM_PROVIDER, api_token=settings.OPENAI_API_KEY))`,返回 `{name, baseSelector, fields:[...]}`。
- **Rationale**: 实测签名 `generate_schema(html, schema_type='CSS', query, target_json_example, llm_config, ..., validate=True, max_refinements=3)` 存在;一次性生成、后续运行零 LLM 成本,正是 FR-7 诉求。`validate=True`+`max_refinements` 自带自校验/重试。
- **Alternatives**: 每次跑都用 `LLMExtractionStrategy`(每页烧 token,慢且贵);手写 selector(高门槛,本特性要消除的)。
- **Note**: 抓页须先于 generate;需登录站点先绑会话(R-5)。生成后用同一 HTML/页面试跑一次得到样例(FR-7)。

## R-3: 翻页节点子提取双执行

- **Decision**: runner 预扫描:对每个 `paginate` 节点,沿出边找到其 `css_extract`/`ai_extract` 子节点,记入 `owned_by_paginate` 集合;主拓扑循环跳过该集合中的节点。
- **Rationale**: 现状子节点既在 paginate 循环内执行又在拓扑序中独立执行(`runner.py:138-152` + 同节点在 execution_order),导致重复且结果错乱。
- **Alternatives**: 让 paginate 不内嵌执行、改由边驱动逐页(改动大,且 crawl4ai session 翻页天然是命令式循环,内嵌更自然)。

## R-4: 节点间数据引用

- **Decision**: 统一 `{{node_id.dotted.path}}` 模板。执行前对节点 config 的字符串值做解析,从 `node_results[node_id]` 按点路径取值;数组可用 `node_id.data[].href` 取列。解析失败→空值 + warning 日志。保留 batch_crawl 的 `use_upstream` 作为语法糖(等价引用上游 links)。
- **Rationale**: 现 `_collect_upstream_urls` 只认 `data.links`,无法表达"价格→下游""详情URL→批量爬"等。模板法通用、对前端透明。
- **Alternatives**: 固定端口/字段映射 UI(更重);GraphQL 式 resolver(过度设计)。

## R-5: 登录墙检测(FR-8)

- **Decision**: 抓页后启发式判定"需登录":(a) 最终 URL 命中登录路径/域(`login`/`passport`/`sign`),或 (b) DOM 含 `input[type=password]`,或 (c) 标题/正文含登录关键词。命中且未选会话 → 不生成,返回 `{need_session: true, message}`。公开页正常生成。
- **Rationale**: 轻量、无需站点白名单;避免拿登录墙 HTML 生成垃圾 schema。
- **Alternatives**: 维护需登录站点白名单(维护成本、覆盖不全);完全交给用户判断(违背 FR-8)。

## R-6: 结果持久化与轮转(FR-6)

- **Decision**: 每次运行结束写 `data/results/{workflow_id}.json`(含 run 元信息 + 各提取节点结构化数据)。写入后做轮转:按 mtime 排序,超 `RESULTS_MAX_RUNS`(默认 200)或总字节超 `RESULTS_MAX_MB`(默认 500)则删最旧,直到满足。
- **Rationale**: 文件级简单、零依赖;轮转防磁盘无限增长(澄清结论)。
- **Alternatives**: SQLite/Postgres(本特性 Non-Goal:不引数据库);永久保留(澄清已否决)。

## R-7: 运行可中止(FR-9)

- **Decision**: 模块级 `_RUNNING: dict[str, asyncio.Task]`。`/run` 注册 task;runner 在每个节点开始前检查 `asyncio.current_task().cancelled()`/取消标志;`/stop` 对该 task `.cancel()` 并广播 `failed/stopped`。
- **Rationale**: 现 stopWorkflow 仅关前端 WS,后端继续跑(资源浪费/数据污染)。节点边界取消足够,无需强杀单次 arun。
- **Alternatives**: 进程级杀(过重);忽略(违背 FR-9)。

## R-8: 后端收敛(FR-1)

- **Decision**: 删 `server/src/` 全部 TS;删/清 `server/package.json`(仅 TS 用);根 `package.json` `dev:server` → `cd server && poetry run python main.py`;`main.py` `@app.on_event("startup")` → `lifespan` async context。前端节点词汇已与 Python 后端一致,无需改 palette。
- **Rationale**: crawl4ai 仅 Python;两套并存导致前端/后端词汇错配、dev 跑不通(关键 bug)。
- **Alternatives**: 保留 TS(失去 crawl4ai 与 generate_schema,违背用户决策)。

## 未决默认值(tasks 阶段定稿)

- `RESULTS_MAX_RUNS=200`,`RESULTS_MAX_MB=500`(settings 可覆盖)。
- 引用语法 `{{node_id.path}}`。
- 取消粒度=节点边界。
