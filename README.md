# GST Invoice — Shopify Service

Python (stdlib-only) HTTP service that turns a Shopify order JSON into a
print-ready GST invoice (CGST/SGST for intra-state, IGST for inter-state).

- `GET /health` → `{"status": "ok"}`
- `POST /invoice` with a Shopify order JSON → invoice number, full totals
  JSON and rendered HTML invoice.

The GST engine (`gst_invoice/`) is Decimal-exact and covered by its own
tests; this service only wraps it. No dependencies — runs on any Python
3.12 host.

## Shopify app backend (v1)

- `GET /` → embedded app UI (works inside the Shopify admin iframe and
  directly in a browser, accepts `?shop=`). Its demo button generates an
  invoice from the bundled sample intra-state order via `POST /invoice`
  and shows it inline.
- `GET /auth?shop=<shop>.myshopify.com` → validates the shop domain and
  redirects to Shopify's OAuth authorize URL (state nonce in a cookie).
- `GET /auth/callback` → validates the callback HMAC and state, exchanges
  the code for an access token (kept in memory only), and redirects back
  into the embedded app in Shopify admin.

Configuration comes only from environment variables (never files):
`SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`, `SCOPES` (default
`read_products,read_orders,read_customers,read_draft_orders`) and `HOST`
(the app's public base URL). Without them the `/auth*` routes answer
"not configured" while `/`, `/invoice` and `/health` keep working.

## Deploy (Render free tier)

This repo carries a `render.yaml` Blueprint: web service, free plan,
start command `python3 gst_service.py` (binds `0.0.0.0:$PORT`).

## Local test

```bash
python3 test_deploy.py   # boots on $PORT and verifies both sample orders
```
