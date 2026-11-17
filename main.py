"""把 DeepSeek Harness 的对话与后台任务事件转发到 QQ。

DSH 侧（`dsh-notify-qq` Host 插件）在两种时刻 POST 一个 JSON body 到本插件的
Web API：

- `kind: "turn"` —— 任意顶层对话的一轮结束时（`turn/end`），带工作区、会话、
  耗时和回复尾部；
- `kind: "job"`  —— 后台任务结算且没有等待者时（`awaited === false`）。

本插件负责鉴权、去重、格式化，然后用 `context.send_message` 主动把消息发到已经
用 `/dsh bind` 绑定过的会话。

路由（由 AstrBot 的插件扩展 API 暴露）：

    POST /api/v1/plugins/extensions/astrbot_plugin_dsh_notify/dsh

请求头需要带 AstrBot 的 Dashboard API Key（`X-API-Key: <key>`，scope 含
`plugin`）；如果配置了 `extra_token`，还要带 `X-DSH-Token: <token>`。
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time
from datetime import datetime, timedelta, timezone

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_dsh_notify"
WEB_ROUTE = f"{PLUGIN_NAME}/dsh"

KV_TARGETS = "targets"
DEDUPE_MAX_ENTRIES = 2000
MESSAGE_MAX_CHARS = 1800
MAX_BODY_BYTES = 256 * 1024

JOB_STATUS_LABELS = {
    "completed": ("✅", "完成"),
    "failed": ("❌", "失败"),
    "killed": ("⛔", "取消"),
    "running": ("⏳", "运行中"),
    "stopping": ("⏳", "停止中"),
}

TURN_REASON_LABELS = {
    "completed": ("✅", "完成"),
    "error": ("❌", "出错"),
    "max-tokens": ("⚠️", "触顶"),
    "blocked": ("⏸️", "阻塞"),
    "aborted": ("⛔", "取消"),
    "interrupted": ("⚠️", "中断"),
    "forked": ("🔀", "分叉"),
}


def _human_duration(ms: float) -> str:
    seconds = max(0.0, float(ms or 0) / 1000.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _clip(text: str, limit: int) -> str:
    text = str(text or "").replace("\r\n", "\n").strip()
    if limit > 0 and len(text) > limit:
        return text[-limit:]
    return text


class DshNotify(Star):
    """DSH 对话 / 后台任务通知接收端。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._seen: dict[str, float] = {}
        self._stats = {
            "received": 0,
            "delivered": 0,
            "suppressed": 0,
            "turns": 0,
            "jobs": 0,
            "last_at": None,
            "last_label": None,
        }
        self._lock = asyncio.Lock()
        # AstrBot 把插件 Web API 的注册方法放在 Context 上（Star 没有这个方法）。
        # 路由需要以插件名为前缀，Dashboard 会挂到
        # /api/v1/plugins/extensions/<plugin_name>/<route>。
        self.context.register_web_api(
            WEB_ROUTE, self._on_push, ["POST"], "接收 DSH 对话与后台任务通知"
        )
        logger.info(f"[{PLUGIN_NAME}] web api registered at /api/v1/plugins/extensions/{WEB_ROUTE}")

    # ------------------------------------------------------------------ 配置

    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _csv(self, key: str) -> list[str]:
        raw = str(self._cfg(key, "") or "")
        return [item.strip() for item in raw.split(",") if item.strip()]

    # ------------------------------------------------------------ HTTP 入口

    async def _on_push(self):
        """DSH 推送的入口；AstrBot 已在本路由外层校验 Dashboard API Key。"""
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return error_response("body too large", status_code=413)

        expected = str(self._cfg("extra_token", "") or "").strip()
        if expected:
            got = request.headers.get("x-dsh-token", "")
            if not hmac.compare_digest(got, expected):
                return error_response("invalid dsh token", status_code=401)

        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("invalid json body", status_code=400)
        if not self._is_supported(payload):
            return error_response("missing job or turn", status_code=400)

        if not bool(self._cfg("enabled", True)):
            return json_response({"status": "disabled"})

        result = await self._dispatch(payload)
        return json_response({"status": "ok", **result})

    @staticmethod
    def _is_supported(payload: dict) -> bool:
        job = payload.get("job")
        if isinstance(job, dict) and job.get("id"):
            return True
        turn = payload.get("turn")
        return isinstance(turn, dict) and bool(turn.get("sessionId"))

    # -------------------------------------------------------------- 分发逻辑

    async def _dispatch(self, payload: dict) -> dict:
        ref = self._ref(payload)
        async with self._lock:
            self._stats["received"] += 1
            self._stats["last_at"] = time.time()
            self._stats["last_label"] = ref

            reason = self._suppression_reason(payload)
            if reason:
                self._stats["suppressed"] += 1
                logger.info(f"[{PLUGIN_NAME}] suppress {ref}: {reason}")
                return {"delivered": 0, "suppressed": reason}

            targets = await self._load_targets()
            if not targets:
                self._stats["suppressed"] += 1
                logger.warning(f"[{PLUGIN_NAME}] no target bound; drop {ref}")
                return {"delivered": 0, "suppressed": "no-target"}

            text = self._format(payload)
            at_qq = str(self._cfg("at_qq", "") or "").strip()
            delivered, failed = 0, []
            for target in targets:
                umo = target.get("umo", "")
                if not umo:
                    continue
                try:
                    ok = await self._send(umo, text, at_qq)
                except Exception as exc:  # noqa: BLE001 - 单目标失败不影响其他目标
                    logger.error(f"[{PLUGIN_NAME}] send to {umo} failed: {exc}")
                    failed.append(umo)
                    continue
                if ok:
                    delivered += 1
                else:
                    failed.append(umo)

            self._stats["delivered"] += delivered
            if delivered:
                self._remember(payload)
                if "turn" in payload:
                    self._stats["turns"] += 1
                else:
                    self._stats["jobs"] += 1
            return {"delivered": delivered, "failed": failed}

    def _suppression_reason(self, payload: dict) -> str | None:
        job = payload.get("job") if isinstance(payload.get("job"), dict) else None
        turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else None

        if turn is not None and not bool(self._cfg("notify_turns", True)):
            return "turn-disabled"

        if job is not None:
            kinds = self._csv("kinds")
            if kinds and str(job.get("kind", "")) not in kinds:
                return "kind-filtered"
            status = str(job.get("status", ""))
        else:
            status = str(turn.get("reason", "")) if turn else ""

        min_seconds = float(self._cfg("min_duration_seconds", 0) or 0)
        if min_seconds > 0:
            body = job or turn or {}
            try:
                duration = (
                    float(body.get("finishedAt") or 0) - float(body.get("startedAt") or 0)
                ) / 1000.0
            except (TypeError, ValueError):
                duration = 0.0
            if duration < min_seconds:
                return "too-short"

        key = self._dedupe_key(payload)
        if key and key in self._seen:
            return "duplicate"

        if self._in_quiet_hours():
            if status in ("failed", "error") and bool(self._cfg("quiet_keep_failed", True)):
                return None
            return "quiet-hours"
        return None

    # ---------------------------------------------------------------- 去重

    @staticmethod
    def _ref(payload: dict) -> str:
        job = payload.get("job")
        if isinstance(job, dict):
            return str(job.get("label") or job.get("id") or "job")
        turn = payload.get("turn")
        if isinstance(turn, dict):
            workspace = str(turn.get("workspace") or "")
            session = str(turn.get("sessionId") or "")[-8:]
            return f"{workspace}@{session}" if workspace else f"turn@{session}"
        return "unknown"

    @staticmethod
    def _dedupe_key(payload: dict) -> str:
        job = payload.get("job")
        if isinstance(job, dict) and job.get("id"):
            return f"job:{job['id']}:{job.get('finishedAt') or 0}"
        turn = payload.get("turn")
        if isinstance(turn, dict) and turn.get("sessionId"):
            return (
                f"turn:{turn['sessionId']}:{turn.get('turn')}:{turn.get('finishedAt') or 0}"
            )
        return ""

    def _remember(self, payload: dict) -> None:
        key = self._dedupe_key(payload)
        if not key:
            return
        ttl_seconds = float(self._cfg("dedupe_ttl_hours", 24) or 24) * 3600
        now = time.time()
        self._seen[key] = now + ttl_seconds
        if len(self._seen) > DEDUPE_MAX_ENTRIES:
            for stale in [k for k, expire_at in self._seen.items() if expire_at < now]:
                self._seen.pop(stale, None)
            while len(self._seen) > DEDUPE_MAX_ENTRIES:
                self._seen.pop(next(iter(self._seen)), None)

    # ------------------------------------------------------------ 免打扰时段

    def _in_quiet_hours(self) -> bool:
        spec = str(self._cfg("quiet_hours", "") or "").strip()
        if not spec:
            return False
        match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", spec)
        if not match:
            return False
        sh, sm, eh, em = (int(part) for part in match.groups())
        if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
            return False
        offset = float(self._cfg("tz_offset_hours", 8) or 8)
        offset = max(-23.0, min(23.0, offset))
        now = datetime.now(timezone(timedelta(hours=offset)))
        minutes = now.hour * 60 + now.minute
        start, end = sh * 60 + sm, eh * 60 + em
        if start == end:
            return False
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end  # 跨午夜

    # -------------------------------------------------------------- 消息渲染

    def _format(self, payload: dict) -> str:
        if isinstance(payload.get("turn"), dict):
            text = self._format_turn(payload)
        else:
            text = self._format_job(payload)
        if len(text) > MESSAGE_MAX_CHARS:
            text = f"{text[: MESSAGE_MAX_CHARS - 12]}…(已截断)"
        return text

    def _format_turn(self, payload: dict) -> str:
        turn = payload.get("turn") or {}
        reason = str(turn.get("reason") or "")
        icon, label = TURN_REASON_LABELS.get(reason, ("🔔", reason or "结束"))
        try:
            duration = _human_duration(
                float(turn.get("finishedAt") or 0) - float(turn.get("startedAt") or 0)
            )
        except (TypeError, ValueError):
            duration = "?"

        workspace = str(turn.get("workspace") or "").strip()
        session = str(turn.get("sessionId") or "")
        lines = [f"🔔 DSH 对话结束 · {payload.get('host') or 'unknown-host'}"]
        where = f"📁 {workspace}" if workspace else ""
        if session:
            where += f" · 会话 {session[-8:]}"
        if where:
            lines.append(where)
        lines.append(f"{icon} {label} · turn {turn.get('turn')} · {duration}")

        reply = _clip(turn.get("text"), int(float(self._cfg("turn_text_chars", 600) or 0)))
        if reply:
            lines.append("── 回复 ──")
            lines.append(reply)
        return "\n".join(lines)

    def _format_job(self, payload: dict) -> str:
        job = payload.get("job") or {}
        status = str(job.get("status", ""))
        icon, label = JOB_STATUS_LABELS.get(status, ("🔔", status or "结束"))
        detail = str(job.get("detail") or "").strip()
        try:
            duration = _human_duration(
                float(job.get("finishedAt") or 0) - float(job.get("startedAt") or 0)
            )
        except (TypeError, ValueError):
            duration = "?"

        lines = [f"🔔 DSH 后台任务结束 · {payload.get('host') or 'unknown-host'}"]
        lines.append(str(job.get("label") or job.get("id") or "未命名任务").replace("\n", " ")[:200])
        head = f"{icon} {label} · {duration}"
        if detail:
            head += f" · {detail.replace(chr(10), ' ')[:120]}"
        lines.append(head)

        meta = f"类型 {job.get('kind') or '?'}"
        owner = str(job.get("owner") or "")
        if owner:
            meta += f" · 会话 {owner[-8:]}"
        lines.append(meta)

        tail = _clip(job.get("tail"), int(float(self._cfg("max_tail_chars", 800) or 0)))
        if tail:
            lines.append("── 输出尾部 ──")
            lines.append(tail)
        return "\n".join(lines)

    # ---------------------------------------------------------------- 发送

    async def _send(self, umo: str, text: str, at_qq: str = "") -> bool:
        chain = []
        if at_qq:
            chain.append(At(qq=at_qq))
        chain.append(Plain(text=text))
        return bool(await self.context.send_message(umo, MessageChain(chain=chain)))

    # ---------------------------------------------------------------- 目标

    async def _load_targets(self) -> list[dict]:
        data = await self.get_kv_data(KV_TARGETS, [])
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("umo")]

    async def _save_targets(self, targets: list[dict]) -> None:
        await self.put_kv_data(KV_TARGETS, targets)

    # ---------------------------------------------------------------- 指令

    async def _cmd_help(self) -> str:
        return (
            "DSH 通知插件\n"
            "/dsh bind [备注]  把当前会话设为通知目标\n"
            "/dsh unbind       取消当前会话\n"
            "/dsh targets      查看已绑定的目标\n"
            "/dsh test         发一条测试通知\n"
            "/dsh status       运行状态"
        )

    @filter.command("dsh")
    async def dsh(self, event: AstrMessageEvent):
        """DSH 对话 / 后台任务通知的控制指令。"""
        parts = event.message_str.strip().split()
        sub = parts[1].lower() if len(parts) > 1 else "help"
        umo = event.unified_msg_origin

        if sub in ("help", "-h", "--help"):
            yield event.plain_result(await self._cmd_help())
            return

        if sub == "bind":
            label = " ".join(parts[2:]).strip() or umo
            targets = await self._load_targets()
            for target in targets:
                if target.get("umo") == umo:
                    target["label"] = label
                    break
            else:
                targets.append({"umo": umo, "label": label, "added_at": int(time.time())})
            await self._save_targets(targets)
            yield event.plain_result(f"已绑定通知目标：{label}\n共 {len(targets)} 个目标。")
            return

        if sub == "unbind":
            targets = await self._load_targets()
            remaining = [item for item in targets if item.get("umo") != umo]
            await self._save_targets(remaining)
            changed = len(targets) - len(remaining)
            yield event.plain_result(
                "已取消当前会话的通知。" if changed else "当前会话本来就不是通知目标。"
            )
            return

        if sub == "targets":
            targets = await self._load_targets()
            if not targets:
                yield event.plain_result("还没有绑定任何目标，先在要收通知的会话里发 /dsh bind。")
                return
            lines = ["已绑定的通知目标："]
            for index, target in enumerate(targets, 1):
                lines.append(f"{index}. {target.get('label')} 〔{target.get('umo')}〕")
            yield event.plain_result("\n".join(lines))
            return

        if sub == "test":
            targets = await self._load_targets()
            if not targets:
                yield event.plain_result("还没有绑定任何目标，先发 /dsh bind。")
                return
            now = int(time.time() * 1000)
            samples = [
                {
                    "source": "dsh",
                    "kind": "turn",
                    "host": "test-host",
                    "ts": now,
                    "turn": {
                        "sessionId": "session-test-000000000000",
                        "cwd": "/home/user/Projects/demo",
                        "workspace": "demo",
                        "origin": "session",
                        "turn": 1,
                        "reason": "completed",
                        "startedAt": now - 65_000,
                        "finishedAt": now,
                        "text": "这是一条来自 astrbot_plugin_dsh_notify 的测试通知。",
                    },
                },
                {
                    "source": "dsh",
                    "kind": "job",
                    "host": "test-host",
                    "ts": now,
                    "job": {
                        "id": "subagent-0",
                        "kind": "subagent",
                        "label": "后台任务测试通知",
                        "status": "completed",
                        "detail": "exit code: 0",
                        "owner": "session-test-000000000000",
                        "startedAt": now - 133_000,
                        "finishedAt": now,
                        "tail": "hello from astrbot_plugin_dsh_notify",
                    },
                },
            ]
            sent = 0
            for target in targets:
                try:
                    ok = True
                    for sample in samples:
                        ok = await self._send(target.get("umo", ""), self._format(sample)) and ok
                    if ok:
                        sent += 1
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[{PLUGIN_NAME}] test send failed: {exc}")
            yield event.plain_result(f"测试通知已发送到 {sent}/{len(targets)} 个目标。")
            return

        if sub == "status":
            targets = await self._load_targets()
            last = (
                datetime.fromtimestamp(self._stats["last_at"]).strftime("%Y-%m-%d %H:%M:%S")
                if self._stats["last_at"]
                else "无"
            )
            yield event.plain_result(
                "DSH 通知状态\n"
                f"目标数：{len(targets)}\n"
                f"收到：{self._stats['received']} 条（对话 {self._stats['turns']} / 任务 {self._stats['jobs']}），"
                f"已投递：{self._stats['delivered']}，被抑制：{self._stats['suppressed']}\n"
                f"最近一条：{last} · {self._stats['last_label'] or '无'}"
            )
            return

        yield event.plain_result(await self._cmd_help())
