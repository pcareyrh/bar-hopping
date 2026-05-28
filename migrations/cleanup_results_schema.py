"""Cleanup migration: drop orphaned results-feature schema.

Run AFTER taking a backup of trial_results and dogs tables (see migrations/README.md).
Safe to re-run — every operation checks existence first.

Usage:
    python migrations/cleanup_results_schema.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import inspect, text
from app.database import engine


def _column_exists(conn, table: str, column: str) -> bool:
    try:
        cols = {c["name"] for c in inspect(conn).get_columns(table)}
        return column in cols
    except Exception:
        return False


def _table_exists(conn, table: str) -> bool:
    return inspect(conn).has_table(table)


def _index_exists(conn, index: str, table: str) -> bool:
    return any(
        ix["name"] == index
        for ix in inspect(conn).get_indexes(table)
        if _table_exists(conn, table)
    )


def run() -> None:
    print("Starting cleanup migration for orphaned results-feature schema…")

    with engine.begin() as conn:
        # Drop orphaned index before dropping columns/table.
        if _table_exists(conn, "trials") and _index_exists(conn, "ix_trials_results_status", "trials"):
            conn.execute(text("DROP INDEX ix_trials_results_status"))
            print("  dropped index ix_trials_results_status")
        else:
            print("  skip: index ix_trials_results_status not found")

        # SQLite does not support DROP COLUMN before 3.35.0 and has limited
        # ALTER TABLE support. Columns that only waste a few bytes per row are
        # low-priority; skip with a note if the DB engine cannot drop them.
        dialect = engine.dialect.name
        if dialect == "sqlite":
            print(
                "  note: SQLite detected — skipping column drops "
                "(sessions.last_results_view_at, trials.results_synced_at, "
                "trials.results_status). Recreate the DB from scratch or use "
                "Postgres to reclaim these columns."
            )
        else:
            for table, column in [
                ("sessions", "last_results_view_at"),
                ("trials", "results_synced_at"),
                ("trials", "results_status"),
            ]:
                if _column_exists(conn, table, column):
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
                    print(f"  dropped column {table}.{column}")
                else:
                    print(f"  skip: column {table}.{column} not found")

        # Drop tables (trial_results before dogs due to FK).
        for table in ("trial_results", "dogs"):
            if _table_exists(conn, table):
                conn.execute(text(f"DROP TABLE {table}"))
                print(f"  dropped table {table}")
            else:
                print(f"  skip: table {table} not found")

    print("Cleanup migration complete.")


if __name__ == "__main__":
    run()
