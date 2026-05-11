# Feature Specification: 修复节省时间指标计算

## Overview

将仪表盘"节省时间"指标的数据源从 ProductLink 去重条数改为按已完成任务的实际处理条数累加，使指标能真实反映每次任务执行的自动化工作量。

## Problem Statement

当前"节省时间"指标基于 ProductLink 表的去重统计：
- `keyword_search` 条数 * 2min + `monitored` 条数 * 3min
- 任务复查已有 URL 时不新增 ProductLink 记录，导致这些工作量未被计入
- 结果：仪表盘数字不增长，无法反映实际自动化节省的时间

用户已确认方向：统计所有已完成任务的 `result.total` 之和，按任务类型乘以对应系数。

## Goals

- 节省时间指标准确反映每次任务的实际处理量（包括重复检查已有 URL 的工作）
- 指标随着每次任务完成而增长，不再因 URL 去重而停滞
- 保持现有的 2min/条（关键词任务）和 3min/条（详情任务）的换算系数不变

## Non-Goals

- 不改变任务模型或任务执行逻辑
- 不改变前端仪表盘的视觉布局
- 不引入新的时间追踪或计费系统

## User Scenarios & Testing

### Primary Scenario

1. 用户提交一批关键词搜索任务，任务完成后仪表盘"节省时间"数字增长
2. 用户对同一批关键词再次搜索（复查），由于 URL 大部分已存在，ProductLink 不新增，但"节省时间"仍会增长（因为任务实际处理了这些条目）
3. 用户提交详情监控任务，完成后"节省时间"按 3min/条增长

### Edge Cases

- 任务 `result` 字段为 null 或不含 `total` 字段时，该任务不计入节省时间（视为 0）
- 任务状态为 failed/pending/running 时，不计入节省时间
- `business_task` 类型既非 `keyword_search` 也非 `ecommerce_link_monitor` 时，不计入节省时间

## Functional Requirements

### FR-1: 后端提供按任务统计的节省时间数据

**Description**: 后端新增或修改 API，返回按任务类型分类的已完成任务 `result.total` 累加值。

**Acceptance Criteria**:
- API 返回 `keyword_search_total`（所有已完成的 keyword_search 任务的 result.total 之和）
- API 返回 `ecommerce_link_monitor_total`（所有已完成的 ecommerce_link_monitor 任务的 result.total 之和）
- 仅统计 `status='completed'` 的任务
- `result` 为 null 或无 `total` 字段时按 0 处理

### FR-2: 前端使用新数据源计算节省时间

**Description**: 前端 `estimateSavedMinutes()` 函数改用后端返回的任务统计数据，替代当前从 ProductLink stats 获取的 `keyword_search` 和 `monitored` 条数。

**Acceptance Criteria**:
- `estimateSavedMinutes()` 使用 `keyword_search_total * 2 + ecommerce_link_monitor_total * 3` 计算
- 仪表盘卡片的提示文案更新为匹配新的分类（关键词节约 2min/条、详情节约 3min/条）
- 页面加载时获取新的统计数据

### FR-3: 数据获取时机

**Description**: 节省时间统计数据在页面初始化和任务列表刷新时同步更新。

**Acceptance Criteria**:
- 连接成功后（`saveApiKey`）触发数据加载
- 任务轮询刷新时同步更新节省时间
- 不额外增加 API 调用次数（可复用现有 `loadTasks` 返回的数据，或复用 `loadProductStats` 接口扩展字段）

## Success Criteria

- 节省时间数字在每次任务完成后都有增长（不再停滞）
- 数值等于所有已完成任务的 `result.total` 按类型加权求和（关键词 2min + 详情 3min）
- 现有仪表盘其他功能不受影响

## Key Entities

| Entity                    | Description                                                          |
|---------------------------|----------------------------------------------------------------------|
| Task                      | 已有任务模型，`result.total` 为本次任务处理的条目总数               |
| Task.params.business_task | 区分任务类型：`keyword_search` 或 `ecommerce_link_monitor`          |

## Assumptions

- 已完成任务的 `result.total` 字段已正确记录了每次任务实际处理的条目数（含已存在 URL 的复查）
- 前端 `loadTasks()` 已能获取所有任务的 `status`、`params.business_task`、`result.total`
- 前端已有 `_allTasks` 全量任务数据，可以直接在客户端计算，无需新增后端 API（这是最简方案）

## Dependencies

- Task 模型的 `result` JSON 字段已包含 `total` 键
- Task 模型的 `params` JSON 字段已包含 `business_task` 键

## Open Questions

无（需求已由用户确认）
