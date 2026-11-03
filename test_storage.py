"""Phase 3 storage test: persistent settings + token + invoice series.

Neon is mocked by a LOCAL stub implementing Neon's SQL-over-HTTPS
(/sql) endpoint against an in-memory table; no network, no real
database, DUMMY token only. "Restart" is simulated honestly — every
persistence check reads through a FRESH python process (for the unit
checks) or a FRESH service boot (for the end-to-end check), never
through a warm in-process cache.

Checks:
  A. File backend (no DATABASE_URL): save in one process -> settings,
     token and series visible in the next; series advances; a second
     commit of the same number conflicts (never issued twice).
  B. Neon backend (stub): the same lifecycle over the HTTP SQL API.
  C. End-to-end with DATABASE_URL set: boot service, save settings,
     invoice INV/500; kill; save the shop token via the storage layer;
     boot again WITHOUT any injected token — /orders works from the
     persisted token and the next invoice is INV/501.

Run:  python3 test_storage.py
"""
import http.client
import json
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
SHOP = "storage-shop.myshopify.com"
TOKEN = "dummy_token"
NEON = 8911
ORDERS = 8912
SETTINGS = {"name": "Storage Traders", "gstin": "18ABCDE1234F1Z5",
            "address": "1 Storage Lane", "state_code": "18",
            "invoice_prefix": "INV"}
SAMPLE = json.loads((HERE / "samples" / "order_intra_state.json")
                    .read_text())
SAMPLE = dict(SAMPLE, financial_status="paid")


# ------------------------------------------------------- Neon stub

class NeonState:
    def __init__(self):
        self.shops = {}  # shop -> {settings, token, next, prefix}


def neon_handle(state: NeonState, query: str, params: list) -> list:
    q = " ".join(query.split()).lower()
    if q.startswith("create table"):
        return []
    if q.startswith("select"):
        rec = state.shops.get(params[0])
        if not rec:
            return []
        return [{"settings": rec.get("settings"),
                 "access_token": rec.get("token"),
                 "next_invoice_no": rec.get("next"),
                 "prefix": rec.get("prefix")}]
    if q.startswith("insert into shops"):
        shop = params[0]
        rec = state.shops.setdefault(
            shop, {"settings": None, "token": None, "next": None,
                   "prefix": None})
        if "access_token" in q and "settings" not in q:
            rec["token"] = params[1]
        else:
            rec["settings"] = json.loads(params[1])
            rec["prefix"] = params[2]
            if "next_invoice_no" in q:
                rec["next"] = params[3]
        return []
    if q.startswith("update shops"):  # series compare-and-swap
        new, shop, expected = params
        rec = state.shops.get(shop)
        if rec and rec.get("next") == expected:
            rec["next"] = new
            return [{"next_invoice_no": new}]
        return []
    raise ValueError(f"stub cannot handle query: {query[:60]}")


def make_neon_handler(state: NeonState):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            if not self.path.startswith("/sql"):
                code, payload = 404, {"error": "not found"}
            else:
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length") or 0)))
                try:
                    rows = neon_handle(state, body.get("query", ""),
                                       body.get("params") or [])
                    code, payload = 200, {"rows": rows,
                                          "rowCount": len(rows)}
                except Exception as exc:
                    code, payload = 400, {"error": str(exc)}
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass
    return Handler


class OrdersHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.headers.get("X-Shopify-Access-Token") != TOKEN:
            body, code = {"error": "unauthorized"}, 401
        elif self.path.startswith("/admin/api/2024-10/orders.json"):
            body, code = {"orders": [SAMPLE]}, 200
        elif self.path.startswith(
                f"/admin/api/2024-10/orders/{SAMPLE['id']}.json"):
            body, code = {"order": SAMPLE}, 200
        else:
            body, code = {"error": "not found"}, 404
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


# ------------------------------------------------------- helpers

def run_storage(code: str, env_extra: dict) -> tuple[int, str]:
    """Run a snippet in a FRESH interpreter with storage importable."""
    env = dict(os.environ)
    for key in ("DATABASE_URL", "NEON_HTTP_BASE", "DATA_DIR"):
        env.pop(key, None)
    env.update(env_extra)
    env["PYTHONPATH"] = str(HERE)
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=30)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


SAVE_SNIPPET = (
    "import storage, json;"
    f"storage.save_shop_settings({SHOP!r}, json.loads({json.dumps(SETTINGS)!r}),"
    " next_number=500);"
    f"storage.save_shop_token({SHOP!r}, {TOKEN!r});"
    "print('saved')")

READ_SNIPPET = (
    "import storage, json;"
    f"rec = storage.get_shop({SHOP!r});"
    "print(json.dumps({'settings': (rec or {}).get('settings'),"
    " 'token': (rec or {}).get('access_token'),"
    " 'next': (rec or {}).get('next_invoice_no')}))")

COMMIT_SNIPPET = (
    "import storage;"
    f"storage.commit_invoice_number({SHOP!r}, 500);"
    "print('committed')")

CONFLICT_SNIPPET = "\n".join([
    "import storage",
    "try:",
    f"    storage.commit_invoice_number({SHOP!r}, 500)",
    "    print('NO-CONFLICT')",
    "except storage.StorageConflict:",
    "    print('CONFLICT')",
])

TOKEN_SNIPPET = (
    "import storage;"
    f"storage.save_shop_token({SHOP!r}, {TOKEN!r});"
    "print('token saved')")


