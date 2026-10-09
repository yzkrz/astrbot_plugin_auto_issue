import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.session_waiter import (
    USER_SESSIONS,
    SessionController,
    SessionFilter,
    session_waiter,
)

PLUGIN_NAME = "astrbot_plugin_kunter_auto_issue"

CANCEL_WORDS = {"取消", "退出", "结束", "算了", "cancel", "quit", "exit"}
FORCE_DRAFT_WORDS = {"生成", "直接生成", "生成草稿", "够了", "就这些", "没有了"}
CONFIRM_PATTERN = re.compile(r"(确认发布|确认|发布|提交|确定|ok|yes|y)\s*([\d\s,，、]*)")
COMMAND_PATTERN = re.compile(r"^\s*/?(issue|反馈)(?:\s+|$)", re.I)

USAGE = (
    "用法：\n"
    "/issue 进入生成模式，之后的对话会被整理成 issue\n"
    "/issue list 列出所有 open 的 issue\n"
    "/issue <编号> 查看 issue 详情\n"
    "/issue <编号> <内容> 为 issue 添加评论"
)

BODY_PREVIEW_LIMIT = 800
COMMENT_PREVIEW_LIMIT = 200
DETAIL_COMMENT_COUNT = 3

OUTPUT_FORMAT_PROMPT = """
# 输出格式（必须严格遵守）
只输出一个 JSON 对象，不要输出任何其他文字，不要使用 Markdown 代码块包裹。可选的三种形式：
1. 信息不足，需要继续追问：
{"action": "ask", "message": "发给群友的追问内容"}
2. 信息已经充足，生成 issue 草稿：
{"action": "draft", "issues": [{"title": "issue 标题", "body": "issue 正文（Markdown）"__LABELS_FIELD__, "images": [1, 2]}]}
3. 最新的消息是与当前 issue 无关的群聊闲聊，不需要回应：
{"action": "ignore"}

# 拆分规则
- 如果群友反馈中包含多个相互独立的问题或需求，必须拆分为多个 issue，放在 issues 数组中，每个 issue 只描述一件事。
- 同一个问题的不同表现、补充信息不要拆分。
- 追问时可以同时针对多个问题提问。
- images 填写与该 issue 相关的图片编号（见群聊记录中的“图片 #编号”），没有相关图片时填 []。
"""


@dataclass
class IssueDraft:
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    # 图片编号（从 1 开始），None 表示未指定，附带全部图片
    images: list[int] | None = None


