# Validate gateway capacity with a load test

[한국어](loadtest.md) · **English** · [NDS documentation](README.en.md)

Use the designated test environment. Mock Bedrock reproduces streaming duration and concurrency so operators can check executor saturation, metrics and HPA behavior. Use a separate small real-provider check for production integration assumptions.

1. Record the current gateway Helm values, test namespace, and rollback steps. Prepare the mock service and a test VK.
2. Enable the mock service and route the test gateway to it using the documented Helm override. Confirm rollout and that requests reach the mock.
3. Run the repository’s streaming load tool under `loadtest/`, ramping connections and checking time to first byte, 503s, queue depth, and active streams. Do not share the test credential in logs.
4. Check the metrics path from OTLP to Prometheus adapter and the HPA reaction. Record the intended load shape and what capacity setting it exercised.
5. Restore the managed release with `./deploy apply` after confirming that the rendered configuration excludes the mock endpoint. For a manual Helm release, export the user-supplied values, remove the mock endpoint key, and apply the cleaned values as shown below.

## Restore a manual Helm test release

Set `NS` to the test namespace and use a protected local file for release values. Remove `gatewayProxy.env.AWS_ENDPOINT_URL_BEDROCK_RUNTIME` from the exported YAML before upgrading.

```bash
values_file=$(mktemp)
helm -n "$NS" get values llm-gateway -o yaml > "$values_file"
# Edit the exported YAML before running the next command.
helm upgrade llm-gateway deployment/charts/llm-gateway -n "$NS" -f "$values_file"
kubectl -n "$NS" scale deploy/mock-bedrock --replicas=0
```

Verify the endpoint override is absent from the running proxy and the stored Helm values. Confirm a representative live model request.

Use [the exact mock, run, and rollback commands](loadtest.md) and record findings with the environment and date. See [Observability](observability.en.md) and [Capacity settings](gateway-yaml.en.md).
