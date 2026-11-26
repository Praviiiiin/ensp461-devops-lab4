#!/usr/bin/env bash
# Off-cluster S3 object store for Velero: Garage (single node) in Docker container pv-garage on
# the kind network, so backups survive the loss of the whole cluster.
# Secrets (RPC secret, admin token, S3 key) are generated into .secrets/ and never committed.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .secrets
[ -s .secrets/garage.toml ] || cat > .secrets/garage.toml <<TOML
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "$(openssl rand -hex 32)"

[s3_api]
s3_region = "garage"
api_bind_addr = "[::]:3900"

[admin]
api_bind_addr = "[::]:3903"
admin_token = "$(openssl rand -base64 32)"
TOML
docker volume create pv-garage-meta >/dev/null; docker volume create pv-garage-data >/dev/null
docker ps -a --format '{{.Names}}' | grep -qx pv-garage || docker run -d --name pv-garage --restart unless-stopped \
  --network pv-lab4-net -p 127.0.0.1:3900:3900 \
  -v "$PWD/.secrets/garage.toml:/etc/garage.toml:ro" -v pv-garage-meta:/var/lib/garage/meta -v pv-garage-data:/var/lib/garage/data \
  dxflrs/garage:v2.1.0
g() { docker exec pv-garage /garage "$@"; }
until g status >/dev/null 2>&1; do sleep 1; done
node=$(g node id -q | cut -d@ -f1)
if ! g layout show | grep -q "$(echo "$node" | cut -c1-16)"; then
  g layout assign -z lab -c 10G "$node"
  g layout apply --version 1
fi
g bucket info velero >/dev/null 2>&1 || g bucket create velero
if [ ! -s .secrets/velero-s3-credentials ]; then
  out=$(g key create velero-key)
  id=$(echo "$out" | awk '/Key ID/ {print $NF}'); secret=$(echo "$out" | awk '/Secret key/ {print $NF}')
  printf '[default]\naws_access_key_id=%s\naws_secret_access_key=%s\n' "$id" "$secret" > .secrets/velero-s3-credentials
  chmod 600 .secrets/velero-s3-credentials
fi
g bucket allow --read --write --owner velero --key velero-key
g status
g bucket info velero | head -8
