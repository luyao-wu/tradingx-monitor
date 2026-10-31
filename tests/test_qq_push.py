import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError
from io import BytesIO

from tradingx_monitor.config import Config
from tradingx_monitor.qq_push import QQAPI, QQError, QQQueue, format_parts, load_settings, save_settings
from tradingx_monitor.store import Store, now
from test_monitor import post, snapshot


class QQPushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name))
        self.store = Store(self.config)
        save_settings(self.config, {"appid":"123456", "target_group_number":"123456789", "target_group_name":"测试群", "group_openid":"", "enabled":False, "events":["new_post","updated"]})
        self.queue = QQQueue(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, rows):
        self.store.success(snapshot(rows, viewer="member",auth_configured=True), now())

    def bind(self):
        self.assertTrue(self.queue.bind("opaque_group_openid", self.queue.binding_command()))

    def test_binding_rejects_wrong_command_raw_number_and_rebinding(self):
        self.assertFalse(self.queue.bind("other_openid","wrong command"))
        self.assertFalse(self.queue.bind("123456789",self.queue.binding_command()))
        self.bind()
        self.assertFalse(self.queue.bind("other_openid",self.queue.binding_command()))
        self.assertEqual(load_settings(self.config)["group_openid"],"opaque_group_openid")

    def test_activation_skips_old_posts_but_new_and_edits_are_queued(self):
        self.ingest([post(1,"旧帖")])
        self.bind()
        self.queue.enqueue()
        self.assertEqual(self.queue.status()["outbox"],{})
        self.ingest([post(2,"新帖"),post(1,"旧帖")])
        self.queue.enqueue()
        self.queue.enqueue()
        self.assertEqual(self.queue.status()["outbox"],{"pending":1})
        api=Mock();api.send.return_value="ack-1"
        self.assertTrue(self.queue.deliver_one(api))
        self.assertFalse(self.queue.deliver_one(api))
        api.send.assert_called_once()
        self.assertEqual(api.send.call_args.args[0],"opaque_group_openid")
        self.assertIn("新帖",api.send.call_args.args[1])
        self.ingest([post(2,"编辑后的新帖"),post(1,"旧帖")])
        self.queue.enqueue()
        self.assertEqual(self.queue.status()["outbox"],{"pending":1,"sent":1})

    def test_uncertain_delivery_is_not_automatically_repeated(self):
        self.ingest([]);self.bind()
        self.ingest([post(1,"新帖")]);self.queue.enqueue()
        api=Mock();api.send.side_effect=QQError("network_error",True)
        self.assertFalse(self.queue.deliver_one(api))
        self.assertFalse(self.queue.deliver_one(api))
        self.assertEqual(api.send.call_count,1)
        self.assertEqual(self.queue.status()["outbox"],{"uncertain":1})
        self.assertEqual(self.queue.status()["last_error"],"network_error")

    def test_interrupted_send_is_marked_uncertain_on_restart(self):
        self.ingest([]);self.bind();self.ingest([post(1,"新帖")]);self.queue.enqueue()
        with self.store.connect() as db:
            db.execute("UPDATE qq_outbox SET status='sending'")
        self.queue.recover()
        self.assertEqual(self.queue.status()["outbox"],{"uncertain":1})

    def test_failed_active_test_stays_visible_after_reconnect_until_test_succeeds(self):
        self.bind()
        api=Mock();api.send.side_effect=QQError("40034105")
        with self.assertRaises(QQError):
            self.queue.send_test(api)
        with self.store.connect() as db:
            self.store._update(db, qq_connection="connected", qq_last_error=None)
        self.assertEqual(self.queue.status()["last_error"],"40034105")
        self.assertFalse(self.queue.status()["active_send_verified"])
        api.send.side_effect=None;api.send.return_value="active-test-ack"
        self.assertEqual(self.queue.send_test(api),"active-test-ack")
        self.assertIsNone(self.queue.status()["last_error"])
        self.assertTrue(self.queue.status()["active_send_verified"])
        self.assertIsNotNone(self.queue.status()["last_test"])

    def test_long_text_is_split_without_dropping_body_and_locked_text_is_omitted(self):
        body="完整正文测试"*700
        parts=format_parts(post(1,body),"new_post")
        self.assertGreater(len(parts),1)
        self.assertTrue(all(len(p)<1800 for p in parts))
        recovered=''.join(p.split('\n\n',1)[1].rsplit('\n\n',1)[0] for p in parts)
        self.assertEqual(recovered,body)
        locked=post();locked['text']='不应发送的占位文字'
        self.assertNotIn('不应发送的占位文字',format_parts(locked,'new_post')[0])

    def test_api_uses_current_official_domain_and_never_exposes_secret(self):
        secret=self.config.root/'secrets'/'qq_app_secret'
        secret.write_text('fake-test-app-secret');secret.chmod(0o600)
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.side_effect=[b'{"access_token":"fake-test-access-token","expires_in":7200}',b'{"id":"message-ack"}']
        api=QQAPI(self.config,opener=opener)
        self.assertEqual(api.send('opaque_group_openid','test message'),'message-ack')
        requests=[call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(requests[0].full_url,'https://api.bot.qq.com/app/getAppAccessToken')
        self.assertEqual(json.loads(requests[0].data)['clientSecret'],'fake-test-app-secret')
        self.assertEqual(requests[1].headers['Authorization'],'QQBot fake-test-access-token')
        self.assertNotIn('fake-test-app-secret',json.dumps(self.queue.status()))
        with self.assertRaises(QQError):api.send('123456789','test')

    def test_remote_error_is_sanitized_and_missing_ack_is_uncertain(self):
        opener=MagicMock()
        opener.open.side_effect=HTTPError('https://api.bot.qq.com/test',403,'private error',{},BytesIO(b'{"code":40034105,"message":"private-sensitive-data"}'))
        api=QQAPI(self.config,opener=opener);api.token='fake';api.token_until=10**12
        with self.assertRaises(QQError) as error:api.send('opaque_group_openid','test')
        self.assertEqual(error.exception.code,'40034105')
        self.assertNotIn('private-sensitive-data',str(error.exception))
        opener=MagicMock();opener.open.return_value.__enter__.return_value.read.return_value=b'{}'
        api=QQAPI(self.config,opener=opener);api.token='fake';api.token_until=10**12
        with self.assertRaises(QQError) as error:api.send('opaque_group_openid','test')
        self.assertTrue(error.exception.uncertain)


if __name__=='__main__':unittest.main()
