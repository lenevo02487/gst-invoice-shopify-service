"""Shopify app backend v1 — embedded UI + OAuth, stdlib only.

Added on top of gst_service.py (GST engine untouched). Responsibilities:

    GET /              -> embedded app UI (works in Shopify admin iframe
                          and directly in a browser; accepts ?shop=...)
    GET /auth          -> validate shop, redirect to Shopify OAuth
    GET /auth/callback -> validate HMAC + state, exchange code for an
                          access token, redirect back into the app

Secrets come ONLY from the environment — never from files:
    SHOPIFY_API_KEY, SHOPIFY_API_SECRET
    SCOPES  (default: read_products,read_orders,read_customers,read_draft_orders)
    HOST    (public app base URL, e.g. https://...onrender.com; if it is
             not a URL the caller's request Host header is used instead)

If the Shopify env is missing, /auth* answer a clear "not configured"
while /, /invoice and /health keep working. Access tokens live in a
plain in-memory dict — never written to disk.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets as _secrets
import urllib.request
from urllib.parse import parse_qsl, quote, urlencode, urlparse

DEFAULT_SCOPES = "read_products,read_orders,read_customers,read_draft_orders"
SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")

# shop domain -> access token (memory only, lost on restart — fine for v1)
TOKENS: dict[str, str] = {}


def config() -> dict:
    return {
        "api_key": os.environ.get("SHOPIFY_API_KEY", ""),
        "api_secret": os.environ.get("SHOPIFY_API_SECRET", ""),
        "scopes": os.environ.get("SCOPES", DEFAULT_SCOPES),
        "host": os.environ.get("HOST", ""),
    }


def app_base(cfg: dict, request_host: str | None) -> str:
    """Public base URL of this app (no trailing slash)."""
    host = (cfg.get("host") or "").rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    if host:  # bare hostname given
        return "https://" + host
    return "https://" + (request_host or "localhost")


def valid_shop(shop: str | None) -> bool:
    return bool(shop) and bool(SHOP_RE.match(shop.strip().lower()))


def _hmac_message(pairs: list[tuple[str, str]]) -> str:
    """Shopify callback HMAC message: drop hmac/signature, sort, join.

    Per Shopify docs, in keys escape % -> %25, = -> %3D, & -> %26 and in
    values escape % -> %25, & -> %26, then join 'key=value' with '&'.
    """
    parts = []
    for key, value in pairs:
        if key in ("hmac", "signature"):
            continue
        k = key.replace("%", "%25").replace("=", "%3D").replace("&", "%26")
        v = value.replace("%", "%25").replace("&", "%26")
        parts.append(f"{k}={v}")
    return "&".join(sorted(parts))


def valid_hmac(pairs: list[tuple[str, str]], secret: str) -> bool:
    given = dict(pairs).get("hmac", "")
    if not given or not secret:
        return False
    digest = hmac.new(secret.encode(), _hmac_message(pairs).encode(),
                      hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, given)


def exchange_code(shop: str, code: str, cfg: dict) -> str:
    """POST the OAuth code to Shopify and return the access token."""
    body = json.dumps({
        "client_id": cfg["api_key"],
        "client_secret": cfg["api_secret"],
        "code": code,
    }).encode()
    req = urllib.request.Request(
        f"https://{shop}/admin/oauth/access_token", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["access_token"]


def app_ui_html(shop: str, sample_order: dict, configured: bool) -> str:
    shop_txt = html.escape(shop or "")
    status = ("Shopify OAuth configured." if configured
              else "Shopify OAuth not configured (demo mode — invoice "
                   "demo still works).")
    sample_js = json.dumps(sample_order)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GST Invoice</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #202223; }}
 button {{ background: #008060; color: #fff; border: 0; border-radius: 6px;
          padding: .6rem 1.2rem; font-size: 1rem; cursor: pointer; }}
 iframe {{ width: 100%; height: 720px; border: 1px solid #dfe3e8;
          border-radius: 8px; margin-top: 1rem; background: #fff; }}
 .status {{ color: #616a75; }}
</style></head><body>
<h1>GST Invoice</h1>
<p class="status">Status: service live. {html.escape(status)}</p>
{f'<p class="status">Shop: {shop_txt}</p>' if shop_txt else ''}
<p>Try it: generate an invoice from the bundled sample intra-state order.</p>
<button id="demo" type="button">Generate demo invoice</button>
<iframe id="out" title="Invoice preview"></iframe>
<script>
const SAMPLE_ORDER = {sample_js};
document.getElementById('demo').addEventListener('click', async () => {{
  const resp = await fetch('/invoice', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(SAMPLE_ORDER)
  }});
  const data = await resp.json();
  document.getElementById('out').srcdoc = data.html || ('Error: ' + (data.error || resp.status));
}});
</script>
</body></html>"""


def _cookie_value(cookie_header: str | None, name: str) -> str:
    for part in (cookie_header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k == name:
                return v
    return ""


def route_get(handler, sample_order: dict) -> bool:
    """Handle /, /auth, /auth/callback. Returns True if handled."""
    parsed = urlparse(handler.path)
    path = parsed.path
    if path not in ("/", "/auth", "/auth/callback"):
        return False
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    params = dict(pairs)
    cfg = config()

    if path == "/":
        page = app_ui_html(params.get("shop", ""), sample_order,
                           configured=bool(cfg["api_key"] and cfg["api_secret"]))
        handler._send(200, page.encode(), "text/html; charset=utf-8")
        return True

    if path == "/auth":
        shop = (params.get("shop") or "").strip().lower()
        if not valid_shop(shop):
            handler._json(400, {"error": "invalid shop domain"})
            return True
        if not cfg["api_key"] or not cfg["api_secret"]:
            handler._json(503, {"error": "shopify oauth not configured"})
            return True
        nonce = _secrets.token_urlsafe(24)
        base = app_base(cfg, handler.headers.get("Host"))
        query = urlencode({
            "client_id": cfg["api_key"],
            "scope": cfg["scopes"],
            "redirect_uri": f"{base}/auth/callback",
            "state": nonce,
        })
        handler.send_response(302)
        handler.send_header("Location",
                            f"https://{shop}/admin/oauth/authorize?{query}")
        handler.send_header(
            "Set-Cookie",
            f"shopify_oauth_state={nonce}; Path=/; HttpOnly; SameSite=Lax")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True

    # /auth/callback
    if not cfg["api_key"] or not cfg["api_secret"]:
        handler._json(503, {"error": "shopify oauth not configured"})
        return True
    shop = (params.get("shop") or "").strip().lower()
    if not valid_shop(shop):
        handler._json(400, {"error": "invalid shop domain"})
        return True
    if not valid_hmac(pairs, cfg["api_secret"]):
        handler._json(401, {"error": "invalid hmac"})
        return True
    state_cookie = _cookie_value(handler.headers.get("Cookie"),
                                 "shopify_oauth_state")
    if not params.get("state") or params.get("state") != state_cookie:
        handler._json(401, {"error": "invalid state"})
        return True
    code = params.get("code", "")
    if not code:
        handler._json(400, {"error": "missing code"})
        return True
    try:
        TOKENS[shop] = exchange_code(shop, code, cfg)
    except Exception as exc:  # Shopify unreachable / bad code etc.
        handler._json(502, {"error": f"token exchange failed: {exc}"})
        return True
    handle = shop.split(".")[0]
    target = (f"https://admin.shopify.com/store/{handle}/apps/{cfg['api_key']}"
              if cfg["api_key"] else f"/?shop={quote(shop)}")
    handler.send_response(302)
    handler.send_header("Location", target)
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return True
