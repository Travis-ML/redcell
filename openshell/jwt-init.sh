#!/bin/sh
# Generate the gateway's sandbox-JWT signing keypair if it does not exist yet.
#
# The OpenShell gateway refuses to start without these files and does not create
# them itself. They must live under /var/lib/openshell, which is bind-mounted at
# the same absolute path on both sides so the Docker daemon can resolve it when
# it creates sandbox containers -- which also means this script has to run in a
# container, not on the host: on Docker Desktop that path exists only inside the
# Linux VM.
#
# Ed25519 is what the gateway expects. This is not documented anywhere; it was
# determined by testing against 0.0.92.
set -eu

JWT_DIR=/var/lib/openshell/jwt

if [ -s "$JWT_DIR/signing.pem" ] && [ -s "$JWT_DIR/public.pem" ] && [ -s "$JWT_DIR/kid" ]; then
	echo "jwt-init: keypair already present in $JWT_DIR, nothing to do"
	exit 0
fi

echo "jwt-init: generating Ed25519 sandbox-JWT keypair in $JWT_DIR"
mkdir -p "$JWT_DIR"
openssl genpkey -algorithm ed25519 -out "$JWT_DIR/signing.pem"
openssl pkey -in "$JWT_DIR/signing.pem" -pubout -out "$JWT_DIR/public.pem"
# The kid is an opaque key identifier; any stable string works.
printf 'redcell-gateway-1' >"$JWT_DIR/kid"
chmod 600 "$JWT_DIR/signing.pem"
chmod 644 "$JWT_DIR/public.pem" "$JWT_DIR/kid"
echo "jwt-init: done"
