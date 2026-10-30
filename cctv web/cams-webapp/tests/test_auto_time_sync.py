import copy
from datetime import datetime
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import main
from app.auto_time_sync import AutoTimeSync, DEFAULT_POLICY, validate_policy


class AutoSyncTests(unittest.TestCase):
    def setUp(self):
        self.policy = {**DEFAULT_POLICY, 'enabled': True}
        self.nvrs = [{'ip': '192.0.2.1', 'name': 'One', 'nvr_time': '2020-01-01 00:00:00'},
                     {'ip': '192.0.2.2', 'name': 'Two'}]
        self.read = MagicMock(return_value=datetime.now().astimezone().isoformat())
        self.sync = MagicMock(return_value={'verified': True, 'offset_seconds': 0.5, 'message': 'Synced and verified'})
        self.record = MagicMock()
        self.worker = AutoTimeSync(lambda: {'auto_time_sync': self.policy}, lambda: self.nvrs,
                                   self.sync, self.record, self.read)

    def test_default_is_off_and_disabled_policy_does_no_network_work(self):
        self.assertFalse(validate_policy({})['enabled'])
        self.policy['enabled'] = False
        self.assertEqual(self.worker.run_once(), [])
        self.read.assert_not_called()
        self.sync.assert_not_called()

    def test_selection_can_be_one_or_multiple_nvrs(self):
        self.policy.update(scope='selected', nvr_ips=['192.0.2.2'])
        with patch('app.auto_time_sync.clock_offset', return_value=181):
            results = self.worker.run_once()
        self.assertEqual([r['ip'] for r in results], ['192.0.2.2'])
        self.sync.assert_called_once_with('192.0.2.2')

    def test_all_scope_uses_current_nvrs_including_new_devices(self):
        self.nvrs.append({'ip': '192.0.2.3', 'name': 'New'})
        results = self.worker.run_once()
        self.assertEqual(len(results), 3)
        self.sync.assert_not_called()
        self.assertTrue(all(r['state'] == 'within_threshold' for r in results))

    def test_fresh_clock_is_used_instead_of_stale_monitor_value(self):
        result = self.worker.run_once()
        self.assertEqual(self.read.call_count, 2)
        self.sync.assert_not_called()
        self.assertTrue(all(r['state'] == 'within_threshold' for r in result))

    def test_dashboard_mode_tracks_warning_threshold_in_both_directions(self):
        self.policy['match_warning_threshold'] = True
        settings = {'auto_time_sync': self.policy, 'time_tolerance': 120}
        self.worker.get_settings = lambda: settings
        self.nvrs = self.nvrs[:1]
        for threshold, offset, expected in [(120, 121, True), (120, -121, True),
                                            (120, 120, False), (240, 121, False)]:
            with self.subTest(threshold=threshold, offset=offset):
                settings['time_tolerance'] = threshold
                self.worker.last_attempt.clear()
                self.sync.reset_mock()
                with patch('app.auto_time_sync.clock_offset', return_value=offset):
                    self.worker.run_once()
                self.assertEqual(bool(self.sync.call_count), expected)

    def test_threshold_is_strict_and_covers_ahead_and_behind(self):
        self.nvrs = self.nvrs[:1]
        for offset, should_sync in [(180, False), (-180, False), (181, True), (-181, True)]:
            with self.subTest(offset=offset), patch('app.auto_time_sync.clock_offset', return_value=offset):
                self.worker.last_attempt.clear()
                self.sync.reset_mock()
                result = self.worker.run_once()
                self.assertEqual(bool(self.sync.call_count), should_sync)
                self.assertEqual(result[0]['state'], 'synced' if should_sync else 'within_threshold')

    def test_disable_after_clock_read_prevents_device_write(self):
        def read(nvr):
            self.policy['enabled'] = False
            return datetime.now().astimezone().isoformat()
        self.worker.read_clock = read
        with patch('app.auto_time_sync.clock_offset', return_value=300):
            results = self.worker.run_once()
        self.sync.assert_not_called()
        self.assertTrue(all(r['state'] == 'skipped' for r in results))

    def test_unreadable_device_is_not_synced_and_read_retries_back_off(self):
        self.nvrs = self.nvrs[:1]
        self.read.side_effect = ValueError('Unreadable')
        first = self.worker.run_once()
        second = self.worker.run_once()
        self.assertEqual(first[0]['state'], 'failed')
        self.assertEqual(second[0]['state'], 'cooldown')
        self.read.assert_called_once()
        self.sync.assert_not_called()

    def test_unverified_sync_is_failed_and_not_repeated_immediately(self):
        self.nvrs = self.nvrs[:1]
        self.sync.return_value = main.JSONResponse({'verified': False, 'message': 'Device rejected time'}, status_code=502)
        with patch('app.auto_time_sync.clock_offset', return_value=300):
            first = self.worker.run_once()
            second = self.worker.run_once()
        self.assertEqual(first[0]['state'], 'failed')
        self.assertIn('rejected', first[0]['message'])
        self.assertEqual(second[0]['state'], 'cooldown')
        self.sync.assert_called_once()

    def test_unavailable_settings_fail_closed(self):
        self.worker.get_settings = MagicMock(side_effect=RuntimeError('Database unavailable'))
        self.assertEqual(self.worker.run_once(), [])
        self.read.assert_not_called()

    def test_stop_prevents_work(self):
        self.worker.stop()
        self.assertEqual(self.worker.run_once(), [])
        self.read.assert_not_called()

    def test_status_is_copied_and_latest_results_are_recorded(self):
        self.worker.run_once()
        snapshot = self.worker.snapshot()
        self.assertEqual(len(snapshot['results']), 2)
        self.assertIsNotNone(snapshot['next_check_at'])
        snapshot['results'].clear()
        self.assertEqual(len(self.worker.snapshot()['results']), 2)
        self.assertEqual(self.record.call_count, 2)


