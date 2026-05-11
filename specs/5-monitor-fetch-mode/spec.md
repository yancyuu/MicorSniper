# Feature Specification: 电商链接监控补全/覆盖模式

## Overview

在电商链接监控的任务创建弹窗中新增"抓取范围"选项，允许用户选择"补全模式"（只抓取缺少数据的商品）或"覆盖模式"（全部重新抓取）。

## Problem Statement

当前电商链接监控任务每次都会抓取所有长期监控链接，无法区分哪些商品已有完整数据。用户在只想补充缺失数据时，仍然要等待全部商品重新抓取，浪费时间和浏览器资源。

## Goals

- 用户可以在创建电商链接监控任务时选择抓取范围
- 补全模式下，系统自动跳过已有完整数据的商品，只处理缺失的
- 覆盖模式下，所有商品重新抓取（与现有行为一致）
- 复用后端已有的缺失检测逻辑

## Non-Goals

- 不修改缺失检测的具体判断逻辑
- 不修改关键词搜索任务的流程
- 不改变任务结果的存储格式

## User Scenarios & Testing

### Primary Scenario 1：补全模式

1. 用户打开"电商链接监控"弹窗
2. 选择监控平台、监控模式（详情/价格）
3. 将"抓取范围"设为"补全模式"
4. 点击"校验并启动"
5. 系统自动过滤掉已有完整数据的商品，只对缺失数据的商品创建任务
6. 执行流程提示中显示实际需要处理的商品数量（应少于总数）

### Primary Scenario 2：覆盖模式

1. 用户打开"电商链接监控"弹窗
2. 选择监控平台、监控模式
3. 将"抓取范围"设为"覆盖模式"（默认值）
4. 点击"校验并启动"
5. 系统对所有长期监控商品创建任务（与当前行为一致）

### Edge Cases

- 如果补全模式下所有商品都已有完整数据，任务应正常创建但 URL 列表为空，任务自动标记为完成并提示"无需补全"
- 补全模式下的缺失判断取决于监控模式：价格模式看 price 是否为空，详情模式看详情数据是否完整
- 切换监控模式时，补全模式的说明文案应随之变化

## Functional Requirements

### FR-1: 前端新增"抓取范围"选项

**Description**: 在电商链接监控弹窗（"监控模式"下拉框之后）新增一个"抓取范围"下拉框，包含两个选项：覆盖模式（默认）和补全模式。

**Acceptance Criteria**:
- 弹窗中"监控模式"下拉框下方出现"抓取范围"下拉框
- 默认选中"覆盖模式"
- "覆盖模式"的说明为：全部重新抓取，覆盖已有数据
- "补全模式"的说明为：只抓取数据缺失的商品，跳过已有的
- 选择补全模式后，说明文案根据当前监控模式动态变化（价格模式提示"跳过已有价格的商品"，详情模式提示"跳过已有详情的商品"）

### FR-2: 前端传递抓取范围参数到后端

**Description**: 点击"校验并启动"时，将选中的抓取范围作为参数传递给后端创建任务的接口。

**Acceptance Criteria**:
- 选中覆盖模式时，不传 `retry_missing` 或传 `retry_missing=false`
- 选中补全模式时，传 `retry_missing=true`
- 参数通过已有的 `createBulkDetailTasks` 函数发送到 `/api/tasks/business/ecommerce-link-monitor`

### FR-3: 后端将抓取范围传递到任务参数

**Description**: 后端创建电商链接监控任务时，将 `retry_missing` 参数写入每个子任务的 params 中。

**Acceptance Criteria**:
- 后端接收到 `retry_missing=true` 时，将其写入每个子任务的 `params.retry_missing`
- 后端接收到 `retry_missing=false` 或未传时，不写入该参数（保持现有行为）
- 任务执行时，`product_detail_fetch.py` 已有的 `retry_missing` 逻辑正常工作，无需修改执行脚本
- 同时写入 `fetch_mode` 字段（值为 `complement` 或 `overwrite`）到 params，作为用户意图的持久记录

### FR-4: 重试逻辑遵循任务创建时的抓取范围

**Description**: 重试（retry）失败/取消的任务时，应从任务 params 中读取创建时保存的 `retry_missing` 值，而不是硬编码为 `true`。

**Acceptance Criteria**:
- 任务创建时，params 中保存 `fetch_mode`（`complement` 或 `overwrite`）和 `retry_missing`（`true` 或不设置）
- 重试时，不强制覆盖 `retry_missing = True`，而是保留任务创建时的原始值
- 具体行为：
  - 如果原任务是覆盖模式（params 中无 `retry_missing` 或为 `false`），重试时仍为覆盖模式
  - 如果原任务是补全模式（params 中 `retry_missing=true`），重试时仍为补全模式
- 当前 `retry_task` 中 `params["retry_missing"] = True` 的硬编码需改为条件判断：仅在原 params 已有 `retry_missing=true` 时保留，否则不设置

### FR-5: 补全模式下的执行反馈

**Description**: 补全模式启动后，执行流程区域应提示用户过滤了多少商品。

**Acceptance Criteria**:
- 执行流程提示中显示："补全模式：从 X 条长期监控链接中筛选出 Y 条需要更新"（X 为总数，Y 为缺失数）
- 如果 Y=0（全部完整），提示"所有商品数据完整，无需补全"并终止

## Success Criteria

- 用户能在电商链接监控弹窗中选择补全或覆盖模式
- 补全模式下，只抓取数据缺失的商品，跳过完整的
- 覆盖模式下，行为与改动前完全一致
- 任务执行反馈中明确提示过滤结果
- 重试任务时保持创建时的抓取范围模式，不硬编码为补全模式

## Key Entities

| Entity              | Description                                                       |
|---------------------|-------------------------------------------------------------------|
| 抓取范围选项         | 新增的 UI 控件，值为覆盖（默认）或补全                             |
| retry_missing 参数   | 已有的后端参数，控制是否只处理缺失数据的 URL                       |
| ProductLink          | 已有模型，补全模式下检查 title/price/image/shop_name 是否完整       |
| ProductDetail        | 已有模型，补全模式下检查详情抓取任务的 sku_info/detail_images 等    |

## Assumptions

- 后端 `product_detail_fetch.py` 的 `_load_detail_urls()` 已完整实现补全模式的过滤逻辑（检查 summary 和 detail 的缺失），无需修改
- 后端 `search_by_urls.py`（价格模式）的补全逻辑需确认是否也支持 `retry_missing`，如果不支持则需要补充
- 默认选择"覆盖模式"，避免影响用户已有习惯

## Dependencies

- 后端 `_load_detail_urls()` 中 `retry_missing` 过滤逻辑已正确实现
- 前端 `createBulkDetailTasks` 函数需扩展以传递新参数
- 后端 `create_ecommerce_link_monitor_business_task` 需将 `retry_missing` 传递到子任务 params
- 后端 `retry_task`（`api/routes/sniper.py`）需修改硬编码的 `params["retry_missing"] = True`，改为保留原值

## Open Questions

无（补全判断条件已由代码确认，前端位置已明确）
