#!/usr/bin/env bash
# Issue (or renew) the Let's Encrypt certificate for data-ai-systems.com and
# switch nginx to HTTPS. Idempotent: run it again after DNS changes, or from
# cron for renewal. Section 201.
#
#   /opt/fastapi/scripts/issue_cert.sh            # issue if missing, else renew
#
# REQUIRES DNS. On 2026-09-19 data-ai-systems.com resolved to 74.91.138.134,
# not this droplet (159.223.127.31); the HTTP-01 challenge cannot succeed until
# the A records for the apex and www point here. The script checks and says so.
set -euo pipefail
DOMAIN=data-ai-systems.com
EMAIL=${CERTBOT_EMAIL:-venkatangirala@gmail.com}
ROOT=/opt/fastapi
CONF=$ROOT/app/nginx/conf.d
DC="docker compose -f $ROOT/docker-compose.yml -f $ROOT/docker-compose.prod.yml --project-directory $ROOT"
MY_IP=$(curl -s -m 10 https://api.ipify.org || hostname -I | awk '{print $1}')

for h in $DOMAIN www.$DOMAIN; do
  got=$(getent ahostsv4 "$h" | awk '{print $1}' | sort -u | tr '\n' ' ')
  if ! echo " $got" | grep -q " $MY_IP "; then
    echo "DNS: $h -> ${got:-nothing}; this droplet is $MY_IP. Point the A record here first." >&2
    exit 2
  fi
done

mkdir -p $ROOT/certbot/www $ROOT/certbot/conf
if [ -f "$ROOT/certbot/conf/live/$DOMAIN/fullchain.pem" ]; then
  docker run --rm -v $ROOT/certbot/www:/var/www/certbot -v $ROOT/certbot/conf:/etc/letsencrypt \
    certbot/certbot renew --webroot -w /var/www/certbot --quiet
else
  docker run --rm -v $ROOT/certbot/www:/var/www/certbot -v $ROOT/certbot/conf:/etc/letsencrypt \
    certbot/certbot certonly --webroot -w /var/www/certbot \
    -d $DOMAIN -d www.$DOMAIN --email "$EMAIL" --agree-tos --no-eff-email --non-interactive
fi

# The HTTPS block, written only now that the files it points at exist.
cat > $CONF/data-ai-systems-ssl.conf <<EOF
# Written by scripts/issue_cert.sh on $(date -u +%F). Do not edit; re-run the script.
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name $DOMAIN www.$DOMAIN;

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

    include /etc/nginx/conf.d/data-ai-systems-locations.inc;
}

# Once HTTPS exists, plain HTTP on the domain redirects. The IP / catch-all
# server in data-ai-systems.conf keeps serving :80 for the ACME path.
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN www.$DOMAIN;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF

# Same location set as the HTTP server, extracted once so the two never drift.
awk '/^    resolver/,/^}$/' $CONF/data-ai-systems.conf | sed '$d' | grep -vE 'add_header (X-Frame|X-Content|Referrer|Permissions)' > $CONF/data-ai-systems-locations.inc

$DC exec -T nginx nginx -t
$DC exec -T nginx nginx -s reload
echo "HTTPS is on for $DOMAIN."
