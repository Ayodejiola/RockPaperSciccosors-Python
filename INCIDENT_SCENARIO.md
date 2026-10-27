# Example Incident: API Latency from Redis Connection Exhaustion

## Scenario

At 14:05 UTC, the checkout API running on AKS begins returning slowly. The service uses a highly available Redis cache for session and product data, and Azure SQL Managed Instance (SQL MI) for durable order data. A recent checkout API deployment changed how Redis connections are pooled. Each pod now opens more connections than expected.

The incident copilot is connected to alerting, Azure Monitor, AKS, Redis telemetry, and the incident tracker. It can inspect resources and run approved, allowlisted remediation actions. It cannot make production changes without an operator's approval.

## Alerts and impact

- Checkout API p95 latency exceeds 2 seconds for 5 minutes; the SLO is 500 ms.
- HTTP 5xx responses rise from 0.2% to 4%.
- Redis reports connections near its configured limit and an increase in rejected connections.
- SQL MI CPU and connection utilization remain within normal ranges.
- The issue is limited to checkout; other services using SQL MI remain healthy.

## Copilot investigation

The copilot opens an incident and gathers read-only evidence:

1. It confirms the latency and error alerts are still firing and identifies affected checkout pods.
2. It checks AKS events, pod restarts, readiness, and recent deployment history. The latest rollout completed shortly before the alerts began; pods are ready, with no node pressure or scheduling failures.
3. It checks Redis connection count, rejected connections, memory, and availability. Connections rose sharply after the rollout, while memory and server availability remain healthy.
4. It checks SQL MI health and relevant application errors. SQL MI has no corresponding resource or connectivity degradation.
5. It compares the current deployment configuration with the prior revision and finds that the Redis pool size was increased.

**Assessment:** The rollout likely caused Redis connection exhaustion by multiplying the per-pod connection pool across the checkout replica count. Confidence: high. Evidence points to the application-to-Redis connection path, not SQL MI or AKS node health.

## Proposed healing

The copilot recommends rolling the checkout API deployment back to its previous, known-good revision. It presents the operator with:

- **Target:** checkout API deployment in the affected AKS namespace.
- **Expected effect:** restore the previous Redis pool setting and reduce aggregate Redis connections.
- **Risk:** a rollback may remove unrelated changes included in the latest release.
- **Preconditions:** confirm the previous revision is available, no active release is in progress, and the incident is scoped to checkout.
- **Verification:** watch rollout health, Redis connections and rejected connections, checkout error rate, and p95 latency.
- **Fallback:** stop if the rollout does not become healthy or service indicators worsen; page the on-call SRE and continue investigation without retrying automatically.

The copilot asks the incident commander to approve or reject the rollback. It makes no production changes while waiting.

## Approval and execution

The incident commander reviews the evidence and approves the proposed rollback. The copilot submits the approved action through a narrowly scoped remediation service identity. The action is recorded with the approver, target, timestamp, and result.

The copilot then checks that the deployment is healthy and observes the service and Redis signals for 10 minutes. Redis connections and rejected connections fall, API errors return near baseline, and p95 latency recovers below the SLO. The copilot reports recovery but leaves the incident open for the incident commander to close.

If the rollout fails or the signals do not recover within the verification window, the copilot stops and escalates instead of attempting additional changes on its own.

## Incident record draft

- **Summary:** Checkout API latency and errors increased after a release raised per-pod Redis connection pool size, exhausting the Redis connection limit.
- **Customer impact:** Checkout requests were slow or failed from 14:05 UTC until recovery after rollback.
- **Mitigation:** Incident commander approved rollback of the checkout API deployment to its previous revision.
- **Result:** Redis connection pressure and checkout error rate declined; latency returned below the SLO during the verification window.
- **Likely cause:** Connection pool sizing was not evaluated against the number of running replicas and the Redis connection limit.
- **Follow-up:** Add a load-test check for aggregate Redis connections, alert on rejected connections, and document a reviewed pool-sizing limit for checkout.

## Safety boundaries demonstrated

- Diagnosis uses read-only access; production writes go through a separate, scoped identity.
- The operator approves a specific action and target before execution.
- The copilot verifies recovery and stops on failure rather than chaining unapproved actions.
- The incident record distinguishes observed evidence from the likely cause and preserves the approval and action trail.