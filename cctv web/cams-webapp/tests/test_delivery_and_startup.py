import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import main


class DeliveryTests(unittest.TestCase):
    def test_partial_delivery_reports_failure(self):
        def send(recipient, *args):
            return {'recipient': recipient, 'success': recipient == 'ok@example.com', 'error': 'rejected'}

        with patch.object(main, '_get_smtp_targets', return_value=[('smtp', 25)]), patch.object(main, '_send_to_recipient', side_effect=send):
            ok, error, details = main._send_alert_email({'smtp_from': 'sender@example.com'}, ['ok@example.com', 'fail@example.com'], 'subject', 'body')
        self.assertFalse(ok)
        self.assertIn('fail@example.com', error)
        self.assertEqual(details['deliveries'][0]['deliveries'][0]['recipient'], 'ok@example.com')

    def test_fallback_retries_only_failed_recipients(self):
        calls = []
        def send(recipient, subject, body, sender, host, *args):
            calls.append((recipient, host))
            return {'recipient': recipient, 'success': host == 'backup' or recipient == 'ok@example.com', 'error': 'rejected'}

        with patch.object(main, '_get_smtp_targets', return_value=[('primary', 25), ('backup', 25)]), patch.object(main, '_send_to_recipient', side_effect=send):
            ok, error, details = main._send_alert_email({'smtp_from': 'sender@example.com'}, ['ok@example.com', 'fail@example.com'], 'subject', 'body')
        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertNotIn(('ok@example.com', 'backup'), calls)
        self.assertIn(('fail@example.com', 'backup'), calls)
        self.assertEqual(len(details['deliveries']), 2)

    def test_both_mode_reports_partial_failure(self):
        def send(recipient, subject, body, sender, host, *args):
            return {'recipient': recipient, 'success': host == 'primary', 'error': 'rejected'}

        with patch.object(main, '_get_smtp_targets', return_value=[('primary', 25), ('backup', 25)]), patch.object(main, '_send_to_recipient', side_effect=send):
            ok, error, _ = main._send_alert_email({'smtp_from': 'sender@example.com'}, ['a@example.com'], 'subject', 'body', mode='both')
        self.assertFalse(ok)
        self.assertIn('backup', error)


class StartupTests(unittest.TestCase):
    def test_configured_poll_interval_used_when_no_saved_setting(self):
        self.check_interval({}, 42)

    def test_saved_poll_interval_takes_precedence(self):
        self.check_interval({'refresh_interval': 30}, 30)

    def check_interval(self, settings, expected):
        client = MagicMock()
        client['cameras']['settings'].find_one.return_value = settings
        with patch.object(main, 'MongoClient', return_value=client), patch.object(main, 'monitor') as monitor, patch.object(main, '_default_poll_interval', 42), patch.object(main.threading, 'Thread'), patch.object(main, '_process_alert_cycle_once'):
            main.startup_event()
            self.assertEqual(monitor.poll_interval, expected)


if __name__ == '__main__':
    unittest.main()
