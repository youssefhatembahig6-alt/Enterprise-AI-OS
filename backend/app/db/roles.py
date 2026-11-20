"""Database role names used by migrations (validated identifiers)."""

from __future__ import annotations

import os
import re

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def app_role_name() -> str:
    name = os.environ.get("EAIOS_APP_DB_ROLE", "eaios_app")
    if not _IDENTIFIER.match(name):
        raise ValueError("EAIOS_APP_DB_ROLE must be a lowercase SQL identifier")
    return name
