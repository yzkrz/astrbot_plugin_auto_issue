import json
import re
import time
from dataclasses import dataclass, field

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

CONFIRM_WORDS = {"确认", "确认发布", "发布", "提交", "确定", "ok", "yes", "y"}
CANCEL_WORDS = {"取消", "退出", "结束", "算了", "cancel", "quit", "exit"}
FORCE_DRAFT_WORDS = {"生成", "直接生成", "生成草稿", "够了", "就这些", "没有了"}

OUTPUT_FORMAT_PROMPT = """
# 输出格式（必须严格遵守）
只输出一个 JSON 对象，不要输出任何其他文字，不要使用 Markdown 代码块包裹。可选的三种形式：
1. 信息不足，需要继续追问：
{"action": "ask", "message": "发给群友的追问内容"}
2. 信息已经充足，生成 issue 草稿：
{"action": "draft", "title": "issue 标题", "body": "issue 正文（Markdown）"__LABELS_FIELD__}
3. 最新的消息是与当前 issue 无关的群聊闲聊，不需要回应：
{"action": "ignore"}
"""


@dataclass
class IssueDraft:
    title: str
    body: str
    labels: list[str] = field(default_factory=list)


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
    draft: IssueDraft | None = None
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
    "1.0.0",
)
class AutoIssuePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ------------------------------------------------------------------ 指令

    @filter.command("issue", alias={"反馈"})
    async def issue(self, event: AstrMessageEvent):
        """发起一次 issue 收集：/issue 需求或问题描述"""
        deny_reason = self._check_permission(event)
        if deny_reason:
            yield event.plain_result(deny_reason)
            return

        repo = self._parse_repo(self.config.get("github_repo", ""))
        if not repo or not self.config.get("github_token"):
            yield event.plain_result(
                "插件尚未配置 GitHub 仓库或 Token，请联系管理员在插件配置页填写。"
            )
            return

        if IssueSessionFilter.key_of(event) in USER_SESSIONS:
            yield event.plain_result("当前会话已有进行中的 issue 收集，请先完成或发送「取消」。")
            return

        state = IssueSession(
            initiator_id=str(event.get_sender_id()),
            initiator_name=event.get_sender_name(),
            group_id=str(event.get_group_id() or ""),
            platform=event.get_platform_name(),
        )

        content = re.sub(r"^\s*(issue|反馈)\s*", "", event.message_str, count=1).strip()
        has_input = self._record_message(state, event, content)

        if has_input:
            yield event.plain_result("收到，正在整理…")
            await self._advance(event, state)
        else:
            yield event.plain_result(
                "请描述你遇到的问题或想要的功能，我会继续追问一些细节后整理成 issue。\n"
                "随时发送「取消」可退出。"
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
                tip = "（草稿未发布）" if state.draft else ""
                yield event.plain_result(f"issue 收集会话已超时结束{tip}。")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] session error: {e}", exc_info=True)
            yield event.plain_result(f"issue 收集会话异常结束：{e}")
        finally:
            event.stop_event()

    # ------------------------------------------------------------------ 会话

    async def _on_session_message(
        self,
        controller: SessionController,
        event: AstrMessageEvent,
        state: IssueSession,
        timeout: int,
    ):
        text = re.sub(r"^\s*(issue|反馈)\s*", "", event.message_str.strip(), count=1).strip()
        lowered = text.lower()

        if lowered in CANCEL_WORDS:
            if not self._can_operate(event, state):
                await event.send(event.plain_result("只有发起人或管理员可以取消本次 issue 收集。"))
                return
            state.closed = True
            await event.send(event.plain_result("已取消本次 issue 收集。"))
            controller.stop()
            return

        if state.draft and lowered in CONFIRM_WORDS:
            if not self._can_operate(event, state):
                await event.send(event.plain_result("只有发起人或管理员可以确认发布。"))
                return
            # 发布期间不让会话超时
            controller.keep(timeout=timeout + 60, reset_timeout=True)
            try:
                url = await self._create_issue(state)
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] create issue failed: {e}")
                await event.send(
                    event.plain_result(f"发布失败：{e}\n可以稍后再次回复「确认」重试，或「取消」放弃。")
                )
                controller.keep(timeout=timeout, reset_timeout=True)
                return
            state.closed = True
            await event.send(event.plain_result(f"Issue 已发布：{url}\n感谢反馈！"))
            controller.stop()
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

        if action == "draft" and result.get("title") and result.get("body"):
            state.draft = IssueDraft(
                title=str(result["title"]).strip(),
                body=str(result["body"]).strip(),
                labels=self._filter_labels(result.get("labels")),
            )
            state.transcript.append(("assistant", "助手", "（已生成 issue 草稿，等待确认）"))
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

        auto_label = bool(self.config.get("auto_label", True))
        labels = [str(x) for x in self.config.get("available_labels", []) if str(x).strip()]
        labels_field = ', "labels": ["标签1"]' if auto_label and labels else ""
        system_prompt = (
            self.config.get("system_prompt", "").strip()
            + "\n"
            + OUTPUT_FORMAT_PROMPT.replace("__LABELS_FIELD__", labels_field)
        )
        if auto_label and labels:
            system_prompt += f"\nlabels 只能从以下列表中选择 0~3 个：{json.dumps(labels, ensure_ascii=False)}\n"

        parts = []
        project_context = self.config.get("project_context", "").strip()
        if project_context:
            parts.append(f"# 项目背景\n{project_context}")
        parts.append(f"# 目标仓库\n{self._parse_repo(self.config.get('github_repo', ''))}")
        parts.append("# 群聊记录（按时间顺序）\n" + self._format_transcript(state))
        if state.image_urls:
            parts.append(f"# 附件\n群友共发送了 {len(state.image_urls)} 张图片，发布时会自动附在 issue 末尾，正文中无需引用。")
        if state.draft:
            parts.append(
                "# 当前草稿\n"
                + json.dumps(
                    {"title": state.draft.title, "body": state.draft.body, "labels": state.draft.labels},
                    ensure_ascii=False,
                )
                + "\n草稿已经发给群友预览。如果最新消息是对草稿的修改意见或补充信息，请输出修改后的完整草稿（action=draft）。"
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

    # ------------------------------------------------------------------ GitHub

    async def _create_issue(self, state: IssueSession) -> str:
        assert state.draft is not None
        repo = self._parse_repo(self.config.get("github_repo", ""))
        api_base = (self.config.get("github_api_base") or "https://api.github.com").rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.config.get('github_token', '')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": PLUGIN_NAME,
        }
        payload: dict = {"title": state.draft.title, "body": self._build_body(state)}
        if state.draft.labels:
            payload["labels"] = state.draft.labels

        url = f"{api_base}/repos/{repo}/issues"
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 422 and "labels" in payload:
                    # 标签无效时去掉标签重试
                    logger.warning(f"[{PLUGIN_NAME}] 带标签创建失败，去掉标签重试：{data}")
                    payload.pop("labels")
                    async with session.post(url, json=payload, headers=headers) as resp2:
                        data = await resp2.json(content_type=None)
                        status = resp2.status
                else:
                    status = resp.status

        if status != 201:
            message = data.get("message") if isinstance(data, dict) else data
            raise RuntimeError(f"GitHub API 返回 {status}：{message}")
        return data.get("html_url", "")

    def _build_body(self, state: IssueSession) -> str:
        assert state.draft is not None
        body = state.draft.body

        if self.config.get("include_images", True) and state.image_urls:
            images = "\n".join(
                f"![image{i}]({u})" for i, u in enumerate(state.image_urls, 1)
            )
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

    def _record_message(self, state: IssueSession, event: AstrMessageEvent, text: str) -> bool:
        """记录一条群友消息，返回是否包含有效内容。"""
        new_images = self._record_images(state, event)
        if not text and not new_images:
            return False
        content = text
        if new_images:
            content += f"（发送了 {new_images} 张图片）"
        state.transcript.append(("user", self._speaker(event), content.strip()))
        state.participants[str(event.get_sender_id())] = event.get_sender_name()
        return True

    def _record_images(self, state: IssueSession, event: AstrMessageEvent) -> int:
        count = 0
        for comp in event.get_messages():
            if isinstance(comp, Comp.Image):
                url = comp.url or comp.file or ""
                if url.startswith(("http://", "https://")) and url not in state.image_urls:
                    state.image_urls.append(url)
                    count += 1
        return count

    @staticmethod
    def _speaker(event: AstrMessageEvent) -> str:
        return f"{event.get_sender_name()}({event.get_sender_id()})"

    @staticmethod
    def _format_transcript(state: IssueSession) -> str:
        lines = []
        for role, speaker, content in state.transcript:
            prefix = "助手" if role == "assistant" else f"群友 {speaker}"
            lines.append(f"[{prefix}]: {content}")
        return "\n".join(lines)

    def _render_preview(self, state: IssueSession) -> str:
        assert state.draft is not None
        d = state.draft
        lines = ["📝 Issue 草稿预览", "", f"标题：{d.title}"]
        if d.labels:
            lines.append(f"标签：{', '.join(d.labels)}")
        if state.image_urls and self.config.get("include_images", True):
            lines.append(f"附件图片：{len(state.image_urls)} 张")
        lines += ["", d.body, "", "———", "回复「确认」发布，「取消」放弃，或直接回复修改意见。"]
        return "\n".join(lines)

    def _filter_labels(self, labels) -> list[str]:
        if not self.config.get("auto_label", True) or not isinstance(labels, list):
            return []
        allowed = {str(x).strip() for x in self.config.get("available_labels", [])}
        return [str(x) for x in labels if str(x).strip() in allowed][:3]

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
                return "请 @我 并发送 /issue 你的需求或问题。"

        users = {str(u).strip() for u in self.config.get("user_whitelist", []) if str(u).strip()}
        if users and not event.is_admin() and str(event.get_sender_id()) not in users:
            return "你没有发起 issue 反馈的权限。"
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
