#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import io
import json
import logging
import os
import re
import sys
import time
import hashlib
import html
import threading
import fcntl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.parse import quote as url_quote

import requests
from bakong_khqr import KHQR

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID   = 5002402843
EXTRA_ADMIN_IDS: set = set()
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
KHMER_MESSAGE = "ជ្រើសរើស Account ដើម្បីបញ្ជាទិញ"

def is_admin(uid):
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return False
    return uid_int == ADMIN_ID or uid_int in EXTRA_ADMIN_IDS

# ── Bot API ───────────────────────────────────────────────────────────────────
BOT_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"

# ── Thread pools ─────────────────────────────────────────────────────────────
worker_pool    = ThreadPoolExecutor(max_workers=16)
background_pool = ThreadPoolExecutor(max_workers=8)
_data_lock     = threading.RLock()

# HTTP session — used for Telegram Bot API, Neon DB, and Bakong API
http = requests.Session()
http.headers.update({'Connection': 'keep-alive'})
adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50)
http.mount('https://', adapter)
http.mount('http://', adapter)

# ── Bakong KHQR ───────────────────────────────────────────────────────────────
BAKONG_TOKEN = os.environ.get("BAKONG_TOKEN", "")
khqr_client  = KHQR(BAKONG_TOKEN)
PAYMENT_NAME = "RADY"
MAINTENANCE_MODE = False
CLONE_BOT_TOKEN = ""
CLONE_BOT_ACTIVE = False
_clone_bot_thread = None
_clone_bot_prefs: dict = {}
_clone_bots_list: list = []
_clone_bots_lock  = threading.Lock()

# ── Translate Bot (separate from TTS Clone Bot) ───────────────────────────────
TRANSLATE_BOT_TOKEN  = ""
TRANSLATE_BOT_ACTIVE = False
_translate_bot_thread = None
_translate_bot_prefs: dict = {}   # {user_id: {lang_code, lang_name}}
_admin_settings_msg: dict = {}   # {user_id: message_id} — settings panel to edit in place

