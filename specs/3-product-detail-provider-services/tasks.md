# Tasks: 商品详情 Provider Service 分层

## Phase 1: Service 包搭建

- [x] T001 新增 `services/product_detail/base.py`，定义 `ProductDetailService` 基类和默认详情图补采 JS。
- [x] T002 新增 `services/product_detail/generic.py`，迁移通用兜底提取逻辑。
- [x] T003 新增 `services/product_detail/__init__.py`，实现 `get_provider_service(platform)` 工厂。

## Phase 2: Provider 迁移

- [x] T004 新增 `services/product_detail/taobao.py`，迁移当前 `_TAOBAO_DETAIL_JS`。
- [x] T005 新增 `services/product_detail/tmall.py`，迁移当前 `_TMALL_DETAIL_JS`。
- [x] T006 新增 `services/product_detail/jd.py`，迁移当前 `_JD_DETAIL_JS`。
- [x] T007 确认淘宝、天猫、京东 service 均返回 ProductLink 兼容字段：`title`、`price`、`original_price`、`sales`、`shop_name`、`shop_url`、`image`、`main_images`、`sku_info`、`location`、`detail_images`。

## Phase 3: 脚本改造

- [x] T008 在 `scripts/product_detail_fetch.py` 中引入 `get_provider_service`。
- [x] T009 删除脚本内 `_TAOBAO_DETAIL_JS`、`_TMALL_DETAIL_JS`、`_JD_DETAIL_JS`、`_GENERIC_DETAIL_JS` 和 `_PLATFORM_DETAIL_JS`。
- [x] T010 将任务模式中的 `page.evaluate(detail_js)` 替换为 `service.extract(page)`。
- [x] T011 将任务模式中的滚动后详情图补采替换为 `service.extract_detail_images(page)`。
- [x] T012 将 standalone 模式中的 `page.evaluate(detail_js)` 替换为 `service.extract(page)`。
- [x] T013 保持 `_DETECT_PLATFORM_JS`、`_agent_act()`、`_get_page()`、任务落库逻辑和 CLI 参数不变。

## Phase 4: 验证

- [x] T014 使用 ReadLints 检查 `scripts/product_detail_fetch.py` 和 `services/product_detail/`。
- [x] T015 复测淘宝链接 `https://item.taobao.com/item.htm?id=739198382798`，确认标题、价格、销量、店铺和图片正常。
- [x] T016 复测天猫链接 `https://detail.tmall.com/item.htm?id=922333886401`，确认标题、价格、销量、店铺和图片正常。
- [x] T017 对比重构前后输出结构，确认任务返回统计和 ProductLink 字段没有变化。

## Deferred

- [ ] D001 后续评估是否把 `scripts/search_by_urls.py` 的轻量价格提取逻辑也迁入同一 provider service 包。
- [ ] D002 后续评估淘宝/天猫共同的 `__ICE_APP_CONTEXT__` 解析逻辑是否抽成共享 JS 片段。
