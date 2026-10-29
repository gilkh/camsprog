import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import main
from app.nvr_accounts import AccountReadError, parse_accounts, read_accounts
from app.nvr_time import clock_close, sync_onvif_time


def response(text='', status=200, payload=None):
    result = MagicMock(status_code=status, text=text)
    result.json.return_value = payload
    return result


class AccountTests(unittest.TestCase):
    def test_native_list_groups_users_without_exposing_device_secrets(self):
        rows = parse_accounts('<UserList xmlns="urn:test"><User><userName>admin</userName><userLevel>Administrator</userLevel><password>secret</password></User><User><userName>office</userName><userLevel>Operator</userLevel></User></UserList>', 'ISAPI')
        self.assertEqual([r['username'] for r in rows], ['office', 'admin'])
        self.assertTrue(rows[1]['is_admin'])
        self.assertNotIn('secret', json.dumps(rows))

    def test_onvif_list_empty_list_and_fault(self):
        self.assertEqual(parse_accounts('<GetUsersResponse xmlns="urn:test"/>', 'ONVIF'), [])
        rows = parse_accounts('<GetUsersResponse><User><Username>office</Username><UserLevel>User</UserLevel></User></GetUsersResponse>', 'ONVIF')
        self.assertEqual(rows[0]['username'], 'office')
        for body in ['<Envelope><Fault/></Envelope>', '<html>Login</html>', 'invalid']:
            with self.subTest(body=body), self.assertRaises(AccountReadError):
                parse_accounts(body, 'ONVIF')

    def test_missing_login_does_not_guess_credentials(self):
        with patch('app.nvr_accounts.requests.get') as get, self.assertRaises(AccountReadError):
            read_accounts({'ip': '192.0.2.1'})
        get.assert_not_called()

    def test_list_returns_saved_status_without_password(self):
        app = MagicMock()
        app.state.db['nvr_account_passwords'].find.return_value = [{'_id': {'nvr_ip': '192.0.2.1', 'username': 'office'}}]
        with patch.object(main, 'app', app), patch.object(main, '_find_nvr_by_ip', return_value={'name': 'Test'}), patch.object(main, 'read_accounts', return_value={'accounts': [{'username': 'office'}]}):
            result = main.api_nvr_accounts('192.0.2.1')
        row = json.loads(result.body)['accounts'][0]
        self.assertTrue(row['has_saved_password'])
        self.assertNotIn('password', row)
        self.assertEqual(result.headers['cache-control'], 'no-store')

    def test_save_and_reveal_only_write_to_app_database(self):
        app = MagicMock()
        app.state.db['nvr_account_passwords'].find_one.return_value = {'password': 'known-password'}
        with patch.object(main, 'app', app), patch.object(main, '_find_nvr_by_ip', return_value={'ip': '192.0.2.1'}), patch.object(main.requests, 'put') as device_write:
            saved = main.api_save_account_password('192.0.2.1', main.SavedAccountPassword(username='office', password='known-password'))
            revealed = main.api_get_account_password('192.0.2.1', 'office')
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(json.loads(revealed.body)['password'], 'known-password')
        self.assertEqual(revealed.headers['cache-control'], 'no-store')
        device_write.assert_not_called()

    def test_native_milesight_accounts_exclude_disabled_users_and_passwords(self):
        helper = MagicMock()
        helper._milesight_web_login.return_value = True
        helper._milesight_web_post_json.return_value = response(payload=[
            {'name': 'admin', 'type': 0, 'enable': 1, 'password': 'device-secret'},
            {'name': 'office', 'type': 2, 'enable': 1, 'password': 'another-secret'},
            {'name': 'disabled', 'type': 2, 'enable': 0},
        ])
        with patch('app.nvr_accounts.MonitorState', return_value=helper):
            result = read_accounts({'ip': '192.0.2.1', 'type': 'Milesight', 'username': 'admin', 'password': 'test'})
        self.assertEqual(result['source'], 'Milesight native')
        self.assertEqual([row['username'] for row in result['accounts']], ['office', 'admin'])
        self.assertNotIn('secret', json.dumps(result))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.nvr = {'ip': '192.0.2.1', 'type': 'Hikvision', 'username': 'admin', 'password': 'test'}

    def test_clock_handles_offsets_and_rejects_wrong_times(self):
        target = datetime(2026, 10, 29, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        self.assertTrue(clock_close('2026-10-29T10:00:00Z', target))
        self.assertTrue(clock_close('2026-10-29 12:00:05', target))
        self.assertFalse(clock_close('2026-10-29 11:59:00', target))
        self.assertFalse(clock_close(None, target))

    def test_hikvision_requires_correct_clock_after_write(self):
        for correct in (True, False):
            text = datetime.now().astimezone().isoformat() if correct else '2020-01-01T00:00:00Z'
            with self.subTest(correct=correct), patch.object(main.monitor, 'get_snapshot', return_value=[self.nvr]), patch.object(main.monitor, 'refresh_once'), patch.object(main.requests, 'put', return_value=response()), patch.object(main.requests, 'get', return_value=response(f'<Time><localTime>{text}</localTime></Time>')), patch.object(main.time, 'sleep'), patch.object(main, 'sync_onvif_time', return_value=(False, 'Unavailable')) as fallback:
                result = main.api_sync_time('192.0.2.1')
            if correct:
                self.assertEqual(result['status'], 'ok')
                fallback.assert_not_called()
            else:
                self.assertEqual(result.status_code, 502)

    def test_unknown_vendor_uses_onvif(self):
        self.nvr['type'] = None
        with patch.object(main.monitor, 'get_snapshot', return_value=[self.nvr]), patch.object(main.monitor, 'refresh_once'), patch.object(main, 'sync_onvif_time', return_value=(True, None)) as fallback:
            result = main.api_sync_time('192.0.2.1')
        self.assertEqual(result['method'], 'ONVIF')
        fallback.assert_called_once_with(self.nvr)

    def test_milesight_changing_clock_is_insufficient(self):
        self.nvr['type'] = 'Milesight Old'
        def get(url, **kwargs):
            if 'checkUser' in url:
                return response('type=0\nblankTime=0')
            if 'get.system.time' in url:
                return response('manual_time=2020-01-01 00:00:01')
            return response('OK')
        with patch.object(main.monitor, 'get_snapshot', return_value=[self.nvr]), patch.object(main.monitor, '_milesight_web_login', return_value=False), patch.object(main.requests, 'get', side_effect=get), patch.object(main.time, 'sleep'), patch.object(main, 'sync_onvif_time', return_value=(False, 'Unavailable')):
            result = main.api_sync_time('192.0.2.1')
        self.assertEqual(result.status_code, 502)

    def test_uniview_verifies_local_epoch_and_turns_off_ntp(self):
        self.nvr['type'] = 'Uniview'
        written = []
        def get(url, **kwargs):
            if 'Synchronization' in url:
                return response('json', payload={'Response': {'Data': {'Enabled': 0}}})
            # The device's write API takes local epoch, but reads return UTC.
            data = {**written[-1], 'DeviceTime': int(datetime.now().timestamp())} if written else {'DeviceTime': 0, 'NTPServerInfo': {'Enabled': 1}}
            return response('json', payload={'Response': {'Data': data}})
        def put(url, **kwargs):
            written.append(kwargs['json'])
            return response('json', payload={'Response': {'StatusCode': 0}})
        with patch.object(main.monitor, 'get_snapshot', return_value=[self.nvr]), patch.object(main.monitor, 'refresh_once'), patch.object(main.requests, 'get', side_effect=get), patch.object(main.requests, 'put', side_effect=put), patch.object(main.time, 'sleep'), patch.object(main, 'sync_onvif_time') as fallback:
            result = main.api_sync_time('192.0.2.1')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(written[-1]['NTPServerInfo']['Enabled'], 0)
        offset = int(datetime.now().astimezone().utcoffset().total_seconds())
        self.assertAlmostEqual(written[-1]['DeviceTime'], int(datetime.now().timestamp()) + offset, delta=2)
        self.assertTrue(result['verified'])
        fallback.assert_not_called()

    def test_milesight_uses_native_json_and_reads_back_clock(self):
        self.nvr['type'] = 'Milesight'
        settings = {'dateYear': '2026', 'timeZone': 'UTC-2', 'timeZoneName': 'LBN', 'dstEnable': 1, 'ntpEnable': 1}
        replies = [response(payload=settings), response('OK'), response(payload={'serverTime': datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')})]
        with patch.object(main.monitor, 'get_snapshot', return_value=[self.nvr]), patch.object(main.monitor, '_milesight_web_login', return_value=True), patch.object(main.monitor, '_milesight_web_post_json', side_effect=replies) as post, patch.object(main.time, 'sleep'), patch.object(main, 'sync_onvif_time') as fallback:
            result = main.api_sync_time('192.0.2.1')
        self.assertTrue(result['verified'])
        self.assertEqual(result['method'], 'Milesight native')
        payload = post.call_args_list[1].kwargs['payload']
        self.assertEqual(payload['ntpEnable'], 0)
        self.assertEqual(payload['timeZoneName'], 'LBN')
        self.assertIn('manualTime', payload)
        fallback.assert_not_called()

    def test_bulk_reports_partial_success_with_failure_reason(self):
        nvrs = [{'ip': '192.0.2.1', 'name': 'Working'}, {'ip': '192.0.2.2', 'name': 'Unavailable'}]
        replies = [{'status': 'ok', 'verified': True}, main.JSONResponse({'status': 'error', 'verified': False, 'message': 'Offline'}, status_code=502)]
        with patch.object(main.monitor, 'get_snapshot', return_value=nvrs), patch.object(main, 'api_sync_time', side_effect=replies):
            result = main.api_sync_time_all()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['verified_count'], 1)
        self.assertEqual(result['failed_count'], 1)
        self.assertEqual(result['results'][1]['message'], 'Offline')

    def test_onvif_reads_back_utc_and_uses_device_clock_for_security(self):
        tds, tt = 'http://www.onvif.org/ver10/device/wsdl', 'http://www.onvif.org/ver10/schema'
        now = datetime.now(timezone.utc)
        clock = f'<GetSystemDateAndTimeResponse xmlns="{tds}"><SystemDateAndTime xmlns="{tt}"><DaylightSavings>false</DaylightSavings><UTCDateTime><Time><Hour>{now.hour}</Hour><Minute>{now.minute}</Minute><Second>{now.second}</Second></Time><Date><Year>{now.year}</Year><Month>{now.month}</Month><Day>{now.day}</Day></Date></UTCDateTime></SystemDateAndTime></GetSystemDateAndTimeResponse>'
        def soap(body):
            return f'<Envelope xmlns="http://www.w3.org/2003/05/soap-envelope"><Body>{body}</Body></Envelope>'
        replies = [response(soap(clock)), response(soap(f'<SetSystemDateAndTimeResponse xmlns="{tds}"/>')), response(soap(clock))]
        with patch('app.nvr_time.requests.post', side_effect=replies) as post:
            ok, error = sync_onvif_time(self.nvr)
        self.assertTrue(ok, error)
        self.assertEqual(post.call_count, 3)
        self.assertNotIn(b'UsernameToken', post.call_args_list[0].kwargs['data'])
        self.assertIn(b'UsernameToken', post.call_args_list[1].kwargs['data'])
