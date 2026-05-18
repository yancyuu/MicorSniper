# Implementation Tasks: Shop Monitor + Xiaohongshu Channel

## Dependency Graph

```
Phase 1 (Setup)
    ↓
Phase 2 (Foundational)
    ↓
├── Phase 3 (US1: 小红书链接采集) ──────────────────┐
│                                                 ↓
Phase 4 (US2: 店铺监控任务 CRUD)                    │
    ↓                                               │
Phase 5 (US3+US4: 监控执行+告警) ←─────────────────┘
    ↓
Phase 6 (Polish)
```

## Phase 1: Setup

- [x] T001 [P] 在 `services/keyword_search/` 下创建 `xiaohongshu.py`，继承 `KeywordSearchService`
- [x] T002 [P] 在 `services/keyword_search/__init__.py` 注册 `xiaohongshu` service
- [ ] T003 验证小红书搜索页面结构（临时脚本调试 `.note-card` 选择器）

## Phase 2: Foundational

- [x] T004 创建数据模型 `models/shop_monitor_task.py`（ShopMonitorTask）
- [x] T005 创建数据模型 `models/monitor_record.py`（MonitorRecord）
- [x] T005b 注册 `ShopMonitorTask` 和 `MonitorRecord` 到 `models/__init__.py`
- [ ] T008 执行数据库迁移（生成并运行 migration）
- [x] T009 创建 `services/keyword_search/xiaohongshu.py` 的 `build_search_url()` 方法
- [x] T010 创建 `services/keyword_search/xiaohongshu.py` 的 `canonicalize_url()` 方法
- [x] T012 更新 `services/sniper_tasks.py` 和 `services/task_runner.py` 支持 xiaohongshu task_type

## Phase 3: US1 — 小红书商品链接采集

- [x] T011 [US1] 实现 `scripts/xiaohongshu_link_search.py` 独立脚本入口
- [x] T012 [US1] 实现 `XiaohongshuKeywordSearchService.get_page()` 方法
- [x] T013 [US1] 实现 `XiaohongshuKeywordSearchService.wait_page_ready()` 方法
- [x] T014 [US1] 实现 `XiaohongshuKeywordSearchService.scroll_and_extract()` 提取商品卡片
- [x] T015 [US1] 实现 `XiaohongshuKeywordSearchService.go_next_page()` 分页逻辑
- [x] T016 [US1] 实现 `XiaohongshuKeywordSearchService.after_navigate()` 弹窗处理
- [ ] T017 [US1] 运行完整脚本测试，验证链接采集功能

## Phase 4: US2 — 店铺监控任务 CRUD

- [ ] T018 [US2] 创建 `api/routes/shop_monitor.py`，实现 CRUD endpoints
- [ ] T019 [US2] 在 `app.py` 或路由配置中注册 `/api/shop-monitors` 路由
- [ ] T020 [US2] 创建 `scripts/shop_monitor_run.py` CLI 入口
- [ ] T021 [US2] 实现 `ShopMonitorService` 业务逻辑类
- [ ] T022 [US2] 手动测试 CRUD API（创建/查询/更新/删除任务）

## Phase 5: US3+US4 — 监控执行与告警

- [ ] T023 [US3] 实现 `scripts/shop_monitor_check.py` 定时检查脚本
- [ ] T024 [US3] 实现店铺上新检测逻辑（快照对比）
- [ ] T025 [US3] 实现价格变动检测逻辑（记录 MonitorRecord.price）
- [ ] T026 [US3] 接入飞书消息通知（调用 lark-im skill 发送告警）
- [ ] T027 [US4] 实现监控数据查询 API（按时间范围、店铺名筛选）
- [ ] T028 [US4] 实现 CSV 导出功能
- [ ] T029 [US3] 配置 cron 定时触发 `shop_monitor_run.py --all`

## Phase 6: Polish & Cross-Cutting

- [ ] T030 前端：添加商品链接弹窗增加小红书渠道选项
- [ ] T031 前端：店铺监控配置页面（任务列表、新增/编辑任务）
- [ ] T032 添加错误日志和监控指标
- [ ] T033 更新 README 或相关文档（如有新脚本/配置）

---

## Task Count Summary

| Phase | Tasks | Description |
|-------|-------|-------------|
| Phase 1 | 3 | Setup（service 注册、页面结构验证） |
| Phase 2 | 7 | Foundational（数据模型、基础方法） |
| Phase 3 | 7 | US1 小红书链接采集 |
| Phase 4 | 5 | US2 店铺监控 CRUD |
| Phase 5 | 7 | US3+US4 监控执行+告警+导出 |
| Phase 6 | 4 | Polish（前端、文档） |
| **Total** | **33** | |

---

## Completed So Far

| Task | File | Status |
|------|------|--------|
| T001, T002 | `services/keyword_search/xiaohongshu.py` | ✅ |
| T004, T005 | `models/shop_monitor_task.py`, `models/monitor_record.py` | ✅ |
| T005b | `models/__init__.py` 更新 | ✅ |
| T009, T010 | `xiaohongshu.py` 的 build_search_url + canonicalize_url | ✅ |
| T011-T016 | `XiaohongshuKeywordSearchService` 全部方法 | ✅ |
| T012 (更新) | `services/sniper_tasks.py` + `task_runner.py` | ✅ |
| T017 | 运行时验证 | ⏳ 待测试 |
| T018-T033 | 未开始 | ⏳ |

---

## Independent Test Criteria

| User Story | Test Criteria |
|------------|---------------|
| **US1** | 关键词"洗面奶"搜索 → 返回商品链接列表（≥5条），每条含 title/url/shop/price |
| **US2** | 创建任务 → 查询列表 → 更新任务 → 删除任务，API 全部返回 200 |
| **US3** | 监控任务触发 → 生成 MonitorRecord → 发送飞书消息 |
| **US4** | 查询监控记录 → 导出 CSV → 文件包含正确字段 |

---

## Implementation Strategy

1. **Phase 1 → Phase 2**：✅ 完成
2. **Phase 3（US1）**：✅ 代码完成，⏳ 待实际运行验证（T017）
3. **Phase 4（US2）**：待开始（依赖数据库迁移）
4. **Phase 5（US3+US4）**：待开始
5. **Phase 6（Polish）**：最后统一联调