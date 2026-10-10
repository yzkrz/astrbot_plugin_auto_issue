import asyncio
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.session_waiter import (
    USER_SESSIONS,
    SessionController,
    SessionFilter,
    session_waiter,
)

PLUGIN_NAME = "astrbot_plugin_auto_issue"

CANCEL_WORDS = {"取消", "退出", "结束", "算了", "cancel", "quit", "exit"}
START_WORD = "生成"
CONFIRM_PATTERN = re.compile(r"(确认发布|确认|发布|提交|确定|ok|yes|y)\s*([\d\s,，、]*)")
COMMAND_PATTERN = re.compile(r"^\s*/?(issue|反馈)(?:\s+|$)", re.I)

USAGE = (
    "用法：\n"
    "/issue 进入生成模式，之后的对话会被整理成 issue\n"
    "/issue list 列出所有 open 的 issue\n"
    "/issue <编号> 查看 issue 详情\n"
    "/issue <编号> <内容> 为 issue 添加评论"
)

NO_TOKEN_TIP = "你还没有绑定 GitHub Token，无法提交 issue 或评论，请联系管理员在插件配置页绑定。"

BODY_PREVIEW_LIMIT = 800
COMMENT_PREVIEW_LIMIT = 200
DETAIL_COMMENT_COUNT = 3

OUTPUT_FORMAT_PROMPT = """
# 输出格式（必须严格遵守）
只输出一个 JSON 对象，不要输出任何其他文字，不要使用 Markdown 代码块包裹。可选的四种形式：
1. 信息不足，需要继续追问：
{"action": "ask", "message": "发给群友的追问内容"}
2. 信息已经充足，生成 issue 草稿：
{"action": "draft", "issues": [{"title": "issue 标题", "body": "issue 正文（Markdown）"__LABELS_FIELD__, "images": [1, 2]}]}
3. 最新的消息是群友之间的闲聊，并不是在向你反馈，不需要回应：
{"action": "ignore"}
4. 最新的消息是在向你反馈，但内容与本项目的开发无关，拒绝生成 issue：
{"action": "reject", "message": "简短友好地说明为什么不能生成 issue"}

# 相关性规则
- 只有与本项目开发相关的内容才能生成 issue，例如 Bug、功能需求、文档问题、使用问题、性能或兼容性问题。
- 八卦、娱乐、时事、私人话题、广告、与本项目无关的其他软件或产品问题等，一律不能生成 issue：如果是在向你反馈，输出 reject；如果只是群友闲聊，输出 ignore。
- 是否相关以“项目背景”为准；拿不准时可以追问这件事和项目有什么关系。
- 一条消息里既有相关内容又有无关内容时，只为相关部分生成 issue，无关部分直接舍弃。
- 要求立即生成草稿时，如果记录里没有任何与项目相关的内容，输出 reject。
- 收集阶段的群聊记录中可能夹杂群友闲聊，整理时只采纳与项目相关的内容。

# 拆分规则
- 如果群友反馈中包含多个相互独立的问题或需求，必须拆分为多个 issue，放在 issues 数组中，每个 issue 只描述一件事。
- 同一个问题的不同表现、补充信息不要拆分。
- 追问时可以同时针对多个问题提问。
- images 填写与该 issue 相关的图片编号（见群聊记录中的“图片 #编号”），没有相关图片时填 []。

# 图片
- “图片识别结果”是对群友所发图片的识别摘录，请把其中有用的信息（报错、日志、版本号、界面状态等）整理进 issue 正文。
- 与问题无关的图片（表情包等）不要放进 images。
"""

IMAGE_CONTEXT_PROMPT = "发送者在同一条消息中附带的文字：{context}"


@dataclass
class IssueDraft:
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    # 图片编号（从 1 开始），None 表示未指定，附带全部图片
    images: list[int] | None = None


@dataclass
class ImageItem:
    # 聊天平台的原始图片链接，用作 issue 插图；拿不到时为空
    url: str
    # 插件数据目录中的本地副本，用于识图；拿不到时为空
    local_path: str
    sender: str
    # 同一条消息中的文字，帮助模型理解图片
    context: str
    # 模型识别结果，None 表示尚未识别
    description: str | None = None