class AutoSyncSettingsTests(unittest.TestCase):
    def test_invalid_settings_are_rejected(self):
        for changes in [{'enabled': 'true'}, {'match_warning_threshold': 'true'}, {'scope': 'other'}, {'threshold_seconds': -1},
                        {'check_interval_seconds': 0}, {'nvr_ips': '192.0.2.1'},
                        {'enabled': True, 'scope': 'selected', 'nvr_ips': []}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_policy(changes)

    def test_save_persists_policy_and_wakes_worker(self):
        policy = {**DEFAULT_POLICY, 'enabled': True, 'scope': 'selected', 'nvr_ips': ['192.0.2.1']}
        app = MagicMock()
        with patch.object(main, 'app', app), patch.object(main.monitor, 'get_snapshot', return_value=[{'ip': '192.0.2.1'}]), patch.object(main, '_save_settings_to_file') as save:
            result = main.api_set_settings({'auto_time_sync': policy})
        self.assertEqual(result['status'], 'ok')
        saved = app.state.db['settings'].update_one.call_args.args[1]['$set']['auto_time_sync']
        self.assertEqual(saved, policy)
        save.assert_called_once_with({'auto_time_sync': policy})
        app.state.auto_time_sync.wake_event.set.assert_called_once()

    def test_save_failure_is_reported_and_policy_is_not_mirrored(self):
        app = MagicMock()
        app.state.db['settings'].update_one.side_effect = RuntimeError('Unavailable')
        with patch.object(main, 'app', app), patch.object(main, '_save_settings_to_file') as save:
            result = main.api_set_settings({'auto_time_sync': copy.deepcopy(DEFAULT_POLICY)})
        self.assertEqual(result.status_code, 503)
        save.assert_not_called()

    def test_unknown_recorder_rejected_before_persistence(self):
        app = MagicMock()
        policy = {**DEFAULT_POLICY, 'enabled': True, 'scope': 'selected', 'nvr_ips': ['unknown']}
        with patch.object(main, 'app', app), patch.object(main.monitor, 'get_snapshot', return_value=[]):
            result = main.api_set_settings({'auto_time_sync': policy})
        self.assertEqual(result.status_code, 400)
        app.state.db['settings'].update_one.assert_not_called()

    def test_get_settings_exposes_default_off_policy(self):
        with patch.object(main, '_get_merged_settings', return_value={}):
            result = main.api_get_settings()
        self.assertEqual(json.loads(result.body)['auto_time_sync'], DEFAULT_POLICY)
