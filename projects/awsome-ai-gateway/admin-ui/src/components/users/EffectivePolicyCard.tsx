'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { getEffectivePolicyAction } from '@/lib/actions/users';
import { Badge } from '@/components/common/Badge';
import { CLIENTS as GATEWAY_CLIENTS } from '@/lib/constants/gateway';
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
 * model×app 매트릭스(어느 축에서 막혔는지) + 예산·rate limit·downgrade·web search 요약.
 * 다운그레이드 규칙은 매트릭스보다 위에 둔다 — 모델 수만큼 표가 길어져도 스크롤 없이 보이게.
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

  const modelAliases = [...new Set(policy.cells.map((c) => c.model_alias))].sort();
  // 컬럼 순서는 GATEWAY_CLIENTS 고정 — cells 의 발견 순서에 맡기면 행마다 축이 흔들린다.
  const present = new Set(policy.cells.map((c) => c.client));
  const clients = [
    ...GATEWAY_CLIENTS.filter((cl) => present.has(cl)),
    ...[...present].filter((cl) => !(GATEWAY_CLIENTS as readonly string[]).includes(cl)),
  ];

  const cellAt = (client: string, alias: string): EffectivePolicyCell | undefined =>
    policy.cells.find((c) => c.client === client && c.model_alias === alias);

  return (
    <div className="space-y-3">
      {/* 좌: 다운그레이드 규칙(여러 개면 아래로 늘어남) / 우: 모델×앱 매트릭스.
          매트릭스는 행=모델이라 모델 수만큼 길어지는데, 단일 컬럼이면 우측이
          통째로 비어 보여서 두 컬럼으로 배치한다. 규칙이 없으면 매트릭스만. */}
      <div className="flex flex-wrap items-start gap-x-6 gap-y-3">
      {policy.downgrade_rules.length > 0 && (
        <div className="min-w-64">
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

      {/* 모델 × 앱 매트릭스 — 행=모델, 열=앱. 웹서치는 모델과 무관한 앱별 값이라
          표 맨 아래 행으로 둔다. */}
      <div className="overflow-x-auto">
        {/* w-full 이 아니라 w-auto — 표가 카드 너비만큼 늘어나면 모델명과 앱 체크
            표시 사이에 빈 공백이 크게 벌어져 같은 행인지 읽기 어렵다. */}
        <table className="w-auto text-xs">
          <thead>
            <tr className="border-b">
              <th className="text-left py-1 pr-4 font-medium text-muted-foreground">{t('model')}</th>
              {clients.map((client) => (
                <th key={client} className="text-center py-1 px-3 font-medium text-muted-foreground whitespace-nowrap">
                  {client}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {modelAliases.map((m) => (
              <tr key={m} className="border-b last:border-0">
                <td className="py-1.5 pr-2 font-medium whitespace-nowrap">{m}</td>
                {clients.map((client) => {
                  const cell = cellAt(client, m);
                  if (!cell) return <td key={client} className="text-center">-</td>;
                  if (cell.allowed) {
                    return (
                      <td key={client} className="text-center text-teal-600" aria-label={t('allowed')}>✓</td>
                    );
                  }
                  const reasons = cell.blocked_by
                    .filter((a): a is (typeof AXIS_KEYS)[number] =>
                      (AXIS_KEYS as readonly string[]).includes(a),
                    )
                    .map((a) => t(`axis.${a}`));
                  return (
                    <td key={client} className="text-center">
                      {/* title 어트리뷰트 툴팁은 표시 지연·무시되는 환경이 있어
                          CSS 팝오버로 대체 — hover 와 키보드 focus 둘 다 동작한다. */}
                      <span className="relative inline-flex group">
                        <button
                          type="button"
                          className="text-destructive rounded-sm px-0.5 leading-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                          aria-label={`${t('denied')}: ${reasons.join(', ')}`}
                        >
                          ✗
                        </button>
                        <span
                          role="tooltip"
                          className="pointer-events-none invisible absolute bottom-full left-1/2 z-50 mb-1.5 w-max max-w-56 -translate-x-1/2 rounded-md border border-border bg-popover px-2.5 py-2 text-left text-xs text-popover-foreground shadow-md opacity-0 transition-opacity duration-100 group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
                        >
                          <span className="block font-medium mb-1">{t('deniedTitle')}</span>
                          <ul className="list-disc pl-3.5 space-y-0.5">
                            {reasons.map((r) => (
                              <li key={r}>{r}</li>
                            ))}
                          </ul>
                        </span>
                      </span>
                    </td>
                  );
                })}
              </tr>
            ))}
            {/* 웹서치 — 앱별 토글이라 모델 행들과 같은 표의 마지막 행으로 표현 */}
            <tr className="border-b last:border-0">
              <td className="py-1.5 pr-2 font-medium whitespace-nowrap text-muted-foreground">
                {t('webSearch')}
              </td>
              {clients.map((client) => (
                <td key={client} className="text-center">
                  {policy.web_search[client] ? (
                    <span className="text-teal-600">✓</span>
                  ) : (
                    <span className="text-muted-foreground">-</span>
                  )}
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
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

    </div>
  );
}
