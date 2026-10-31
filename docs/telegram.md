# Telegram 可选推送

此功能为预置适配，默认关闭。已通过模拟接口和临时数据库测试，尚未使用真实 Telegram 账号或机器人验证送达；启用者需要在自己的部署中验证网络、目标、权限和文件上传。仓库不提供 Token 或接收目标。

## 支持范围

| 内容 | 当前行为 |
| --- | --- |
| 帖子新发、编辑 | 与 QQ 共用授权归档，纯文本拆分发送，不启用 Markdown/HTML 解析 |
| 原图 | 本机校验后通过 sendDocument 发送原始文件，保留原图字节，表现为文件卡片 |
| 视频新发、资料更新 | 发送标题、期数、时长和源站观看链接 |
| 完整 MP4 | 可选，50 MB 以内用 sendVideo 上传；超过时仅发链接及超限说明 |
| 目标 | 单个 Chat ID 或 @频道用户名，可选 message_thread_id 话题 |
| 队列 | 独立事件游标、持久化、媒体去重、限流退避和不确定发送暂停 |

Telegram 与 QQ 可单独或同时启用，发送队列互不阻塞。TG 不读取 QQ 群绑定，也不会获取其他群聊内容。只使用出站 Bot API，不自动调用 getUpdates 或设置 Webhook，不需要公开入站端口。

官方云端 API 的文字上限为 4096 字符，文件/视频上传上限为 50 MB。本模块保守按 UTF-16 长度拆分文字，图片使用文件上传而非 sendPhoto。视频超过限制不会截断、不会自动分卷或二次压缩，也不会把未上传的文件计为已发；已归档的完整 MP4 继续保留。

下载器与 QQ 共用，现有归档仍按最多 195 MB 的本机发送策略选择画质/必要时压缩；Telegram 不会改变 QQ 已存文件。只启用 Telegram 的视频功能时也能启动下载器，不要求配置 QQ。暂不支持自建 Local Bot API、自定义 API 地址、自动查找 Chat ID 或多接收目标。

## 1. 准备自己的机器人与目标

通过 Telegram 官方 [BotFather](https://core.telegram.org/bots/features#botfather) 创建机器人，取得自己的 Bot Token。

- 私聊：接收者需先与机器人开始会话，准备自己的 Chat ID。
- 群/超级群：将机器人加入自己管理的目标，并允许发送消息和媒体。
- 频道：将机器人设为能发布消息的管理员，可用 @频道用户名或 Chat ID。
- 话题：提供该目标支持的 message_thread_id；不使用话题则留空。私信话题的其他参数未在本版本适配。

Chat ID 由使用者自己的授权管理工具取得；此模块不会为了找 ID 自动读取聊天更新。不要把带 Token 的 API URL、Chat ID、聊天截图或机器人密钥提交到 Issue。

## 2. 本机配置，默认不发送

不需要安装额外的 Telegram SDK，使用核心 Python 标准库。在交互式终端执行：

```sh
./monitor telegram configure
./monitor telegram status
```

配置命令依次询问 Chat ID / @频道用户名、可选话题 ID、是否发送视频文件，以及隐藏输入的 Bot Token。只在本机保存，不请求 Telegram，不发送测试消息；已有配置或 Token 时拒绝覆盖。

| 文件 | 用途 |
| --- | --- |
| `telegram_bot.example.json` | 无凭据示例，enabled=false |
| `telegram_bot.json` | 本机目标及开关，权限 600，被 Git 忽略 |
| `secrets/telegram_bot_token` | 本机 Bot Token，权限 600，被 Git 忽略 |

初始化后 `enabled` 保持 `false`，即使重启后台也不会启动 Telegram 推送。确认目标及权限后，再在本机 `telegram_bot.json` 改为 `enabled: true`。不要把 Token 写进 JSON、环境命令参数或源码。终端 `telegram status` 可显示自己的目标，网页接口只显示“Telegram 机器人对话框”。

## 3. 自行启用与验证

以下命令由部署者确认配置后自行执行；本次功能预置没有执行真实请求。

```sh
./monitor telegram doctor
./monitor service stop
./monitor service start
./monitor service status
./monitor telegram test
./monitor telegram status
```

`doctor` 只调用 getMe 验证 Token，不发送消息，也不证明目标 ID、目标权限或文件上传可用。`test` 要求 enabled=true，明确发送一条标为测试的文字；测试文字成功也不代表媒体上传已验证。以自己的机器人实际接收情况为准。

监督进程仅在开关开启且本机 Token 文件存在时启动独立 Telegram 子进程；可从 `telegram_worker_running` 查看。首次启用以当前事件建立基线，不推送旧帖子或此前已下载的视频。之后的新内容、更新及明确手动请求的视频才进入新通道。

手动 `telegram run` 只启动出站队列 worker，不代替采集和视频下载；完整功能用 `service start` 或项目 systemd 模板。Telegram 本身不依赖 `.qq-venv`；现有 systemd 模板使用该路径的 Python，单用 TG 时也可只创建该虚拟环境而不安装 QQ SDK，或修改模板的 Python 路径。

## 4. 视频、限流和失败

`send_images` 控制原图文件，`send_video_files` 控制完整视频上传，两者与目录文字通知分别设置。下载可用自己的会员登录态，Telegram Token 不会发送给原站，会员令牌不会发送给 Telegram。

云端视频超过 50 MB 时，队列改发源站观看链接和原因，状态中单独统计 `sent_video_links`，不增加 `sent_video_files`。源站链接不含会员令牌；接收者需要自行具备访问权限。本机文件不会为绕过平台限制而截短，也不公开本机下载地址。

worker 默认每 5 秒处理一条，429 时按官方 retry_after 等待。网络超时、服务端 5xx 或缺少消息 ID 等结果不确定时，标记 uncertain 并暂停 Telegram 通道，避免重复；QQ 通道保持独立。明确拒绝会标为 failed，也需先修复原因。

先检查实际 Telegram 消息，确认后才能指定记录重试：

```sh
./monitor telegram retry --delivery 记录ID
```

发送中被强制中断的任务会在 worker 重启时标为 uncertain。更改已启用过的 Chat ID 或话题时会提示 destination_changed 并暂停，旧队列不会被自动转投新目标；恢复原配置，或在独立部署目录为新目标重新建立通道。当前没有目标迁移向导。

停用时将 `enabled` 改回 `false`，监督进程会停止该通道，也可重启后台。队列仍保留，不会删除历史或影响 QQ。备份/迁移时将本机配置和 Token 经可信 SSH 单独传输，数据库中的 telegram_outbox、游标及去重状态一并保留。

## 5. 离线测试与后续扩展

```sh
python3 -m unittest discover -s tests -v
```

测试用模拟 Token、模拟 HTTPS 返回和临时文件验证 JSON、流式 multipart、限流、错误脱敏、去重、超限回退、QQ 隔离及 worker 启动策略，不访问真实 Telegram 服务。真实联调需部署者另行完成。

后续适配入口为 `tradingx_monitor/telegram_push.py` 中的 TelegramAPI（传输）、TelegramQueue（事件消费）、视频超限策略和本机配置。更换 API 服务或扩大文件限制时，还应同步验证 Token 的发送来源、上传实现、重试语义和文档，不能仅增大常量就声称平台支持。

官方依据：[Bot API](https://core.telegram.org/bots/api)、[文件上传限制](https://core.telegram.org/bots/api#sending-files)、[发送限制 FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this)。平台限制可能变化，以官方及实际响应为准。
