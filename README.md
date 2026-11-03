# GST Invoice — Shopify Service

Python (stdlib-only) HTTP service that turns a Shopify order JSON into a
print-ready GST invoice (CGST/SGST for intra-state, IGST for inter-state).

- `GET /health` → `{"status": "ok"}`
- `POST /invoice` with a Shopify order JSON → invoice number, full totals
  JSON and rendered HTML invoice.

The GST engine (`gst_invoice/`) is Decimal-exact and covered by its own
tests; this service only wraps it. No dependencies — runs on any Python
3.12 host.

## Deploy (Render free tier)

This repo carries a `render.yaml` Blueprint: web service, free plan,
start command `python3 gst_service.py` (binds `0.0.0.0:$PORT`).

## Local test

```bash
python3 test_deploy.py   # boots on $PORT and verifies both sample orders
```
