import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tradingx_monitor.__main__ import configure_qq, configure_telegram
from tradingx_monitor.config import Config
from tradingx_monitor.qq_push import QQError, configure_settings, load_settings, read_secret


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(root=Path(self.temp.name))
        self.config.prepare()

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_setup_keeps_secret_private_and_sending_disabled_until_binding(self):
        secret = 'fake-private-setup-secret'
        result = configure_settings(self.config, '123456', '123456789', '测试群', secret, True)
        settings = load_settings(self.config)
        self.assertFalse(settings['enabled'])
        self.assertEqual(settings['group_openid'], '')
        self.assertTrue(settings['send_video_files'])
        self.assertEqual(read_secret(self.config), secret)
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, (self.config.root / 'qq_bot.json').read_text())
        for file in (self.config.root / 'qq_bot.json', self.config.root / 'secrets/qq_app_secret'):
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.config.root.rglob('.qq-setup-*.tmp')), [])
        before = (self.config.root / 'qq_bot.json').read_bytes()
        with self.assertRaises(QQError):
            configure_settings(self.config, '654321', '987654321', '其他群', 'replacement')
        self.assertEqual((self.config.root / 'qq_bot.json').read_bytes(), before)
        self.assertEqual(read_secret(self.config), secret)

    def test_invalid_setup_and_interrupted_publish_leave_no_partial_credentials(self):
        for secret in ('', 'contains whitespace'):
            with self.assertRaises(QQError):
                configure_settings(self.config, '123456', '123456789', '测试群', secret)
            self.assertFalse((self.config.root / 'qq_bot.json').exists())
            self.assertFalse((self.config.root / 'secrets/qq_app_secret').exists())
        from os import link
        count = [0]
        def publish(source, destination):
            count[0] += 1
            if count[0] == 2:
                raise OSError('simulated publish failure')
            return link(source, destination)
        with patch('tradingx_monitor.qq_push.os.link', side_effect=publish):
            with self.assertRaises(OSError):
                configure_settings(self.config, '123456', '123456789', '测试群', 'fake-secret')
        self.assertFalse((self.config.root / 'qq_bot.json').exists())
        self.assertFalse((self.config.root / 'secrets/qq_app_secret').exists())
        self.assertEqual(list(self.config.root.rglob('.qq-setup-*.tmp')), [])

    def test_cli_requires_tty_and_never_echoes_secret_or_sends_messages(self):
        with patch('tradingx_monitor.__main__.sys.stdin.isatty', return_value=False):
            with self.assertRaises(ValueError):
                configure_qq(self.config)
        output = io.StringIO()
        with patch('tradingx_monitor.__main__.sys.stdin.isatty', return_value=True), \
             patch('builtins.input', side_effect=['123456', '123456789', '测试群', 'n']), \
             patch('tradingx_monitor.__main__.getpass.getpass', return_value='fake-hidden-secret') as hidden, \
             patch('sys.stdout', output):
            configure_qq(self.config)
        hidden.assert_called_once()
        self.assertNotIn('fake-hidden-secret', output.getvalue())
        self.assertFalse(load_settings(self.config)['enabled'])
        self.assertFalse(load_settings(self.config)['send_video_files'])

    def test_telegram_cli_is_offline_hidden_and_disabled_after_configuration(self):
        from tradingx_monitor.telegram_push import load_settings as telegram_settings, ready
        with patch('tradingx_monitor.__main__.sys.stdin.isatty', return_value=False):
            with self.assertRaises(ValueError): configure_telegram(self.config)
        token = str(123456) + ':' + 'x' * 35
        output = io.StringIO()
        with patch('tradingx_monitor.__main__.sys.stdin.isatty', return_value=True), \
             patch('builtins.input', side_effect=['-1001234567890', '', 'n']), \
             patch('tradingx_monitor.__main__.getpass.getpass', return_value=token), \
             patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')) as network, \
             patch('sys.stdout', output):
            configure_telegram(self.config)
        network.assert_not_called()
        self.assertNotIn(token, output.getvalue())
        self.assertFalse(telegram_settings(self.config)['enabled'])
        self.assertFalse(ready(self.config))


if __name__ == '__main__':
    unittest.main()
