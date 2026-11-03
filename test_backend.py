"""Backend v1 test: boots gst_service.py with DUMMY Shopify env values
(no real secrets), checks the embedded UI, OAuth redirect, HMAC checks
and the not-configured behaviour. Invoice totals are covered separately
by test_deploy.py.

Run:  python3 test_backend.py
"""
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

HERE = Path(__file__).resolve().parent
SECRET = "dummy_test_secret"  # test-only value, NOT a real secret
KEY = "dummy_test_key"


def get(port: int, path: str, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read().decode()
    out = (resp.status, dict(resp.getheaders()), body)
    conn.close()
    return out


def boot(port: int, extra_env: dict) -> subprocess.Popen:
    env = dict(os.environ, PORT=str(port))
    env.pop("SHOPIFY_API_KEY", None)
    env.pop("SHOPIFY_API_SECRET", None)
    env.pop("HOST", None)
    env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "gst_service.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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


def sign(params: dict, secret: str) -> str:
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), message.encode(),
                    hashlib.sha256).hexdigest()


def main() -> int:
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    # --- Instance 1: dummy Shopify env configured ---
    proc = boot(8898, {"SHOPIFY_API_KEY": KEY, "SHOPIFY_API_SECRET": SECRET,
                       "HOST": "https://example-app.test"})
    try:
        status, _, body = get(8898, "/?shop=test-shop.myshopify.com")
        check("GET / embedded UI", status == 200 and "GST Invoice" in body
              and "Generate demo invoice" in body, f"status={status}")

        status, _, _ = get(8898, "/auth?shop=evil.example.com")
        check("GET /auth invalid shop -> 400", status == 400,
              f"status={status}")
        status, _, _ = get(8898, "/auth")
        check("GET /auth missing shop -> 400", status == 400,
              f"status={status}")

        status, headers, _ = get(8898, "/auth?shop=test-shop.myshopify.com")
        loc = headers.get("Location", "")
        q = dict(parse_qsl(urlparse(loc).query))
        cookie = headers.get("Set-Cookie", "")
        check("GET /auth valid shop -> 302 to Shopify OAuth",
              status == 302
              and loc.startswith("https://test-shop.myshopify.com"
                                 "/admin/oauth/authorize?")
              and q.get("client_id") == KEY
              and q.get("redirect_uri") == "https://example-app.test"
                                            "/auth/callback"
              and "read_orders" in q.get("scope", "")
              and q.get("state", "") in cookie,
              f"status={status}")
        state = q.get("state", "")

        status, _, body = get(
            8898, "/auth/callback?shop=test-shop.myshopify.com&code=x"
                  "&state=" + state + "&hmac=deadbeef",
            headers={"Cookie": f"shopify_oauth_state={state}"})
        check("callback wrong HMAC -> 401", status == 401,
              f"status={status}")

        params = {"shop": "test-shop.myshopify.com", "code": "testcode",
                  "state": state, "timestamp": "1700000000"}
        good = dict(params, hmac=sign(params, SECRET))
        query = "&".join(f"{k}={v}" for k, v in good.items())
        status, _, body = get(
            8898, f"/auth/callback?{query}",
            headers={"Cookie": f"shopify_oauth_state={state}"})
        # HMAC + state accepted => it proceeds to the token exchange,
        # which cannot succeed against a fake shop: 502 (or 302 if a
        # future stub answers). Anything except 401 proves validation.
        check("callback valid HMAC passes validation",
              status in (302, 502) and "invalid" not in body,
              f"status={status}")
    finally:
        proc.terminate()

    # --- Instance 2: no Shopify env -> /auth* 'not configured', / fine ---
    proc = boot(8897, {})
    try:
        status, _, body = get(8897, "/auth?shop=test-shop.myshopify.com")
        check("no env: /auth -> 503 not configured",
              status == 503 and "not configured" in body,
              f"status={status}")
        status, _, body = get(8897, "/")
        check("no env: / still 200", status == 200
              and "GST Invoice" in body, f"status={status}")
    finally:
        proc.terminate()

    passed = sum(results)
    print(f"RESULT: {passed}/{len(results)} backend checks PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