@dataclass
class IssueSession:
    initiator_id: str
    initiator_name: str
    group_id: str
    platform: str
    # (角色, 发言人, 内容)，角色为 user / assistant
    transcript: list[tuple[str, str, str]] = field(default_factory=list)
    images: list[ImageItem] = field(default_factory=list)
    participants: dict[str, str] = field(default_factory=dict)
    # 是否已发送「生成」开始整理；之前只记录消息，不调用 AI
    started: bool = False
    ask_rounds: int = 0
    drafts: list[IssueDraft] = field(default_factory=list)
    closed: bool = False
    # 记录最近一条消息之前的状态，用于撤销无关消息：(对话条数, 图片数, 参与者)
    mark: tuple[int, int, dict[str, str]] | None = None


class IssueSessionFilter(SessionFilter):
    """群聊内共享一个会话；可选只接管 @机器人 / 引用机器人 / 带唤醒词的消息。"""

    def __init__(self, only_at: bool):
        self.only_at = only_at

    @staticmethod
    def key_of(event: AstrMessageEvent) -> str:
        return f"{PLUGIN_NAME}:{event.unified_msg_origin}"

    def filter(self, event: AstrMessageEvent) -> str:
        if (
            self.only_at
            and not event.is_private_chat()
            and not event.is_at_or_wake_command
        ):
            # 返回一个不会被注册的 ID，让这条消息正常走其他流程
            return ""
        return self.key_of(event)


