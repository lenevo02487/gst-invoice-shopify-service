"""Expiring offline token test — stdlib only, local stub, DUMMY values.

Shopify stopped accepting non-expiring access tokens on the Admin
API, so the OAuth exchange now asks for expiring=1 and get_token()
refreshes through the stored refresh token. A local stub stands in
for Shopify's token endpoint, GraphQL endpoint and REST orders.

Checks:
  * exchange_code sends expiring=1 and returns the full record
    (access token + refresh token + absolute expiry from expires_in).
  * set_auth persists all three through the storage layer.
  * get_token with expiry far away makes NO refresh call.
  * get_token with expiry past (or inside the 5-minute buffer)
    refreshes once, stores the rotated pair, and the REST orders
    path (shopify_phase2.api_get) rides on the refreshed token.
  * a failed refresh -> get_token None + 're-authorize needed' error.
  * a legacy saved token (save_shop_token only, no refresh/expiry)
    still works with no refresh attempt (backward compatibility).
  * billing create_subscription succeeds with the refreshed token.
  * end-to-end: a real service boot runs /auth + a signed callback
    against the stub; the expiring trio lands in the storage file.

Run:  python3 test_expiring_tokens.py
"""
import hashlib
import hmac
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
from urllib.parse import parse_qsl, urlparse

HERE = Path(__file__).resolve().parent
SECRET = "dummy_test_secret"  # test-only value, NOT a real secret
KEY = "dummy_test_key"
STUB_PORT = 8925
SVC_PORT = 8926
CONFIRM_URL = "https://example.test/confirm/exp123"

SHOP_A = "exp-a.myshopify.com"
SHOP_B = "exp-b.myshopify.com"
SHOP_C = "exp-c.myshopify.com"
SHOP_D = "exp-d.myshopify.com"
SHOP_E = "exp-e.myshopify.com"
SHOP_F = "exp-f.myshopify.com"

OAUTH_CALLS: list[dict] = []
GQL_TOKENS: list[str] = []
REST_TOKENS: list[str] = []


def refresh_calls() -> list[dict]:
    return [c for c in OAUTH_CALLS
            if c.get("grant_type") == "refresh_token"]


class StubHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/admin/oauth/access_token"):
            OAUTH_CALLS.append(body)
            if body.get("grant_type") == "refresh_token":
                if body.get("refresh_token") in ("ref-stale", "ref-good"):
                    self._send_json(200, {
                        "access_token": "tok-fresh",
                        "refresh_token": "ref-fresh",
                        "expires_in": 3600})
                else:
                    self._send_json(400, {
                        "error": "invalid_request",
                        "error_description": "refresh token invalid"})
                return
            if body.get("code") == "code-long":
                self._send_json(200, {
                    "access_token": "tok-long",
                    "refresh_token": "ref-long",
                    "expires_in": 3600})
            elif body.get("code") == "code-plain":
                self._send_json(200, {"access_token": "tok-plain"})
            else:
                self._send_json(400, {"error": "bad code"})
            return
        if self.path.startswith("/admin/api/2024-10/graphql.json"):
            GQL_TOKENS.append(
                self.headers.get("X-Shopify-Access-Token", ""))
            query = body.get("query", "")
            if "appSubscriptionCreate" in query:
                self._send_json(200, {"data": {"appSubscriptionCreate": {
                    "confirmationUrl": CONFIRM_URL,
                    "appSubscription": {
                        "id": "gid://shopify/AppSubscription/9",
                        "status": "PENDING"},
                    "userErrors": []}}})
            else:
                self._send_json(200, {"data": {
                    "currentAppInstallation": {"activeSubscriptions": []}}})
            return
        self._send_json(404, {"error": "not found"})

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/admin/api/2024-10/orders"):
            REST_TOKENS.append(
                self.headers.get("X-Shopify-Access-Token", ""))
            self._send_json(200, {"orders": []})
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, *args):  # quiet
        pass


def http_get(port: int, path: str, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    out = (resp.status, dict(resp.getheaders()),
           resp.read().decode())
    conn.close()
    return out


def sign(params: dict, secret: str) -> str:
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), message.encode(),
                    hashlib.sha256).hexdigest()


