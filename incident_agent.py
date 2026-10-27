import asyncio
import json
import logging
import os
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from fastapi import FastAPI, HTTPException
from openai import AzureOpenAI
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("incident-agent")

NAMESPACE = os.getenv("TARGET_NAMESPACE", "guessing-game")
DEPLOYMENT = os.getenv("TARGET_DEPLOYMENT", "guessing-api")
PROMETHEUS_URL = os.getenv(
    "PROMETHEUS_URL",
    "http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090",
)
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_service_signals",
            "description": "Read fixed Prometheus service, latency, and DB pool indicators.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_checkout_deployment",
            "description": "Read checkout deployment and pod rollout status without secrets.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "read_service_signals": lambda: read_service_signals(),
    "inspect_checkout_deployment": lambda: inspect_checkout_deployment(),
}


class InvestigationRequest(BaseModel):
    incident_context: str = Field(min_length=10, max_length=3000)


class InvestigationResponse(BaseModel):
    assessment: str
    evidence: dict[str, Any]
    action_boundary: str = "Read-only investigation only; an operator must approve any rollback."


def read_service_signals() -> dict[str, Any]:
    queries = {
        "request_rate_per_second": 'sum(rate(http_requests_total{path!="/metrics"}[5m]))',
        "error_rate": (
            'sum(rate(http_requests_total{status_code=~"5.."}[5m])) '
            '/ clamp_min(sum(rate(http_requests_total[5m])), 0.000001)'
        ),
        "p95_latency_seconds": (
            "histogram_quantile(0.95, "
            "sum by (le) (rate(http_request_duration_seconds_bucket[5m])))"
        ),
        "database_pool_utilization": (
            "max(app_db_pool_checked_out / clamp_min(app_db_pool_capacity, 1))"
        ),
        "database_pool_peak_utilization": (
            "max(app_db_pool_checked_out_peak / clamp_min(app_db_pool_capacity, 1))"
        ),
        "redis_connected_clients": "max(redis_connected_clients)",
        "redis_rejected_connections": "max(redis_rejected_connections_total)",
        "redis_memory_used_bytes": "max(redis_memory_used_bytes)",
    }
    results: dict[str, Any] = {}
    with httpx.Client(timeout=8) as client:
        for name, query in queries.items():
            response = client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                raise RuntimeError(f"Prometheus query {name} failed: {payload.get('error', 'unknown error')}")
            results[name] = payload["data"]["result"]
    return results


def inspect_checkout_deployment() -> dict[str, Any]:
    from kubernetes import client, config
    from kubernetes.config.config_exception import ConfigException

    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config()

    apps_api = client.AppsV1Api()
    core_api = client.CoreV1Api()
    deployment = apps_api.read_namespaced_deployment(DEPLOYMENT, NAMESPACE)
    deployment_status = deployment.status
    pods = core_api.list_namespaced_pod(
        NAMESPACE,
        label_selector="app.kubernetes.io/name=guessing-api",
    )
    replica_sets = apps_api.list_namespaced_replica_set(
        NAMESPACE,
        label_selector="app.kubernetes.io/name=guessing-api",
    )
    return {
        "deployment": DEPLOYMENT,
        "namespace": NAMESPACE,
        "generation": deployment.metadata.generation,
        "observed_generation": (
            deployment_status.observed_generation if deployment_status else None
        ),
        "revision": (deployment.metadata.annotations or {}).get(
            "deployment.kubernetes.io/revision"
        ),
        "replicas": {
            "desired": deployment.spec.replicas,
            "updated": (deployment_status.updated_replicas or 0) if deployment_status else 0,
            "ready": (deployment_status.ready_replicas or 0) if deployment_status else 0,
            "available": (deployment_status.available_replicas or 0) if deployment_status else 0,
        },
        "replica_sets": [
            {
                "name": replica_set.metadata.name,
                "revision": (replica_set.metadata.annotations or {}).get(
                    "deployment.kubernetes.io/revision"
                ),
                "created_at": (
                    replica_set.metadata.creation_timestamp.isoformat()
                    if replica_set.metadata.creation_timestamp
                    else None
                ),
                "desired_replicas": replica_set.spec.replicas or 0,
                "image": replica_set.spec.template.spec.containers[0].image,
            }
            for replica_set in replica_sets.items
            if replica_set.spec.template.spec.containers
        ],
        "pods": [
            {
                "name": pod.metadata.name,
                "phase": pod.status.phase,
                "ready": any(
                    condition.status == "True" and condition.type == "Ready"
                    for condition in (pod.status.conditions or [])
                ),
                "restart_count": sum(
                    container.restart_count for container in (pod.status.container_statuses or [])
                ),
                "waiting_reasons": [
                    container.state.waiting.reason
                    for container in (pod.status.container_statuses or [])
                    if container.state and container.state.waiting
                ],
            }
            for pod in pods.items
        ],
    }


