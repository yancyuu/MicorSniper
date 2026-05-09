# Feature Spec: 商品详情 Provider Service 分层

## Overview

将商品详情提取逻辑从 `scripts/product_detail_fetch.py` 拆分到按 provider 组织的 service 层。脚本继续负责任务生命周期、浏览器会话、页面导航、滚动加载和 ProductLink 落库；各 provider service 只负责从已打开的商品详情页提取结构化商品信息。

## Problem Statement

当前商品详情提取存在以下问题：

1. **脚本职责过重**：`product_detail_fetch.py` 同时包含浏览器编排、平台检测、淘宝/天猫/京东提取 JS、详情图补采和落库逻辑。
2. **provider 逻辑难维护**：淘宝、天猫、京东的选择器和页面数据结构都在同一个文件里，单个平台调整容易影响其他平台。
3. **新增平台成本高**：新增 provider 需要继续扩大脚本文件，而不是以清晰的 service 插件形式扩展。
4. **复用能力不足**：后续 `search_by_urls.py` 或其他脚本如需完整商品详情提取，无法直接复用当前按平台封装的提取能力。

## User Scenarios & Testing

### 场景 1：抓取淘宝商品详情

- 用户创建或运行 `product_detail_fetch` 任务，传入淘宝商品链接
- 系统打开商品页面并检测到 provider 为 `taobao`
- 脚本通过淘宝 provider service 提取标题、价格、原价、销量、店铺、主图、SKU、发货地和详情图
- 结果写入或更新 ProductLink

### 场景 2：抓取天猫商品详情

- 用户传入天猫商品链接
- 系统检测到 provider 为 `tmall`
- 脚本通过天猫 provider service 提取完整商品信息
- 新版天猫页面优先读取 `window.__ICE_APP_CONTEXT__` 中的结构化 SSR 数据

### 场景 3：抓取京东商品详情

- 用户传入京东商品链接
- 系统检测到 provider 为 `jd`
- 脚本通过京东 provider service 提取已有支持字段
- 不影响淘宝/天猫提取逻辑

### 场景 4：未知 provider 兜底

- 用户传入未知或暂不支持的商品详情页
- 系统使用 generic provider service
- 尽量提取页面标题、价格和基础图片字段，不让任务因 provider 未注册直接失败

### 验收场景

- 淘宝链接 `https://item.taobao.com/item.htm?id=739198382798` 仍能提取标题、价格、销量、店铺、发货地和图片
- 天猫链接 `https://detail.tmall.com/item.htm?id=922333886401` 仍能提取标题、价格、销量、店铺、发货地和图片
- `product_detail_fetch.py` 不再内联淘宝、天猫、京东详情提取 JS
- 新增 provider 时只需要新增 service 文件并注册到工厂

## Functional Requirements

### FR-1: Provider Service 包

- FR-1.1: 系统新增 `services/product_detail/` 包，专门承载商品详情 provider service。
- FR-1.2: 包内提供 provider 工厂方法，按平台名返回对应 service。
- FR-1.3: 未注册 provider 返回 generic service。

### FR-2: 统一 Service 接口

- FR-2.1: 所有 provider service 暴露统一的 `extract(page)` 方法。
- FR-2.2: 所有 provider service 暴露统一的 `extract_detail_images(page)` 方法。
- FR-2.3: `extract(page)` 返回结构与 ProductLink 字段兼容。
- FR-2.4: service 不负责浏览器会话创建、导航、任务状态或数据库写入。

### FR-3: Provider 拆分

- FR-3.1: 淘宝详情提取逻辑迁移到 `services/product_detail/taobao.py`。
- FR-3.2: 天猫详情提取逻辑迁移到 `services/product_detail/tmall.py`。
- FR-3.3: 京东详情提取逻辑迁移到 `services/product_detail/jd.py`。
- FR-3.4: 通用兜底逻辑迁移到 `services/product_detail/generic.py`。

### FR-4: 脚本调用 Service

- FR-4.1: `scripts/product_detail_fetch.py` 通过 provider 工厂获取 service。
- FR-4.2: 平台检测仍由脚本在页面打开后执行。
- FR-4.3: 第一遍详情提取改为 `await service.extract(page)`。
- FR-4.4: 滚动后详情图补采改为 `await service.extract_detail_images(page)`。
- FR-4.5: 任务入参、返回结构、standalone CLI 和 ProductLink 写入字段保持兼容。

## Success Criteria

1. `product_detail_fetch.py` 的 provider JS 内联逻辑被移除，文件职责更集中。
2. 淘宝、天猫、京东 provider service 可以独立维护。
3. 淘宝和天猫两条已验证链接复测成功。
4. `ReadLints` 对改动文件无新增错误。
5. 不改变现有 API 任务类型和 CLI 使用方式。

## Assumptions

- provider 名称继续使用现有值：`taobao`、`tmall`、`jd`、`unknown`。
- Playwright `page.evaluate()` 仍是商品详情提取的主要方式。
- AgentBay session 生命周期继续由脚本管理，不下沉到 provider service。
- 本次只拆完整商品详情提取，不合并 `search_by_urls.py` 中的轻量价格提取逻辑。

## Out of Scope

- 不调整任务分发器 `services/task_runner.py`。
- 不重构搜索脚本 `taobao_link_search.py`、`tmall_link_search.py`、`jd_link_search.py`。
- 不新增数据库字段或迁移。
- 不引入新的第三方依赖。
- 不把 provider service 改成类继承现有 `services/sniper/connectors/BaseConnector`；该 connector 偏登录/内容平台场景，本功能保持轻量页面提取策略。
