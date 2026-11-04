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
while /, /invoice and /health keep working. Access tokens are kept in
an in-memory cache on top of the persistent storage layer
(storage.py): with DATABASE_URL set they survive restarts/redeploys
in Neon; tokens are never printed or logged.
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

import shopify_billing
import storage

DEFAULT_SCOPES = "read_products,read_orders,read_customers,read_draft_orders"
SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")

# shop domain -> access token (in-memory cache; the persistent copy
# lives in the storage layer — see get_token/set_token below)
TOKENS: dict[str, str] = {}


def get_token(shop: str) -> str | None:
    """Token for a shop: memory cache first, then persistent storage
    (so a token earned before a restart/redeploy is still usable)."""
    if TOKENS.get(shop):
        return TOKENS[shop]
    try:
        token = storage.get_shop_token(shop)
    except Exception:
        token = None
    if token:
        TOKENS[shop] = token
    return token


def has_token(shop: str) -> bool:
    return get_token(shop) is not None


def set_token(shop: str, token: str) -> None:
    """Cache a fresh token and persist it. A storage failure must not
    break the OAuth flow — the in-memory copy still serves this run."""
    TOKENS[shop] = token
    try:
        storage.save_shop_token(shop, token)
    except Exception:
        pass


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


def app_ui_html(shop: str, sample_order: dict, configured: bool,
                billing_html: str = "") -> str:
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
{f'<p><a href="/settings?shop={shop_txt}">Seller settings</a> · <a href="/orders?shop={shop_txt}">Recent orders</a></p>' if shop_txt else ''}
{billing_html}
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
    """Handle /, /auth, /auth/callback, /privacy, /billing/subscribe.
    Returns True if handled."""
    parsed = urlparse(handler.path)
    path = parsed.path
    if path not in ("/", "/auth", "/auth/callback", "/privacy",
                    "/billing/subscribe"):
        return False
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    params = dict(pairs)
    cfg = config()

    if path == "/privacy":
        handler._send(200, privacy_html().encode(),
                      "text/html; charset=utf-8")
        return True

    if path == "/":
        shop = (params.get("shop") or "").strip().lower()
        billing_html = ""
        if valid_shop(shop):
            status = shopify_billing.plan_status(
                shop, get_token(shop) if cfg["api_key"] else None)
            billing_html = shopify_billing.billing_section_html(
                shop, status)
        page = app_ui_html(shop, sample_order,
                           configured=bool(cfg["api_key"]
                                           and cfg["api_secret"]),
                           billing_html=billing_html)
        handler._send(200, page.encode(), "text/html; charset=utf-8")
        return True

    if path == "/billing/subscribe":
        shop = (params.get("shop") or "").strip().lower()
        if not valid_shop(shop):
            handler._json(400, {"error": "invalid shop domain"})
            return True
        if not cfg["api_key"] or not cfg["api_secret"]:
            handler._json(503, {"error": "shopify oauth not configured"})
            return True
        token = get_token(shop)
        if not token:
            handler._json(401, {"error": "shop not connected — "
                                         "authorize first"})
            return True
        base = app_base(cfg, handler.headers.get("Host"))
        return_url = f"{base}/?shop={quote(shop)}"
        try:
            confirm_url = shopify_billing.create_subscription(
                shop, token, return_url)
        except Exception as exc:  # BillingError or transport failure
            handler._json(502, {"error": f"billing failed: {exc}"})
            return True
        handler.send_response(302)
        handler.send_header("Location", confirm_url)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
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
        set_token(shop, exchange_code(shop, code, cfg))
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


# ------------------------------------------------------------ /privacy

def privacy_html() -> str:
    """Plain, truthful privacy policy for the app (GET /privacy)."""
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy — GST Invoice</title>
<style>
 body { font-family: system-ui, sans-serif; margin: 2rem auto;
        max-width: 44rem; padding: 0 1rem; color: #202223;
        line-height: 1.55; }