def create_foundry_client() -> tuple[AzureOpenAI, DefaultAzureCredential]:
    if not AZURE_OPENAI_ENDPOINT:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT must be configured.")
    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential,
        "https://cognitiveservices.azure.com/.default",
    )
    return (
        AzureOpenAI(
            azure_endpoint=AZURE_OPENAI_ENDPOINT,
            api_version=AZURE_OPENAI_API_VERSION,
            azure_ad_token_provider=token_provider,
        ),
        credential,
    )


def run_investigation(incident_context: str) -> InvestigationResponse:
    client, credential = create_foundry_client()
    try:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are an incident investigation assistant for a development AKS workload. "
                    "Use both read-only tools before assessing the incident. Cite only values returned "
                    "by those tools, distinguish facts from hypotheses, and include confidence. "
                    "Never claim to have executed an action. If evidence is insufficient, say so. "
                    "Recommend rollback only when rollout timing and service signals support it. "
                    "State the target deployment, expected effect, risks, preconditions, and verification "
                    "signals. A human operator must approve and run any remediation."
                ),
            },
            {"role": "user", "content": incident_context},
        ]
        evidence: dict[str, Any] = {}
        for _ in range(4):
            completion = client.chat.completions.create(
                model=AZURE_OPENAI_DEPLOYMENT,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="required" if len(evidence) < len(TOOL_FUNCTIONS) else "auto",
                temperature=0,
            )
            message = completion.choices[0].message
            if not message.tool_calls:
                if len(evidence) != len(TOOL_FUNCTIONS):
                    raise RuntimeError("Foundry returned an assessment before both investigation tools ran.")
                if not message.content:
                    raise RuntimeError("Foundry returned an empty assessment.")
                return InvestigationResponse(assessment=message.content, evidence=evidence)

            messages.append(message.model_dump(exclude_none=True))
            for tool_call in message.tool_calls:
                name = tool_call.function.name
                if name not in TOOL_FUNCTIONS:
                    raise RuntimeError(f"Foundry requested a non-allowlisted tool: {name}")
                if tool_call.function.arguments not in ("", "{}"):
                    raise RuntimeError(f"Tool {name} does not accept arguments.")
                result = TOOL_FUNCTIONS[name]()
                evidence[name] = result
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(result),
                    }
                )
        raise RuntimeError("Foundry exceeded the maximum investigation tool-call turns.")
    finally:
        client.close()
        credential.close()


app = FastAPI(
    title="Read-only Incident Agent",
    version="1.0.0",
    description="Uses Foundry tool calling for read-only Prometheus and Kubernetes investigation.",
)


@app.get("/health/live", include_in_schema=False)
async def liveness():
    return {"status": "alive"}


@app.get("/health/ready", include_in_schema=False)
async def readiness():
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_DEPLOYMENT:
        raise HTTPException(status_code=503, detail="Foundry model configuration is missing")
    return {"status": "ready"}


@app.post("/investigate", response_model=InvestigationResponse)
async def investigate(payload: InvestigationRequest):
    try:
        return await asyncio.to_thread(run_investigation, payload.incident_context)
    except Exception as exc:
        logger.exception("Incident investigation failed")
        raise HTTPException(status_code=502, detail="Incident investigation failed") from exc
