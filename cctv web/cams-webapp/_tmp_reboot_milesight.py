import requests
import sys

sys.path.append('c:/Users/user/Documents/GitHub/camsprog/cctv web/cams-webapp')
from app.monitor import MonitorState

ip = '192.168.18.23'
username = 'admin'
password = 'Patgil12'

m = MonitorState()
s = requests.Session()

login_ok = m._milesight_web_login(s, ip, username, password, timeout=8)
print('login_ok', login_ok)
if not login_ok:
    raise SystemExit(2)

r1 = m._milesight_web_get(s, ip, username, password, '/cgi/main/1032', timeout=8)
print('get_1032', r1.status_code, (r1.text or '')[:200])

r2 = m._milesight_web_post_json(s, ip, username, password, '/cgi/main/1004', payload={}, timeout=8)
print('post_1004', r2.status_code, (r2.text or '')[:400])
