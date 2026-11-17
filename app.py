import os
import json
import time
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import warnings

warnings.simplefilter("ignore")

app = Flask(__name__)
app.secret_key = os.urandom(24)

KEY_FILE = "license.key"
LIVE_FILE = "live.txt"
KEYS_JSON = "keys.json"

def load_keys_db():
    if not os.path.exists(KEYS_JSON):
        default_data = {"keys": {"KYU-VIP-2026": {"expires_ts": time.time() + 86400 * 30, "active": True}}}
        with open(KEYS_JSON, "w") as f:
            json.dump(default_data, f, indent=4)
    with open(KEYS_JSON, "r") as f:
        try:
            return json.load(f)
        except:
            return {"keys": {}}

def save_keys_db(data):
    with open(KEYS_JSON, "w") as f:
        json.dump(data, f, indent=4)

@app.route("/")
def index():
    if "key" not in session:
        return redirect(url_for("login"))
    return render_template("checker.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        key = request.form.get("key", "").strip()
        db = load_keys_db()
        if key in db.get("keys", {}):
            kdata = db["keys"][key]
            if kdata.get("active", True) and (kdata.get("expires_ts", 0) > time.time() or kdata.get("expires_ts") == 0):
                session["key"] = key
                return redirect(url_for("index"))
            else:
                error = "Key expired or disabled."
        else:
            error = "Invalid license key."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.pop("key", None)
    return redirect(url_for("login"))

@app.route("/api/check", methods=["POST"])
def api_check():
    if "key" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    
    data = request.get_json() or {}
    card_str = data.get("card", "").strip()
    
    if not card_str:
        return jsonify({"success": False, "error": "No card provided"})
    
    from ownmerch import parse_card_input, TokenManager, run_card, INLINE_BEARER_TOKEN
    
    try:
        card = parse_card_input(card_str)
        token_mgr = TokenManager(INLINE_BEARER_TOKEN)
        code, detail, meta = run_card(card, token_mgr, "US", None, "")
        
        if code == 0:
            with open(LIVE_FILE, "a", encoding="utf-8") as f:
                f.write(f"{card_str}|{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                
        return jsonify({
            "success": True,
            "code": code,
            "status": "LIVE" if code == 0 else "DEAD",
            "detail": detail,
            "meta": meta
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/admin")
def admin():
    db = load_keys_db()
    return render_template("admin.html", keys=db.get("keys", {}))

@app.route("/admin/add", methods=["POST"])
def admin_add():
    key = request.form.get("key", "").strip()
    try:
        days = int(request.form.get("days", 30))
    except ValueError:
        days = 30
    if key:
        db = load_keys_db()
        db["keys"][key] = {
            "expires_ts": time.time() + (86400 * days) if days > 0 else 0,
            "active": True
        }
        save_keys_db(db)
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
