# Azure AKS game demo

This repository is a cost-aware development foundation for the requested Azure platform. It replaces the interactive guessing script with a small HTTP API that persists games in PostgreSQL, caches statistics in Redis, and exports Prometheus metrics.

## Included

- `infra/main.bicep`: AKS, Azure Container Registry, private Azure Database for PostgreSQL Flexible Server, Azure Managed Redis, a NAT Gateway, and the network and managed identities that connect them. It also provisions a Microsoft Foundry project, a GPT-4.1 mini deployment, and a workload identity limited to model inference.
- `k8s/app`: a two-replica API deployment, health probes, CPU autoscaling, Prometheus scrape annotations, and a Redis exporter.
- `k8s/incident-agent`: a read-only Foundry tool-calling agent with Kubernetes RBAC limited to reading the demo deployment and pods.
- `k8s/argocd-apps/apps.yaml`: Argo CD applications for the API, incident agent, and Prometheus/Grafana Helm chart.
- `scripts/bootstrap-argocd.sh`: installs Argo CD and configures the Grafana admin secret without committing credentials.
- `scripts/approve_rollback.sh`: requires a specific prior revision and an explicit confirmation phrase before a single rollback; it never retries automatically.
- `docker-compose.yml`: local PostgreSQL, Redis, and API for development.
- `scripts/test_connection_pool.py`: concurrent request runner that checks the observed SQLAlchemy pool peak against its configured limit.

The Python API offers `POST /api/games`, `POST /api/games/{id}/guesses`, `GET /api/games`, `GET /api/stats`, `/health/live`, `/health/ready`, and `/metrics`. Statistics use a 30-second Redis cache TTL.

## Local development

Requires Docker Compose.

```sh
docker compose up --build
```

In another terminal:

```sh
python scripts/test_connection_pool.py --requests 100 --concurrency 30
```

The API documentation is at `http://localhost:8000/docs`. Local Compose uses disposable development credentials and schema auto-creation; do not reuse those settings for production.

## Azure deployment outline

The Azure deployment `game-dev-completion-final-20261027` succeeded. The East US resource group `rg-game-dev-eastus` now has AKS, ACR, the Foundry account/project and GPT-4.1 mini deployment, and private Azure Managed Redis. PostgreSQL Flexible Server is in Central US, with private DNS linked to both VNets and global VNet peering to AKS. Both application images are in ACR, the API and read-only incident agent are running in AKS, and Prometheus/Grafana are installed with ClusterIP services. The API has been smoke-tested against PostgreSQL and Redis, and the incident agent completed a read-only investigation.

Argo CD has not yet been installed. The commits described in this deployment are local until pushed to the GitHub repository; push them before bootstrapping Argo CD so its Applications can read the manifests. The Redis Enterprise preview API still emits Bicep warning BCP081 because type metadata is unavailable; the Azure deployment itself succeeded. Cross-region VNet peering data-transfer charges are variable and are not included in the monthly estimate.

1. Sign in to Azure CLI and select the intended subscription. Ensure the deployment identity can create role assignments, since the template grants the AKS, ACR, and Foundry workload identities their scoped roles.
2. Review the Bicep and cost notes below. This is a continuously running stack; stop or delete development resources when not in use.
3. To reproduce or update the deployment, use `infra/main.bicep` and pass `postgresAdminPassword` as a secure parameter (for example, through a protected parameter file). AKS and Redis are in East US; PostgreSQL defaults to Central US (`postgresLocation`) because PostgreSQL version 16 is unavailable to this subscription in East US. The template peers the VNets and links private DNS to both. Pass `aksAdminGroupObjectIds` for a Microsoft Entra group whose members need cluster-admin access; otherwise, grant the deployment operator the required AKS user and Azure RBAC roles separately.
4. To rebuild and publish the images, use the deployed ACR:

   ```sh
   az acr build --registry acrgameuemrtz --image guessing-api:0.1.0 .
   az acr build --registry acrgameuemrtz --image incident-agent:0.1.0 --file Dockerfile.agent .
   ```

5. The non-secret ACR and Foundry values are already set in the Kubernetes manifests. The database and Redis connection strings are stored in the `guessing-api-secrets` Kubernetes Secret; never commit them to Git. When recreating that Secret, use:
   - `database-url`: `postgresql+asyncpg://gameadmin:<URL-encoded-password>@<private-postgres-fqdn>:5432/game?ssl=require`
   - `redis-url`: `rediss://default:<redis-access-key>@<redis-hostname>:10000/0`

   Create the `guessing-game` namespace before its Secret. Obtain the Redis access key through an authorized Azure CLI flow and keep both credentials out of Git.
