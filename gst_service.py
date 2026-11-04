"""GST Invoice HTTP service — Phase 1 MVP skeleton.

Wraps the Phase 0 prototype pipeline in a tiny HTTP API using ONLY the
Python standard library (http.server) — no framework, no install.

The GST math is NOT reimplemented here: the prototype package
(../../prototype/gst_invoice/ + pipeline.py) is imported and reused
as-is; its core logic is untouched.

Endpoints:
    GET  /health   -> {"status": "ok"}
    POST /invoice  -> Shopify order JSON in, invoice out.
    Phase 2 (shopify_phase2.py): GET/POST /settings, GET /orders,
    POST /order-invoice — seller settings, real orders, order invoices.

Request body for POST /invoice — either form:
    1. A bare Shopify order object (seller config = server default), or
    2. {"order": {...}, "seller": {...}} to override the seller config.

Response:
    - Default (or Accept: application/json): JSON with invoice_number,
      totals (same shape as the prototype .totals.json sidecar) and the
      rendered HTML in the "html" field.
    - Accept: text/html: the print-ready HTML invoice directly.
    - Shopify header cross-checks (order.total_tax / total_price vs the
      engine) run exactly like the prototype CLI; a mismatch is a 422.

Run:
    python3 gst_service.py [--port 8787] [--seller path/to/seller.json]
Default seller config: prototype/samples/seller.json (demo seller).
Set GST_SELLER_JSON to override.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Deploy package is self-contained: the Phase 0 engine (gst_invoice/)
# and adapter (pipeline.py) ship in this same directory, byte-identical
# to the prototype — core GST tax logic untouched.
PACKAGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_DIR))

from pipeline import order_to_invoice, totals_sidecar  # noqa: E402
from gst_invoice.render import render_html  # noqa: E402
from gst_invoice.tax import compute_invoice  # noqa: E402
from shopify_backend import route_get as shopify_route_get  # noqa: E402
from shopify_backend import route_post as shopify_route_post  # noqa: E402
from shopify_phase2 import route_get as phase2_route_get  # noqa: E402
from shopify_phase2 import route_post as phase2_route_post  # noqa: E402

DEFAULT_SELLER = PACKAGE_DIR / "samples" / "seller.json"
SAMPLE_ORDER = PACKAGE_DIR / "samples" / "order_intra_state.json"


def build_invoice_payload(order: dict, seller_cfg: dict) -> tuple[dict, str]:
    """order -> (json payload, html). Raises ValueError on cross-check fail."""
    invoice = order_to_invoice(order, seller_cfg)
    totals = compute_invoice(invoice)

    for label, field in (("total_tax", totals.total_tax),
                         ("total_price", totals.grand_total)):
        if order.get(field) is not None:
            expected = Decimal(str(order[field]))
            if expected != (totals.total_tax if label == "total_tax"
                            else totals.grand_total):
                raise ValueError(
                    f"cross-check failed: order.{field}={expected} "
                    f"engine={totals.total_tax if label == 'total_tax' else totals.grand_total}")

    payload = {
        "invoice_number": invoice.number,
        "totals": totals_sidecar(invoice, totals),
    }
    return payload, render_html(invoice, totals)


def make_handler(default_seller: dict, sample_order: dict | None = None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj).encode(),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            from urllib.parse import urlparse
            if urlparse(self.path).path == "/health":
                self._json(200, {"status": "ok"})
            elif shopify_route_get(self, sample_order or {}):
                pass  # /, /auth, /auth/callback handled by shopify_backend
            elif phase2_route_get(self, sample_order or {}):
                pass  # /settings, /orders handled by shopify_phase2
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            from urllib.parse import urlparse
            if shopify_route_post(self):
                return  # GDPR webhooks handled by shopify_backend
            if phase2_route_post(self, default_seller):
                return  # /settings, /order-invoice handled by shopify_phase2
            if urlparse(self.path).path != "/invoice":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if "order" in body:
                    order, seller = body["order"], body.get("seller") or default_seller
                else:
                    order, seller = body, default_seller
                payload, html = build_invoice_payload(order, seller)
            except (ValueError, KeyError, TypeError) as exc:
                self._json(422, {"error": str(exc)})
                return
            except Exception as exc:  # malformed JSON etc.
                self._json(400, {"error": str(exc)})
                return
            if "text/html" in (self.headers.get("Accept") or ""):
                self._send(200, html.encode(), "text/html; charset=utf-8")
            else:
                payload["html"] = html
                self._json(200, payload)

        def log_message(self, fmt, *args):  # keep stdout clean
            sys.stderr.write("[gst-service] %s\n" % (fmt % args))

    return Handler


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    # Hosting platforms (Render etc.) inject $PORT and need 0.0.0.0.
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8787")))
    # NOTE: $HOST doubles as the Shopify app's public base URL (a full
    # https:// URL) — never bind to it. Bind host: --host / GST_BIND_HOST.
    ap.add_argument("--host", default=os.environ.get("GST_BIND_HOST",
                                                     "0.0.0.0"))
    ap.add_argument("--seller", default=os.environ.get(
        "GST_SELLER_JSON", str(DEFAULT_SELLER)))
    args = ap.parse_args(argv[1:])

    seller_cfg = json.loads(Path(args.seller).read_text())
    sample_order = json.loads(SAMPLE_ORDER.read_text())
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(seller_cfg, sample_order))
    print(f"[gst-service] listening on http://{args.host}:{args.port} "
          f"(seller={args.seller})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
