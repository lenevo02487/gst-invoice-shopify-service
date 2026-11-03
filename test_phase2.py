"""Phase 2 test: seller settings, real-orders flow and order->invoice,
all against a LOCAL stub of the Shopify Admin API with a DUMMY token
(no real secrets, no network).

Boots:
  - stub Shopify API on :8902 serving the bundled intra-state sample
    order at /admin/api/2024-10/orders.json and .../orders/<id>.json
  - gst_service.py on :8901 with SHOPIFY_API_BASE pointed at the stub,
    SHOPIFY_TEST_TOKENS injecting a dummy token, DATA_DIR in a temp dir

Run:  python3 test_phase2.py
"""
import http.client
import json
from decimal import Decimal
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHOP = "phase2-shop.myshopify.com"
OTHER_SHOP = "other-shop.myshopify.com"
THIRD_SHOP = "third-shop.myshopify.com"  # token held, NO saved settings
TOKEN = "dummy_token"
SVC = 8901
STUB = 8902

SAMPLE = json.loads((HERE / "samples" / "order_intra_state.json").read_text())
SAMPLE = dict(SAMPLE, financial_status="paid")
# Draft-order style: no customer and no shipping/billing address at all,
# so no buyer state (province_code) can be read from the order.
NOADDR = {k: v for k, v in SAMPLE.items()
          if k not in ("shipping_address", "billing_address", "customer")}
NOADDR = dict(NOADDR, id=6100001999, order_number=1002)


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.headers.get("X-Shopify-Access-Token") != TOKEN:
            body, code = {"error": "unauthorized"}, 401
        elif self.path.startswith("/admin/api/2024-10/orders.json"):
            body, code = {"orders": [SAMPLE]}, 200
        elif self.path.startswith(
                f"/admin/api/2024-10/orders/{SAMPLE['id']}.json"):
            body, code = {"order": SAMPLE}, 200
        elif self.path.startswith(
                f"/admin/api/2024-10/orders/{NOADDR['id']}.json"):
            body, code = {"order": NOADDR}, 200
        else:
            body, code = {"error": "not found"}, 404
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # quiet
        pass


def req(method: str, path: str, body=None, ctype=None, accept=None):
    conn = http.client.HTTPConnection("127.0.0.1", SVC, timeout=15)
    headers = {}
    data = None
    if body is not None:
        if ctype == "application/json":
            data = json.dumps(body).encode()
        else:
            from urllib.parse import urlencode
            data = urlencode(body).encode()
            ctype = "application/x-www-form-urlencoded"
        headers["Content-Type"] = ctype
    if accept:
        headers["Accept"] = accept
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    out = (resp.status, resp.read().decode())
    conn.close()
    return out