# ── Telegram Bot API helper ───────────────────────────────────────────────────
def _tg_api(method, _files=None, **kwargs):
    """Call a Telegram Bot API method. Returns the 'result' field or None."""
    url = f"{BOT_API_URL}{method}"
    try:
        if _files:
            data = {}
            for k, v in kwargs.items():
                if v is None:
                    continue
                data[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
            resp = http.post(url, data=data, files=_files, timeout=30)
        else:
            payload = {k: v for k, v in kwargs.items() if v is not None}
            resp = http.post(url, json=payload, timeout=20)
        result = resp.json()
        if result.get('ok'):
            return result.get('result')
        logger.warning(f"Telegram API {method} error: {result.get('description')}")
    except Exception as e:
        logger.error(f"_tg_api {method} failed: {type(e).__name__}: {e}")
    return None

# ── Keyboard converter ────────────────────────────────────────────────────────
def _convert_keyboard(markup):
    """Pass Bot-API-style keyboard dicts through as-is for the HTTP API."""
    if markup is None or markup is False:
        return None
    if isinstance(markup, str):
        return None
    if isinstance(markup, dict):
        return markup
    return None

def _pm(parse_mode: str | None) -> str | None:
    """Return parse_mode string as-is for Bot API."""
    return parse_mode or None

# ── Manual KHQR builder (fallback) ───────────────────────────────────────────
def _crc16_ccitt(data: str) -> str:
    crc = 0xFFFF
    for ch in data:
        crc ^= ord(ch) << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"

def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"

def _build_khqr_manual(bank_account, merchant_name, merchant_city,
                        amount, bill_number, phone, store_label, terminal_label):
    if phone.startswith('855'):
        phone_local = '0' + phone[3:]
    else:
        phone_local = phone[-9:] if len(phone) > 9 else phone
    add_data = (
        _tlv("03", store_label) +
        _tlv("02", phone_local) +
        _tlv("01", bill_number) +
        _tlv("07", terminal_label)
    )
    now_ms = str(int(time.time() * 1000))
    exp_ms = str(int((time.time() + 86400) * 1000))
    info_data = _tlv("00", now_ms) + _tlv("01", exp_ms)
    body = (
        _tlv("00", "01") +
        _tlv("01", "12") +
        _tlv("29", _tlv("00", bank_account)) +
        _tlv("52", "5999") +
        _tlv("53", "840") +
        _tlv("54", f"{amount:.2f}") +
        _tlv("58", "KH") +
        _tlv("59", merchant_name) +
        _tlv("60", merchant_city) +
        _tlv("62", add_data) +
        _tlv("99", info_data) +
        "6304"
    )
    return body + _crc16_ccitt(body)

def generate_payment_qr(amount):
    if not BAKONG_TOKEN:
        msg = "BAKONG_TOKEN មិនមានក្នុង environment"
        logger.error(msg)
        return None, msg, None
    try:
        bill_number = f"TRX{int(time.time())}"
        try:
            try:
                qr = khqr_client.create_qr(
                    bank_account='sovannrady@aclb',
                    merchant_name=PAYMENT_NAME,
                    merchant_city='KPS',
                    amount=amount,
                    currency='USD',
                    store_label=PAYMENT_NAME,
                    phone_number='85593330905',
                    bill_number=bill_number,
                    terminal_label='Cashier-01',
                    static=False,
                    expiration=1
                )
                logger.info("create_qr with expiration=1 succeeded")
            except TypeError:
                qr = khqr_client.create_qr(
                    bank_account='sovannrady@aclb',
                    merchant_name=PAYMENT_NAME,
                    merchant_city='KPS',
                    amount=amount,
                    currency='USD',
                    store_label=PAYMENT_NAME,
                    phone_number='85593330905',
                    bill_number=bill_number,
                    terminal_label='Cashier-01',
                    static=False
                )
                logger.info("create_qr without expiration succeeded (older library)")
            logger.info(f"KHQR string created, length={len(qr)}, start={qr[:40]}")
            if '5303840' not in qr or '5404' not in qr:
                logger.warning("Library KHQR missing currency/amount — using manual builder")
                qr = _build_khqr_manual(
                    bank_account='sovannrady@aclb',
                    merchant_name=PAYMENT_NAME,
                    merchant_city='KPS',
                    amount=amount,
                    bill_number=bill_number,
                    phone='85593330905',
                    store_label=PAYMENT_NAME,
                    terminal_label='Cashier-01'
                )
                logger.info(f"Manual KHQR built, length={len(qr)}, start={qr[:40]}")
        except Exception as e:
            msg = f"create_qr failed: {type(e).__name__}: {e}"
            logger.error(msg)
            return None, msg, None
        md5 = compute_md5(qr)
        logger.info(f"MD5 computed: {md5}")
        img_bytes = None
        try:
            img_bytes = khqr_client.qr_image(qr, format='bytes')
            logger.info("QR image generated via bakong-khqr library")
        except Exception as e1:
            logger.warning(f"bakong-khqr image failed ({type(e1).__name__}: {e1}), trying qrcode library")
        if not img_bytes:
            try:
                import qrcode
                qr_img = qrcode.make(qr)
                buf = io.BytesIO()
                qr_img.save(buf, format='PNG')
                img_bytes = buf.getvalue()
                logger.info("QR image generated via qrcode library")
            except Exception as e2:
                logger.warning(f"qrcode library failed ({type(e2).__name__}: {e2}), trying API fallback")
        if not img_bytes:
            try:
                qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&data={url_quote(qr)}"
                resp = http.get(qr_api_url, timeout=10)
                resp.raise_for_status()
                img_bytes = resp.content
                logger.info("QR image generated via qrserver.com API")
            except Exception as e3:
                msg = f"all 3 QR image methods failed. Last: {type(e3).__name__}: {e3}"
                logger.error(msg)
                return None, msg, None
        logger.info(f"Generated KHQR for amount ${amount}, bill {bill_number}, md5 {md5}, size {len(img_bytes)}b")
        return img_bytes, md5, qr
    except Exception as e:
        msg = f"Unexpected: {type(e).__name__}: {e}"
        logger.error(f"Failed to generate payment QR: {msg}")
        return None, msg, None

def _bakong_api_url():
    if BAKONG_TOKEN and BAKONG_TOKEN.startswith("rbk"):
        return "https://api.bakongrelay.com/v1"
    return "https://api-bakong.nbc.gov.kh/v1"

def compute_md5(qr: str) -> str:
    return hashlib.md5(qr.encode('utf-8')).hexdigest()

def check_payment_status(md5):
    try:
        base = _bakong_api_url()
        resp = http.post(
            f"{base}/check_transaction_by_md5",
            json={"md5": md5},
            headers={
                "Authorization": f"Bearer {BAKONG_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=10
        )
        data = resp.json()
        logger.info(f"check_payment response: status={resp.status_code} body={data}")
        if data.get("responseCode") == 0:
            return True, data.get("data", {})
        return False, None
    except Exception as e:
        logger.error(f"Failed to check payment status: {type(e).__name__}: {e}")
    return False, None

# ── Neon DB ───────────────────────────────────────────────────────────────────
NEON_DATABASE_URL = os.environ.get("NEON_DATABASE_URL", "")
_neon_host    = urlparse(NEON_DATABASE_URL).hostname if NEON_DATABASE_URL else ""
_neon_api_url = f"https://{_neon_host}/sql"
_neon_headers = {
    'Neon-Connection-String': NEON_DATABASE_URL,
    'Content-Type': 'application/json',
    'Accept': 'application/json'
}

def _neon_query(query, params=None, _retries=3, _backoff=2):
    body = {'query': query}
    if params:
        body['params'] = [str(p) if p is not None else None for p in params]
    last_exc = None
    for attempt in range(1, _retries + 1):
        try:
            resp = http.post(_neon_api_url, headers=_neon_headers, json=body, timeout=20)
            if not resp.ok:
                logger.warning(f"Neon query HTTP {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_exc = e
            if attempt < _retries:
                wait = _backoff * attempt
                logger.warning(f"Neon query failed (attempt {attempt}/{_retries}), retrying in {wait}s: {e}")
                time.sleep(wait)
    raise last_exc

def _neon_cleanup():
    try:
        r1 = _neon_query("DELETE FROM bot_sent_verifications WHERE first_sent_at < NOW() - INTERVAL '30 days'")
        deleted_verif = (r1.get('rowCount') or 0)
        r2 = _neon_query("DELETE FROM bot_scheduled_deletions WHERE delete_at < NOW() - INTERVAL '1 day'")
        deleted_sched = (r2.get('rowCount') or 0)
        r3 = _neon_query(
            "UPDATE bot_purchase_history SET accounts = '[]'::jsonb "
            "WHERE accounts != '[]'::jsonb AND purchased_at < NOW() - INTERVAL '90 days'"
        )
        cleared_accounts = (r3.get('rowCount') or 0)
        logger.info(
            f"Neon cleanup: removed {deleted_verif} old verifications, "
            f"{deleted_sched} expired deletions, "
            f"cleared credentials on {cleared_accounts} old history rows"
        )
    except Exception as e:
        logger.warning(f"Neon cleanup failed: {e}")

def _neon_keepalive():
    cleanup_interval = 24 * 60 * 60
    ping_interval    = 240
    pings_per_cleanup = cleanup_interval // ping_interval
    ping_count = 0
    while True:
        time.sleep(ping_interval)
        try:
            _neon_query("SELECT 1")
            logger.debug("Neon keep-alive ping OK")
        except Exception as e:
            logger.warning(f"Neon keep-alive ping failed: {e}")
        ping_count += 1
        if ping_count >= pings_per_cleanup:
            ping_count = 0
            _neon_cleanup()

def _init_db():
    try:
        _neon_query("""
            CREATE TABLE IF NOT EXISTS bot_accounts (
                id SERIAL PRIMARY KEY,
                data JSONB NOT NULL DEFAULT '{}'
            )
        """)
        _neon_query("""
            CREATE TABLE IF NOT EXISTS bot_sessions (
                id SERIAL PRIMARY KEY,
                data JSONB NOT NULL DEFAULT '{}'
            )
        """)
        _neon_query("""
            CREATE TABLE IF NOT EXISTS bot_pending_payments (
                user_id BIGINT PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                account_type TEXT,
                quantity INT,
                total_price NUMERIC,
                md5_hash TEXT,
                qr_message_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        _neon_query("""
            CREATE TABLE IF NOT EXISTS bot_purchase_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                account_type TEXT,
                quantity INT,
                total_price NUMERIC,
                accounts JSONB DEFAULT '[]',
                purchased_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        _neon_query("ALTER TABLE bot_purchase_history ADD COLUMN IF NOT EXISTS accounts JSONB DEFAULT '[]'")
        _neon_query("""
            CREATE TABLE IF NOT EXISTS bot_known_users (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                first_seen TIMESTAMPTZ DEFAULT NOW(),
                last_seen TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        _neon_query("ALTER TABLE bot_known_users ADD COLUMN IF NOT EXISTS admin_notified INTEGER DEFAULT 0")
        _neon_query("""
            CREATE TABLE IF NOT EXISTS bot_sent_verifications (
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                first_sent_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (email, code)
            )
        """)
        _neon_query("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        _neon_query("""
            CREATE TABLE IF NOT EXISTS bot_scheduled_deletions (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                delete_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (chat_id, message_id)
            )
        """)
        _neon_query("""
            CREATE TABLE IF NOT EXISTS bot_email_buyer_map (
                email TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                account_type TEXT,
                purchased_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        _neon_query("""
            INSERT INTO bot_known_users (user_id, first_seen, last_seen, admin_notified)
            SELECT DISTINCT user_id, MIN(purchased_at), MAX(purchased_at), 1
            FROM bot_purchase_history
            GROUP BY user_id
            ON CONFLICT (user_id) DO UPDATE SET admin_notified = 1
        """)
        _neon_query("""
            INSERT INTO bot_email_buyer_map (email, user_id, account_type, purchased_at)
            SELECT DISTINCT ON (acc->>'email')
                acc->>'email'   AS email,
                user_id::BIGINT,
                account_type,
                purchased_at
            FROM bot_purchase_history,
                 jsonb_array_elements(
                     CASE jsonb_typeof(accounts::jsonb)
                         WHEN 'array' THEN accounts::jsonb
                         ELSE '[]'::jsonb
                     END
                 ) AS acc
            WHERE acc->>'email' IS NOT NULL
              AND acc->>'email' <> ''
            ORDER BY acc->>'email', purchased_at DESC
            ON CONFLICT (email) DO UPDATE
                SET user_id      = EXCLUDED.user_id,
                    account_type = EXCLUDED.account_type,
                    purchased_at = EXCLUDED.purchased_at
        """)
        r = _neon_query("SELECT COUNT(*) as cnt FROM bot_accounts")
        if int(r['rows'][0]['cnt']) == 0:
            _neon_query("INSERT INTO bot_accounts (data) VALUES ($1)",
                        [json.dumps({'accounts': [], 'account_types': {}, 'prices': {}})])
        r = _neon_query("SELECT COUNT(*) as cnt FROM bot_sessions")
        if int(r['rows'][0]['cnt']) == 0:
            _neon_query("INSERT INTO bot_sessions (data) VALUES ($1)", [json.dumps({})])
        logger.info("Neon DB initialized successfully")
    except Exception as e:
        logger.error(f"DB init failed: {e}")

def get_setting(key, default=None):
    try:
        r = _neon_query("SELECT value FROM bot_settings WHERE key = $1", [key])
        rows = r.get('rows', []) or []
        if rows:
            return rows[0].get('value')
    except Exception as e:
        logger.error(f"Failed to read setting {key}: {e}")
    return default

def set_setting(key, value):
    try:
        _neon_query("""
            INSERT INTO bot_settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, [key, str(value)])
    except Exception as e:
        logger.error(f"Failed to save setting {key}: {e}")

_data_loaded_ok = False

def load_data():
    global _data_loaded_ok
    try:
        r = _neon_query("SELECT data FROM bot_accounts LIMIT 1", _retries=6, _backoff=3)
        if r['rows']:
            data = r['rows'][0]['data']
            if isinstance(data, str):
                data = json.loads(data)
            logger.info("Loaded accounts data from Neon DB")
            _data_loaded_ok = True
            return data
    except Exception as e:
        logger.error(f"Failed to load data from DB: {e}")
    _data_loaded_ok = False
    return {'accounts': [], 'account_types': {}, 'prices': {}}

def save_data():
    global _data_loaded_ok
    if not _data_loaded_ok:
        logger.warning("save_data called before successful load — attempting reload before saving")
        reloaded = load_data()
        if not _data_loaded_ok:
            logger.error("save_data aborted: could not verify DB state. Data NOT overwritten.")
            return
        with _data_lock:
            for key in ('account_types', 'prices'):
                if accounts_data.get(key):
                    reloaded[key] = accounts_data[key]
            if accounts_data.get('accounts'):
                reloaded['accounts'] = accounts_data['accounts']
            accounts_data.update(reloaded)
    try:
        _neon_query("UPDATE bot_accounts SET data = $1",
                    [json.dumps(accounts_data, ensure_ascii=False)])
        logger.info("Saved accounts data to Neon DB")
    except Exception as e:
        logger.error(f"Failed to save data to DB: {e}")

def load_sessions():
    global user_sessions
    try:
        r = _neon_query("SELECT data FROM bot_sessions LIMIT 1")
        if r['rows']:
            data = r['rows'][0]['data']
            if isinstance(data, str):
                data = json.loads(data)
            user_sessions = {int(k): v for k, v in data.items()}
            logger.info("Loaded sessions from Neon DB")
    except Exception as e:
        logger.error(f"Failed to load sessions from DB: {e}")

def save_sessions():
    try:
        with _data_lock:
            payload = {str(k): v for k, v in user_sessions.items()}
        _neon_query("UPDATE bot_sessions SET data = $1",
                    [json.dumps(payload, ensure_ascii=False)])
    except Exception as e:
        logger.error(f"Failed to save sessions to DB: {e}")

def _run_background(name, func, *args, **kwargs):
    def runner():
        try:
            func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Background task {name} failed: {type(e).__name__}: {e}")
    background_pool.submit(runner)

def save_sessions_async():
    _run_background("save_sessions", save_sessions)

def save_pending_payment_async(user_id, chat_id, session):
    _run_background("save_pending_payment", save_pending_payment, user_id, chat_id, session)

def delete_pending_payment_async(user_id):
    _run_background("delete_pending_payment", delete_pending_payment, user_id)

def save_purchase_history_async(user_id, account_type, quantity, total_price, accounts=None):
    _run_background("save_purchase_history", save_purchase_history, user_id, account_type, quantity, total_price, accounts)

# ── Message deletion ──────────────────────────────────────────────────────────
def _delete_message_now(chat_id, message_id):
    try:
        result = _tg_api('deleteMessage', chat_id=chat_id, message_id=message_id)
        if result is not None:
            logger.info(f"Deleted message {message_id} from chat {chat_id}")
            return True
        return False
    except Exception as e:
        logger.warning(f"Failed to delete message {message_id} in chat {chat_id}: {e}")
        return False

def delete_message_async(chat_id, message_id):
    if not message_id:
        return
    _run_background("delete_message", _delete_message_now, chat_id, message_id)

def _record_scheduled_deletion(chat_id, message_id, delay_seconds):
    try:
        _neon_query("""
            INSERT INTO bot_scheduled_deletions (chat_id, message_id, delete_at)
            VALUES ($1, $2, NOW() + ($3 || ' seconds')::interval)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET
                delete_at = EXCLUDED.delete_at
        """, [str(chat_id), str(message_id), str(delay_seconds)])
    except Exception as e:
        logger.error(f"Failed to record scheduled deletion: {e}")

def _clear_scheduled_deletion(chat_id, message_id):
    try:
        _neon_query(
            "DELETE FROM bot_scheduled_deletions WHERE chat_id = $1 AND message_id = $2",
            [str(chat_id), str(message_id)]
        )
    except Exception as e:
        logger.error(f"Failed to clear scheduled deletion: {e}")

def _run_scheduled_delete(chat_id, message_id, delay_seconds):
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    for attempt in range(2):
        try:
            if _delete_message_now(chat_id, message_id):
                _clear_scheduled_deletion(chat_id, message_id)
                return
        except Exception as e:
            logger.warning(f"Failed delayed message delete attempt {attempt + 1}: {e}")
        time.sleep(2)
    _clear_scheduled_deletion(chat_id, message_id)

def delete_message_later(chat_id, message_id, delay_seconds=120):
    if not message_id:
        return
    _record_scheduled_deletion(chat_id, message_id, delay_seconds)
    _run_background("delete_message_later", _run_scheduled_delete, chat_id, message_id, delay_seconds)

def resume_scheduled_deletions():
    try:
        r = _neon_query(
            "SELECT chat_id, message_id, "
            "GREATEST(0, EXTRACT(EPOCH FROM (delete_at - NOW())))::int AS remaining "
            "FROM bot_scheduled_deletions"
        )
        rows = r.get('rows', []) or []
        for row in rows:
            try:
                chat_id   = int(row['chat_id'])
                message_id = int(row['message_id'])
                remaining  = int(row.get('remaining') or 0)
                _run_background("resume_scheduled_delete", _run_scheduled_delete, chat_id, message_id, remaining)
            except Exception as e:
                logger.warning(f"Bad scheduled deletion row {row}: {e}")
        if rows:
            logger.info(f"Resumed {len(rows)} scheduled message deletion(s) from DB")
    except Exception as e:
        logger.error(f"Failed to resume scheduled deletions: {e}")

# ── Pending payments ──────────────────────────────────────────────────────────
def save_pending_payment(user_id, chat_id, session):
    try:
        _neon_query("""
            INSERT INTO bot_pending_payments
                (user_id, chat_id, account_type, quantity, total_price, md5_hash, qr_message_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (user_id) DO UPDATE SET
                chat_id = EXCLUDED.chat_id,
                account_type = EXCLUDED.account_type,
                quantity = EXCLUDED.quantity,
                total_price = EXCLUDED.total_price,
                md5_hash = EXCLUDED.md5_hash,
                qr_message_id = EXCLUDED.qr_message_id,
                created_at = NOW()
        """, [
            str(user_id), str(chat_id),
            session.get('account_type'), str(session.get('quantity', 1)),
            str(session.get('total_price', 0)), session.get('md5_hash'),
            str(session.get('qr_message_id', 0))
        ])
        logger.info(f"Saved pending payment for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to save pending payment: {e}")

def get_pending_payment(user_id):
    try:
        r = _neon_query("SELECT * FROM bot_pending_payments WHERE user_id = $1", [str(user_id)])
        if r['rows']:
            row = r['rows'][0]
            return {
                'state': 'payment_pending',
                'account_type': row.get('account_type'),
                'quantity': int(row.get('quantity') or 1),
                'total_price': float(row.get('total_price') or 0),
                'md5_hash': row.get('md5_hash'),
                'qr_message_id': int(row.get('qr_message_id') or 0),
                'chat_id': int(row.get('chat_id') or 0)
            }
    except Exception as e:
        logger.error(f"Failed to get pending payment: {e}")
    return None

def delete_pending_payment(user_id):
    try:
        _neon_query("DELETE FROM bot_pending_payments WHERE user_id = $1", [str(user_id)])
        logger.info(f"Deleted pending payment for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to delete pending payment: {e}")

# ── Purchase history ──────────────────────────────────────────────────────────
def save_purchase_history(user_id, account_type, quantity, total_price, accounts=None):
    try:
        accounts_list = accounts or []
        accounts_json = json.dumps(accounts_list, ensure_ascii=False)
        _neon_query(
            "INSERT INTO bot_purchase_history (user_id, account_type, quantity, total_price, accounts) VALUES ($1, $2, $3, $4, $5)",
            [str(user_id), account_type, str(quantity), str(total_price), accounts_json]
        )
        for acc in accounts_list:
            if isinstance(acc, dict) and acc.get('email'):
                try:
                    _neon_query("""
                        INSERT INTO bot_email_buyer_map (email, user_id, account_type)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (email) DO UPDATE
                            SET user_id      = EXCLUDED.user_id,
                                account_type = EXCLUDED.account_type,
                                purchased_at = NOW()
                    """, [str(acc['email']).strip().lower(), str(user_id), account_type])
                except Exception as map_err:
                    logger.error(f"Failed to update email_buyer_map for {acc['email']}: {map_err}")
    except Exception as e:
        logger.error(f"Failed to save purchase history: {e}")

def get_purchase_history(user_id, limit=10):
    try:
        r = _neon_query(
            "SELECT account_type, quantity, total_price, accounts, purchased_at FROM bot_purchase_history WHERE user_id = $1 ORDER BY purchased_at DESC LIMIT $2",
            [str(user_id), str(limit)]
        )
        return r.get('rows', [])
    except Exception as e:
        logger.error(f"Failed to get purchase history: {e}")
    return []

def get_all_buyer_ids():
    try:
        r = _neon_query("SELECT DISTINCT user_id FROM bot_purchase_history")
        return [int(row['user_id']) for row in r.get('rows', [])]
    except Exception as e:
        logger.error(f"Failed to get buyer IDs: {e}")
    return []

# ── Email/buyer lookup ────────────────────────────────────────────────────────
def find_buyer_by_email(email):
    email = (email or '').strip().lower()
    if not email:
        return None
    try:
        r = _neon_query("SELECT user_id FROM bot_email_buyer_map WHERE LOWER(email) = $1", [email])
        if r.get('rows'):
            return int(r['rows'][0]['user_id'])
    except Exception as e:
        logger.error(f"email_buyer_map lookup failed for {email}: {e}")
    try:
        r = _neon_query(
            "SELECT user_id FROM bot_purchase_history WHERE accounts @> $1::jsonb ORDER BY purchased_at DESC LIMIT 1",
            [json.dumps([{"email": email}])]
        )
        if r.get('rows'):
            uid = int(r['rows'][0]['user_id'])
            try:
                _neon_query("""
                    INSERT INTO bot_email_buyer_map (email, user_id) VALUES ($1, $2)
                    ON CONFLICT (email) DO UPDATE SET user_id = EXCLUDED.user_id, purchased_at = NOW()
                """, [email, str(uid)])
            except Exception:
                pass
            return uid
    except Exception as e:
        logger.error(f"Failed to find buyer by email {email}: {e}")
    return None

def find_all_buyers_by_email(email):
    email = (email or '').strip().lower()
    if not email:
        return []
    buyers = []
    seen = set()
    try:
        r = _neon_query(
            "SELECT user_id, MAX(purchased_at) AS last_at FROM bot_purchase_history "
            "WHERE accounts @> $1::jsonb GROUP BY user_id ORDER BY last_at DESC",
            [json.dumps([{"email": email}])]
        )
        for row in r.get('rows', []) or []:
            uid = int(row['user_id'])
            if uid not in seen:
                seen.add(uid)
                buyers.append(uid)
    except Exception as e:
        logger.error(f"JSONB buyer scan failed for {email}: {e}")
    try:
        r2 = _neon_query(
            "SELECT user_id, accounts, purchased_at FROM bot_purchase_history "
            "WHERE accounts::text ILIKE $1 ORDER BY purchased_at DESC",
            [f"%{email}%"]
        )
        for row in r2.get('rows', []) or []:
            accs = row.get('accounts') or []
            if isinstance(accs, str):
                try:
                    accs = json.loads(accs)
                except Exception:
                    accs = []
            for account in accs:
                if str(account.get('email', '')).strip().lower() == email:
                    uid = int(row['user_id'])
                    if uid not in seen:
                        seen.add(uid)
                        buyers.append(uid)
                    break
    except Exception as e:
        logger.error(f"ILIKE buyer scan failed for {email}: {e}")
    return buyers

# ── DB init + settings restore ────────────────────────────────────────────────
_init_db()

_saved_payment_name = get_setting('PAYMENT_NAME')
if _saved_payment_name:
    PAYMENT_NAME = _saved_payment_name
    logger.info(f"Loaded PAYMENT_NAME from DB: {PAYMENT_NAME}")
_saved_maintenance = get_setting('MAINTENANCE_MODE')
if _saved_maintenance is not None:
    MAINTENANCE_MODE = (str(_saved_maintenance).lower() == 'true')
    logger.info(f"Loaded MAINTENANCE_MODE from DB: {MAINTENANCE_MODE}")
_saved_extra_admins = get_setting('EXTRA_ADMIN_IDS')
if _saved_extra_admins:
    try:
        EXTRA_ADMIN_IDS = set(int(x) for x in json.loads(_saved_extra_admins))
        logger.info(f"Loaded {len(EXTRA_ADMIN_IDS)} extra admin(s) from DB")
    except Exception as e:
        logger.error(f"Failed to parse EXTRA_ADMIN_IDS from DB: {e}")
_saved_bakong = get_setting('BAKONG_TOKEN')
if _saved_bakong:
    BAKONG_TOKEN = _saved_bakong
    try:
        khqr_client = KHQR(BAKONG_TOKEN)
    except Exception as e:
        logger.error(f"Failed to rebuild KHQR client from saved token: {e}")
    logger.info(f"Loaded BAKONG_TOKEN from DB: {BAKONG_TOKEN[:10]}...")
_saved_channel_id = get_setting('TELEGRAM_CHANNEL_ID')
if _saved_channel_id:
    CHANNEL_ID = _saved_channel_id.strip()
    logger.info(f"Loaded TELEGRAM_CHANNEL_ID from DB: {CHANNEL_ID}")
_saved_clone_token = get_setting('CLONE_BOT_TOKEN')
if _saved_clone_token:
    CLONE_BOT_TOKEN = _saved_clone_token
    logger.info("Loaded CLONE_BOT_TOKEN from DB")
_saved_clone_active = get_setting('CLONE_BOT_ACTIVE')
if _saved_clone_active:
    CLONE_BOT_ACTIVE = (str(_saved_clone_active).lower() == 'true')
_saved_tr_token = get_setting('TRANSLATE_BOT_TOKEN')
if _saved_tr_token:
    TRANSLATE_BOT_TOKEN = _saved_tr_token
    logger.info("Loaded TRANSLATE_BOT_TOKEN from DB")
_saved_tr_active = get_setting('TRANSLATE_BOT_ACTIVE')
if _saved_tr_active:
    TRANSLATE_BOT_ACTIVE = (str(_saved_tr_active).lower() == 'true')

# ── Session / account storage ─────────────────────────────────────────────────
user_sessions: dict = {}
_notified_users: set = set()
_notified_users_lock = threading.Lock()

def _is_admin_notified(uid):
    with _notified_users_lock:
        if uid in _notified_users:
            return True
    try:
        r = _neon_query("SELECT admin_notified FROM bot_known_users WHERE user_id = $1", [str(uid)])
        rows = r.get('rows', []) or []
        if rows and rows[0].get('admin_notified'):
            with _notified_users_lock:
                _notified_users.add(uid)
            return True
    except Exception as e:
        logger.error(f"Failed to check admin_notified for {uid}: {e}")
    return False

# ── Fetch user info via Bot API ───────────────────────────────────────────────
def fetch_user_info(user_id):
    """Fetch a user's profile from Telegram via Bot API getChat."""
    try:
        data = _tg_api('getChat', chat_id=user_id)
        if data:
            return {
                'id': data.get('id'),
                'first_name': data.get('first_name') or '',
                'last_name': data.get('last_name') or '',
                'username': data.get('username') or '',
            }
    except Exception as e:
        logger.error(f"getChat failed for {user_id}: {e}")
    return None

def backfill_known_user_profiles():
    try:
        r = _neon_query(
            "SELECT user_id FROM bot_known_users "
            "WHERE COALESCE(first_name, '') = '' AND COALESCE(last_name, '') = '' AND COALESCE(username, '') = ''"
        )
        rows = r.get('rows', [])
        for row in rows:
            uid = int(row['user_id'])
            info = fetch_user_info(uid)
            if not info:
                continue
            first = info.get('first_name') or ''
            last  = info.get('last_name') or ''
            uname = info.get('username') or ''
            try:
                _neon_query(
                    "UPDATE bot_known_users SET first_name=$1, last_name=$2, username=$3 WHERE user_id=$4",
                    [first, last, uname, str(uid)]
                )
                logger.info(f"Backfilled profile for {uid}: {first} {last} @{uname}")
            except Exception as e:
                logger.error(f"Failed to update profile for {uid}: {e}")
    except Exception as e:
        logger.error(f"backfill_known_user_profiles error: {e}")

# ── Notify admin of new users ─────────────────────────────────────────────────
def notify_admin_new_user(user):
    try:
        uid = user.get('id')
        if not uid or uid == ADMIN_ID:
            return
        if _is_admin_notified(uid):
            return
        with _notified_users_lock:
            if uid in _notified_users:
                return
            _notified_users.add(uid)
        first       = user.get('first_name', '') or ''
        last        = user.get('last_name', '') or ''
        full_name   = f"{first} {last}".strip() or 'N/A'
        username    = user.get('username')
        username_str = f"@{username}" if username else '—'
        msg = (
            "🆕 អ្នកប្រើប្រាស់ថ្មី!\n\n"
            f"👤 ឈ្មោះ: {html.escape(full_name)}\n"
            f"🔖 Username: {html.escape(username_str)}\n"
            f"🪪 ID: <code>{uid}</code>"
        )
        def _send():
            try:
                send_message(ADMIN_ID, msg, parse_mode='HTML', reply_to_message_id=False, reply_markup=False)
            except Exception as e:
                logger.error(f"Failed to send new-user notification: {e}")
            try:
                _neon_query("""
                    INSERT INTO bot_known_users (user_id, first_name, last_name, username, first_seen, last_seen, admin_notified)
                    VALUES ($1, $2, $3, $4, NOW(), NOW(), 1)
                    ON CONFLICT (user_id) DO UPDATE SET
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        username = EXCLUDED.username,
                        last_seen = NOW(),
                        admin_notified = 1
                """, [str(uid), first, last, username or ''])
            except Exception as e:
                logger.error(f"Failed to record known user {uid}: {e}")
        _run_background("notify_admin_new_user", _send)
    except Exception as e:
        logger.error(f"notify_admin_new_user error: {e}")

# ── Account storage ───────────────────────────────────────────────────────────
accounts_data = load_data()
load_sessions()

# ── Thread-local reply context ────────────────────────────────────────────────
_reply_context = threading.local()
START_BANNER_FILE_ID = get_setting('START_BANNER_FILE_ID') or os.environ.get("START_BANNER_FILE_ID", "")
if START_BANNER_FILE_ID:
    logger.info(f"Loaded START_BANNER_FILE_ID from DB/env: {START_BANNER_FILE_ID[:20]}...")

def _set_reply_to_id(message_id):
    _reply_context.message_id = message_id

def _get_reply_to_id():
    return getattr(_reply_context, 'message_id', None)

def _type_callback_id(account_type):
    return hashlib.sha1(account_type.encode('utf-8')).hexdigest()[:12]

def _account_type_from_callback_id(callback_id):
    for account_type in accounts_data.get('account_types', {}):
        if _type_callback_id(account_type) == callback_id:
            return account_type
    return None

def _short_label(text, limit=36):
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"

# ── Telegram send helpers (Bot API HTTP) ──────────────────────────────────────
def send_message(chat_id, text, reply_to_message_id=None, parse_mode=None,
                 reply_markup=None, message_effect_id=None):
    effective_reply_to = _get_reply_to_id() if reply_to_message_id is None else reply_to_message_id
    if effective_reply_to is False:
        effective_reply_to = None

    if reply_markup == "no_keyboard":
        kb = None
    else:
        effective_markup = reply_markup if (reply_markup is not None and reply_markup is not False) else MAIN_REPLY_KEYBOARD
        kb = _convert_keyboard(effective_markup)

    params = {'chat_id': chat_id, 'text': text}
    if effective_reply_to:
        params['reply_to_message_id'] = int(effective_reply_to)
    if parse_mode:
        params['parse_mode'] = parse_mode
    if kb is not None:
        params['reply_markup'] = kb

    try:
        result = _tg_api('sendMessage', **params)
        if result:
            return {'ok': True, 'result': result}
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
    return None

def send_sticker(chat_id, sticker_id, reply_markup=None):
    params = {'chat_id': chat_id, 'sticker': sticker_id}
    kb = _convert_keyboard(reply_markup) if reply_markup is not None else None
    if kb is not None:
        params['reply_markup'] = kb
    try:
        result = _tg_api('sendSticker', **params)
        if result:
            return {'ok': True, 'result': result}
    except Exception as e:
        logger.error(f"Failed to send sticker: {e}")
    return None

def send_photo(chat_id, photo_path, caption=None, parse_mode=None,
               reply_markup=None, message_effect_id=None):
    params = {'chat_id': chat_id, 'photo': photo_path}
    if caption:
        params['caption'] = caption
    if parse_mode:
        params['parse_mode'] = parse_mode
    kb = _convert_keyboard(reply_markup) if reply_markup is not None else None
    if kb is not None:
        params['reply_markup'] = kb
    try:
        result = _tg_api('sendPhoto', **params)
        if result:
            photos = result.get('photo', [])
            return {'ok': True, 'result': {'message_id': result.get('message_id'),
                                            'photo': photos}}
    except Exception as e:
        logger.error(f"Failed to send photo: {e}")
    return None

def send_photo_bytes(chat_id, photo_bytes, caption=None, parse_mode=None, reply_markup=None):
    buf = io.BytesIO(photo_bytes)
    buf.name = "photo.png"
    params = {'chat_id': chat_id}
    if caption:
        params['caption'] = caption
    if parse_mode:
        params['parse_mode'] = parse_mode
    kb = _convert_keyboard(reply_markup) if reply_markup is not None else None
    if kb is not None:
        params['reply_markup'] = kb
    try:
        result = _tg_api('sendPhoto', _files={'photo': buf}, **params)
        if result:
            return {'ok': True, 'result': result}
    except Exception as e:
        logger.error(f"Failed to send photo bytes: {e}")
    return None

def send_photo_url(chat_id, photo_url, caption=None, parse_mode=None, reply_markup=None):
    return send_photo(chat_id, photo_url, caption=caption, parse_mode=parse_mode,
                      reply_markup=reply_markup)

def send_start_banner(chat_id, caption=None, parse_mode=None,
                      message_effect_id=None, reply_markup=None):
    global START_BANNER_FILE_ID
    params = {'chat_id': chat_id}
    if caption:
        params['caption'] = caption
    if parse_mode:
        params['parse_mode'] = parse_mode
    kb = _convert_keyboard(reply_markup) if reply_markup is not None else None
    if kb is not None:
        params['reply_markup'] = kb

    if START_BANNER_FILE_ID:
        try:
            result = _tg_api('sendPhoto', photo=START_BANNER_FILE_ID, **params)
            if result:
                return {'ok': True, 'result': result}
        except Exception as e:
            logger.warning(f"Cached start banner failed, uploading again: {e}")
            START_BANNER_FILE_ID = ""

    try:
        with open('start_banner.jpg', 'rb') as f:
            result = _tg_api('sendPhoto', _files={'photo': f}, **params)
        if result:
            photos = result.get('photo', [])
            if photos:
                new_id = photos[-1].get('file_id', '')
                if new_id and new_id != START_BANNER_FILE_ID:
                    START_BANNER_FILE_ID = new_id
                    _run_background("save_banner_file_id", set_setting, 'START_BANNER_FILE_ID', new_id)
            return {'ok': True, 'result': result}
    except FileNotFoundError:
        logger.error("start_banner.jpg not found")
    except Exception as e:
        logger.error(f"Failed to send start banner: {e}")
    return None

def answer_callback(callback_query_id, text=None, show_alert=False):
    params = {'callback_query_id': callback_query_id}
    if text:
        params['text'] = text
    if show_alert:
        params['show_alert'] = True
    try:
        _tg_api('answerCallbackQuery', **params)
    except Exception as e:
        logger.warning(f"Failed to answer callback quickly: {e}")

def copy_message(chat_id, from_chat_id, message_id, reply_markup=None):
    params = {'chat_id': chat_id, 'from_chat_id': from_chat_id, 'message_id': message_id}
    if reply_markup:
        kb = _convert_keyboard(reply_markup)
        if kb is not None:
            params['reply_markup'] = kb
    try:
        result = _tg_api('copyMessage', **params)
        if result:
            return {'ok': True, 'result': result}
    except Exception as e:
        logger.error(f"Failed to copy message: {e}")
    return None

def _send_document_bytes(chat_id, content_bytes, filename, caption=None):
    """Send a document from raw bytes."""
    buf = io.BytesIO(content_bytes)
    buf.name = filename
    params = {'chat_id': chat_id}
    if caption:
        params['caption'] = caption
    try:
        result = _tg_api('sendDocument', _files={'document': (filename, buf)}, **params)
        return result
    except Exception as e:
        logger.error(f"Failed to send document: {e}")
        return None

# ── Channel helpers ───────────────────────────────────────────────────────────
def _is_configured_channel(chat_id):
    return CHANNEL_ID and str(chat_id) == str(CHANNEL_ID)

def parse_egets_verification_message(text):
    email_match = re.search(r'[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}', text or '')
    code_match  = re.search(r'(?<!\d)\d{4,8}(?!\d)', text or '')
    if not email_match or not code_match:
        return None, None
    return email_match.group(0).strip().lower(), code_match.group(0)

def format_egets_verification_message(email, code):
    return (
        "📩 <b>លេខកូដផ្ទៀងផ្ទាត់ E-GetS</b>\n\n"
        f"{html.escape(email)}\n\n"
        f"<code>{html.escape(code)}</code>"
    )

def handle_channel_post(channel_post):
    chat        = channel_post.get('chat', {})
    chat_id     = chat.get('id')
    message_id  = channel_post.get('message_id')
    if not _is_configured_channel(chat_id) or not message_id:
        return

    text = channel_post.get('text') or channel_post.get('caption') or ''
    verification_email, verification_code = parse_egets_verification_message(text)
    if verification_email and verification_code:
        formatted_message = format_egets_verification_message(verification_email, verification_code)
        buyer_ids = find_all_buyers_by_email(verification_email)
        delivered_to = []
        for buyer_id in buyer_ids:
            buyer_sent = send_message(buyer_id, formatted_message, parse_mode="HTML",
                                      reply_to_message_id=False, reply_markup=False)
            if buyer_sent and buyer_sent.get('result'):
                buyer_message_id = buyer_sent['result'].get('message_id')
                delete_message_later(buyer_id, buyer_message_id, 60)
                delivered_to.append(buyer_id)
                logger.info(f"Sent verification code for {verification_email} to buyer {buyer_id}")
            else:
                logger.warning(f"Direct send to buyer {buyer_id} failed for {verification_email}")
        if not delivered_to:
            logger.warning(f"No buyer reachable for {verification_email}; sending to admin")
            sent = send_message(ADMIN_ID, formatted_message, parse_mode="HTML",
                                reply_to_message_id=False, reply_markup=False)
            if sent and sent.get('result'):
                delete_message_later(ADMIN_ID, sent['result'].get('message_id'), 60)
        return

    copied = copy_message(ADMIN_ID, chat_id, message_id)
    if copied:
        logger.info(f"Copied channel post {message_id} from {chat_id} to admin {ADMIN_ID}")
        return
    if text:
        send_message(ADMIN_ID, text, reply_to_message_id=False, reply_markup=False)

# ── Account selection / keyboards ─────────────────────────────────────────────
ACCOUNT_BTN_PREFIX = "ទិញ "
ACCOUNT_BTN_SUFFIX = " - មានក្នុងស្តុក "

def show_account_selection(chat_id):
    send_account_selection_inline(chat_id)

def send_account_selection_inline(chat_id):
    inline_rows = []
    with _data_lock:
        types = dict(accounts_data.get('account_types', {}))
    if not types:
        return
    for account_type, accounts in types.items():
        count = len(accounts)
        if count <= 0:
            continue
        btn_text = f"ទិញ {account_type} - មានក្នុងស្តុក {count}"
        cb = f"buy:{_type_callback_id(account_type)}"
        inline_rows.append([{'text': btn_text, 'callback_data': cb}])
    if not inline_rows:
        send_message(chat_id, "<i>សូមអភ័យទោស អស់ពីស្តុក 🪤</i>",
                     parse_mode="HTML", reply_to_message_id=False)
        return
    inline_keyboard = {'inline_keyboard': inline_rows}
    send_message(chat_id, "<b>សូមជ្រើសរើសគូប៉ុងដើម្បីទិញ៖</b>",
                 reply_to_message_id=False, reply_markup=inline_keyboard, parse_mode="HTML")

MAIN_REPLY_KEYBOARD    = {'remove_keyboard': True}
ADMIN_REPLY_KEYBOARD   = {'remove_keyboard': True}
ADMIN_SETTINGS_BTN     = '⚙️កំណត់'

def _main_kb(uid):
    return MAIN_REPLY_KEYBOARD

BTN_ADD_ACCOUNT     = '➕ បន្ថែម Account'
BTN_DELETE_TYPE     = '🗑 លុបប្រភេទ'
BTN_USERS           = '👥 អ្នកប្រើប្រាស់'
BTN_BUYERS          = '📋 របាយការណ៍ទិញ'
BTN_PAYMENT         = '💳 ឈ្មោះ Payment'
BTN_BAKONG          = '🔑 Bakong Token'
BTN_CHANNEL         = '📢 Channel ID'
BTN_ADMINS          = '👑 គ្រប់គ្រង Admin'
BTN_MAINTENANCE     = '🛠 Maintenance Mode'
BTN_BROADCAST       = '📢 ផ្សាយព័ត៌មាន'
BTN_BACK_HOME       = '🏠 ត្រឡប់ទៅម៉ឺនុយដើម'
BTN_BACK_SETTINGS   = '↩️ ត្រឡប់ទៅកំណត់'
BTN_PAYMENT_EDIT    = '✏️ ប្តូរឈ្មោះ Payment'
BTN_BAKONG_EDIT     = '✏️ ប្តូរ Bakong Token'
BTN_CHANNEL_EDIT    = '✏️ ប្តូរ Channel ID'
BTN_CHANNEL_CLEAR   = '🗑 លុប Channel ID'
BTN_ADMIN_ADD       = '➕ បន្ថែម Admin'
BTN_ADMIN_REMOVE    = '➖ ដក Admin'
BTN_MAINT_ON        = '🔴 បិទ Bot'
BTN_MAINT_OFF       = '🟢 បើក Bot'
BTN_CANCEL_INPUT    = '🚫 បោះបង់'
BTN_DELETE_CONFIRM  = '✅ បញ្ជាក់លុប'
BTN_DELETE_CANCEL   = '🚫 បោះបង់ការលុប'
BTN_BROADCAST_CONFIRM = '✅ បញ្ជាក់ផ្សាយ'
BTN_BROADCAST_CANCEL  = '🚫 បោះបង់ការផ្សាយ'
BTN_CLONE_BOT         = '🎙 TTS Bot'
BTN_CLONE_START       = '▶️ ចាប់ផ្តើម TTS Bot'
BTN_CLONE_STOP        = '⏹ បញ្ឈប់ TTS Bot'
BTN_CLONE_SET_TOKEN   = '🔑 Token — TTS'
BTN_CLONE_TOKEN_CLEAR = '🗑 លុប — TTS'
BTN_TRANSLATE         = '🌐 Translate Bot'
BTN_TR_START          = '▶️ ចាប់ផ្តើម Translate Bot'
BTN_TR_STOP           = '⏹ បញ្ឈប់ Translate Bot'
BTN_TR_SET_TOKEN      = '🔑 Token — Translate'
BTN_TR_TOKEN_CLEAR    = '🗑 លុប — Translate'

TRANSLATE_LANGUAGES = {
    "km": "🇰🇭 ខ្មែរ",
    "en": "🇺🇸 អង់គ្លេស",
    "zh-CN": "🇨🇳 ចិន",
    "ja": "🇯🇵 ជប៉ុន",
    "ko": "🇰🇷 កូរ៉េ",
    "fr": "🇫🇷 បារាំង",
    "th": "🇹🇭 ថៃ",
    "vi": "🇻🇳 វៀតណាម",
    "de": "🇩🇪 អាល្លឺម៉ង់",
    "es": "🇪🇸 អេស្ប៉ាញ",
    "ru": "🇷🇺 រុស្សី",
    "ar": "🇸🇦 អារ៉ាប់",
    "pt": "🇵🇹 ព័រទុយហ្គាល់",
    "it": "🇮🇹 អ៊ីតាលី",
    "hi": "🇮🇳 ហិណ្ឌូ",
    "id": "🇮🇩 អ៊ីនដូនេស៊ី",
    "ms": "🇲🇾 ម៉ាឡេស៊ី",
    "tl": "🇵🇭 ហ្វីលីពីន",
    "tr": "🇹🇷 តួគី",
    "nl": "🇳🇱 ហូឡង់",
    "pl": "🇵🇱 ប៉ូឡូញ",
    "uk": "🇺🇦 អ៊ុយក្រែន",
    "sv": "🇸🇪 ស៊ុយអែត",
    "da": "🇩🇰 ដាណឺម៉ាក",
    "fi": "🇫🇮 ហ្វាំងឡង់",
    "my": "🇲🇲 មីយ៉ាន់ម៉ា",
    "lo": "🇱🇦 ឡាវ",
    "mn": "🇲🇳 ម៉ុងហ្គោល",
}

BROADCAST_CONFIRM_KEYBOARD = {'inline_keyboard': [
    [{'text': BTN_BROADCAST_CONFIRM, 'callback_data': 's:broadcast_confirm'},
     {'text': BTN_BROADCAST_CANCEL,  'callback_data': 's:broadcast_cancel'}],
]}

# ── Settings panel — ALL inline (editMessageText navigation) ─────────────────
ADMIN_SETTINGS_REPLY_KEYBOARD = {'remove_keyboard': True}   # keep name for compat

SETTINGS_MAIN_IKB = {'inline_keyboard': [
    [{'text': '➕ បន្ថែម Account',  'callback_data': 's:add_acc'},
     {'text': '🗑 លុបប្រភេទ',      'callback_data': 's:del_type'}],
    [{'text': '📋 របាយការណ៍ទិញ',  'callback_data': 's:buyers'},
     {'text': '💳 ឈ្មោះ Payment',  'callback_data': 's:pay'}],
    [{'text': '🔑 Bakong Token',    'callback_data': 's:bak'},
     {'text': '📢 Channel ID',      'callback_data': 's:ch'}],
    [{'text': '👑 Admin',           'callback_data': 's:adm'},
     {'text': '🛠 Maintenance',     'callback_data': 's:mnt'}],
    [{'text': '🎙 TTS Bot',         'callback_data': 's:tts'},
     {'text': '🌐 Translate Bot',   'callback_data': 's:tr'}],
]}
SETTINGS_PAY_IKB = {'inline_keyboard': [
    [{'text': '✏️ ប្តូរឈ្មោះ Payment', 'callback_data': 's:pay_edit'}],
    [{'text': '↩️ ត្រឡប់',            'callback_data': 's:main'}],
]}
SETTINGS_BAK_IKB = {'inline_keyboard': [
    [{'text': '✏️ ប្តូរ Bakong Token', 'callback_data': 's:bak_edit'}],
    [{'text': '↩️ ត្រឡប់',            'callback_data': 's:main'}],
]}
SETTINGS_CH_IKB = {'inline_keyboard': [
    [{'text': '✏️ ប្តូរ Channel ID',   'callback_data': 's:ch_edit'},
     {'text': '🗑 លុប Channel ID',     'callback_data': 's:ch_clear'}],
    [{'text': '↩️ ត្រឡប់',            'callback_data': 's:main'}],
]}
SETTINGS_ADM_IKB = {'inline_keyboard': [
    [{'text': '➕ បន្ថែម Admin',       'callback_data': 's:adm_add'},
     {'text': '➖ ដក Admin',           'callback_data': 's:adm_rm'}],
    [{'text': '↩️ ត្រឡប់',            'callback_data': 's:main'}],
]}
SETTINGS_CANCEL_IKB = {'inline_keyboard': [
    [{'text': '🚫 បោះបង់',            'callback_data': 's:cancel_input'}],
]}
# Dynamic builders (state-dependent)
def _build_settings_maint_ikb():
    btn = BTN_MAINT_OFF if MAINTENANCE_MODE else BTN_MAINT_ON
    cb  = 's:mnt_off'  if MAINTENANCE_MODE else 's:mnt_on'
    return {'inline_keyboard': [
        [{'text': btn, 'callback_data': cb}],
        [{'text': '↩️ ត្រឡប់', 'callback_data': 's:main'}],
    ]}
def _build_settings_tts_ikb():
    running = CLONE_BOT_ACTIVE and _clone_bot_thread and _clone_bot_thread.is_alive()
    return {'inline_keyboard': [
        [{'text': BTN_CLONE_STOP if running else BTN_CLONE_START,
          'callback_data': 's:tts_stop' if running else 's:tts_start'}],
        [{'text': BTN_CLONE_SET_TOKEN, 'callback_data': 's:tts_token'},
         {'text': BTN_CLONE_TOKEN_CLEAR, 'callback_data': 's:tts_clear'}],
        [{'text': '🌐 Translate Bot',  'callback_data': 's:tr'},
         {'text': '↩️ ត្រឡប់',        'callback_data': 's:main'}],
    ]}
def _build_settings_tr_ikb():
    running = TRANSLATE_BOT_ACTIVE and _translate_bot_thread and _translate_bot_thread.is_alive()
    return {'inline_keyboard': [
        [{'text': BTN_TR_STOP if running else BTN_TR_START,
          'callback_data': 's:tr_stop' if running else 's:tr_start'}],
        [{'text': BTN_TR_SET_TOKEN,  'callback_data': 's:tr_token'},
         {'text': BTN_TR_TOKEN_CLEAR,'callback_data': 's:tr_clear'}],
        [{'text': '🎙 TTS Bot',       'callback_data': 's:tts'},
         {'text': '↩️ ត្រឡប់',       'callback_data': 's:main'}],
    ]}

# Legacy compat aliases (some internal callers reference these names)
PAYMENT_SUBMENU_KEYBOARD    = SETTINGS_PAY_IKB
BAKONG_SUBMENU_KEYBOARD     = SETTINGS_BAK_IKB
CHANNEL_SUBMENU_KEYBOARD    = SETTINGS_CH_IKB
ADMINS_SUBMENU_KEYBOARD     = SETTINGS_ADM_IKB
MAINTENANCE_SUBMENU_KEYBOARD = None  # replaced by _build_settings_maint_ikb()
CLONE_BOT_MENU_KEYBOARD_ACTIVE   = None  # replaced by _build_settings_tts_ikb()
CLONE_BOT_MENU_KEYBOARD_INACTIVE = None  # replaced by _build_settings_tts_ikb()
TRANSLATE_BOT_MENU_KB_ACTIVE     = None  # replaced by _build_settings_tr_ikb()
TRANSLATE_BOT_MENU_KB_INACTIVE   = None  # replaced by _build_settings_tr_ikb()
TRANSLATE_SUBMENU_KEYBOARD = {'remove_keyboard': True}
CLONE_MAIN_KB              = {'remove_keyboard': True}
CANCEL_INPUT_KEYBOARD      = SETTINGS_CANCEL_IKB
ADD_ACCOUNT_KEYBOARD = SETTINGS_CANCEL_IKB
CONFIRM_REPLY_KEYBOARD = {
    'keyboard': [[{'text': '🚫 បោះបង់'}, {'text': '✅ យល់ព្រម'}]],
    'resize_keyboard': True, 'one_time_keyboard': True
}

def _build_account_type_keyboard():
    with _data_lock:
        types = sorted(
            t for t, accs in accounts_data.get('account_types', {}).items()
            if len(accs) > 0
        )
    rows = []
    for i in range(0, len(types), 2):
        row = [{'text': types[i]}]
        if i + 1 < len(types):
            row.append({'text': types[i + 1]})
        rows.append(row)
    rows.append([{'text': BTN_BACK_SETTINGS}])
    return {'keyboard': rows, 'resize_keyboard': True, 'is_persistent': True}

ADMIN_BUTTON_LABELS = {
    BTN_ADD_ACCOUNT, BTN_DELETE_TYPE, BTN_BUYERS,
    BTN_PAYMENT, BTN_BAKONG, BTN_CHANNEL, BTN_MAINTENANCE, '🤖 Clone Bot',
    BTN_BACK_SETTINGS,
    BTN_PAYMENT_EDIT, BTN_BAKONG_EDIT,
    BTN_CHANNEL_EDIT, BTN_CHANNEL_CLEAR,
    BTN_ADMIN_ADD, BTN_ADMIN_REMOVE,
    BTN_MAINT_ON, BTN_MAINT_OFF,
    BTN_CLONE_BOT, BTN_CLONE_START, BTN_CLONE_STOP,
    BTN_CLONE_SET_TOKEN, BTN_CLONE_TOKEN_CLEAR,
    BTN_TRANSLATE,
}

# ── Translation helpers ───────────────────────────────────────────────────────
def _get_translate_lang_keyboard():
    rows = []
    items = list(TRANSLATE_LANGUAGES.items())
    for i in range(0, len(items), 3):
        row = [{'text': name, 'callback_data': f'lang_{code}'}
               for code, name in items[i:i+3]]
        rows.append(row)
    return {'inline_keyboard': rows}

def _show_translate_menu(chat_id, user_id):
    with _data_lock:
        user_sessions[user_id] = {'state': 'translate_mode', 'lang_code': 'en', 'lang_name': '🇺🇸 អង់គ្លេស'}
    save_sessions_async()
    send_message(
        chat_id,
        "🌐 <b>បកប្រែភាសា</b>\n\nសូមជ្រើសរើសភាសាដែលចង់បកប្រែទៅ៖",
        parse_mode="HTML",
        reply_to_message_id=False,
        reply_markup=_get_translate_lang_keyboard()
    )

def _translate_text(text, target_lang):
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source='auto', target=target_lang).translate(text)
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return None

# ── Admin settings panel helpers ──────────────────────────────────────────────
def _settings_edit(chat_id, user_id, text, ikb):
    """Edit the stored settings-panel message in place; fall back to new send."""
    msg_id = _admin_settings_msg.get(user_id)
    if msg_id:
        result = _tg_api('editMessageText', chat_id=chat_id, message_id=msg_id,
                         text=text, parse_mode='HTML', reply_markup=ikb)
        if result:
            return result
    # Fallback: send new and store id
    r = send_message(chat_id, text, parse_mode='HTML',
                     reply_to_message_id=False, reply_markup=ikb)
    if r and isinstance(r, dict):
        _admin_settings_msg[user_id] = r.get('message_id')
    return r

def _settings_main_text():
    return "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើសប្រតិបត្តិការ៖"

def send_admin_settings_menu(chat_id, user_id=None):
    """Send (or re-send) the settings panel as a fresh message and store its id."""
    if user_id is None:
        user_id = chat_id
    r = send_message(chat_id, _settings_main_text(), parse_mode='HTML',
                     reply_to_message_id=False, reply_markup=SETTINGS_MAIN_IKB)
    if r and isinstance(r, dict):
        _admin_settings_msg[user_id] = r.get('message_id')

def _prompt_admin_input(chat_id, user_id, key, prompt_text, return_menu='main'):
    """Edit the settings panel in place to show an input prompt."""
    with _data_lock:
        user_sessions[user_id] = {
            'state': f'admin_input:{key}',
            'settings_return': return_menu,
        }
    save_sessions_async()
    _settings_edit(
        chat_id, user_id,
        prompt_text + "\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
        SETTINGS_CANCEL_IKB,
    )

def _show_users_list_inline(chat_id):
    try:
        backfill_known_user_profiles()
    except Exception as e:
        logger.error(f"Inline backfill failed: {e}")
    try:
        r = _neon_query(
            "SELECT user_id, first_name, last_name, username, first_seen "
            "FROM bot_known_users ORDER BY first_seen DESC"
        )
        rows = r.get('rows', [])
    except Exception as e:
        logger.error(f"Failed to load known users: {e}")
        rows = []
    if not rows:
        send_message(chat_id, "\U0001f4ed <b>\u1798\u17b7\u1793\u178f\u17b6\u1793\u1798\u17b6\u1793\u17a2\u17d2នក\u1794្រ\u17be\u179f\u1794្រ\u17be</b>",
                     parse_mode="HTML", reply_to_message_id=False)
        return
    total = len(rows)
    lines = [f"👥 អ្នកប្រើប្រាស់សរុប: {total}", ""]
    for i, row in enumerate(rows, 1):
        first    = row.get('first_name') or ''
        last     = row.get('last_name') or ''
        full_name = f"{first} {last}".strip() or 'N/A'
        uname    = row.get('username') or ''
        uname_str = f"@{uname}" if uname else '—'
        uid      = row.get('user_id')
        lines.append(f"{i}. {full_name}")
        lines.append(f"   🔖 {uname_str}")
        lines.append(f"   🪪 {uid}")
        lines.append("")
    txt      = "\n".join(lines).encode('utf-8')
    import datetime as _dt
    filename = f"users_{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
    result   = _send_document_bytes(chat_id, txt, filename, caption=f"\U0001f465 \u1794\u1789\u17d2\u1787\u17b8\u17a2\u17d2\u1793\u1780\u1794្រ\u17be\u179f\u1794្រ\u17be \u2014 {total} \u1793\u17b6\u1780")
    if not result:
        send_message(chat_id, "\u274c \u1794រ\u17b6\u1787\u1799\u178f្រ\u1780\u17d2ន\u1780ារផ្ញើស\u200bអ\u178f្ថ\u200bហ\u17bdរ",
                     reply_to_message_id=False)

def _show_delete_type_menu_inline(chat_id, user_id=None):
    uid = user_id if user_id is not None else chat_id
    types = [
        t for t in accounts_data.get('account_types', {}).keys()
        if len(accounts_data['account_types'].get(t, [])) > 0
    ]
    if not types:
        _settings_edit(chat_id, uid, "⚠️ <b>មិនមានប្រភេទ Account ណាមួយទេ!</b>",
                       {'inline_keyboard': [[{'text': '↩️ ត្រឡប់', 'callback_data': 's:main'}]]})
        return
    rows = []
    for i in range(0, len(types), 2):
        row = []
        for t in types[i:i+2]:
            count = len(accounts_data['account_types'].get(t, []))
            price = accounts_data.get('prices', {}).get(t, 0)
            label = f"{_short_label(t)} ({count} · ${price})"
            row.append({'text': label, 'callback_data': f"dts:{_type_callback_id(t)}"})
        rows.append(row)
    rows.append([{'text': '↩️ ត្រឡប់', 'callback_data': 's:main'}])
    _settings_edit(chat_id, uid,
                   "🗑 <b>ជ្រើសរើសប្រភេទ Account ដែលចង់លុប៖</b>",
                   {'inline_keyboard': rows})

def _export_buyers_report_inline(chat_id):
    try:
        r = _neon_query(
            "SELECT ph.user_id, ph.account_type, ph.quantity, ph.total_price, "
            "ph.accounts, ph.purchased_at, "
            "ku.first_name, ku.last_name, ku.username "
            "FROM bot_purchase_history ph "
            "LEFT JOIN bot_known_users ku ON ku.user_id = ph.user_id "
            "ORDER BY ph.user_id, ph.purchased_at DESC"
        )
        rows = r.get('rows', []) or []
        if not rows:
            send_message(chat_id, "មិនមានទិន្នន័យ​ទិញ​នៅឡើយ​ទេ។", reply_to_message_id=False)
            return
        grouped = {}
        for row in rows:
            uid = str(row.get('user_id'))
            grouped.setdefault(uid, {
                'first_name': row.get('first_name') or '',
                'last_name':  row.get('last_name') or '',
                'username':   row.get('username') or '',
                'purchases':  []
            })
            accs = row.get('accounts') or []
            if isinstance(accs, str):
                try:
                    accs = json.loads(accs)
                except Exception:
                    accs = []
            emails = [str(a.get('email', '')) for a in accs if isinstance(a, dict) and a.get('email')]
            grouped[uid]['purchases'].append({
                'type': row.get('account_type') or '',
                'qty': row.get('quantity') or 0,
                'price': row.get('total_price') or 0,
                'when': str(row.get('purchased_at') or ''),
                'emails': emails
            })
        lines = []
        import datetime as _dt
        total_coupons = 0
        for uid, info in grouped.items():
            full_name = (info['first_name'] + ' ' + info['last_name']).strip() or '(no name)'
            seen_emails = set()
            all_emails  = []
            for p in info['purchases']:
                for em in p['emails']:
                    if em.lower() not in seen_emails:
                        seen_emails.add(em.lower())
                        all_emails.append(em)
            total_coupons += len(all_emails)
            lines.append(f"ឈ្មោះ : {full_name}")
            lines.append(f"ID    : {uid}")
            lines.append("")
            if all_emails:
                for em in all_emails:
                    lines.append(em)
            else:
                lines.append("—")
            lines.append("")
        txt      = "\n".join(lines).encode('utf-8')
        filename = f"buyers_{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
        result   = _send_document_bytes(
            chat_id, txt, filename,
            caption=f"📋 របាយការណ៍ទិញ — {len(grouped)} នាក់, {total_coupons} គូប៉ុង"
        )
        if not result:
            send_message(chat_id, "❌ បរាជ័យក្នុងការផ្ញើ​ឯកសារ", reply_to_message_id=False)
        send_admin_settings_menu(chat_id)
    except Exception as e:
        logger.error(f"buyers export failed: {e}")
        send_message(chat_id, f"❌ Error: <code>{html.escape(str(e))}</code>",
                     parse_mode="HTML", reply_to_message_id=False)

def _show_admins_inline(chat_id, user_id=None):
    uid    = user_id or chat_id
    extras = sorted(EXTRA_ADMIN_IDS)
    extras_str = "\n".join(f"• <code>{x}</code>" for x in extras) if extras else "(គ្មាន)"
    text_msg = (
        f"👑 <b>Admin បឋម៖</b> <code>{ADMIN_ID}</code>\n\n"
        f"➕ <b>Admin បន្ថែម៖</b>\n{extras_str}"
    )
    _settings_edit(chat_id, uid, text_msg, SETTINGS_ADM_IKB)

def _show_channel_inline(chat_id, user_id=None):
    uid     = user_id or chat_id
    current = CHANNEL_ID if CHANNEL_ID else "(មិនទាន់កំណត់)"
    text_msg = f"📢 <b>Channel ID បច្ចុប្បន្ន៖</b>\n<code>{html.escape(str(current))}</code>"
    _settings_edit(chat_id, uid, text_msg, SETTINGS_CH_IKB)

def _show_payment_inline(chat_id, user_id=None):
    uid      = user_id or chat_id
    text_msg = f"💳 <b>ឈ្មោះ Payment បច្ចុប្បន្ន៖</b>\n<code>{html.escape(PAYMENT_NAME or '(មិនទាន់កំណត់)')}</code>"
    _settings_edit(chat_id, uid, text_msg, SETTINGS_PAY_IKB)

def _show_bakong_inline(chat_id, user_id=None):
    uid  = user_id or chat_id
    full = BAKONG_TOKEN if BAKONG_TOKEN else "(មិនទាន់កំណត់)"
    text_msg = f"🔑 <b>Bakong Token បច្ចុប្បន្ន៖</b>\n<code>{html.escape(full)}</code>"
    _settings_edit(chat_id, uid, text_msg, SETTINGS_BAK_IKB)

def _show_maintenance_inline(chat_id, user_id=None):
    uid      = user_id or chat_id
    status   = "🔴 បិទ" if MAINTENANCE_MODE else "🟢 បើក"
    text_msg = f"🛠 <b>ស្ថានភាព Bot បច្ចុប្បន្ន៖</b> {status}"
    _settings_edit(chat_id, uid, text_msg, _build_settings_maint_ikb())

def _start_add_account_flow(chat_id, user_id, message_id=None):
    with _data_lock:
        user_sessions[user_id] = {'state': 'waiting_for_accounts'}
    save_sessions_async()
    _settings_edit(
        chat_id, user_id,
        "➕ <b>បន្ថែម Account</b>\n\n"
        "📧 ផ្ញើ Email ម្តងមួយបន្ទាត់:\n\n"
        "<code>l1jebywyzos2@10mail.info\nabc123@gmail.com</code>\n\n"
        "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
        SETTINGS_CANCEL_IKB,
    )

def _handle_admin_settings_input(chat_id, user_id, message_id, key, text):
    global PAYMENT_NAME, BAKONG_TOKEN, khqr_client, CHANNEL_ID, EXTRA_ADMIN_IDS, CLONE_BOT_TOKEN, TRANSLATE_BOT_TOKEN

    raw         = (text or '').strip()
    sess        = user_sessions.get(user_id, {})
    return_menu = sess.get('settings_return', 'main')

    def _finish_input(goto='main'):
        """Clear session and navigate settings panel."""
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        _dispatch_settings_panel(chat_id, user_id, goto)

    cancel_words = {'បោះបង់', '🚫 បោះបង់'}
    if raw in cancel_words:
        _finish_input(return_menu)
        return True

    if raw == BTN_BACK_SETTINGS:
        _finish_input('main')
        return True

    if key == 'payment':
        if not raw:
            _settings_edit(chat_id, user_id,
                "💳 <b>ឈ្មោះ Payment</b>\n\nសូមផ្ញើឈ្មោះ Payment ថ្មី\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                SETTINGS_CANCEL_IKB)
            return True
        PAYMENT_NAME = raw
        set_setting('PAYMENT_NAME', PAYMENT_NAME)
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        _show_payment_inline(chat_id, user_id)
        return True

    if key == 'bakong':
        if not raw:
            _settings_edit(chat_id, user_id,
                "🔑 <b>Bakong Token</b>\n\nសូមផ្ញើ Token ថ្មី\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                SETTINGS_CANCEL_IKB)
            return True
        try:
            new_client = KHQR(raw)
        except Exception as e:
            _settings_edit(chat_id, user_id,
                f"❌ <b>Token មិនត្រឹមត្រូវ</b>\n\n<code>{html.escape(str(e))}</code>\n\n"
                "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>", SETTINGS_CANCEL_IKB)
            return True
        BAKONG_TOKEN = raw
        khqr_client  = new_client
        set_setting('BAKONG_TOKEN', raw)
        delete_message_async(chat_id, message_id)
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        _show_bakong_inline(chat_id, user_id)
        return True

    if key == 'channel':
        if not raw:
            _settings_edit(chat_id, user_id,
                "📢 <b>Channel ID</b>\n\nសូមផ្ញើ Channel ID (ឧ. <code>-1001234567890</code>)\n"
                "ឬ <code>off</code> ដើម្បីបិទ\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                SETTINGS_CANCEL_IKB)
            return True
        if raw.lower() in ('off', 'none', 'clear', 'delete', 'remove'):
            CHANNEL_ID = ""
            set_setting('TELEGRAM_CHANNEL_ID', '')
        else:
            CHANNEL_ID = raw
            set_setting('TELEGRAM_CHANNEL_ID', raw)
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        _show_channel_inline(chat_id, user_id)
        return True

    if key in ('admin_add', 'admin_remove'):
        action = 'add' if key == 'admin_add' else 'remove'
        try:
            target_id = int(raw)
        except ValueError:
            _settings_edit(chat_id, user_id,
                "❌ <b>user_id ត្រូវតែជាលេខ</b>\n\nសូមផ្ញើ Telegram User ID\n\n"
                "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>", SETTINGS_CANCEL_IKB)
            return True
        if target_id == ADMIN_ID:
            _settings_edit(chat_id, user_id,
                "ℹ️ Admin បឋមមិនអាចកែប្រែបានទេ\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                SETTINGS_CANCEL_IKB)
            return True
        if action == 'add':
            EXTRA_ADMIN_IDS.add(target_id)
        else:
            EXTRA_ADMIN_IDS.discard(target_id)
        set_setting('EXTRA_ADMIN_IDS', json.dumps(sorted(EXTRA_ADMIN_IDS)))
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        _show_admins_inline(chat_id, user_id)
        return True

    if key == 'clone_token':
        if not raw:
            _settings_edit(chat_id, user_id,
                "🔑 <b>TTS Bot Token</b>\n\nសូមផ្ញើ Bot Token\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                SETTINGS_CANCEL_IKB)
            return True
        try:
            test_resp = http.get(f"https://api.telegram.org/bot{raw}/getMe", timeout=10).json()
            if not test_resp.get('ok'):
                _settings_edit(chat_id, user_id,
                    f"❌ <b>Token មិនត្រឹមត្រូវ</b>\n\n{test_resp.get('description', 'Unknown error')}\n\n"
                    "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>", SETTINGS_CANCEL_IKB)
                return True
        except Exception as e:
            _settings_edit(chat_id, user_id,
                f"❌ <b>មិនអាចភ្ជាប់ Telegram</b>\n\n{e}\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                SETTINGS_CANCEL_IKB)
            return True
        CLONE_BOT_TOKEN = raw
        set_setting('CLONE_BOT_TOKEN', raw)
        delete_message_async(chat_id, message_id)
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        _show_clone_bot_menu(chat_id, user_id)
        return True

    if key == 'translate_token':
        if not raw:
            _settings_edit(chat_id, user_id,
                "🔑 <b>Translate Bot Token</b>\n\nសូមផ្ញើ Bot Token\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                SETTINGS_CANCEL_IKB)
            return True
        try:
            test_resp = http.get(f"https://api.telegram.org/bot{raw}/getMe", timeout=10).json()
            if not test_resp.get('ok'):
                _settings_edit(chat_id, user_id,
                    f"❌ <b>Token មិនត្រឹមត្រូវ</b>\n\n{test_resp.get('description', 'Unknown error')}\n\n"
                    "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>", SETTINGS_CANCEL_IKB)
                return True
        except Exception as e:
            _settings_edit(chat_id, user_id,
                f"❌ <b>មិនអាចភ្ជាប់ Telegram</b>\n\n{e}\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                SETTINGS_CANCEL_IKB)
            return True
        TRANSLATE_BOT_TOKEN = raw
        set_setting('TRANSLATE_BOT_TOKEN', raw)
        delete_message_async(chat_id, message_id)
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        _show_translate_bot_menu(chat_id, user_id)
        return True

    if key == 'broadcast':
        if not message_id:
            _settings_edit(chat_id, user_id,
                "📢 <b>ផ្សាយព័ត៌មាន</b>\n\nសូមផ្ញើសារដែលចង់ផ្សាយ\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                SETTINGS_CANCEL_IKB)
            return True
        is_text_only = bool(raw)
        with _data_lock:
            user_sessions[user_id] = {
                'state': 'broadcast_confirm',
                'broadcast_message_id': message_id,
                'broadcast_chat_id': chat_id,
                'broadcast_use_copy': is_text_only,
            }
        save_sessions_async()
        _settings_edit(
            chat_id, user_id,
            "❓ <b>តើអ្នកប្រាកដជាចង់ផ្សាយសារខាងលើនេះទៅអ្នកប្រើប្រាស់ទាំងអស់មែនទេ?</b>",
            BROADCAST_CONFIRM_KEYBOARD,
        )
        return True

    return False

def _run_broadcast(admin_chat_id, source_message_id, use_copy=False):
    """Broadcast a message to all known users via Pyrogram copy/forward."""
    try:
        try:
            r = _neon_query("SELECT user_id FROM bot_known_users")
            rows = r.get('rows', []) or []
        except Exception as e:
            logger.error(f"Broadcast: failed to load users: {e}")
            send_message(admin_chat_id,
                         f"❌ មិន​អាច​ផ្ទុក​បញ្ជី​អ្នក​ប្រើ​ប្រាស់​បាន: <code>{html.escape(str(e))}</code>",
                         parse_mode="HTML", reply_to_message_id=False,
                         reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
            return
        total = len(rows)
        sent = failed = blocked = 0
        for row in rows:
            uid = row.get('user_id')
            if not uid:
                continue
            try:
                if use_copy:
                    result = _pyro_sync(
                        app.copy_message(uid, admin_chat_id, source_message_id),
                        timeout=15, reraise=True
                    )
                else:
                    result = _pyro_sync(
                        app.forward_messages(uid, admin_chat_id, source_message_id),
                        timeout=15, reraise=True
                    )
                if result:
                    sent += 1
                else:
                    failed += 1
            except Exception as e:
                err = str(e).lower()
                if any(w in err for w in ('blocked', 'deactivated', 'invalid', 'forbidden',
                                          'peer', 'not found', 'chat not found')):
                    blocked += 1
                else:
                    failed += 1
                    logger.warning(f"Broadcast to {uid} error: {e}")
            time.sleep(0.05)
        summary = (
            "📢 <b>ផ្សាយ​សារ​បាន​ចប់</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👥 សរុប:         {total}\n"
            f"✅ ផ្ញើ​ជោគជ័យ:   {sent}\n"
            f"⛔ បាន​ប្លុក/លុប:  {blocked}\n"
            f"❌ បរាជ័យ:        {failed}"
        )
        back_ikb = {'inline_keyboard': [[{'text': '↩️ ត្រឡប់ទៅកំណត់', 'callback_data': 's:main'}]]}
        send_message(admin_chat_id, summary, parse_mode="HTML",
                     reply_to_message_id=False, reply_markup=back_ikb)
    except Exception as e:
        logger.error(f"Broadcast crashed: {e}")
        try:
            back_ikb = {'inline_keyboard': [[{'text': '↩️ ត្រឡប់ទៅកំណត់', 'callback_data': 's:main'}]]}
            send_message(admin_chat_id,
                         f"❌ Broadcast error: <code>{html.escape(str(e))}</code>",
                         parse_mode="HTML", reply_to_message_id=False,
                         reply_markup=back_ikb)
        except Exception:
            pass

# ── QR / payment flow helpers ─────────────────────────────────────────────────
CHECK_PAYMENT_KEYBOARD = {
    'inline_keyboard': [[
        {'text': '🚫 បោះបង់', 'callback_data': 'cancel_purchase'},
        {'text': '✅ ពិនិត្យការបង់ប្រាក់', 'callback_data': 'check_payment'}
    ]]
}

def _generate_and_send_qr(chat_id, user_id, session):
    try:
        img_bytes, md5_or_err, qr_string = generate_payment_qr(session['total_price'])
        if not img_bytes:
            err_detail = md5_or_err or "មិនដឹងមូលហេតុ"
            logger.error(f"QR generation returned None: {err_detail}")
            if str(user_id) == str(ADMIN_ID):
                send_message(chat_id, f"❌ *QR បរាជ័យ (Admin Debug):*\n`{err_detail}`", parse_mode="Markdown")
            else:
                send_message(chat_id, "❌ *មានបញ្ហាក្នុងការបង្កើត QR Code*\n\nសូមព្យាយាមម្តងទៀត។", parse_mode="Markdown")
                send_message(ADMIN_ID, f"⚠️ *QR Error (user {user_id}):*\n`{err_detail}`", parse_mode="Markdown")
            with _data_lock:
                if user_id in user_sessions:
                    del user_sessions[user_id]
            save_sessions_async()
            return
        md5_hash = md5_or_err
        session['md5_hash'] = md5_hash
        session['qr_sent_at'] = time.time()
        photo_resp = send_photo_bytes(chat_id, img_bytes, reply_markup=CHECK_PAYMENT_KEYBOARD)
        if photo_resp and photo_resp.get('result'):
            msg_id = photo_resp['result']['message_id']
            session['photo_message_id'] = msg_id
            session['qr_message_id'] = msg_id
        save_sessions_async()
        save_pending_payment_async(user_id, chat_id, session)
        logger.info(f"Generated QR for user {user_id}: Amount ${session['total_price']}, MD5: {md5_hash}")
    except Exception as e:
        logger.error(f"Error generating KHQR: {type(e).__name__}: {e}")
        send_message(chat_id, "❌ *មានបញ្ហាក្នុងការបង្កើត QR Code*\n\nសូមព្យាយាមម្តងទៀត។", parse_mode="Markdown")
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()

def _remind_pending_payment(chat_id, session):
    photo_msg_id = session.get('photo_message_id') or session.get('qr_message_id')
    if photo_msg_id:
        copy_message(chat_id, chat_id, photo_msg_id, reply_markup=CHECK_PAYMENT_KEYBOARD)
    else:
        send_message(chat_id,
                     "⚠️ លោកអ្នកមានការទិញដែលកំពុងរង់ចាំការបង់ប្រាក់។ សូមបញ្ចប់ ឬ ចុច 🚫 បោះបង់ ដើម្បីចាប់ផ្តើមថ្មី។",
                     reply_to_message_id=False)

def send_purchase_notification(message):
    if CHANNEL_ID:
        send_message(CHANNEL_ID, message, parse_mode="HTML",
                     reply_to_message_id=False, reply_markup="no_keyboard")
    else:
        send_message(ADMIN_ID, message, parse_mode="HTML",
                     reply_to_message_id=False, reply_markup=ADMIN_REPLY_KEYBOARD)

# ── Deliver accounts after confirmed payment ───────────────────────────────────
def deliver_accounts(chat_id, user_id, session, payment_data=None, user_name=''):
    account_type = session['account_type']
    quantity     = session['quantity']

    photo_message_id = session.get('photo_message_id')
    if photo_message_id:
        delete_message_async(chat_id, photo_message_id)
    qr_message_id = session.get('qr_message_id')
    if qr_message_id:
        delete_message_async(chat_id, qr_message_id)

    with _data_lock:
        if account_type not in accounts_data['account_types']:
            available_count     = None
            delivered_accounts  = None
        else:
            available_accounts  = accounts_data['account_types'][account_type]
            available_count     = len(available_accounts)
            if available_count < quantity:
                delivered_accounts = None
            else:
                delivered_accounts = available_accounts[:quantity]
                accounts_data['account_types'][account_type] = available_accounts[quantity:]
                if user_id in user_sessions:
                    del user_sessions[user_id]

    if delivered_accounts is None:
        if available_count is None:
            send_message(chat_id, f"❌ *មានបញ្ហា!*\n\nគ្មាន Account ប្រភេទ {account_type} ក្នុងស្តុក។",
                         parse_mode="Markdown")
        else:
            send_message(chat_id, f"❌ *មានបញ្ហា!*\n\nសុំទោស! មានត្រឹមតែ {available_count} Accounts នៅក្នុងស្តុក។",
                         parse_mode="Markdown")
        return

    save_data()
    save_purchase_history_async(user_id, account_type, quantity,
                                session.get('total_price', 0), delivered_accounts)

    accounts_message = f'<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji> <b>ការទិញបានបញ្ជាក់ដោយជោគជ័យ</b>\n\n'
    accounts_message += '<b>គូប៉ុងរបស់អ្នក៖ <tg-emoji emoji-id="5470177992950946662">👇</tg-emoji></b>\n\n'
    for account in delivered_accounts:
        if 'email' in account:
            accounts_message += f"{account['email']}\n"
        else:
            accounts_message += f"{account.get('phone', '')} | {account.get('password', '')}\n"
    accounts_message += f"\n<i>សូមអរគុណសម្រាប់ការទិញ <tg-emoji emoji-id=\"5897474556834091884\">🙏</tg-emoji></i>"

    send_message(chat_id, accounts_message, parse_mode="HTML",
                 message_effect_id="5046509860389126442", reply_markup=_main_kb(user_id))
    send_account_selection_inline(chat_id)

    try:
        import datetime
        cambodia_tz = datetime.timezone(datetime.timedelta(hours=7))
        now_str     = datetime.datetime.now(cambodia_tz).strftime("%d/%m/%Y %H:%M")
        pd          = payment_data or {}
        from_account = pd.get('fromAccountId') or pd.get('hash') or 'N/A'
        memo         = pd.get('memo') or 'គ្មាន'
        ref          = pd.get('externalRef') or pd.get('transactionId') or pd.get('md5') or 'N/A'
        amount       = session.get('total_price', 0)
        buyer_label  = f"{user_name} ({user_id})" if user_name else str(user_id)
        admin_msg = (
            "🎉 ទទួលបានការបង់ប្រាក់ជោគជ័យ\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ឈ្មោះអ្នកទិញ(ID): {buyer_label}\n"
            f"💵 ទឹកប្រាក់: {amount} USD\n"
            f"👤 ពីធនាគារ: {from_account}\n"
            f"📝 ចំណាំ: {memo}\n"
            f"🧾 លេខយោង: {ref}\n"
            f"⏰ ម៉ោង: {now_str}"
        )
        send_purchase_notification(admin_msg)
    except Exception as e:
        logger.error(f"Failed to send admin payment notification: {e}")

    save_sessions_async()
    logger.info(f"Payment confirmed and {quantity} accounts delivered to user {user_id}")

# ── Callback query handler ────────────────────────────────────────────────────
def handle_callback_query(update):
    global CHANNEL_ID, MAINTENANCE_MODE, CLONE_BOT_TOKEN, TRANSLATE_BOT_TOKEN, TRANSLATE_BOT_ACTIVE
    _set_reply_to_id(None)
    try:
        callback_query = update.get('callback_query')
        if not callback_query:
            return
        chat_id       = callback_query['message']['chat']['id']
        callback_data = callback_query.get('data')
        user          = callback_query.get('from', {})
        user_id       = user.get('id')
        logger.info(f"Received callback from user {user.get('first_name', 'Unknown')} (ID: {user_id}): {callback_data}")
        notify_admin_new_user(user)

        if callback_data.startswith('buy:') or callback_data.startswith('buy_'):
            existing_session = user_sessions.get(user_id)
            if existing_session and existing_session.get('state') == 'payment_pending':
                answer_callback(callback_query['id'])
                delete_message_async(chat_id, callback_query['message']['message_id'])
                _remind_pending_payment(chat_id, existing_session)
                return
            if callback_data.startswith('buy:'):
                account_type = _account_type_from_callback_id(callback_data[4:])
            else:
                account_type = callback_data.replace('buy_', '')
            if not account_type:
                answer_callback(callback_query['id'], 'ប្រភេទនេះមិនមានទៀតហើយ។ សូមចាប់ផ្តើមម្តងទៀត។', True)
                return
            if account_type in accounts_data['account_types']:
                with _data_lock:
                    accounts  = accounts_data['account_types'][account_type]
                    count     = len(accounts)
                    price     = accounts_data['prices'].get(account_type, 0)
                if count > 0:
                    answer_callback(callback_query['id'])
                    with _data_lock:
                        user_sessions[user_id] = {
                            'state': 'waiting_for_quantity',
                            'account_type': account_type,
                            'price': price,
                            'available_count': count
                        }
                    save_sessions_async()
                    qty_buttons = [{'text': str(n), 'callback_data': f'qty:{n}'} for n in range(1, count + 1)]
                    qty_rows    = [qty_buttons[i:i+4] for i in range(0, len(qty_buttons), 4)]
                    qty_keyboard = {'inline_keyboard': qty_rows}
                    send_message(chat_id, "*សូមជ្រើសរើសចំនួនដែលចង់ទិញ៖*",
                                 reply_to_message_id=False, parse_mode="Markdown", reply_markup=qty_keyboard)
                    delete_message_async(chat_id, callback_query['message']['message_id'])
                    logger.info(f"User {user_id} selected account type {account_type}, waiting for quantity input")
                else:
                    answer_callback(callback_query['id'], f"សុំទោស! {account_type} អស់ស្តុកហើយ។", True)

        elif callback_data.startswith('out_of_stock:') or callback_data.startswith('out_of_stock_'):
            answer_callback(callback_query['id'])
            delete_message_async(chat_id, callback_query['message']['message_id'])
            send_account_selection_inline(chat_id)

        elif callback_data == 'confirm_buy':
            session = user_sessions.get(user_id)
            if not session or session.get('state') != 'waiting_for_confirmation':
                answer_callback(callback_query['id'], 'មិនមានការទិញដែលកំពុងរង់ចាំ។', True)
                return
            answer_callback(callback_query['id'], 'កំពុងបង្កើត QR...')
            with _data_lock:
                session['state'] = 'payment_pending'
            summary_message_id = callback_query['message']['message_id']
            delete_message_async(chat_id, summary_message_id)
            try:
                img_bytes, md5_or_err, qr_string = generate_payment_qr(session['total_price'])
                if not img_bytes:
                    err_detail = md5_or_err or "មិនដឹងមូលហេតុ"
                    if str(user_id) == str(ADMIN_ID):
                        send_message(chat_id, f"❌ *QR បរាជ័យ (Admin Debug):*\n`{err_detail}`", parse_mode="Markdown")
                    else:
                        send_message(chat_id, "❌ *មានបញ្ហាក្នុងការបង្កើត QR Code*\n\nសូមព្យាយាមម្តងទៀត។", parse_mode="Markdown")
                        send_message(ADMIN_ID, f"⚠️ *QR Error (user {user_id}):*\n`{err_detail}`", parse_mode="Markdown")
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                    return
                md5_hash = md5_or_err
                session['md5_hash'] = md5_hash
                session['qr_sent_at'] = time.time()
                rm_resp = send_message(chat_id, ".", reply_to_message_id=False, reply_markup={'remove_keyboard': True})
                if rm_resp and rm_resp.get('result'):
                    delete_message_async(chat_id, rm_resp['result']['message_id'])
                photo_resp = send_photo_bytes(chat_id, img_bytes, reply_markup=CHECK_PAYMENT_KEYBOARD)
                if photo_resp and photo_resp.get('result'):
                    msg_id = photo_resp['result']['message_id']
                    session['photo_message_id'] = msg_id
                    session['qr_message_id'] = msg_id
                save_sessions_async()
                save_pending_payment_async(user_id, chat_id, session)
                logger.info(f"Generated QR for user {user_id}: Amount ${session['total_price']}, MD5: {md5_hash}")
            except Exception as e:
                logger.error(f"Error generating KHQR: {type(e).__name__}: {e}")
                send_message(chat_id, "❌ *មានបញ្ហាក្នុងការបង្កើត QR Code*\n\nសូមព្យាយាមម្តងទៀត។", parse_mode="Markdown")
                with _data_lock:
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                save_sessions_async()
            return

        elif callback_data.startswith('dts:') and is_admin(user_id):
            type_name = _account_type_from_callback_id(callback_data[4:]) or callback_data[4:]
            if type_name not in accounts_data.get('account_types', {}):
                answer_callback(callback_query['id'], 'ប្រភេទនេះមិនមានទៀតហើយ!', True)
                return
            answer_callback(callback_query['id'])
            _admin_settings_msg[user_id] = callback_query['message']['message_id']
            count    = len(accounts_data['account_types'].get(type_name, []))
            price    = accounts_data.get('prices', {}).get(type_name, 0)
            confirm_cb = f"dtc:{_type_callback_id(type_name)}"
            keyboard = {'inline_keyboard': [[
                {'text': '✅ បញ្ជាក់លុប', 'callback_data': confirm_cb},
                {'text': '🚫 បោះបង់',     'callback_data': 'dtcancel'}
            ]]}
            _settings_edit(chat_id, user_id,
                f"⚠️ <b>តើអ្នកពិតជាចង់លុបប្រភេទ Account នេះមែនទេ?</b>\n\n"
                f"<blockquote>🔹 ប្រភេទ: {html.escape(type_name)}\n🔹 ចំនួន Account: {count}\n🔹 តម្លៃ: ${price}</blockquote>\n\n"
                f"⚠️ Account ទាំងអស់នឹងត្រូវបានលុបចោលជាអចិន្ត្រៃយ៍!",
                keyboard)
            return

        elif callback_data.startswith('dtc:') and is_admin(user_id):
            type_name = _account_type_from_callback_id(callback_data[4:]) or callback_data[4:]
            if type_name not in accounts_data.get('account_types', {}):
                answer_callback(callback_query['id'], 'ប្រភេទនេះមិនមានទៀតហើយ!', True)
                return
            answer_callback(callback_query['id'])
            _admin_settings_msg[user_id] = callback_query['message']['message_id']
            count = len(accounts_data['account_types'].pop(type_name, []))
            accounts_data.get('prices', {}).pop(type_name, None)
            accounts_data['accounts'] = [a for a in accounts_data.get('accounts', []) if a.get('type') != type_name]
            save_data()
            _settings_edit(chat_id, user_id,
                f"✅ <b>បានលុបប្រភេទ Account <code>{html.escape(type_name)}</code> ចំនួន {count} records ដោយជោគជ័យ!</b>",
                {'inline_keyboard': [[{'text': '↩️ ត្រឡប់', 'callback_data': 's:del_type'}]]})
            return

        elif callback_data == 'dtcancel' and is_admin(user_id):
            answer_callback(callback_query['id'])
            _admin_settings_msg[user_id] = callback_query['message']['message_id']
            _show_delete_type_menu_inline(chat_id, user_id)
            return

        elif callback_data.startswith('lang_') and is_admin(user_id):
            lang_code = callback_data[5:]
            lang_name = TRANSLATE_LANGUAGES.get(lang_code, lang_code)
            answer_callback(callback_query['id'], f"✅ {lang_name}")
            with _data_lock:
                user_sessions[user_id] = {'state': 'translate_mode', 'lang_code': lang_code, 'lang_name': lang_name}
            save_sessions_async()
            delete_message_async(chat_id, callback_query['message']['message_id'])
            send_message(
                chat_id,
                f"✅ <b>ភាសាបកប្រែ៖ {lang_name}</b>\n\n"
                f"💬 សូមផ្ញើអក្សរដែលចង់បកប្រែ។\n"
                f"↩️ ចុច <b>ត្រឡប់ទៅកំណត់</b> ដើម្បីចេញ។",
                parse_mode="HTML",
                reply_to_message_id=False,
                reply_markup=TRANSLATE_SUBMENU_KEYBOARD
            )
            return

        elif callback_data.startswith('adm:') and is_admin(user_id):
            global PAYMENT_NAME, BAKONG_TOKEN, khqr_client, CHANNEL_ID, EXTRA_ADMIN_IDS, MAINTENANCE_MODE
            action   = callback_data[4:]
            answer_callback(callback_query['id'])
            menu_msg_id = callback_query['message']['message_id']

            if action == 'close':
                delete_message_async(chat_id, menu_msg_id)
                return
            if action == 'back':
                delete_message_async(chat_id, menu_msg_id)
                send_admin_settings_menu(chat_id)
                return
            if action == 'add_account':
                delete_message_async(chat_id, menu_msg_id)
                _start_add_account_flow(chat_id, user_id, None)
                return
            if action == 'delete_type':
                delete_message_async(chat_id, menu_msg_id)
                _show_delete_type_menu_inline(chat_id, user_id)
                return
            if action == 'users':
                delete_message_async(chat_id, menu_msg_id)
                _show_users_list_inline(chat_id)
                return
            if action == 'buyers':
                delete_message_async(chat_id, menu_msg_id)
                _export_buyers_report_inline(chat_id)
                return
            if action == 'payment':
                delete_message_async(chat_id, menu_msg_id)
                _show_payment_inline(chat_id)
                return
            if action == 'payment_set':
                delete_message_async(chat_id, menu_msg_id)
                _prompt_admin_input(chat_id, user_id, 'payment',
                    f"💳 ឈ្មោះ Payment បច្ចុប្បន្ន៖ <b>{html.escape(PAYMENT_NAME or '(មិនទាន់កំណត់)')}</b>\n\nសូមផ្ញើឈ្មោះ Payment ថ្មី៖")
                return
            if action == 'bakong':
                delete_message_async(chat_id, menu_msg_id)
                _show_bakong_inline(chat_id)
                return
            if action == 'bakong_set':
                delete_message_async(chat_id, menu_msg_id)
                _prompt_admin_input(chat_id, user_id, 'bakong',
                    "🔑 សូមផ្ញើ Bakong Token ថ្មី៖\n<i>(សារនឹងត្រូវលុបដោយស្វ័យប្រវត្តិ)</i>")
                return
            if action == 'channel':
                delete_message_async(chat_id, menu_msg_id)
                _show_channel_inline(chat_id)
                return
            if action == 'channel_set':
                delete_message_async(chat_id, menu_msg_id)
                _prompt_admin_input(chat_id, user_id, 'channel',
                    "📢 សូមផ្ញើ Channel ID ថ្មី (ឧ. <code>-1001234567890</code>)\nឬ <code>off</code> ដើម្បីលុប")
                return
            if action == 'channel_clear':
                CHANNEL_ID = ""
                set_setting('TELEGRAM_CHANNEL_ID', '')
                delete_message_async(chat_id, menu_msg_id)
                send_message(chat_id, "✅ បានលុប Channel ID",
                             reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                return
            if action == 'admins':
                delete_message_async(chat_id, menu_msg_id)
                _show_admins_inline(chat_id)
                return
            if action == 'admin_add':
                delete_message_async(chat_id, menu_msg_id)
                _prompt_admin_input(chat_id, user_id, 'admin_add',
                    "👑 សូមផ្ញើ user_id របស់អ្នកដែលចង់បន្ថែមជា admin (ជាលេខ)៖")
                return
            if action == 'admin_remove':
                delete_message_async(chat_id, menu_msg_id)
                _prompt_admin_input(chat_id, user_id, 'admin_remove',
                    "👑 សូមផ្ញើ user_id របស់ admin ដែលចង់ដក (ជាលេខ)៖")
                return
            if action == 'maintenance':
                delete_message_async(chat_id, menu_msg_id)
                _show_maintenance_inline(chat_id)
                return
            if action == 'maint_on':
                MAINTENANCE_MODE = True
                set_setting('MAINTENANCE_MODE', 'true')
                delete_message_async(chat_id, menu_msg_id)
                send_message(chat_id, "✅ <b>Maintenance mode ON</b>", parse_mode="HTML",
                             reply_to_message_id=False, reply_markup=_main_kb(user_id))
                return
            if action == 'maint_off':
                MAINTENANCE_MODE = False
                set_setting('MAINTENANCE_MODE', 'false')
                delete_message_async(chat_id, menu_msg_id)
                send_message(chat_id, "✅ <b>Maintenance mode OFF</b> — Bot ដំណើរការធម្មតាហើយ",
                             parse_mode="HTML", reply_to_message_id=False, reply_markup=_main_kb(user_id))
                return
            return

        elif callback_data.startswith('s:') and is_admin(user_id):
            action = callback_data[2:]
            answer_callback(callback_query['id'])
            # Always refresh stored panel message_id from the live callback
            _admin_settings_msg[user_id] = callback_query['message']['message_id']
            if action == 'cancel_input':
                sess = user_sessions.get(user_id, {})
                return_key = sess.get('settings_return', 'main')
                with _data_lock:
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                save_sessions_async()
                _dispatch_settings_panel(chat_id, user_id, return_key)
                return
            if action == 'main':
                _settings_edit(chat_id, user_id, _settings_main_text(), SETTINGS_MAIN_IKB)
                return
            if action == 'pay':
                _show_payment_inline(chat_id, user_id)
                return
            if action == 'pay_edit':
                _prompt_admin_input(chat_id, user_id, 'payment',
                    "💳 ស្អមផ្ញើស​ <b>អត្ថកំនួយ Payment</b>​ ថ្មី (1–60 តួអក្សរ)ខ្មែរ:", 'pay')
                return
            if action == 'bak':
                _show_bakong_inline(chat_id, user_id)
                return
            if action == 'bak_edit':
                _prompt_admin_input(chat_id, user_id, 'bakong',
                    "🔑 ស្អមផ្ញើស​ <b>Bakong Token</b>​ ថ្មី:", 'bak')
                return
            if action == 'ch':
                _show_channel_inline(chat_id, user_id)
                return
            if action == 'ch_edit':
                _prompt_admin_input(chat_id, user_id, 'channel',
                    "📢 ស្អមផ្ញើស <b>Channel ID</b>​ ថ្មី (ហណ. <code>-1001234567890</code>):", 'ch')
                return
            if action == 'ch_clear':
                CHANNEL_ID = ""
                set_setting('TELEGRAM_CHANNEL_ID', '')
                _show_channel_inline(chat_id, user_id)
                return
            if action == 'adm':
                _show_admins_inline(chat_id, user_id)
                return
            if action == 'adm_add':
                _prompt_admin_input(chat_id, user_id, 'admin_add',
                    "➕ ស្អមផ្ញើស <b>Telegram User ID</b>​ ដែលចង់បន្តើមជា Admin:", 'adm')
                return
            if action == 'adm_rm':
                _prompt_admin_input(chat_id, user_id, 'admin_remove',
                    "➖ ស្អមផ្ញើស <b>Telegram User ID</b>​ ដែលចង់ដក:", 'adm')
                return
            if action == 'mnt':
                _show_maintenance_inline(chat_id, user_id)
                return
            if action == 'mnt_on':
                MAINTENANCE_MODE = True
                set_setting('MAINTENANCE_MODE', 'true')
                _show_maintenance_inline(chat_id, user_id)
                return
            if action == 'mnt_off':
                MAINTENANCE_MODE = False
                set_setting('MAINTENANCE_MODE', 'false')
                _show_maintenance_inline(chat_id, user_id)
                return
            if action == 'tts':
                _show_clone_bot_menu(chat_id, user_id)
                return
            if action == 'tts_start':
                if not CLONE_BOT_TOKEN:
                    answer_callback(callback_query['id'], text='❌ ស្អម​កំណត់ Token ជា​មុន​សិន', show_alert=True)
                    _show_clone_bot_menu(chat_id, user_id)
                    return
                _start_clone_bot(CLONE_BOT_TOKEN)
                set_setting('CLONE_BOT_ACTIVE', 'true')
                _show_clone_bot_menu(chat_id, user_id)
                return
            if action == 'tts_stop':
                _stop_clone_bot()
                set_setting('CLONE_BOT_ACTIVE', 'false')
                _show_clone_bot_menu(chat_id, user_id)
                return
            if action == 'tts_token':
                _prompt_admin_input(chat_id, user_id, 'clone_token',
                    "🔑 សូម​ផ្ញើ <b>Bot Token</b>​ របស់ 🎙 TTS Bot\n\n"
                    "<i>ទទួលពី @BotFather → /mybots → API Token</i>", 'tts')
                return
            if action == 'tts_clear':
                _stop_clone_bot()
                CLONE_BOT_TOKEN = ""
                set_setting('CLONE_BOT_TOKEN', '')
                set_setting('CLONE_BOT_ACTIVE', 'false')
                _show_clone_bot_menu(chat_id, user_id)
                return
            if action == 'tr':
                _show_translate_bot_menu(chat_id, user_id)
                return
            if action == 'tr_start':
                if not TRANSLATE_BOT_TOKEN:
                    answer_callback(callback_query['id'], text='❌ ស្អម​កំណត់ Token ជា​មុន​សិន', show_alert=True)
                    _show_translate_bot_menu(chat_id, user_id)
                    return
                _start_translate_bot(TRANSLATE_BOT_TOKEN)
                set_setting('TRANSLATE_BOT_ACTIVE', 'true')
                _show_translate_bot_menu(chat_id, user_id)
                return
            if action == 'tr_stop':
                _stop_translate_bot()
                set_setting('TRANSLATE_BOT_ACTIVE', 'false')
                _show_translate_bot_menu(chat_id, user_id)
                return
            if action == 'tr_token':
                _prompt_admin_input(chat_id, user_id, 'translate_token',
                    "🔑 សូម​ផ្ញើ <b>Bot Token</b>​ របស់ 🌐 Translate Bot\n\n"
                    "<i>ទទួលពី @BotFather → /mybots → API Token</i>", 'tr')
                return
            if action == 'tr_clear':
                _stop_translate_bot()
                TRANSLATE_BOT_TOKEN = ""
                set_setting('TRANSLATE_BOT_TOKEN', '')
                set_setting('TRANSLATE_BOT_ACTIVE', 'false')
                _show_translate_bot_menu(chat_id, user_id)
                return
            if action == 'add_acc':
                _start_add_account_flow(chat_id, user_id, callback_query['message']['message_id'])
                return
            if action == 'del_type':
                _show_delete_type_menu_inline(chat_id, user_id)
                return
            if action == 'buyers':
                _export_buyers_report_inline(chat_id)
                return
            if action == 'broadcast_confirm':
                sess = user_sessions.get(user_id, {})
                if not sess or sess.get('state') != 'broadcast_confirm':
                    return
                source_message_id = sess.get('broadcast_message_id')
                broadcast_chat_id = sess.get('broadcast_chat_id', chat_id)
                use_copy = sess.get('broadcast_use_copy', True)
                with _data_lock:
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                save_sessions_async()
                _settings_edit(chat_id, user_id, "📢 <b>ចាប់ផ្តើមផ្សាយ...</b>",
                               {'inline_keyboard': []})
                _run_background("broadcast", _run_broadcast, broadcast_chat_id, source_message_id, use_copy)
                return
            if action == 'broadcast_cancel':
                sess = user_sessions.get(user_id, {})
                if sess and sess.get('state') == 'broadcast_confirm':
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                _settings_edit(chat_id, user_id, "🚫 <b>បានបោះបង់ការផ្សាយ</b>",
                               {'inline_keyboard': [[{'text': '↩️ ត្រឡប់', 'callback_data': 's:main'}]]})
                return
            return

        elif callback_data == 'clone_feat_voice' and is_admin(user_id):
            answer_callback(callback_query['id'])
            _show_clone_bot_menu(chat_id)
            return

        elif callback_data == 'clone_feat_translate' and is_admin(user_id):
            answer_callback(callback_query['id'])
            _show_translate_bot_menu(chat_id)
            return

        elif callback_data == 'cbm_list' and is_admin(user_id):
            answer_callback(callback_query['id'])
            _show_clone_bots_list(chat_id)
            return

        elif callback_data == 'cbm_add' and is_admin(user_id):
            answer_callback(callback_query['id'])
            with _data_lock:
                user_sessions[user_id] = {'state': 'clone_add_name'}
            save_sessions_async()
            send_message(chat_id,
                "🤖 <b>បន្ថែម Clone Bot ថ្មី</b>\n\nសូមផ្ញើ <b>ឈ្មោះ</b> Clone Bot:",
                parse_mode='HTML', reply_to_message_id=False,
                reply_markup=CANCEL_INPUT_KEYBOARD)
            return

        elif callback_data.startswith('cbm:') and is_admin(user_id):
            bot_id = callback_data[4:]
            answer_callback(callback_query['id'])
            _show_clone_bot_detail(chat_id, bot_id,
                                   edit_msg_id=callback_query['message']['message_id'])
            return

        elif callback_data.startswith('cbm_start:') and is_admin(user_id):
            bot_id = callback_data[10:]
            ok = _start_clone_bot_by_id(bot_id)
            answer_callback(callback_query['id'], '🟢 ចាប់ផ្តើម!' if ok else '❌ Token មិនទាន់​កំណត់')
            _show_clone_bot_detail(chat_id, bot_id,
                                   edit_msg_id=callback_query['message']['message_id'])
            return

        elif callback_data.startswith('cbm_stop:') and is_admin(user_id):
            bot_id = callback_data[9:]
            _stop_clone_bot_by_id(bot_id)
            answer_callback(callback_query['id'], '🔴 បានបញ្ឈប់')
            _show_clone_bot_detail(chat_id, bot_id,
                                   edit_msg_id=callback_query['message']['message_id'])
            return

        elif callback_data.startswith('cbm_token:') and is_admin(user_id):
            bot_id = callback_data[10:]
            answer_callback(callback_query['id'])
            with _data_lock:
                user_sessions[user_id] = {'state': 'clone_set_token', 'bot_id': bot_id}
            save_sessions_async()
            send_message(chat_id,
                "🔑 <b>សូមផ្ញើ Bot Token ថ្មី</b>\n<i>ទទួលពី @BotFather → /mybots → API Token</i>",
                parse_mode='HTML', reply_to_message_id=False,
                reply_markup=CANCEL_INPUT_KEYBOARD)
            return

        elif callback_data.startswith('cbm_del:') and is_admin(user_id):
            bot_id = callback_data[8:]
            answer_callback(callback_query['id'])
            kb = {'inline_keyboard': [[
                {'text': '✅ បញ្ជាក់លុប', 'callback_data': f"cbm_delok:{bot_id}"},
                {'text': '🚫 បោះបង់',    'callback_data': f"cbm:{bot_id}"},
            ]]}
            _tg_api('editMessageReplyMarkup',
                    chat_id=chat_id,
                    message_id=callback_query['message']['message_id'],
                    reply_markup=kb)
            return

        elif callback_data.startswith('cbm_delok:') and is_admin(user_id):
            bot_id = callback_data[10:]
            _stop_clone_bot_by_id(bot_id)
            with _clone_bots_lock:
                _clone_bots_list[:] = [b for b in _clone_bots_list if b['id'] != bot_id]
            _save_clone_bots()
            answer_callback(callback_query['id'], '✅ បានលុបហើយ')
            _tg_api('editMessageText',
                    chat_id=chat_id,
                    message_id=callback_query['message']['message_id'],
                    text="✅ <b>បានលុប Clone Bot</b>", parse_mode='HTML',
                    reply_markup=_clone_bots_inline_kb())
            return

        elif callback_data.startswith('cbm_trl:') and is_admin(user_id):
            bot_id = callback_data[8:]
            answer_callback(callback_query['id'])
            items = list(TRANSLATE_LANGUAGES.items())
            rows  = []
            for i in range(0, len(items), 3):
                rows.append([{'text': name, 'callback_data': f"cbm_lang:{bot_id}:{code}"}
                             for code, name in items[i:i+3]])
            rows.append([
                {'text': '🔇 បិទបកប្រែ', 'callback_data': f"cbm_lang:{bot_id}:off"},
                {'text': '↩️ ត្រឡប់',   'callback_data': f"cbm:{bot_id}"},
            ])
            _tg_api('editMessageText',
                    chat_id=chat_id,
                    message_id=callback_query['message']['message_id'],
                    text="🌐 <b>ជ្រើសភាសា Default សម្រាប់ Clone Bot</b>\n\n"
                         "<i>ភាសានេះ apply ដល់អ្នកប្រើ Clone Bot ទាំងអស់ដែលមិនទាន់ជ្រើសភាសារបស់ខ្លួន</i>",
                    parse_mode='HTML',
                    reply_markup={'inline_keyboard': rows})
            return

        elif callback_data.startswith('cbm_lang:') and is_admin(user_id):
            rest   = callback_data[9:]
            bot_id, code = (rest.split(':', 1) + ['off'])[:2]
            with _clone_bots_lock:
                bot = next((b for b in _clone_bots_list if b['id'] == bot_id), None)
                if bot:
                    if code == 'off':
                        bot['default_lang']      = None
                        bot['default_lang_name'] = None
                    else:
                        bot['default_lang']      = code
                        bot['default_lang_name'] = TRANSLATE_LANGUAGES.get(code, code)
            _save_clone_bots()
            lang_name = TRANSLATE_LANGUAGES.get(code, code) if code != 'off' else 'បិទ'
            answer_callback(callback_query['id'], f"✅ ភាសា: {lang_name}")
            _show_clone_bot_detail(chat_id, bot_id,
                                   edit_msg_id=callback_query['message']['message_id'])
            return

        elif callback_data == 'cancel_buy':
            answer_callback(callback_query['id'])
            with _data_lock:
                if user_id in user_sessions:
                    del user_sessions[user_id]
            save_sessions_async()
            summary_message_id = callback_query['message']['message_id']
            delete_message_async(chat_id, summary_message_id)
            send_account_selection_inline(chat_id)
            return

        elif callback_data.startswith('qty:'):
            session = user_sessions.get(user_id)
            if not session or session.get('state') != 'waiting_for_quantity':
                answer_callback(callback_query['id'])
                delete_message_async(chat_id, callback_query['message']['message_id'])
                send_account_selection_inline(chat_id)
                return
            try:
                quantity = int(callback_data.split(':', 1)[1])
            except (ValueError, IndexError):
                answer_callback(callback_query['id'])
                delete_message_async(chat_id, callback_query['message']['message_id'])
                send_account_selection_inline(chat_id)
                return
            if quantity > session['available_count']:
                answer_callback(callback_query['id'])
                delete_message_async(chat_id, callback_query['message']['message_id'])
                send_account_selection_inline(chat_id)
                return
            total_price = quantity * session['price']
            with _data_lock:
                session['quantity']    = quantity
                session['total_price'] = total_price
                session['state']       = 'payment_pending'
            save_sessions_async()
            answer_callback(callback_query['id'], 'កំពុងបង្កើត QR...')
            delete_message_async(chat_id, callback_query['message']['message_id'])
            _generate_and_send_qr(chat_id, user_id, session)
            return

        elif callback_data == 'check_payment':
            session = user_sessions.get(user_id)
            if not session or session.get('state') != 'payment_pending':
                session = get_pending_payment(user_id)
            if not session:
                answer_callback(callback_query['id'])
                delete_message_async(chat_id, callback_query['message']['message_id'])
                send_account_selection_inline(chat_id)
                return
            md5 = session.get('md5_hash')
            if not md5:
                answer_callback(callback_query['id'])
                delete_message_async(chat_id, callback_query['message']['message_id'])
                send_account_selection_inline(chat_id)
                return
            is_paid, payment_data = check_payment_status(md5)
            if is_paid:
                answer_callback(callback_query['id'], '✅ ការបង់ប្រាក់បានបញ្ជាក់!')
                user_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                deliver_accounts(chat_id, user_id, session, payment_data=payment_data, user_name=user_name)
                delete_pending_payment_async(user_id)
                save_sessions_async()
            else:
                answer_callback(callback_query['id'], '⏳ មិនទាន់បានទទួលការបង់ប្រាក់។ សូមព្យាយាមម្តងទៀត។', True)
            return

        elif callback_data == 'cancel_purchase':
            session = user_sessions.get(user_id)
            if not session or session.get('state') != 'payment_pending':
                session = get_pending_payment(user_id)
            md5 = session.get('md5_hash') if session else None
            if md5:
                is_paid, payment_data = check_payment_status(md5)
                if is_paid:
                    answer_callback(callback_query['id'], '✅ ការបង់ប្រាក់បានបញ្ជាក់!')
                    user_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                    deliver_accounts(chat_id, user_id, session, payment_data=payment_data, user_name=user_name)
                    delete_pending_payment_async(user_id)
                    save_sessions_async()
                    return
            answer_callback(callback_query['id'])
            btn_message_id = callback_query['message']['message_id']
            delete_message_async(chat_id, btn_message_id)
            if session:
                for key in ('photo_message_id', 'qr_message_id', 'dot_message_id'):
                    mid = session.get(key)
                    if mid and mid != btn_message_id:
                        delete_message_async(chat_id, mid)
            with _data_lock:
                if user_id in user_sessions:
                    del user_sessions[user_id]
            save_sessions_async()
            delete_pending_payment_async(user_id)
            send_account_selection_inline(chat_id)

    except Exception as e:
        logger.error(f"Error handling callback query: {e}")

# ── TTV (Text-to-Voice) Engine ────────────────────────────────────────────────
import unicodedata as _ucd
try:
    from langdetect import detect as _ld_detect, detect_langs as _ld_detect_langs, DetectorFactory as _ld_factory
    _ld_factory.seed = 0
    _LANGDETECT_OK = True
except ImportError:
    _LANGDETECT_OK = False

_TTV_MALE_VOICES = {
    "af":"af-ZA-WillemNeural","am":"am-ET-AmehaNeural","ar":"ar-SA-HamedNeural",
    "az":"az-AZ-BabekNeural","bg":"bg-BG-BorislavNeural","bn":"bn-BD-PradeepNeural",
    "bs":"bs-BA-GoranNeural","ca":"ca-ES-EnricNeural","cs":"cs-CZ-AntoninNeural",
    "cy":"cy-GB-AledNeural","da":"da-DK-JeppeNeural","de":"de-DE-FlorianMultilingualNeural",
    "el":"el-GR-NestorasNeural","en":"en-US-AndrewMultilingualNeural","es":"es-ES-AlvaroNeural",
    "et":"et-EE-KertNeural","fa":"fa-IR-FaridNeural","fi":"fi-FI-HarriNeural",
    "fil":"fil-PH-AngeloNeural","fr":"fr-FR-RemyMultilingualNeural","ga":"ga-IE-ColmNeural",
    "gl":"gl-ES-RoiNeural","gu":"gu-IN-NiranjanNeural","he":"he-IL-AvriNeural",
    "hi":"hi-IN-MadhurNeural","hr":"hr-HR-SreckoNeural","hu":"hu-HU-TamasNeural",
    "id":"id-ID-ArdiNeural","is":"is-IS-GunnarNeural","it":"it-IT-GiuseppeMultilingualNeural",
    "ja":"ja-JP-KeitaNeural","jv":"jv-ID-DimasNeural","ka":"ka-GE-GiorgiNeural",
    "kk":"kk-KZ-DauletNeural","km":"km-KH-PisethNeural","kn":"kn-IN-GaganNeural",
    "ko":"ko-KR-HyunsuMultilingualNeural","lo":"lo-LA-ChanthavongNeural","lt":"lt-LT-LeonasNeural",
    "lv":"lv-LV-NilsNeural","mk":"mk-MK-AleksandarNeural","ml":"ml-IN-MidhunNeural",
    "mn":"mn-MN-BataaNeural","mr":"mr-IN-ManoharNeural","ms":"ms-MY-OsmanNeural",
    "mt":"mt-MT-JosephNeural","my":"my-MM-ThihaNeural","nb":"nb-NO-FinnNeural",
    "ne":"ne-NP-SagarNeural","nl":"nl-NL-MaartenNeural","pl":"pl-PL-MarekNeural",
    "ps":"ps-AF-GulNawazNeural","pt":"pt-BR-AntonioNeural","ro":"ro-RO-EmilNeural",
    "ru":"ru-RU-DmitryNeural","si":"si-LK-SameeraNeural","sk":"sk-SK-LukasNeural",
    "sl":"sl-SI-RokNeural","so":"so-SO-MuuseNeural","sq":"sq-AL-IlirNeural",
    "sr":"sr-RS-NicholasNeural","su":"su-ID-JajangNeural","sv":"sv-SE-MattiasNeural",
    "sw":"sw-KE-RafikiNeural","ta":"ta-IN-ValluvarNeural","te":"te-IN-MohanNeural",
    "th":"th-TH-NiwatNeural","tr":"tr-TR-AhmetNeural","uk":"uk-UA-OstapNeural",
    "ur":"ur-IN-SalmanNeural","uz":"uz-UZ-SardorNeural","vi":"vi-VN-NamMinhNeural",
    "zh-CN":"zh-CN-YunyangNeural","zh-TW":"zh-TW-YunJheNeural","zu":"zu-ZA-ThembaNeural",
}
_TTV_FEMALE_VOICES = {
    "af":"af-ZA-AdriNeural","am":"am-ET-MekdesNeural","ar":"ar-SA-ZariyahNeural",
    "az":"az-AZ-BanuNeural","bg":"bg-BG-KalinaNeural","bn":"bn-BD-NabanitaNeural",
    "bs":"bs-BA-VesnaNeural","ca":"ca-ES-JoanaNeural","cs":"cs-CZ-VlastaNeural",
    "cy":"cy-GB-NiaNeural","da":"da-DK-ChristelNeural","de":"de-DE-SeraphinaMultilingualNeural",
    "el":"el-GR-AthinaNeural","en":"en-US-AvaMultilingualNeural","es":"es-ES-XimenaNeural",
    "et":"et-EE-AnuNeural","fa":"fa-IR-DilaraNeural","fi":"fi-FI-NooraNeural",
    "fil":"fil-PH-BlessicaNeural","fr":"fr-FR-VivienneMultilingualNeural","ga":"ga-IE-OrlaNeural",
    "gl":"gl-ES-SabelaNeural","gu":"gu-IN-DhwaniNeural","he":"he-IL-HilaNeural",
    "hi":"hi-IN-SwaraNeural","hr":"hr-HR-GabrijelaNeural","hu":"hu-HU-NoemiNeural",
    "id":"id-ID-GadisNeural","is":"is-IS-GudrunNeural","it":"it-IT-IsabellaNeural",
    "ja":"ja-JP-NanamiNeural","jv":"jv-ID-SitiNeural","ka":"ka-GE-EkaNeural",
    "kk":"kk-KZ-AigulNeural","km":"km-KH-SreymomNeural","kn":"kn-IN-SapnaNeural",
    "ko":"ko-KR-SunHiNeural","lo":"lo-LA-KeomanyNeural","lt":"lt-LT-OnaNeural",
    "lv":"lv-LV-EveritaNeural","mk":"mk-MK-MarijaNeural","ml":"ml-IN-SobhanaNeural",
    "mn":"mn-MN-YesuiNeural","mr":"mr-IN-AarohiNeural","ms":"ms-MY-YasminNeural",
    "mt":"mt-MT-GraceNeural","my":"my-MM-NilarNeural","nb":"nb-NO-PernilleNeural",
    "ne":"ne-NP-HemkalaNeural","nl":"nl-NL-ColetteNeural","pl":"pl-PL-ZofiaNeural",
    "ps":"ps-AF-LatifaNeural","pt":"pt-BR-ThalitaMultilingualNeural","ro":"ro-RO-AlinaNeural",
    "ru":"ru-RU-SvetlanaNeural","si":"si-LK-ThiliniNeural","sk":"sk-SK-ViktoriaNeural",
    "sl":"sl-SI-PetraNeural","so":"so-SO-UbaxNeural","sq":"sq-AL-AnilaNeural",
    "sr":"sr-RS-SophieNeural","su":"su-ID-TutiNeural","sv":"sv-SE-SofieNeural",
    "sw":"sw-KE-ZuriNeural","ta":"ta-IN-PallaviNeural","te":"te-IN-ShrutiNeural",
    "th":"th-TH-PremwadeeNeural","tr":"tr-TR-EmelNeural","uk":"uk-UA-PolinaNeural",
    "ur":"ur-IN-GulNeural","uz":"uz-UZ-MadinaNeural","vi":"vi-VN-HoaiMyNeural",
    "zh-CN":"zh-CN-XiaoxiaoNeural","zh-TW":"zh-TW-HsiaoChenNeural","zu":"zu-ZA-ThandoNeural",
}

_TTV_NORMALIZE = {
    "zh-cn":"zh-CN","zh-tw":"zh-TW","zh":"zh-CN",
    "iw":"he","no":"nb","tl":"fil","jw":"jv","in":"id",
}
_TTV_LANG_FALLBACK = {"pa":"hi","or":"bn","hy":"en"}

_TTV_SCRIPT_MAP = [
    (r'[\u1780-\u17FF]','km'),(r'[\u0E00-\u0E7F]','th'),(r'[\u0E80-\u0EFF]','lo'),
    (r'[\u1000-\u109F]','my'),(r'[\u1200-\u137F]','am'),(r'[\u10A0-\u10FF]','ka'),
    (r'[\u0530-\u058F]','hy'),(r'[\u0590-\u05FF]','he'),(r'[\u0900-\u097F]','hi'),
    (r'[\u0980-\u09FF]','bn'),(r'[\u0A00-\u0A7F]','pa'),(r'[\u0A80-\u0AFF]','gu'),
    (r'[\u0B00-\u0B7F]','or'),(r'[\u0B80-\u0BFF]','ta'),(r'[\u0C00-\u0C7F]','te'),
    (r'[\u0C80-\u0CFF]','kn'),(r'[\u0D00-\u0D7F]','ml'),(r'[\u0D80-\u0DFF]','si'),
    (r'[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]','ar'),
    (r'[\u0400-\u04FF]','ru'),(r'[\u0370-\u03FF]','el'),(r'[\u1800-\u18AF]','mn'),
    (r'[\uAC00-\uD7FF]','ko'),(r'[\u3040-\u30FF]','ja'),
    (r'[\u4E00-\u9FFF\u3400-\u4DBF]','zh-CN'),
]

_TTV_SEGMENT_RE = re.compile(
    r'(?P<km>[\u1780-\u17FF]+)'
    r'|(?P<th>[\u0E00-\u0E7F]+)'
    r'|(?P<lo>[\u0E80-\u0EFF]+)'
    r'|(?P<my>[\u1000-\u109F]+)'
    r'|(?P<am>[\u1200-\u137F]+)'
    r'|(?P<ka>[\u10A0-\u10FF]+)'
    r'|(?P<he>[\u0590-\u05FF]+)'
    r'|(?P<hi>[\u0900-\u097F]+)'
    r'|(?P<bn>[\u0980-\u09FF]+)'
    r'|(?P<pa>[\u0A00-\u0A7F]+)'
    r'|(?P<gu>[\u0A80-\u0AFF]+)'
    r'|(?P<ta>[\u0B80-\u0BFF]+)'
    r'|(?P<te>[\u0C00-\u0C7F]+)'
    r'|(?P<kn>[\u0C80-\u0CFF]+)'
    r'|(?P<ml>[\u0D00-\u0D7F]+)'
    r'|(?P<si>[\u0D80-\u0DFF]+)'
    r'|(?P<ar>[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]+)'
    r'|(?P<ru>[\u0400-\u04FF]+)'
    r'|(?P<el>[\u0370-\u03FF]+)'
    r'|(?P<mn_s>[\u1800-\u18AF]+)'
    r'|(?P<ko>[\uAC00-\uD7FF]+)'
    r'|(?P<ja>[\u3040-\u30FF]+)'
    r'|(?P<zh>[\u4E00-\u9FFF\u3400-\u4DBF]+)'
    r'|(?P<other>[^\u1780-\u17FF\u0E00-\u0EFF\u1000-\u109F\u1200-\u137F'
    r'\u10A0-\u10FF\u0590-\u05FF\u0900-\u09FF\u0A00-\u0AFF\u0B80-\u0BFF'
    r'\u0C00-\u0CFF\u0D00-\u0DFF\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF'
    r'\uFE70-\uFEFF\u0400-\u04FF\u0370-\u03FF\u1800-\u18AF\uAC00-\uD7FF'
    r'\u3040-\u30FF\u4E00-\u9FFF\u3400-\u4DBF]+)'
)
_TTV_SCRIPT_LANG = {
    'km':'km','th':'th','lo':'lo','my':'my','am':'am','ka':'ka','he':'he',
    'hi':'hi','bn':'bn','pa':'hi','gu':'gu','ta':'ta','te':'te','kn':'kn',
    'ml':'ml','si':'si','ar':'ar','ru':'ru','el':'el','mn_s':'mn',
    'ko':'ko','ja':'ja','zh':'zh-CN',
}

_TTV_SPEED_RATES = {"x0.5": "-50%", "x1": "+0%", "x1.5": "+50%", "x2": "+100%"}
_TTV_SPEED_KEYS  = list(_TTV_SPEED_RATES.keys())

def _ttv_strip_unspeakable(text):
    result = []
    for ch in text:
        cat = _ucd.category(ch)
        if cat.startswith(('L', 'M', 'N', 'P', 'Z')) or ch in '\n\r\t ':
            result.append(ch)
    return ''.join(result)

def _ttv_segment_text(text):
    """Split text into [(chunk, lang)] by script. Merges adjacent same-lang segments."""
    raw = []
    for m in _TTV_SEGMENT_RE.finditer(text):
        g = m.lastgroup
        chunk = m.group()
        if g == 'other':
            raw.append((chunk, None))
        else:
            lang = _TTV_SCRIPT_LANG.get(g, 'en')
            if g == 'ar' and _LANGDETECT_OK:
                try:
                    d = _TTV_NORMALIZE.get(_ld_detect(chunk), _ld_detect(chunk))
                    if d in ('fa','ur','ps','ar'):
                        lang = d
                except Exception:
                    pass
            elif g == 'ru' and _LANGDETECT_OK:
                try:
                    d = _TTV_NORMALIZE.get(_ld_detect(chunk), _ld_detect(chunk))
                    if d in ('ru','uk','bg','sr','mk','kk','mn'):
                        lang = d
                except Exception:
                    pass
            raw.append((chunk, lang))
    resolved = []
    for chunk, lang in raw:
        if lang is not None:
            resolved.append((chunk, lang))
            continue
        stripped = chunk.strip()
        if not stripped:
            if resolved:
                resolved[-1] = (resolved[-1][0] + chunk, resolved[-1][1])
            continue
        has_latin = bool(re.search(r'[a-zA-Z]', chunk))
        if not has_latin:
            if resolved:
                resolved[-1] = (resolved[-1][0] + chunk, resolved[-1][1])
            else:
                resolved.append((chunk, 'en'))
            continue
        detected = 'en'
        if len(stripped) >= 4 and _LANGDETECT_OK:
            try:
                langs = _ld_detect_langs(stripped)
                if langs and langs[0].prob >= 0.65:
                    detected = _TTV_NORMALIZE.get(langs[0].lang, langs[0].lang)
            except Exception:
                pass
        resolved.append((chunk, detected))
    resolved = [(c, _TTV_LANG_FALLBACK.get(l, l)) for c, l in resolved]
    merged = []
    for chunk, lang in resolved:
        if merged and merged[-1][1] == lang:
            merged[-1] = (merged[-1][0] + chunk, lang)
        else:
            merged.append([chunk, lang])
    return [(c, l) for c, l in merged] if merged else [('', 'en')]

def _ttv_run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

def _ttv_synthesize(text, gender='female', speed='x1'):
    """Convert text → OGG Opus bytes via edge_tts + ffmpeg (mixed-language support)."""
    try:
        import edge_tts
        import imageio_ffmpeg
    except ImportError:
        logger.error("edge_tts / imageio_ffmpeg not installed")
        return None

    rate    = _TTV_SPEED_RATES.get(speed, '+0%')
    vm      = _TTV_MALE_VOICES if gender == 'male' else _TTV_FEMALE_VOICES
    FFMPEG  = imageio_ffmpeg.get_ffmpeg_exe()
    segments = _ttv_segment_text(text)

    async def _synth_pcm(chunk, voice):
        clean = _ttv_strip_unspeakable(chunk).strip()
        if not clean or not re.search(r'\w', clean, re.UNICODE):
            return b''
        comm = edge_tts.Communicate(clean, voice, rate=rate, pitch="+5Hz")
        mp3 = io.BytesIO()
        async for c in comm.stream():
            if c['type'] == 'audio':
                mp3.write(c['data'])
        mp3_data = mp3.getvalue()
        if not mp3_data:
            return b''
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, '-y', '-f', 'mp3', '-i', 'pipe:0',
            '-f', 's16le', '-ac', '1', '-ar', '48000', 'pipe:1',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate(input=mp3_data)
        return stdout or b''

    async def _pcm_to_ogg(pcm):
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, '-y', '-f', 's16le', '-ac', '1', '-ar', '48000', '-i', 'pipe:0',
            '-c:a', 'libopus', '-b:a', '96k', '-f', 'ogg', 'pipe:1',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate(input=pcm)
        return stdout or b''

    async def _run():
        tasks = [
            _synth_pcm(chunk, vm.get(lang) or vm.get('en'))
            for chunk, lang in segments
        ]
        pcm_parts = await asyncio.gather(*tasks)
        pcm_all = b''.join(pcm_parts)
        if not pcm_all:
            return None
        return await _pcm_to_ogg(pcm_all)

    try:
        return _ttv_run_async(_run())
    except Exception as e:
        logger.error(f"TTV synthesis failed: {e}")
        return None

# ── Clone Bot engine ──────────────────────────────────────────────────────────
def _clone_api(base_url, method, _files=None, **kwargs):
    try:
        url = f"{base_url}{method}"
        if _files:
            data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                    for k, v in kwargs.items() if v is not None}
            resp = http.post(url, data=data, files=_files, timeout=30)
        else:
            resp = http.post(url, json={k: v for k, v in kwargs.items() if v is not None}, timeout=15)
        result = resp.json()
        if result.get('ok'):
            return result.get('result')
        logger.debug(f"Clone API {method}: {result.get('description')}")
    except Exception as e:
        logger.error(f"Clone API {method} error: {e}")
    return None

def _clone_send_voice(base_url, chat_id, ogg_bytes, reply_to=None, reply_markup=None):
    buf = io.BytesIO(ogg_bytes)
    buf.name = 'voice.ogg'
    params = {
        'chat_id': chat_id,
        'caption': '<tg-emoji emoji-id="5294339927318739359">🎙</tg-emoji> @limsovannradybot',
        'parse_mode': 'HTML',
    }
    if reply_to:
        params['reply_to_message_id'] = reply_to
    if reply_markup:
        params['reply_markup'] = reply_markup
    return _clone_api(base_url, 'sendVoice', _files={'voice': buf}, **params)

def _get_clone_bot_by_base_url(base_url):
    with _clone_bots_lock:
        for b in _clone_bots_list:
            if b.get('token') and f"bot{b['token']}/" in base_url:
                return dict(b)
    return {}

def _clone_handle_update(base_url, update):
    global _clone_bot_prefs

    if 'callback_query' in update:
        cq      = update['callback_query']
        user_id = cq['from']['id']
        data    = cq.get('data', '')
        chat_id = cq.get('message', {}).get('chat', {}).get('id', user_id)
        msg_id  = cq.get('message', {}).get('message_id')
        _clone_api(base_url, 'answerCallbackQuery', callback_query_id=cq['id'])
        if data.startswith('ttv_gender:'):
            gender = data.split(':', 1)[1]
            _clone_bot_prefs.setdefault(user_id, {})['gender'] = gender
            label = "👩 សំឡេងស្រី" if gender == 'female' else "👨 សំឡេងប្រុស"
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                       text=f'<tg-emoji emoji-id="6217467173917429904">✅</tg-emoji> <b>បានប្តូរទៅ {label}</b>',
                       parse_mode="HTML")
        elif data.startswith('ttv_speed:'):
            speed = data.split(':', 1)[1]
            _clone_bot_prefs.setdefault(user_id, {})['speed'] = speed
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                       text=f"✅ ប្តូរល្បឿនទៅ <b>{speed}</b>", parse_mode="HTML")
        elif data.startswith('cln_lang_'):
            lang_code = data[9:]
            lang_name = TRANSLATE_LANGUAGES.get(lang_code, lang_code)
            _clone_bot_prefs.setdefault(user_id, {})['translate_lang'] = lang_code
            _clone_bot_prefs[user_id]['translate_name'] = lang_name
            _clone_bot_prefs[user_id]['clone_mode'] = 'translate'
            if msg_id:
                _clone_api(base_url, 'deleteMessage', chat_id=chat_id, message_id=msg_id)
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                text=f"✅ <b>Mode: បកប្រែភាសា → {lang_name}</b>\n\n"
                     f"💬 សូមផ្ញើអក្សរ — ខ្ញុំនឹង<i>បកប្រែ</i>ជា {lang_name} ដោយស្វ័យប្រវត្តិ។",
                parse_mode="HTML")
        elif data == 'cln_tr_off':
            _clone_bot_prefs.setdefault(user_id, {}).pop('translate_lang', None)
            _clone_bot_prefs[user_id].pop('translate_name', None)
            _clone_bot_prefs[user_id]['clone_mode'] = 'voice'
            if msg_id:
                _clone_api(base_url, 'deleteMessage', chat_id=chat_id, message_id=msg_id)
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                       text="🎙 <b>ប្តូរទៅ Mode: TTS</b> — សរសេរអក្សរ → ខ្ញុំបំប្លែងជាសំឡេង។",
                       parse_mode="HTML")
        elif data == 'cln_tr_menu':
            rows = []
            items = list(TRANSLATE_LANGUAGES.items())
            for i in range(0, len(items), 3):
                row = [{'text': name, 'callback_data': f'cln_lang_{code}'}
                       for code, name in items[i:i+3]]
                rows.append(row)
            rows.append([{'text': '🔇 បិទបកប្រែ', 'callback_data': 'cln_tr_off'}])
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                text='🌐 <b>ជ្រើសរើសភាសាបកប្រែ</b>',
                parse_mode="HTML",
                reply_markup={'inline_keyboard': rows})
        return

    if 'message' not in update:
        return

    msg     = update['message']
    user_id = msg.get('from', {}).get('id')
    chat_id = msg.get('chat', {}).get('id')
    text    = msg.get('text', '').strip()
    if not text or not user_id:
        return

    _CLONE_START_KB = {'remove_keyboard': True}

    if text.startswith('/start'):
        _clone_bot_prefs.setdefault(user_id, {})
        _clone_bot_prefs[user_id].pop('translate_lang', None)
        _clone_bot_prefs[user_id].pop('translate_name', None)
        _clone_bot_prefs[user_id]['clone_mode'] = 'voice'
        _clone_api(base_url, 'sendMessage', chat_id=chat_id,
            text='<tg-emoji emoji-id="5798587088077066898">👋</tg-emoji> <b>សួស្តី</b> Sovannrady\n\n'
                 '<b>ខ្ញុំជា Text to voice bot</b>\n\n'
                 '<tg-emoji emoji-id="5471978009449731768">👉</tg-emoji><i>គ្រាន់តែ សរសេរអក្សរណាមួយ ហើយ ខ្ញុំនឹងបំប្លែងជាសំឡេងដោយស្វ័យប្រវត្តិ។</i>',
            parse_mode="HTML",
            message_effect_id="5104841245755180586",
            reply_markup=_CLONE_START_KB)
        return


    prefs      = _clone_bot_prefs.get(user_id, {})
    gender     = prefs.get('gender', 'female')
    speed      = prefs.get('speed', 'x1')

    # ── TTS-only mode (default) ───────────────────────────────────────────────
    _clone_api(base_url, 'sendChatAction', chat_id=chat_id, action='record_voice')

    try:
        ogg_bytes = _ttv_synthesize(text, gender=gender, speed=speed)
        if ogg_bytes:
            cur_idx    = _TTV_SPEED_KEYS.index(speed) if speed in _TTV_SPEED_KEYS else 1
            next_speed = _TTV_SPEED_KEYS[(cur_idx + 1) % len(_TTV_SPEED_KEYS)]
            kb_row = [
                {'text': '👨 សំឡេងប្រុស' if gender == 'female' else '👩 សំឡេងស្រី',
                 'callback_data': f"ttv_gender:{'male' if gender == 'female' else 'female'}",
                 'style': 'success'},
                {'text': 'ល្បឿន',
                 'callback_data': f"ttv_speed:{next_speed}",
                 'style': 'primary',
                 'icon_custom_emoji_id': '5445284980978621387'},
            ]
            kb = {'inline_keyboard': [kb_row]}
            _clone_send_voice(base_url, chat_id, ogg_bytes,
                              reply_to=msg.get('message_id'), reply_markup=kb)
        else:
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                       text="⚠️ មានបញ្ហាក្នុងការបង្កើតសំឡេង។ សូមព្យាយាមម្តងទៀត។")
    except Exception as e:
        logger.error(f"Clone bot TTV error: {e}")
        _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                   text="⚠️ មានបញ្ហាក្នុងការបង្កើតសំឡេង។")

def _clone_bot_polling_loop(token):
    base_url = f"https://api.telegram.org/bot{token}/"
    offset   = None
    logger.info("Clone Bot polling started")
    # Delete any active webhook before polling
    try:
        http.post(f"{base_url}deleteWebhook", json={'drop_pending_updates': False}, timeout=10)
        logger.info("Clone Bot: webhook deleted, starting poll")
    except Exception as e:
        logger.warning(f"Clone Bot deleteWebhook failed: {e}")
    while CLONE_BOT_ACTIVE:
        try:
            params = {'timeout': 30, 'allowed_updates': ['message', 'callback_query']}
            if offset is not None:
                params['offset'] = offset
            resp = http.get(f"{base_url}getUpdates", params=params, timeout=40)
            data = resp.json()
            if data.get('ok'):
                for upd in data.get('result', []):
                    offset = upd['update_id'] + 1
                    worker_pool.submit(_clone_handle_update, base_url, upd)
            else:
                logger.warning(f"Clone Bot getUpdates: {data.get('description')}")
                time.sleep(3)
        except Exception as e:
            if CLONE_BOT_ACTIVE:
                logger.error(f"Clone Bot polling error: {e}")
                time.sleep(3)
    logger.info("Clone Bot polling stopped")

def _start_clone_bot(token):
    global _clone_bot_thread, CLONE_BOT_ACTIVE
    _stop_clone_bot()
    CLONE_BOT_ACTIVE = True
    _clone_bot_thread = threading.Thread(
        target=_clone_bot_polling_loop, args=(token,),
        daemon=True, name="clone-bot"
    )
    _clone_bot_thread.start()
    logger.info("Clone Bot thread started")

def _stop_clone_bot():
    global CLONE_BOT_ACTIVE, _clone_bot_thread
    CLONE_BOT_ACTIVE = False
    _clone_bot_thread = None

# ── Translate Bot engine (text-only, no TTS) ──────────────────────────────────
def _tr_api(base_url, method, **kwargs):
    try:
        resp = http.post(f"{base_url}{method}",
                         json={k: v for k, v in kwargs.items() if v is not None}, timeout=15)
        result = resp.json()
        if result.get('ok'):
            return result.get('result')
        logger.debug(f"TR API {method}: {result.get('description')}")
    except Exception as e:
        logger.error(f"TR API {method} error: {e}")
    return None

def _translate_handle_update(base_url, update):
    global _translate_bot_prefs
    if 'callback_query' in update:
        cq      = update['callback_query']
        user_id = cq['from']['id']
        data    = cq.get('data', '')
        chat_id = cq.get('message', {}).get('chat', {}).get('id', user_id)
        msg_id  = cq.get('message', {}).get('message_id')
        _tr_api(base_url, 'answerCallbackQuery', callback_query_id=cq['id'])
        if data.startswith('trl_lang_'):
            lang_code = data[9:]
            lang_name = TRANSLATE_LANGUAGES.get(lang_code, lang_code)
            _translate_bot_prefs.setdefault(user_id, {})['lang_code'] = lang_code
            _translate_bot_prefs[user_id]['lang_name'] = lang_name
            if msg_id:
                _tr_api(base_url, 'deleteMessage', chat_id=chat_id, message_id=msg_id)
            _tr_api(base_url, 'sendMessage', chat_id=chat_id,
                text=f"✅ <b>ភាសាបកប្រែ: {lang_name}</b>\n\n"
                     f"💬 សូមផ្ញើអក្សរ — ខ្ញុំនឹងបកប្រែជា {lang_name} ភ្លាមៗ។",
                parse_mode="HTML")
        return

    if 'message' not in update:
        return
    msg     = update['message']
    user_id = msg.get('from', {}).get('id')
    chat_id = msg.get('chat', {}).get('id')
    text    = msg.get('text', '').strip()
    if not text or not user_id:
        return

    items = list(TRANSLATE_LANGUAGES.items())
    rows  = []
    for i in range(0, len(items), 3):
        rows.append([{'text': name, 'callback_data': f'trl_lang_{code}'}
                     for code, name in items[i:i+3]])
    lang_kb = {'inline_keyboard': rows}

    if text.startswith('/start'):
        _translate_bot_prefs.setdefault(user_id, {})
        _tr_api(base_url, 'sendMessage', chat_id=chat_id,
            text='🌐 <b>Translate Bot</b>\n\n'
                 '<i>ជ្រើសរើសភាសា រួចផ្ញើអក្សរ — ខ្ញុំនឹងបកប្រែជូន។</i>',
            parse_mode="HTML", reply_markup=lang_kb)
        return

    prefs     = _translate_bot_prefs.get(user_id, {})
    lang_code = prefs.get('lang_code')
    lang_name = prefs.get('lang_name', lang_code or '')

    if not lang_code:
        _tr_api(base_url, 'sendMessage', chat_id=chat_id,
            text='🌐 <b>សូមជ្រើសរើសភាសាជាមុនសិន</b>',
            parse_mode="HTML", reply_markup=lang_kb)
        return

    _tr_api(base_url, 'sendChatAction', chat_id=chat_id, action='typing')
    translated = _translate_text(text, lang_code)
    if translated:
        _tr_api(base_url, 'sendMessage', chat_id=chat_id,
            text=f"🌐 <b>{lang_name}</b>\n<blockquote>{html.escape(translated)}</blockquote>",
            parse_mode="HTML")
    else:
        _tr_api(base_url, 'sendMessage', chat_id=chat_id,
                text="❌ មានបញ្ហាក្នុងការបកប្រែ សូមព្យាយាមម្តងទៀត។")

def _translate_bot_polling_loop(token):
    global TRANSLATE_BOT_ACTIVE
    base_url = f"https://api.telegram.org/bot{token}/"
    offset   = None
    logger.info(f"Translate Bot [{token[:10]}...] started")
    try:
        http.post(f"{base_url}deleteWebhook", json={'drop_pending_updates': False}, timeout=10)
        logger.info("Translate Bot: webhook deleted, starting poll")
    except Exception as e:
        logger.warning(f"Translate Bot deleteWebhook failed: {e}")
    while TRANSLATE_BOT_ACTIVE:
        try:
            params = {'timeout': 30, 'allowed_updates': ['message', 'callback_query']}
            if offset is not None:
                params['offset'] = offset
            resp = http.get(f"{base_url}getUpdates", params=params, timeout=40)
            data = resp.json()
            if data.get('ok'):
                for upd in data.get('result', []):
                    offset = upd['update_id'] + 1
                    worker_pool.submit(_translate_handle_update, base_url, upd)
            else:
                logger.warning(f"Translate Bot getUpdates: {data.get('description')}")
                time.sleep(3)
        except Exception as e:
            if TRANSLATE_BOT_ACTIVE:
                logger.error(f"Translate Bot polling error: {e}")
                time.sleep(3)
    logger.info("Translate Bot polling stopped")

def _start_translate_bot(token):
    global _translate_bot_thread, TRANSLATE_BOT_ACTIVE
    _stop_translate_bot()
    TRANSLATE_BOT_ACTIVE = True
    _translate_bot_thread = threading.Thread(
        target=_translate_bot_polling_loop, args=(token,),
        daemon=True, name="translate-bot"
    )
    _translate_bot_thread.start()
    logger.info("Translate Bot thread started")

def _stop_translate_bot():
    global TRANSLATE_BOT_ACTIVE, _translate_bot_thread
    TRANSLATE_BOT_ACTIVE = False
    _translate_bot_thread = None

# ── Multi-bot clone management ────────────────────────────────────────────────
def _clone_bot_loop_v2(token, stop_event):
    base_url = f"https://api.telegram.org/bot{token}/"
    offset   = None
    logger.info(f"Clone Bot [{token[:10]}...] started")
    def _steal_polling():
        """deleteWebhook + immediate getUpdates(timeout=0) to kick any other poller."""
        try:
            http.post(f"{base_url}deleteWebhook",
                      json={'drop_pending_updates': True}, timeout=10)
        except Exception:
            pass
        try:
            http.get(f"{base_url}getUpdates",
                     params={'timeout': 0, 'offset': -1,
                             'allowed_updates': ['message', 'callback_query']},
                     timeout=10)
        except Exception:
            pass

    # ── Steal polling slot from any other running instance ────────────────────
    _steal_polling()
    stop_event.wait(2)
    if stop_event.is_set():
        return
    # ─────────────────────────────────────────────────────────────────────────
    _conflict_count = 0
    while not stop_event.is_set():
        try:
            params = {'timeout': 10, 'allowed_updates': ['message', 'callback_query']}
            if offset is not None:
                params['offset'] = offset
            resp = http.get(f"{base_url}getUpdates", params=params, timeout=15)
            data = resp.json()
            if data.get('ok'):
                _conflict_count = 0
                for upd in data.get('result', []):
                    offset = upd['update_id'] + 1
                    worker_pool.submit(_clone_handle_update, base_url, upd)
            else:
                desc = data.get('description', '')
                logger.warning(f"Clone Bot [{token[:10]}...]: {desc}")
                if 'Conflict' in desc:
                    _conflict_count += 1
                    wait = min(5 * _conflict_count, 30)
                    logger.info(f"Clone Bot [{token[:10]}...]: re-stealing polling (attempt {_conflict_count})")
                    stop_event.wait(wait)
                    if not stop_event.is_set():
                        _steal_polling()
                        stop_event.wait(2)
                else:
                    stop_event.wait(5)
        except Exception as e:
            if not stop_event.is_set():
                logger.error(f"Clone Bot [{token[:10]}...] error: {e}")
                stop_event.wait(5)
    logger.info(f"Clone Bot [{token[:10]}...] stopped")

def _load_clone_bots():
    global _clone_bots_list
    try:
        raw   = get_setting('CLONE_BOTS_LIST')
        saved = json.loads(raw) if raw else []
        # Migrate from old single-bot setting if list is empty and old token exists
        if not saved:
            old_tok = get_setting('CLONE_BOT_TOKEN') or CLONE_BOT_TOKEN
            if old_tok:
                bid       = hashlib.md5(old_tok.encode()).hexdigest()[:8]
                is_active = (get_setting('CLONE_BOT_ACTIVE') or '').lower() == 'true'
                saved     = [{'id': bid, 'name': 'Clone Bot 1', 'token': old_tok, 'active': is_active}]
                logger.info("Migrated old CLONE_BOT_TOKEN to CLONE_BOTS_LIST")
        with _clone_bots_lock:
            _clone_bots_list[:] = [
                {'id': b['id'], 'name': b['name'], 'token': b['token'],
                 'active': bool(b.get('active')), 'thread': None, 'stop_event': None,
                 'default_lang': b.get('default_lang'), 'default_lang_name': b.get('default_lang_name')}
                for b in saved
            ]
        logger.info(f"Loaded {len(_clone_bots_list)} clone bot(s)")
    except Exception as e:
        logger.error(f"_load_clone_bots failed: {e}")

def _save_clone_bots():
    try:
        with _clone_bots_lock:
            to_save = [{'id': b['id'], 'name': b['name'], 'token': b['token'],
                        'active': bool(b.get('active')),
                        'default_lang': b.get('default_lang'),
                        'default_lang_name': b.get('default_lang_name')} for b in _clone_bots_list]
        set_setting('CLONE_BOTS_LIST', json.dumps(to_save, ensure_ascii=False))
    except Exception as e:
        logger.error(f"_save_clone_bots failed: {e}")

def _start_clone_bot_by_id(bot_id):
    with _clone_bots_lock:
        bot = next((b for b in _clone_bots_list if b['id'] == bot_id), None)
        if not bot or not bot.get('token'):
            return False
        # Already running — nothing to do
        if bot.get('thread') and bot['thread'].is_alive():
            logger.info(f"Clone Bot {bot_id} already running, skip start")
            return True
        # Signal old thread to stop (it may still be alive momentarily)
        if bot.get('stop_event'):
            bot['stop_event'].set()
        ev = threading.Event()
        bot['stop_event'] = ev
        bot['active']     = True
        t = threading.Thread(target=_clone_bot_loop_v2, args=(bot['token'], ev),
                             daemon=True, name=f"clone-{bot_id}")
        bot['thread'] = t
    t.start()
    _save_clone_bots()
    logger.info(f"Clone Bot {bot_id} started")
    return True

def _stop_clone_bot_by_id(bot_id):
    with _clone_bots_lock:
        bot = next((b for b in _clone_bots_list if b['id'] == bot_id), None)
        if not bot:
            return
        if bot.get('stop_event'):
            bot['stop_event'].set()
        bot['active']     = False
        bot['thread']     = None
        bot['stop_event'] = None
    _save_clone_bots()
    logger.info(f"Clone Bot {bot_id} stopped")

def _clone_bots_inline_kb():
    with _clone_bots_lock:
        bots = list(_clone_bots_list)
    rows = []
    for b in bots:
        alive = b.get('thread') and b['thread'].is_alive()
        icon  = '🟢' if alive else '🔴'
        rows.append([{'text': f"{icon} {b['name']}", 'callback_data': f"cbm:{b['id']}"}])
    rows.append([{'text': '➕ បន្ថែម Clone Bot ថ្មី', 'callback_data': 'cbm_add'}])
    return {'inline_keyboard': rows}

def _show_clone_bots_list(chat_id):
    with _clone_bots_lock:
        count = len(_clone_bots_list)
    msg = ("<i>មិនទាន់មាន Clone Bot ទេ។ ចុច ➕ ដើម្បីបន្ថែមមួយ។</i>"
           if not count else "🤖")
    send_message(chat_id, msg, parse_mode='HTML',
                 reply_to_message_id=False, reply_markup=_clone_bots_inline_kb())

def _show_clone_bot_detail(chat_id, bot_id, edit_msg_id=None):
    with _clone_bots_lock:
        bot = next((b for b in _clone_bots_list if b['id'] == bot_id), None)
    if not bot:
        kb = _clone_bots_inline_kb()
        if edit_msg_id:
            _tg_api('editMessageText', chat_id=chat_id, message_id=edit_msg_id,
                    text="❌ Bot នេះមិនមានទៀតហើយ", reply_markup=kb)
        else:
            send_message(chat_id, "❌ Bot នេះមិនមានទៀតហើយ",
                         reply_to_message_id=False, reply_markup=kb)
        return
    alive      = bot.get('thread') and bot['thread'].is_alive()
    status     = "🟢 ដំណើរការ" if alive else "🔴 បញ្ឈប់"
    token_disp = f"<code>{bot['token'][:12]}...</code>" if bot.get('token') else "❌ មិនទាន់​កំណត់"
    dlang      = bot.get('default_lang_name') or '—'
    text = (
        f"🤖 <b>{html.escape(bot['name'])}</b>\n\n"
        f"🔑 Token: {token_disp}\n"
        f"📡 ស្ថានភាព: {status}\n"
        f"🌐 ភាសា Default: <b>{dlang}</b>\n\n"
        f"<i>Clone Bot ប្តូរអក្សររបស់អ្នកប្រើជាសំឡេងដោយស្វ័យប្រវត្តិ</i>"
    )
    toggle = ({'text': '⏹ Stop', 'callback_data': f"cbm_stop:{bot_id}"}
              if alive else
              {'text': '▶️ Start', 'callback_data': f"cbm_start:{bot_id}"})
    kb = {'inline_keyboard': [
        [toggle],
        [{'text': '🔑 Token', 'callback_data': f"cbm_token:{bot_id}"},
         {'text': '🗑 លុប',   'callback_data': f"cbm_del:{bot_id}"}],
        [{'text': f"🌐 ភាសា: {dlang}", 'callback_data': f"cbm_trl:{bot_id}"}],
        [{'text': '↩️ ត្រឡប់', 'callback_data': 'cbm_list'}],
    ]}
    if edit_msg_id:
        _tg_api('editMessageText', chat_id=chat_id, message_id=edit_msg_id,
                text=text, parse_mode='HTML', reply_markup=kb)
    else:
        send_message(chat_id, text, parse_mode='HTML',
                     reply_to_message_id=False, reply_markup=kb)

def _show_clone_bot_menu(chat_id, user_id=None):
    uid        = user_id or chat_id
    token_ok   = bool(CLONE_BOT_TOKEN)
    is_running = CLONE_BOT_ACTIVE and _clone_bot_thread and _clone_bot_thread.is_alive()
    token_disp = f"<code>{CLONE_BOT_TOKEN[:12]}...</code>" if token_ok else "❌ មិនតាន​កំណត់"
    status     = "🟢 ដំណើរការ" if is_running else "🔴 បញ្ចឹប់"
    bot_info_line = ""
    if token_ok:
        b_name, b_username = _get_bot_info(CLONE_BOT_TOKEN)
        if b_name or b_username:
            name_part = f"<b>{html.escape(b_name)}</b>" if b_name else ""
            user_part = f"@{b_username}" if b_username else ""
            bot_info_line = f"🤖 Bot: {name_part} {user_part}\n"
    msg = (
        f"🎙 <b>TTS Bot — Text to Voice</b>\n\n"
        f"{bot_info_line}"
        f"🔑 Token: {token_disp}\n"
        f"📡 ស្ថានភាព: {status}\n\n"
        f"<i>Bot នឹង​ប្ត្ឧរ​អក្សរ​ជា​សំប័ក​ត្រកម្លស្វ័យប្រវត្តិ</i>\n"
        f"<i>🌐 គាមត្រ: ខ្មែរ · English · 中文 · ภាษาไทย · ...</i>"
    )
    _settings_edit(chat_id, uid, msg, _build_settings_tts_ikb())

def _get_bot_info(token):
    """Fetch bot name and username via getMe. Returns (name, username) or (None, None)."""
    try:
        resp = http.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8)
        data = resp.json()
        if data.get('ok'):
            r = data['result']
            name     = r.get('first_name', '')
            username = r.get('username', '')
            return name, username
    except Exception:
        pass
    return None, None

def _show_translate_bot_menu(chat_id, user_id=None):
    uid        = user_id or chat_id
    token_ok   = bool(TRANSLATE_BOT_TOKEN)
    is_running = TRANSLATE_BOT_ACTIVE and _translate_bot_thread and _translate_bot_thread.is_alive()
    token_disp = f"<code>{TRANSLATE_BOT_TOKEN[:12]}...</code>" if token_ok else "❌ មិនតាន​កំណត់"
    status     = "🟢 ដំណើរការ" if is_running else "🔴 បញ្ចឹប់"
    bot_info_line = ""
    if token_ok:
        b_name, b_username = _get_bot_info(TRANSLATE_BOT_TOKEN)
        if b_name or b_username:
            name_part = f"<b>{html.escape(b_name)}</b>" if b_name else ""
            user_part = f"@{b_username}" if b_username else ""
            bot_info_line = f"🤖 Bot: {name_part} {user_part}\n"
    msg = (
        f"🌐 <b>Translate Bot — បកប្រែភាសា</b>\n\n"
        f"{bot_info_line}"
        f"🔑 Token: {token_disp}\n"
        f"📡 ស្ថានភាព: {status}\n\n"
        f"<i>Bot នឹង​បកប្រែ​អក្សរ​ជាភាសា​ដែល​ជ្រើស​ត្រកម្លស្វ័យប្រវត្តិ</i>\n"
        f"<i>🌍 គាមត្រ: ខ្មែរ · English · 中文 · ภาษาไทย · ...</i>"
    )
    _settings_edit(chat_id, uid, msg, _build_settings_tr_ikb())

def _show_clone_main_menu(chat_id):
    """Redirect to TTS bot menu (legacy)."""
    _show_clone_bot_menu(chat_id)

def _dispatch_settings_panel(chat_id, user_id, key):
    """Navigate the settings inline panel to a sub-menu by key."""
    if key == 'pay':
        _show_payment_inline(chat_id, user_id)
    elif key == 'bak':
        _show_bakong_inline(chat_id, user_id)
    elif key == 'ch':
        _show_channel_inline(chat_id, user_id)
    elif key == 'adm':
        _show_admins_inline(chat_id, user_id)
    elif key == 'mnt':
        _show_maintenance_inline(chat_id, user_id)
    elif key == 'tts':
        _show_clone_bot_menu(chat_id, user_id)
    elif key == 'tr':
        _show_translate_bot_menu(chat_id, user_id)
    else:
        _settings_edit(chat_id, user_id, _settings_main_text(), SETTINGS_MAIN_IKB)
# ── Main message handler ──────────────────────────────────────────────────────
def handle_message(update):
    global MAINTENANCE_MODE, PAYMENT_NAME, CHANNEL_ID, CLONE_BOT_TOKEN
    try:
        if 'callback_query' in update:
            handle_callback_query(update)
            return
        if 'channel_post' in update:
            handle_channel_post(update['channel_post'])
            return
        if 'edited_channel_post' in update:
            handle_channel_post(update['edited_channel_post'])
            return

        message    = update.get('message')
        if not message:
            return
        chat_id    = message['chat']['id']
        message_id = message.get('message_id')
        text       = message.get('text', '')
        user       = message.get('from', {})
        user_id    = user.get('id')

        _set_reply_to_id(message_id)
        logger.info(f"Received message from user {user.get('first_name', 'Unknown')} (ID: {user_id}): {text}")
        notify_admin_new_user(user)

        def show_account_selection_local():
            show_account_selection(chat_id)

        if MAINTENANCE_MODE and not is_admin(user_id):
            send_message(chat_id, "🔧 <b>Bot កំពុង Update សូមរង់ចាំមួយភ្លែត...</b>",
                         parse_mode="HTML", reply_to_message_id=False)
            return

        if text.strip() == '/start':
            logger.info(f"User {user_id} triggered account selection interface")
            existing = user_sessions.get(user_id)
            if existing and existing.get('state') == 'payment_pending':
                _remind_pending_payment(chat_id, existing)
                return
            with _data_lock:
                had_session = user_id in user_sessions
                if had_session:
                    del user_sessions[user_id]
            if had_session:
                save_sessions_async()
            send_account_selection_inline(chat_id)
            return

        if text.strip() == '💵 ទិញគូប៉ុង':
            show_account_selection_local()
            return

        if text.strip().startswith(ACCOUNT_BTN_PREFIX):
            raw          = text.strip()[len(ACCOUNT_BTN_PREFIX):]
            account_type = raw.split(ACCOUNT_BTN_SUFFIX)[0]
            if account_type in accounts_data.get('account_types', {}):
                with _data_lock:
                    accounts = accounts_data['account_types'][account_type]
                    count    = len(accounts)
                    price    = accounts_data['prices'].get(account_type, 0)
                if count > 0:
                    with _data_lock:
                        user_sessions[user_id] = {
                            'state': 'waiting_for_quantity',
                            'account_type': account_type,
                            'price': price,
                            'available_count': count
                        }
                    save_sessions_async()
                    qty_buttons  = [{'text': str(n), 'callback_data': f'qty:{n}'} for n in range(1, count + 1)]
                    qty_rows     = [qty_buttons[i:i+4] for i in range(0, len(qty_buttons), 4)]
                    qty_keyboard = {'inline_keyboard': qty_rows}
                    send_message(chat_id, "*សូមជ្រើសរើសចំនួនដែលចង់ទិញ៖*",
                                 reply_to_message_id=False, parse_mode="Markdown", reply_markup=qty_keyboard)
                else:
                    send_message(chat_id, f"សូមអភ័យទោស Account {account_type} អស់ពីស្តុក 🪤",
                                 reply_markup=_main_kb(user_id))
                    show_account_selection_local()
            else:
                show_account_selection_local()
            return

        if text.strip() == '👤គណនី':
            full_name    = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or 'N/A'
            account_info = (
                f"👤 <b>ឈ្មោះ:</b> {full_name}\n"
                f'<tg-emoji emoji-id="5422683699130933153">🪪</tg-emoji> <b>ID:</b> <code>{user_id}</code>'
            )
            send_message(chat_id, account_info, parse_mode="HTML", reply_markup=_main_kb(user_id))
            return

        if text.strip() == '🧾ប្រវត្តិទិញ':
            rows = get_purchase_history(user_id, limit=20)
            if not rows:
                send_message(chat_id, "📭 <b>អ្នកមិនទាន់មានប្រវត្តិទិញទេ។</b>", parse_mode="HTML")
            else:
                import datetime
                cambodia_tz = datetime.timezone(datetime.timedelta(hours=7))
                send_message(chat_id, "ការទិញចំនួន ២០ ដងចុងក្រោយរបស់អ្នក:")
                for row in rows:
                    try:
                        dt    = datetime.datetime.fromisoformat(str(row.get('purchased_at', '')).replace('Z', '+00:00'))
                        dt_kh = dt.astimezone(cambodia_tz).strftime("%d/%m/%Y %H:%M")
                    except Exception:
                        dt_kh = str(row.get('purchased_at', ''))
                    accs = row.get('accounts') or []
                    if isinstance(accs, str):
                        try:
                            accs = json.loads(accs)
                        except Exception:
                            accs = []
                    coupon_lines = ""
                    for acc in accs:
                        if 'email' in acc:
                            coupon_lines += f"\n{acc['email']}"
                        else:
                            val = acc.get('phone') or acc.get('password') or ''
                            coupon_lines += f"\n{val}"
                    msg = (
                        f"⎙ <b>ព័ត៌មានប្រតិបត្តិការ</b>\n\n"
                        f"▫️ ប្រភេទ: {row.get('account_type', 'N/A')}\n"
                        f"▫️ ចំនួន: {row.get('quantity', 0)}\n"
                        f"▫️ តម្លៃ: {row.get('total_price', 0)}$\n"
                        f"▫️កាលបរិច្ឆេទ: {dt_kh}\n"
                        f"\n<b>⌲ គូប៉ុង E-GetS</b>\n"
                        f"{coupon_lines}"
                    )
                    send_message(chat_id, msg, parse_mode="HTML", reply_to_message_id=False)
            return

        if text.strip() == '/settings' and is_admin(user_id):
            if user_id in user_sessions and str(user_sessions[user_id].get('state', '')).startswith('admin_input:'):
                with _data_lock:
                    del user_sessions[user_id]
                save_sessions_async()
            send_admin_settings_menu(chat_id)
            return

        if is_admin(user_id) and user_id in user_sessions:
            _state = str(user_sessions[user_id].get('state', ''))
            if _state.startswith('admin_input:'):
                _key = _state.split(':', 1)[1]
                if _handle_admin_settings_input(chat_id, user_id, message_id, _key, text):
                    return



        if user_id in user_sessions:
            session = user_sessions[user_id]

            if session.get('state') == 'payment_pending':
                _remind_pending_payment(chat_id, session)
                return

            if session.get('state') == 'translate_mode' and is_admin(user_id):
                lang_code = session.get('lang_code', 'en')
                lang_name = session.get('lang_name', lang_code)
                if not text or not text.strip():
                    send_message(chat_id, "💬 សូម​ផ្ញើ​អក្សរ​ដែល​ចង់​បកប្រែ។",
                                 reply_to_message_id=False, reply_markup=TRANSLATE_SUBMENU_KEYBOARD)
                    return
                translated = _translate_text(text.strip(), lang_code)
                if translated:
                    send_message(
                        chat_id,
                        f"🌐 <b>{lang_name}</b>\n\n<blockquote>{html.escape(translated)}</blockquote>",
                        parse_mode="HTML",
                        reply_to_message_id=False,
                        reply_markup=TRANSLATE_SUBMENU_KEYBOARD
                    )
                else:
                    send_message(chat_id, "❌ មានបញ្ហាក្នុងការបកប្រែ សូម​ព្យាយាម​ម្តងទៀត។",
                                 reply_to_message_id=False, reply_markup=TRANSLATE_SUBMENU_KEYBOARD)
                return

            if session.get('state') == 'clone_add_name' and is_admin(user_id):
                raw_name = text.strip()
                if not raw_name or raw_name == BTN_CANCEL_INPUT:
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                    send_message(chat_id, "🚫 បានបោះបង់", reply_to_message_id=False,
                                 reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                    return
                bot_id = hashlib.md5((raw_name + str(time.time())).encode()).hexdigest()[:8]
                with _data_lock:
                    user_sessions[user_id] = {'state': 'clone_add_token',
                                              'bot_id': bot_id, 'bot_name': raw_name}
                save_sessions_async()
                send_message(chat_id,
                    f"✅ ឈ្មោះ: <b>{html.escape(raw_name)}</b>\n\n"
                    f"🔑 សូមផ្ញើ <b>Bot Token</b> ពី @BotFather:",
                    parse_mode='HTML', reply_to_message_id=False,
                    reply_markup=CANCEL_INPUT_KEYBOARD)
                return

            if session.get('state') == 'clone_add_token' and is_admin(user_id):
                token    = text.strip()
                bot_id   = session.get('bot_id')
                bot_name = session.get('bot_name', 'Clone Bot')
                if token == BTN_CANCEL_INPUT:
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                    send_message(chat_id, "🚫 បានបោះបង់", reply_to_message_id=False,
                                 reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                    return
                with _clone_bots_lock:
                    _clone_bots_list.append({'id': bot_id, 'name': bot_name, 'token': token,
                                             'active': False, 'thread': None, 'stop_event': None})
                _save_clone_bots()
                with _data_lock:
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                save_sessions_async()
                delete_message_async(chat_id, message_id)
                _show_clone_bot_detail(chat_id, bot_id)
                return

            if session.get('state') == 'clone_set_token' and is_admin(user_id):
                token  = text.strip()
                bot_id = session.get('bot_id')
                if token == BTN_CANCEL_INPUT:
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                    send_message(chat_id, "🚫 បានបោះបង់", reply_to_message_id=False,
                                 reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                    return
                with _clone_bots_lock:
                    bot = next((b for b in _clone_bots_list if b['id'] == bot_id), None)
                    if bot:
                        if bot.get('stop_event'):
                            bot['stop_event'].set()
                        bot['token']     = token
                        bot['active']    = False
                        bot['thread']    = None
                        bot['stop_event'] = None
                _save_clone_bots()
                with _data_lock:
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                save_sessions_async()
                delete_message_async(chat_id, message_id)
                _show_clone_bot_detail(chat_id, bot_id)
                return

            if session['state'] == 'waiting_for_quantity':
                with _data_lock:
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                save_sessions_async()
                send_account_selection_inline(chat_id)
                return

            elif session['state'] == 'waiting_for_confirmation':
                if text.strip() == '✅ យល់ព្រម':
                    with _data_lock:
                        session['state'] = 'payment_pending'
                    try:
                        img_bytes, md5_or_err, qr_string = generate_payment_qr(session['total_price'])
                        if not img_bytes:
                            err_detail = md5_or_err or "មិនដឹងមូលហេតុ"
                            send_message(chat_id, "❌ *មានបញ្ហាក្នុងការបង្កើត QR Code*\n\nសូមព្យាយាមម្តងទៀត។", parse_mode="Markdown")
                            send_message(ADMIN_ID, f"⚠️ *QR Error (user {user_id}):*\n`{err_detail}`", parse_mode="Markdown")
                            with _data_lock:
                                if user_id in user_sessions:
                                    del user_sessions[user_id]
                            save_sessions_async()
                            return
                        md5_hash = md5_or_err
                        session['md5_hash']   = md5_hash
                        session['qr_sent_at'] = time.time()
                        dot_resp = send_sticker(chat_id, "CAACAgUAAxkBAAILvGnnaWwK-AXFeING4WOtIIKmoFYqAAIVAAMxIPsrpHGBfRB524Y7BA",
                                                reply_markup=_main_kb(user_id))
                        if dot_resp and dot_resp.get('result'):
                            session['dot_message_id'] = dot_resp['result']['message_id']
                        photo_resp = send_photo_bytes(chat_id, img_bytes, reply_markup=CHECK_PAYMENT_KEYBOARD)
                        if photo_resp and photo_resp.get('result'):
                            msg_id = photo_resp['result']['message_id']
                            session['photo_message_id'] = msg_id
                            session['qr_message_id']    = msg_id
                        save_sessions_async()
                        save_pending_payment_async(user_id, chat_id, session)
                    except Exception as e:
                        logger.error(f"Error generating KHQR: {type(e).__name__}: {e}")
                        send_message(chat_id, "❌ *មានបញ្ហាក្នុងការបង្កើត QR Code*\n\nសូមព្យាយាមម្តងទៀត។", parse_mode="Markdown")
                        with _data_lock:
                            if user_id in user_sessions:
                                del user_sessions[user_id]
                        save_sessions_async()
                    return

                elif text.strip() == '🚫 បោះបង់':
                    summary_msg_id = session.get('summary_message_id')
                    if summary_msg_id:
                        delete_message_async(chat_id, summary_msg_id)
                    dot_msg_id = session.get('dot_message_id')
                    if dot_msg_id:
                        delete_message_async(chat_id, dot_msg_id)
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                    send_account_selection_inline(chat_id)
                    return


        if not is_admin(user_id):
            existing = user_sessions.get(user_id)
            if existing and existing.get('state') == 'payment_pending':
                _remind_pending_payment(chat_id, existing)
                return
            show_account_selection_local()
            return

        if is_admin(user_id):
            if user_id in user_sessions:
                session = user_sessions[user_id]

                if session['state'] == 'waiting_for_accounts':
                    email_pattern = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
                    accounts      = []
                    seen_in_batch = set()
                    lines         = text.strip().split('\n')
                    for line in lines:
                        email = line.strip()
                        if email and email_pattern.match(email):
                            key = email.lower()
                            if key not in seen_in_batch:
                                seen_in_batch.add(key)
                                accounts.append({'email': email})

                    if not accounts:
                        _settings_edit(chat_id, user_id,
                            "❌ <b>មិនរកឃើញ Email ត្រឹមត្រូវ!</b>\n\n"
                            "📧 ផ្ញើ Email ម្តងមួយបន្ទាត់:\n\n"
                            "<code>l1jebywyzos2@10mail.info\nabc123@gmail.com</code>\n\n"
                            "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                            SETTINGS_CANCEL_IKB)
                        return

                    # Store accounts temporarily and ask for account type
                    with _data_lock:
                        user_sessions[user_id] = {
                            'state': 'waiting_for_account_type',
                            'pending_accounts': accounts
                        }
                    save_sessions_async()
                    _settings_edit(chat_id, user_id,
                        f"✅ <b>បានទទួល {len(accounts)} Accounts</b>\n\n"
                        "📂 សូមបញ្ចូលប្រភេទ Account (ឧ. Facebook, TikTok):\n\n"
                        "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                        SETTINGS_CANCEL_IKB)
                    return

                if session['state'] == 'waiting_for_account_type':
                    account_type = text.strip()
                    if not account_type:
                        _settings_edit(chat_id, user_id,
                            "⚠️ <b>សូមបញ្ចូលប្រភេទ Account ជាអក្សរ</b>\n\n"
                            "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                            SETTINGS_CANCEL_IKB)
                        return
                    pending = session.get('pending_accounts', [])
                    existing_price = accounts_data.get('prices', {}).get(account_type, 0)
                    with _data_lock:
                        user_sessions[user_id] = {
                            'state': 'waiting_for_price',
                            'pending_accounts': pending,
                            'account_type': account_type
                        }
                    save_sessions_async()
                    price_hint = f"\n💵 <b>តម្លៃបច្ចុប្បន្ន: ${existing_price}</b>" if existing_price else ""
                    _settings_edit(chat_id, user_id,
                        f"📂 <b>ប្រភេទ: {html.escape(account_type)}</b>{price_hint}\n\n"
                        "💲 សូមបញ្ចូលតម្លៃ (USD) ក្នុងមួយ Account:\n\n"
                        "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                        SETTINGS_CANCEL_IKB)
                    return

                if session['state'] == 'waiting_for_price':
                    try:
                        price        = float(text.strip())
                        account_type = session['account_type']
                        accounts     = session.get('pending_accounts', [])

                        # Deduplicate against existing stock
                        with _data_lock:
                            existing_emails = {
                                a.get('email', '').lower()
                                for accs in accounts_data.get('account_types', {}).values()
                                for a in accs if a.get('email')
                            }
                        try:
                            sold_rows = _neon_query(
                                "SELECT accounts FROM bot_purchase_history WHERE accounts IS NOT NULL"
                            ).get('rows', []) or []
                            for sr in sold_rows:
                                sold_accs = sr.get('accounts') or []
                                if isinstance(sold_accs, str):
                                    try:
                                        sold_accs = json.loads(sold_accs)
                                    except Exception:
                                        sold_accs = []
                                for sa in sold_accs:
                                    if isinstance(sa, dict) and sa.get('email'):
                                        existing_emails.add(sa['email'].lower())
                        except Exception as sold_err:
                            logger.warning(f"Could not fetch sold emails: {sold_err}")

                        duplicate_emails = [a['email'] for a in accounts if a['email'].lower() in existing_emails]
                        new_accounts     = [a for a in accounts if a['email'].lower() not in existing_emails]

                        if duplicate_emails:
                            dup_list = '\n'.join(f'• {e}' for e in duplicate_emails)
                            if not new_accounts:
                                _settings_edit(chat_id, user_id,
                                    f"❌ <b>មិនអាចបញ្ចូលបាន!</b>\n\nEmail ទាំងអស់មានស្រាប់ក្នុងប្រព័ន្ធ:\n<code>{html.escape(chr(10).join(duplicate_emails))}</code>\n\n"
                                    "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>", SETTINGS_CANCEL_IKB)
                                return
                            else:
                                _settings_edit(chat_id, user_id,
                                    f"⚠️ <b>Email ខាងក្រោមមានស្រាប់ ហើយត្រូវបានរំលង:</b>\n<code>{html.escape(chr(10).join(duplicate_emails))}</code>",
                                    SETTINGS_CANCEL_IKB)

                        accounts = new_accounts
                        count    = len(accounts)

                        with _data_lock:
                            accounts_data['accounts'].extend(accounts)
                            if account_type in accounts_data['account_types']:
                                accounts_data['account_types'][account_type].extend(accounts)
                            else:
                                accounts_data['account_types'][account_type] = accounts
                            accounts_data['prices'][account_type] = price
                            if user_id in user_sessions:
                                del user_sessions[user_id]
                        save_data()
                        save_sessions_async()

                        _settings_edit(chat_id, user_id,
                            f"✅ <b>បានបញ្ចូល Account ដោយជោគជ័យ</b>\n\n"
                            f"<blockquote>🔹 ចំនួន: {count}\n🔹 ប្រភេទ: {html.escape(account_type)}\n🔹 តម្លៃ: ${price}</blockquote>",
                            {'inline_keyboard': [[{'text': '↩️ ត្រឡប់ទៅ Settings', 'callback_data': 's:main'}]]})
                        logger.info(f"Admin {user_id} added {count} accounts of type {account_type} with price ${price}")

                    except ValueError:
                        _settings_edit(chat_id, user_id,
                            "❌ <b>តម្លៃមិនត្រឹមត្រូវ</b>\n\nសូមបញ្ចូលតម្លៃជាលេខ (ឧ. <code>5.99</code>)\n\n"
                            "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>", SETTINGS_CANCEL_IKB)
                    return

            if user_id in user_sessions:
                with _data_lock:
                    del user_sessions[user_id]
                logger.info(f"Cleared session for admin {user_id} due to unrecognized command")
            logger.info(f"Admin {user_id} sent unrecognized command, showing account selection interface")
            show_account_selection_local()

    except Exception as e:
        logger.error(f"Error handling message: {e}")

# ── Bot API long-polling loop ─────────────────────────────────────────────────
def _get_updates(offset=None, timeout=30):
    params = {
        'timeout': timeout,
        'allowed_updates': ['message', 'callback_query', 'channel_post'],
    }
    if offset is not None:
        params['offset'] = offset
    try:
        resp = http.get(f"{BOT_API_URL}getUpdates", params=params, timeout=timeout + 10)
        data = resp.json()
        if data.get('ok'):
            return data.get('result', [])
        logger.warning(f"getUpdates error: {data.get('description')}")
    except Exception as e:
        logger.error(f"getUpdates failed: {type(e).__name__}: {e}")
    return []

def _polling_loop():
    offset = None
    logger.info("Bot API long-polling started. Waiting for updates...")
    while True:
        try:
            updates = _get_updates(offset=offset)
            for update in updates:
                offset = update['update_id'] + 1
                worker_pool.submit(handle_message, update)
        except Exception as e:
            logger.error(f"Polling loop error: {e}")
            time.sleep(5)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # Single-process lock (prevent duplicate instances)
    lock_file = open('/tmp/telegram_bot_simple.lock', 'w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another bot process is already running. Exiting duplicate process.")
        return

    logger.info("Starting Telegram Bot (Bot API HTTP polling)...")
    logger.info(f"Bot token configured: {BOT_TOKEN[:10]}...")

    # Re-arm scheduled deletions from DB
    resume_scheduled_deletions()

    # Start Neon keep-alive daemon
    _ka_thread = threading.Thread(target=_neon_keepalive, daemon=True, name="neon-keepalive")
    _ka_thread.start()
    logger.info("Neon keep-alive thread started (ping every 4 minutes)")

    if CLONE_BOT_ACTIVE and CLONE_BOT_TOKEN:
        _start_clone_bot(CLONE_BOT_TOKEN)
        logger.info("TTS Clone Bot resumed from saved state")

    _load_clone_bots()
    for _cb in list(_clone_bots_list):
        if _cb.get('active') and _cb.get('token'):
            _start_clone_bot_by_id(_cb['id'])
            logger.info(f"Clone Bot '{_cb['name']}' resumed")

    if TRANSLATE_BOT_ACTIVE and TRANSLATE_BOT_TOKEN:
        _start_translate_bot(TRANSLATE_BOT_TOKEN)
        logger.info("Translate Bot resumed from saved state")

    try:
        _polling_loop()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
