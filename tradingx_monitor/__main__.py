import argparse
import csv
import getpass
import io
import json
import os
from pathlib import Path
import sys

from .auth import connect_account, save_token

from .client import Client, FetchError, read_token
from .config import load_config
from .runner import collect, run, setup_logging
from .service import service, supervise
from .store import Store, now
from .qq_push import QQError
from .video_downloads import VideoDownloadError


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser():
    p = argparse.ArgumentParser(description="TradingX 本地自动监控（核心 Python 3.9+；QQ 和视频有附加依赖）")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="只读验证实时接口及正文访问权限")
    commands.add_parser("once", help="执行一轮采集，首次只建立历史基线")
    r = commands.add_parser("run", help="持续采集并提供本地状态页")
    r.add_argument("--no-dashboard", action="store_true")
    commands.add_parser("status", help="查看本地归档与健康状态，不请求网站")
    commands.add_parser("supervise", help=argparse.SUPPRESS)
    a = commands.add_parser("auth", help="在本机保存或清除会员凭据（隐藏输入）")
    a.add_argument("action", choices=("login", "set", "clear", "status"))
    s = commands.add_parser("service", help="管理本机后台守护进程")
    s.add_argument("action", choices=("start", "stop", "status"))
    q = commands.add_parser("qq", help="QQ 官方机器人状态与发送服务")
    q.add_argument("action", choices=("configure", "status", "doctor", "run", "test", "retry", "images", "video-test"))
    q.add_argument("--delivery", type=int)
    q.add_argument("--post", type=int)
    images = commands.add_parser("images", help="下载、校验和查看本机原图归档")
    images.add_argument("action", choices=("sync", "status"))
    videos = commands.add_parser("videos", help="视频目录更新监控与状态")
    videos.add_argument("action", choices=("once", "status", "download", "worker"))
    videos.add_argument("--id", type=int)
    e = commands.add_parser("export", help="导出本地归档，不含凭据")
    e.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    e.add_argument("--output", type=Path, required=True)
    return p


def auth(config, action):
    if action == "clear":
        config.token_file.unlink(missing_ok=True)
        output({"local_token_removed": True, "environment_token_present": bool(os.environ.get("TRADINGX_TOKEN"))})
    elif action == "status":
        output({"configured": bool(read_token(config)), "local_file_present": config.token_file.exists(),
                "environment_token_present": bool(os.environ.get("TRADINGX_TOKEN")), "token_displayed": False})
    elif action == "login":
        if not sys.stdin.isatty():
            raise ValueError("请在交互式终端执行 auth login；不接受命令行或管道中的账号密码")
        print("账号密码仅用于向 wall-street-skill.com 登录，本机只保存登录令牌。")
        email = getpass.getpass("登录邮箱（隐藏输入）: ")
        password = getpass.getpass("密码（隐藏输入）: ")
        setup_logging(config)
        output(connect_account(config, Store(config), email, password))
    else:
        if not sys.stdin.isatty():
            raise ValueError("请在交互式终端执行 auth set；本命令不接受命令行或管道中的凭据")
        print("在已登录的会员网页开发者工具 Console 中执行：copy(localStorage.getItem('ws_token'))")
        print("然后粘贴到下面的隐藏输入。只保存到本机 secrets/token，不会显示或写入日志。")
        value = getpass.getpass("会员凭据（隐藏输入）: ").strip()
        if value.startswith("Bearer "):
            value = value[7:].strip()
        if not value or value in ("null", "undefined") or any(c.isspace() for c in value) or len(value) > 16384:
            raise ValueError("凭据为空或格式不正确，未修改已有配置")
        save_token(config, value)
        print("已保存。下一轮采集会自动使用；运行 ./monitor doctor 可验证会员正文。")


def export(store, args):
    rows = store.posts(-1)
    buffer = io.StringIO()
    if args.format == "jsonl":
        for row in rows:
            buffer.write(json.dumps(row, ensure_ascii=False) + "\n")
    else:
        writer = csv.DictWriter(buffer, fieldnames=("id", "createdAt", "editedAt", "locked", "text", "textLength", "likes", "views", "imageCount", "url", "firstSeen", "lastSeen", "cachedText", "cachedAt"))
        writer.writeheader()
        for row in rows:
            clean = {k: v for k, v in row.items() if k != "images"}
            clean["imageCount"] = len(row["images"])
            # Prevent formula execution when users open remotely supplied text in a spreadsheet.
            for key, value in clean.items():
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    clean[key] = "'" + value
            writer.writerow(clean)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Do not clobber an existing export or follow a symlink.
    descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8-sig" if args.format == "csv" else "utf-8", newline="") as handle:
        handle.write(buffer.getvalue())
    output({"exported": len(rows), "path": str(args.output.resolve()), "format": args.format})


