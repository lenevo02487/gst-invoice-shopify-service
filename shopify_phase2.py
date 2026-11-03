"""Shopify app Phase 2 — seller settings, real orders, order invoices.

Builds on shopify_backend.py (OAuth + token store) and the Phase 0
pipeline (pipeline.order_to_invoice). The GST engine package
(gst_invoice/) is NOT touched — all tax math stays there.

    GET  /settings?shop=   -> settings form (prefilled)
    POST /settings?shop=   -> validate + save seller settings
    GET  /orders?shop=     -> shop's recent orders (Admin API 2024-10),
                              or a "connect" page when no token is held
    POST /order-invoice    -> {shop, order_id}: fetch the order, build a
                              GST invoice with the shop's saved settings;
                              the invoice number comes from the shop's own
                              series (prefix + next number) and is consumed
                              exactly once, only on success

Storage: per-shop settings + invoice-series counter live in one JSON
file under DATA_DIR (env, default /tmp) with an in-memory cache.
WARNING: on Render's free tier the disk is EPHEMERAL — settings and the
series counter are lost on restart/redeploy. A persistent DB is a later
phase; this file store is for the dev-store stage only.

Secrets/tokens: never read from or written to files here. The OAuth
access token comes from shopify_backend.TOKENS (memory only). The
SHOPIFY_TEST_TOKENS env (JSON {"shop": "token"}) exists ONLY so local
tests can inject a dummy token; never set it in production.
SHOPIFY_API_BASE similarly lets tests point the Admin API at a local
stub instead of https://<shop>.
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import shopify_backend
from pipeline import order_to_invoice, totals_sidecar
from gst_invoice.render import render_html
from gst_invoice.tax import compute_invoice

API_VERSION = "2024-10"
# GSTIN: 2-digit state + 5 letters + 4 digits + 1 letter + 1 alnum + Z + 1 alnum
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
STATE_CODE_RE = re.compile(r"^\d{2}$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,11}$")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp"))
STORE_FILE = DATA_DIR / "gst_invoice_shop_settings.json"

_LOCK = threading.Lock()
_CACHE: dict[str, dict] | None = None  # shop -> record (settings + counter)

if os.environ.get("SHOPIFY_TEST_TOKENS"):  # local tests only — dummy token
    try:
        shopify_backend.TOKENS.update(
            json.loads(os.environ["SHOPIFY_TEST_TOKENS"]))
    except ValueError:
        pass


# ---------------------------------------------------------------- store

def _load() -> dict[str, dict]:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(STORE_FILE.read_text())
            if not isinstance(_CACHE, dict):
                _CACHE = {}
        except (OSError, ValueError):
            _CACHE = {}
    return _CACHE


def _persist() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_load(), indent=2))
    tmp.replace(STORE_FILE)


def get_settings(shop: str) -> dict | None:
    rec = _load().get(shop)
    return dict(rec["settings"]) if rec and "settings" in rec else None


def validate_settings(data: dict) -> list[str]:
    errors = []
    if not (data.get("name") or "").strip():
        errors.append("business name is required")
    gstin = (data.get("gstin") or "").strip().upper()
    if not GSTIN_RE.match(gstin):
        errors.append("GSTIN must be a valid 15-character GSTIN "
                      "(e.g. 18ABCDE1234F1Z5)")
    if not STATE_CODE_RE.match((data.get("state_code") or "").strip()):
        errors.append("state code must be 2 digits (e.g. 18)")
    if gstin and STATE_CODE_RE.match((data.get("state_code") or "").strip()):
        if gstin[:2] != data["state_code"].strip():
            errors.append("state code must match the GSTIN's first 2 digits")
    if not PREFIX_RE.match((data.get("invoice_prefix") or "").strip()):
        errors.append("invoice prefix must be 1-12 letters/digits")
    try:
        if int(data.get("start_number")) < 1:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("starting invoice number must be a positive integer")
    return errors


def save_settings(shop: str, data: dict) -> dict:
    """Persist validated settings; the series counter starts at
    start_number (re-saving settings resets the counter to the value in
    the form — the merchant controls their own series)."""
    settings = {
        "name": data["name"].strip(),
        "gstin": data["gstin"].strip().upper(),
        "address": (data.get("address") or "").strip(),
        "state_code": data["state_code"].strip(),
        "invoice_prefix": data["invoice_prefix"].strip(),
    }
    with _LOCK:
        store = _load()
        store[shop] = {"settings": settings,
                       "next_number": int(data["start_number"])}
        _persist()
    return settings


# ------------------------------------------------------------ Shopify API

def _api_url(shop: str, path: str) -> str:
    base = os.environ.get("SHOPIFY_API_BASE", "").rstrip("/")
    return (base + path) if base else (f"https://{shop}" + path)


def api_get(shop: str, path: str) -> dict:
    token = shopify_backend.TOKENS.get(shop)
    if not token:
        raise PermissionError("no access token for shop")
    req = urllib.request.Request(
        _api_url(shop, path),
        headers={"X-Shopify-Access-Token": token,
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def fetch_orders(shop: str) -> list[dict]:
    data = api_get(shop, f"/admin/api/{API_VERSION}/orders.json"
                         "?status=any&limit=10")
    return data.get("orders", [])


def fetch_order(shop: str, order_id: str) -> dict:
    data = api_get(shop, f"/admin/api/{API_VERSION}/orders/{order_id}.json")
    return data["order"]


# ---------------------------------------------------------------- pages

def _page(title: str, body: str) -> bytes:
    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #202223; }}
 input, textarea {{ width: 100%; padding: .5rem; margin: .25rem 0 .8rem;
   border: 1px solid #c9cccf; border-radius: 6px; box-sizing: border-box; }}
 button, .btn {{ background: #008060; color: #fff; border: 0; border-radius: 6px;
   padding: .55rem 1.1rem; font-size: .95rem; cursor: pointer;
   text-decoration: none; display: inline-block; }}
 table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
 th, td {{ border-bottom: 1px solid #dfe3e8; padding: .5rem; text-align: left; }}
 .err {{ color: #b42318; }} .ok {{ color: #067647; }} label {{ font-weight: 600; }}
</style></head><body>{body}</body></html>"""
    return doc.encode()


