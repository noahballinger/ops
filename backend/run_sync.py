#!/usr/bin/env python3
"""
Background Odoo sync service (skubot pattern).

Runs forever, snapshotting Odoo into the local SQLite cache every
ODOO_SYNC_SECONDS (default 600). The web app reads from that cache, so it
stays fast and works even if Odoo is briefly unreachable. A failed/empty pull
never overwrites the last good snapshot.

Run alongside the web app:
    python run_sync.py
Requires the ODOO_* env vars (see .env.example). With none set it exits with a
clear message rather than spinning.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

# Auto-load a .env from the project root or backend/ (no dotenv dependency).
for _envp in (os.path.join(os.path.dirname(__file__), "..", ".env"),
              os.path.join(os.path.dirname(__file__), ".env")):
    if os.path.exists(_envp):
        for _line in open(_envp):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.split("#")[0].strip())

from app.config import load_config
from app.db import init_db, get_session
from app.datasources.odoo_json import OdooJsonDataSource
from app.sync import run_one_sync, SYNC_SECONDS


def main():
    if not all(os.environ.get(k) for k in
               ("ODOO_BASE_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_PASSWORD")):
        print("ODOO_* env vars not set; nothing to sync. See .env.example.")
        sys.exit(1)
    init_db()
    cfg = load_config()
    print(f"Odoo sync service started — every {SYNC_SECONDS}s. Ctrl-C to stop.")
    while True:
        try:
            ds = OdooJsonDataSource.from_env()
            with get_session() as session:
                st = run_one_sync(session, ds, cfg)
                # read attributes while still bound to the session
                status, products, snapshots = st.status, st.products, st.snapshots
                in_transit, good_batch, last_error = (
                    st.in_transit, st.good_batch_id, st.last_error)
            stamp = time.strftime("%H:%M:%S")
            if status == "ok":
                print(f"[{stamp}] sync ok — {products} products, "
                      f"{snapshots} snapshots, {in_transit} in-transit "
                      f"(batch {good_batch})")
            else:
                print(f"[{stamp}] sync {status}: {last_error} "
                      f"(serving previous cache)")
        except Exception as e:               # never let the loop die
            print(f"sync loop error (continuing): {e}")
        time.sleep(SYNC_SECONDS)


if __name__ == "__main__":
    main()
