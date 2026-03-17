import sys, re

filepath = r'c:\Users\user\Documents\GitHub\camsprog\cctv web\cams-webapp\app\monitor.py'

with open(filepath, 'r', encoding='utf-8') as f:
    code = f.read()

old_setup = """        try:
            nvr["channel_statuses"] = []
            if vendor == "Milesight":"""

new_setup = """        try:
            nvr["channel_statuses"] = []
            session = requests.Session()
            if vendor in ("Hikvision", "Uniview"):
                session.auth = HTTPDigestAuth(username, password)
            else:
                session.auth = (username, password)

            if vendor == "Milesight":"""

code = code.replace(old_setup, new_setup)

# We want to replace requests.get(..., auth=..., timeout=X) with session.get(..., timeout=X)
# in the context of _update_vendor_stats. 
# We can match exactly the patterns found.
# e.g., requests.get(time_url, auth=(username, password), timeout=5)
def repl(m):
    url_expr = m.group(1)
    timeout_val = m.group(2)
    return f"session.get({url_expr}, timeout={timeout_val})"

# Regex looking for requests.get(..., auth=..., timeout=...)
# Group 1: anything up to ', auth'
# Group 2: the timeout digits
# The regex:
pattern = r"requests\.get\((.*?), auth=(?:HTTPDigestAuth\([^)]+\)|\([^)]+\)), timeout=(\d+)\)"
code = re.sub(pattern, repl, code)

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(code)

print("Done")
