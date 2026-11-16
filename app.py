import os
import json
import random
import string
import urllib.request
from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
DB_FILE = 'keys.json'
ADMIN_PASSWORD = "admin123"

TELEGRAM_BOT_TOKEN = "9198628207:AAH3PsBEh6S5Xo7SytUohMD3Of95d9eGTbQ"
TELEGRAM_CHAT_ID = "6516330870"

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
    """Nagbabalik ng Unix timestamp (seconds) sa UTC."""
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
        client_tz = data.get('timezone', 'UTC')

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
            if exp_ts:
                exp_label = f"{duration_val} {duration_unit} (UTC Timestamp: {exp_ts})"
            else:
                exp_label = "Lifetime"
            keys_formatted = "\n".join([f"<code>{k}</code>" for k in created_keys])
            tg_msg = (
                f"<b>🔑 NEW LICENSE KEY(S) GENERATED</b>\n\n"
                f"📦 <b>Quantity:</b> {len(created_keys)}\n"
                f"⏳ <b>Duration:</b> {exp_label}\n"
                f"🌍 <b>Creator Timezone:</b> {client_tz}\n\n"
                f"<b>Keys:</b>\n{keys_formatted}"
            )
            send_telegram_alert(tg_msg)

        return jsonify({
            "success": True,
            "message": f"{len(created_keys)} key(s) generated successfully!",
            "keys": created_keys
        })

@app.route('/api/keys/<path:key_id>', methods=['PUT', 'DELETE'])
def modify_key(key_id):
    keys_data = load_db()

    if key_id not in keys_data:
        return jsonify({"success": False, "message": "Key not found"}), 404

    if request.method == 'DELETE':
        del keys_data[key_id]
        save_db(keys_data)
        return jsonify({"success": True, "message": "Key deleted successfully"})

    elif request.method == 'PUT':
        data = request.get_json() or {}
        action = data.get('action')

        if action == 'reset_hwid':
            keys_data[key_id]['device_id'] = ""
            save_db(keys_data)
            return jsonify({"success": True, "message": "Device HWID reset successfully"})

        duration_val = data.get('duration_val', '1')
        duration_unit = data.get('duration_unit', 'lifetime')

        keys_data[key_id]['expires_ts'] = calculate_expiration_utc(duration_val, duration_unit)
        keys_data[key_id]['status'] = 'active'

        save_db(keys_data)
        return jsonify({"success": True, "message": "Key updated successfully"})

@app.route('/verify.php', methods=['POST'])
def verify_key():
    data = request.get_json() or {}
    incoming_key = str(data.get('key', '')).strip()
    device_id = str(data.get('device_id', '')).strip()

    if not incoming_key:
        return jsonify({"success": False, "message": "Empty key provided."})

    keys_data = load_db()

    if incoming_key not in keys_data:
        return jsonify({"success": False, "message": "Invalid license key."})

    key_info = keys_data[incoming_key]

    if key_info.get('status') != 'active':
        return jsonify({"success": False, "message": "License key is expired or inactive."})

    expires_ts = key_info.get('expires_ts')
    now_ts = int(datetime.now(timezone.utc).timestamp())

    if expires_ts and now_ts > expires_ts:
        key_info['status'] = 'expired'
        save_db(keys_data)
        return jsonify({"success": False, "message": "License key has expired."})

    stored_device = key_info.get('device_id', '')
    if not stored_device:
        key_info['device_id'] = device_id
        save_db(keys_data)
    elif stored_device != device_id:
        return jsonify({"success": False, "message": "Key is already bound to another PC/Device."})

    return jsonify({
        "success": True,
        "expires_ts": expires_ts
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
