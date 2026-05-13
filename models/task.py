# -*- coding: utf-8 -*-
"""任务模型 - AI Native 设计，记录任务执行过程和结果"""

from enum import Enum
from tortoise.models import Model
from tortoise.fields import (
    CharField, IntField, BooleanField, DatetimeField, 
    TextField, UUIDField, JSONField
)
import uuid
from datetime import datetime, timedelta


class TaskStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN_INPUT = "waiting_human_input"  # 等待人类输入（通用）
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(Model):
    """
    任务模型 - AI Native 设计
    
    设计理念：
    1. 记录任务执行过程中的每一步上下文
    2. Agent 可以查看上下文，决定下一步操作
    3. Service 负责实际执行，Task 负责记录
    4. 结果存储为自然语言文本，AI 可直接阅读
    """

    # 基础信息
    id = UUIDField(pk=True, default=uuid.uuid4, description="任务唯一标识")
    source = CharField(50, description="请求来源：system, api, user等")
    source_id = CharField(100, description="请求来源ID：用户ID、服务名等")
    task_type = CharField(50, description="任务类型")
    context_id = UUIDField(null=True, description="关联的浏览器上下文ID")
    params = JSONField(null=True, description="任务执行参数")
    screenshot_url = CharField(500, default="", description="最新截图URL")
    browser_url = CharField(2000, default="", description="浏览器实时访问URL")

    # 状态管理
    status = CharField(20, default=TaskStatus.PENDING.value, description="任务状态")
    progress = IntField(default=0, description="任务进度 0-100")
    
    # 结果和错误（AI Native：存储自然语言文本）
    result = JSONField(null=True, description="任务最终结果（AI可读的自然语言格式）")
    error = TextField(null=True, description="错误信息（自然语言描述）")

    # 执行日志 - 记录每一步的输入输出（本身就是上下文）
    logs = JSONField(default=lambda : [], description="执行日志，记录每一步的详细信息")

    # 时间戳
    created_at = DatetimeField(auto_now_add=True, description="创建时间")
    started_at = DatetimeField(null=True, description="开始时间")
    completed_at = DatetimeField(null=True, description="完成时间")

    # 定时任务
    schedule = CharField(100, default="", description="定时规则（秒数或cron表达式）")
    last_run_at = DatetimeField(null=True, description="上次执行时间")
    not_before_at = DatetimeField(null=True, description="最早启动时间，用于分批延迟任务")

    class Meta:
        table = "tasks"
        indexes = [
            ("source_id", "status"),
            ("task_type", "status"),
            ("created_at",),
        ]

    async def _pre_save(self, using_db=None, update_fields=None):
        self._normalize_datetime_fields()
        await super()._pre_save(using_db, update_fields)

    # ===== 任务状态管理方法 =====

    def _normalize_datetime_fields(self):
        """项目使用无时区时间；避免旧数据中的 aware datetime 保存时报错。"""
        for field in ["started_at", "completed_at", "last_run_at", "not_before_at"]:
            value = getattr(self, field, None)
            if value is not None and getattr(value, "tzinfo", None) is not None:
                setattr(self, field, value.replace(tzinfo=None))

    async def start(self):
        """开始执行任务"""
        self.status = TaskStatus.RUNNING
        self.started_at = datetime.now()
        self._normalize_datetime_fields()
        await self.save()


    async def wait_for_human_input(
        self,
        interaction_type: str,
        data: dict,
        resume_point: str = None,
        timeout_seconds: int = 120,
        step: int = None
    ):
        """等待人类交互"""
        self.status = TaskStatus.WAITING_HUMAN_INPUT

        self.result = {
            "human_interaction_required": True,
            "interaction_type": interaction_type,
            "data": data,
        }

        step_name = {
            "login_confirm": "等待登录确认",
            "content_review": "等待内容审核",
        }.get(interaction_type, f"等待人类交互: {interaction_type}")

        await self.log_step(
            step=step or len(self.logs),
            name=step_name,
            input_data={"interaction_type": interaction_type},
            output_data=data,
            status="pending"
        )

        self._normalize_datetime_fields()
        await self.save()

    async def complete(self, result_data: dict = None):
        """完成任务并上传结果到OSS"""
        self.status = TaskStatus.COMPLETED
        self.completed_at = datetime.now()
        self.progress = 100

        if result_data:
            # 如果结果包含output，上传到OSS
            if 'output' in result_data:
                from utils.oss import oss_client
                import json

                # 准备上传内容
                output_content = result_data['output']

                # 生成文件名：tasks/年/月/任务ID_任务类型.txt
                date_prefix = self.completed_at.strftime("%Y/%m")
                filename = f"tasks/{date_prefix}/{self.id}_{self.task_type}.txt"

                try:
                    # 上传到OSS
                    await oss_client.upload_file(filename, output_content.encode('utf-8'))

                    # 生成下载URL
                    download_url = oss_client.get_public_url(filename)

                    # 更新result，添加output_url
                    result_data['output_url'] = download_url

                    from utils.logger import logger
                    logger.info(f"任务结果已上传到OSS: {download_url}")
                except Exception as e:
                    from utils.logger import logger
                    logger.error(f"上传任务结果到OSS失败: {e}")

            self.result = result_data
        self._normalize_datetime_fields()
        await self.save()

    async def fail(self, error_msg: str, current_progress: int = None):
        """标记任务失败"""
        self.status = TaskStatus.FAILED
        self.completed_at = datetime.now()
        self.error = error_msg
        if current_progress is not None:
            self.progress = current_progress
        self._normalize_datetime_fields()
        await self.save()

    async def cancel(self):
        """取消任务"""
        self.status = TaskStatus.CANCELLED
        self.completed_at = datetime.now()
        self.not_before_at = None
        self._normalize_datetime_fields()
        await self.save()

    # ===== 日志管理方法 =====

    async def log_step(self, step: int, name: str, input_data: dict, output_data: dict, status: str = "completed"):
        """
        记录一步执行。如果同 step 已存在则更新，否则追加。
        """
        log_entry = {
            "step": step,
            "name": name,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "input": input_data,
            "output": output_data,
            "status": status
        }
        for i, existing in enumerate(self.logs):
            if existing.get("step") == step:
                self.logs[i] = log_entry
                self._normalize_datetime_fields()
                await self.save()
                return
        self.logs.append(log_entry)
        self._normalize_datetime_fields()
        await self.save()

    # ===== 商品链接查询 =====

    async def get_links(self, offset: int = 0, limit: int = 100, platform: str = None, keyword: str = None):
        """分页查询任务采集的商品链接"""
        from models.product_link import ProductLink
        query = ProductLink.filter(task_id=self.id)
        if platform:
            query = query.filter(platform=platform)
        if keyword:
            query = query.filter(keyword=keyword)
        total = await query.count()
        items = await query.order_by("created_at").offset(offset).limit(limit)
        return {
            "items": [item.to_dict() for item in items],
            "total": total,
        }

    async def get_link_count(self) -> int:
        """获取链接总数"""
        from models.product_link import ProductLink
        return await ProductLink.filter(task_id=self.id).count()

    # ===== AI 可读格式转换 =====

    def estimate_completion_at(self) -> datetime | None:
        """基于进度百分比估算完成时间。"""
        if not self.started_at or self.progress >= 100:
            return self.completed_at
        if self.progress <= 0:
            return None

        started = self.started_at
        if getattr(started, 'tzinfo', None):
            started = started.replace(tzinfo=None)
        now = datetime.now()
        elapsed = (now - started).total_seconds()
        if elapsed <= 0:
            return None

        # 用 progress 百分比估算：已花时间 / 已完成百分比 * 剩余百分比
        remaining_pct = 100 - self.progress
        eta_seconds = elapsed * remaining_pct / self.progress

        # 加上剩余批次的间隔时间
        params = self.params or {}
        batch_size = int(params.get("batch_size") or 6)
        if batch_size <= 0:
            batch_size = 6
        total = int(params.get("total_urls") or 0)
        done = max(1, int(self.progress / 100 * total)) if total else 0
        remaining = max(0, total - done) if total else 0
        remaining_batches = remaining // batch_size
        interval_min = int(params.get("batch_interval_minutes") or 0)
        if interval_min > 0 and remaining_batches > 0:
            eta_seconds += remaining_batches * (interval_min + 5) * 60

        return now + timedelta(seconds=eta_seconds)

    async def to_agent_readable(self) -> dict:
        """
        转换为 Agent 可读的格式 - AI Native 核心方法
        
        Returns:
            包含任务完整信息的字典，适合 LLM 理解
        """
        # 构建自然语言摘要
        summary_parts = []
        summary_parts.append(f"任务类型: {self.task_type}")
        summary_parts.append(f"当前状态: {self.status}")
        summary_parts.append(f"执行进度: {self.progress}%")
        
        if self.error:
            summary_parts.append(f"错误信息: {self.error}")
        
        # 构建日志摘要
        if self.logs:
            log_summary = f"已执行 {len(self.logs)} 个步骤："
            for log in self.logs:
                log_summary += f"\n  - 步骤{log['step']}: {log['name']} ({log['status']})"
            summary_parts.append(log_summary)
        
        # 构建结果摘要
        if self.result:
            if isinstance(self.result, dict):
                if 'analysis' in self.result:
                    summary_parts.append(f"分析结果: {self.result['analysis'][:100]}...")
                elif 'report' in self.result:
                    summary_parts.append(f"报告: {self.result['report'][:100]}...")
                else:
                    summary_parts.append(f"结果: {str(self.result)[:100]}...")
        
        eta = self.estimate_completion_at()

        # 查询关联的 context 名称
        context_name = None
        if self.context_id:
            try:
                from models.context import BrowserContext
                ctx = await BrowserContext.filter(id=self.context_id).first()
                if ctx:
                    context_name = ctx.name or f"{ctx.platform}-{str(ctx.id)[:6]}"
            except Exception:
                pass

        return {
            "task_id": str(self.id),
            "task_type": self.task_type,
            "status": self.status,
            "progress": self.progress,
            "context_id": str(self.context_id) if self.context_id else None,
            "context_name": context_name,
            "browser_url": self.browser_url,
            "screenshot_url": self.screenshot_url,
            "summary": "\n".join(summary_parts),
            "result": self.result,
            "params": self.params,
            "error": self.error,
            "logs": self.logs,
            "schedule": self.schedule,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "not_before_at": self.not_before_at.isoformat() if self.not_before_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "estimated_completion_at": eta.isoformat() if eta else None,
            "next_step_hint": self._get_next_step_hint()
        }

    def _get_next_step_hint(self) -> str:
        """生成下一步操作提示（自然语言）"""
        if self.status == TaskStatus.PENDING:
            return "任务等待开始，请检查前置条件"
        elif self.status == TaskStatus.RUNNING:
            completed_steps = len([l for l in self.logs if l.get('status') == 'completed'])
            return f"任务执行中，已完成 {completed_steps} 个步骤，当前进度 {self.progress}%"
        elif self.status == TaskStatus.WAITING_HUMAN_INPUT:
            # 从 result 中获取交互类型
            if isinstance(self.result, dict):
                interaction_type = self.result.get("interaction_type", "unknown")
                return f"任务等待人类交互: {interaction_type}，请处理请求后任务将自动继续"
            return "任务等待人类交互，请处理请求后任务将自动继续"
        elif self.status == TaskStatus.COMPLETED:
            return "任务已完成，可查看执行结果和报告"
        elif self.status == TaskStatus.FAILED:
            return f"任务失败: {self.error or '未知错误'}，请查看日志了解详情"
        elif self.status == TaskStatus.CANCELLED:
            return "任务已被取消"
        return "未知状态"

    def get_result_text(self) -> str:
        """
        获取结果的纯文本格式 - AI Native
        
        Returns:
            适合 AI 阅读的纯文本结果
        """
        if not self.result:
            return "暂无结果"
        
        if isinstance(self.result, dict):
            # 提取分析或报告
            if 'analysis' in self.result:
                return self.result['analysis']
            elif 'report' in self.result:
                return self.result['report']
            else:
                # 转换为自然语言描述
                lines = []
                for key, value in self.result.items():
                    lines.append(f"{key}: {value}")
                return "\n".join(lines)
        
        return str(self.result)

    def get_logs_summary(self) -> str:
        """
        获取日志摘要 - AI Native
        
        Returns:
            适合 AI 阅读的日志摘要
        """
        if not self.logs:
            return "暂无执行日志"
        
        lines = [f"任务执行日志（共 {len(self.logs)} 步）:"]
        
        for log in self.logs:
            status_icon = "✓" if log.get('status') == 'completed' else "✗"
            lines.append(f"\n{status_icon} 步骤 {log['step']}: {log['name']}")
            lines.append(f"  时间: {log['timestamp']}")
            
            if log.get('input'):
                lines.append(f"  输入: {self._format_dict(log['input'])}")
            if log.get('output'):
                lines.append(f"  输出: {self._format_dict(log['output'])}")
        
        return "\n".join(lines)

    def _format_dict(self, data: dict, max_len: int = 100) -> str:
        """格式化字典为简短的字符串"""
        if not data:
            return ""
        
        items = []
        for key, value in list(data.items())[:3]:  # 只取前3个
            value_str = str(value)
            if len(value_str) > max_len:
                value_str = value_str[:max_len] + "..."
            items.append(f"{key}={value_str}")
        
        return ", ".join(items)