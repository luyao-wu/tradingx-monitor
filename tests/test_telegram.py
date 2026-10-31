import hashlib
from io import BytesIO, StringIO
import json
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from tradingx_monitor.config import Config
from tradingx_monitor.images import ImageArchive
from tradingx_monitor.qq_push import QQQueue, save_settings as save_qq
from tradingx_monitor.service import supervise
from tradingx_monitor.store import Store, encode, now
from tradingx_monitor.telegram_push import (API, MAX_FILE_BYTES, MultipartBody, TelegramAPI, TelegramError,
    TelegramQueue, configure_settings, load_settings, ready, split_text)
from tradingx_monitor.video_downloads import VideoDownloads
from tradingx_monitor.web import make_server
from test_images import PNG, pictured
from test_monitor import post, snapshot


TOKEN = str(123456) + ":" + "f" * 35
CHAT = "-1001234567890"
VIDEO = {"id":158,"number":106,"title":"模拟课程","duration":"0:10","category":"indicator",
         "content_type":"video","url":"https://wall-street-skill.com/video/158"}


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name), port=0)
        self.store = Store(self.config)

    def tearDown(self): self.temp.cleanup()

    def enable(self, video=False, images=True):
        configure_settings(self.config, CHAT, TOKEN, 7, video)
        settings = load_settings(self.config)
        settings.update(enabled=True, send_images=images)
        (self.config.root / 'telegram_bot.json').write_text(encode(settings))
        return TelegramQueue(self.store)

    def ingest(self, posts):
        self.store.success(snapshot(posts, viewer='member', auth_configured=True), now())

    def api(self, responses):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.side_effect = responses
        return TelegramAPI(self.config, opener), opener

    def video_file(self, oversized=False):
        downloads = VideoDownloads(self.store)
        key = hashlib.sha256(str(len(downloads.status()['jobs'])).encode() + str(self.store.status()['event_count']).encode()).hexdigest()
        path = downloads.folder / (key + '.mp4')
        if oversized:
            with path.open('wb') as handle: handle.truncate(MAX_FILE_BYTES + 1)
        else: path.write_bytes(b'fake-complete-video-for-queue-test')
        checksum = 'f' * 64 if oversized else hashlib.sha256(path.read_bytes()).hexdigest()
        with self.store.connect() as db:
            self.store._event(db, now(), 'video_download_requested', video=VIDEO)
            event_id = db.execute('SELECT MAX(id) FROM events').fetchone()[0]
            db.execute("INSERT INTO video_download_jobs(event_id,video_id,video_json,status,asset_key,updated_at) VALUES(?,?,?,'ready',?,?)", (event_id, VIDEO['id'], encode(VIDEO), key, now()))
            db.execute('INSERT INTO video_files VALUES(?,?,?,?,?,?,?,?,?,?)', (key, VIDEO['id'], path.name, path.stat().st_size, checksum, 10, 1280, 720, 1, now()))
        return path

    def test_configuration_is_disabled_private_non_overwriting_and_default_off(self):
        self.assertFalse(ready(self.config))
        self.assertFalse(VideoDownloads(self.store).enabled())
        result = configure_settings(self.config, CHAT, TOKEN)
        self.assertFalse(result['enabled'])
        self.assertNotIn(TOKEN, json.dumps(result))
        for path in (self.config.root/'telegram_bot.json', self.config.root/'secrets/telegram_bot_token'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(TOKEN, (self.config.root/'telegram_bot.json').read_text())
        with self.assertRaises(TelegramError): configure_settings(self.config, CHAT, TOKEN)
        self.assertFalse(ready(self.config))
        self.assertEqual(list(self.config.root.rglob('.telegram-setup-*.tmp')), [])

    def test_disabled_worker_cli_returns_sanitized_error_without_qq_configuration(self):
        from tradingx_monitor.__main__ import main
        output = StringIO()
        with patch('tradingx_monitor.__main__.load_config', return_value=self.config), \
             patch('sys.argv', ['monitor','telegram','run']), patch('sys.stdout', output), \
             patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')) as network:
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(output.getvalue())['code'], 'not_enabled_or_token_missing')
        network.assert_not_called()

    def test_telegram_video_switch_is_independent_of_invalid_optional_qq_config(self):
        self.enable(video=True)
        (self.config.root / 'qq_bot.json').write_text('{invalid-json')
        self.assertTrue(VideoDownloads(self.store).enabled())

    def test_plain_text_has_topic_and_unicode_safe_chunks_without_parse_mode_or_paid_broadcast(self):
        self.enable()
        text = '中文🚀<tag>&_*' * 1000
        parts = split_text(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(part.encode('utf-16-le'))//2 <= 4000 for part in parts))
        api, opener = self.api([b'{"ok":true,"result":{"message_id":123}}'])
        self.assertEqual(api.send_message(CHAT, parts[0], 7), 123)
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith(API+'/bot'))
        body = json.loads(request.data)
        self.assertEqual(body['text'], parts[0])
        self.assertEqual(body['message_thread_id'], 7)
        self.assertNotIn('parse_mode', body)
        self.assertNotIn('allow_paid_broadcast', body)
        self.assertNotIn('Authorization', request.headers)

    def test_multipart_upload_is_bounded_preserves_bytes_and_requires_message_ack(self):
        self.enable()
        file = self.config.root/'original.png'
        file.write_bytes(PNG * 3000)
        api, opener = self.api([b'{"ok":true,"result":{"message_id":21}}'])
        self.assertEqual(api.send_file(CHAT, file, '原图.png', '配图', 7), 21)
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/sendDocument'))
        chunks = list(request.data)
        self.assertTrue(all(len(chunk) <= 65536 for chunk in chunks[1:-1]))
        payload = b''.join(chunks)
        self.assertEqual(len(payload), int(request.get_header('Content-length')))
        self.assertIn(file.read_bytes(), payload)
        self.assertIn('原图.png'.encode(), payload)
        self.assertNotIn(TOKEN.encode(), payload)
        api, _ = self.api([b'{"ok":true,"result":{}}'])
        with self.assertRaises(TelegramError) as error: api.send_file(CHAT, file, 'a.png', 'image')
        self.assertTrue(error.exception.uncertain)

    def test_oversized_direct_upload_is_rejected_before_network(self):
        self.enable()
        path = self.config.root/'large.mp4'
        with path.open('wb') as handle: handle.truncate(MAX_FILE_BYTES + 1)
        api, opener = self.api([])
        with self.assertRaises(TelegramError): api.send_file(CHAT, path, 'large.mp4', '', video=True)
        opener.open.assert_not_called()

    def test_doctor_only_checks_bot_identity_and_never_sends_to_a_chat(self):
        self.enable()
        api, opener = self.api([b'{"ok":true,"result":{"id":123456,"is_bot":true}}'])
        self.assertTrue(api.doctor()['authenticated'])
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/getMe'))
        self.assertEqual(json.loads(request.data), {})
        api, _ = self.api([b'{"ok":true,"result":{"is_bot":false}}'])
        with self.assertRaises(TelegramError): api.doctor()

    def test_errors_hide_token_and_remote_description_and_rate_limit_is_retryable(self):
        self.enable()
        opener = Mock()
        opener.open.side_effect = HTTPError(API+'/bot'+TOKEN+'/sendMessage', 400, 'private', {}, BytesIO(b'{"description":"private-chat-information"}'))
        api = TelegramAPI(self.config, opener)
        with self.assertRaises(TelegramError) as error: api.send_message(CHAT, 'hello')
        self.assertFalse(error.exception.uncertain)
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertNotIn('private-chat-information', str(error.exception))
        opener.open.side_effect = HTTPError(API, 503, 'server error', {}, BytesIO(b'{}'))
        with self.assertRaises(TelegramError) as error: api.send_message(CHAT, 'hello')
        self.assertTrue(error.exception.uncertain)
        opener.open.side_effect = HTTPError(API, 429, 'limited', {}, BytesIO(b'{"parameters":{"retry_after":40}}'))
        with self.assertRaises(TelegramError) as error: api.send_message(CHAT, 'hello')
        self.assertEqual(error.exception.retry_after, 40)
        opener.open.side_effect = URLError('private-url-'+TOKEN)
        with self.assertRaises(TelegramError) as error: api.send_message(CHAT, 'hello')
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn(TOKEN, str(error.exception))

    def test_first_activation_skips_history_and_text_updates_are_sent_once(self):
        queue = self.enable(images=False)
        self.ingest([post(1, '旧记录')]); queue.enqueue()
        self.assertEqual(queue.status()['outbox'], {})
        self.ingest([post(2, '新记录'), post(1, '旧记录')]); queue.enqueue(); queue.enqueue()
        self.assertEqual(queue.status()['outbox'], {'pending':1})
        api = Mock(); api.send_message.return_value = 11
        self.assertTrue(queue.deliver_one(api)); self.assertFalse(queue.deliver_one(api))
        self.assertEqual(api.send_message.call_args.args[0], CHAT)
        self.assertEqual(api.send_message.call_args.args[2], 7)
        self.ingest([post(2, '编辑后的记录'), post(1, '旧记录')]); queue.enqueue()
        self.assertEqual(queue.status()['outbox'], {'pending':1, 'sent':1})

    def test_original_images_share_archive_and_are_not_repeated_on_text_edit(self):
        queue = self.enable()
        self.ingest([]); queue.enqueue()
        self.config.token_file.write_text('fake-member-token'); self.config.token_file.chmod(0o600)
        opener = MagicMock(); opener.open.return_value.__enter__.return_value.read.return_value = PNG
        self.ingest([pictured()])
        ImageArchive(self.store, opener).sync([pictured()])
        queue.enqueue(); queue.enqueue()
        api = Mock(); api.send_message.return_value = 10; api.send_file.return_value = 11
        self.assertTrue(queue.deliver_one(api)); self.assertTrue(queue.deliver_one(api))
        self.assertEqual(api.send_file.call_args.args[1].read_bytes(), PNG)
        self.assertNotIn('video', api.send_file.call_args.kwargs)
        self.ingest([pictured('修改后的文字')]); queue.enqueue()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telegram_outbox WHERE message_type='image'").fetchone()[0], 1)

    def test_retry_after_waits_and_uncertain_delivery_does_not_block_qq(self):
        queue = self.enable(images=False)
        self.ingest([]); queue.enqueue(); self.ingest([post(1, '新记录')]); queue.enqueue()
        api = Mock(); api.send_message.side_effect = TelegramError('rate_limited', retry_after=60)
        self.assertFalse(queue.deliver_one(api)); self.assertFalse(queue.deliver_one(api))
        self.assertEqual(api.send_message.call_count, 1)
        with self.store.connect() as db: db.execute('UPDATE telegram_outbox SET retry_at=0')
        api.send_message.side_effect = TelegramError('network_error', True)
        self.assertFalse(queue.deliver_one(api)); self.assertFalse(queue.deliver_one(api))
        self.assertEqual(queue.status()['outbox'], {'uncertain':1})
        save_qq(self.config, {'appid':'123456','target_group_number':'123456789','target_group_name':'测试目标','group_openid':'opaque_group','enabled':True,'events':[]})
        qq = QQQueue(self.store)
        with self.store.connect() as db:
            db.execute("INSERT INTO qq_outbox(event_id,part,group_openid,content,updated_at) VALUES(999,1,'opaque_group','QQ独立消息',?)", (now(),))
        qq_api = Mock(); qq_api.send.return_value = 'qq-ack'
        self.assertTrue(qq.deliver_one(qq_api))
        with self.store.connect() as db: delivery = db.execute('SELECT id FROM telegram_outbox').fetchone()[0]
        queue.retry(delivery); api.send_message.side_effect = None; api.send_message.return_value = 20
        self.assertTrue(queue.deliver_one(api))

    def test_telegram_only_video_reuses_archive_and_oversized_video_uses_link(self):
        queue = self.enable(video=True)
        self.assertFalse((self.config.root/'qq_bot.json').exists())
        self.assertTrue(VideoDownloads(self.store).enabled())
        self.video_file()  # historical completed file must not be replayed
        queue.enqueue(); self.assertEqual(queue.status()['outbox'], {})
        path = self.video_file(); queue.enqueue(); queue.enqueue()
        api = Mock(); api.send_file.return_value = 31
        self.assertTrue(queue.deliver_one(api))
        self.assertEqual(api.send_file.call_args.args[1], path)
        self.assertEqual(api.send_file.call_args.args[2], '第106期_模拟课程.mp4')
        self.assertTrue(api.send_file.call_args.kwargs['video'])
        self.video_file(oversized=True); queue.enqueue(); queue.enqueue()
        api.send_message.return_value = 32
        self.assertTrue(queue.deliver_one(api))
        self.assertIn('50 MB', api.send_message.call_args.args[1])
        self.assertIn(VIDEO['url'], api.send_message.call_args.args[1])
        self.assertEqual(api.send_file.call_count, 1)
        self.assertEqual(queue.status()['sent_video_files'], 1)
        self.assertEqual(queue.status()['sent_video_links'], 1)

    def test_target_change_is_blocked_and_web_status_never_exposes_target_or_token(self):
        queue = self.enable()
        queue.enqueue()
        server = make_server(self.config, self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/telegram/status') as response: data = json.load(response)
        finally: server.shutdown(); server.server_close(); thread.join()
        self.assertNotIn(CHAT, json.dumps(data))
        self.assertNotIn(TOKEN, json.dumps(data))
        self.assertNotIn('message_thread_id', data)
        self.assertFalse(data['live_delivery_verified'])
        settings = load_settings(self.config); settings['chat_id'] = '-1009876543210'
        (self.config.root/'telegram_bot.json').write_text(encode(settings))
        with self.assertRaises(TelegramError) as error: queue.enqueue()
        self.assertEqual(error.exception.code, 'destination_changed')

    def test_restart_marks_only_telegram_sending_as_uncertain(self):
        queue = self.enable(images=False)
        self.ingest([]); queue.enqueue(); self.ingest([post(1, '新记录')]); queue.enqueue()
        with self.store.connect() as db: db.execute("UPDATE telegram_outbox SET status='sending'")
        queue.recover()
        self.assertEqual(queue.status()['outbox'], {'uncertain':1})
        self.assertTrue(queue.status()['paused'])
        self.assertEqual(queue.status()['last_error'], 'worker_interrupted')
        self.assertFalse(queue.deliver_one(Mock()))

    def test_supervisor_starts_telegram_and_video_without_qq_sdk_or_configuration(self):
        self.enable(video=True)
        processes = [Mock(pid=value) for value in (101,102,103)]
        for process in processes: process.poll.return_value = None
        stopped = Mock(); stopped.is_set.side_effect = [False, True]; stopped.wait.side_effect = [False, True]
        logger = logging.getLogger('supervisor'); before = list(logger.handlers)
        try:
            with patch('tradingx_monitor.service.threading.Event', return_value=stopped), \
                 patch('tradingx_monitor.service.signal.signal'), \
                 patch('tradingx_monitor.service.subprocess.Popen', side_effect=processes) as popen:
                supervise(self.config)
            commands = [call.args[0] for call in popen.call_args_list]
            self.assertEqual([command[-2:] for command in commands], [[str(self.config.root/'start.py'),'run'], ['telegram','run'], ['videos','worker']])
            for process in processes: process.terminate.assert_called_once()
            self.assertFalse((self.config.root/'service/state.json').exists())
        finally:
            for handler in list(logger.handlers):
                if handler not in before: logger.removeHandler(handler); handler.close()


if __name__ == '__main__': unittest.main()
