#!/bin/sh
# Génère un certificat auto-signé (si absent ou si l'adresse a changé) puis
# lance l'app en HTTPS. Le HTTPS est requis par les navigateurs pour accéder
# au micro (getUserMedia) depuis un autre appareil que localhost.
set -e

CERT_DIR=${CERT_DIR:-/data/certs}
HOST=${LIVEFLOW_HOST:-localhost}
CERT="$CERT_DIR/cert.pem"
KEY="$CERT_DIR/key.pem"
mkdir -p "$CERT_DIR"

case "$HOST" in
  *[!0-9.]*) SAN="DNS:$HOST" ;;  # nom d'hôte
  *)         SAN="IP:$HOST"  ;;  # adresse IP
esac

if [ ! -f "$CERT" ] || ! openssl x509 -in "$CERT" -noout -text 2>/dev/null | grep -q "$HOST"; then
  echo "Génération du certificat auto-signé pour $HOST..."
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$KEY" -out "$CERT" -subj "/CN=$HOST" \
    -addext "subjectAltName=$SAN,DNS:localhost,IP:127.0.0.1"
fi

exec uvicorn main:app --host 0.0.0.0 --port 8000 \
  --ssl-certfile "$CERT" --ssl-keyfile "$KEY"
