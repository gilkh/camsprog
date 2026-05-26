import sys
import os
import unittest
from unittest.mock import patch, MagicMock, ANY

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.main import _process_alert_cycle_once, _build_current_alerts

class TestAlertSettings(unittest.TestCase):
    @patch('app.main.app')
    @patch('app.main._get_merged_settings')
    @patch('app.main._normalize_smtp_to')
    @patch('app.main._send_alert_email')
    def test_new_alert_sends_immediate_email_before_next_summary(self, mock_send, mock_norm, mock_merged, mock_app):
        import time
        now = int(time.time())
        mock_norm.return_value = ["admin@example.com"]
        mock_send.return_value = (True, "", {"host": "smtp.example.com", "port": 25})
        mock_db = MagicMock()
        mock_app.state.db = mock_db
        alerts_col = mock_db["alerts"]
        email_col = mock_db["email_events"]
        new_doc = {
            "_id": "nvr_offline:1",
            "alert_type": "nvr_offline",
            "severity": "critical",
            "nvr_name": "NVR 1",
            "nvr_ip": "192.168.1.10",
            "message": "NVR NVR 1 (192.168.1.10) is offline",
        }
        alerts_col.find.side_effect = [
            [],
            [new_doc],
        ]

        with patch('app.main.monitor') as mock_monitor:
            mock_monitor.get_snapshot.return_value = []
            with patch('app.main._build_current_alerts', return_value={"nvr_offline:1": new_doc}):
                mock_merged.return_value = {
                    "email_enabled": True,
                    "smtp_to": ["admin@example.com"],
                    "alert_email_interval_seconds": 600,
                    "last_unack_alert_summary_emailed_at": now,
                    "acknowledged_alert_email_enabled": False,
                }

                _process_alert_cycle_once()

        mock_send.assert_called_once()
        alerts_col.update_many.assert_any_call(
            {"_id": {"$in": ["nvr_offline:1"]}},
            {"$set": {"last_emailed_at": ANY, "last_email_status": "success"}}
        )
        email_col.insert_one.assert_called_once()
        self.assertEqual(email_col.insert_one.call_args.args[0]["email_type"], "new_alert")

    @patch('app.main.app')
    @patch('app.main._get_merged_settings')
    @patch('app.main._normalize_smtp_to')
    @patch('app.main._send_alert_email')
    def test_unacknowledged_summary_uses_global_interval(self, mock_send, mock_norm, mock_merged, mock_app):
        import time
        now = int(time.time())
        mock_norm.return_value = ["admin@example.com"]
        mock_db = MagicMock()
        mock_app.state.db = mock_db
        alerts_col = mock_db["alerts"]
        alerts_col.find.return_value = [
            {"_id": "nvr_offline:1", "alert_type": "nvr_offline", "severity": "critical"}
        ]

        with patch('app.main.monitor') as mock_monitor:
            mock_monitor.get_snapshot.return_value = []
            with patch('app.main._build_current_alerts', return_value={}):
                mock_merged.return_value = {
                    "email_enabled": True,
                    "smtp_to": ["admin@example.com"],
                    "alert_email_interval_seconds": 600,
                    "last_unack_alert_summary_emailed_at": now,
                    "acknowledged_alert_email_enabled": False,
                }

                _process_alert_cycle_once()

        mock_send.assert_not_called()

    @patch('app.main.app')
    @patch('app.main._get_merged_settings')
    @patch('app.main._normalize_smtp_to')
    @patch('app.main._send_alert_email')
    def test_acknowledged_alert_reminder_uses_separate_interval(self, mock_send, mock_norm, mock_merged, mock_app):
        import time
        now = int(time.time())
        mock_norm.return_value = ["admin@example.com"]
        mock_send.return_value = (True, "", {"host": "smtp.example.com", "port": 25})
        mock_db = MagicMock()
        mock_app.state.db = mock_db
        alerts_col = mock_db["alerts"]
        alerts_col.find.return_value = [
            {
                "_id": "nvr_time_drift:1",
                "alert_type": "nvr_time_drift",
                "severity": "warning",
                "acknowledged": True,
                "acknowledged_at": now - 90000,
            }
        ]

        with patch('app.main.monitor') as mock_monitor:
            mock_monitor.get_snapshot.return_value = []
            with patch('app.main._build_current_alerts', return_value={}):
                mock_merged.return_value = {
                    "email_enabled": True,
                    "smtp_to": ["admin@example.com"],
                    "alert_email_interval_seconds": 600,
                    "last_unack_alert_summary_emailed_at": now,
                    "acknowledged_alert_email_enabled": True,
                    "acknowledged_alert_email_interval_seconds": 86400,
                }

                _process_alert_cycle_once()

        mock_send.assert_called_once()
        alerts_col.update_many.assert_any_call(
            {"_id": {"$in": ["nvr_time_drift:1"]}},
            {"$set": {"last_ack_reminder_emailed_at": ANY, "ack_reminder_email_status": "success"}}
        )

    @patch('app.main.app')
    @patch('app.main._get_merged_settings')
    @patch('app.main._normalize_smtp_to')
    @patch('app.main._send_alert_email')
    def test_process_alert_cycle_filtering(self, mock_send, mock_norm, mock_merged, mock_app):
        # Set up mocks
        mock_norm.return_value = ["admin@example.com"]
        mock_send.return_value = (True, "", {"host": "smtp.example.com", "port": 25})
        
        # Mock database
        mock_db = MagicMock()
        mock_app.state.db = mock_db
        alerts_col = mock_db["alerts"]
        email_col = mock_db["email_events"]
        
        # Mock monitor
        with patch('app.main.monitor') as mock_monitor:
            mock_monitor.get_snapshot.return_value = []
            
            # We mock the build_current_alerts to return nothing because we will put due alerts in alerts_col.find
            with patch('app.main._build_current_alerts', return_value={}):
                
                # Test Case 1: Overall email notification disabled
                mock_merged.return_value = {
                    "email_enabled": False,
                    "email_nvr_offline": True,
                    "email_nvr_time_drift": True,
                    "email_recording_mismatch": True,
                    "email_channel_not_recording": True,
                    "smtp_to": ["admin@example.com"],
                    "alert_email_interval_seconds": 600,
                    "acknowledged_alert_email_enabled": False,
                }
                
                # 4 active alerts due for email
                alerts_col.find.return_value = [
                    {"_id": "nvr_offline:1", "alert_type": "nvr_offline", "severity": "critical"},
                    {"_id": "nvr_time_drift:1", "alert_type": "nvr_time_drift", "severity": "warning"},
                    {"_id": "recording_expected_mismatch:1", "alert_type": "recording_expected_mismatch", "severity": "warning"},
                    {"_id": "channel_not_recording:1", "alert_type": "channel_not_recording", "severity": "non-critical"}
                ]
                
                _process_alert_cycle_once()
                
                # Since overall email is disabled, all 4 alerts should be in to_skip
                # and none sent. Check update_many was called to mark them skipped.
                alerts_col.update_many.assert_any_call(
                    {"_id": {"$in": ["nvr_offline:1", "nvr_time_drift:1", "recording_expected_mismatch:1", "channel_not_recording:1"]}},
                    {"$set": {"last_email_status": "skipped"}}
                )
                mock_send.assert_not_called()
                
                # Reset mocks
                alerts_col.update_many.reset_mock()
                mock_send.reset_mock()
                
                # Test Case 2: Specific toggles disabled, overall enabled
                mock_merged.return_value = {
                    "email_enabled": True,
                    "email_nvr_offline": False,
                    "email_nvr_time_drift": True,
                    "email_recording_mismatch": False,
                    "email_channel_not_recording": True,
                    "smtp_to": ["admin@example.com"],
                    "alert_email_interval_seconds": 600,
                    "acknowledged_alert_email_enabled": False,
                }
                
                _process_alert_cycle_once()
                
                # nvr_offline and recording_expected_mismatch should be skipped.
                # nvr_time_drift and channel_not_recording should be sent.
                # Check update_many for skipped
                alerts_col.update_many.assert_any_call(
                    {"_id": {"$in": ["nvr_offline:1", "recording_expected_mismatch:1"]}},
                    {"$set": {"last_email_status": "skipped"}}
                )
                # Check update_many for sent
                alerts_col.update_many.assert_any_call(
                    {"_id": {"$in": ["nvr_time_drift:1", "channel_not_recording:1"]}},
                    {"$set": {"last_emailed_at": ANY, "last_email_status": "success"}}
                )
                mock_send.assert_called_once()
                
                # Reset mocks
                alerts_col.update_many.reset_mock()
                mock_send.reset_mock()
                
                # Test Case 3: email_channel_not_recording disabled, others enabled
                mock_merged.return_value = {
                    "email_enabled": True,
                    "email_nvr_offline": True,
                    "email_nvr_time_drift": True,
                    "email_recording_mismatch": True,
                    "email_channel_not_recording": False,
                    "smtp_to": ["admin@example.com"],
                    "alert_email_interval_seconds": 600,
                    "acknowledged_alert_email_enabled": False,
                }
                
                _process_alert_cycle_once()
                
                # channel_not_recording should be skipped.
                # All other 3 alerts should be sent.
                alerts_col.update_many.assert_any_call(
                    {"_id": {"$in": ["channel_not_recording:1"]}},
                    {"$set": {"last_email_status": "skipped"}}
                )
                alerts_col.update_many.assert_any_call(
                    {"_id": {"$in": ["nvr_offline:1", "nvr_time_drift:1", "recording_expected_mismatch:1"]}},
                    {"$set": {"last_emailed_at": ANY, "last_email_status": "success"}}
                )
                mock_send.assert_called_once()

    def test_build_current_alerts_time_drift(self):
        import time
        from datetime import datetime
        now = int(time.time())
        
        # Test case A: default tolerance (120 seconds)
        dt_120 = datetime.fromtimestamp(now - 120).isoformat()
        snapshot_120 = [{"ip": "192.168.1.10", "name": "Test NVR", "nvr_time": dt_120}]
        alerts_120 = _build_current_alerts(snapshot_120, {})
        self.assertNotIn("nvr_time_drift:192.168.1.10", alerts_120)
        
        dt_121 = datetime.fromtimestamp(now - 121).isoformat()
        snapshot_121 = [{"ip": "192.168.1.10", "name": "Test NVR", "nvr_time": dt_121}]
        alerts_121 = _build_current_alerts(snapshot_121, {})
        self.assertIn("nvr_time_drift:192.168.1.10", alerts_121)

        # Test case B: custom tolerance (60 seconds)
        dt_60 = datetime.fromtimestamp(now - 60).isoformat()
        snapshot_60 = [{"ip": "192.168.1.10", "name": "Test NVR", "nvr_time": dt_60}]
        alerts_60 = _build_current_alerts(snapshot_60, {"time_tolerance": 60})
        self.assertNotIn("nvr_time_drift:192.168.1.10", alerts_60)

        dt_61 = datetime.fromtimestamp(now - 61).isoformat()
        snapshot_61 = [{"ip": "192.168.1.10", "name": "Test NVR", "nvr_time": dt_61}]
        alerts_61 = _build_current_alerts(snapshot_61, {"time_tolerance": 60})
        self.assertIn("nvr_time_drift:192.168.1.10", alerts_61)

        # Test case C: custom tolerance (300 seconds)
        dt_300 = datetime.fromtimestamp(now - 300).isoformat()
        snapshot_300 = [{"ip": "192.168.1.10", "name": "Test NVR", "nvr_time": dt_300}]
        alerts_300 = _build_current_alerts(snapshot_300, {"time_tolerance": 300})
        self.assertNotIn("nvr_time_drift:192.168.1.10", alerts_300)

        dt_301 = datetime.fromtimestamp(now - 301).isoformat()
        snapshot_301 = [{"ip": "192.168.1.10", "name": "Test NVR", "nvr_time": dt_301}]
        alerts_301 = _build_current_alerts(snapshot_301, {"time_tolerance": 300})
        self.assertIn("nvr_time_drift:192.168.1.10", alerts_301)

    def test_build_current_alerts_flags_blue_recording_channels_as_not_motion(self):
        snapshot = [
            {
                "ip": "192.168.1.20",
                "name": "Test NVR",
                "status": "Online",
                "channel_statuses": [
                    {"channel": 1, "status": "motion"},
                    {"channel": 2, "status": "recording"},
                    {"channel": 3, "status": "not-recording"},
                ],
            }
        ]

        alerts = _build_current_alerts(snapshot, {})

        self.assertNotIn("channel_not_motion_recording:192.168.1.20:1", alerts)
        self.assertEqual(
            alerts["channel_not_motion_recording:192.168.1.20:2"]["message"],
            "Channel 2 is recording continuously instead of motion",
        )
        self.assertEqual(
            alerts["channel_not_recording:192.168.1.20:3"]["message"],
            "Channel 3 is not recording",
        )


if __name__ == "__main__":
    unittest.main()