def main() -> int:
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    stub = ThreadingHTTPServer(("127.0.0.1", STUB), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    data_dir = tempfile.mkdtemp(prefix="phase2-data-")
    env = dict(os.environ, PORT=str(SVC),
               SHOPIFY_API_KEY="dummy_test_key",
               SHOPIFY_API_SECRET="dummy_test_secret",
               HOST="https://example-app.test",
               SHOPIFY_API_BASE=f"http://127.0.0.1:{STUB}",
               SHOPIFY_TEST_TOKENS=json.dumps({SHOP: TOKEN,
                                               THIRD_SHOP: TOKEN}),
               DATA_DIR=data_dir)
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "gst_service.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{SVC}/health", timeout=2) as r:
                    if json.loads(r.read())["status"] == "ok":
                        break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("service did not come up")

        good = {"name": "Phase2 Traders", "gstin": "18ABCDE1234F1Z5",
                "address": "MG Road, Guwahati", "state_code": "18",
                "invoice_prefix": "INV", "start_number": "500"}

        status, body = req("POST", f"/settings?shop={SHOP}",
                           dict(good, gstin="18ABCDE1234F1Z"))
        check("settings: short/invalid GSTIN rejected",
              status == 422 and "GSTIN" in body, f"status={status}")

        status, body = req("POST", f"/settings?shop={SHOP}", good)
        check("settings: valid save", status == 200
              and "Settings saved" in body, f"status={status}")

        status, body = req("GET", f"/settings?shop={SHOP}")
        check("settings: GET prefilled", status == 200
              and "Phase2 Traders" in body and "18ABCDE1234F1Z5" in body
              and 'value="500"' in body, f"status={status}")

        status, body = req("GET", f"/orders?shop={OTHER_SHOP}")
        check("orders: no token -> connect page", status == 200
              and "Connect / Re-authorize" in body
              and f"/auth?shop={OTHER_SHOP}" in body, f"status={status}")

        status, body = req("GET", f"/orders?shop={SHOP}")
        check("orders: list from stub API", status == 200
              and "#1001" in body and "Rahul Das" in body
              and "11528.46" in body and "GST Invoice" in body,
              f"status={status}")

        status, body = req("POST", "/order-invoice",
                           {"shop": SHOP, "order_id": SAMPLE["id"]},
                           ctype="application/json", accept="application/json")
        payload = json.loads(body) if status == 200 else {}
        t = payload.get("totals", {})
        check("order-invoice: series number + intra totals",
              status == 200 and payload.get("invoice_number") == "INV/500"
              and t.get("grand_total_rounded") == "11528"
              and t.get("total_tax") == "1181.46"
              and Decimal(t.get("total_igst", "1")) == 0
              and Decimal(t.get("total_cgst", "0")) > 0,
              f"status={status} inv={payload.get('invoice_number')}")

        status, body = req("POST", "/order-invoice",
                           {"shop": SHOP, "order_id": SAMPLE["id"]},
                           ctype="application/json", accept="application/json")
        payload = json.loads(body) if status == 200 else {}
        check("order-invoice: number consumed exactly once",
              status == 200 and payload.get("invoice_number") == "INV/501",
              f"status={status} inv={payload.get('invoice_number')}")

        status, body = req("POST", "/order-invoice",
                           {"shop": OTHER_SHOP, "order_id": SAMPLE["id"]},
                           ctype="application/json")
        check("order-invoice: no token -> 401 with auth link",
              status == 401 and "/auth?shop=" in body, f"status={status}")

        status, body = req("POST", "/order-invoice",
                           {"shop": SHOP, "order_id": NOADDR["id"]},
                           ctype="application/json", accept="application/json")
        payload = json.loads(body) if status == 200 else {}
        t = payload.get("totals", {})
        notes = payload.get("notes", [])
        check("order-invoice: no-address order -> seller-state fallback",
              status == 200 and payload.get("invoice_number") == "INV/502"
              and t.get("place_of_supply_state_code") == "18"
              and t.get("grand_total_rounded") == "11528"
              and Decimal(t.get("total_igst", "1")) == 0
              and Decimal(t.get("total_cgst", "0")) > 0
              and any("seller state used" in n for n in notes)
              and "seller state used" in payload.get("html", ""),
              f"status={status} inv={payload.get('invoice_number')}")

        status, body = req("POST", "/order-invoice",
                           {"shop": THIRD_SHOP, "order_id": NOADDR["id"]},
                           ctype="application/json", accept="application/json")
        payload = json.loads(body) if status == 200 else {}
        t = payload.get("totals", {})
        notes = payload.get("notes", [])
        check("order-invoice: no settings -> demo seller state fallback",
              status == 200 and payload.get("invoice_number") == "SHP/1002"
              and t.get("place_of_supply_state_code") == "18"
              and Decimal(t.get("total_igst", "1")) == 0
              and any("seller state used" in n for n in notes),
              f"status={status} inv={payload.get('invoice_number')}")
    finally:
        proc.terminate()
        stub.shutdown()

    passed = sum(results)
    print(f"RESULT: {passed}/{len(results)} phase2 checks PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
