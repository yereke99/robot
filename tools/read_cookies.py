import os, shutil, sqlite3

user_data = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
for root, dirs, files in os.walk(user_data):
    if "Cookies" in files:
        cookie_file = os.path.join(root, "Cookies")
        temp_file = os.path.join(r"C:\atagos-master", "temp_cookies.db")
        try:
            shutil.copyfile(cookie_file, temp_file)
            conn = sqlite3.connect(temp_file)
            c = conn.cursor()
            c.execute("SELECT host_key, name, value, length(encrypted_value) FROM cookies WHERE host_key LIKE '%goszakup%'")
            rows = c.fetchall()
            if rows:
                print("Found in:", cookie_file)
                for r in rows:
                    print(" ", r[0], r[1], "val_len=", len(r[2]), "enc_len=", r[3])
            conn.close()
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception as e:
            print("Err:", e)
