# Deploy BI Insight

[한국어](chat-agent.md) · **English** · [NDS documentation](README.en.md)

BI Insight is an optional AgentCore-backed capability for gateway administrators. Enable it on ECS or EKS through `features.bi_insight: true`.

1. Review the intended deployment region, runtime image, IAM resources, and database access path.
2. Render and preview the deployment. EKS uses the managed Terraform overlay. ECS passes feature inputs through its environment tfvars.
3. Apply the approved change. Verify the AgentCore runtime is ready and the administrator API uses the resulting ARN. Check the chat-reader credential setup and query tool connectivity.
4. Confirm **BI Insight** is visible to a gateway administrator. Run an approved test question and verify the expected records and result.
5. Use the [feature teardown and manual recovery details](chat-agent.md) for an environment owned by the documented provisioning routes.

For usage, see [BI Insight in the administrator guide](guides/admin-guide.en.html#bi-insight).
