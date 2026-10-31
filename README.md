# TradingX Monitor

个人部署的 TradingX 更新监控工具：归档账号有权访问的正文和原图，监控视频更新、下载完整 MP4，再通过 QQ 官方机器人推送到自己绑定的群。

每个部署者使用自己的会员账号、QQ 机器人和群配置。仓库只包含源码、配置示例和文档，不附带令牌、密钥、群绑定信息、数据库或会员内容；与原站没有官方隶属关系。

## 功能

| 功能 | 行为 |
| --- | --- |
| 帖子 | 发现新帖及编辑，保存历史版本；点赞和阅读数变化不通知 |
| 原图 | 下载账号授权可读的原图，在本机预览并随正文发群 |
| 视频 | 监控课程、随机 K 线复盘及文章配套视频，下载 HLS 并合并音轨 |
| QQ | 正文分段、图片富媒体、视频/MP4 文件，持久化排队和去重 |
| 管理 | 本机面板、健康状态、手动下载旧课程、JSONL/CSV 导出 |
| 后台 | 独立采集、QQ 连接及发送、视频下载进程；提供 systemd 模板 |

默认约每 120 秒检查一次，带 10% 随机偏移。视频下载和上传另需时间，不保证两分钟内到群。首次运行建立历史基线，不批量群发或下载旧视频。

## 环境

- macOS 或 Linux。macOS 已实测；Ubuntu 24.04 提供部署说明和 systemd 模板，需在目标服务器验收。
- 核心 Python 3.9+、SQLite（Python 自带）。完整功能建议 Python 3.12；锁定 QQ 依赖需要 Python 3.11+。
- QQ：自己的官方机器人 AppID、AppSecret、可添加机器人的目标群及主动发言权限。
- 视频：FFmpeg、FFprobe，可从运行用户的 PATH 找到。
- 原生 Windows 暂不支持（使用 POSIX 文件锁和信号）；可使用 Linux 虚拟机。

完整配置、Linux 开机启动、迁移和故障排查见 **[首次部署说明](docs/deployment.md)**，事件和接口见 [代码逻辑](docs/architecture.md)。

## 先启动本机监控

```sh
git clone https://github.com/luyao-wu/tradingx-monitor.git
cd tradingx-monitor
python3 --version
./monitor service start
./monitor service status
```

打开 <http://127.0.0.1:8765/>，点击“接入会员账号”，在本机填写自己的账号密码。只向原站 HTTPS 登录接口提交，程序不保存密码，本机仅保存登录令牌。也可在交互式终端执行：

```sh
./monitor auth login
./monitor doctor
```

确认 `viewer: member` 且存在可读正文。原站浏览器登录不会自动接入监控。令牌到期需重新登录，当前不保存密码自动续期。不配置 QQ 时仍可使用本机归档和预览。

`./monitor run` 是前台采集模式；完整功能使用 `service start` 或 systemd。macOS 后台命令不配置开机启动，休眠或关机会中断采集。

## 接入自己的 QQ 机器人

首次部署执行；已有配置不会被初始化覆盖：

```sh
python3.12 -m venv .qq-venv
.qq-venv/bin/python -m pip install -r requirements-qq-lock.txt
./monitor qq configure
./monitor service stop
./monitor service start
```

系统 Python 为相应版本时可用 `python3` 创建环境。`qq configure` 输入自己的 AppID、数字群号、群名及是否自动发送视频文件，AppSecret 隐藏输入。命令只保存本机配置，不请求 QQ、不发送消息；绑定前发送保持关闭。

面板统一展示“机器人对话框”，不显示具体群名、群号和绑定码。首次绑定时在本机终端运行 `./monitor qq status` 获取 `binding_command`，把机器人添加到自己的目标群，并在该群 **@机器人发送完整指令**。该指令包含目标信息，不要公开。绑定后允许主动发言，再明确发送一次测试：

```sh
./monitor qq doctor
./monitor qq test
./monitor qq status
```

数字群号不是官方 OpenID；不要复制其他部署者的机器人配置、密钥或绑定码。视频可在首次配置时开启，也可编辑本机 `qq_bot.json` 的 `send_video_files: true` 后重启。

## 日常管理

```sh
./monitor status
./monitor images status
./monitor videos status
./monitor videos once
./monitor videos download --id 158
./monitor service stop
```

`--id` 是网站真实课程 ID，不是期数。手动下载会在完成后发到已绑定群，同群同一视频源去重。文件名按 `第106期_课程标题.mp4` 格式生成。超过 30 MB 使用完整 MP4 文件卡片，本机发送上限 195 MB，必要时尝试压缩，仍超限明确失败。

下载失败自动退避。文件明确上传失败保留记录，修复后指定 `./monitor qq retry --delivery 记录ID`。发送结果不确定时暂停，先核对群消息再重试。

## 凭据和运行数据

| 本机路径 | 内容 | 纳入 Git |
| --- | --- | --- |
| `config.json`、`qq_bot.example.json` | 普通配置和无凭据示例 | 是 |
| `qq_bot.json` | 当前部署的机器人及群绑定配置 | 否 |
| `secrets/` | 会员令牌、QQ AppSecret、绑定码 | 否 |
| `data/` | SQLite、原图、MP4、发送队列及游标 | 否 |
| `logs/`、`service/` | 日志和进程状态 | 否 |

凭据文件权限 600、运行目录 700；这是本机文件保存，不是加密保险箱。不要在源码、命令参数、Issue、截图或源码压缩包中提供凭据。迁移和备份通过可信 SSH 或加密备份传输。

面板只监听 `127.0.0.1`，没有公众用户登录或权限隔离；服务器管理使用 SSH 隧道。视频目前长期保留，没有自动清理策略，需要管理磁盘容量。

## 验证与限制

```sh
python3 -m unittest discover -s tests -v
```

测试使用临时数据库、模拟凭据和模拟接口，不需要真实账号，不会发群。维护者已验证会员正文、原图及完整有声 MP4 的真实下载与 QQ 送达；账号和内容不随仓库分发。

- 仅适配当前站点接口，不是 X/Twitter 官方采集工具；接口变动可能需要更新。
- 默认最多扫描 10 页。两轮之间出现又删除的内容可能遗漏，超出范围的旧帖编辑可能未覆盖。
- 只下载账号有权播放的 Cloudflare HLS；YouTube 外链和额外 DRM 暂不支持下载。
- 本项目不授予网站内容的传播权。使用者需确认账号权限、网站约定和内容转发授权。站点 [robots.txt](https://wall-street-skill.com/robots.txt) 将 `/api/` 列为 Disallow，接口可访问不等同于自动抓取获许可。

## 来源与授权状态

站点适配依据其公开前端及正常授权接口。视频下载思路参考 [Deal-Data](https://github.com/luyao-wu/Deal-Data) 的 HLS → FFmpeg 方案，本仓库未捆绑其下载脚本。Python、FFmpeg、QQ SDK 等依赖遵循各自许可证；程序调用系统安装的 FFmpeg。

本仓库暂未指定开源许可证。源码公开可见不等于授予任意修改、再分发或商用授权；授权范围请与维护者确认。[GitHub 关于许可证的说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)
