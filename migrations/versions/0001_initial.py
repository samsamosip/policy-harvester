"""Create the append-only policy data model."""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    ddl_path = Path(__file__).resolve().parents[2] / "schema.sql"
    ddl = ddl_path.read_text(encoding="utf-8")
    ddl = ddl.replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    # psycopg's simple protocol accepts the PL/pgSQL bodies and the complete DDL batch.
    # Running this through SQLAlchemy's prepared path would reject multiple statements.
    driver_connection = op.get_bind().connection.driver_connection
    driver_connection.execute(ddl, prepare=False)


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS inha_policy CASCADE")
