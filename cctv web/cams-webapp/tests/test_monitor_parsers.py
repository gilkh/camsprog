import sys, os, unittest, json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.monitor import MonitorState


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


if __name__ == "__main__":
    unittest.main()
