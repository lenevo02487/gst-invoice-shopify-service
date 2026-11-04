"""Shopify Billing — Phase 3, step 2 (stdlib only).

Recurring subscription via the GraphQL Admin API (2024-10):

    plan_status(shop, token)      -> currentAppInstallation
                                     { activeSubscriptions } parsed into
                                     {"active": bool, "subscriptions":
                                      [...], "error": str | None}
    create_subscription(shop, token, return_url)
                                  -> appSubscriptionCreate; returns the
                                     confirmationUrl the merchant must
                                     visit to approve the charge.

Plan constants live at the top of this file on purpose:

    PLAN_PRICE_USD = 9.00 is the SUGGESTED price only — Abhi's final
    pricing decision is still pending and must be confirmed before
    the App Store listing is submitted.

Test charges: while the TEST_CHARGE env is anything other than the
exact string "false" (including unset), subscriptions are created
with test: true, so a dev store NEVER sees a real charge. Set
TEST_CHARGE=false only on the production service at launch.

Tokens are supplied by the caller (shopify_backend.get_token, backed
by the storage layer); token values are never printed or logged here.
SHOPIFY_API_BASE (tests only) overrides https://<shop> so a local stub
can stand in for Shopify, mirroring shopify_phase2.py.
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request

API_VERSION = "2024-10"

PLAN_NAME = "GST Invoice Pro"
# SUGGESTED price — Abhi ka final faisla listing submit se pehle hoga;
# abhi ke liye sirf ye suggested constant use ho raha hai.
PLAN_PRICE_USD = 9.00
PLAN_CURRENCY = "USD"
PLAN_INTERVAL = "EVERY_30_DAYS"
TRIAL_DAYS = 7

STATUS_QUERY = """
{
  currentAppInstallation {
    activeSubscriptions {
      id
      name
      status
      trialDays
      test
    }
  }
}
"""

CREATE_MUTATION = """
mutation AppSubscriptionCreate(
  $name: String!,
  $lineItems: [AppSubscriptionLineItemInput!]!,
  $returnUrl: URL!,
  $trialDays: Int,
  $test: Boolean
) {
  appSubscriptionCreate(
    name: $name
    lineItems: $lineItems
    returnUrl: $returnUrl
    trialDays: $trialDays
    test: $test
  ) {
    confirmationUrl
    appSubscription {
      id
      status
    }
    userErrors {
      field
      message
    }
  }
}
"""


class BillingError(RuntimeError):
    """Shopify billing call failed — message never carries the token."""


def test_charge_enabled() -> bool:
    """True unless TEST_CHARGE is exactly "false" (safe default)."""
    return os.environ.get("TEST_CHARGE", "") != "false"


def graphql_url(shop: str) -> str:
    base = os.environ.get("SHOPIFY_API_BASE", "").rstrip("/")
    root = base if base else f"https://{shop}"
    return f"{root}/admin/api/{API_VERSION}/graphql.json"


def _sanitize(text: str, token: str | None = None) -> str:
    """Mask any access-token value before text enters an error message.
    Shopify error bodies do not normally carry the token, but never
    let one through: the caller's exact token and any shpat_... value
    are replaced with ***."""
    if token:
        text = text.replace(token, "***")
    return re.sub(r"shpat_[A-Za-z0-9_-]+", "***", text)


def _format_graphql_errors(errors: object, token: str | None = None) -> str:
    """Top-level GraphQL `errors` array -> one readable line: each
    error's message, with its path / error code when Shopify sent one."""
    parts = []
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, dict):
                msg = str(err.get("message") or err)
                extras = []
                if err.get("path"):
                    extras.append("path=" + "/".join(
                        str(p) for p in err["path"]))
                code = (err.get("extensions") or {}).get("code")
                if code:
                    extras.append(f"code={code}")
                if extras:
                    msg += " (" + ", ".join(extras) + ")"
                parts.append(msg)
            else:
                parts.append(str(err))
    if not parts:
        parts.append(str(errors))
    return _sanitize("; ".join(parts), token)[:500]


