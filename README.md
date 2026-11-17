# astrbot_plugin_dsh_notify

把 DeepSeek Harness（DSH）里“**跑完了但没人看见**”这件事补上：对话轮次结束、后台任务结算，
都由 DSH 主动 POST 到 AstrBot，AstrBot 再用 QQ 把消息发到你的手机。

本仓库是这套流程的 **AstrBot 接收端**。DSH 侧的推送插件负责产生事件，只要按下面的 HTTP
约定调用本插件即可，不绑定具体实现。

```
┌──────────── DSH 主机 ────────────┐          ┌──────── AstrBot ────────┐
│ turn/end、job settled 事件        │  POST    │ astrbot_plugin_dsh_notify│
│ 失败写本地 outbox，定时重试       │ ───────▶ │ 鉴权 → 去重 → 免打扰     │
└──────────────────────────────────┘  JSON    │ context.send_message(umo)│
                                              └───────────┬─────────────┘
                                                          ▼
                                                        QQ
```

## 两种通知

| `kind` | 触发点 | 内容 |
|---|---|---|
| `turn` | 任意顶层对话的一轮结束 | 工作区、会话、轮次、耗时、结束原因、回复尾部 |
| `job` | 后台任务结算且没人等待 | 任务名、类型、状态、耗时、出口码、输出尾部 |

## 安装

把插件目录放进 AstrBot 的 `data/plugins/`，然后重载插件（或重启容器）：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/dbkx29/astrbot_plugin_dsh_notify.git
```

日志里应出现：

```
[astrbot_plugin_dsh_notify] web api registered at /api/v1/plugins/extensions/astrbot_plugin_dsh_notify/dsh
```

## 建一个 API Key

AstrBot WebUI → **API Keys** → 新建，scope 至少包含 `plugin`。
把生成的 `astrbot_...` 复制出来，DSH 侧要用。

## 绑定接收会话

在**想收通知的那个 QQ 会话**里发：

```
/dsh bind 手机
```

| 指令 | 作用 |
|---|---|
| `/dsh bind [备注]` | 把当前会话设为通知目标（群聊、私聊都行，可绑多个） |
| `/dsh unbind` | 取消当前会话 |
| `/dsh targets` | 查看已绑定的目标 |
| `/dsh test` | 立刻发一条对话通知 + 一条任务通知 |
| `/dsh status` | 收到多少条、投递多少、最近一条 |

## 配置

在 AstrBot WebUI 的插件配置页修改：

| 配置 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `extra_token` | 空 | 填了就必须带 `X-DSH-Token`，与推送端 `token` 一致 |
| `notify_turns` | `true` | 是否转发对话轮次 |
| `turn_text_chars` | `600` | 对话通知里附带回复的最后 N 个字符，0=不附带 |
| `kinds` | `subagent,bash` | 只转发这些**任务**类型，空=全部 |
| `min_duration_seconds` | `0` | 忽略太短的轮次/任务（兜底用） |
| `max_tail_chars` | `800` | 任务通知里附带多少输出尾部，0=不附带 |
| `quiet_hours` | 空 | 如 `23:30-08:00`，支持跨午夜 |
| `quiet_keep_failed` | `true` | 免打扰时段仍然推失败任务 / 出错轮次 |
| `tz_offset_hours` | `8` | 容器通常是 UTC，免打扰按这个偏移计算 |
| `at_qq` | 空 | 群消息里额外 @ 谁 |
| `dedupe_ttl_hours` | `24` | 去重窗口 |

## 推送接口

```
POST /api/v1/plugins/extensions/astrbot_plugin_dsh_notify/dsh
X-API-Key: astrbot_xxxxxxxxxxxxxxxxxxxx        # Dashboard API Key，scope 含 plugin
X-DSH-Token: <可选，与 extra_token 一致>
Content-Type: application/json
```

成功时返回：

```json
{"status": "ok", "delivered": 1, "failed": []}
```

`delivered: 0` 时看 `suppressed`：`no-target`（没 bind）、`kind-filtered`、`too-short`、
`duplicate`、`quiet-hours`、`turn-disabled`。

### 对话通知（`turn`）

```json
{
  "source": "dsh",
  "kind": "turn",
  "host": "workstation-a",
  "ts": 1794893391000,
  "turn": {
    "sessionId": "session-0000000000000001",
    "cwd": "/home/user/Projects/demo",
    "workspace": "demo",
    "origin": "session",
    "turn": 3,
    "reason": "completed",
    "startedAt": 1794893321764,
    "finishedAt": 1794893386764,
    "text": "改完了，测试全绿。"
  }
}
```

`reason` 常见值：`completed`、`error`、`max-tokens`、`blocked`、`aborted`、`interrupted`、`forked`。

### 任务通知（`job`）

```json
{
  "source": "dsh",
  "kind": "job",
  "host": "workstation-a",
  "ts": 1794893391000,
  "job": {
    "id": "subagent-0",
    "kind": "subagent",
    "label": "跑一遍测试",
    "status": "completed",
    "detail": "exit code: 0",
    "owner": "session-0000000000000001",
    "startedAt": 1794893258764,
    "finishedAt": 1794893390764,
    "tail": "…"
  }
}
```

`status` 常见值：`completed`、`failed`、`killed`、`running`、`stopping`。

## 对接 DSH

DSH 侧的 Host 插件负责订阅 `turn/end` 与 job 结算事件，并按上面的格式推送。它需要配置：

| 字段 | 说明 |
|---|---|
| `endpoint` | 本插件的完整路由地址 |
| `apiKey` | Dashboard API Key |
| `token` | 与 `extra_token` 一致（可空） |
| `notifyTurns` / `notifyJobs` | 是否推送对话 / 任务 |
| `turnReasons` | 只推这些结束原因 |
| `turnTextChars` | 回复摘录长度 |
| `kinds` / `statuses` | 任务类型 / 状态白名单 |
| `tailChars` | 输出尾部长度 |

DSH 侧插件独立部署，不在本仓库内；只要请求体和上面的 JSON 一致，任何推送端都能对接。

## 设计要点

- **去重两层**：接收端按 `job.id+finishedAt` / `sessionId+turn+finishedAt` 去重（默认 24h），
  重试、AstrBot 重启都不会重复刷屏。
- **鉴权两层**：外层是 AstrBot 自己的 Dashboard API Key（scope 含 `plugin`），内层可选
  `X-DSH-Token` 共享密钥。没有自制的“无鉴权 webhook”。
- **安静地失败**：平台发送失败只记日志并计入 `failed`，不影响其它目标。

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `401 Missing API key` / `Invalid API key` | 请求头用了 `Bearer` 或没带 `X-API-Key`；这个路由只认 API Key 或 Dashboard JWT |
| `403 Insufficient API key scope` | key 的 scope 不含 `plugin`，重建一个 |
| `404` / `未找到该路由` | 插件没加载成功，或推送地址里的插件目录名/路由名不一致 |
| `401 invalid dsh token` | 两侧 `token` 不一致；不想用就把 `extra_token` 留空 |
| 返回 ok 但没消息 | 没 `bind`、命中 `kinds`/`min_duration_seconds`/免打扰，或平台发送失败（看 AstrBot 日志） |
| 消息重复 | 检查 `dedupe_ttl_hours`；AstrBot 重启后内存去重会清空 |
| 免打扰时间不对 | docker 里通常是 UTC，用 `tz_offset_hours: 8` 校正，别改容器时区 |
| QQ 官方接口报主动消息受限 | 官方机器人对主动消息有配额；NapCat / aiocqhttp 没有这个限制 |

## 许可

AGPL-3.0，见 [LICENSE](LICENSE)。
