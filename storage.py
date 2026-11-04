"""Persistent per-shop storage — Phase 3, step 1.

One interface, two backends:

    get_shop(shop)            -> record dict or None
    save_shop_settings(shop, settings, next_number=None)
    save_shop_token(shop, token)
    get_shop_token(shop)      -> token string or None
    save_shop_auth(shop, access_token, refresh_token=None,
                   token_expires_at=None)
    get_shop_auth(shop)       -> {"access_token", "refresh_token",
                                  "token_expires_at"} or None
    peek_invoice_number(shop) -> next series number or None
    commit_invoice_number(shop, expected)
        Atomically advance the series from `expected` to `expected + 1`.
        Raises StorageConflict if the stored number is no longer
        `expected` (another invoice consumed it first), so a number is
        never handed out twice.

Record shape (both backends):
    {"shop": str, "settings": dict | None,
     "access_token": str | None, "next_invoice_no": int | None,
     "prefix": str | None,
     "refresh_token": str | None, "token_expires_at": int | None}

The last two carry Shopify's EXPIRING offline-token pair: the access
token's absolute expiry (epoch seconds) and the refresh token used to
renew it. Records saved before expiring tokens existed simply have
both as None and keep working (non-expiring legacy token).

Backend (a) — Neon Postgres over HTTP: used when the DATABASE_URL env
holds a Neon connection string. Queries go to Neon's SQL-over-HTTPS
endpoint (https://<host>/sql) with the connection string carried in
the 'Neon-Connection-String' header, stdlib urllib only. One table:

    shops(shop_domain TEXT PRIMARY KEY, settings JSONB,
          access_token TEXT, next_invoice_no INT, prefix TEXT,
          refresh_token TEXT, token_expires_at BIGINT,
          updated_at TIMESTAMPTZ)

created lazily (CREATE TABLE IF NOT EXISTS) on first use; the two
token columns are added with ALTER TABLE ... ADD COLUMN IF NOT EXISTS
so a table created by an earlier version gains them in place.

Backend (b) — local JSON file fallback: used when DATABASE_URL is not
set. Same file and format as the Phase 2 store (DATA_DIR env, default
/tmp, file gst_invoice_shop_settings.json:
{shop: {"settings": {...}, "next_number": int}}) plus an
"access_token" key per record so tokens persist the same way. This
keeps local/dev behaviour identical to Phase 2.

Secrets discipline: the connection string and access tokens are read
from the environment / passed in by callers and are only ever sent to
their intended destination (Neon header / shops table). They are
never printed or logged here. NEON_HTTP_BASE (tests only) overrides
the SQL endpoint base URL so tests can point at a local stub; never
set it in production.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS shops (
    shop_domain TEXT PRIMARY KEY,
    settings JSONB,
    access_token TEXT,
    next_invoice_no INT,
    prefix TEXT,
    refresh_token TEXT,
    token_expires_at BIGINT,
    updated_at TIMESTAMPTZ DEFAULT now()
)
"""

# Columns added after the first release of this table; applied
# best-effort on first use so existing databases gain them in place.
ADD_COLUMN_SQL = (
    "ALTER TABLE shops ADD COLUMN IF NOT EXISTS refresh_token TEXT",
    "ALTER TABLE shops ADD COLUMN IF NOT EXISTS token_expires_at BIGINT",
)


class StorageError(RuntimeError):
    """Backend failure (network/HTTP/SQL) — message never carries
    credentials or tokens."""


class StorageConflict(StorageError):
    """Invoice-series compare-and-swap lost a race."""


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_dict(value):
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


# ------------------------------------------------------------- Neon HTTP

