#!/usr/bin/env bash
set -euo pipefail
umask 077

namespace="guessing-game"
deployment="guessing-api"
target_revision="${1:-}"
audit_log="${ROLLBACK_AUDIT_LOG:-./rollback-audit.jsonl}"

if [[ ! "$target_revision" =~ ^[0-9]+$ ]]; then
  printf 'Usage: %s <previous-revision-number>\n' "$0" >&2
  exit 2
fi

deployment_json="$(kubectl get deployment "$deployment" -n "$namespace" -o json)"
read -r current_revision rollout_ready < <(
  python -c '
import json, sys
item = json.loads(sys.stdin.read())
status = item.get("status", {})
desired = item.get("spec", {}).get("replicas", 1)
ready = (
    status.get("observedGeneration", 0) >= item["metadata"].get("generation", 0)
    and status.get("updatedReplicas", 0) == desired
    and status.get("readyReplicas", 0) == desired
)
print(item.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision", ""), str(ready).lower())
' <<<"$deployment_json"
)

if [[ "$rollout_ready" != "true" ]]; then
  printf 'Refusing rollback: the current deployment is not fully rolled out and ready.\n' >&2
  exit 1
fi
if [[ "$target_revision" == "$current_revision" ]]; then
  printf 'Refusing rollback: revision %s is already current.\n' "$target_revision" >&2
  exit 1
fi
if ! kubectl rollout history "deployment/$deployment" -n "$namespace" --revision="$target_revision"; then
  printf 'Refusing rollback: revision %s is not available.\n' "$target_revision" >&2
  exit 1
fi

approval_text="ROLLBACK ${namespace}/${deployment} TO REVISION ${target_revision}"
printf '\nCurrent revision: %s\nTarget revision: %s\n' "$current_revision" "$target_revision"
printf 'Review the target history above and verify the incident is scoped to this deployment.\n'
read -r -p "Type '$approval_text' to approve: " approval
if [[ "$approval" != "$approval_text" ]]; then
  printf 'Approval not confirmed; no change was made.\n'
  exit 1
fi
approver="$(kubectl auth whoami -o jsonpath='{.status.userInfo.username}')"

record_result() {
  local entry
  entry="$(python - "$current_revision" "$target_revision" "$approver" "$1" <<'PY'
import datetime, json, sys
print(json.dumps({
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "approver": sys.argv[3],
    "namespace": "guessing-game",
    "deployment": "guessing-api",
    "from_revision": sys.argv[1],
    "to_revision": sys.argv[2],
    "result": sys.argv[4],
}))
PY
)"
  printf '%s\n' "$entry" | tee -a "$audit_log"
}

record_result "approved"
if ! kubectl rollout undo "deployment/$deployment" -n "$namespace" --to-revision="$target_revision"; then
  record_result "submission_failed"
  exit 1
fi
if ! kubectl rollout status "deployment/$deployment" -n "$namespace" --timeout=5m; then
  record_result "rollout_failed_escalate"
  printf 'Rollback did not become healthy. Stop and escalate; do not retry automatically.\n' >&2
  exit 1
fi

record_result "rollout_healthy"
printf 'Observe API errors, p95 latency, database pool use, and Redis signals for 10 minutes before reporting recovery.\n'
