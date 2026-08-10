"""One place to normalize a Postgres DSN to the driver this package actually uses.

`SHELLBOX_DATABASE_URL` (and Lakebase connection strings, later, in W9's ``lakebase.py``)
arrive as plain ``postgresql://`` URLs. SQLAlchemy resolves that scheme to whatever
``psycopg2`` is importable, but this package depends on ``psycopg`` (3.x). Both
``postgres.py`` and the alembic ``env.py`` need the same rewrite, so it lives here once
rather than twice.
"""

from __future__ import annotations

import os
from urllib.parse import quote

_PLAIN_SCHEME = "postgresql://"
_PSYCOPG3_SCHEME = "postgresql+psycopg://"

# CRITICAL: these defaults serve LOCAL DEVELOPMENT AND CI, and nothing else. They are a
# coherent set describing one machine -- a Postgres on localhost with a throwaway credential --
# and `"shellbox"` is the right database name there. The ``registry`` job in
# `.github/workflows/ci.yml` runs against a `postgres:16-alpine` service and sets
# ``SHELLBOX_PG_DB: shellbox`` explicitly, and `docs/registration.md` documents this table.
#
# They are NOT the deployed default, and `"DB"` here is deliberately NOT the same string as
# `DEFAULT_DATABASE` in `lakebase.py`. That constant is `databricks_postgres`, the database a
# Lakebase project auto-provisions, and it is pinned to the bundle's `pg_database` variable by
# `test_the_default_database_is_the_one_the_bundle_declares` in `tests/unit/test_lakebase.py`.
#
# The two never meet. `dsn_from_env` is reached only when no ``SHELLBOX_PG_RESOURCE`` is set --
# a configured endpoint WINS, which `tests/unit/test_migration_target.py` asserts by which host
# was dialled. So a caller is on exactly one of the two paths, and each path's default names
# the database that path actually reaches. Making these agree would break every local run to
# tidy a value no deployed caller reads.
_COMPONENT_DEFAULTS = {
    "USER": "shellbox",
    "PASSWORD": "shellbox",
    "HOST": "localhost",
    "PORT": "55432",
    "DB": "shellbox",
}


def dsn_from_env() -> str | None:
    """Resolve a DSN from the environment, or ``None`` if none is configured.

    ``SHELLBOX_DATABASE_URL`` wins when set. Otherwise a DSN is assembled from
    ``SHELLBOX_PG_USER`` / ``_PASSWORD`` / ``_HOST`` / ``_PORT`` / ``_DB``, and ``None`` is
    returned when *none* of those are set, so callers can fall back to ``NullRegistry``
    rather than silently connecting somewhere unintended.

    Assembly lives here rather than in a caller because the alternative is writing a
    complete credential-bearing URL into a CI config or a test fixture. Doing it once, from
    parts, keeps that shape out of the repo entirely -- and gives one place to fix the
    escaping, which a hand-built string in YAML would get wrong the first time a password
    contained an ``@``.
    """
    explicit = os.environ.get("SHELLBOX_DATABASE_URL")
    if explicit:
        return explicit

    if not any(os.environ.get(f"SHELLBOX_PG_{k}") for k in _COMPONENT_DEFAULTS):
        return None

    def part(key: str) -> str:
        return os.environ.get(f"SHELLBOX_PG_{key}", _COMPONENT_DEFAULTS[key])

    user = quote(part("USER"), safe="")
    secret = quote(part("PASSWORD"), safe="")
    dsn = f"{_PLAIN_SCHEME}{user}:{secret}@{part('HOST')}:{part('PORT')}/{part('DB')}"

    # `SHELLBOX_PG_SSLMODE` has NO default, deliberately: a local Postgres has no TLS
    # configured, so defaulting to `require` would break every developer and CI run. It
    # exists because Lakebase *demands* TLS, and the alternative for reaching it through
    # this function is putting a complete credential-bearing URL in an env var --
    # precisely the shape this docstring argues against. Assembling it here keeps the
    # OAuth token in its own variable.
    sslmode = os.environ.get("SHELLBOX_PG_SSLMODE")
    if sslmode:
        dsn = f"{dsn}?sslmode={quote(sslmode, safe='')}"
    return dsn


def redact(dsn: str) -> str:
    """A DSN safe for a log line or a pytest skip message.

    Test credentials are throwaway, but skip messages land in CI logs, and a log line is
    exactly where a real credential would eventually be found.
    """
    if "//" not in dsn or "@" not in dsn:
        return dsn
    scheme, _, rest = dsn.partition("//")
    _, _, hostpart = rest.rpartition("@")
    return f"{scheme}//***:***@{hostpart}"


def normalize_postgres_dsn(url: str) -> str:
    """Rewrite a bare ``postgresql://`` DSN to explicitly use the ``psycopg`` (3.x) driver.

    A DSN that already names a driver (``postgresql+psycopg://``,
    ``postgresql+asyncpg://``, ...) is returned unchanged.
    """
    if url.startswith(_PLAIN_SCHEME):
        return _PSYCOPG3_SCHEME + url[len(_PLAIN_SCHEME) :]
    return url
