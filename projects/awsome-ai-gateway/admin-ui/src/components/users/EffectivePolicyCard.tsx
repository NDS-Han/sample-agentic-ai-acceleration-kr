'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { getEffectivePolicyAction } from '@/lib/actions/users';
import { Badge } from '@/components/common/Badge';
import type { EffectivePolicy, EffectivePolicyCell } from '@/types/entities';

interface Props {
  userId: string;
  /** 부모(UserPanel)가 이미 fetch한 정책을 넘기면 재조회를 건너뛴다. */
  policy?: EffectivePolicy | null;
}

/** 거부 축 id → i18n 키 매핑. */
const AXIS_KEYS = ['user_app', 'user_model', 'model_app'] as const;

/**
 * 사용자에게 실제로 적용되는 정책의 합성 읽기 전용 뷰.
 * app×model 매트릭스(어느 축에서 막혔는지) + 예산·rate limit·downgrade·web search 요약.
 */
export function EffectivePolicyCard({ userId, policy: policyProp }: Props) {
  const t = useTranslations('users.effectivePolicy');
  const [fetched, setFetched] = useState<EffectivePolicy | null>(null);
  const [failed, setFailed] = useState(false);

  const policy = policyProp ?? fetched;

  useEffect(() => {
    if (policyProp !== undefined) return; // 부모가 데이터를 소유
    setFetched(null);
    setFailed(false);
    getEffectivePolicyAction(userId).then((r) => {
      if (r.success) setFetched(r.data);
      else setFailed(true);
    });
  }, [userId, policyProp]);

  if (failed) {
    return <p className="text-xs text-destructive py-1">{t('loadFailed')}</p>;
  }
  if (!policy) {
    return <p className="text-xs text-muted-foreground py-1">{t('loading')}</p>;
  }

  const modelAliases = [...new Set(policy.cells.map((c) => c.model_alias))];
  const clients = [...new Set(policy.cells.map((c) => c.client))];

  const cellAt = (client: string, alias: string): EffectivePolicyCell | undefined =>
    policy.cells.find((c) => c.client === client && c.model_alias === alias);

  return (
    <div className="space-y-3">
      {/* 앱 × 모델 매트릭스 */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b">
              <th className="text-left py-1 pr-2 font-medium text-muted-foreground">{t('app')}</th>
              {modelAliases.map((m) => (
                <th key={m} className="text-center py-1 px-1 font-medium text-muted-foreground whitespace-nowrap">
                  {m}
                </th>
              ))}
              <th className="text-center py-1 px-1 font-medium text-muted-foreground whitespace-nowrap">
                {t('webSearch')}
              </th>
            </tr>
          </thead>
          <tbody>
            {clients.map((client) => (
              <tr key={client} className="border-b last:border-0">
                <td className="py-1.5 pr-2 font-medium whitespace-nowrap">{client}</td>
                {modelAliases.map((m) => {
                  const cell = cellAt(client, m);
                  if (!cell) return <td key={m} className="text-center">-</td>;
                  if (cell.allowed) {
                    return (
                      <td key={m} className="text-center text-teal-600" aria-label={t('allowed')}>✓</td>
                    );
                  }
                  const reasons = cell.blocked_by
                    .filter((a): a is (typeof AXIS_KEYS)[number] =>
                      (AXIS_KEYS as readonly string[]).includes(a),
                    )
                    .map((a) => t(`axis.${a}`))
                    .join(', ');
                  return (
                    <td key={m} className="text-center">
                      <span
                        className="text-destructive cursor-help"
                        title={reasons}
                        aria-label={`${t('denied')}: ${reasons}`}
                      >
                        ✗
                      </span>
                    </td>
                  );
                })}
                <td className="text-center">
                  {policy.web_search[client] ? (
                    <span className="text-teal-600">✓</span>
                  ) : (
                    <span className="text-muted-foreground">-</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* 모델 정책 출처 */}
      <p className="text-xs text-muted-foreground">
        {t('modelsSource.label')}:{' '}
        <span className="font-medium text-foreground">
          {policy.allowed_models_source === 'none'
            ? t('modelsSource.none')
            : t(`modelsSource.${policy.allowed_models_source}`)}
        </span>
      </p>

      {/* 예산은 바로 위의 예산 섹션(BudgetGaugeRow)이 이미 같은 데이터를
          보여주므로 여기서는 생략한다 — 카드는 접근 판정에 집중. */}

      {/* rate limit 요약 */}
      {policy.rate_limits.length > 0 && (
        <div>
          <p className="text-xs font-medium mb-1">{t('rateLimits')}</p>
          <ul className="text-xs text-muted-foreground space-y-0.5">
            {policy.rate_limits.map((r, i) => (
              <li key={i}>
                {r.scope}
                {r.model_alias ? ` · ${r.model_alias}` : ''} —{' '}
                {[
                  r.rpm_limit != null && `RPM ${r.rpm_limit}`,
                  r.tpm_limit != null && `TPM ${r.tpm_limit}`,
                  r.cpm_limit_usd != null && `$${r.cpm_limit_usd}/min`,
                  r.cph_limit_usd != null && `$${r.cph_limit_usd}/hr`,
                ]
                  .filter(Boolean)
                  .join(' · ')}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 다운그레이드 규칙 — [scope] [임계] [from → to] 배지 행 */}
      {policy.downgrade_rules.length > 0 && (
        <div>
          <p className="text-xs font-medium mb-1.5">{t('downgrade')}</p>
          <div className="rounded-md border border-border divide-y divide-border overflow-hidden">
            {policy.downgrade_rules.map((d, i) => (
              <div key={i} className="px-3 py-2 text-xs space-y-1.5">
                <div>
                  <Badge tone="sky">
                    {d.scope === 'TEAM' ? t('scopeTeam') : t('scopeUser')}
                  </Badge>
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-muted-foreground">{t('downgradeAtPre')}</span>
                  <Badge tone="amber">{d.threshold_pct}%</Badge>
                  <span className="text-muted-foreground">{t('downgradeAtPost')}</span>
                  <Badge tone="neutral">{d.from_model_alias}</Badge>
                  <span className="text-muted-foreground" aria-hidden="true">→</span>
                  <Badge tone="teal">{d.to_model_alias}</Badge>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
