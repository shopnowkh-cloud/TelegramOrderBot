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
        logger.info("Replit PostgreSQL DB initialized")
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
BTN_CLONE_BOT         = 'បង្កើតសំឡេង Ai'
BTN_CLONE_MENU        = '🤖 Clone Bot'
BTN_CLONE_START       = '▶️ ចាប់ផ្តើម Clone Bot'
BTN_CLONE_STOP        = '⏹ បញ្ឈប់ Clone Bot'
BTN_CLONE_SET_TOKEN   = '🔑 កំណត់ Token'
BTN_CLONE_TOKEN_CLEAR = '🗑 លុប Token'
BTN_TRANSLATE         = '🌐 បកប្រែភាសា'

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

BROADCAST_CONFIRM_KEYBOARD = {
    'keyboard': [
        [{'text': BTN_BROADCAST_CONFIRM}],
        [{'text': BTN_BROADCAST_CANCEL}],
    ],
    'resize_keyboard': True,
    'is_persistent': True
}

ADMIN_SETTINGS_REPLY_KEYBOARD = {
    'keyboard': [
        [{'text': BTN_ADD_ACCOUNT}, {'text': BTN_DELETE_TYPE}],
        [{'text': BTN_BUYERS}, {'text': BTN_PAYMENT}],
        [{'text': BTN_BAKONG}, {'text': BTN_CHANNEL}],
        [{'text': BTN_MAINTENANCE}],
        [{'text': BTN_CLONE_MENU, 'style': 'primary'}],
    ],
    'resize_keyboard': True,
    'is_persistent': True
}

PAYMENT_SUBMENU_KEYBOARD = {
    'keyboard': [[{'text': BTN_PAYMENT_EDIT}], [{'text': BTN_BACK_SETTINGS}]],
    'resize_keyboard': True, 'is_persistent': True
}
BAKONG_SUBMENU_KEYBOARD = {
    'keyboard': [[{'text': BTN_BAKONG_EDIT}], [{'text': BTN_BACK_SETTINGS}]],
    'resize_keyboard': True, 'is_persistent': True
}
CHANNEL_SUBMENU_KEYBOARD = {
    'keyboard': [[{'text': BTN_CHANNEL_EDIT}, {'text': BTN_CHANNEL_CLEAR}], [{'text': BTN_BACK_SETTINGS}]],
    'resize_keyboard': True, 'is_persistent': True
}
ADMINS_SUBMENU_KEYBOARD = {
    'keyboard': [[{'text': BTN_ADMIN_ADD}, {'text': BTN_ADMIN_REMOVE}], [{'text': BTN_BACK_SETTINGS}]],
    'resize_keyboard': True, 'is_persistent': True
}
MAINTENANCE_SUBMENU_KEYBOARD = {
    'keyboard': [[{'text': BTN_MAINT_ON}, {'text': BTN_MAINT_OFF}], [{'text': BTN_BACK_SETTINGS}]],
    'resize_keyboard': True, 'is_persistent': True
}
CLONE_BOT_MENU_KEYBOARD_ACTIVE = {
    'keyboard': [
        [{'text': BTN_CLONE_STOP}],
        [{'text': BTN_CLONE_SET_TOKEN}, {'text': BTN_CLONE_TOKEN_CLEAR}],
        [{'text': BTN_CLONE_BOT}, {'text': BTN_TRANSLATE}],
        [{'text': BTN_BACK_SETTINGS}],
    ], 'resize_keyboard': True, 'is_persistent': True
}
CLONE_BOT_MENU_KEYBOARD_INACTIVE = {
    'keyboard': [
        [{'text': BTN_CLONE_START}],
        [{'text': BTN_CLONE_SET_TOKEN}, {'text': BTN_CLONE_TOKEN_CLEAR}],
        [{'text': BTN_CLONE_BOT}, {'text': BTN_TRANSLATE}],
        [{'text': BTN_BACK_SETTINGS}],
    ], 'resize_keyboard': True, 'is_persistent': True
}
TRANSLATE_SUBMENU_KEYBOARD = {
    'keyboard': [[{'text': BTN_BACK_SETTINGS}]],
    'resize_keyboard': True, 'is_persistent': True
}
CANCEL_INPUT_KEYBOARD = {
    'keyboard': [[{'text': BTN_CANCEL_INPUT}]],
    'resize_keyboard': True, 'one_time_keyboard': False, 'is_persistent': True
}
ADD_ACCOUNT_KEYBOARD = {
    'keyboard': [[{'text': BTN_BACK_SETTINGS}]],
    'resize_keyboard': True, 'is_persistent': True
}
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
    BTN_PAYMENT, BTN_BAKONG, BTN_CHANNEL, BTN_MAINTENANCE,
    BTN_BACK_SETTINGS,
    BTN_PAYMENT_EDIT, BTN_BAKONG_EDIT,
    BTN_CHANNEL_EDIT, BTN_CHANNEL_CLEAR,
    BTN_ADMIN_ADD, BTN_ADMIN_REMOVE,
    BTN_MAINT_ON, BTN_MAINT_OFF,
    BTN_CLONE_BOT, BTN_CLONE_MENU, BTN_CLONE_START, BTN_CLONE_STOP,
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

# ── Admin menu helpers ────────────────────────────────────────────────────────
def send_admin_settings_menu(chat_id):
    send_message(
        chat_id,
        "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើសប្រតិបត្តិការខាងក្រោម៖",
        parse_mode="HTML",
        reply_to_message_id=False,
        reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD
    )

