#!/usr/bin/env bash
# Generate a local CA, a server cert, and a client cert for RTWP TLS / mTLS.
#   scripts/gen_certs.sh            -> deployment/certs/{ca,server,client}.{crt,key}
# Server TLS:  GATEWAY_TLS_ENABLED=true GATEWAY_TLS_CERTFILE=deployment/certs/server.crt \
#              GATEWAY_TLS_KEYFILE=deployment/certs/server.key python -m netgw.server
# mTLS (also require a client cert): add GATEWAY_TLS_CAFILE=deployment/certs/ca.crt
set -euo pipefail
DIR="${1:-deployment/certs}"
mkdir -p "$DIR"; cd "$DIR"
SUBJ_CA="/CN=RTACE-Dev-CA"; SUBJ_SRV="/CN=localhost"; SUBJ_CLI="/CN=rtwp-client"

openssl genrsa -out ca.key 2048 2>/dev/null
openssl req -x509 -new -nodes -key ca.key -sha256 -days 825 -subj "$SUBJ_CA" -out ca.crt 2>/dev/null

openssl genrsa -out server.key 2048 2>/dev/null
openssl req -new -key server.key -subj "$SUBJ_SRV" -out server.csr 2>/dev/null
printf "subjectAltName=DNS:localhost,IP:127.0.0.1" > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 825 -sha256 -extfile server.ext -out server.crt 2>/dev/null

openssl genrsa -out client.key 2048 2>/dev/null
openssl req -new -key client.key -subj "$SUBJ_CLI" -out client.csr 2>/dev/null
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 825 -sha256 -out client.crt 2>/dev/null

rm -f server.csr client.csr server.ext ca.srl
echo "wrote CA, server and client certs to $DIR"
