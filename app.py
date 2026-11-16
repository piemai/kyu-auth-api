import os
import json
import random
import string
from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta

app = Flask(__name__)
DB_FILE = 'keys.json'
ADMIN_PASSWORD = "admin123"

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

def calculate_expiration(amount, unit):
    if not amount or unit == 'lifetime':
        return ""
    try:
        val = int(amount)
        now = datetime.now()
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
            return ""
        return exp.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ""

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
        now = datetime.now()
        updated = False

        for key, info in keys_data.items():
            expires_at = info.get('expires_at', '')
            status = info.get('status', 'active')

            # Auto-check kung expired na
            if expires_at and status == 'active':
                try:
                    exp_time = datetime.strptime(expires_at, '%Y-%m-%d %H:%M:%S')
                    if now > exp_time:
                        status = 'expired'
                        keys_data[key]['status'] = 'expired'
                        updated = True
                except Exception:
                    pass

            result.append({
                "key": key,
                "expires_at": expires_at,
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

        # Gumawa ng random key kung walang binigay
        if not custom_key:
            part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            part2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            custom_key = f"KYU-{part1}-{part2}"

        if custom_key in keys_data:
            return jsonify({"success": False, "message": "Key already exists!"})

        formatted_expire = calculate_expiration(duration_val, duration_unit)

        keys_data[custom_key] = {
            "expires_at": formatted_expire,
            "status": "active",
            "device_id": ""
        }
        save_db(keys_data)
        return jsonify({"success": True, "message": "Key generated successfully", "key": custom_key})

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

        keys_data[key_id]['expires_at'] = calculate_expiration(duration_val, duration_unit)
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

    expires_at = key_info.get('expires_at', '')
    if expires_at:
        try:
            exp_time = datetime.strptime(expires_at, '%Y-%m-%d %H:%M:%S')
            if datetime.now() > exp_time:
                key_info['status'] = 'expired'
                save_db(keys_data)
                return jsonify({"success": False, "message": "License key has expired."})
        except Exception:
            pass

    stored_device = key_info.get('device_id', '')
    if not stored_device:
        key_info['device_id'] = device_id
        save_db(keys_data)
    elif stored_device != device_id:
        return jsonify({"success": False, "message": "Key is already bound to another PC/Device."})

    return jsonify({"success": True, "expires_at": expires_at or "Lifetime"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