class NeonStore:
    """Neon Postgres via the SQL-over-HTTPS endpoint."""

    def __init__(self, database_url: str):
        self._conn = database_url
        host = urlparse(database_url).netloc.split("@")[-1]
        base = os.environ.get("NEON_HTTP_BASE", "").rstrip("/")
        self._endpoint = (base + "/sql") if base else f"https://{host}/sql"
        self._table_ready = False
        self._lock = threading.Lock()

    # -- low level
    def _query(self, sql: str, params: list | None = None) -> list[dict]:
        body = json.dumps({"query": sql, "params": params or []}).encode()
        req = urllib.request.Request(
            self._endpoint, data=body,
            headers={"Content-Type": "application/json",
                     "Neon-Connection-String": self._conn})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read() or b"{}")
        except Exception as exc:
            raise StorageError(f"neon query failed: {exc}") from None
        if isinstance(payload, dict) and payload.get("error"):
            # Neon's error text describes the SQL problem only.
            raise StorageError(
                f"neon error: {str(payload['error'])[:200]}")
        rows = payload.get("rows") if isinstance(payload, dict) else None
        return rows if isinstance(rows, list) else []

    def _ensure_table(self) -> None:
        if self._table_ready:
            return
        with self._lock:
            if not self._table_ready:
                self._query(CREATE_TABLE_SQL)
                for ddl in ADD_COLUMN_SQL:
                    try:
                        self._query(ddl)
                    except StorageError:
                        pass  # column already there / old server
                self._table_ready = True

    # -- interface
    def get_shop(self, shop: str) -> dict | None:
        self._ensure_table()
        rows = self._query(
            "SELECT settings, access_token, refresh_token, "
            "token_expires_at, next_invoice_no, prefix "
            "FROM shops WHERE shop_domain = $1", [shop])
        if not rows:
            return None
        row = rows[0]
        settings = _as_dict(row.get("settings"))
        prefix = row.get("prefix") or (settings or {}).get(
            "invoice_prefix")
        return {"shop": shop, "settings": settings,
                "access_token": row.get("access_token"),
                "refresh_token": row.get("refresh_token"),
                "token_expires_at": _int_or_none(
                    row.get("token_expires_at")),
                "next_invoice_no": _int_or_none(row.get("next_invoice_no")),
                "prefix": prefix}

    def save_shop_settings(self, shop: str, settings: dict,
                           next_number: int | None = None) -> None:
        self._ensure_table()
        prefix = settings.get("invoice_prefix")
        if next_number is None:  # keep any existing series position
            self._query(
                "INSERT INTO shops (shop_domain, settings, prefix) "
                "VALUES ($1, $2::jsonb, $3) "
                "ON CONFLICT (shop_domain) DO UPDATE SET "
                "settings = EXCLUDED.settings, prefix = EXCLUDED.prefix, "
                "updated_at = now()",
                [shop, json.dumps(settings), prefix])
        else:
            self._query(
                "INSERT INTO shops (shop_domain, settings, prefix, "
                "next_invoice_no) VALUES ($1, $2::jsonb, $3, $4) "
                "ON CONFLICT (shop_domain) DO UPDATE SET "
                "settings = EXCLUDED.settings, prefix = EXCLUDED.prefix, "
                "next_invoice_no = EXCLUDED.next_invoice_no, "
                "updated_at = now()",
                [shop, json.dumps(settings), prefix, int(next_number)])

    def save_shop_token(self, shop: str, token: str) -> None:
        self._ensure_table()
        self._query(
            "INSERT INTO shops (shop_domain, access_token) "
            "VALUES ($1, $2) "
            "ON CONFLICT (shop_domain) DO UPDATE SET "
            "access_token = EXCLUDED.access_token, updated_at = now()",
            [shop, token])

    def get_shop_token(self, shop: str) -> str | None:
        rec = self.get_shop(shop)
        return rec.get("access_token") if rec else None

    def save_shop_auth(self, shop: str, access_token: str,
                       refresh_token: str | None = None,
                       token_expires_at=None) -> None:
        """Store an (expiring) token set. A None refresh_token /
        token_expires_at keeps the previously stored value, so a
        plain access-token save never erases the refresh pair."""
        self._ensure_table()
        self._query(
            "INSERT INTO shops (shop_domain, access_token, "
            "refresh_token, token_expires_at) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (shop_domain) DO UPDATE SET "
            "access_token = EXCLUDED.access_token, "
            "refresh_token = COALESCE(EXCLUDED.refresh_token, "
            "shops.refresh_token), "
            "token_expires_at = COALESCE(EXCLUDED.token_expires_at, "
            "shops.token_expires_at), updated_at = now()",
            [shop, access_token, refresh_token,
             _int_or_none(token_expires_at)])

    def get_shop_auth(self, shop: str) -> dict | None:
        rec = self.get_shop(shop)
        if not rec or not rec.get("access_token"):
            return None
        return {"access_token": rec["access_token"],
                "refresh_token": rec.get("refresh_token"),
                "token_expires_at": rec.get("token_expires_at")}

    def peek_invoice_number(self, shop: str) -> int | None:
        rec = self.get_shop(shop)
        return rec.get("next_invoice_no") if rec else None

    def commit_invoice_number(self, shop: str, expected: int) -> None:
        self._ensure_table()
        rows = self._query(
            "UPDATE shops SET next_invoice_no = $1, updated_at = now() "
            "WHERE shop_domain = $2 AND next_invoice_no = $3 "
            "RETURNING next_invoice_no",
            [int(expected) + 1, shop, int(expected)])
        if not rows:
            raise StorageConflict(
                f"invoice number {expected} already consumed for {shop}")

    def delete_shop(self, shop: str) -> None:
        """Erase every stored trace of a shop (GDPR shop/redact)."""
        self._ensure_table()
        self._query("DELETE FROM shops WHERE shop_domain = $1", [shop])


# ------------------------------------------------------------ file store

