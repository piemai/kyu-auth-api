import os
import json
import time
import queue
import threading
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
import warnings

warnings.simplefilter("ignore")

app = Flask(__name__)
app.secret_key = os.urandom(24)

KEY_FILE = "license.key"
LIVE_FILE = "live.txt"
KEYS_JSON = "keys.json"

# Global Queue para sa SSE Mass/Single Checker streaming
checker_queue = queue.Queue()
checker_active = False

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

# Background checker thread para sa SSE streaming
def background_checker_task(card_lines, proxies_raw):
    global checker_active
    from ownmerch import parse_card_input, TokenManager, run_card, ProxyPool, INLINE_BEARER_TOKEN, load_proxies_from_file
    
    proxy_list = []
    if proxies_raw:
        for p in proxies_raw.splitlines():
            p = p.strip()
            if p:
                if "://" in p:
                    proxy_list.append(p)
                else:
                    parts = p.split(":")
                    if len(parts) == 2:
                        proxy_list.append(f"http://{p}")
                    elif len(parts) == 4:
                        h, po, u, pw = parts
                        proxy_list.append(f"http://{u}:{pw}@{h}:{po}")
    pool = ProxyPool(proxy_list)
    token_mgr = TokenManager(INLINE_BEARER_TOKEN, pool)

    total = len(card_lines)
    live_count = 0
    dead_count = 0
    error_count = 0
    checked_count = 0

    for line in card_lines:
        if not checker_active:
            break
        line = line.strip()
        if not line:
            continue
        
        try:
            card = parse_card_input(line)
        except ValueError as e:
            checked_count += 1
            error_count += 1
            checker_queue.put({
                "card": line,
                "code": 2,
                "status": "ERROR",
                "detail": str(e),
                "checked": checked_count,
                "live": live_count,
                "dead": dead_count,
                "error": error_count
            })
            continue

        code, detail, meta = run_card(card, token_mgr, "US", pool, "")
        checked_count += 1
        status_str = "LIVE"
        if code == 0:
            live_count += 1
            with open(LIVE_FILE, "a", encoding="utf-8") as f:
                f.write(f"{line}|{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        elif code == 1:
            dead_count += 1
            status_str = "DEAD"
        else:
            error_count += 1
            status_str = "ERROR"

        checker_queue.put({
            "card": line,
            "code": code,
            "status": status_str,
            "detail": f"{detail} | [{meta.get('brand')} - {meta.get('bank')} - {meta.get('country')}]",
            "checked": checked_count,
            "live": live_count,
            "dead": dead_count,
            "error": error_count
        })

    checker_queue.put({"finished": True})
    checker_active = False

@app.route("/api/checker/start", methods=["POST"])
def api_checker_start():
    global checker_active
    if "key" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    
    data = request.get_json() or {}
    cards_text = data.get("cards", "")
    proxies_text = data.get("proxies", "")
    
    card_lines = [c.strip() for c in cards_text.splitlines() if c.strip()]
    if not card_lines:
        return jsonify({"success": False, "error": "No cards provided"})
    
    # I-clear ang queue
    while not checker_queue.empty():
        try:
            checker_queue.get_nowait()
        except queue.Empty:
            break

    checker_active = True
    threading.Thread(target=background_checker_task, args=(card_lines, proxies_text), daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/checker/stop", methods=["POST"])
def api_checker_stop():
    global checker_active
    checker_active = False
    return jsonify({"success": True})

@app.route("/api/checker/stream")
def api_checker_stream():
    if "key" not in session:
        return "Unauthorized", 401
    
    def generate():
        while True:
            try:
                item = checker_queue.get(timeout=20)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("finished"):
                    break
            except queue.Empty:
                yield "data: {"ping": true}\n\n"
                
    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
