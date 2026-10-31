# 首次部署与维护

每个人使用自己的会员账号、QQ 机器人和群。本文不提供共享会员、公共机器人或公共内容服务。

## 准备环境

完整功能建议 Python 3.12；核心 Python 3.9+ 可运行，锁定 QQ 依赖要求 Python 3.11+。需要 Git、包含 FFprobe 的 FFmpeg 和时区数据。

Ubuntu 24.04 示例：

```sh
sudo apt update
sudo apt install -y git python3 python3-venv ffmpeg tzdata
```

macOS 使用 Homebrew 时可安装：

```sh
brew install python@3.12 ffmpeg
```

检查 `python3 --version`、`ffmpeg -version`、`ffprobe -version`。多个 Python 共存时明确使用 `python3.12` 创建 QQ 环境。虚拟环境需在目标机器重建，不能把 Mac 的 `.qq-venv` 直接复制到 Linux。

## 下载和接入会员

```sh
git clone https://github.com/luyao-wu/tradingx-monitor.git
cd tradingx-monitor
./monitor service start
./monitor service status
```

打开 <http://127.0.0.1:8765/>，点击“接入会员账号”，或在交互式终端运行：

```sh
./monitor auth login
./monitor doctor
```

确认 `viewer: member`、`readable_posts > 0`。邮箱和密码隐藏输入，密码不落盘；只保存令牌。原站要求额外验证时，在原站自行完成后用 `./monitor auth set` 隐藏导入已有 `ws_token`。令牌到期需重新登录，当前没有密码自动续期。

没有会员时只归档实际公开的元信息，不把模糊占位文字当正文。首次采集建立基线；不要同时运行 `run` 和 `service start`。修改 `config.json` 端口或轮询参数后重启。`TRADINGX_TOKEN` 优先于文件，推荐使用本机令牌文件，避免命令历史里保存凭据。

## 配置自己的 QQ 机器人

