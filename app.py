import os
import json
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)
DB_FILE = 'keys.json'

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