6. After pushing the commits to the configured GitHub repository, bootstrap Argo CD:

   ```sh
   REPOSITORY_URL=https://github.com/Ayodejiola/RockPaperSciccosors-Python.git scripts/bootstrap-argocd.sh
   ```

   The bootstrap script installs Argo CD, configures the Grafana admin Secret, and applies the Applications. For a private Git repository, configure repository credentials in Argo CD first.

7. Verify the API, incident-agent, and monitoring pods. Forward the agent, API, and Grafana services locally as needed; do not expose them publicly without adding TLS and authentication.

The incident agent investigates only when called; it does not poll continuously or execute cluster changes. Forward the service and submit a specific incident context:

```sh
kubectl port-forward -n incident-agent svc/incident-agent 8080:80
curl -X POST http://localhost:8080/investigate \
  -H 'Content-Type: application/json' \
  -d '{"incident_context":"Investigate current API latency, errors, database pool use, Redis connections, and the latest rollout. Do not make changes."}'
```

If the evidence supports rollback, verify the exact prior revision and run `scripts/approve_rollback.sh <revision>`. The script checks readiness, shows that revision's history, requires the operator to type the target-specific approval phrase, submits one rollback, and waits for rollout status. It appends JSON approval/result records to `rollback-audit.jsonl` (override with `ROLLBACK_AUDIT_LOG`) and prints the same records to stdout. Then observe the metrics for 10 minutes. If rollout verification fails, it stops and records an escalation result. The operator's `kubectl` identity is the only identity that can perform the rollback; the agent's identity has no Kubernetes write permissions. Ship the audit output to an access-controlled durable log in production.

The API manifest uses schema auto-creation only to keep this disposable demo simple. Before production, replace it with versioned database migrations, managed secret delivery, a protected ingress, and environment-specific GitOps configuration.

## Connection pool sizing

Each API process is configured for a PostgreSQL pool of 5 connections plus up to 2 overflow connections, and a Redis pool capped at 10 connections. With the initial two replicas and one worker per pod, the configured ceilings are:

- PostgreSQL: `2 replicas × 1 worker × (5 + 2) = 14` connections.
- Redis: `2 replicas × 1 worker × 10 = 20` connections.

The Horizontal Pod Autoscaler can grow the API to five replicas, so budget the maximum deployment at 35 PostgreSQL and 50 Redis connections. Keep the database's connection limit and Redis capacity above those ceilings with room for migrations and administrative connections. The app exports SQL pool size, current checkouts, overflow, and peak checkout metrics. Run the load test against a single pod to observe that process's pool; for multi-pod behavior, compare each pod's metrics.

## AI-assisted incident response boundaries

The Foundry agent uses two allowlisted, read-only tools: fixed Prometheus queries for API, PostgreSQL pool, and Redis signals; and Kubernetes reads for the demo deployment, ReplicaSets, and pod health. It does not receive Redis or PostgreSQL credentials, does not accept arbitrary PromQL or Kubernetes targets, and has no Kubernetes write permissions. Its model recommendation is advisory. A human operator's separately authenticated `kubectl` identity and the explicit approval script are required for rollback.

The agent is an on-demand internal service, not an autonomous remediation loop. The demo stack does not wire an alert to trigger an action automatically. For production, add authenticated incident intake, an external approval/audit system, alert routing, durable action records, and a reviewed least-privilege remediation service. Keep rollback approval and post-action verification bounded to a single named deployment.

## Approximate monthly cost

This is a planning estimate for East US, not a quote. The estimate assumes one `Standard_D2as_v5` AKS node running continuously, a `Balanced_B0` Azure Managed Redis instance, a burstable `Standard_B1ms` PostgreSQL server with 32 GiB storage, a NAT Gateway with a static IP, Basic ACR, and small persistent volumes for in-cluster Prometheus/Grafana:

| Component | Approximate monthly range |
| --- | ---: |
| AKS worker VM and OS disk | $70–$110 |
| Azure Managed Redis | $40–$100 |
| PostgreSQL Flexible Server and storage | $20–$50 |
| NAT Gateway and public IP | $35–$45 |
| ACR and monitoring storage | $10–$25 |
| **Estimated total** | **$175–$330/month** |

Taxes, cross-region VNet peering data transfer, other bandwidth, snapshots, log retention, support, and Foundry model token usage are not included. Rates vary by region, SKU availability, and usage; confirm with the Azure Pricing Calculator, model quota availability, and deployment what-if before creating anything.
