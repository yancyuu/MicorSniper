# -*- coding: utf-8 -*-
"""Generic AgentBay extraction schema and normalization."""

from typing import Any

from pydantic import BaseModel, Field


class AgentExtractRecord(BaseModel):
    """Agent 通用抽取记录，可表达商品、笔记、评论、回复等任意页面实体。"""

    record_type: str = Field(default="", description="记录类型，如 product/note/comment/reply/user/link/text")
    title: str = Field(default="", description="记录标题或核心文本摘要；没有则为空")
    text: str = Field(default="", description="完整可见正文、评论内容或说明文本；没有则为空")
    url: str = Field(default="", description="真实 http(s) URL 或以 / 开头的站内路径；没有则为空，不要填写 dom_index")
    author: str = Field(default="", description="作者、店铺、账号或发布者；没有则为空")
    published_at: str = Field(default="", description="页面可见的发布时间或评论时间；没有则为空")
    metrics: dict[str, Any] = Field(default_factory=dict, description="点赞、评论数、价格、销量等可量化或标签信息")
    fields: dict[str, Any] = Field(default_factory=dict, description="目标要求的其他字段，按页面语义自由补充；DOM 引用可放 dom_ref")
    children: list[dict[str, Any]] = Field(default_factory=list, description="子记录，如评论下的回复")
    source: str = Field(default="", description="该记录来自页面的哪个区域或线索")


class AgentExtractResult(BaseModel):
    """Agent 每轮观察后的结构化输出。"""

    summary: str = Field(default="", description="当前页面和已获取信息摘要")
    records: list[AgentExtractRecord] = Field(default_factory=list, description="本轮抽取到的通用记录")
    items: list[AgentExtractRecord] = Field(default_factory=list, description="兼容旧字段；新逻辑优先使用 records")
    missing_info: list[str] = Field(default_factory=list, description="为了完成目标还缺的信息")
    next_action: str = Field(default="", description="建议下一步在页面上执行的自然语言动作")
    done: bool = Field(default=False, description="目标是否已经完成")


def extract_mode_kwargs(mode: str) -> dict[str, bool]:
    if mode == "text":
        return {"use_text_extract": True, "use_vision": False}
    if mode == "vision":
        return {"use_text_extract": False, "use_vision": True}
    return {"use_text_extract": True, "use_vision": True}


def normalize_extract_result(payload: AgentExtractResult) -> dict[str, Any]:
    data = payload.model_dump()
    records = data.get("records") or data.get("items") or []
    normalized = []
    for record in records:
        if not isinstance(record, dict):
            continue
        url = str(record.get("url") or "")
        if url.startswith("dom_index:") or (url and not url.startswith(("http://", "https://", "/"))):
            fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
            if url:
                fields.setdefault("dom_ref", url)
            record["fields"] = fields
            record["url"] = ""
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        dom_ref = str(fields.get("dom_ref") or "")
        if dom_ref and not dom_ref.startswith("dom_index:"):
            fields.setdefault("locator_hint", dom_ref)
            fields.pop("dom_ref", None)
            record["fields"] = fields
        normalized.append(record)
    data["records"] = normalized
    data["items"] = []
    return data


def build_extract_instruction(goal: str) -> str:
    return (
        f"目标：{goal}\n"
        "请只依据当前浏览器页面抽取结构化信息。不要编造。"
        "将页面里的商品、笔记、用户、评论、回复、链接或普通文本都表示为 records。"
        "评论和回复请用 record_type=comment/reply，内容放 text，作者放 author，点赞/时间等放 metrics 或 published_at。"
        "url 字段只能填写真实 http(s) URL 或以 / 开头的站内路径；不要把 dom_index 或元素编号填入 url，"
        "如只有元素引用，请放到 fields.dom_ref，并在 next_action 里建议打开该记录以获取真实链接。"
        "fields 里只放语义字段；不要把 CSS 选择器、XPath 或 h1+p 这类定位表达式当成数据。"
        "优先提取真实 url；平台内稳定 ID 通常可从 url 中解析，不需要单独输出。"
        "如果当前列表页看不到真实 url，请留空并在 next_action 中建议打开详情页获取。"
        "如果信息不足，请给出下一步应执行的页面动作；如果目标已完成，done=true。"
    )
