#!/usr/bin/env bash
# Brings up the Lab 4 platform from nothing. Everything after the age key comes from Git via Flux.
#   1. kind cluster pv-lab4 on Docker network pv-lab4-net
#   2. the SOPS age private key as flux-system/sops-age (the only out-of-band secret)
#   3. Flux controllers + the GitRepository/Kustomization pointing at this repo
set -euo pipefail
cd "$(dirname "$0")/.."
kind get clusters | grep -qx pv-lab4 || KIND_EXPERIMENTAL_DOCKER_NETWORK=pv-lab4-net kind create cluster --config platform/kind.yaml --wait 120s
kubectl config use-context kind-pv-lab4
kubectl create namespace flux-system --dry-run=client -o yaml | kubectl apply -f -
kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=.secrets/age.agekey \
  --dry-run=client -o yaml | kubectl apply -f -
# First pass installs the CRDs (the sync objects are rejected), second pass after they are established.
kubectl apply --server-side -k clusters/pv-lab4/flux-system || true
kubectl wait --for condition=established --timeout=60s crd/gitrepositories.source.toolkit.fluxcd.io crd/kustomizations.kustomize.toolkit.fluxcd.io
kubectl apply --server-side -k clusters/pv-lab4/flux-system
flux check
