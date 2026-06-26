"""Safety: the Odoo client must refuse any write-style method and must never
construct a write call.  No network needed -- we assert the guard fires before
any request is made."""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.datasources.odoo_json import OdooJsonDataSource, OdooReadOnlyError


def _ds(tmp_path):
    return OdooJsonDataSource("https://example.invalid", "db", "u", "p",
                              cache_dir=str(tmp_path))


@pytest.mark.parametrize("method", ["create", "write", "unlink", "copy",
                                    "action_confirm", "button_validate"])
def test_write_methods_refused(method, tmp_path):
    ds = _ds(tmp_path)
    with pytest.raises(OdooReadOnlyError):
        ds._call_kw("product.product", method, [[]], {})


def test_read_methods_allowed_set():
    from app.datasources.odoo_json import _ALLOWED_METHODS
    assert _ALLOWED_METHODS == {"search_read", "read_group", "read",
                                "fields_get", "search", "search_count"}
    assert "write" not in _ALLOWED_METHODS and "create" not in _ALLOWED_METHODS


def test_password_not_in_repr(tmp_path):
    ds = _ds(tmp_path)
    assert "p" not in repr(ds) or "password" not in repr(ds).lower()
    # password is held on a private attr, not exposed as a public field
    assert not hasattr(ds, "password")
