"""Test isolation guard.

Several tests DELETE rows (SyncState, Product, InventorySnapshot, ...) to start
from a clean slate. They must therefore NEVER run against a real database.

`app.db._resolve_url()` honours DATABASE_URL ahead of ISHA_DB_PATH, so if a
developer has DATABASE_URL set (e.g. Postgres in .env / the shell), the suite
would wipe and repopulate production-like data. We neutralise that here, before
`app.db` is imported by any test module, by removing DATABASE_URL and pinning a
throwaway SQLite file.
"""
import os
import tempfile

# Drop any inherited Postgres/other URL so the SQLite path is used.
os.environ.pop("DATABASE_URL", None)
# Pin an isolated, disposable SQLite file for the whole suite.
os.environ["ISHA_DB_PATH"] = os.path.join(tempfile.gettempdir(), "isha_test.db")

# Hard guard: confirm the suite actually resolved to SQLite. If anything still
# points at a real database, fail collection rather than risk wiping data.
from app.db import IS_SQLITE  # noqa: E402
assert IS_SQLITE, (
    "Tests must run on SQLite, but the engine resolved to a non-SQLite "
    "database. Refusing to run to avoid destroying real data.")
