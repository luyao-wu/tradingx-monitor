# 代码逻辑与本地接口

## 流程与进程

```text
正常授权网站接口 → 帖子/视频快照 → SQLite 归档及版本事件
                              ├→ 原图下载 → 本地图片
                              ├→ QQ 通知队列 → 官方消息确认
                              └→ 视频任务 → HLS + 音轨 → 完整 MP4 → QQ 分片上传 → 消息确认
```

监督进程管理采集及网页、QQ 连接及发送、独立视频下载进程。下载不阻塞采集轮询，QQ 按队列顺序发送；未配置 QQ 时仍可单独运行本机归档。

## 采集与归档

`client.py` 读取网站自己的 `/api/tradingx-posts`，不调用 X/Twitter API。会员令牌只发送给固定原站 HTTPS 来源，禁止携带凭据跟随重定向。未解锁时保存实际元信息，模糊占位文字不视作正文。

`store.py` 按帖子 ID 保存当前及历史版本；新 ID 产生 `new_post`，正文、编辑时间、文字长度或配图 ID 变化记录版本，统计只更新计数。多页全部成功才提交，目录缺席不直接当删除。默认最多 10 页，覆盖不足会标记；两轮之间出现又删除的内容可能遗漏。

`images.py` 使用授权图片接口下载原图，记录 SHA256、大小及状态，失败退避重试；每轮最多 20 张、单张上限 20 MiB。目录 700、文件 600，相同图片复用。

`auth.py` 提供一次性密码登录或令牌导入，密码不落盘。权限失效时历史正文仍保留并标为旧缓存，不冒充当前版本；目前无密码自动续期。

## 视频

`videos.py` 检查已发布且可播放的目录，使用真实课程 ID，期数只展示。初次建基线，新视频产生 `video_new`，标题、时长、简介或视频标识变化产生 `video_updated`；统计和排序变化不误报。与帖子分别安排检查和退避。

`video_downloads.py` 消费目录和明确手动下载事件，首次开启跳过历史。正常会员播放接口取得临时播放信息，签名仅写入任务私有临时清单，结束删除，不进进程参数、日志或状态响应。播放/分片 URL 限制 HTTPS 来源，网站会员令牌不发给 QQ。

选择估算适合发送的最高档画质（最多 1080P），FFmpeg 合并音轨、FFprobe 验证时长和音轨，再保存 MP4、SHA256 和完成事件。超过 195 MB 尝试压缩，仍超限或不完整则失败，不发送截断文件。下载失败按 2 分钟至 1 小时退避，同课程同视频源复用缓存；重启恢复未完成任务。

`video_download_jobs` 保存任务，`video_files` 保存元数据，字节在 `data/videos/`。物理文件用归档键，展示和发送名按期数与标题生成。

## QQ

`qq_push.py` 使用 `https://api.bot.qq.com`。首次配置隐藏输入 AppSecret，本机文件 600，绑定前 `enabled: false`。监听器只处理一次性绑定指令，不归档其他群聊天。验证后保存平台 OpenID 并启用，数字群号不能直接作为 OpenID。

`qq_outbox` 持久化排队，事件分段去重，同群同图片/视频源去重。正文编辑不重复发送未变更媒体，新增配图可补发。先在本机保存媒体，再上传 QQ，不发送会员资源签名。

大文件使用官方分片接口，COS 上传不携带 QQ 或会员令牌，兼容 0/1 起始分片编号；超过 30 MB 的 MP4 使用文件卡片。收到消息 ID 才记为已发，群文件到期不删除本机缓存。

正文和图片明确失败暂停队列；视频文件明确上传失败单独保留，不阻止后续文字。发送超时、进程中断、缺少消息 ID 等标为 `uncertain` 并暂停，必须先核对群消息再决定重试。下载退避和发送结果确认是不同阶段。

## 本机接口

只监听 `127.0.0.1`，校验 Host/端口及登录 Origin/CSRF，没有公共账号系统。服务器用相同端口的 SSH 隧道访问。

| 接口 | 用途 |
| --- | --- |
| `/api/status` | 采集、权限、覆盖范围和计划 |
| `/api/posts?limit=50` | 当前归档及明确标记的旧缓存 |
| `/api/images/status`、`/api/images/归档键` | 原图状态和本地原图 |
| `/api/videos/status`、`/api/videos?limit=10` | 目录、下载进度及最新文件 |
| `/api/video-files/归档键` | MP4，支持 Range 和可读保存名 |
| `/api/qq/status` | 当前 QQ 连接、绑定和队列 |
| `/api/events?after=0&limit=100` | 从指定游标之后读取事件 |
| `/api/revisions?id=3` | 指定帖子的观测版本 |
| `/healthz` | 采集新鲜且最近无错时 200，否则 503 |
| `GET /api/auth/session` | 登录表单防伪令牌 |
| `POST /api/auth/login` | 原站登录、保存令牌和验证，不返回令牌 |

事件消费者按最后 ID 继续翻页直到空列表。`healthy` 不单独证明会员正文、QQ 送达或进程存活；分别查看 doctor、qq status、service status。

JSONL/CSV 使用 export 导出，不覆盖已有文件，CSV 转义潜在公式前缀。SQLite 使用 WAL，停机复制完整数据目录或用 backup API 备份。日志轮转、轮次保留最近 1,000 次；正文版本、事件及媒体目前长期保留。

接口来源：[时间线前端](https://wall-street-skill.com/src/lib/tradingx.js)、[授权请求](https://wall-street-skill.com/src/lib/api.js)、[网站前端](https://wall-street-skill.com/src/main.js)、[QQ 富媒体](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_files.post.html)、[QQ 分片准备](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_id_upload_prepare.post.html)。站内接口无公开稳定性承诺，升级后需验证。
