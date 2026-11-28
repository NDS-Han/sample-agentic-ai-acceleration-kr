# Administrator console

[한국어](admin-ui.md) · **English** · [Comparison index](README.en.md)

## Feature and navigation changes

| Menu | NDS operator workflow |
|---|---|
| Dashboard | Select app and period, inspect model and search spend, usage, budgets and reporting time |
| Users/Teams | Deep links, effective and inherited policy, Cognito sync, team reauthentication, rate limits, downgrade and search quotas |
| Model Management | Alias, provider and context fields, account routing, long-context rates, pricing preview/apply and lifecycle confirmations |
| App Policy | Configure app-specific defaults and access in `/apps` |
| Budget Management | User/team caps, allocation, commitment ratio, app views and bulk tasks |
| API Keys | Review employee VK status, expiry and revocation |
| Agents | Issue, rotate and repair service account VKs. `/agents/[userId]` provides the detail flow |
| Monitoring | Inspect filters and traces. `/monitoring/body-logs` depends on deployment |
| Analytics | User and model data, client and date filters, reporting timezone and exports |
| BI Insight | Conversational analytics at `/chat`, subject to infrastructure and flags |
| Audit Log | Locate administrative actions and affected records at `/audit` |
| Integration | Approved client and application guidance at `/integration` |
| Organization Policy | Inspect organization defaults alongside team and user settings |

There are compatibility redirects for `/service-accounts`, `/rate-limits` and `/analytics/models`. Routes and navigation depend on the role, feature flags and image. [Detailed administrator guide](../guides/admin-guide.en.html) · [UI source](../../../admin-ui/src)

## Verify an operator change

The UI supports Korean and English. Use matching date ranges in the configured reporting timezone. After applying a change, check the result and the audit event, then confirm effective permissions with the affected employee or team-leader role. `/v1/auth/provision` failures prevent login completion. Pricing sources and cost views depend on the backend and service version. Use the documented development baseline when interpreting the captures. [Comparison scope](README.en.md) · [Operations handover](../op-guide/README.md)