def boot(port: int, extra_env: dict) -> subprocess.Popen:
    env = dict(os.environ, PORT=str(port))
    for name in ("SHOPIFY_API_KEY", "SHOPIFY_API_SECRET", "HOST",
                 "DATABASE_URL", "SHOPIFY_API_BASE", "DATA_DIR",
                 "SHOPIFY_TEST_TOKENS"):
        env.pop(name, None)
    env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "gst_service.py")],
        cwd=str(HERE), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True)
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


def main() -> int:
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    data_dir = tempfile.mkdtemp(prefix="gst-expiring-test-")
    os.environ["DATA_DIR"] = data_dir
    os.environ["SHOPIFY_API_BASE"] = f"http://127.0.0.1:{STUB_PORT}"
    os.environ["SHOPIFY_API_KEY"] = KEY
    os.environ["SHOPIFY_API_SECRET"] = SECRET
    os.environ.pop("DATABASE_URL", None)
    sys.path.insert(0, str(HERE))
    import shopify_backend as backend
    import shopify_billing as billing
    import shopify_phase2 as phase2
    import storage

    cfg = backend.config()

    # ------------------------------------------ exchange asks expiring
    before = time.time()
    record = backend.exchange_code(SHOP_A, "code-long", cfg)
    sent = OAUTH_CALLS[-1] if OAUTH_CALLS else {}
    check("exchange: payload carries expiring=1 + code",
          sent.get("expiring") == "1" and sent.get("code") == "code-long"
          and sent.get("client_id") == KEY, f"sent={sorted(sent)}")
    check("exchange: record has access + refresh + expiry",
          record.get("access_token") == "tok-long"
          and record.get("refresh_token") == "ref-long"
          and record.get("token_expires_at") is not None
          and before + 3500 <= record["token_expires_at"]
          <= time.time() + 3700,
          f"expires_at={record.get('token_expires_at')}")

    # ------------------------------------------ persistence of the trio
    backend.set_auth(SHOP_A, record)
    saved = storage.get_shop_auth(SHOP_A) or {}
    check("storage: auth trio persisted",
          saved.get("access_token") == "tok-long"
          and saved.get("refresh_token") == "ref-long"
          and saved.get("token_expires_at") is not None
          and abs(saved["token_expires_at"]
                  - record["token_expires_at"]) < 2,
          f"saved={ {k: saved.get(k) for k in saved} }")

    # ------------------------------------------ no refresh when valid
    n_refresh = len(refresh_calls())
    check("get_token: expiry far away -> stored token, NO refresh",
          backend.get_token(SHOP_A) == "tok-long"
          and len(refresh_calls()) == n_refresh)

    # ------------------------------------------ refresh when expired
    backend.set_auth(SHOP_B, {"access_token": "tok-stale",
                              "refresh_token": "ref-stale",
                              "token_expires_at": time.time() - 10})
    n_refresh = len(refresh_calls())
    got = backend.get_token(SHOP_B)
    new_calls = refresh_calls()[n_refresh:]
    check("get_token: expiry past -> refresh call -> fresh token",
          got == "tok-fresh" and len(new_calls) == 1
          and new_calls[0].get("refresh_token") == "ref-stale",
          f"got={got} calls={len(new_calls)}")
    saved_b = storage.get_shop_auth(SHOP_B) or {}
    check("refresh: rotated pair stored (new access + refresh + expiry)",
          saved_b.get("access_token") == "tok-fresh"
          and saved_b.get("refresh_token") == "ref-fresh"
          and (saved_b.get("token_expires_at") or 0) > time.time() + 3000)
    check("get_token: refreshed token reused, no second refresh",
          backend.get_token(SHOP_B) == "tok-fresh"
          and len(refresh_calls()) == n_refresh + 1)

    # ------------------------------------------ refresh inside buffer
    backend.set_auth(SHOP_C, {"access_token": "tok-soon",
                              "refresh_token": "ref-good",
                              "token_expires_at": time.time() + 100})
    check("get_token: expiry inside 5-min buffer -> refresh",
          backend.get_token(SHOP_C) == "tok-fresh")

    # ------------------------------------------ refresh failure
    backend.set_auth(SHOP_D, {"access_token": "tok-dead",
                              "refresh_token": "ref-bad",
                              "token_expires_at": time.time() - 10})
    got_d = backend.get_token(SHOP_D)
    err_d = backend.token_error(SHOP_D) or ""
    check("get_token: refresh fails -> None + 're-authorize needed'",
          got_d is None and "re-authorize needed" in err_d,
          f"err={err_d[:80]}")

    # ------------------------------------------ legacy backward-compat
    storage.save_shop_token(SHOP_E, "legacy-tok")
    backend.AUTH.pop(SHOP_E, None)
    backend.TOKENS.pop(SHOP_E, None)
    n_refresh = len(refresh_calls())
    check("legacy: non-expiring saved token works, no refresh attempt",
          backend.get_token(SHOP_E) == "legacy-tok"
          and len(refresh_calls()) == n_refresh)
    plain = backend.exchange_code(SHOP_E, "code-plain", cfg)
    backend.set_auth(SHOP_E, plain)
    check("legacy: exchange without expires_in -> plain record works",
          plain.get("refresh_token") is None
          and plain.get("token_expires_at") is None
          and backend.get_token(SHOP_E) == "tok-plain")

    # ------------------------------------------ REST path rides along
    data = phase2.api_get(SHOP_B, "/admin/api/2024-10/orders.json"
                                  "?status=any&limit=10")
    check("REST orders: api_get uses the refreshed token",
          data == {"orders": []}
          and REST_TOKENS and REST_TOKENS[-1] == "tok-fresh",
          f"rest_token={REST_TOKENS[-1] if REST_TOKENS else None}")

    # ------------------------------------------ billing with fresh token
    token = backend.get_token(SHOP_B)
    url = billing.create_subscription(
        SHOP_B, token, f"https://example-app.test/?shop={SHOP_B}")
    check("billing: subscribe succeeds on the expiring token",
          url == CONFIRM_URL
          and GQL_TOKENS and GQL_TOKENS[-1] == "tok-fresh",
          f"url={url}")

    # ------------------------------------------ e2e OAuth via service
    svc_dir = tempfile.mkdtemp(prefix="gst-expiring-svc-")
    proc = boot(SVC_PORT, {
        "SHOPIFY_API_KEY": KEY, "SHOPIFY_API_SECRET": SECRET,
        "HOST": "https://example-app.test",
        "DATA_DIR": svc_dir,
        "SHOPIFY_API_BASE": f"http://127.0.0.1:{STUB_PORT}"})
    try:
        status, headers, _ = http_get(SVC_PORT, f"/auth?shop={SHOP_F}")
        q = dict(parse_qsl(urlparse(headers.get("Location", "")).query))
        state = q.get("state", "")
        cookie = headers.get("Set-Cookie", "").split(";")[0]
        params = {"shop": SHOP_F, "code": "code-long", "state": state,
                  "timestamp": "1700000000"}
        query = "&".join(f"{k}={v}" for k, v in
                         dict(params, hmac=sign(params, SECRET)).items())
        status, _, _ = http_get(SVC_PORT, f"/auth/callback?{query}",
                                headers={"Cookie": cookie})
        check("e2e callback: signed callback -> 302 (exchange ran)",
              status == 302, f"status={status}")
        store_file = Path(svc_dir) / "gst_invoice_shop_settings.json"
        on_disk = (json.loads(store_file.read_text())
                   if store_file.exists() else {})
        rec_f = on_disk.get(SHOP_F, {})
        check("e2e callback: expiring trio persisted to storage",
              rec_f.get("access_token") == "tok-long"
              and rec_f.get("refresh_token") == "ref-long"
              and isinstance(rec_f.get("token_expires_at"), int),
              f"rec={ {k: rec_f.get(k) for k in rec_f} }")
    finally:
        proc.terminate()
        stub.shutdown()

    passed = sum(results)
    print(f"RESULT: {passed}/{len(results)} expiring-token checks PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
