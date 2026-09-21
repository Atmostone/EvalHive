#!/usr/bin/env bash
# SPA-78 — obtain the initial Let's Encrypt certificate for the EvalHive edge.
# Adapted from the standard nginx-certbot bootstrap: nginx can't start on :443
# without a cert, and the cert can't be issued without nginx serving the ACME
# challenge on :80 — so we stand up a throwaway self-signed cert, start nginx,
# then swap in the real certificate.
#
# Prereqs: ./deploy.sh already run once; DNS for $DOMAIN points at this host;
# ports 80 and 443 are open to the internet.
#
# Env:
#   DOMAIN   (default evalhive.cloud)
#   EMAIL    (optional; used for expiry notices)
#   STAGING  (set 1 to use LE staging while testing, avoids rate limits)
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

DOMAIN="${DOMAIN:-evalhive.cloud}"
EMAIL="${EMAIL:-}"
STAGING="${STAGING:-0}"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
CERT_PATH="/etc/letsencrypt/live/$DOMAIN"

echo "==> [1/5] Dummy certificate so nginx can boot on :443"
$COMPOSE run --rm --entrypoint sh certbot -c "\
  mkdir -p $CERT_PATH && \
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout $CERT_PATH/privkey.pem \
    -out $CERT_PATH/fullchain.pem \
    -subj /CN=localhost"

echo "==> [2/5] Starting the nginx edge (serves the ACME challenge on :80)"
$COMPOSE up -d frontend

echo "==> [3/5] Removing the dummy certificate"
$COMPOSE run --rm --entrypoint sh certbot -c "\
  rm -rf /etc/letsencrypt/live/$DOMAIN \
         /etc/letsencrypt/archive/$DOMAIN \
         /etc/letsencrypt/renewal/$DOMAIN.conf"

echo "==> [4/5] Requesting the real certificate for $DOMAIN"
email_arg="--register-unsafely-without-email"
[ -n "$EMAIL" ] && email_arg="--email $EMAIL"
staging_arg=""
[ "$STAGING" != "0" ] && staging_arg="--staging"

$COMPOSE run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
  $staging_arg $email_arg \
  -d "$DOMAIN" \
  --rsa-key-size 4096 --agree-tos --non-interactive --force-renewal

echo "==> [5/5] Reloading the nginx edge to pick up the real certificate"
$COMPOSE exec frontend nginx -s reload

echo
echo "Done. https://$DOMAIN should now serve a valid certificate."
[ "$STAGING" != "0" ] && echo "(STAGING cert — browsers will warn; re-run without STAGING=1 for a trusted cert.)"