@dataclass
class IssueSession:
    initiator_id: str
    initiator_name: str
    group_id: str
    platform: str
    # (角色, 发言人, 内容)，角色为 user / assistant
    transcript: list[tuple[str, str, str]] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    participants: dict[str, str] = field(default_factory=dict)
    ask_rounds: int = 0
    drafts: list[IssueDraft] = field(default_factory=list)
    closed: bool = False


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
    "1.1.0",
)
class AutoIssuePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ------------------------------------------------------------------ 指令

    @filter.command("issue", alias={"反馈"})
    async def issue(self, event: AstrMessageEvent):
        """/issue 新建 | /issue list 列出 open issue | /issue <id> 查看详情 | /issue <id> <内容> 评论"""
        deny_reason = self._check_permission(event) or self._check_configured()
        if deny_reason:
            yield event.plain_result(deny_reason)
            return

        _, args = self._strip_command(event.message_str)
        if not args:
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
        try:
            if query[0] == "list":
                return await self._list_issues()
            if query[0] == "detail":
                return await self._issue_detail(query[1])
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
            "📝 已进入 issue 生成模式，接下来的消息都会被记录。\n"
            "请描述你遇到的问题或想要的功能（可以一次说多个，我会拆分成多个 issue），"
            "我会继续追问一些细节后整理成 issue。\n"
            "发送「生成」直接出草稿，「取消」退出。"
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

        force_draft = lowered in FORCE_DRAFT_WORDS
        if force_draft:
            state.transcript.append(
                ("user", self._speaker(event), "（请根据已有信息直接生成 issue 草稿）")
            )
            self._record_images(state, event)
        elif not self._record_message(state, event, text):
            return

        controller.keep(timeout=timeout, reset_timeout=True)
        await self._advance(event, state, force_draft=force_draft)

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

    async def _advance(
        self, event: AstrMessageEvent, state: IssueSession, force_draft: bool = False
    ):
        """调用 LLM，决定继续追问还是生成草稿。"""
        max_rounds = int(self.config.get("max_rounds", 6))
        must_draft = force_draft or state.ask_rounds >= max_rounds

        try:
            result = await self._call_llm(event, state, must_draft)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] llm error: {e}", exc_info=True)
            await event.send(event.plain_result(f"AI 调用失败：{e}\n你可以重新发送消息再试一次。"))
            return

        action = result.get("action")
        if action == "ignore" and not force_draft:
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

    async def _call_llm(
        self, event: AstrMessageEvent, state: IssueSession, must_draft: bool
    ) -> dict:
        provider_id = self.config.get("provider_id") or (
            await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        )

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
        if state.image_urls:
            parts.append(
                f"# 附件\n群友共发送了 {len(state.image_urls)} 张图片，"
                "发布时会按 images 字段自动附在对应 issue 末尾，正文中无需引用图片链接。"
            )
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
        if must_draft:
            parts.append("# 要求\n不要再追问，请根据已有信息立即输出 action=draft 的 JSON。")
        else:
            parts.append(f"# 状态\n已追问 {state.ask_rounds} 轮。请根据最新消息决定下一步。")

        image_urls = None
        if self.config.get("send_images_to_llm", False) and state.image_urls:
            image_urls = state.image_urls

        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt="\n\n".join(parts),
            image_urls=image_urls,
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
                    if str(i).isdigit() and 1 <= int(i) <= len(state.image_urls)
                ]
            else:
                images = None
            drafts.append(IssueDraft(title, body, self._filter_labels(item.get("labels")), images))
        return drafts

    # ------------------------------------------------------------------ GitHub

    async def _github(
        self, method: str, path: str, payload: dict | None = None, params: dict | None = None
    ) -> tuple[int, Any]:
        repo = self._parse_repo(self.config.get("github_repo", ""))
        api_base = (self.config.get("github_api_base") or "https://api.github.com").rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.config.get('github_token', '')}",
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
        if status == 404:
            return RuntimeError("未找到（请检查 issue 编号、仓库地址及 Token 权限）")
        message = data.get("message") if isinstance(data, dict) else data
        return RuntimeError(f"GitHub API 返回 {status}：{message}")

    async def _create_issue(self, state: IssueSession, draft: IssueDraft) -> str:
        payload: dict = {"title": draft.title, "body": self._build_body(state, draft)}
        if draft.labels:
            payload["labels"] = draft.labels

        status, data = await self._github("POST", "/issues", payload)
        if status == 422 and "labels" in payload:
            # 标签无效时去掉标签重试
            logger.warning(f"[{PLUGIN_NAME}] 带标签创建失败，去掉标签重试：{data}")
            payload.pop("labels")
            status, data = await self._github("POST", "/issues", payload)

        if status != 201:
            raise self._api_error(status, data)
        return data.get("html_url", "")

    async def _list_issues(self) -> str:
        limit = max(1, int(self.config.get("list_limit", 50)))
        issues: list[dict] = []
        has_more = False
        page = 1
        while True:
            status, data = await self._github(
                "GET",
                "/issues",
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

    async def _issue_detail(self, number: int) -> str:
        status, data = await self._github("GET", f"/issues/{number}")
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
                "GET", f"/issues/{number}/comments", params={"per_page": 100, "page": last_page}
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
            urls = [u for u in (self._image_url(c) for c in event.get_messages()) if u]
            if urls:
                body += "\n\n" + "\n".join(f"![image{i}]({u})" for i, u in enumerate(urls, 1))
        if self.config.get("append_submitter_info", True):
            source = f"群 {event.get_group_id()} " if event.get_group_id() else ""
            body += (
                f"\n\n---\n\n> 由 {event.get_sender_name()} ({event.get_sender_id()}) "
                f"在 {event.get_platform_name()} {source}通过 AstrBot 提交"
            )

        status, data = await self._github("POST", f"/issues/{number}/comments", {"body": body})
        if status != 201:
            raise self._api_error(status, data)
        return f"已评论 #{number}：{data.get('html_url', '')}"

    def _build_body(self, state: IssueSession, draft: IssueDraft) -> str:
        body = draft.body

        if self.config.get("include_images", True):
            if draft.images is None:
                urls = state.image_urls
            else:
                urls = [state.image_urls[i - 1] for i in draft.images]
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
        first_new = len(state.image_urls) + 1
        new_images = self._record_images(state, event)
        if not text and not new_images:
            return False
        content = text
        if new_images:
            tags = " ".join(f"#{i}" for i in range(first_new, first_new + new_images))
            content += f"（发送了图片 {tags}）"
        state.transcript.append(("user", self._speaker(event), content.strip()))
        state.participants[str(event.get_sender_id())] = event.get_sender_name()
        return True

    def _record_images(self, state: IssueSession, event: AstrMessageEvent) -> int:
        count = 0
        for comp in event.get_messages():
            url = self._image_url(comp)
            if url and url not in state.image_urls:
                state.image_urls.append(url)
                count += 1
        return count

    @staticmethod
    def _image_url(comp) -> str:
        if not isinstance(comp, Comp.Image):
            return ""
        url = comp.url or comp.file or ""
        return url if url.startswith(("http://", "https://")) else ""

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
            image_count = len(state.image_urls) if d.images is None else len(d.images)
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

    def _check_configured(self) -> str | None:
        if not self._parse_repo(self.config.get("github_repo", "")) or not self.config.get(
            "github_token"
        ):
            return "插件尚未配置 GitHub 仓库或 Token，请联系管理员在插件配置页填写。"
        return None

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