</style></head><body>
<h1>Privacy Policy — GST Invoice</h1>
<p>GST Invoice is a Shopify app that generates GST-compliant tax
invoices for Indian sellers. This page explains, in plain language,
what data the app handles and where it goes.</p>

<h2>What we collect</h2>
<ul>
<li><strong>Shop domain</strong> of the store that installs the app.</li>
<li><strong>Seller settings</strong> the merchant enters in the app:
business name, GSTIN, business address, state code and the invoice
number series (prefix and next number).</li>
<li><strong>Order data</strong> fetched from Shopify at the moment an
invoice is generated (items, amounts, taxes, and the buyer's
name/address as shown on the invoice). Order data is used
on-the-fly to build that invoice.</li>
<li><strong>The app access token</strong> Shopify issues when the
merchant authorizes the app, so the app keeps working after restarts.</li>
</ul>

<h2>What we do NOT collect</h2>
<p>We do not store customer personal data. Customer names and
addresses appear only inside the invoice generated for that order;
they are not saved in our database. We do not sell data, share it
with advertisers, or use it for any purpose other than running the
app for the merchant's own store.</p>

<h2>Where data is stored</h2>
<p>Shop domain, seller settings, invoice-series position and the app
access token are stored in a Postgres database hosted by Neon
(transmitted over encrypted HTTPS connections). Access tokens are
kept server-side only and are never shown in the app interface.</p>

<h2>Deletion</h2>
<p>When a merchant uninstalls the app, Shopify sends us a mandatory
shop-redact request and we permanently delete that shop's stored
settings, invoice series and access token. Merchants and customers
may also contact us at any time to ask what is stored or to request
deletion.</p>

<h2>Contact</h2>
<p>Questions about this policy or a deletion request:
<a href="mailto:lenevo02487@gmail.com">lenevo02487@gmail.com</a></p>
</body></html>"""


# ------------------------------------------------- GDPR webhooks (POST)

WEBHOOK_PATHS = ("/webhooks/customers/data_request",
                 "/webhooks/customers/redact",
                 "/webhooks/shop/redact")


def valid_webhook_hmac(raw_body: bytes, header_value: str | None,
                       secret: str) -> bool:
    """Shopify webhook check: X-Shopify-Hmac-Sha256 carries the
    base64-encoded HMAC-SHA256 of the raw request body."""
    import base64
    if not header_value or not secret:
        return False
    digest = hmac.new(secret.encode(), raw_body,
                      hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, header_value.strip())


def route_post(handler) -> bool:
    """Handle the three mandatory GDPR webhooks. Returns True if the
    path matched (the response — 200/401/503 — has been sent)."""
    parsed = urlparse(handler.path)
    if parsed.path not in WEBHOOK_PATHS:
        return False
    cfg = config()
    if not cfg["api_secret"]:
        handler._json(503, {"error": "shopify oauth not configured"})
        return True
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    raw = handler.rfile.read(length) if length > 0 else b""
    if not valid_webhook_hmac(
            raw, handler.headers.get("X-Shopify-Hmac-Sha256"),
            cfg["api_secret"]):
        handler._json(401, {"error": "invalid webhook hmac"})
        return True
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        payload = {}
    shop = (payload.get("shop_domain") or "").strip().lower()

    if parsed.path == "/webhooks/shop/redact":
        # Erase everything we hold for this shop — settings, invoice
        # series and the access token — from memory AND storage.
        if shop:
            TOKENS.pop(shop, None)
            try:
                storage.delete_shop(shop)
            except Exception:
                handler._json(500, {"error": "shop data delete failed"})
                return True
        handler._json(200, {"ok": True, "redacted": shop or None})
        return True

    # customers/data_request and customers/redact: this app stores NO
    # customer personal data (order data is read on-the-fly only to
    # render an invoice), so there is nothing to export or erase.
    # Acknowledge with 200, as Shopify requires.
    handler._json(200, {"ok": True, "stored_customer_data": None})
    return True
