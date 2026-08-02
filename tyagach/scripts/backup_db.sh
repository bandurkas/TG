#!/usr/bin/env bash
# Daily SQLite backup for tyagach.db. Cron on VPS3 (see crontab -l), not run
# in a container -- reads the docker volume file directly from the host.
# `.backup` is safe against a live WAL writer (SQLite's own online-backup API),
# no need to stop the loop/api containers.
set -euo pipefail

DB_PATH="/var/lib/docker/volumes/tyagach_tyagach_data/_data/tyagach.db"
BACKUP_DIR="/root/backups/tyagach"
RETENTION_DAYS=14

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
DEST="${BACKUP_DIR}/tyagach_${STAMP}.db"

sqlite3 "$DB_PATH" ".backup '${DEST}'"
gzip "$DEST"

find "$BACKUP_DIR" -name 'tyagach_*.db.gz' -mtime "+${RETENTION_DAYS}" -delete
