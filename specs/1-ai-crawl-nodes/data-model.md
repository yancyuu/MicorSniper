# Data Model

## 实体

### Session(会话 / 上下文)
登录得到的持久浏览器状态,被工作流引用。

| 字段 | 类型 | 说明 |
|---|---|---|
| name | str (PK) | 会话名,对应 `data/browser_profiles/{name}` 目录 |
| url | str | 登录时所在 URL |
| cookies | list[dict] | 备用注入(profile 缺失时回退) |
| created_at | str(ISO) | 创建时间 |

存储:`data/sessions/{name}.json` + `data/browser_profiles/{name}/`(持久 profile)。

### Workflow
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str(uuid) | 主键 |
| name | str | 名称 |
| nodes | list[WorkflowNode] | 节点 |
| edges | list[WorkflowEdge] | 连线 |
| status | enum | idle/running/completed/failed |
| created_at/updated_at | str(ISO) | |

存储:`data/workflows/{id}.json`。

### WorkflowNode
| 字段 | 类型 | 说明 |
|---|---|---|
| id | str | 节点 id |
| type | str | 固定 `"workflowNode"`(React Flow 类型) |
| position | {x,y} | 画布坐标 |
| data.type | NodeType | 见下 |
| data.label | str | 显示名 |
| data.config | dict | 节点配置(值可含 `{{node_id.path}}` 引用) |
| data.status/result/error/screenshot | | 运行态(瞬态) |

**NodeType**(与前端 palette 对齐,新增 `context`):
`context` · `crawl` · `batch_crawl` · `css_extract` · `ai_extract` · `js_execute` · `paginate`

#### 各节点 config 关键字段
| NodeType | config 字段 |
|---|---|
| context | `session_name`(选已登录会话) |
| crawl | `url`, `wait_until` |
| batch_crawl | `urls` 或 `use_upstream` / 引用 `{{node.data[].href}}` |
| css_extract | `base_selector`, `fields`(JSON,可由 generate 填充) |
| ai_extract | `instruction`, `schema`, `provider` |
| js_execute | `js_code`, `delay` |
| paginate | `next_js`, `max_pages`, `page_delay`(子提取节点经边连接) |

### WorkflowEdge
| 字段 | 类型 |
|---|---|
| id / source / target | str |

### RunResult(运行结果)— 新增
| 字段 | 类型 | 说明 |
|---|---|---|
| workflow_id / run_id | str | 标识本次运行 |
| started_at / finished_at | str(ISO) | |
| status | enum | completed/failed/stopped |
| node_outputs | dict[node_id, any] | 各节点结构化输出 |
| items | list | 主提取结果(便于导出) |

存储:`data/results/{workflow_id}.json`;受 `RESULTS_MAX_RUNS`/`RESULTS_MAX_MB` 轮转。

### GenerateRequest / GenerateResponse — 新增(瞬态,不落库)
- **Request**: `{ url: str, goal: str, session_name?: str }`
- **Response(成功)**: `{ nodes: [...], edges: [...], sample: [...], schema: {...}, low_confidence: bool }`
  - `low_confidence=true` 时为尽力而为草图(样例空/字段缺失),需人工确认,字段不编造。
  - 含 paginate 节点时,其 `config.next_js`(翻页脚本)与 `config.max_pages` 必须由生成填充。
- **Response(需登录)**: `{ need_session: true, message: str }`

## 状态流转
- Workflow.status: `idle → running → (completed | failed)`;`/stop` → `failed`(子状态 stopped)。
- Node.status: `idle → running → (success | error)`。
- 节点执行前解析 config 中的 `{{...}}` 引用;`context` 节点为执行入口(决定 BrowserConfig)。
