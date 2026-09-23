#!/bin/bash
# Re-copy the HTTP server's location set into the HTTPS server and reload.
#
# The HTTPS block (dataaisys-ssl.conf, written by issue_cert.sh) includes
# dataaisys-locations.inc, a COPY of dataaisys.conf's locations taken when the
# certificate was issued. A location added to dataaisys.conf afterwards (a
# rate limit, a new route) is live on http:// only until this runs.
#
# Usage (on the droplet, after git pull):  ./scripts/sync_nginx_locations.sh
set -e
ROOT=/opt/fastapi
CONF=$ROOT/app/nginx/conf.d
DC="docker compose -f $ROOT/docker-compose.yml -f $ROOT/docker-compose.prod.yml --project-directory $ROOT"

# Same extraction as issue_cert.sh, so the two never drift.
awk '/^    resolver/,/^}$/' $CONF/dataaisys.conf | sed '$d' | grep -vE 'add_header (X-Frame|X-Content|Referrer|Permissions)' > $CONF/dataaisys-locations.inc.new
mv $CONF/dataaisys-locations.inc.new $CONF/dataaisys-locations.inc

$DC exec -T nginx nginx -t
$DC exec -T nginx nginx -s reload
echo "nginx locations synced and reloaded."
