"""Bootstrap the dedicated principal-state schema in RDS.

This command is intentionally explicit: it creates tables only and never
reads or imports the legacy global ``data/watchlist.json`` or
``data/positions.json`` files.
"""

from __future__ import annotations

import sys

from app.config import settings
from app.user_state_mysql import MYSQL_SCHEMA_STATEMENTS, MySqlPrincipalStateRepository


def main() -> int:
    backend = str(settings.user_store_backend or "").strip().lower()
    if backend != "mysql":
        print(
            "Refusing user-store bootstrap: WATCH_USER_STORE_BACKEND must be mysql "
            f"(got {backend or 'unset'}).",
            file=sys.stderr,
        )
        return 2

    repository = MySqlPrincipalStateRepository.from_settings(settings)
    try:
        repository.ensure_schema()
    except Exception as error:
        print(f"User-store schema bootstrap failed: {error}", file=sys.stderr)
        return 1

    print(f"Initialized user-store schema at {repository.connection_target}")
    print("Tables:")
    for statement in MYSQL_SCHEMA_STATEMENTS:
        # Statements are code-owned constants; extract the table name only for
        # concise operator output, without exposing credentials.
        marker = "CREATE TABLE IF NOT EXISTS "
        line = next((line.strip() for line in statement.splitlines() if marker in line), "")
        if line:
            print(f"- {line.split(marker, 1)[1].split(' ', 1)[0]}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by deployment shell
    raise SystemExit(main())
