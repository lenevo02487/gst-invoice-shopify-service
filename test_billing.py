"""Phase 3 billing + GDPR test — stdlib only, no real Shopify, no real
secrets, no network beyond 127.0.0.1.

Unit checks (in-process):
  * appSubscriptionCreate payload: plan name, $9.00 price, EVERY_30_DAYS,
    7 trial days; test flag ON by default and OFF only when
    TEST_CHARGE=false.
  * currentAppInstallation status parsing (active / inactive / other
    plan / malformed).
  * webhook HMAC helper: correct vs wrong signature.
  * storage.delete_shop removes a shop from the file backend.
  * GraphQL error surfacing: an HTTP 403 body lands (sanitized — no
    token/shpat_ value) in the raised error; a 200 with a GraphQL
    `errors` array becomes a readable message with its error code;
    /billing/subscribe passes Shopify's reason through in its 502.

End-to-end (service booted with DUMMY env; a local stub stands in for
the Shopify GraphQL endpoint via SHOPIFY_API_BASE):
  * app home shows the billing section — Subscribe offer when no plan,
    "Plan active" when the stub returns an ACTIVE subscription.
  * /billing/subscribe -> 302 to the stub's confirmationUrl, and the
    stub saw the right mutation variables.
  * GET /privacy renders the policy (contact + Neon named).
  * GDPR webhooks: wrong HMAC -> 401; customers/data_request and
    customers/redact with a valid HMAC -> 200; shop/redact deletes the
    shop's saved settings from the storage file.

Run:  python3 test_billing.py
"""
import base64
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SECRET = "dummy_test_secret"  # test-only value, NOT a real secret
KEY = "dummy_test_key"
STUB_PORT = 8921
SVC_PORT = 8891
SHOP_NEW = "billing-shop.myshopify.com"
SHOP_ACTIVE = "active-shop.myshopify.com"
SHOP_REDACT = "redact-shop.myshopify.com"
SHOP_FORBID = "forbid-shop.myshopify.com"
SHOP_GQLERR = "gqlerr-shop.myshopify.com"
CONFIRM_URL = "https://example.test/confirm/abc123"

MUTATION_SEEN: list[dict] = []


# ---------------------------------------------------------- GraphQL stub

class StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        query = body.get("query", "")
        token = self.headers.get("X-Shopify-Access-Token", "")
        if token == "tok-forbid":
            raw = json.dumps(
                {"error": "billing not enabled for this app",
                 "echo": "shpat_deadbeefsecret"}).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        elif token == "tok-gqlerr":
            payload = {"errors": [{
                "message": "Throttled - too many requests",
                "extensions": {"code": "THROTTLED"}}]}
        elif "appSubscriptionCreate" in query:
            MUTATION_SEEN.append(body)
            payload = {"data": {"appSubscriptionCreate": {
                "confirmationUrl": CONFIRM_URL,
                "appSubscription": {"id": "gid://shopify/AppSubscription/1",
                                    "status": "PENDING"},
                "userErrors": []}}}
        elif "currentAppInstallation" in query:
            subs = ([{"id": "gid://shopify/AppSubscription/1",
                      "name": "GST Invoice Pro", "status": "ACTIVE",
                      "trialDays": 7, "test": True}]
                    if token == "tok-active" else [])
            payload = {"data": {"currentAppInstallation": {
                "activeSubscriptions": subs}}}
        else:
            payload = {"data": {}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # quiet
        pass


# ------------------------------------------------------------- helpers

def http_req(port: int, method: str, path: str, body: bytes | None = None,
             headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    out = (resp.status, dict(resp.getheaders()),
           resp.read().decode())
    conn.close()
    return out


def webhook_hmac(raw: bytes, secret: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()


def boot(port: int, extra_env: dict) -> subprocess.Popen:
    env = dict(os.environ, PORT=str(port))
    for name in ("SHOPIFY_API_KEY", "SHOPIFY_API_SECRET", "HOST",
                 "DATABASE_URL", "SHOPIFY_API_BASE", "SHOPIFY_TEST_TOKENS",
                 "TEST_CHARGE"):
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

    # ------------------------------------------------ unit: payload
    sys.path.insert(0, str(HERE))
    os.environ.pop("DATABASE_URL", None)
    import shopify_billing as billing

    payload = billing.build_subscription_payload(
        "https://example-app.test/?shop=" + SHOP_NEW)
    variables = payload.get("variables", {})
    line = (variables.get("lineItems") or [{}])[0]
    pricing = ((line.get("plan") or {}).get("appRecurringPricingDetails")
               or {})
    check("payload: mutation + plan name",
          "appSubscriptionCreate" in payload.get("query", "")
          and variables.get("name") == "GST Invoice Pro")
    check("payload: price/interval/trial",
          pricing.get("price", {}).get("amount") == 9.00
          and pricing.get("price", {}).get("currencyCode") == "USD"
          and pricing.get("interval") == "EVERY_30_DAYS"
          and variables.get("trialDays") == 7,
          f"pricing={pricing}")
    check("payload: test charge ON by default",
          variables.get("test") is True)
    os.environ["TEST_CHARGE"] = "false"
    try:
        check("payload: TEST_CHARGE=false -> real charge flag",
              billing.build_subscription_variables("https://x.test/")
              .get("test") is False)
    finally:
        os.environ.pop("TEST_CHARGE", None)

    # ------------------------------------------------ unit: status parse
    active_body = {"data": {"currentAppInstallation": {
        "activeSubscriptions": [{"id": "1", "name": "GST Invoice Pro",
                                 "status": "ACTIVE", "trialDays": 7,
                                 "test": True}]}}}
    parsed = billing.parse_subscription_status(active_body)
    check("parse: ACTIVE plan -> active", parsed["active"] is True
          and parsed["subscriptions"][0]["name"] == "GST Invoice Pro")
    check("parse: no subscriptions -> inactive",
          billing.parse_subscription_status(
              {"data": {"currentAppInstallation":
                        {"activeSubscriptions": []}}})["active"] is False)
    other = {"data": {"currentAppInstallation": {"activeSubscriptions": [
        {"id": "2", "name": "Some Other Plan", "status": "ACTIVE"}]}}}
    check("parse: other plan ACTIVE -> still inactive for us",
          billing.parse_subscription_status(other)["active"] is False)
    check("parse: malformed body -> inactive, no crash",
          billing.parse_subscription_status({})["active"] is False)

    # ------------------------------------------ unit: error surfacing
    check("errors-format: message + path + code readable",
          billing._format_graphql_errors(
              [{"message": "Access denied",
                "path": ["appSubscriptionCreate"],
                "extensions": {"code": "ACCESS_DENIED"}}])
          == "Access denied (path=appSubscriptionCreate, "
             "code=ACCESS_DENIED)")
    check("sanitize: caller token + shpat_ values masked",
          billing._sanitize("tok shpat_abc123XYZ end", "tok")
          == "*** *** end")

    # ------------------------------------------------ unit: webhook hmac
    import shopify_backend
    raw = b'{"shop_domain":"x.myshopify.com"}'
    good_sig = webhook_hmac(raw, SECRET)
    check("webhook hmac: correct signature accepted",
          shopify_backend.valid_webhook_hmac(raw, good_sig, SECRET))
    check("webhook hmac: wrong signature rejected",
          not shopify_backend.valid_webhook_hmac(raw, "AAAA", SECRET)
          and not shopify_backend.valid_webhook_hmac(
              raw, webhook_hmac(raw, "other-secret"), SECRET)
          and not shopify_backend.valid_webhook_hmac(raw, None, SECRET))

    # ------------------------------------------------ unit: storage delete
    with tempfile.TemporaryDirectory() as td:
        os.environ["DATA_DIR"] = td
        os.environ.pop("NEON_HTTP_BASE", None)
        import storage
        storage.save_shop_settings(SHOP_REDACT, {
            "name": "Redact Traders", "gstin": "18ABCDE1234F1Z5",
            "address": "1 Gone Lane", "state_code": "18",
            "invoice_prefix": "INV"}, next_number=2001)
        storage.save_shop_token(SHOP_REDACT, "dummy-token")
        check("storage: shop saved before delete",
              storage.get_shop(SHOP_REDACT) is not None)
        storage.delete_shop(SHOP_REDACT)
        check("storage: delete_shop erases settings + token",
              storage.get_shop(SHOP_REDACT) is None
              and storage.get_shop_token(SHOP_REDACT) is None)
        os.environ.pop("DATA_DIR", None)

    # ------------------------------------------------ end-to-end
    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), StubHandler)
    import threading
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    data_dir = tempfile.mkdtemp(prefix="gst-billing-test-")
    tokens = json.dumps({SHOP_NEW: "tok-none", SHOP_ACTIVE: "tok-active",
                         SHOP_FORBID: "tok-forbid",
                         SHOP_GQLERR: "tok-gqlerr"})
    proc = boot(SVC_PORT, {
        "SHOPIFY_API_KEY": KEY, "SHOPIFY_API_SECRET": SECRET,
        "HOST": "https://example-app.test",
        "DATA_DIR": data_dir,
        "SHOPIFY_API_BASE": f"http://127.0.0.1:{STUB_PORT}",
        "SHOPIFY_TEST_TOKENS": tokens})
    try:
        status, _, body = http_req(SVC_PORT, "GET", f"/?shop={SHOP_NEW}")
        check("home: billing section + Subscribe offer",
              status == 200 and "Billing" in body
              and "Subscribe" in body and "GST Invoice Pro" in body
              and "Plan active" not in body, f"status={status}")
        status, _, body = http_req(SVC_PORT, "GET", f"/?shop={SHOP_ACTIVE}")
        check("home: ACTIVE plan shows 'Plan active'",
              status == 200 and "Plan active" in body, f"status={status}")

        status, headers, _ = http_req(
            SVC_PORT, "GET", f"/billing/subscribe?shop={SHOP_NEW}")
        check("subscribe: 302 to Shopify confirmationUrl",
              status == 302 and headers.get("Location") == CONFIRM_URL,
              f"status={status} loc={headers.get('Location')}")
        sent = (MUTATION_SEEN[-1].get("variables", {})
                if MUTATION_SEEN else {})
        sent_line = (sent.get("lineItems") or [{}])[0]
        sent_pricing = ((sent_line.get("plan") or {})
                        .get("appRecurringPricingDetails") or {})
        check("subscribe: mutation carried plan constants",
              sent.get("name") == "GST Invoice Pro"
              and sent_pricing.get("price", {}).get("amount") == 9.00
              and sent_pricing.get("interval") == "EVERY_30_DAYS"
              and sent.get("trialDays") == 7
              and sent.get("test") is True
              and SHOP_NEW in sent.get("returnUrl", ""),
              f"vars={json.dumps(sent)[:160]}")
        status, _, _ = http_req(SVC_PORT, "GET",
                                "/billing/subscribe?shop=evil.example.com")
        check("subscribe: invalid shop -> 400", status == 400,
              f"status={status}")

        # Error-body surfacing, direct against the stub: a 403 must
        # carry Shopify's reason (sanitized) and a 200 with a GraphQL
        # errors array must surface a readable message.
        os.environ["SHOPIFY_API_BASE"] = f"http://127.0.0.1:{STUB_PORT}"
        err403 = ""
        try:
            billing.graphql_request(
                SHOP_FORBID, "tok-forbid", billing.CREATE_MUTATION,
                billing.build_subscription_variables(
                    "https://example-app.test/"))
        except billing.BillingError as exc:
            err403 = str(exc)
        check("graphql 403: Shopify body in error, token masked",
              "403" in err403 and "billing not enabled" in err403
              and "shpat_deadbeefsecret" not in err403
              and "***" in err403, f"err={err403[:120]}")
        err_gql = ""
        try:
            billing.graphql_request(SHOP_GQLERR, "tok-gqlerr",
                                    billing.STATUS_QUERY)
        except billing.BillingError as exc:
            err_gql = str(exc)
        check("graphql errors array: readable message + code",
              "Throttled" in err_gql and "THROTTLED" in err_gql,
              f"err={err_gql[:120]}")
        os.environ.pop("SHOPIFY_API_BASE", None)

        status, _, body = http_req(
            SVC_PORT, "GET", f"/billing/subscribe?shop={SHOP_FORBID}")
        check("subscribe 403: 502 carries Shopify's reason, no secret",
              status == 502 and "billing not enabled" in body
              and "shpat_deadbeefsecret" not in body,
              f"status={status}")

        status, _, body = http_req(SVC_PORT, "GET", "/privacy")
        check("privacy page: policy + contact + Neon",
              status == 200 and "Privacy Policy" in body
              and "lenevo02487@gmail.com" in body and "Neon" in body
              and "do not store customer" in body.lower(),
              f"status={status}")

        # Seed seller settings for the redact shop THROUGH the service.
        form = ("name=Redact+Traders&gstin=18ABCDE1234F1Z5"
                "&address=1+Gone+Lane&state_code=18"
                "&invoice_prefix=INV&start_number=2001").encode()
        status, _, _ = http_req(
            SVC_PORT, "POST", f"/settings?shop={SHOP_REDACT}", body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        store_file = Path(data_dir) / "gst_invoice_shop_settings.json"
        saved = (json.loads(store_file.read_text())
                 if store_file.exists() else {})
        check("redact setup: settings persisted via service",
              status == 200 and SHOP_REDACT in saved, f"status={status}")

        redact_body = json.dumps(
            {"shop_domain": SHOP_REDACT}).encode()
        status, _, _ = http_req(
            SVC_PORT, "POST", "/webhooks/shop/redact", body=redact_body,
            headers={"Content-Type": "application/json",
                     "X-Shopify-Hmac-Sha256": "bogus"})
        still = json.loads(store_file.read_text())
        check("webhook shop/redact: wrong HMAC -> 401, data kept",
              status == 401 and SHOP_REDACT in still, f"status={status}")

        sig = webhook_hmac(redact_body, SECRET)
        status, _, _ = http_req(
            SVC_PORT, "POST", "/webhooks/shop/redact", body=redact_body,
            headers={"Content-Type": "application/json",
                     "X-Shopify-Hmac-Sha256": sig})
        after = (json.loads(store_file.read_text())
                 if store_file.exists() else {})
        check("webhook shop/redact: valid HMAC -> 200, shop erased",
              status == 200 and SHOP_REDACT not in after,
              f"status={status}")

        for topic in ("data_request", "redact"):
            payload = json.dumps({"shop_domain": SHOP_NEW}).encode()
            status, _, _ = http_req(
                SVC_PORT, "POST", f"/webhooks/customers/{topic}",
                body=payload,
                headers={"Content-Type": "application/json",
                         "X-Shopify-Hmac-Sha256":
                             webhook_hmac(payload, SECRET)})
            check(f"webhook customers/{topic}: valid HMAC -> 200 ack",
                  status == 200, f"status={status}")
        payload = json.dumps({"shop_domain": SHOP_NEW}).encode()
        status, _, _ = http_req(
            SVC_PORT, "POST", "/webhooks/customers/redact", body=payload,
            headers={"Content-Type": "application/json",
                     "X-Shopify-Hmac-Sha256": "bogus"})
        check("webhook customers/redact: wrong HMAC -> 401",
              status == 401, f"status={status}")
    finally:
        proc.terminate()
        stub.shutdown()

    passed = sum(results)
    print(f"RESULT: {passed}/{len(results)} billing checks PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