def _prompt_admin_input(chat_id, user_id, key, prompt_text):
    with _data_lock:
        user_sessions[user_id] = {'state': f'admin_input:{key}'}
    save_sessions_async()
    send_message(
        chat_id,
        prompt_text + "\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
        parse_mode="HTML",
        reply_to_message_id=False,
        reply_markup=CANCEL_INPUT_KEYBOARD
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
    back_keyboard = {'keyboard': [[{'text': BTN_BACK_SETTINGS}]], 'resize_keyboard': True, 'is_persistent': True}
    if not rows:
        send_message(chat_id, "📭 <b>មិនទាន់មានអ្នកប្រើប្រាស់ទេ។</b>",
                     parse_mode="HTML", reply_to_message_id=False, reply_markup=back_keyboard)
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
    result   = _send_document_bytes(chat_id, txt, filename, caption=f"👥 បញ្ជីអ្នកប្រើប្រាស់ — {total} នាក់")
    if not result:
        send_message(chat_id, "❌ បរាជ័យក្នុងការផ្ញើ​ឯកសារ",
                     reply_to_message_id=False, reply_markup=back_keyboard)
        return
    send_message(chat_id, "↩️ ជ្រើសរើសខាងក្រោម៖",
                 reply_to_message_id=False, reply_markup=back_keyboard)

def _show_delete_type_menu_inline(chat_id, user_id=None):
    types = [
        t for t in accounts_data.get('account_types', {}).keys()
        if len(accounts_data['account_types'].get(t, [])) > 0
    ]
    if not types:
        send_message(chat_id, "⚠️ <b>មិនមានប្រភេទ Account ណាមួយទេ!</b>",
                     parse_mode="HTML", reply_to_message_id=None)
        send_admin_settings_menu(chat_id)
        return
    rows = []
    labels_map = {}
    for t in types:
        count = len(accounts_data['account_types'].get(t, []))
        price = accounts_data.get('prices', {}).get(t, 0)
        label = f"{_short_label(t)} ({count} pcs · ${price})"
        rows.append([{'text': label}])
        labels_map[label] = t
    rows.append([{'text': BTN_BACK_SETTINGS}])
    reply_keyboard = {'keyboard': rows, 'resize_keyboard': True, 'is_persistent': True}
    uid = user_id if user_id is not None else chat_id
    with _data_lock:
        user_sessions[uid] = {'state': 'delete_type_select', 'labels': labels_map}
    save_sessions_async()
    send_message(chat_id, "🗑 <b>ជ្រើសរើសប្រភេទ Account ដែលចង់លុប៖</b>",
                 parse_mode="HTML", reply_to_message_id=False, reply_markup=reply_keyboard)

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
        else:
            send_message(chat_id, "↩️ ត្រឡប់ទៅកំណត់",
                         reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
    except Exception as e:
        logger.error(f"buyers export failed: {e}")
        send_message(chat_id, f"❌ Error: <code>{html.escape(str(e))}</code>",
                     parse_mode="HTML", reply_to_message_id=False)

def _show_admins_inline(chat_id):
    extras = sorted(EXTRA_ADMIN_IDS)
    extras_str = "\n".join(f"• <code>{x}</code>" for x in extras) if extras else "(គ្មាន)"
    text_msg = (
        f"👑 <b>Admin បឋម៖</b> <code>{ADMIN_ID}</code>\n\n"
        f"➕ <b>Admin បន្ថែម៖</b>\n{extras_str}"
    )
    send_message(chat_id, text_msg, parse_mode="HTML",
                 reply_to_message_id=False, reply_markup=ADMINS_SUBMENU_KEYBOARD)

def _show_channel_inline(chat_id):
    current  = CHANNEL_ID if CHANNEL_ID else "(មិនទាន់កំណត់)"
    text_msg = f"📢 <b>Channel ID បច្ចុប្បន្ន៖</b>\n<code>{html.escape(str(current))}</code>"
    send_message(chat_id, text_msg, parse_mode="HTML",
                 reply_to_message_id=False, reply_markup=CHANNEL_SUBMENU_KEYBOARD)

def _show_payment_inline(chat_id):
    text_msg = f"💳 <b>ឈ្មោះ Payment បច្ចុប្បន្ន៖</b>\n<code>{html.escape(PAYMENT_NAME or '(មិនទាន់កំណត់)')}</code>"
    send_message(chat_id, text_msg, parse_mode="HTML",
                 reply_to_message_id=False, reply_markup=PAYMENT_SUBMENU_KEYBOARD)

def _show_bakong_inline(chat_id):
    full     = BAKONG_TOKEN if BAKONG_TOKEN else "(មិនទាន់កំណត់)"
    text_msg = f"🔑 <b>Bakong Token បច្ចុប្បន្ន៖</b>\n<code>{html.escape(full)}</code>"
    send_message(chat_id, text_msg, parse_mode="HTML",
                 reply_to_message_id=False, reply_markup=BAKONG_SUBMENU_KEYBOARD)

def _show_maintenance_inline(chat_id):
    status   = "🔴 បិទ" if MAINTENANCE_MODE else "🟢 បើក"
    text_msg = f"🛠 <b>ស្ថានភាព Bot បច្ចុប្បន្ន៖</b> {status}"
    send_message(chat_id, text_msg, parse_mode="HTML",
                 reply_to_message_id=False, reply_markup=MAINTENANCE_SUBMENU_KEYBOARD)

def _start_add_account_flow(chat_id, user_id, message_id):
    with _data_lock:
        user_sessions[user_id] = {'state': 'waiting_for_accounts'}
    save_sessions_async()
    send_message(
        chat_id,
        "*បញ្ចូល Account សម្រាប់លក់ (អ៊ីមែលម្តងមួយបន្ទាត់)៖*\n\n"
        "```\nl1jebywyzos2@10mail.info\nabc123@gmail.com\n```",
        reply_to_message_id=message_id, parse_mode="Markdown",
        reply_markup=ADD_ACCOUNT_KEYBOARD
    )

def _handle_admin_settings_input(chat_id, user_id, message_id, key, text):
    global PAYMENT_NAME, BAKONG_TOKEN, khqr_client, CHANNEL_ID, EXTRA_ADMIN_IDS, CLONE_BOT_TOKEN

    raw          = (text or '').strip()
    cancel_words = {'បោះបង់', '🚫 បោះបង់'}
    if raw in cancel_words:
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        send_message(chat_id, "🚫 បានបោះបង់ការកំណត់",
                     reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
        return True

    if raw == BTN_BACK_SETTINGS:
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        send_admin_settings_menu(chat_id)
        return True

    if key == 'payment':
        if not raw:
            send_message(chat_id, "សូមផ្ញើឈ្មោះ Payment ថ្មី (ឬចុច 🚫 បោះបង់)", reply_to_message_id=False)
            return True
        PAYMENT_NAME = raw
        set_setting('PAYMENT_NAME', PAYMENT_NAME)
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        send_message(chat_id, f"✅ បានប្តូរឈ្មោះ Payment ទៅជា <b>{html.escape(PAYMENT_NAME)}</b>",
                     parse_mode="HTML", reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
        return True

    if key == 'bakong':
        if not raw:
            send_message(chat_id, "សូមផ្ញើ Bakong token ថ្មី (ឬចុច 🚫 បោះបង់)", reply_to_message_id=False)
            return True
        try:
            new_client = KHQR(raw)
        except Exception as e:
            send_message(chat_id, f"❌ Token មិនត្រឹមត្រូវ៖ <code>{html.escape(str(e))}</code>",
                         parse_mode="HTML", reply_to_message_id=False)
            return True
        BAKONG_TOKEN = raw
        khqr_client  = new_client
        set_setting('BAKONG_TOKEN', raw)
        delete_message_async(chat_id, message_id)
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        send_message(
            chat_id,
            f"✅ បានប្តូរ Bakong token (Prefix៖ <code>{html.escape(raw[:10])}…</code>)",
            parse_mode="HTML", reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD
        )
        return True

    if key == 'channel':
        if not raw:
            send_message(chat_id,
                         "សូមផ្ញើ Channel ID ថ្មី (ឧ. <code>-1001234567890</code>) ឬ <code>off</code> ដើម្បីបិទ",
                         parse_mode="HTML", reply_to_message_id=False)
            return True
        if raw.lower() in ('off', 'none', 'clear', 'delete', 'remove'):
            CHANNEL_ID = ""
            set_setting('TELEGRAM_CHANNEL_ID', '')
            with _data_lock:
                if user_id in user_sessions:
                    del user_sessions[user_id]
            save_sessions_async()
            send_message(chat_id, "✅ បានលុប Channel ID",
                         reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
            return True
        CHANNEL_ID = raw
        set_setting('TELEGRAM_CHANNEL_ID', raw)
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        send_message(
            chat_id,
            f"✅ បានកំណត់ Channel ID ទៅជា <code>{html.escape(raw)}</code>\n"
            f"សូមប្រាកដថា bot ជា admin/member ក្នុង channel នោះ។",
            parse_mode="HTML", reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD
        )
        return True

    if key in ('admin_add', 'admin_remove'):
        action = 'add' if key == 'admin_add' else 'remove'
        try:
            target_id = int(raw)
        except ValueError:
            send_message(chat_id, "❌ user_id ត្រូវតែជាលេខ (ឬចុច 🚫 បោះបង់)", reply_to_message_id=False)
            return True
        if target_id == ADMIN_ID:
            send_message(chat_id, "ℹ️ Admin បឋមមិនអាចលុប/បន្ថែមបានទេ។",
                         reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
            with _data_lock:
                if user_id in user_sessions:
                    del user_sessions[user_id]
            save_sessions_async()
            return True
        if action == 'add':
            EXTRA_ADMIN_IDS.add(target_id)
            msg = f"✅ បានបន្ថែម <code>{target_id}</code> ជា admin"
        else:
            EXTRA_ADMIN_IDS.discard(target_id)
            msg = f"✅ បានដក <code>{target_id}</code> ចេញពី admin"
        set_setting('EXTRA_ADMIN_IDS', json.dumps(sorted(EXTRA_ADMIN_IDS)))
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        send_message(chat_id, msg, parse_mode="HTML",
                     reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
        return True

    if key == 'clone_token':
        if not raw:
            send_message(chat_id, "សូម​ផ្ញើ Token របស់ Clone Bot (ឬ​ចុច 🚫 បោះបង់)", reply_to_message_id=False)
            return True
        try:
            test_resp = http.get(f"https://api.telegram.org/bot{raw}/getMe", timeout=10).json()
            if not test_resp.get('ok'):
                send_message(chat_id,
                    f"❌ Token មិន​ត្រឹម​ត្រូវ: {test_resp.get('description', 'Unknown error')}",
                    reply_to_message_id=False)
                return True
            bot_info = test_resp.get('result', {})
            bot_name = bot_info.get('first_name', 'Bot')
            bot_username = bot_info.get('username', '')
        except Exception as e:
            send_message(chat_id, f"❌ មិន​អាច​ភ្ជាប់ Telegram: {e}", reply_to_message_id=False)
            return True
        CLONE_BOT_TOKEN = raw
        set_setting('CLONE_BOT_TOKEN', raw)
        delete_message_async(chat_id, message_id)
        with _data_lock:
            if user_id in user_sessions:
                del user_sessions[user_id]
        save_sessions_async()
        send_message(
            chat_id,
            f"✅ <b>ស្ថាបនា Clone Bot ជោគជ័យ!</b>\n\n"
            f"🤖 Bot: <b>{html.escape(bot_name)}</b> (@{html.escape(bot_username)})\n\n"
            f"<i>ឥឡូវ​ចុច ▶️ ចាប់ផ្តើម ដើម្បី​បើក Clone Bot</i>",
            parse_mode="HTML", reply_to_message_id=False,
            reply_markup=CLONE_BOT_MENU_KEYBOARD_INACTIVE
        )
        return True

    if key == 'broadcast':
        if not message_id:
            send_message(chat_id, "សូមផ្ញើ​សារ​ដែល​ចង់​ផ្សាយ (ឬចុច 🚫 បោះបង់)", reply_to_message_id=False)
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
        send_message(
            chat_id,
            "❓ <b>តើ​អ្នក​ប្រាកដ​ជា​ចង់​ផ្សាយ​សារ​ខាង​លើ​នេះ​ទៅ​អ្នក​ប្រើ​ប្រាស់​ទាំង​អស់​មែន​ទេ?</b>\n\n"
            "ចុច <b>✅ បញ្ជាក់ផ្សាយ</b> ដើម្បី​ផ្សាយ ឬ <b>🚫 បោះបង់ការផ្សាយ</b> ដើម្បី​បោះបង់។",
            parse_mode="HTML", reply_to_message_id=False, reply_markup=BROADCAST_CONFIRM_KEYBOARD
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
        send_message(admin_chat_id, summary, parse_mode="HTML",
                     reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
    except Exception as e:
        logger.error(f"Broadcast crashed: {e}")
        try:
            send_message(admin_chat_id,
                         f"❌ Broadcast error: <code>{html.escape(str(e))}</code>",
                         parse_mode="HTML", reply_to_message_id=False,
                         reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
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
            count    = len(accounts_data['account_types'].get(type_name, []))
            price    = accounts_data.get('prices', {}).get(type_name, 0)
            confirm_cb = f"dtc:{_type_callback_id(type_name)}"
            keyboard = {'inline_keyboard': [[
                {'text': '✅ បញ្ជាក់លុប', 'callback_data': confirm_cb},
                {'text': '🚫 បោះបង់',     'callback_data': 'dtcancel'}
            ]]}
            send_message(chat_id,
                f"⚠️ <b>តើអ្នកពិតជាចង់លុបប្រភេទ Account នេះមែនទេ?</b>\n\n"
                f"<blockquote>🔹 ប្រភេទ: {type_name}\n🔹 ចំនួន Account: {count}\n🔹 តម្លៃ: ${price}</blockquote>\n\n"
                f"Account ទាំងអស់ក្នុងប្រភេទនេះនឹងត្រូវបានលុបចោលជាអចិន្ត្រៃយ៍!",
                parse_mode="HTML", reply_to_message_id=None, reply_markup=keyboard)
            return

        elif callback_data.startswith('dtc:') and is_admin(user_id):
            type_name = _account_type_from_callback_id(callback_data[4:]) or callback_data[4:]
            if type_name not in accounts_data.get('account_types', {}):
                answer_callback(callback_query['id'], 'ប្រភេទនេះមិនមានទៀតហើយ!', True)
                return
            answer_callback(callback_query['id'])
            count = len(accounts_data['account_types'].pop(type_name, []))
            accounts_data.get('prices', {}).pop(type_name, None)
            accounts_data['accounts'] = [a for a in accounts_data.get('accounts', []) if a.get('type') != type_name]
            save_data()
            delete_message_async(chat_id, callback_query['message']['message_id'])
            send_message(chat_id,
                f"✅ <b>បានលុបប្រភេទ Account <code>{type_name}</code> ចំនួន {count} records ដោយជោគជ័យ!</b>",
                parse_mode="HTML", reply_to_message_id=None)
            return

        elif callback_data == 'dtcancel' and is_admin(user_id):
            answer_callback(callback_query['id'])
            delete_message_async(chat_id, callback_query['message']['message_id'])
            send_message(chat_id, "🚫 <b>បានបោះបង់ការលុបប្រភេទ Account</b>",
                         parse_mode="HTML", reply_to_message_id=None)
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

        elif callback_data == 'clone_feat_voice' and is_admin(user_id):
            answer_callback(callback_query['id'])
            _show_clone_bot_menu(chat_id)
            return

        elif callback_data == 'clone_feat_translate' and is_admin(user_id):
            answer_callback(callback_query['id'])
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

_TTV_MALE_VOICES = {
    "km": "km-KH-PisethNeural",
    "en": "en-US-AndrewMultilingualNeural",
    "zh-CN": "zh-CN-YunyangNeural",
    "zh-TW": "zh-TW-YunJheNeural",
    "th": "th-TH-NiwatNeural",
    "lo": "lo-LA-ChanthavongNeural",
    "vi": "vi-VN-NamMinhNeural",
    "ko": "ko-KR-HyunsuMultilingualNeural",
    "ja": "ja-JP-KeitaNeural",
    "fr": "fr-FR-RemyMultilingualNeural",
    "de": "de-DE-FlorianMultilingualNeural",
    "ru": "ru-RU-DmitryNeural",
    "ar": "ar-SA-HamedNeural",
    "hi": "hi-IN-MadhurNeural",
    "pt": "pt-BR-AntonioNeural",
    "es": "es-ES-AlvaroNeural",
    "id": "id-ID-ArdiNeural",
    "ms": "ms-MY-OsmanNeural",
    "my": "my-MM-ThihaNeural",
}
_TTV_FEMALE_VOICES = {
    "km": "km-KH-SreymomNeural",
    "en": "en-US-AvaMultilingualNeural",
    "zh-CN": "zh-CN-XiaoxiaoNeural",
    "zh-TW": "zh-TW-HsiaoChenNeural",
    "th": "th-TH-PremwadeeNeural",
    "lo": "lo-LA-KeomanyNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "ko": "ko-KR-SunHiNeural",
    "ja": "ja-JP-NanamiNeural",
    "fr": "fr-FR-VivienneMultilingualNeural",
    "de": "de-DE-SeraphinaMultilingualNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "ar": "ar-SA-ZariyahNeural",
    "hi": "hi-IN-SwaraNeural",
    "pt": "pt-BR-ThalitaMultilingualNeural",
    "es": "es-ES-XimenaNeural",
    "id": "id-ID-GadisNeural",
    "ms": "ms-MY-YasminNeural",
    "my": "my-MM-NilarNeural",
}
_TTV_SCRIPT_MAP = [
    (r'[\u1780-\u17FF]', 'km'),
    (r'[\u0E00-\u0E7F]', 'th'),
    (r'[\u0E80-\u0EFF]', 'lo'),
    (r'[\u1000-\u109F]', 'my'),
    (r'[\u0900-\u097F]', 'hi'),
    (r'[\u0600-\u06FF]', 'ar'),
    (r'[\u0400-\u04FF]', 'ru'),
    (r'[\uAC00-\uD7FF]', 'ko'),
    (r'[\u3040-\u30FF]', 'ja'),
    (r'[\u4E00-\u9FFF\u3400-\u4DBF]', 'zh-CN'),
]
_TTV_SPEED_RATES = {"x0.5": "-50%", "x1": "+0%", "x1.5": "+50%", "x2": "+100%"}
_TTV_SPEED_KEYS  = list(_TTV_SPEED_RATES.keys())

def _ttv_detect_lang(text):
    for pattern, lang in _TTV_SCRIPT_MAP:
        if re.search(pattern, text):
            return lang
    return 'en'

def _ttv_strip_unspeakable(text):
    result = []
    for ch in text:
        cat = _ucd.category(ch)
        if cat.startswith(('L', 'M', 'N', 'P', 'Z')) or ch in '\n\r\t ':
            result.append(ch)
    return ''.join(result)

def _ttv_run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)

def _ttv_synthesize(text, gender='female', speed='x1'):
    """Convert text → OGG Opus bytes via edge_tts + ffmpeg."""
    try:
        import edge_tts
        import imageio_ffmpeg
    except ImportError:
        logger.error("edge_tts / imageio_ffmpeg not installed")
        return None

    rate  = _TTV_SPEED_RATES.get(speed, '+0%')
    lang  = _ttv_detect_lang(text)
    vm    = _TTV_MALE_VOICES if gender == 'male' else _TTV_FEMALE_VOICES
    voice = vm.get(lang) or vm.get('en')
    clean = _ttv_strip_unspeakable(text).strip()
    if not clean:
        return None
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

    async def _run():
        communicate = edge_tts.Communicate(clean, voice, rate=rate, pitch="+5Hz")
        mp3_buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk['type'] == 'audio':
                mp3_buf.write(chunk['data'])
        mp3_data = mp3_buf.getvalue()
        if not mp3_data:
            return None
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, '-y', '-f', 'mp3', '-i', 'pipe:0',
            '-c:a', 'libopus', '-b:a', '64k', '-f', 'ogg', 'pipe:1',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate(input=mp3_data)
        return stdout or None

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
    params = {'chat_id': chat_id}
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
            label = "👩 ស្រី" if gender == 'female' else "👨 ប្រុស"
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                       text=f"✅ ប្តូរទៅ <b>{label}</b>", parse_mode="HTML")
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
            if msg_id:
                _clone_api(base_url, 'deleteMessage', chat_id=chat_id, message_id=msg_id)
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                text=f"✅ <b>ភាសាបកប្រែ៖ {lang_name}</b>\n\n"
                     f"💬 សូមផ្ញើអក្សរ — ខ្ញុំនឹង<i>បកប្រែ</i> + <i>បំប្លែងជាសំឡេង</i> ដោយស្វ័យប្រវត្តិ។",
                parse_mode="HTML")
        elif data == 'cln_tr_off':
            _clone_bot_prefs.setdefault(user_id, {}).pop('translate_lang', None)
            _clone_bot_prefs[user_id].pop('translate_name', None)
            if msg_id:
                _clone_api(base_url, 'deleteMessage', chat_id=chat_id, message_id=msg_id)
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                       text="🔇 <b>បានបិទបកប្រែ</b> — ត្រឡប់ទៅ Text to Voice ធម្មតា។",
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

    if text.startswith('/start'):
        _clone_bot_prefs.setdefault(user_id, {}).pop('translate_lang', None)
        _clone_bot_prefs.setdefault(user_id, {}).pop('translate_name', None)
        bot_cfg      = _get_clone_bot_by_base_url(base_url)
        default_lang = bot_cfg.get('default_lang')
        if default_lang:
            _clone_bot_prefs[user_id]['translate_lang'] = default_lang
            _clone_bot_prefs[user_id]['translate_name'] = bot_cfg.get('default_lang_name', default_lang)
        _clone_api(base_url, 'sendMessage', chat_id=chat_id,
            text='<tg-emoji emoji-id="5798587088077066898">👋</tg-emoji> <b>សួស្តី</b> Sovannrady\n\n'
                 '<b>ខ្ញុំជា Text to voice bot</b>\n\n'
                 '<tg-emoji emoji-id="5471978009449731768">👉</tg-emoji>'
                 '<i>គ្រាន់តែ សរសេរអក្សរណាមួយ ហើយ ខ្ញុំនឹងបំប្លែងជាសំឡេងដោយស្វ័យប្រវត្តិ។</i> '
                 '<tg-emoji emoji-id="5199885118214436622">🔥</tg-emoji>',
            parse_mode="HTML",
            reply_markup={'keyboard': [[{'text': '🌐 បកប្រែភាសា'}]],
                          'resize_keyboard': True, 'is_persistent': True})
        return

    if text == '🌐 បកប្រែភាសា':
        items = list(TRANSLATE_LANGUAGES.items())
        rows  = []
        for i in range(0, len(items), 3):
            rows.append([{'text': name, 'callback_data': f'cln_lang_{code}'}
                         for code, name in items[i:i+3]])
        rows.append([{'text': '🔇 បិទបកប្រែ', 'callback_data': 'cln_tr_off'}])
        _clone_api(base_url, 'sendMessage', chat_id=chat_id,
            text='🌐 <b>ជ្រើសរើសភាសាបកប្រែ</b>',
            parse_mode="HTML",
            reply_markup={'inline_keyboard': rows})
        return

    prefs       = _clone_bot_prefs.get(user_id, {})
    gender      = prefs.get('gender', 'female')
    speed       = prefs.get('speed', 'x1')
    bot_cfg     = _get_clone_bot_by_base_url(base_url)
    trans_lang  = prefs.get('translate_lang') or bot_cfg.get('default_lang')
    trans_name  = (prefs.get('translate_name')
                   or bot_cfg.get('default_lang_name')
                   or trans_lang or '')

    tts_text = text
    if trans_lang:
        _clone_api(base_url, 'sendChatAction', chat_id=chat_id, action='typing')
        translated = _translate_text(text, trans_lang)
        if translated:
            tts_text = translated
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                text=f"🌐 <b>{trans_name}</b>\n<blockquote>{html.escape(translated)}</blockquote>",
                parse_mode="HTML")
        else:
            _clone_api(base_url, 'sendMessage', chat_id=chat_id,
                       text="❌ មានបញ្ហាក្នុងការបកប្រែ សូមព្យាយាមម្តងទៀត។")
            return

    _clone_api(base_url, 'sendChatAction', chat_id=chat_id, action='record_voice')

    try:
        ogg_bytes = _ttv_synthesize(tts_text, gender=gender, speed=speed)
        if ogg_bytes:
            cur_idx    = _TTV_SPEED_KEYS.index(speed) if speed in _TTV_SPEED_KEYS else 1
            next_speed = _TTV_SPEED_KEYS[(cur_idx + 1) % len(_TTV_SPEED_KEYS)]
            kb_row = [
                {'text': '👨 ប្រុស' if gender == 'female' else '👩 ស្រី',
                 'callback_data': f"ttv_gender:{'male' if gender == 'female' else 'female'}"},
                {'text': f"⚡ {next_speed}", 'callback_data': f"ttv_speed:{next_speed}"},
            ]
            if trans_lang:
                kb_row.append({'text': '🌐 ភាសា', 'callback_data': 'cln_tr_menu'})
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

# ── Multi-bot clone management ────────────────────────────────────────────────
def _clone_bot_loop_v2(token, stop_event):
    base_url = f"https://api.telegram.org/bot{token}/"
    offset   = None
    logger.info(f"Clone Bot [{token[:10]}...] started")
    # ── Kick out any stale long-poll from a previous session ──────────────────
    try:
        http.get(f"{base_url}getUpdates",
                 params={'timeout': 0, 'allowed_updates': ['message', 'callback_query']},
                 timeout=10)
    except Exception:
        pass
    # ─────────────────────────────────────────────────────────────────────────
    while not stop_event.is_set():
        try:
            params = {'timeout': 10, 'allowed_updates': ['message', 'callback_query']}
            if offset is not None:
                params['offset'] = offset
            resp = http.get(f"{base_url}getUpdates", params=params, timeout=15)
            data = resp.json()
            if data.get('ok'):
                for upd in data.get('result', []):
                    offset = upd['update_id'] + 1
                    worker_pool.submit(_clone_handle_update, base_url, upd)
            else:
                desc = data.get('description', '')
                logger.warning(f"Clone Bot [{token[:10]}...]: {desc}")
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

def _show_clone_bot_menu(chat_id):
    token_ok   = bool(CLONE_BOT_TOKEN)
    is_running = CLONE_BOT_ACTIVE and _clone_bot_thread and _clone_bot_thread.is_alive()
    token_disp = f"<code>{CLONE_BOT_TOKEN[:12]}...</code>" if token_ok else "❌ មិនទាន់​កំណត់"
    status     = "🟢 ដំណើរការ" if is_running else "🔴 បញ្ឈប់"
    msg = (
        f"🤖 <b>បង្កើតសំឡេង Ai — Text to Voice</b>\n\n"
        f"🔑 Token: {token_disp}\n"
        f"📡 ស្ថានភាព: {status}\n\n"
        f"<i>Clone Bot នឹង​ប្តូរ​អក្សររបស់​អ្នក​ប្រើ​ជា​សំឡេង​ដោយ​ស្វ័យប្រវត្តិ</i>\n"
        f"<i>🌐 គាំទ្រ: ខ្មែរ · English · 中文 · ภาษาไทย · ລາວ · မြန်မာ · 日本語 ...</i>"
    )
    kb = CLONE_BOT_MENU_KEYBOARD_ACTIVE if is_running else CLONE_BOT_MENU_KEYBOARD_INACTIVE
    send_message(chat_id, msg, parse_mode="HTML", reply_to_message_id=False, reply_markup=kb)

def _show_clone_main_menu(chat_id):
    """Show the top-level Clone Bot menu: 2 features."""
    msg = (
        "🤖 <b>Clone Bot — មុខងារ</b>\n\n"
        "  🎙 <b>បង្កើតសំឡេង Ai</b>\n"
        "  🌐 <b>បកប្រែភាសា</b>"
    )
    kb = {'inline_keyboard': [
        [{'text': "🎙 បង្កើតសំឡេង Ai", 'callback_data': 'clone_feat_voice'}],
        [{'text': "🌐 បកប្រែភាសា",     'callback_data': 'clone_feat_translate'}],
    ]}
    send_message(chat_id, msg, parse_mode="HTML", reply_to_message_id=False, reply_markup=kb)

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

            if _state == 'delete_type_select':
                stripped   = text.strip()
                labels     = user_sessions[user_id].get('labels', {}) or {}
                type_name  = labels.get(stripped)
                if type_name and type_name in accounts_data.get('account_types', {}):
                    count = len(accounts_data['account_types'].get(type_name, []))
                    price = accounts_data.get('prices', {}).get(type_name, 0)
                    with _data_lock:
                        user_sessions[user_id] = {'state': 'delete_type_confirm', 'type_name': type_name}
                    save_sessions_async()
                    confirm_kb = {
                        'keyboard': [[{'text': BTN_DELETE_CONFIRM}], [{'text': BTN_DELETE_CANCEL}]],
                        'resize_keyboard': True, 'is_persistent': True,
                    }
                    send_message(chat_id,
                        f"⚠️ <b>តើអ្នកពិតជាចង់លុបប្រភេទ Account នេះមែនទេ?</b>\n\n"
                        f"<blockquote>🔹 ប្រភេទ: {html.escape(type_name)}\n🔹 ចំនួន Account: {count}\n🔹 តម្លៃ: ${price}</blockquote>\n\n"
                        f"Account ទាំងអស់ក្នុងប្រភេទនេះនឹងត្រូវបានលុបចោលជាអចិន្ត្រៃយ៍!",
                        parse_mode="HTML", reply_to_message_id=False, reply_markup=confirm_kb)
                    return

            if _state == 'delete_type_confirm':
                stripped  = text.strip()
                type_name = user_sessions[user_id].get('type_name')
                if stripped == BTN_DELETE_CONFIRM:
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                    if not type_name or type_name not in accounts_data.get('account_types', {}):
                        send_message(chat_id, "⚠️ <b>ប្រភេទនេះមិនមានទៀតហើយ!</b>",
                                     parse_mode="HTML", reply_to_message_id=False,
                                     reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                        return
                    count = len(accounts_data['account_types'].pop(type_name, []))
                    accounts_data.get('prices', {}).pop(type_name, None)
                    accounts_data['accounts'] = [a for a in accounts_data.get('accounts', []) if a.get('type') != type_name]
                    save_data()
                    send_message(chat_id,
                        f"✅ <b>បានលុបប្រភេទ Account <code>{html.escape(type_name)}</code> ចំនួន {count} records ដោយជោគជ័យ!</b>",
                        parse_mode="HTML", reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                    return
                if stripped == BTN_DELETE_CANCEL:
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                    send_message(chat_id, "🚫 <b>បានបោះបង់ការលុបប្រភេទ Account</b>",
                                 parse_mode="HTML", reply_to_message_id=False,
                                 reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                    return

        if is_admin(user_id) and text.strip() in ADMIN_BUTTON_LABELS:
            btn = text.strip()
            if btn == BTN_BACK_SETTINGS:
                if user_id in user_sessions:
                    with _data_lock:
                        del user_sessions[user_id]
                    save_sessions_async()
                send_admin_settings_menu(chat_id)
                return
            if btn == BTN_ADD_ACCOUNT:
                _start_add_account_flow(chat_id, user_id, message_id)
                return
            if btn == BTN_DELETE_TYPE:
                _show_delete_type_menu_inline(chat_id, user_id)
                return
            if btn == BTN_BUYERS:
                _export_buyers_report_inline(chat_id)
                return
            if btn == BTN_PAYMENT:
                _show_payment_inline(chat_id)
                return
            if btn == BTN_BAKONG:
                _show_bakong_inline(chat_id)
                return
            if btn == BTN_CHANNEL:
                _show_channel_inline(chat_id)
                return
            if btn == BTN_MAINTENANCE:
                _show_maintenance_inline(chat_id)
                return
            if btn == BTN_PAYMENT_EDIT:
                _prompt_admin_input(chat_id, user_id, 'payment', "💳 សូមផ្ញើ <b>ឈ្មោះ Payment</b> ថ្មី (1–60 តួអក្សរ)៖")
                return
            if btn == BTN_BAKONG_EDIT:
                _prompt_admin_input(chat_id, user_id, 'bakong', "🔑 សូមផ្ញើ <b>Bakong Token</b> ថ្មី៖")
                return
            if btn == BTN_CHANNEL_EDIT:
                _prompt_admin_input(chat_id, user_id, 'channel', "📢 សូមផ្ញើ <b>Channel ID</b> ថ្មី (លេខ ដូចជា <code>-1001234567890</code>)៖")
                return
            if btn == BTN_CHANNEL_CLEAR:
                CHANNEL_ID = ""
                set_setting('TELEGRAM_CHANNEL_ID', "")
                send_message(chat_id, "✅ បានលុប Channel ID រួចរាល់", parse_mode="HTML",
                             reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                return
            if btn == BTN_ADMIN_ADD:
                _prompt_admin_input(chat_id, user_id, 'admin_add', "➕ សូមផ្ញើ <b>Telegram User ID</b> ដែលចង់បន្ថែមជា Admin៖")
                return
            if btn == BTN_ADMIN_REMOVE:
                _prompt_admin_input(chat_id, user_id, 'admin_remove', "➖ សូមផ្ញើ <b>Telegram User ID</b> ដែលចង់ដក៖")
                return
            if btn == BTN_MAINT_ON:
                MAINTENANCE_MODE = True
                set_setting('MAINTENANCE_MODE', 'true')
                send_message(chat_id, "🔴 បានបិទ Bot", parse_mode="HTML",
                             reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                return
            if btn == BTN_MAINT_OFF:
                MAINTENANCE_MODE = False
                set_setting('MAINTENANCE_MODE', 'false')
                send_message(chat_id, "🟢 បានបើក Bot", parse_mode="HTML",
                             reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                return
            if btn == BTN_CLONE_BOT:
                _show_clone_bot_menu(chat_id)
                return
            if btn == BTN_CLONE_MENU:
                _show_clone_main_menu(chat_id)
                return
            if btn == BTN_TRANSLATE:
                return
            if btn == BTN_CLONE_START:
                if not CLONE_BOT_TOKEN:
                    send_message(chat_id, "❌ សូម​កំណត់ Token ជា​មុន​សិន",
                                 reply_to_message_id=False,
                                 reply_markup=CLONE_BOT_MENU_KEYBOARD_INACTIVE)
                    return
                _start_clone_bot(CLONE_BOT_TOKEN)
                set_setting('CLONE_BOT_ACTIVE', 'true')
                send_message(chat_id, "🟢 <b>Clone Bot ចាប់ផ្តើម​ដំណើរការ!</b>",
                             parse_mode="HTML", reply_to_message_id=False,
                             reply_markup=CLONE_BOT_MENU_KEYBOARD_ACTIVE)
                return
            if btn == BTN_CLONE_STOP:
                _stop_clone_bot()
                set_setting('CLONE_BOT_ACTIVE', 'false')
                send_message(chat_id, "🔴 <b>Clone Bot បាន​បញ្ឈប់</b>",
                             parse_mode="HTML", reply_to_message_id=False,
                             reply_markup=CLONE_BOT_MENU_KEYBOARD_INACTIVE)
                return
            if btn == BTN_CLONE_SET_TOKEN:
                _prompt_admin_input(chat_id, user_id, 'clone_token',
                    "🔑 សូម​ផ្ញើ <b>Bot Token</b> របស់ Clone Bot\n\n"
                    "<i>ទទួលពី @BotFather → /mybots → API Token</i>")
                return
            if btn == BTN_CLONE_TOKEN_CLEAR:
                _stop_clone_bot()
                CLONE_BOT_TOKEN = ""
                set_setting('CLONE_BOT_TOKEN', '')
                set_setting('CLONE_BOT_ACTIVE', 'false')
                send_message(chat_id, "✅ <b>បាន​លុប Token ហើយ​បញ្ឈប់ Clone Bot</b>",
                             parse_mode="HTML", reply_to_message_id=False,
                             reply_markup=CLONE_BOT_MENU_KEYBOARD_INACTIVE)
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

            # Admin broadcast confirm/cancel flow
            elif session.get('state') == 'broadcast_confirm' and is_admin(user_id):
                stripped = text.strip()
                if stripped == BTN_BROADCAST_CONFIRM:
                    source_message_id  = session.get('broadcast_message_id')
                    broadcast_chat_id  = session.get('broadcast_chat_id', chat_id)
                    use_copy           = session.get('broadcast_use_copy', True)
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                    send_message(chat_id, "📢 <b>ចាប់ផ្តើមផ្សាយ...</b>", parse_mode="HTML",
                                 reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
                    _run_background("broadcast", _run_broadcast, broadcast_chat_id, source_message_id, use_copy)
                    return
                if stripped == BTN_BROADCAST_CANCEL:
                    with _data_lock:
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                    save_sessions_async()
                    send_message(chat_id, "🚫 <b>បានបោះបង់ការផ្សាយ</b>", parse_mode="HTML",
                                 reply_to_message_id=False, reply_markup=ADMIN_SETTINGS_REPLY_KEYBOARD)
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
                        send_message(chat_id,
                            "*មិនរកឃើញអ៊ីមែលត្រឹមត្រូវ! សូមបញ្ចូលតាមទម្រង់៖*\n\n"
                            "```\nl1jebywyzos2@10mail.info\nabc123@gmail.com\n```",
                            reply_to_message_id=message_id, parse_mode="Markdown",
                            reply_markup=ADD_ACCOUNT_KEYBOARD)
                        return

                    # Store accounts temporarily and ask for account type
                    with _data_lock:
                        user_sessions[user_id] = {
                            'state': 'waiting_for_account_type',
                            'pending_accounts': accounts
                        }
                    save_sessions_async()
                    send_message(chat_id,
                        f"*✅ បានទទួល {len(accounts)} Accounts*\n\nសូមបញ្ចូលប្រភេទ Account (ឧ. Facebook, TikTok):",
                        reply_to_message_id=message_id, parse_mode="Markdown",
                        reply_markup=ADD_ACCOUNT_KEYBOARD)
                    return

                if session['state'] == 'waiting_for_account_type':
                    account_type = text.strip()
                    if not account_type or account_type == BTN_BACK_SETTINGS:
                        if account_type == BTN_BACK_SETTINGS:
                            with _data_lock:
                                if user_id in user_sessions:
                                    del user_sessions[user_id]
                            save_sessions_async()
                            send_admin_settings_menu(chat_id)
                        else:
                            send_message(chat_id, "សូមបញ្ចូលប្រភេទ Account ជាអក្សរ",
                                         reply_to_message_id=message_id)
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
                    price_hint = f"\n\n(តម្លៃបច្ចុប្បន្ន: ${existing_price})" if existing_price else ""
                    send_message(chat_id,
                        f"*ប្រភេទ: {account_type}*\n\nសូមបញ្ចូលតម្លៃ (USD) ក្នុងមួយ Account:{price_hint}",
                        reply_to_message_id=message_id, parse_mode="Markdown",
                        reply_markup=ADD_ACCOUNT_KEYBOARD)
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
                            dup_list = '\n'.join(duplicate_emails)
                            if not new_accounts:
                                send_message(chat_id,
                                    f"❌ *មិនអាចបញ្ចូលបាន!*\n\nEmail ទាំងអស់មានស្រាប់ក្នុងប្រព័ន្ធ៖\n```\n{dup_list}\n```",
                                    reply_to_message_id=message_id, parse_mode="Markdown")
                                return
                            else:
                                send_message(chat_id,
                                    f"⚠️ *Email ខាងក្រោមមានស្រាប់ ហើយត្រូវបានរំលង៖*\n```\n{dup_list}\n```",
                                    reply_to_message_id=message_id, parse_mode="Markdown")

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

                        send_message(chat_id,
                            f"*✅ បានបញ្ចូល Account ដោយជោគជ័យ*\n\n"
                            f"```\n🔹 ចំនួន: {count}\n\n🔹 ប្រភេទ: {account_type}\n\n🔹 តម្លៃ: {price}$\n```",
                            reply_to_message_id=message_id, parse_mode="Markdown")
                        send_admin_settings_menu(chat_id)
                        logger.info(f"Admin {user_id} added {count} accounts of type {account_type} with price ${price}")

                    except ValueError:
                        send_message(chat_id, "តម្លៃមិនត្រឹមត្រូវ។ សូមបញ្ចូលតម្លៃជាលេខ (ឧទាហរណ៍: 5.99)",
                                     reply_to_message_id=message_id)
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

    _load_clone_bots()
    for _cb in list(_clone_bots_list):
        if _cb.get('active') and _cb.get('token'):
            _start_clone_bot_by_id(_cb['id'])
            logger.info(f"Clone Bot '{_cb['name']}' resumed")

    try:
        _polling_loop()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
