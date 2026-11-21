import os, sqlite3, subprocess

src = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Profile 1\Network\Cookies")
dst = r"C:\atagos-master\temp_cookies.db"

ps_script = f"""
$src = [System.IO.File]::Open('{src}', [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
$dst = [System.IO.File]::Create('{dst}')
$src.CopyTo($dst)
$src.Close()
$dst.Close()
"""

subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=True)

conn = sqlite3.connect(dst)
c = conn.cursor()
c.execute("SELECT host_key, name, length(encrypted_value) FROM cookies WHERE host_key LIKE '%goszakup%'")
rows = c.fetchall()
for r in rows:
    print(r)
conn.close()
os.remove(dst)
