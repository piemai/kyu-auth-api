import os
import json
from flask import Flask, request, jsonify, render_template
from datetime import datetime

app = Flask(__name__)
DB_FILE = 'keys.json'
ADMIN_PASSWORD = "admin123"  # Pwede mong palitan ang password na ito

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

# --- FRONTEND ROUTE ---
@app.route('/')
def home():
    return render_template('index.html')

# --- ADMIN API ROUTES ---
@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json() or {}
    if data.get('password') == ADMIN_PASSWORD:
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Maling password!"})

@app.route('/api/keys', methods=['GET', 'POST'])
def handle_keys():
    keys_data = load_db()

    if request.method == 'GET':
        # I-format ang data para madaling basahin sa admin panel table
        result = []
        for key, info in keys_data.items():
            result.append({
                "key": key,
                "owner": info.get('owner', 'Walang Pangalan'),
                "expire": info.get('expires_at', ''),
                "status": info.get('status', 'active'),
                "device_id": info.get('device_id', '')
            })
        return jsonify({"success": True, "keys": result})

    elif request.method == 'POST':
        data = request.get_json() or {}
        owner = data.get('owner', 'Walang Pangalan')
        expire = data.get('expire', '') # Format: YYYY-MM-DD o YYYY-MM-DD HH:MM:SS
        custom_key = data.get('key')

        # Kung may expiration date, lagyan natin ng default time na 23:59:59 kung date lang binigay
        formatted_expire = ""
        if expire:
            if len(expire) == 10:  # YYYY-MM-DD format
                formatted_expire = f"{expire} 23:59:59"
            else:
                formatted_expire = expire

        # Gumawa ng random key kung walang binigay
        if not custom_key:
            import random
            import string
            part1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            part2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            custom_key = f"KEY-{part1}-{part2}"

        keys_data[custom_key] = {
            "owner": owner,
            "expires_at": formatted_expire,
            "status": "active",
            "device_id": ""
        }
        save_db(keys_data)
        return jsonify({"success": True, "message": "Key created successfully", "key": custom_key})

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
        owner = data.get('owner')
        expire = data.get('expire')
        status = data.get('status')

        if owner is not None:
            keys_data[key_id]['owner'] = owner
        if expire is not None:
            if len(expire) == 10:
                keys_data[key_id]['expires_at'] = f"{expire} 23:59:59"
            else:
                keys_data[key_id]['expires_at'] = expire
        if status is not None:
            keys_data[key_id]['status'] = status

        save_db(keys_data)
        return jsonify({"success": True, "message": "Key updated successfully"})

# --- ORIGINAL VERIFY ROUTE ---
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

    # Check expiration time if not lifetime
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

    # Hardware ID Binding
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