@register(
    PLUGIN_NAME,
    "YourName",
    "群聊需求转 GitHub Issue：AI 多轮追问补全细节，预览确认后发布",
    "1.4.1",
)
class AutoIssuePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.image_dir: Path | None = None
        try:
            self.image_dir = StarTools.get_data_dir(PLUGIN_NAME) / "images"
            # 清理上次运行残留的图片副本
            shutil.rmtree(self.image_dir, ignore_errors=True)
            self.image_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 无法创建图片目录，将直接使用临时文件识图：{e}")
            self.image_dir = None

    # ------------------------------------------------------------------ 指令

    @filter.command("issue", alias={"反馈"})
    async def issue(self, event: AstrMessageEvent):
        """/issue 新建 | /issue list 列出 open issue | /issue <id> 查看详情 | /issue <id> <内容> 评论"""
        deny_reason = self._check_permission(event) or self._check_configured(event)
        if deny_reason:
            yield event.plain_result(deny_reason)
            return

        _, args = self._strip_command(event.message_str)
        if not args:
            if not self._token_for(str(event.get_sender_id()), write=True):
                yield event.plain_result(NO_TOKEN_TIP)
                return
            async for result in self._run_new_session(event):
                yield result
            return

        query = self._match_query(args)
        if query:
            yield event.plain_result(await self._handle_query(event, query))
        else:
            yield event.plain_result(USAGE)

    def _match_query(self, args: str) -> tuple | None:
        """解析查询类子指令，无法识别时返回 None。"""
        if args.lower() in {"list", "ls", "列表"}:
            return ("list",)
        m = re.match(r"^#?(\d+)(?:\s+(.+))?$", args, re.S)
        if not m:
            return None
        number = int(m.group(1))
        comment = (m.group(2) or "").strip()
        return ("comment", number, comment) if comment else ("detail", number)

    async def _handle_query(self, event: AstrMessageEvent, query: tuple) -> str:
        sender = str(event.get_sender_id())
        if query[0] == "comment" and not self._token_for(sender, write=True):
            return NO_TOKEN_TIP
        try:
            if query[0] == "list":
                return await self._list_issues(self._token_for(sender))
            if query[0] == "detail":
                return await self._issue_detail(query[1], self._token_for(sender))
            return await self._add_comment(event, query[1], query[2])
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] query {query[0]} failed: {e}")
            return f"操作失败：{e}"

    # ------------------------------------------------------------------ 新建会话

    async def _run_new_session(self, event: AstrMessageEvent):
        """进入生成模式，从下一条消息开始记录。"""
        if IssueSessionFilter.key_of(event) in USER_SESSIONS:
            yield event.plain_result("当前会话已有进行中的 issue 收集，请先完成或发送「取消」。")
            return

        state = IssueSession(
            initiator_id=str(event.get_sender_id()),
            initiator_name=event.get_sender_name(),
            group_id=str(event.get_group_id() or ""),
            platform=event.get_platform_name(),
        )
        state.participants[state.initiator_id] = state.initiator_name

        yield event.plain_result(
            "📝 已进入 issue 生成模式，接下来的文字和图片都会被记录。\n"
            "请描述你遇到的问题或想要的功能，可以分多条发送，也可以一次说多个问题"
            "（我会拆分成多个 issue）。\n"
            f"描述完毕后发送「{START_WORD}」，我会整理信息并追问细节；发送「取消」退出。"
        )

        timeout = max(30, int(self.config.get("session_timeout", 300)))

        @session_waiter(timeout=timeout)
        async def waiter(controller: SessionController, ev: AstrMessageEvent):
            await self._on_session_message(controller, ev, state, timeout)

        try:
            await waiter(
                event,
                session_filter=IssueSessionFilter(
                    bool(self.config.get("only_at_in_session", False))
                ),
            )
        except TimeoutError:
            if not state.closed:
                tip = f"（{len(state.drafts)} 个草稿未发布）" if state.drafts else ""
                yield event.plain_result(f"issue 收集会话已超时结束{tip}。")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] session error: {e}", exc_info=True)
            yield event.plain_result(f"issue 收集会话异常结束：{e}")
        finally:
            self._discard_images(state.images)
            event.stop_event()

    async def _on_session_message(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        state: IssueSession,
        timeout: int,
    ):
        is_cmd, text = self._strip_command(event.message_str)
        lowered = text.lower()

        # 会话中也允许使用查询类子指令，不计入对话
        if is_cmd and event.is_at_or_wake_command:
            if not text:
                await event.send(
                    event.plain_result("当前已在 issue 生成模式中，请直接描述问题，或发送「取消」退出。")
                )
                return
            query = self._match_query(text)
            if query:
                deny_reason = self._check_permission(event)
                await event.send(
                    event.plain_result(deny_reason or await self._handle_query(event, query))
                )
                return

        if lowered in CANCEL_WORDS:
            if not self._can_operate(event, state):
                await event.send(event.plain_result("只有发起人或管理员可以取消本次 issue 收集。"))
                return
            state.closed = True
            await event.send(event.plain_result("已取消本次 issue 收集。"))
            controller.stop()
            return

        confirm = CONFIRM_PATTERN.fullmatch(lowered) if state.drafts else None
        if confirm:
            if not self._can_operate(event, state):
                await event.send(event.plain_result("只有发起人或管理员可以确认发布。"))
                return
            await self._publish(controller, event, state, confirm.group(2), timeout)
            return

        if not state.started:
            await self._collect(controller, event, state, text, timeout)
            return

        if not self._record_message(state, event, text):
            return
        controller.keep(timeout=timeout, reset_timeout=True)
        await self._advance(event, state)

    async def _collect(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        state: IssueSession,
        text: str,
        timeout: int,
    ):
        """收集阶段：只记录消息，直到发起人发送「生成」才交给 AI 整理。"""
        if text != START_WORD:
            if self._record_message(state, event, text):
                controller.keep(timeout=timeout, reset_timeout=True)
            return

        if not self._can_operate(event, state):
            await event.send(event.plain_result(f"只有发起人或管理员可以发送「{START_WORD}」开始整理。"))
            return
        # 「生成」消息里附带的图片也要记录
        self._record_message(state, event, "")
        if not any(role == "user" for role, _, _ in state.transcript):
            await event.send(event.plain_result(f"还没有收到任何描述，请先描述问题后再发送「{START_WORD}」。"))
            return

        state.started = True
        controller.keep(timeout=timeout, reset_timeout=True)
        await event.send(event.plain_result("收到，正在整理…"))
        await self._advance(event, state)

    async def _publish(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        state: IssueSession,
        selection: str,
        timeout: int,
    ):
        numbers = {int(n) for n in re.findall(r"\d+", selection)}
        if numbers and not numbers <= set(range(1, len(state.drafts) + 1)):
            await event.send(
                event.plain_result(f"草稿编号无效，当前共有 {len(state.drafts)} 个草稿。")
            )
            return

        # 发布期间不让会话超时
        controller.keep(timeout=timeout + 60 * len(state.drafts), reset_timeout=True)

        published: list[str] = []
        remaining: list[IssueDraft] = []
        error: Exception | None = None
        for idx, draft in enumerate(state.drafts, 1):
            if (numbers and idx not in numbers) or error:
                remaining.append(draft)
                continue
            try:
                url = await self._create_issue(state, draft)
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] create issue failed: {e}")
                error = e
                remaining.append(draft)
                continue
            published.append(f"✅ {draft.title}\n{url}")
            state.transcript.append(("assistant", "助手", f"（已发布 issue：{draft.title}）"))
        state.drafts = remaining

        lines = []
        if published:
            lines += ["Issue 已发布：", *published]
        if error:
            lines.append(f"❌ 发布失败：{error}")
        if remaining:
            lines += ["", f"剩余 {len(remaining)} 个草稿："]
            lines += [f"【{i}】{d.title}" for i, d in enumerate(remaining, 1)]
            lines.append("回复「确认」继续发布（或「确认 1 2」只发布部分），回复修改意见调整草稿，或「取消」放弃。")
            controller.keep(timeout=timeout, reset_timeout=True)
        else:
            lines.append("感谢反馈！")
            state.closed = True
            controller.stop()
        await event.send(event.plain_result("\n".join(lines)))

    async def _advance(self, event: AstrMessageEvent, state: IssueSession):
        """调用 LLM，决定继续追问还是生成草稿。"""
        max_rounds = int(self.config.get("max_rounds", 6))
        must_draft = state.ask_rounds >= max_rounds
        # 发送「生成」后的第一次整理，此时 AI 面对的是整段收集记录
        first = state.ask_rounds == 0 and not state.drafts

        try:
            await self._describe_images(event, state)
            result = await self._call_llm(event, state, must_draft, first)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] llm error: {e}", exc_info=True)
            await event.send(event.plain_result(f"AI 调用失败：{e}\n你可以重新发送消息再试一次。"))
            return

        action = result.get("action")
        if action == "reject" and first:
            # 收集到的内容整体无关：清空记录，回到收集阶段
            message = str(result.get("message") or "").strip() or "这与项目开发无关，无法生成 issue。"
            state.transcript.clear()
            self._discard_images(state.images)
            state.images.clear()
            state.participants = {state.initiator_id: state.initiator_name}
            state.mark = None
            state.started = False
            await event.send(
                event.plain_result(
                    f"{message}\n可以重新描述项目相关的问题，完成后发送「{START_WORD}」，或发送「取消」退出。"
                )
            )
            return

        if action == "ignore" and not first:
            self._rollback(state)
            return

        if action == "reject":
            self._rollback(state)
            message = str(result.get("message") or "").strip() or "这与项目开发无关，无法生成 issue。"
            if state.drafts:
                message += f"\n当前 {len(state.drafts)} 个草稿不受影响，可回复「确认」发布或继续修改。"
            else:
                message += "\n如有项目相关的问题请继续描述，或发送「取消」退出。"
            await event.send(event.plain_result(message))
            return

        drafts = self._parse_drafts(state, result) if action == "draft" else []
        if drafts:
            state.drafts = drafts
            state.transcript.append(
                ("assistant", "助手", f"（已生成 {len(drafts)} 个 issue 草稿，等待确认）")
            )
            await event.send(event.plain_result(self._render_preview(state)))
            return

        message = str(result.get("message") or "").strip()
        if not message:
            message = "能再详细描述一下吗？"
        state.ask_rounds += 1
        state.transcript.append(("assistant", "助手", message))
        await event.send(event.plain_result(message))

    # ------------------------------------------------------------------ LLM

    async def _provider_id(self, event: AstrMessageEvent, key: str = "provider_id") -> str:
        return (
            self.config.get(key)
            or self.config.get("provider_id")
            or await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        )

    async def _describe_images(self, event: AstrMessageEvent, state: IssueSession):
        """用模型识别尚未识别的图片，提取其中的有用信息。"""
        if not self.config.get("image_recognition", True):
            return
        pending = [
            img for img in state.images if img.description is None and (img.local_path or img.url)
        ]
        if not pending:
            return
        provider_id = await self._provider_id(event, "image_provider_id")
        prompt = self.config.get("image_prompt", "").strip() or "请描述这张图片中与软件问题相关的信息。"

        async def describe(img: ImageItem):
            full_prompt = prompt
            if img.context:
                full_prompt += "\n\n" + IMAGE_CONTEXT_PROMPT.format(context=img.context)
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=full_prompt,
                    image_urls=[img.local_path or img.url],
                )
                img.description = (resp.completion_text or "").strip() or "（未识别出内容）"
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] image recognition failed: {e}")
                img.description = "（识别失败）"

        await asyncio.gather(*(describe(img) for img in pending))

    async def _call_llm(
        self, event: AstrMessageEvent, state: IssueSession, must_draft: bool, first: bool
    ) -> dict:
        provider_id = await self._provider_id(event)

        labels = self._available_labels()
        labels_field = ', "labels": ["标签1"]' if labels else ""
        system_prompt = (
            self.config.get("system_prompt", "").strip()
            + "\n"
            + OUTPUT_FORMAT_PROMPT.replace("__LABELS_FIELD__", labels_field)
        )
        if labels:
            system_prompt += f"- labels 只能从以下列表中选择 0~3 个：{json.dumps(labels, ensure_ascii=False)}\n"

        parts = []
        project_context = self.config.get("project_context", "").strip()
        if project_context:
            parts.append(f"# 项目背景\n{project_context}")
        parts.append(f"# 目标仓库\n{self._parse_repo(self.config.get('github_repo', ''))}")
        parts.append("# 群聊记录（按时间顺序）\n" + self._format_transcript(state))
        if state.images:
            lines = [
                f"# 图片识别结果\n群友共发送了 {len(state.images)} 张图片，"
                "发布时会按 images 字段自动附在对应 issue 末尾，正文中无需引用图片链接。"
            ]
            for i, img in enumerate(state.images, 1):
                lines.append(f"- 图片 #{i}（{img.sender} 发送）：{img.description or '（未识别）'}")
            parts.append("\n".join(lines))
        if state.drafts:
            drafts_json = json.dumps(
                [
                    {"title": d.title, "body": d.body, "labels": d.labels, "images": d.images}
                    for d in state.drafts
                ],
                ensure_ascii=False,
            )
            parts.append(
                f"# 当前待发布的草稿\n{drafts_json}\n"
                "草稿已经发给群友预览。如果最新消息是对草稿的修改意见、补充信息或新的问题，"
                "请输出修改后的完整草稿列表（action=draft，包含所有仍需发布的 issue）。"
            )
        if first:
            parts.append(
                "# 要求\n以上是群友在收集阶段发送的全部内容，现在开始整理。"
                "信息不足就追问（ask），信息充足就生成草稿（draft），"
                "全部与项目无关则拒绝（reject），不要输出 ignore。"
            )
        elif must_draft:
            parts.append(
                "# 要求\n不要再追问，请根据已有信息立即输出 action=draft 的 JSON；"
                "如果没有任何与项目相关的内容，输出 action=reject。"
            )
        else:
            parts.append(f"# 状态\n已追问 {state.ask_rounds} 轮。请根据最新消息决定下一步。")

        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt="\n\n".join(parts),
        )
        return self._parse_llm_json(resp.completion_text or "")

    @staticmethod
    def _parse_llm_json(text: str) -> dict:
        text = text.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        candidates = [fenced.group(1)] if fenced else []
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
        for c in candidates:
            try:
                data = json.loads(c)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
        # 模型未按格式输出时，把原文当作追问
        logger.warning(f"[{PLUGIN_NAME}] LLM 输出不是合法 JSON：{text[:200]}")
        return {"action": "ask", "message": text}

    def _parse_drafts(self, state: IssueSession, result: dict) -> list[IssueDraft]:
        items = result.get("issues")
        if not isinstance(items, list):
            # 兼容模型直接在顶层输出 title / body
            items = [result]

        drafts = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            body = str(item.get("body") or "").strip()
            if not title or not body:
                continue
            images = item.get("images")
            if isinstance(images, list):
                images = [
                    int(i)
                    for i in images
                    if str(i).isdigit() and 1 <= int(i) <= len(state.images)
                ]
            else:
                images = None
            drafts.append(IssueDraft(title, body, self._filter_labels(item.get("labels")), images))
        return drafts

    # ------------------------------------------------------------------ GitHub

    async def _github(
        self,
        method: str,
        path: str,
        token: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> tuple[int, Any]:
        repo = self._parse_repo(self.config.get("github_repo", ""))
        api_base = (self.config.get("github_api_base") or "https://api.github.com").rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": PLUGIN_NAME,
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.request(
                method,
                f"{api_base}/repos/{repo}{path}",
                json=payload,
                params=params,
                headers=headers,
            ) as resp:
                return resp.status, await resp.json(content_type=None)

    @staticmethod
    def _api_error(status: int, data) -> RuntimeError:
        if status == 401:
            return RuntimeError("GitHub Token 无效或已过期，请联系管理员更新")
        if status == 404:
            return RuntimeError("未找到（请检查 issue 编号、仓库地址及 Token 权限）")
        message = data.get("message") if isinstance(data, dict) else data
        return RuntimeError(f"GitHub API 返回 {status}：{message}")

    async def _create_issue(self, state: IssueSession, draft: IssueDraft) -> str:
        payload: dict = {"title": draft.title, "body": self._build_body(state, draft)}
        if draft.labels:
            payload["labels"] = draft.labels

        token = self._token_for(state.initiator_id, write=True)
        if not token:
            raise RuntimeError("发起人未绑定 GitHub Token")
        status, data = await self._github("POST", "/issues", token, payload)
        if status == 422 and "labels" in payload:
            # 标签无效时去掉标签重试
            logger.warning(f"[{PLUGIN_NAME}] 带标签创建失败，去掉标签重试：{data}")
            payload.pop("labels")
            status, data = await self._github("POST", "/issues", token, payload)

        if status != 201:
            raise self._api_error(status, data)
        return data.get("html_url", "")

    async def _list_issues(self, token: str) -> str:
        limit = max(1, int(self.config.get("list_limit", 50)))
        issues: list[dict] = []
        has_more = False
        page = 1
        while True:
            status, data = await self._github(
                "GET",
                "/issues",
                token,
                params={"state": "open", "per_page": 100, "page": page},
            )
            if status != 200:
                raise self._api_error(status, data)
            # issues 接口会同时返回 PR，这里过滤掉
            issues += [i for i in data if "pull_request" not in i]
            if len(issues) > limit:
                has_more = True
                break
            if len(data) < 100:
                break
            page += 1

        repo = self._parse_repo(self.config.get("github_repo", ""))
        if not issues:
            return f"{repo} 当前没有 open 的 issue。"

        lines = [f"📋 {repo} 的 open issue："]
        for i in issues[:limit]:
            labels = " ".join(f"[{lb['name']}]" for lb in i.get("labels", []))
            lines.append(f"#{i['number']} {i['title']} {labels}".rstrip())
        if has_more:
            lines.append(f"……仅显示前 {limit} 个，更多请访问 https://github.com/{repo}/issues")
        lines.append("发送 /issue <编号> 查看详情")
        return "\n".join(lines)

    async def _issue_detail(self, number: int, token: str) -> str:
        status, data = await self._github("GET", f"/issues/{number}", token)
        if status != 200:
            raise self._api_error(status, data)

        kind = "PR" if "pull_request" in data else "Issue"
        labels = ", ".join(lb["name"] for lb in data.get("labels", []))
        lines = [
            f"{kind} #{data['number']} [{data['state']}] {data['title']}",
            f"作者：{data['user']['login']} | 创建：{data['created_at'][:10]} | 评论：{data['comments']}",
        ]
        if labels:
            lines.append(f"标签：{labels}")
        lines += [data["html_url"], "", self._truncate(data.get("body") or "（无正文）", BODY_PREVIEW_LIMIT)]

        if data["comments"]:
            # 取最后一页的末尾几条评论
            last_page = (data["comments"] - 1) // 100 + 1
            c_status, comments = await self._github(
                "GET", f"/issues/{number}/comments", token, params={"per_page": 100, "page": last_page}
            )
            if c_status == 200 and comments:
                lines += ["", f"💬 最近 {min(len(comments), DETAIL_COMMENT_COUNT)} 条评论："]
                for c in comments[-DETAIL_COMMENT_COUNT:]:
                    lines.append(
                        f"- {c['user']['login']}（{c['created_at'][:10]}）："
                        f"{self._truncate(c.get('body') or '', COMMENT_PREVIEW_LIMIT)}"
                    )
        return "\n".join(lines)

    async def _add_comment(self, event: AstrMessageEvent, number: int, text: str) -> str:
        body = text
        if self.config.get("include_images", True):
            urls = [url for url, _ in self._extract_images(event) if url]
            if urls:
                body += "\n\n" + "\n".join(f"![image{i}]({u})" for i, u in enumerate(urls, 1))
        if self.config.get("append_submitter_info", True):
            source = f"群 {event.get_group_id()} " if event.get_group_id() else ""
            body += (
                f"\n\n---\n\n> 由 {event.get_sender_name()} ({event.get_sender_id()}) "
                f"在 {event.get_platform_name()} {source}通过 AstrBot 提交"
            )

        token = self._token_for(str(event.get_sender_id()), write=True)
        status, data = await self._github(
            "POST", f"/issues/{number}/comments", token, {"body": body}
        )
        if status != 201:
            raise self._api_error(status, data)
        return f"已评论 #{number}：{data.get('html_url', '')}"

    def _build_body(self, state: IssueSession, draft: IssueDraft) -> str:
        body = draft.body

        if self.config.get("include_images", True):
            if draft.images is None:
                urls = [img.url for img in state.images]
            else:
                urls = [state.images[i - 1].url for i in draft.images]
            urls = [u for u in urls if u]
            if urls:
                images = "\n".join(f"![image{i}]({u})" for i, u in enumerate(urls, 1))
                body += f"\n\n### 附件图片\n\n{images}"

        if self.config.get("append_submitter_info", True):
            initiator = f"{state.initiator_name} ({state.initiator_id})"
            others = [
                f"{name} ({uid})"
                for uid, name in state.participants.items()
                if uid != state.initiator_id
            ]
            lines = [
                "> 本 issue 由 AstrBot 从用户交流群整理后自动提交",
                f"> - 平台：{state.platform}",
            ]
            if state.group_id:
                lines.append(f"> - 来源群：{state.group_id}")
            lines.append(f"> - 发起人：{initiator}")
            if others:
                lines.append(f"> - 参与者：{'、'.join(others)}")
            lines.append(f"> - 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
            body += "\n\n---\n\n" + "\n".join(lines)

        return body

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _strip_command(message: str) -> tuple[bool, str]:
        """去掉开头的 issue / 反馈 指令名，返回 (是否为指令, 剩余内容)。"""
        message = (message or "").strip()
        m = COMMAND_PATTERN.match(message)
        if not m:
            return False, message
        return True, message[m.end() :].strip()

    def _record_message(self, state: IssueSession, event: AstrMessageEvent, text: str) -> bool:
        """记录一条群友消息，返回是否包含有效内容。"""
        self._save_mark(state)
        first_new = len(state.images) + 1
        new_images = self._record_images(state, event, text)
        if not text and not new_images:
            return False
        content = text
        if new_images:
            tags = " ".join(f"#{i}" for i in range(first_new, first_new + new_images))
            content += f"（发送了图片 {tags}）"
        state.transcript.append(("user", self._speaker(event), content.strip()))
        state.participants[str(event.get_sender_id())] = event.get_sender_name()
        return True

    @staticmethod
    def _save_mark(state: IssueSession):
        state.mark = (len(state.transcript), len(state.images), dict(state.participants))

    @staticmethod
    def _rollback(state: IssueSession):
        """撤销最近一条被判定为无关的消息，避免混入 issue 和参与者名单。"""
        if state.mark is None:
            return
        transcript_len, image_len, participants = state.mark
        del state.transcript[transcript_len:]
        self._discard_images(state.images[image_len:])
        del state.images[image_len:]
        state.participants = participants
        state.mark = None

    def _record_images(self, state: IssueSession, event: AstrMessageEvent, context: str) -> int:
        known = {img.url for img in state.images if img.url}
        count = 0
        for url, local in self._extract_images(event):
            if url and url in known:
                continue
            state.images.append(ImageItem(url, self._copy_image(local), self._speaker(event), context))
            if url:
                known.add(url)
            count += 1
        return count

    def _extract_images(self, event: AstrMessageEvent) -> list[tuple[str, str]]:
        """返回消息中每张图片的 (原始链接, 本地文件)。

        AstrBot 的预处理阶段会把图片下载为本地临时文件，并用本地路径覆盖组件的 url，
        所以原始链接需要从平台的原始消息中找回。
        """
        raw_urls = self._raw_image_urls(event)
        result = []
        index = 0
        for comp in event.get_messages():
            if not isinstance(comp, Comp.Image):
                continue
            refs = [r for r in (comp.url, comp.file, comp.path) if r]
            url = next((r for r in refs if self._is_http(r)), "")
            if not url and index < len(raw_urls):
                url = raw_urls[index]
            local = next((r for r in refs if not self._is_http(r) and Path(r).is_file()), "")
            index += 1
            if url or local:
                result.append((url, local))
        return result

    def _raw_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """从 OneBot（aiocqhttp）原始消息中按顺序取出图片链接，取不到的位置为空字符串。"""
        raw = getattr(event.message_obj, "raw_message", None)
        try:
            segments = raw.get("message") if hasattr(raw, "get") else getattr(raw, "message", None)
        except Exception:
            return []
        if not isinstance(segments, list):
            return []
        urls = []
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") == "image":
                data = seg.get("data") or {}
                url = str(data.get("url") or data.get("file") or "")
                urls.append(url if self._is_http(url) else "")
        return urls

    def _copy_image(self, local: str) -> str:
        """把预处理阶段的临时文件复制到插件目录，避免在发送「生成」前被清理。"""
        if not local or self.image_dir is None:
            return local
        try:
            target = self.image_dir / f"{uuid.uuid4().hex}{Path(local).suffix}"
            shutil.copyfile(local, target)
            return str(target)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 复制图片失败：{e}")
            return local

    def _discard_images(self, images: list[ImageItem]):
        if self.image_dir is None:
            return
        for img in images:
            path = img.local_path
            if path and Path(path).parent == self.image_dir:
                Path(path).unlink(missing_ok=True)

    @staticmethod
    def _is_http(ref: str) -> bool:
        return ref.startswith(("http://", "https://"))

    @staticmethod
    def _speaker(event: AstrMessageEvent) -> str:
        return f"{event.get_sender_name()}({event.get_sender_id()})"

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = text.strip()
        return text if len(text) <= limit else text[:limit] + "……"

    @staticmethod
    def _format_transcript(state: IssueSession) -> str:
        lines = []
        for role, speaker, content in state.transcript:
            prefix = "助手" if role == "assistant" else f"群友 {speaker}"
            lines.append(f"[{prefix}]: {content}")
        return "\n".join(lines)

    def _render_preview(self, state: IssueSession) -> str:
        multi = len(state.drafts) > 1
        lines = [f"📝 共 {len(state.drafts)} 个 Issue 草稿" if multi else "📝 Issue 草稿预览"]
        for idx, d in enumerate(state.drafts, 1):
            lines += ["", f"【{idx}】{d.title}" if multi else f"标题：{d.title}"]
            if d.labels:
                lines.append(f"标签：{', '.join(d.labels)}")
            picked = state.images if d.images is None else [state.images[i - 1] for i in d.images]
            image_count = sum(1 for img in picked if img.url)
            if image_count and self.config.get("include_images", True):
                lines.append(f"附件图片：{image_count} 张")
            lines += ["", d.body]
        lines += ["", "———"]
        if multi:
            lines.append("回复「确认」全部发布，「确认 1 3」只发布指定编号，「取消」放弃，或直接回复修改意见。")
        else:
            lines.append("回复「确认」发布，「取消」放弃，或直接回复修改意见。")
        return "\n".join(lines)

    def _available_labels(self) -> list[str]:
        if not self.config.get("auto_label", True):
            return []
        return [str(x).strip() for x in self.config.get("available_labels", []) if str(x).strip()]

    def _filter_labels(self, labels) -> list[str]:
        if not isinstance(labels, list):
            return []
        allowed = set(self._available_labels())
        return [str(x).strip() for x in labels if str(x).strip() in allowed][:3]

    def _check_configured(self, event: AstrMessageEvent) -> str | None:
        if not self._parse_repo(self.config.get("github_repo", "")) or not self._token_for(
            str(event.get_sender_id())
        ):
            return "插件尚未配置 GitHub 仓库或 Token，请联系管理员在插件配置页填写。"
        return None

    def _token_for(self, user_id: str, write: bool = False) -> str:
        """获取用户使用的 Token：优先使用绑定的 Token，否则使用默认 Token。

        write 为 True 且关闭了「未绑定用户使用默认 Token」时，未绑定用户返回空字符串。
        """
        for item in self.config.get("user_tokens", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("user_id", "")).strip() == user_id:
                token = str(item.get("token", "")).strip()
                if token:
                    return token
        if write and not self.config.get("fallback_to_default_token", True):
            return ""
        return str(self.config.get("github_token", "")).strip()

    def _check_permission(self, event: AstrMessageEvent) -> str | None:
        """返回拒绝原因，允许时返回 None。"""
        if event.is_private_chat():
            if not self.config.get("allow_private", True):
                return "本功能仅限在群聊中使用。"
        else:
            groups = {str(g).strip() for g in self.config.get("group_whitelist", []) if str(g).strip()}
            if groups and str(event.get_group_id()) not in groups:
                return "本群未开启 issue 反馈功能。"
            if self.config.get("require_at", True) and not self._is_at_bot(event):
                return "请 @我 并发送 /issue 指令。"

        users = {str(u).strip() for u in self.config.get("user_whitelist", []) if str(u).strip()}
        if users and not event.is_admin() and str(event.get_sender_id()) not in users:
            return "你没有使用 issue 功能的权限。"
        return None

    def _can_operate(self, event: AstrMessageEvent, state: IssueSession) -> bool:
        sender = str(event.get_sender_id())
        if sender == state.initiator_id or event.is_admin():
            return True
        users = {str(u).strip() for u in self.config.get("user_whitelist", []) if str(u).strip()}
        return sender in users

    @staticmethod
    def _is_at_bot(event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id())
        for comp in event.get_messages():
            if isinstance(comp, Comp.At) and str(comp.qq) == self_id:
                return True
            if isinstance(comp, Comp.Reply) and str(comp.sender_id) == self_id:
                return True
        return False

    @staticmethod
    def _parse_repo(raw: str) -> str:
        raw = (raw or "").strip()
        raw = re.sub(r"^(https?://)?(www\.)?github\.com/", "", raw)
        raw = re.sub(r"\.git$", "", raw).strip("/")
        parts = raw.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 and all(parts[:2]) else ""
