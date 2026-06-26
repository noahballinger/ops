#!/usr/bin/env python3
"""Quick end-to-end Gmail test. Run on the host (it has network + the token):
    python send_test_email.py you@example.com
Sends a real email via the authorized account and prints the result.
"""
import sys
from app.db import init_db, get_session
from app.mailer import get_provider

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python send_test_email.py recipient@example.com")
    to = sys.argv[1]
    init_db()
    prov = get_provider()
    print("provider:", prov.name)   # expect 'gmail' now that you're authorized
    with get_session() as s:
        res = prov.send(s, to, "Isha Life — email test",
                        "<p>✅ Gmail sending works. This is a test from the "
                        "Ordering Tool.</p>", kind="test")
    print("result:", res)
