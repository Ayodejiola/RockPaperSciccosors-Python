#!/usr/bin/env bash
set -euo pipefail

: "${ARGOCD_NAMESPACE:=argocd}"
: "${REPOSITORY_URL:?Set REPOSITORY_URL to the Git repository URL containing this project}"

kubectl create namespace "$ARGOCD_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update argo
helm upgrade --install argocd argo/argo-cd \
  --namespace "$ARGOCD_NAMESPACE" \
  --wait

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
GRAFANA_PASSWORD="$(openssl rand -base64 32)"
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$GRAFANA_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

sed "s#https://github.com/REPLACE_WITH_OWNER/REPLACE_WITH_REPOSITORY.git#$REPOSITORY_URL#" \
  k8s/argocd-apps/apps.yaml | kubectl apply -f -
printf 'Grafana username: admin\nGrafana password: %s\nStore this password in a secure password manager.\n' "$GRAFANA_PASSWORD"
