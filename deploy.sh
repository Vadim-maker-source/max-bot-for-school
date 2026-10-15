#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec 9>.deploy.lock
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
git diff --quiet && git diff --cached --quiet
test -z "$(git ls-files --others --exclude-standard)" || { echo 'Review untracked files.' >&2; exit 1; }
test -f .env || { echo 'Configure .env first.' >&2; exit 1; }
docker compose config --quiet
git fetch --prune
target=$(git rev-parse --verify --end-of-options "${1:-@{u}}^{commit}")
previous=$(git rev-parse HEAD)
backup="private/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup"
chmod 700 private/backups "$backup"
stopped=false
migration_started=false
failed() {
    echo "Update failed. Previous commit: $previous. Backup: $backup" >&2
    if $stopped && ! $migration_started; then docker compose start app || true; fi
    echo 'Database rollback is not automatic. Review migrations before using an older image.' >&2
}
trap failed ERR
printf '%s\n' "$previous" > "$backup/commit.txt"
git merge --ff-only "$target"
docker compose build app
docker compose stop app
stopped=true
docker compose exec -T db sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$APP_DB_NAME" -Fc' > "$backup/database.dump"
test -s "$backup/database.dump"
docker compose run --rm --no-deps --entrypoint tar app -C /app/private -czf - photos > "$backup/photos.tar.gz"
test -s "$backup/photos.tar.gz"
migration_started=true
docker compose run --rm --no-deps app python database.py migrate
docker compose up -d --no-deps app
for attempt in $(seq 1 30); do
    if docker compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)" 2>/dev/null; then
        echo "Updated to $target; ready. Backup: $backup"
        exit 0
    fi
    sleep 2
done
echo 'Application readiness check failed.' >&2
false