def lifecycle(name: str, env_extra: dict, check) -> None:
    rc, out = run_storage(SAVE_SNIPPET, env_extra)
    check(f"{name}: save in process 1", rc == 0 and "saved" in out,
          out.splitlines()[-1] if out else "")
    rc, out = run_storage(READ_SNIPPET, env_extra)
    try:
        got = json.loads(out.splitlines()[-1]) if rc == 0 else {}
    except ValueError:
        got = {}
    check(f"{name}: restart reads settings+token+series",
          got.get("settings", {}).get("name") == "Storage Traders"
          and got.get("token") == TOKEN and got.get("next") == 500,
          f"got next={got.get('next')}")
    rc, out = run_storage(COMMIT_SNIPPET, env_extra)
    check(f"{name}: commit 500", rc == 0 and "committed" in out)
    rc, out = run_storage(CONFLICT_SNIPPET, env_extra)
    check(f"{name}: re-commit 500 conflicts (no duplicate number)",
          "CONFLICT" in out and "NO-CONFLICT" not in out)
    rc, out = run_storage(READ_SNIPPET, env_extra)
    try:
        got = json.loads(out.splitlines()[-1]) if rc == 0 else {}
    except ValueError:
        got = {}
    check(f"{name}: series continues at 501 after restart",
          got.get("next") == 501, f"got next={got.get('next')}")


def req(port: int, method: str, path: str, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers, data = {}, None
    if body is not None:
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json"}
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    out = (resp.status, resp.read().decode())
    conn.close()
    return out


def boot(port: int, env_extra: dict) -> subprocess.Popen:
    env = dict(os.environ, PORT=str(port))
    for key in ("DATABASE_URL", "NEON_HTTP_BASE", "DATA_DIR",
                "SHOPIFY_TEST_TOKENS", "SHOPIFY_API_BASE"):
        env.pop(key, None)
    env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "gst_service.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    for _ in range(50):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if json.loads(r.read())["status"] == "ok":
                    return proc
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"service did not come up on port {port}")


# ------------------------------------------------------- main

def main() -> int:
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # ---- A. file backend lifecycle
    data_dir = tempfile.mkdtemp(prefix="storage-data-")
    lifecycle("file", {"DATA_DIR": data_dir}, check)
    store_file = Path(data_dir) / "gst_invoice_shop_settings.json"
    on_disk = json.loads(store_file.read_text()) if store_file.exists() \
        else {}
    rec = on_disk.get(SHOP, {})
    check("file: legacy JSON shape on disk (settings + next_number)",
          rec.get("settings", {}).get("invoice_prefix") == "INV"
          and rec.get("next_number") == 501)

    # ---- B. neon backend lifecycle (stub SQL-over-HTTPS)
    state = NeonState()
    neon = ThreadingHTTPServer(("127.0.0.1", NEON),
                               make_neon_handler(state))
    threading.Thread(target=neon.serve_forever, daemon=True).start()
    neon_env = {"DATABASE_URL": "postgres://u:p@neon.invalid/db",
                "NEON_HTTP_BASE": f"http://127.0.0.1:{NEON}"}
    lifecycle("neon", neon_env, check)

    # ---- C. end-to-end: service restart on the neon backend
    orders = ThreadingHTTPServer(("127.0.0.1", ORDERS), OrdersHandler)
    threading.Thread(target=orders.serve_forever, daemon=True).start()
    svc_env = dict(neon_env,
                   SHOPIFY_API_KEY="dummy_test_key",
                   SHOPIFY_API_SECRET="dummy_test_secret",
                   HOST="https://example-app.test",
                   SHOPIFY_API_BASE=f"http://127.0.0.1:{ORDERS}")
    form = {"name": "E2E Traders", "gstin": "18ABCDE1234F1Z5",
            "address": "2 Restart Road", "state_code": "18",
            "invoice_prefix": "INV", "start_number": "500"}

    proc = boot(8913, dict(svc_env, SHOPIFY_TEST_TOKENS=json.dumps(
        {SHOP: TOKEN})))
    try:
        from urllib.parse import urlencode
        conn = http.client.HTTPConnection("127.0.0.1", 8913, timeout=15)
        conn.request("POST", f"/settings?shop={SHOP}",
                     body=urlencode(form).encode(),
                     headers={"Content-Type":
                              "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        status, body = resp.status, resp.read().decode()
        conn.close()
        check("e2e boot1: settings saved", status == 200
              and "Settings saved" in body, f"status={status}")
        status, body = req(8913, "POST", "/order-invoice",
                           {"shop": SHOP, "order_id": SAMPLE["id"]})
        payload = json.loads(body) if status == 200 else {}
        check("e2e boot1: invoice INV/500",
              payload.get("invoice_number") == "INV/500",
              f"status={status} inv={payload.get('invoice_number')}")
    finally:
        proc.terminate()

    rc, out = run_storage(TOKEN_SNIPPET, neon_env)
    check("e2e: token persisted via storage layer",
          rc == 0 and "token saved" in out)

    proc = boot(8914, svc_env)  # NOTE: no SHOPIFY_TEST_TOKENS this time
    try:
        status, body = req(8914, "GET", f"/orders?shop={SHOP}")
        check("e2e boot2 (restart): /orders works from persisted token",
              status == 200 and "#1001" in body, f"status={status}")
        status, body = req(8914, "POST", "/order-invoice",
                           {"shop": SHOP, "order_id": SAMPLE["id"]})
        payload = json.loads(body) if status == 200 else {}
        check("e2e boot2 (restart): series continues at INV/501",
              payload.get("invoice_number") == "INV/501",
              f"status={status} inv={payload.get('invoice_number')}")
    finally:
        proc.terminate()
        neon.shutdown()
        orders.shutdown()

    passed = sum(results)
    print(f"RESULT: {passed}/{len(results)} storage checks PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