def settings_page(shop: str, values: dict, message: str = "",
                  errors: list[str] | None = None) -> bytes:
    v = {k: html.escape(str(values.get(k, "") or ""))
         for k in ("name", "gstin", "address", "state_code",
                   "invoice_prefix", "start_number")}
    err_html = "".join(f'<p class="err">{html.escape(e)}</p>'
                       for e in (errors or []))
    msg_html = f'<p class="ok">{html.escape(message)}</p>' if message else ""
    body = f"""
<h1>Seller settings</h1>
<p>Shop: {html.escape(shop)}</p>
{msg_html}{err_html}
<form method="post" action="/settings?shop={html.escape(shop)}">
 <label>Business name <input name="name" value="{v['name']}" required></label>
 <label>GSTIN <input name="gstin" value="{v['gstin']}" maxlength="15" required></label>
 <label>Address <textarea name="address" rows="3">{v['address']}</textarea></label>
 <label>State code (2 digits) <input name="state_code" value="{v['state_code']}" maxlength="2" required></label>
 <label>Invoice prefix <input name="invoice_prefix" value="{v['invoice_prefix']}" required></label>
 <label>Next invoice number <input name="start_number" type="number" min="1" value="{v['start_number'] or 1}" required></label>
 <button type="submit">Save settings</button>
</form>
<p><a href="/?shop={html.escape(shop)}">Back to app</a> ·
   <a href="/orders?shop={html.escape(shop)}">View orders</a></p>"""
    return _page("Seller settings — GST Invoice", body)


def orders_page(shop: str, orders: list[dict]) -> bytes:
    rows = ""
    for o in orders:
        cust = o.get("customer") or {}
        cname = (f"{cust.get('first_name', '')} "
                 f"{cust.get('last_name', '')}").strip() \
            or (o.get("billing_address") or {}).get("name", "")
        rows += f"""<tr>
<td>{html.escape(str(o.get('name') or o.get('order_number', '')))}</td>
<td>{html.escape(str(o.get('created_at', ''))[:10])}</td>
<td>{html.escape(cname)}</td>
<td>{html.escape(str(o.get('total_price', '')))}</td>
<td>{html.escape(str(o.get('financial_status', '') or ''))}</td>
<td><form method="post" action="/order-invoice" style="display:inline">
<input type="hidden" name="shop" value="{html.escape(shop)}">
<input type="hidden" name="order_id" value="{html.escape(str(o.get('id', '')))}">
<button type="submit">GST Invoice</button></form></td></tr>"""
    body = f"""
<h1>Recent orders</h1>
<p>Shop: {html.escape(shop)}</p>
<table><tr><th>Order</th><th>Date</th><th>Customer</th><th>Total</th>
<th>Payment</th><th></th></tr>{rows}</table>
<p><a href="/?shop={html.escape(shop)}">Back to app</a> ·
   <a href="/settings?shop={html.escape(shop)}">Seller settings</a></p>"""
    return _page("Orders — GST Invoice", body)


def connect_page(shop: str) -> bytes:
    body = f"""
<h1>Shop not connected</h1>
<p>No access token is stored for {html.escape(shop)} yet, so orders
cannot be loaded. Connect (or re-connect) the app first:</p>
<p><a class="btn" href="/auth?shop={html.escape(shop)}">Connect / Re-authorize</a></p>
<p><a href="/?shop={html.escape(shop)}">Back to app</a></p>"""
    return _page("Connect shop — GST Invoice", body)


# ---------------------------------------------------------------- routes

def _shop_param(handler, params: dict) -> str | None:
    shop = (params.get("shop") or "").strip().lower()
    return shop if shopify_backend.valid_shop(shop) else None


