"""QQ official WebSocket listener; accepts only the local one-use bind command."""
import asyncio
import importlib
import signal

from .qq_push import QQAPI, QQError, QQQueue, load_settings, read_secret
from .runner import lock
from .store import Store, now


def run_qq(config):
    try:
        import botpy
        from botpy.http import Route
        from botpy.robot import Token
    except ImportError:
        raise ValueError("QQ 依赖尚未安装，请使用 .qq-venv 中的 Python 运行") from None
    store = Store(config)
    queue = QQQueue(store)
    settings = load_settings(config)
    api = QQAPI(config, settings)
    secret = read_secret(config)
    # The current official API domain differs from the older SDK release.
    Route.DOMAIN = "api.bot.qq.com"

    class CurrentToken(Token):
        async def update_access_token(self):
            self.access_token = await asyncio.to_thread(api.access_token)
            self.expires_in = api.token_until

    importlib.import_module("botpy.client").Token = CurrentToken

    def state(**values):
        with store.connect() as db:
            store._update(db, **values)

    class PushClient(botpy.Client):
        task = None

        async def on_ready(self):
            state(qq_connection="connected", qq_last_ready=now(), qq_last_error=None)
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self.deliver_loop())

        async def on_resumed(self):
            state(qq_connection="connected", qq_last_ready=now())

        async def on_group_at_message_create(self, message):
            content = message.content.strip()
            # QQ includes the bot mention before the user's command.
            if content.startswith("<@") and ">" in content:
                content = content.split(">", 1)[1].strip()
            if not content.startswith("绑定推送 "):
                return
            if not queue.bind(message.group_openid, content):
                return
            try:
                await asyncio.to_thread(api.send, message.group_openid,
                    f"订阅更新提醒已连接。\n目标：{settings['target_group_name']}（{settings['target_group_number']}）\n"
                    "从现在开始推送新帖与内容编辑，不回放历史帖子。", message.id)
                state(qq_last_binding_reply=now(), qq_last_error=None)
            except QQError as error:
                state(qq_last_error=error.code)

        async def deliver_loop(self):
            while True:
                try:
                    state(qq_heartbeat=now())
                    await asyncio.to_thread(queue.enqueue)
                    await asyncio.to_thread(queue.deliver_one, api)
                except QQError as error:
                    state(qq_last_error=error.code)
                except Exception:
                    state(qq_last_error="worker_error")
                await asyncio.sleep(5)

    with lock(config, "qq.lock"):
        queue.recover()
        state(qq_connection="connecting", qq_heartbeat=now())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = PushClient(intents=botpy.Intents(public_messages=True), timeout=20,
                            bot_log=False, ext_handlers=False)
        def stop_tasks():
            for task in asyncio.all_tasks(loop):
                task.cancel()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop_tasks)
        try:
            client.run(appid=settings["appid"], secret=secret)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        except Exception:
            state(qq_last_error="connection_failed")
        finally:
            state(qq_connection="stopped", qq_heartbeat=now())
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(client.close())
            loop.close()