def graphql_request(shop: str, token: str, query: str,
                    variables: dict | None = None) -> dict:
    """POST one GraphQL operation; returns the decoded response body.
    Raises BillingError on HTTP failure — with the response body
    included (sanitized, ~500 chars) so Shopify's actual reason is
    visible — or on a top-level GraphQL `errors` array."""
    payload: dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    req = urllib.request.Request(
        graphql_url(shop), data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "X-Shopify-Access-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        detail = _sanitize(raw.strip(), token)[:500]
        suffix = f": {detail}" if detail else " (empty response body)"
        raise BillingError(f"graphql HTTP {exc.code}{suffix}") from None
    except Exception as exc:
        raise BillingError(f"graphql request failed: {exc}") from None
    if isinstance(body, dict) and body.get("errors"):
        raise BillingError(
            "graphql errors: "
            + _format_graphql_errors(body["errors"], token))
    return body if isinstance(body, dict) else {}


def parse_subscription_status(body: dict) -> dict:
    """GraphQL response body -> {"active", "subscriptions", "error"}.
    Pure function (no I/O) so tests can feed it canned payloads."""
    data = body.get("data") if isinstance(body, dict) else None
    install = (data or {}).get("currentAppInstallation") or {}
    subs = install.get("activeSubscriptions") or []
    clean = []
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        clean.append({
            "id": sub.get("id"),
            "name": sub.get("name"),
            "status": sub.get("status"),
            "trial_days": sub.get("trialDays"),
            "test": bool(sub.get("test")),
        })
    active = any(s.get("status") == "ACTIVE" and
                 s.get("name") == PLAN_NAME for s in clean)
    return {"active": active, "subscriptions": clean, "error": None}


def plan_status(shop: str | None, token: str | None) -> dict:
    """Live plan status for a shop. Never raises: any failure comes
    back as {"active": False, ..., "error": "..."} so the app home
    page always renders."""
    if not shop:
        return {"active": False, "subscriptions": [],
                "error": "no shop"}
    if not token:
        return {"active": False, "subscriptions": [],
                "error": "shop not connected"}
    try:
        body = graphql_request(shop, token, STATUS_QUERY)
        return parse_subscription_status(body)
    except BillingError as exc:
        return {"active": False, "subscriptions": [], "error": str(exc)}


def build_subscription_variables(return_url: str) -> dict:
    """Variables for appSubscriptionCreate from the plan constants."""
    return {
        "name": PLAN_NAME,
        "lineItems": [{
            "plan": {
                "appRecurringPricingDetails": {
                    "price": {"amount": PLAN_PRICE_USD,
                              "currencyCode": PLAN_CURRENCY},
                    "interval": PLAN_INTERVAL,
                }
            }
        }],
        "returnUrl": return_url,
        "trialDays": TRIAL_DAYS,
        "test": test_charge_enabled(),
    }


def build_subscription_payload(return_url: str) -> dict:
    """Full GraphQL request body (query + variables) for the create
    mutation — kept separate so tests can inspect it without I/O."""
    return {"query": CREATE_MUTATION,
            "variables": build_subscription_variables(return_url)}


def create_subscription(shop: str, token: str,
                        return_url: str) -> str:
    """Create the subscription; returns Shopify's confirmationUrl.
    Raises BillingError on userErrors or a missing URL."""
    payload = build_subscription_payload(return_url)
    body = graphql_request(shop, token, payload["query"],
                           payload["variables"])
    result = ((body.get("data") or {}).get("appSubscriptionCreate")
              or {})
    errors = result.get("userErrors") or []
    if errors:
        msgs = "; ".join(str(e.get("message", "?")) for e in errors)
        raise BillingError(f"subscription rejected: {msgs}")
    url = result.get("confirmationUrl")
    if not url:
        raise BillingError("no confirmationUrl in Shopify response")
    return url


def billing_section_html(shop: str, status: dict) -> str:
    """Small billing block for the app home page (shop already
    validated by the caller). Pure rendering of a plan_status dict."""
    shop_q = html.escape(shop, quote=True)
    test_note = (" Test mode is ON — no real charge will be made."
                 if test_charge_enabled() else "")
    if status.get("active"):
        inner = (f"<p><strong>Plan active:</strong> {html.escape(PLAN_NAME)}"
                 f" — ${PLAN_PRICE_USD:.2f} / 30 days"
                 f" ({TRIAL_DAYS}-day free trial on signup).</p>")
    elif status.get("error") == "shop not connected":
        inner = ("<p>This shop is not connected yet — authorize the app "
                 "first, then you can subscribe.</p>")
    else:
        err = status.get("error")
        err_html = (f"<p class=\"status\">Could not check the plan just "
                    f"now: {html.escape(str(err))}</p>") if err else ""
        inner = (f"{err_html}<p>No active subscription. "
                 f"<strong>{html.escape(PLAN_NAME)}</strong> is "
                 f"${PLAN_PRICE_USD:.2f} / 30 days with a "
                 f"{TRIAL_DAYS}-day free trial.</p>"
                 f"<p><a href=\"/billing/subscribe?shop={shop_q}\">"
                 f"<button type=\"button\">Subscribe</button></a></p>")
    return (f"<section><h2>Billing</h2>{inner}"
            f"<p class=\"status\">Billing runs through Shopify — the "
            f"charge appears on your regular Shopify invoice.{test_note}"
            f"</p></section>")