1. 在 [QQ 官方机器人后台](https://q.qq.com/)用自己的账号创建机器人，取得 AppID、AppSecret；按官方要求配置使用范围及目标群。
2. 安装 QQ 环境并初始化本机配置：

```sh
python3.12 -m venv .qq-venv
.qq-venv/bin/python -m pip install -r requirements-qq-lock.txt
./monitor qq configure
```

Ubuntu 24.04 可用相应版本的 `python3` 创建环境。初始化询问 AppID、数字群号、群名、是否自动下载视频，AppSecret 隐藏输入。全部校验通过才保存，文件权限 600；已有配置或密钥时拒绝覆盖。此命令不请求 QQ 接口、不发消息，并保持 `enabled: false`。

示例文件只用于了解字段，不要覆盖正在使用的配置。已有部署可备份后编辑本机 `qq_bot.json`；更换 AppSecret 后重启。不要手填数字群号作为 `group_openid`。

```sh
./monitor service stop
./monitor service start
./monitor qq doctor
```

3. 将机器人加入自己的目标群，在本机面板找到“绑定推送 数字群号 一次性校验码”，在目标群 **@机器人发送完整指令**。不要公开绑定码。
4. 确认绑定成功，在手机 QQ 的群机器人管理中允许主动发言，再明确发送测试：

```sh
./monitor qq test
./monitor qq status
```

绑定回执不能证明主动推送可用，以主动测试及群内收到为准。监听器只处理绑定指令，不归档其他群聊天。服务器不需要安装 QQ 客户端，也不需要开放 QQ 回调端口。

## 视频自动下载

初始化时选择开启，或编辑本机 `qq_bot.json` 的 `send_video_files: true` 后重启。

```sh
./monitor videos once
./monitor videos status
./monitor service status
```

确认目录检查健康、下载开关开启、`video_worker_running`。首次只建基线；下载旧课程需明确指定真实 ID（不是期数）：

```sh
./monitor videos download --id 158
```

任务完成后会发到绑定群，同群同一视频源不重复发。下载器保留音轨并校验完整时长。超过 30 MB 使用 MP4 文件卡片，超过本机 195 MB 上限尝试压缩，仍超限明确失败。群文件可能到期，本机文件会保留。

## Linux 开机启动

下例是 Ubuntu 24.04 的新安装示例，路径 `/opt/tradingx-monitor`、运行用户 `tradingx`。模板需在目标服务器验收；如账号或目录已有部署，先检查原配置，不重复创建。

先安装上述系统依赖，然后：

```sh
sudo useradd --system --user-group --home-dir /opt/tradingx-monitor --shell /usr/sbin/nologin tradingx
sudo git clone https://github.com/luyao-wu/tradingx-monitor.git /opt/tradingx-monitor
sudo chown -R tradingx:tradingx /opt/tradingx-monitor
sudo -u tradingx python3 -m venv /opt/tradingx-monitor/.qq-venv
sudo -u tradingx /opt/tradingx-monitor/.qq-venv/bin/python -m pip install -r /opt/tradingx-monitor/requirements-qq-lock.txt
```

在服务器交互式终端，以同一运行用户配置账号及机器人。已有部署迁移时，改用下文的运行资料迁移，不重新初始化：

```sh
sudo -u tradingx /opt/tradingx-monitor/.qq-venv/bin/python /opt/tradingx-monitor/start.py auth login
sudo -u tradingx /opt/tradingx-monitor/.qq-venv/bin/python /opt/tradingx-monitor/start.py qq configure
sudo -u tradingx /opt/tradingx-monitor/.qq-venv/bin/python /opt/tradingx-monitor/start.py doctor
sudo install -m 644 /opt/tradingx-monitor/deploy/tradingx-monitor.service /etc/systemd/system/tradingx-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now tradingx-monitor
sudo systemctl status tradingx-monitor --no-pager
```

模板直接执行前台 `supervise`，管理采集、QQ、视频子进程。不要把自行脱离终端的 `service start` 再包进 systemd，也不要同时启动两份后台。位置或用户不同时，调整模板的路径及 User/Group。绑定群仍按上文步骤进行。

```sh
sudo systemctl restart tradingx-monitor
sudo systemctl stop tradingx-monitor
sudo journalctl -u tradingx-monitor -n 100 --no-pager
```

详细运行日志在项目 `logs/`，会轮转。开机恢复不等于会员令牌自动续期，令牌仍可能需要手动更新。

## 远程访问面板

服务器面板只监听 `127.0.0.1:8765`。在自己的电脑执行，将 `user@server` 替换为实际登录信息：

```sh
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 user@server
```

打开 <http://127.0.0.1:8765/>。本机 8765 必须空闲；迁移后旧服务应已停止。程序严格校验 Host/端口，隧道两端要保持相同面板端口。如需同时保留本机面板，可将服务器端口改为 18765，重启后同时转发并访问 18765。

面板没有公众用户登录或权限隔离，不作为公开内容网站部署。

## 更新、迁移和备份

更新前确认没有发送中的文件，停止后台。systemd 部署示例：

```sh
sudo systemctl stop tradingx-monitor
sudo -u tradingx git -C /opt/tradingx-monitor pull --ff-only
sudo -u tradingx /opt/tradingx-monitor/.qq-venv/bin/python -m pip install -r /opt/tradingx-monitor/requirements-qq-lock.txt
sudo -u tradingx /opt/tradingx-monitor/.qq-venv/bin/python -m unittest discover -s /opt/tradingx-monitor/tests -v
sudo systemctl start tradingx-monitor
```

macOS 项目目录以 `./monitor service stop/start` 对应操作。更新保留 Git 忽略的运行资料。

迁移先准备目标环境；正式切换先停旧实例，再经可信 SSH 迁移整个 `data/`、`secrets/`、`qq_bot.json` 和所需普通配置，设为目标运行用户所有，目录 700、凭据文件 600。数据库包含发送记录、队列和游标；保留它们可延续去重。不要迁移旧虚拟环境、PID/锁状态或 `service/state.json`。

SQLite 使用 WAL，运行中不能只复制主数据库。最简单是完整停机后复制整个 `data/`；在线备份用 SQLite backup API。只备份数据库不包含媒体字节，原图、视频同样要备份。验证新机器账号、QQ 目标及队列，再启用发送；同一目标只保持一个活跃实例。

视频会长期累积，目前没有自动清理。定期检查磁盘，备份加密或存放在可信权限范围，不推入源码仓库。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| guest / 正文锁定 | 在本机面板或 auth login 接入有效会员，而不只是登录原站 |
| invalid_config | 示例占位符未填写或格式错误；首次优先使用 qq configure |
| 已有配置 / configuration_exists | 初始化保护生效，不覆盖；备份后编辑原配置 |
| QQ 未连接 | 检查 .qq-venv、配置、密钥和权限，运行 qq doctor |
| 不能主动发言 / 40034105 | 检查目标群机器人主动发言开关，再运行主动测试 |
| 没收到历史内容 | 首次建基线属预期行为；旧视频用真实课程 ID 明确下载 |
| 下载失败 | 检查播放权限、FFmpeg、网络、磁盘、是否 YouTube/DRM；任务退避重试 |
| uncertain / 发送暂停 | 核对群内实际消息后，决定是否 qq retry --delivery ID |
| 面板 403 | 用 localhost/127.0.0.1、与服务器端口一致的 SSH 隧道 |

报告问题只提供错误码、系统/Python 版本和去除凭据及会员内容的日志，不提供 AppSecret、令牌、HAR、原始数据库或绑定码。

参考：[Python venv](https://docs.python.org/3/library/venv.html)、[systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)、[OpenSSH](https://man.openbsd.org/ssh.1)、[QQ 开放平台](https://q.qq.com/)。
