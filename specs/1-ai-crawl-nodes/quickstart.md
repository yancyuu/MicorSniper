# Quickstart & 验收

## 本地启动(收敛后)
```bash
# 后端依赖
cd server && poetry install && poetry run playwright install chromium

# 一键起前后端(根目录)
npm run dev          # dev:server = poetry run python main.py ; dev:frontend = vite :3000
# 后端 :1111  前端 :3000
```

> 收敛验证:`server/src/` 已删除;`npm run dev` 启动的是 FastAPI(非 tsx)。

## 配置(server/.env)
```
PORT=1111
LLM_PROVIDER=openai/gpt-4o-mini      # 或其它 crawl4ai 支持的 provider
OPENAI_API_KEY=sk-...
RESULTS_MAX_RUNS=200
RESULTS_MAX_MB=500
```

## 按 FR 验收

- **FR-1/2(基线)**:画布拖一个「爬取页面」→ 填公开 URL → 运行 → 节点变 success、收到截图+内容摘要、工作流 completed。
- **FR-3(会话)**:先在会话里登录某站 → 画布加「上下文」选该会话 → 接「爬取页面」(登录可见页)→ 运行得到登录态内容;删/不选会话则提示。
- **FR-4(翻页)**:paginate(max_pages=3)+子提取 → 运行后结果恰 3 批,无重复。
- **FR-5(引用)**:crawl 节点输出 → 下游 config 用 `{{node_2.data.links[].href}}` → 下游确实收到上游 URL 列表。
- **FR-6(结果)**:任一次运行后 `data/results/{id}.json` 存在;`GET /api/results/{id}` 取回;跑过 200 次后最旧被清。
- **FR-7(生成)**:`POST /api/workflows/generate {url,goal}` → 返回含 context→crawl→css_extract 的 nodes/edges + 非空 `sample`;回填画布可直接跑出非空结果;分页页含 paginate 节点。
- **FR-8(生成登录约束)**:对需登录页未选会话 → 返回 `need_session:true`;公开页正常生成。
- **FR-9(停止)**:多节点运行中途 `POST /api/workflows/{id}/stop` → 后续节点不再执行,后端无新状态推送。

## 冒烟脚本(生成端点)
```bash
curl -s localhost:1111/api/workflows/generate \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","goal":"采集标题和链接"}' | jq '.nodes|length, .sample'
```
