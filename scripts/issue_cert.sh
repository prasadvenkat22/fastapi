#!/usr/bin/env bash
# Issue (or renew) the Let's Encrypt certificate for dataaisys.com and
# switch nginx to HTTPS. Idempotent: run it again after DNS changes, or from
# cron for renewal. Section 201.
#
#   /opt/fastapi/scripts/issue_cert.sh            # issue if missing, else renew
#
# REQUIRES DNS. The domain moved to dataaisys.com on 2026-09-20 (the old
# data-ai-systems.com was dropped); the A records for the apex and www must
# point at this droplet (159.223.127.31) or the HTTP-01 challenge cannot
# succeed. The script checks and says so.
set -euo pipefail
DOMAIN=dataaisys.com
EMAIL=${CERTBOT_EMAIL:-venkatangirala@gmail.com}
ROOT=/opt/fastapi
CONF=$ROOT/app/nginx/conf.d
DC="docker compose -f $ROOT/docker-compose.yml -f $ROOT/docker-compose.prod.yml --project-directory $ROOT"
MY_IP=$(curl -s -m 10 https://api.ipify.org || hostname -I | awk '{print $1}')

# The apex is required. www is included only if it also points here: on
# 2026-09-20 dataaisys.com resolved to this droplet and www.dataaisys.com had
# no record at all, and Let's Encrypt fails the whole order if any one name
# in it cannot be reached. Add the www A record and re-run to widen the cert.
resolves_here() {
  local got
  got=$(getent ahostsv4 "$1" | awk '{print $1}' | sort -u | tr '\n' ' ')
  echo " $got" | grep -q " $MY_IP "
}
if ! resolves_here "$DOMAIN"; then
  echo "DNS: $DOMAIN does not resolve to this droplet ($MY_IP). Point the A record here first." >&2
  exit 2
fi
NAMES="$DOMAIN"
DOMAIN_ARGS="-d $DOMAIN"
if resolves_here "www.$DOMAIN"; then
  NAMES="$DOMAIN www.$DOMAIN"
  DOMAIN_ARGS="$DOMAIN_ARGS -d www.$DOMAIN"
else
  echo "www.$DOMAIN does not resolve here; issuing for $DOMAIN only." >&2
fi

mkdir -p $ROOT/certbot/www $ROOT/certbot/conf
# One certbot invocation for all three cases. --cert-name pins the lineage so
# the nginx paths below never change; --expand lets the same lineage grow
# from apex-only to apex+www once the www record exists (no deleting the old
# order by hand); --keep-until-expiring makes an unchanged name set a no-op
# until the last 30 days, which is what the weekly cron relies on.
docker run --rm -v $ROOT/certbot/www:/var/www/certbot -v $ROOT/certbot/conf:/etc/letsencrypt   certbot/certbot certonly --webroot -w /var/www/certbot   --cert-name $DOMAIN $DOMAIN_ARGS --expand --keep-until-expiring   --email "$EMAIL" --agree-tos --no-eff-email --non-interactive

# The HTTPS block, written only now that the files it points at exist.
cat > $CONF/dataaisys-ssl.conf <<EOF
# Written by scripts/issue_cert.sh on $(date -u +%F). Do not edit; re-run the script.
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name $NAMES;

    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options SAMEORIGIN always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

    include /etc/nginx/conf.d/dataaisys-locations.inc;
}

# Once HTTPS exists, plain HTTP on the domain redirects. The IP / catch-all
# server in dataaisys.conf keeps serving :80 for the ACME path.
server {
    listen 80;
    listen [::]:80;
    server_name $NAMES;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF

# Same location set as the HTTP server, extracted once so the two never drift.
awk '/^    resolver/,/^}$/' $CONF/dataaisys.conf | sed '$d' | grep -vE 'add_header (X-Frame|X-Content|Referrer|Permissions)' > $CONF/dataaisys-locations.inc

$DC exec -T nginx nginx -t
$DC exec -T nginx nginx -s reload
echo "HTTPS is on for $DOMAIN."