class FileStore:
    """Phase 2-compatible local JSON file store (fallback)."""

    def __init__(self, data_dir: str | None = None):
        self._dir = Path(data_dir or os.environ.get("DATA_DIR", "/tmp"))
        self._file = self._dir / "gst_invoice_shop_settings.json"
        self._lock = threading.Lock()
        self._cache: dict | None = None

    def _load(self) -> dict:
        if self._cache is None:
            try:
                data = json.loads(self._file.read_text())
                self._cache = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def _persist(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._load(), indent=2))
        tmp.replace(self._file)

    def get_shop(self, shop: str) -> dict | None:
        rec = self._load().get(shop)
        if not isinstance(rec, dict):
            return None
        settings = rec.get("settings")
        return {"shop": shop,
                "settings": dict(settings) if isinstance(
                    settings, dict) else None,
                "access_token": rec.get("access_token"),
                "refresh_token": rec.get("refresh_token"),
                "token_expires_at": _int_or_none(
                    rec.get("token_expires_at")),
                "next_invoice_no": _int_or_none(rec.get("next_number")),
                "prefix": (settings or {}).get("invoice_prefix")
                if isinstance(settings, dict) else None}

    def save_shop_settings(self, shop: str, settings: dict,
                           next_number: int | None = None) -> None:
        with self._lock:
            rec = dict(self._load().get(shop) or {})
            rec["settings"] = dict(settings)
            if next_number is not None:
                rec["next_number"] = int(next_number)
            self._load()[shop] = rec
            self._persist()

    def save_shop_token(self, shop: str, token: str) -> None:
        with self._lock:
            rec = dict(self._load().get(shop) or {})
            rec["access_token"] = token
            self._load()[shop] = rec
            self._persist()

    def get_shop_token(self, shop: str) -> str | None:
        rec = self.get_shop(shop)
        return rec.get("access_token") if rec else None

    def save_shop_auth(self, shop: str, access_token: str,
                       refresh_token: str | None = None,
                       token_expires_at=None) -> None:
        """Store an (expiring) token set; None refresh/expiry values
        keep whatever was stored before (see NeonStore.save_shop_auth)."""
        with self._lock:
            rec = dict(self._load().get(shop) or {})
            rec["access_token"] = access_token
            if refresh_token is not None:
                rec["refresh_token"] = refresh_token
            if token_expires_at is not None:
                rec["token_expires_at"] = _int_or_none(token_expires_at)
            self._load()[shop] = rec
            self._persist()

    def get_shop_auth(self, shop: str) -> dict | None:
        rec = self.get_shop(shop)
        if not rec or not rec.get("access_token"):
            return None
        return {"access_token": rec["access_token"],
                "refresh_token": rec.get("refresh_token"),
                "token_expires_at": rec.get("token_expires_at")}

    def peek_invoice_number(self, shop: str) -> int | None:
        rec = self.get_shop(shop)
        return rec.get("next_invoice_no") if rec else None

    def commit_invoice_number(self, shop: str, expected: int) -> None:
        with self._lock:
            rec = self._load().get(shop)
            current = (rec or {}).get("next_number")
            if _int_or_none(current) != int(expected):
                raise StorageConflict(
                    f"invoice number {expected} already consumed "
                    f"for {shop}")
            rec["next_number"] = int(expected) + 1
            self._persist()

    def delete_shop(self, shop: str) -> None:
        """Erase every stored trace of a shop (GDPR shop/redact)."""
        with self._lock:
            if shop in self._load():
                del self._load()[shop]
                self._persist()


# ------------------------------------------------------------- selection

_BACKEND = None
_BACKEND_KEY = None


def backend():
    """The process-wide store: Neon when DATABASE_URL is set, else the
    local file store. Cached per distinct configuration."""
    global _BACKEND, _BACKEND_KEY
    url = os.environ.get("DATABASE_URL", "").strip()
    key = (url, os.environ.get("DATA_DIR", ""),
           os.environ.get("NEON_HTTP_BASE", ""))
    if _BACKEND is None or key != _BACKEND_KEY:
        _BACKEND = NeonStore(url) if url else FileStore()
        _BACKEND_KEY = key
    return _BACKEND


def using_database() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


# Module-level interface (what the app code calls):

def get_shop(shop: str) -> dict | None:
    return backend().get_shop(shop)


def save_shop_settings(shop: str, settings: dict,
                       next_number: int | None = None) -> None:
    backend().save_shop_settings(shop, settings, next_number)


def save_shop_token(shop: str, token: str) -> None:
    backend().save_shop_token(shop, token)


def get_shop_token(shop: str) -> str | None:
    return backend().get_shop_token(shop)


def save_shop_auth(shop: str, access_token: str,
                   refresh_token: str | None = None,
                   token_expires_at=None) -> None:
    backend().save_shop_auth(shop, access_token, refresh_token,
                             token_expires_at)


def get_shop_auth(shop: str) -> dict | None:
    return backend().get_shop_auth(shop)


def peek_invoice_number(shop: str) -> int | None:
    return backend().peek_invoice_number(shop)


def commit_invoice_number(shop: str, expected: int) -> None:
    backend().commit_invoice_number(shop, expected)


def delete_shop(shop: str) -> None:
    backend().delete_shop(shop)
