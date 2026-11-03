"""Deploy-package test: boots gst_service.py with $PORT set (like Render),
POSTs both bundled sample orders, checks verified totals.

Run:  python3 test_deploy.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = "8899"
BASE = f"http://127.0.0.1:{PORT}"
CASES = [
    ("order_intra_state.json", "SHP/1001", "11528", "1181.46"),
    ("order_inter_state.json", "SHP/1002", "9889", "1262.31"),
]


def post(order: dict) -> dict:
    req = urllib.request.Request(
        BASE + "/invoice", data=json.dumps(order).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        return json.loads(resp.read())


def main() -> int:
    env = dict(os.environ, PORT=PORT)
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "gst_service.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                    if json.loads(r.read())["status"] == "ok":
                        break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("service did not come up on $PORT")
        failures = 0
        for fname, inv_no, rounded, tax in CASES:
            order = json.loads((HERE / "samples" / fname).read_text())
            payload = post(order)
            t = payload["totals"]
            ok = (payload["invoice_number"] == inv_no
                  and t["grand_total_rounded"] == rounded
                  and t["total_tax"] == tax)
            failures += 0 if ok else 1
            print(f"[{'PASS' if ok else 'FAIL'}] {fname}: "
                  f"invoice={payload['invoice_number']} tax={t['total_tax']} "
                  f"grand={t['grand_total']} rounded={t['grand_total_rounded']}")
        print(f"RESULT: {len(CASES) - failures}/{len(CASES)} cases PASS "
              f"(booted via $PORT={PORT})")
        return 1 if failures else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
