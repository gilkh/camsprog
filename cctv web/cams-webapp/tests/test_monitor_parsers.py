import sys, os, unittest, json
from unittest.mock import patch, mock_open, MagicMock, call
import subprocess

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.monitor import MonitorState, load_nvrs_from_config, ping_ip


class TestMilesightIpcstatusDetails(unittest.TestCase):
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_real_milesight_format(self):
        """Test actual Milesight ipcstatus format with double-bracket connectStatus."""
        text = (
            "record[0]=1\nalarm[0]=0\nchnid[0]=0\n"
            "connectStatus[0][0]=1\n"
            "record[1]=1\nalarm[1]=0\nchnid[1]=1\n"
            "connectStatus[1][0]=1\n"
            "record[2]=0\nalarm[2]=0\nchnid[2]=2\n"
            "connectStatus[2][0]=1\n"
            "record[3]=1\nalarm[3]=0\nchnid[3]=3\n"
            "connectStatus[3][0]=0\n"
        )
        conn, rec = self.m._parse_milesight_ipcstatus_details(text)
        # Channels 0,1,2 connected; channel 3 not connected (connectStatus=0)
        self.assertEqual(conn, {0, 1, 2})
        # Channels 0,1,3 recording; channel 2 not recording
        self.assertEqual(rec, {0, 1, 3})
        # Intersection: recording AND connected = channels 0,1
        self.assertEqual(conn & rec, {0, 1})

    def test_dot_attr_format(self):
        """Test ipc[idx].attr=val format."""
        text = (
            "ipc[0].status=online\n"
            "ipc[0].record=1\n"
            "ipc[1].status=offline\n"
            "ipc[1].record=1\n"
        )
        conn, rec = self.m._parse_milesight_ipcstatus_details(text)
        self.assertIn(0, conn)
        self.assertNotIn(1, conn)
        self.assertEqual(rec, {0, 1})

    def test_no_connect_info_assumes_connected(self):
        """If no connectStatus info, assume all channels are connected."""
        text = (
            "record[0]=1\nchnid[0]=0\n"
            "record[1]=0\nchnid[1]=1\n"
        )
        conn, rec = self.m._parse_milesight_ipcstatus_details(text)
        self.assertEqual(conn, {0, 1})
        self.assertEqual(rec, {0})

    def test_empty_text(self):
        conn, rec = self.m._parse_milesight_ipcstatus_details("")
        self.assertEqual(conn, set())
        self.assertEqual(rec, set())


