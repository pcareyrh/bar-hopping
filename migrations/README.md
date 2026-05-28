# Migrations

Bar Hopping uses a hand-rolled additive migration system in `app/main.py:_migrate()`.
Migrations are idempotent and append-only; columns and tables are never dropped
automatically to avoid surprise data loss on existing databases.

---

## Orphaned schema — results feature removal

The results feature was removed in commit a057d0f (May 2026). The following schema
items are now unused and should be cleaned up once any local data backup is taken.

### Orphaned columns

| Table      | Column               | Type      |
|------------|----------------------|-----------|
| `sessions` | `last_results_view_at` | TIMESTAMP |
| `trials`   | `results_synced_at`  | TIMESTAMP |
| `trials`   | `results_status`     | VARCHAR   |

### Orphaned tables

| Table          | Rows (approx.)        | Notes |
|----------------|-----------------------|-------|
| `dogs`         | small (one per dog)   | Dog identity/normalisation for result matching |
| `trial_results`| potentially large     | Scraped historical run data — back up before drop |

### Orphaned index

| Index                      | Table    |
|----------------------------|----------|
| `ix_trials_results_status` | `trials` |

---

## Retention / backup policy

Before running the cleanup migration:

1. **Back up `trial_results`** — it may contain years of historical run data that
   cannot be re-scraped once dropped. Export to CSV or a separate SQLite file:

   ```sh
   sqlite3 app.db ".mode csv" ".headers on" ".output trial_results_backup.csv" \
     "SELECT * FROM trial_results;" ".quit"
   sqlite3 app.db ".mode csv" ".headers on" ".output dogs_backup.csv" \
     "SELECT * FROM dogs;" ".quit"
   ```

2. Verify backups before proceeding.

3. Run the cleanup migration (see `cleanup_results_schema.py` below).

---

## Cleanup migration

`migrations/cleanup_results_schema.py` — run manually when ready, after backup.

```sh
python migrations/cleanup_results_schema.py
```

The script is idempotent (safe existence checks before every DROP).
