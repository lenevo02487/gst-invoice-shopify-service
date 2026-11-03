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
Access tokens are cached in memory and persisted through the storage
layer below (Phase 3), so they survive restarts when a database is
configured.

## Phase 2 — settings, real orders, order invoices

- `GET /settings?shop=` → seller settings form (prefilled). Fields:
  business name, GSTIN (validated: 15 chars — 2-digit state code, 5
  letters, 4 digits, 1 letter, 1 alphanumeric, `Z`, 1 alphanumeric —
  and the state code must match the GSTIN's first 2 digits), address,
  state code, invoice prefix, next invoice number.
- `POST /settings?shop=` → validates and saves the settings.
- `GET /orders?shop=` → the shop's 10 most recent orders from the
  Shopify Admin API (2024-10), using the shop's stored OAuth token.
  With no token it shows a "Connect / Re-authorize" page linking to
  `/auth?shop=`.
- `POST /order-invoice` `{shop, order_id}` → fetches the full order,
  builds the GST invoice with the shop's saved settings (falling back
  to the demo seller when none are saved) and returns the invoice HTML
  (JSON incl. totals + html with `Accept: application/json`). The
  invoice number comes from the shop's own series — prefix + next
  number — and a number is consumed exactly once, only when the
  invoice builds successfully. Note: unlike `POST /invoice`, this
  route does not cross-check Shopify's header totals (real orders can
  include shipping etc. that the line-item engine does not model).

Storage: settings, the series counter and access tokens go through
the storage layer below (Phase 3) — see the next section.

## Phase 3 — persistent storage (storage.py)

One interface (`get_shop` / `save_shop_settings` / `save_shop_token` /
`get_shop_token` / atomic invoice-series peek + commit) with two
backends:

- **Neon Postgres (when `DATABASE_URL` is set):** set `DATABASE_URL`
  to the Neon connection string (from the Neon console / Render env —
  never commit it). Queries run over Neon's SQL-over-HTTPS endpoint
  (`https://<host>/sql`, connection string in the
  `Neon-Connection-String` header, stdlib `urllib` only). A single
  table `shops(shop_domain TEXT PRIMARY KEY, settings JSONB,
  access_token TEXT, next_invoice_no INT, prefix TEXT,
  updated_at TIMESTAMPTZ)` is created on first use. Settings, tokens
  and the invoice series now **survive restarts and redeploys**; the
  series commit is an atomic compare-and-swap, so an invoice number
  can never be issued twice.
- **Local file fallback (no `DATABASE_URL`):** exactly the Phase 2
  behaviour — one JSON file under `DATA_DIR` (env, default `/tmp`).
  **Limitation: on Render's free tier that disk is ephemeral** —
  settings, tokens and the counter are lost on restart/redeploy.
  Use the Neon backend for anything real.

`NEON_HTTP_BASE` is a test-only hook that overrides the SQL endpoint
base URL (local stub); never set it in production.

## Deploy (Render free tier)

This repo carries a `render.yaml` Blueprint: web service, free plan,
start command `python3 gst_service.py` (binds `0.0.0.0:$PORT`).

## Local test

```bash
python3 test_deploy.py   # boots on $PORT and verifies both sample orders
python3 test_backend.py  # OAuth + embedded UI checks (dummy env)
python3 test_phase2.py   # settings/orders/order-invoice vs a local
                         # stub Shopify API (dummy token, no network)
python3 test_storage.py  # persistent storage: file + Neon-stub
                         # lifecycles across fresh processes, plus a
                         # full service-restart check (INV/500 -> 501)
```
