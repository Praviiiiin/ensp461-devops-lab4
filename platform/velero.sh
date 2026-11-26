#!/usr/bin/env bash
# Velero with the AWS (S3) plugin against the off-cluster Garage bucket, plus the node agent
# for file-system (Kopia) backups of opted-in pod volumes. Installed outside Flux on purpose:
# the DR tooling must not depend on the GitOps layer it may have to rebuild.
# publicUrl lets the velero CLI on the Mac fetch logs/descriptions through the published port.
set -euo pipefail
cd "$(dirname "$0")/.."
# The Homebrew CLI reports its version without the "v" prefix, so the image is pinned explicitly.
velero install \
  --provider aws \
  --image velero/velero:v1.19.0 \
  --plugins velero/velero-plugin-for-aws:v1.15.0 \
  --bucket velero \
  --secret-file .secrets/velero-s3-credentials \
  --backup-location-config region=garage,s3ForcePathStyle=true,s3Url=http://pv-garage:3900,publicUrl=http://127.0.0.1:3900 \
  --use-volume-snapshots=false \
  --use-node-agent \
  --default-volumes-to-fs-backup=false \
  --wait
velero backup-location get