class TestHikvisionRecordingConfig(unittest.TestCase):
    """Test Hikvision recording configuration parser using DefaultRecordingMode."""
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_default_recording_mode_cmr(self):
        """CMR (Continuous Manual Recording) should count as configured to record."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <TrackList>
            <Track>
                <id>101</id>
                <Channel>101</Channel>
                <Enable>false</Enable>
                <DefaultRecordingMode>CMR</DefaultRecordingMode>
                <SrcDescriptor><SrcChannel>1</SrcChannel></SrcDescriptor>
            </Track>
            <Track>
                <id>201</id>
                <Channel>201</Channel>
                <Enable>false</Enable>
                <DefaultRecordingMode>CMR</DefaultRecordingMode>
                <SrcDescriptor><SrcChannel>2</SrcChannel></SrcDescriptor>
            </Track>
            <Track>
                <id>301</id>
                <Channel>301</Channel>
                <Enable>false</Enable>
                <DefaultRecordingMode>OFF</DefaultRecordingMode>
                <SrcDescriptor><SrcChannel>3</SrcChannel></SrcDescriptor>
            </Track>
        </TrackList>"""
        configured = self.m._parse_hikvision_record_tracks_configured_channels(xml)
        self.assertIn(1, configured)
        self.assertIn(2, configured)
        self.assertNotIn(3, configured)
        self.assertEqual(len(configured), 2)

    def test_motion_recording_mode(self):
        """MR (Motion Recording) should count as configured to record."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <TrackList>
            <Track>
                <id>101</id>
                <Enable>false</Enable>
                <DefaultRecordingMode>MR</DefaultRecordingMode>
                <SrcDescriptor><SrcChannel>1</SrcChannel></SrcDescriptor>
            </Track>
        </TrackList>"""
        configured = self.m._parse_hikvision_record_tracks_configured_channels(xml)
        self.assertIn(1, configured)

    def test_fallback_to_enable_flag(self):
        """If no DefaultRecordingMode, fall back to Enable flag."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <TrackList>
            <Track>
                <SrcChannel>1</SrcChannel>
                <Enable>true</Enable>
            </Track>
            <Track>
                <SrcChannel>2</SrcChannel>
                <Enable>false</Enable>
            </Track>
        </TrackList>"""
        configured = self.m._parse_hikvision_record_tracks_configured_channels(xml)
        self.assertIn(1, configured)
        self.assertNotIn(2, configured)

    def test_id_fallback_to_track_id_div_100(self):
        """If no SrcChannel, derive physical channel from id/100."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <TrackList>
            <Track>
                <id>201</id>
                <DefaultRecordingMode>CMR</DefaultRecordingMode>
            </Track>
        </TrackList>"""
        configured = self.m._parse_hikvision_record_tracks_configured_channels(xml)
        self.assertIn(2, configured)

    def test_intersection_with_connected(self):
        """recording_count = connected cameras ∩ configured-to-record channels."""
        connected_ids = {1, 3, 5}
        # Channels 1,2,3 configured, but channel 2 camera is offline
        rec_configured = {1, 2, 3}
        self.assertEqual(len(connected_ids & rec_configured), 2)  # channels 1,3

    def test_channel_modes(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <TrackList>
            <Track>
                <id>101</id>
                <DefaultRecordingMode>MR</DefaultRecordingMode>
                <SrcDescriptor><SrcChannel>1</SrcChannel></SrcDescriptor>
            </Track>
            <Track>
                <id>201</id>
                <DefaultRecordingMode>CMR</DefaultRecordingMode>
                <SrcDescriptor><SrcChannel>2</SrcChannel></SrcDescriptor>
            </Track>
            <Track>
                <id>301</id>
                <DefaultRecordingMode>OFF</DefaultRecordingMode>
                <SrcDescriptor><SrcChannel>3</SrcChannel></SrcDescriptor>
            </Track>
        </TrackList>"""
        modes = self.m._parse_hikvision_record_tracks_channel_modes(xml)
        self.assertEqual(modes[1], "motion")
        self.assertEqual(modes[2], "recording")
        self.assertEqual(modes[3], "not-recording")


class TestHikvisionTimeFormatting(unittest.TestCase):
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_iso_with_timezone_trims_to_minute(self):
        raw = "2026-03-18T10:48:49+02:00"
        self.assertEqual(self.m._format_hikvision_time_value(raw), "2026-03-18 10:48")

    def test_space_separated_with_seconds_trims_to_minute(self):
        raw = "2026-03-18 10:48:49"
        self.assertEqual(self.m._format_hikvision_time_value(raw), "2026-03-18 10:48")


class TestHikvisionInputProxy(unittest.TestCase):
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_inputproxy_connected_ids(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <InputProxyChannelStatusList>
            <InputProxyChannelStatus>
                <id>1</id>
                <online>true</online>
            </InputProxyChannelStatus>
            <InputProxyChannelStatus>
                <id>2</id>
                <online>false</online>
            </InputProxyChannelStatus>
            <InputProxyChannelStatus>
                <id>3</id>
                <online>true</online>
            </InputProxyChannelStatus>
        </InputProxyChannelStatusList>"""
        ids = self.m._parse_hikvision_inputproxy_channels_status_connected_ids(xml)
        self.assertIn('1', ids)
        self.assertNotIn('2', ids)
        self.assertIn('3', ids)


class TestHikvisionMotionDetection(unittest.TestCase):
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_motion_detection_enabled(self):
        xml = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
        <MotionDetection>
            <enabled>true</enabled>
        </MotionDetection>"""
        self.assertTrue(self.m._parse_hikvision_motion_detection_enabled(xml))


class TestMilesightIpclistConnectedIds(unittest.TestCase):
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_json_online_field(self):
        data = {"list": [
            {"id": 0, "online": "1"},
            {"id": 1, "online": "0"},
            {"id": 2, "online": "1"},
        ]}
        ids = self.m._parse_milesight_ipclist_connected_ids(json.dumps(data))
        self.assertEqual(ids, {0, 2})

    def test_connectstate_field(self):
        """Test actual Milesight NVR format with connectState and state fields."""
        data = {"cnt": 3, "list": [
            {"id": 0, "state": 2, "connectState": 1},
            {"id": 1, "state": 2, "connectState": 1},
            {"id": 2, "state": 0, "connectState": 0},
        ]}
        ids = self.m._parse_milesight_ipclist_connected_ids(json.dumps(data))
        self.assertEqual(ids, {0, 1})

    def test_state_field_only(self):
        """state=2 means connected on Milesight NVRs."""
        data = {"list": [
            {"id": 0, "state": 2},
            {"id": 1, "state": 0},
        ]}
        ids = self.m._parse_milesight_ipclist_connected_ids(json.dumps(data))
        self.assertEqual(ids, {0})


class TestMilesightMotionConfig(unittest.TestCase):
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_motion_config_indices(self):
        text = (
            "camera[0].motion=1\n"
            "camera[1].motion=0\n"
            "camera[2].md_enable=1\n"
        )
        ids = self.m._parse_milesight_motion_config_indices(text)
        self.assertEqual(ids, {0, 2})

    def test_motion_config_simple_bracket_format(self):
        text = (
            "motion[0]=1\n"
            "md[1]=0\n"
            "motion[2]=true\n"
        )
        ids = self.m._parse_milesight_motion_config_indices(text)
        self.assertEqual(ids, {0, 2})


class TestMilesightChannelIdAlignment(unittest.TestCase):
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_aligns_zero_based_config_to_one_based_connected(self):
        connected = {1, 2, 3, 4, 5, 6, 7}
        configured = {0, 1, 2, 3, 4, 5, 6}
        aligned = self.m._align_channel_id_set(connected, configured)
        self.assertEqual(aligned, {1, 2, 3, 4, 5, 6, 7})


class TestUniviewParsers(unittest.TestCase):
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_uniview_detailinfos_connected_ids(self):
        text = json.dumps({
            "Response": {
                "Data": {
                    "DetailInfos": [
                        {"ChannelID": 1, "Status": 1},
                        {"ChannelID": 2, "Status": 0},
                        {"ChannelID": 3, "Online": True},
                        {"ChannelID": 4, "ConnectStatus": "1"},
                    ]
                }
            }
        })
        ids = self.m._parse_uniview_channel_detail_infos_connected_ids(text)
        self.assertEqual(ids, {1, 3, 4})

    def test_uniview_detailinfos_camera_count_from_status(self):
        text = json.dumps({
            "Response": {
                "Data": {
                    "DetailInfos": [
                        {"ChannelID": 1, "Status": 1},
                        {"ChannelID": 2, "Status": 0},
                        {"ChannelID": 3, "Status": 1},
                    ]
                }
            }
        })
        cc = self.m._parse_uniview_channel_detail_infos_camera_count(text)
        self.assertEqual(cc, 2)

    def test_uniview_detailinfos_camera_count_nums_fallback(self):
        text = json.dumps({"Response": {"Data": {"Nums": 8}}})
        cc = self.m._parse_uniview_channel_detail_infos_camera_count(text)
        self.assertEqual(cc, 8)

    def test_uniview_time_value_from_xml(self):
        xml = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
        <Time><localTime>2026-03-18T10:18:19+02:00</localTime></Time>"""
        self.assertEqual(
            self.m._parse_uniview_time_value(xml),
            "2026-03-18T10:18:19+02:00",
        )

    def test_uniview_time_value_from_json(self):
        text = json.dumps({"Response": {"Data": {"CurrentDeviceTime": "2026-03-18 10:18:19"}}})
        self.assertEqual(self.m._parse_uniview_time_value(text), "2026-03-18 10:18:19")

    def test_uniview_time_value_from_lapi_epoch(self):
        text = json.dumps({
            "Response": {
                "Data": {
                    "TimeZone": "GMT+02:00",
                    "DeviceTime": 1773822563,
                }
            }
        })
        self.assertEqual(self.m._parse_uniview_time_value(text), "2026-03-18 10:29:23")

    def test_uniview_lapi_record_schedule_mode_recording(self):
        text = json.dumps({
            "Response": {
                "Data": {
                    "Enabled": 1,
                    "WeekPlan": {
                        "Days": [
                            {
                                "TimeSectionInfos": [
                                    {"Begin": "00:00:00", "End": "24:00:00", "ArmingType": 0}
                                ]
                            }
                        ]
                    }
                }
            }
        })
        self.assertEqual(self.m._parse_uniview_lapi_record_schedule_mode(text), "recording")

    def test_uniview_lapi_record_schedule_mode_motion(self):
        text = json.dumps({
            "Response": {
                "Data": {
                    "Enabled": 1,
                    "WeekPlan": {
                        "Days": [
                            {
                                "TimeSectionInfos": [
                                    {"Begin": "08:00:00", "End": "09:00:00", "ArmingType": 2}
                                ]
                            }
                        ]
                    }
                }
            }
        })
        self.assertEqual(self.m._parse_uniview_lapi_record_schedule_mode(text), "motion")

    def test_uniview_lapi_record_schedule_mode_disabled(self):
        text = json.dumps({"Response": {"Data": {"Enabled": 0}}})
        self.assertEqual(self.m._parse_uniview_lapi_record_schedule_mode(text), "not-recording")


class TestHikvisionInputProxyModes(unittest.TestCase):
    def setUp(self):
        self.m = MonitorState.__new__(MonitorState)

    def test_inputproxy_channel_modes(self):
        xml = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
        <InputProxyChannelStatusList>
            <InputProxyChannelStatus>
                <id>1</id>
                <online>true</online>
                <recording>true</recording>
            </InputProxyChannelStatus>
            <InputProxyChannelStatus>
                <id>2</id>
                <online>true</online>
                <recording>false</recording>
            </InputProxyChannelStatus>
            <InputProxyChannelStatus>
                <id>3</id>
                <online>false</online>
                <recording>false</recording>
            </InputProxyChannelStatus>
        </InputProxyChannelStatusList>"""
        modes = self.m._parse_hikvision_inputproxy_channels_status_channel_modes(xml)
        self.assertEqual(modes[1], "recording")
        self.assertEqual(modes[2], "not-recording")
        self.assertEqual(modes[3], "no-camera")


class TestLoadNvrsFromConfig(unittest.TestCase):
    """Test loading NVRs from config.json with various structures."""

    @patch('builtins.open', new_callable=mock_open, read_data='{"nvrs": [{"name": "NVR1", "ip": "192.168.1.100"}]}')
    @patch('os.path.exists', return_value=True)
    def test_load_nvrs_from_top_level_nvrs(self, mock_exists, mock_file):
        """Should load NVRs from top-level 'nvrs' key."""
        result = load_nvrs_from_config()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "NVR1")
        self.assertEqual(result[0]["ip"], "192.168.1.100")

    @patch('builtins.open', new_callable=mock_open, read_data='{"config": {"nvrs": [{"name": "NVR2", "ip": "192.168.1.101"}]}}')
    @patch('os.path.exists', return_value=True)
    def test_load_nvrs_from_nested_config_nvrs(self, mock_exists, mock_file):
        """Should fallback to 'config.nvrs' if top-level 'nvrs' not present."""
        result = load_nvrs_from_config()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "NVR2")

    @patch('os.path.exists', return_value=False)
    def test_load_nvrs_config_file_not_exists(self, mock_exists):
        """Should return empty list when config.json doesn't exist."""
        result = load_nvrs_from_config()
        self.assertEqual(result, [])

    @patch('builtins.open', new_callable=mock_open, read_data='invalid json')
    @patch('os.path.exists', return_value=True)
    def test_load_nvrs_malformed_json(self, mock_exists, mock_file):
        """Should return empty list when JSON is malformed."""
        result = load_nvrs_from_config()
        self.assertEqual(result, [])

    @patch('builtins.open', new_callable=mock_open, read_data='{"other_key": "value"}')
    @patch('os.path.exists', return_value=True)
    def test_load_nvrs_no_nvrs_key(self, mock_exists, mock_file):
        """Should return empty list when 'nvrs' key is missing."""
        result = load_nvrs_from_config()
        self.assertEqual(result, [])


class TestPingIp(unittest.TestCase):
    """Test IP ping functionality."""

    @patch('subprocess.run')
    def test_ping_ip_online(self, mock_run):
        """Should return True when ping succeeds (returncode 0)."""
        mock_run.return_value = MagicMock(returncode=0)
        result = ping_ip("192.168.1.1")
        self.assertTrue(result)
        mock_run.assert_called_once()

    @patch('subprocess.run')
    def test_ping_ip_offline(self, mock_run):
        """Should return False when ping fails (returncode non-zero)."""
        mock_run.return_value = MagicMock(returncode=1)
        result = ping_ip("192.168.1.999")
        self.assertFalse(result)

    @patch('subprocess.run', side_effect=Exception("Network error"))
    def test_ping_ip_exception(self, mock_run):
        """Should return False when subprocess raises exception."""
        result = ping_ip("invalid-ip")
        self.assertFalse(result)

    @patch('subprocess.run')
    def test_ping_ip_custom_timeout(self, mock_run):
        """Should use custom timeout value in ping command."""
        mock_run.return_value = MagicMock(returncode=0)
        ping_ip("192.168.1.1", timeout_ms=2000)
        # Verify timeout is passed to ping command
        call_args = mock_run.call_args[0][0]
        self.assertIn("2000", call_args)


class TestMonitorHeartbeatGapHandling(unittest.TestCase):
    """Regression tests for restart-gap classification between offline and unknown."""

    @patch("app.monitor.time.time", return_value=2600)
    @patch.object(MonitorState, "_read_heartbeat", return_value=2000)
    @patch.object(MonitorState, "_read_first_heartbeat", return_value=1200)
    @patch.object(MonitorState, "_write_first_heartbeat")
    @patch.object(MonitorState, "_write_heartbeat")
    @patch.object(MonitorState, "_record_unknown_gap")
    @patch.object(MonitorState, "_record_offline_interval")
    def test_reboot_gap_splits_offline_and_marks_unknown(
        self,
        mock_record_offline,
        mock_record_unknown,
        mock_write_hb,
        mock_write_first,
        mock_read_first,
        mock_read_hb,
        mock_time,
    ):
        monitor = MonitorState(poll_interval=60, db=None)
        monitor.nvrs = [
            {
                "name": "NVR1",
                "ip": "192.168.1.10",
                "status": "Offline",
                "offline_since": 1000,
                "nvr_time": "Offline",
                "camera_count": "Offline",
                "recording_count": "Offline",
                "channel_statuses": [1],
            }
        ]

        monitor._init_heartbeat()

        mock_record_unknown.assert_called_once_with(2000, 2600)
        mock_record_offline.assert_called_once_with("192.168.1.10", 1000, 2000)
        self.assertEqual(monitor.nvrs[0]["status"], "Unknown")
        self.assertIsNone(monitor.nvrs[0]["offline_since"])
        self.assertEqual(monitor.nvrs[0]["nvr_time"], "Unknown")
        self.assertEqual(monitor.nvrs[0]["camera_count"], "Unknown")
        self.assertEqual(monitor.nvrs[0]["recording_count"], "Unknown")
        self.assertEqual(monitor.nvrs[0]["channel_statuses"], [])
        mock_write_hb.assert_called_once_with(2600)
        mock_write_first.assert_not_called()

    @patch("app.monitor.time.time", return_value=2100)
    @patch.object(MonitorState, "_read_heartbeat", return_value=2000)
    @patch.object(MonitorState, "_read_first_heartbeat", return_value=1200)
    @patch.object(MonitorState, "_write_heartbeat")
    @patch.object(MonitorState, "_record_unknown_gap")
    @patch.object(MonitorState, "_record_offline_interval")
    def test_no_restart_gap_keeps_existing_offline_interval(
        self,
        mock_record_offline,
        mock_record_unknown,
        mock_write_hb,
        mock_read_first,
        mock_read_hb,
        mock_time,
    ):
        monitor = MonitorState(poll_interval=60, db=None)
        monitor.nvrs = [
            {
                "name": "NVR1",
                "ip": "192.168.1.10",
                "status": "Offline",
                "offline_since": 1000,
            }
        ]

        monitor._init_heartbeat()

        mock_record_unknown.assert_not_called()
        mock_record_offline.assert_not_called()
        self.assertEqual(monitor.nvrs[0]["status"], "Offline")
        self.assertEqual(monitor.nvrs[0]["offline_since"], 1000)
        mock_write_hb.assert_called_once_with(2100)


class TestMonitorStateAddOrUpdateNvr(unittest.TestCase):
    """Test adding and updating NVRs with field normalization."""

    def setUp(self):
        self.monitor = MonitorState(poll_interval=60, db=None)
        self.monitor.nvrs = []

    @patch.object(MonitorState, '_write_back')
    def test_add_nvr_with_vendor_normalization(self, mock_write):
        """Should normalize vendor type names (e.g., 'hickvision' -> 'Hikvision')."""
        nvr_data = {
            "name": "Test NVR",
            "ip": "192.168.1.100",
            "type": "hickvision",
            "username": "admin",
            "password": "pass123"
        }
        result = self.monitor.add_or_update_nvr(nvr_data)
        self.assertEqual(result["type"], "Hikvision")
        mock_write.assert_called_once()

    @patch.object(MonitorState, '_write_back')
    def test_add_nvr_milesight_normalization(self, mock_write):
        """Should normalize 'mileSight' to 'Milesight'."""
        nvr_data = {
            "name": "Test NVR",
            "ip": "192.168.1.101",
            "type": "mileSight"
        }
        result = self.monitor.add_or_update_nvr(nvr_data)
        self.assertEqual(result["type"], "Milesight")

    @patch.object(MonitorState, '_write_back')
    def test_add_nvr_missing_required_field(self, mock_write):
        """Should raise ValueError when required field is missing."""
        nvr_data = {"name": "Test NVR"}  # Missing 'ip'
        with self.assertRaises(ValueError) as context:
            self.monitor.add_or_update_nvr(nvr_data)
        self.assertIn("ip", str(context.exception))

    @patch.object(MonitorState, '_write_back')
    def test_update_existing_nvr(self, mock_write):
        """Should update existing NVR when IP matches."""
        self.monitor.nvrs = [
            {"name": "Old Name", "ip": "192.168.1.100", "type": "Hikvision"}
        ]
        nvr_data = {
            "name": "New Name",
            "ip": "192.168.1.100",
            "type": "Hikvision"
        }
        result = self.monitor.add_or_update_nvr(nvr_data)
        self.assertEqual(len(self.monitor.nvrs), 1)
        self.assertEqual(self.monitor.nvrs[0]["name"], "New Name")

    @patch.object(MonitorState, '_write_back')
    def test_add_nvr_sets_default_fields(self, mock_write):
        """Should set default fields for new NVR."""
        nvr_data = {"name": "Test", "ip": "192.168.1.100"}
        result = self.monitor.add_or_update_nvr(nvr_data)
        self.assertEqual(result["status"], "Unknown")
        self.assertIsNone(result["last_online"])
        self.assertEqual(result["camera_count"], "Unknown")


class TestMonitorStateDeleteNvr(unittest.TestCase):
    """Test deleting NVRs by IP."""

    def setUp(self):
        self.monitor = MonitorState(poll_interval=60, db=None)
        self.monitor.nvrs = [
            {"name": "NVR1", "ip": "192.168.1.100"},
            {"name": "NVR2", "ip": "192.168.1.101"},
        ]

    @patch.object(MonitorState, '_write_back')
    def test_delete_nvr_by_ip(self, mock_write):
        """Should delete NVR by IP and return True."""
        result = self.monitor.delete_nvr("192.168.1.100")
        self.assertTrue(result)
        self.assertEqual(len(self.monitor.nvrs), 1)
        self.assertEqual(self.monitor.nvrs[0]["ip"], "192.168.1.101")
        mock_write.assert_called_once()

    @patch.object(MonitorState, '_write_back')
    def test_delete_nvr_not_found(self, mock_write):
        """Should return False when IP not found."""
        result = self.monitor.delete_nvr("192.168.1.999")
        self.assertFalse(result)
        self.assertEqual(len(self.monitor.nvrs), 2)
        mock_write.assert_not_called()

    @patch.object(MonitorState, '_write_back')
    def test_delete_nvr_empty_ip(self, mock_write):
        """Should return False when IP is empty."""
        result = self.monitor.delete_nvr("")
        self.assertFalse(result)
        mock_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
