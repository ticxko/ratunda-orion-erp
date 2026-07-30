#!/usr/bin/env bash
# Nightly ERPNext backup — runs from cron as root on kvm4.
# bench backup --with-files, then copy out of the docker volume to /srv/backup/erpnext
# and prune local copies older than 30 days; then push offsite to Google Drive.
set -euo pipefail

SITE=erp.ratunda.id
DEST=/srv/backup/erpnext
SITES_VOL=/var/lib/docker/volumes/erpnext_sites/_data

docker compose --project-directory /opt/erpnext exec -T backend \
  bench --site "$SITE" backup --with-files >/dev/null

mkdir -p "$DEST"
rsync -a "$SITES_VOL/$SITE/private/backups/" "$DEST/"
find "$DEST" -type f -mtime +30 -delete

rclone copy "$DEST" gdrive:Ratunda/erpnext-backups
rclone delete gdrive:Ratunda/erpnext-backups --min-age 30d