def configure_qq(config):
    from .qq_push import configure_settings
    if not sys.stdin.isatty():
        raise ValueError("请在交互式终端执行 qq configure；AppSecret 使用隐藏输入，不接受命令行或管道。")
    if (config.root / "qq_bot.json").exists() or (config.root / "secrets" / "qq_app_secret").exists():
        raise ValueError("已有 QQ 配置，未修改。请按部署文档备份并编辑原配置；更换密钥后重启后台。")
    appid = input("QQ 官方机器人 AppID: ").strip()
    number = input("目标 QQ 数字群号: ").strip()
    name = input("目标群名称: ").strip()
    choice = input("自动下载并发送授权视频文件？[y/N]: ").strip().lower()
    if choice not in ("", "y", "yes", "n", "no"):
        raise ValueError("请输入 y 或 n，尚未保存任何 QQ 配置。")
    secret = getpass.getpass("机器人 AppSecret（隐藏输入）: ").strip()
    output(configure_settings(config, appid, number, name, secret, choice in ("y", "yes")))
    print("已保存本机配置，尚未启用发送。启动后台后，按监控面板显示的指令绑定目标群。")


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        config = load_config()
        if args.command == "auth":
            auth(config, args.action)
        elif args.command == "service":
            output(service(config, args.action))
        elif args.command == "supervise":
            supervise(config)
        elif args.command == "images":
            from .images import ImageArchive
            store = Store(config)
            archive = ImageArchive(store)
            result = archive.sync(store.posts(-1)) if args.action == "sync" else {}
            output({**result, **archive.status()})
        elif args.command == "videos":
            from .videos import VideoMonitor
            from .video_downloads import VideoDownloads,run_downloads
            video_monitor = VideoMonitor(Store(config))
            if args.action == "status":
                output({**video_monitor.status(),"downloads":VideoDownloads(video_monitor.store).status()})
            elif args.action == "worker":
                run_downloads(config)
            elif args.action == "download":
                if not args.id or args.id<1:raise ValueError("请用 --id 指定网站课程 ID；期数与课程 ID 不同")
                VideoDownloads(video_monitor.store).request(args.id)
                output({"queued":True,"video_id":args.id})
            else:
                result, error = video_monitor.poll()
                if error:
                    raise error
                output(result)
        elif args.command == "qq":
            from .qq_push import QQAPI, QQError, QQQueue, load_settings
            store = Store(config)
            queue = QQQueue(store)
            if args.action == "configure":
                configure_qq(config)
            elif args.action == "status":
                output(queue.status())
            elif args.action == "doctor":
                api = QQAPI(config)
                bot = api._request("/users/@me")
                gateway = api._request("/gateway/bot")
                output({"authenticated": True, "bot_id": bot.get("id"), "bot_name": bot.get("username"),
                        "gateway_available": isinstance(gateway.get("url"), str), "bound": queue.status()["bound"]})
            elif args.action == "run":
                from .qq_worker import run_qq
                run_qq(config)
            elif args.action == "test":
                settings = load_settings(config)
                message_id = queue.send_test(QQAPI(config, settings))
                output({"sent": True, "message_id": message_id, "group": settings["target_group_name"]})
            elif args.action == "video-test":
                message_id = queue.send_video_test(QQAPI(config))
                output({"sent": True, "test": "video_notification", "message_id": message_id})
            elif args.action == "images":
                if not args.post or args.post < 1:
                    raise ValueError("请用 --post 指定要补发配图的帖子 ID")
                output({"queued_images": queue.enqueue_images(args.post), "post": args.post})
            else:
                if not args.delivery:
                    raise ValueError("请用 --delivery 指定需要重试的发送记录；结果不确定时先核对 QQ 群避免重复")
                with store.connect() as db:
                    db.execute("UPDATE qq_outbox SET status='pending',error_code=NULL WHERE id=? AND status IN ('failed','uncertain')", (args.delivery,))
                output({"queued_for_retry": args.delivery})
        elif args.command == "doctor":
            result = Client(config).fetch()
            output({"source_reachable": True, "viewer": result["viewer"], "auth_configured": result["auth_configured"],
                    "posts_in_scan": len(result["posts"]), "site_total": result["total"],
                    "readable_posts": sum(not p["locked"] for p in result["posts"]),
                    "locked_posts": sum(p["locked"] for p in result["posts"]), "truncated": result["truncated"],
                    "latest_post": {k: result["posts"][0][k] for k in ("id", "createdAt", "locked")} if result["posts"] else None})
        else:
            store = Store(config)
            if args.command == "status":
                output(store.status())
            elif args.command == "export":
                export(store, args)
            else:
                setup_logging(config)
                if args.command == "once":
                    result, error = collect(config, store)
                    if error:
                        output({"ok": False, "code": error.code, "message": str(error)})
                        return 1
                    output(result)
                else:
                    run(config, store, args.no_dashboard)
        return 0
    except FetchError as error:
        output({"ok": False, "code": error.code, "message": str(error)})
        return 1
    except QQError as error:
        output({"ok": False, "code": error.code, "message": str(error)})
        return 1
    except VideoDownloadError as error:
        output({"ok":False,"code":error.code,"message":str(error)})
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        # Deliberately avoid arbitrary exception values: paths or config may contain secrets.
        print("操作失败：请检查配置格式、文件权限、端口占用及输出文件是否已存在。", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
