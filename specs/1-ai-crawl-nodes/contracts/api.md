# API Contracts

所有路径相对后端 (FastAPI, 默认 :1111)。新增项标 🆕。

## Workflows

### POST /api/workflows/generate 🆕  (FR-7, FR-8)
AI 看页面自动生成节点图。

**Request**
```json
{ "url": "https://list.example.com/search?q=按摩仪", "goal": "采集每个商品的标题、价格、销量、链接", "session_name": "ctx-ab12cd34" }
```
`session_name` 可选;公开页可省略。

**Response 200 — 成功**
```json
{
  "nodes": [
    {"id":"node_1","type":"workflowNode","position":{"x":0,"y":0},"data":{"type":"context","label":"上下文","config":{"session_name":"ctx-ab12cd34"}}},
    {"id":"node_2","type":"workflowNode","position":{"x":260,"y":0},"data":{"type":"crawl","label":"爬取页面","config":{"url":"https://list.example.com/search?q=按摩仪"}}},
    {"id":"node_3","type":"workflowNode","position":{"x":520,"y":0},"data":{"type":"css_extract","label":"CSS 提取","config":{"base_selector":".item","fields":[{"name":"title","selector":".title","type":"text"}]}}}
  ],
  "edges": [
    {"id":"e1","source":"node_1","target":"node_2"},
    {"id":"e2","source":"node_2","target":"node_3"}
  ],
  "schema": {"name":"items","baseSelector":".item","fields":[/* ... */]},
  "sample": [{"title":"...","price":"...","sales":"...","href":"..."}],
  "low_confidence": false
}
```
- 检测为分页列表时,`nodes`/`edges` 额外包含 `paginate` 节点,且其 `config.next_js` 为可点击下一页的脚本、`config.max_pages` 有默认值(FR-7)。
- `sample` 为生成后试跑首页所得(FR-7)。
- `low_confidence: true` 表示试跑样例为空/字段缺失,节点图为尽力而为草图、需人工确认;字段仅来自页面实际可定位元素,不编造。

**Response 200 — 需登录 (FR-8)**
```json
{ "need_session": true, "message": "目标页面需要登录,请先选择一个已登录会话再生成。" }
```

**Response 4xx/5xx**
```json
{ "detail": "抓取失败: <原因>" }
```

---

### POST /api/workflows/{id}/stop 🆕  (FR-9)
中止运行中的工作流。

**Response 200**: `{ "success": true }`  
**Response 404**: 工作流未在运行。  
副作用:经 WS 广播 `{node_id:"root", status:"failed", log:"Workflow stopped"}`。

---

### POST /api/workflows/run  (既有, 行为不变)
**Response**: `{ "success": true, "workflow_id": "<uuid>" }`,随后连 `WS /ws/workflows/{id}`。

### POST /api/workflows/ · GET /api/workflows/ · GET /api/workflows/latest · GET/PUT/DELETE /api/workflows/{id}
既有 CRUD,契约不变。

---

## Results 🆕  (FR-6)

### GET /api/results/{workflow_id}
取回某次运行的结构化结果。

**Response 200**
```json
{ "workflow_id":"...", "run_id":"...", "status":"completed",
  "started_at":"...", "finished_at":"...",
  "items":[/* 主提取结果 */],
  "node_outputs": {"node_3":[/* ... */]} }
```
**404**: 无结果。

### GET /api/results/{workflow_id}/export?format=json|csv
导出。`json` 原样;`csv` 扁平化 `items`。返回文件下载。

### GET /api/results/  (可选)
列出已存结果(分页)。

---

## Sessions  (既有, 契约不变)
`GET /api/sessions/` · `POST /api/sessions/create` · `POST /api/sessions/{name}/navigate` · `POST /api/sessions/login` · `POST /api/sessions/{name}/confirm-login` · `DELETE /api/sessions/{name}` · `WS /ws/sessions/{name}/stream`

---

## WebSocket /ws/workflows/{id}  (既有, 行为不变)
服务端推送(每节点):
```json
{ "node_id":"node_2", "status":"running|success|error",
  "screenshot":"<base64>", "result":<any>,
  "log":"...", "node_name":"...", "level":"info|error" }
```
终止帧:`{ "node_id":"root", "status":"completed|failed", "log":"..." }`
