# Gateway proxy request path

[한국어](gateway-proxy.md) · **English** · [Comparison index](README.en.md)

## What changes for one request

| Stage | NDS change | Operator consequence |
|---|---|---|
| Authentication and policy | Extends existing VK and client validation with app defaults, model and user/team/org rules, and `GET /v1/policy` | Verify the actual client and its effective policy |
| Model selection | Model ARN, then client ARN, then workload IAM. The alias stays independent of the provider ID | Verify the invoking account, subscription and allowed ARNs |
| Budget | User/team reservation, settlement and downgrade. Each Claude Code or Cowork cap combines with the matching group cap | The lower cap applies. Zero blocks, and deletion removes the group constraint |
| Transport and cost | Anthropic tool blocks and mixed search/client tools, Codex and streaming paths, long-context and search costs | Correlate time to first token, result and ledger entry |
| Observability | Request, session, key prefix, routing, settlement and executor metrics, plus optional body logs | Start with metadata and audit evidence |

## Search and cost

The range extends the existing search fields with AgentCore Gateway provisioning, native tool blocks, search cost settlement, quotas and backfill. Search quotas have organization and team pools and per-user overrides. Model and search costs are recorded separately. Distinguish historical search backfill in reporting from contemporaneous enforcement. For long context, compare the applied request tier and input, output and cache rate bands. [Quota implementation](../../../gateway-proxy/src/app/services/web_search_quota.py) · [Cost ledger](../../../gateway-proxy/src/app/services/cost_ledger.py)

## Availability and enforcement

Budget degradation in `fafbf571` uses bounded protection and prior state where policy permits and can return 503 if a protected decision cannot rely on usable state. Reservation and settlement use Redis Lua in standalone and cluster modes. `8040b1d5` restores settlement parity and cache invalidation behavior. Other additions include a local model cache, startup warmup, fewer hot-path round trips, proxy isolation and saturation metrics. Classify 503s by provider, executor, Redis or budget protection, and DB before changing capacity or policy. [Budget service](../../../gateway-proxy/src/app/services/budget_service.py) · [Degraded path](../../../gateway-proxy/src/app/services/budget_degraded.py)

### Budget, provider and cost diagnosis

User/app and team budgets reserve spend before processing and settle using the known usage cost. When a team uses downgrade rules, verify the threshold, destination model permissions and pricing together. If the reservation, final event and settlement disagree, compare ledger, Redis and reconciliation metrics first. Distinguish rate changes for new requests from rollup corrections. For provider errors, identify the model, role and region used.

## Body logs and client policy

Body logging requires infrastructure and an administrative runtime switch. It can write unmasked payloads through Firehose to S3, with audited viewing. `/v1/policy` serves policy fragments to the optional VK-authenticated helper. If a governance provider is configured, check its availability and the actual response when troubleshooting a fragment request. [Observability](../observability.en.md) · [Client distribution](clients.en.md)