def route_get(handler, sample_order: dict) -> bool:
    """Handle GET /settings and GET /orders. True if handled."""
    parsed = urlparse(handler.path)
    if parsed.path not in ("/settings", "/orders"):
        return False
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    shop = _shop_param(handler, params)
    if not shop:
        handler._json(400, {"error": "invalid shop domain"})
        return True

    if parsed.path == "/settings":
        saved = get_settings(shop) or {}
        values = dict(saved)
        rec = _load().get(shop) or {}
        values["start_number"] = rec.get("next_number", 1)
        handler._send(200, settings_page(shop, values),
                      "text/html; charset=utf-8")
        return True

    # /orders
    if shop not in shopify_backend.TOKENS:
        handler._send(200, connect_page(shop), "text/html; charset=utf-8")
        return True
    try:
        orders = fetch_orders(shop)
    except Exception as exc:
        handler._send(502, _page("Orders — GST Invoice",
                                 f"<h1>Could not load orders</h1>"
                                 f'<p class="err">{html.escape(str(exc))}</p>'
                                 f'<p><a href="/?shop={shop}">Back</a></p>'),
                      "text/html; charset=utf-8")
        return True
    handler._send(200, orders_page(shop, orders),
                  "text/html; charset=utf-8")
    return True


def route_post(handler, default_seller: dict) -> bool:
    """Handle POST /settings and POST /order-invoice. True if handled."""
    parsed = urlparse(handler.path)
    if parsed.path not in ("/settings", "/order-invoice"):
        return False
    try:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) or b""
        if "application/json" in (handler.headers.get("Content-Type") or ""):
            body = json.loads(raw or b"{}")
        else:
            body = dict(parse_qsl(raw.decode(), keep_blank_values=True))
    except Exception as exc:
        handler._json(400, {"error": f"bad request body: {exc}"})
        return True

    if parsed.path == "/settings":
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        shop = _shop_param(handler, params) or _shop_param(handler, body)
        if not shop:
            handler._json(400, {"error": "invalid shop domain"})
            return True
        data = {
            "name": body.get("name") or body.get("business_name") or "",
            "gstin": body.get("gstin") or "",
            "address": body.get("address") or "",
            "state_code": body.get("state_code") or "",
            "invoice_prefix": body.get("invoice_prefix") or "",
            "start_number": body.get("start_number")
                            or body.get("starting_invoice_number") or "",
        }
        errors = validate_settings(data)
        if errors:
            values = dict(data)
            values["start_number"] = data["start_number"]
            handler._send(422, settings_page(shop, values, errors=errors),
                          "text/html; charset=utf-8")
            return True
        saved = save_settings(shop, data)
        values = dict(saved)
        values["start_number"] = int(data["start_number"])
        handler._send(200, settings_page(
            shop, values, message="Settings saved."), "text/html; charset=utf-8")
        return True

    # /order-invoice
    shop = _shop_param(handler, body)
    if not shop:
        handler._json(400, {"error": "invalid shop domain"})
        return True
    order_id = str(body.get("order_id") or "").strip()
    if not order_id:
        handler._json(400, {"error": "order_id is required"})
        return True
    if shop not in shopify_backend.TOKENS:
        handler._json(401, {"error": "shop not connected — authorize first",
                            "auth_url": f"/auth?shop={shop}"})
        return True
    try:
        order = fetch_order(shop, order_id)
    except Exception as exc:
        handler._json(502, {"error": f"could not fetch order: {exc}"})
        return True

    settings = get_settings(shop) or default_seller
    seller_cfg = {
        "name": settings.get("name", ""),
        "gstin": settings.get("gstin", ""),
        "address": settings.get("address", ""),
        "state_code": str(settings.get("state_code", "")),
        "invoice_prefix": settings.get("invoice_prefix", "SHP"),
    }
    # Series number: preview under lock, consume ONLY after the invoice
    # builds successfully — a failed build never burns a number.
    with _LOCK:
        rec = _load().get(shop) or {}
        seq = int(rec.get("next_number",
                          settings.get("start_number") or 1)) \
            if get_settings(shop) else int(order.get("order_number") or 0)
        order_copy = dict(order)
        order_copy["order_number"] = seq
        try:
            invoice = order_to_invoice(order_copy, seller_cfg)
            totals = compute_invoice(invoice)
        except (ValueError, KeyError, TypeError) as exc:
            handler._json(422, {"error": str(exc)})
            return True
        if get_settings(shop):
            rec = _load().setdefault(shop, {})
            rec["next_number"] = seq + 1
            _persist()
    invoice_html = render_html(invoice, totals)
    if "application/json" in (handler.headers.get("Accept") or ""):
        payload = {"invoice_number": invoice.number,
                   "totals": totals_sidecar(invoice, totals),
                   "html": invoice_html}
        handler._json(200, payload)
    else:
        handler._send(200, invoice_html.encode(),
                      "text/html; charset=utf-8")
    return True
