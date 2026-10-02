'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 모델 카탈로그 테이블 — LiteLLM 스타일 확장 행.
 *
 * 접힌 행(운영 스캔용): 이름 + alias(라우팅 키) + provider + 앱제한 배지,
 * 컨텍스트, 입력/출력/캐시 단가, 상태, 액션.
 * 펼친 행(상세용): 전체 단가(5개), provider_model_id + 복사, 스펙,
 * 등록일, 설명, 앱 범위 정확한 의미(/apps 링크).
 *
 * alias·상태·앱제한은 접힌 상태에서도 보여야 한다 — 라우팅/예산/팀 접근이
 * 전부 alias 를 키로 쓰고, "왜 이 앱에서 안 되지" 의 발견가능성이 배지 목적.
 */

import { Fragment, useMemo, useState, useTransition } from 'react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { ChevronRight, Check, Copy } from 'lucide-react';
import type { ModelListItem } from '@/types/entities';
import { activateModelAction } from '@/lib/actions/models';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { fmtPricePerM } from '@/lib/utils/pricing';
import { fmtDate, fmtTokensCompact } from '@/lib/utils/format';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { CreateModelDialog } from './CreateModelDialog';
import { DeactivateModelDialog } from './DeactivateModelDialog';

interface ModelsTableProps {
  models: ModelListItem[];
}

function ProviderBadge({ provider }: { provider: string }) {
  const toneMap: Record<string, BadgeTone> = {
    bedrock: 'sky',
    'on-prem': 'pink',
    bedrock_mantle: 'amber',       // Cowork → Mantle Opus (Tokyo)
    bedrock_mantle_openai: 'teal', // Codex → Mantle GPT-5.5 (Ohio)
  };
  return <Badge tone={toneMap[provider.toLowerCase()] ?? 'neutral'}>{provider}</Badge>;
}

function StatusBadge({ isActive, activeLabel, inactiveLabel }: { isActive: boolean; activeLabel: string; inactiveLabel: string }) {
  return <Badge tone={isActive ? 'teal' : 'neutral'}>{isActive ? activeLabel : inactiveLabel}</Badge>;
}

/** 단가 셀 — 가격 행 없음(false)은 '—', 0은 실제 무료가 아니라 미등록의
    sanitize 결과일 수 있으나 1K 0 은 명시적 입력으로 취급해 '—' 로 병합. */
function PriceCell({ hasPricing, per1k, muted }: { hasPricing: boolean; per1k: number; muted?: boolean }) {
  return (
    <Td numeric className={muted ? 'text-muted-foreground' : undefined}>
      {hasPricing && per1k > 0 ? fmtPricePerM(per1k) : '—'}
    </Td>
  );
}

export function ModelsTable({ models }: ModelsTableProps) {
  const t = useTranslations('models');
  const { toast } = useToast();
  const tz = useReportingTz();
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [copiedAlias, setCopiedAlias] = useState<string | null>(null);
  // 같은 transition 이 모든 행을 pending 시키는 결함 — 활성화 중인 alias 만 추적.
  const [, startTransition] = useTransition();
  const [activatingAlias, setActivatingAlias] = useState<string | null>(null);
  const [selectedModel, setSelectedModel] = useState<ModelListItem | null>(null);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [deactivateDialogOpen, setDeactivateDialogOpen] = useState(false);

  // 기본 정렬: 활성 먼저, 같은 상태면 최근 등록순 — 운영에서 켜진 모델이 위에 온다.
  const sorted = useMemo(
    () =>
      [...models].sort(
        (a, b) =>
          Number(b.is_active) - Number(a.is_active) ||
          (b.created_at ?? '').localeCompare(a.created_at ?? ''),
      ),
    [models],
  );

  const toggle = (alias: string) =>
    setExpanded((prev) => ({ ...prev, [alias]: !prev[alias] }));

  const handleEdit = (model: ModelListItem) => {
    setSelectedModel(model);
    setEditDialogOpen(true);
  };

  const handleDeactivate = (model: ModelListItem) => {
    setSelectedModel(model);
    setDeactivateDialogOpen(true);
  };

  const handleActivate = (model: ModelListItem) => {
    setActivatingAlias(model.alias);
    startTransition(async () => {
      const result = await activateModelAction(model.alias);
      setActivatingAlias(null);
      if (result.success) {
        toast({
          type: 'success',
          message: t('activateSuccess', { alias: model.alias }),
          auto_dismiss_ms: 3000,
        });
      } else {
        toast({
          type: 'error',
          message: result.error,
          auto_dismiss_ms: 5000,
        });
      }
    });
  };

  const handleCopyModelId = async (model: ModelListItem) => {
    try {
      await navigator.clipboard.writeText(model.model_id);
      setCopiedAlias(model.alias);
      setTimeout(() => setCopiedAlias((cur) => (cur === model.alias ? null : cur)), 1500);
    } catch {
      toast({ type: 'error', message: t('copyFailed'), auto_dismiss_ms: 3000 });
    }
  };

  const colCount = 8;

  return (
    <>
      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>{t('modelColumn')}</Th>
              <Th numeric>{t('context')}</Th>
              <Th numeric>{t('inputPriceShort')}</Th>
              <Th numeric>{t('outputPriceShort')}</Th>
              <Th numeric>{t('cacheRead')}</Th>
              <Th numeric>{t('cacheWrite5m')}</Th>
              <Th>{t('status')}</Th>
              <Th>{t('actions')}</Th>
            </Tr>
          </THead>
          <TBody>
            {sorted.length === 0 ? (
              <TEmpty colSpan={colCount}>{t('noModels')}</TEmpty>
            ) : (
              sorted.map((model) => {
                const isOpen = expanded[model.alias] ?? false;
                const detailId = `model-detail-${model.alias}`;
                const displayName = model.display_name ?? model.alias;
                return (
                  <Fragment key={model.alias}>
                    <Tr>
                      {/* 모델 식별 셀 — 1행 표시명, 2행 alias(라우팅 키)·provider·앱제한 */}
                      <Td emphasis className={!model.is_active ? 'text-muted-foreground' : undefined}>
                        <div className="flex items-center gap-2 min-w-0">
                          <button
                            type="button"
                            onClick={() => toggle(model.alias)}
                            aria-expanded={isOpen}
                            aria-controls={isOpen ? detailId : undefined}
                            aria-label={
                              isOpen
                                ? t('collapse', { name: displayName })
                                : t('expand', { name: displayName })
                            }
                            className="pressable flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                          >
                            <ChevronRight
                              size={14}
                              className={`transition-transform duration-200${isOpen ? ' rotate-90' : ''}`}
                            />
                          </button>
                          <div className="min-w-0">
                            <div className="truncate font-medium">{displayName}</div>
                            <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
                              <span className="font-mono mono-id text-xs text-muted-foreground">
                                {model.alias}
                              </span>
                              {/* 실제 호출되는 공급자 모델 ID — 라우팅 키인 alias와
                                  함께 접힌 행에서도 보여야 매핑을 즉시 확인할 수 있다 */}
                              <span
                                className="max-w-56 truncate font-mono mono-id text-xs text-muted-foreground/80"
                                title={model.model_id}
                              >
                                {model.model_id}
                              </span>
                              <ProviderBadge provider={model.provider} />
                              {/* 모델×앱 제한 — null=무제한은 배지 생략, []=전면 차단은
                                  destructive 톤. 편집은 /apps 소유라 여기선 링크만. */}
                              {model.allowed_clients !== null && (
                                <Link
                                  href="/apps"
                                  title={model.allowed_clients.join(', ') || undefined}
                                  className="rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                                >
                                  <Badge tone={model.allowed_clients.length === 0 ? 'pink' : 'amber'}>
                                    {model.allowed_clients.length === 0
                                      ? t('appScopeBlocked')
                                      : t('appScopeRestricted', { count: model.allowed_clients.length })}
                                  </Badge>
                                </Link>
                              )}
                            </div>
                          </div>
                        </div>
                      </Td>
                      <Td numeric className="text-muted-foreground">
                        {fmtTokensCompact(model.context_window)}
                      </Td>
                      {/* 단가는 앱 표준 per-1M 표기(fmtPricePerM) — 가격동기화·
                          다운그레이드·analytics 와 같은 단위. */}
                      <PriceCell hasPricing={model.has_pricing} per1k={model.input_price_per_1k} />
                      <PriceCell hasPricing={model.has_pricing} per1k={model.output_price_per_1k} />
                      <PriceCell hasPricing={model.has_pricing} per1k={model.cache_read_price_per_1k} muted />
                      <PriceCell hasPricing={model.has_pricing} per1k={model.cache_creation_5m_price_per_1k} muted />
                      <Td>
                        <StatusBadge isActive={model.is_active} activeLabel={t('active')} inactiveLabel={t('inactive')} />
                      </Td>
                      <Td>
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => handleEdit(model)}
                            className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                          >
                            {t('edit')}
                          </button>
                          {model.is_active ? (
                            <button
                              onClick={() => handleDeactivate(model)}
                              className="inline-flex items-center justify-center rounded-md border border-destructive/30 bg-background px-3 py-1.5 text-xs font-medium text-destructive hover:bg-destructive/10 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                            >
                              {t('deactivate')}
                            </button>
                          ) : (
                            // 비활성 모델의 활성화는 1클릭 유지 — 패널을 열어야
                            // 하면 운영 동선이 한 단계 길어진다.
                            <button
                              onClick={() => handleActivate(model)}
                              disabled={activatingAlias === model.alias}
                              className="inline-flex items-center justify-center rounded-md border border-primary/40 bg-background px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/10 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                            >
                              {t('activate')}
                            </button>
                          )}
                        </div>
                      </Td>
                    </Tr>
                    {isOpen && (
                      // 상세 행 — glass 중첩(backdrop-filter 중첩)은 무겁고 지저분해서
                      // muted 표면만. 행 hover 틴트는 상세 행에서 끈다.
                      <Tr id={detailId} className="bg-muted/30 hover:bg-muted/30 dark:bg-white/[0.03] dark:hover:bg-white/[0.03]">
                        <Td colSpan={colCount}>
                          {/* 섹션을 얇은 보더 카드로 분리 — muted 패널 위에
                              평면으로 나열하면 경계가 안 보인다는 피드백 반영. */}
                          <div className="grid gap-3 py-2 md:grid-cols-2 lg:grid-cols-[1.2fr_1fr_1fr]">
                            <section className="rounded-lg border border-border bg-card p-4">
                              <h3 className="mb-3 text-xs font-semibold tracking-wide text-foreground/80">
                                {t('sectionPricing')}
                                <span className="ml-1.5 font-normal text-muted-foreground">{t('per1m')}</span>
                              </h3>
                              <dl className="space-y-1.5 text-sm">
                                {[
                                  [t('priceInput'), model.input_price_per_1k],
                                  [t('priceOutput'), model.output_price_per_1k],
                                  [t('priceCacheRead'), model.cache_read_price_per_1k],
                                  [t('priceCacheCreate5m'), model.cache_creation_5m_price_per_1k],
                                  [t('priceCacheCreate1h'), model.cache_creation_1h_price_per_1k],
                                ].map(([label, per1k]) => (
                                  <div key={label as string} className="flex items-baseline justify-between gap-4">
                                    <dt className="text-muted-foreground">{label}</dt>
                                    <dd className="num font-medium">
                                      {model.has_pricing && (per1k as number) > 0
                                        ? fmtPricePerM(per1k as number)
                                        : '—'}
                                    </dd>
                                  </div>
                                ))}
                              </dl>
                            </section>
                            <section className="rounded-lg border border-border bg-card p-4">
                              <h3 className="mb-3 text-xs font-semibold tracking-wide text-foreground/80">
                                {t('sectionInfo')}
                              </h3>
                              <dl className="space-y-1.5 text-sm">
                                <div className="flex items-baseline justify-between gap-4">
                                  <dt className="text-muted-foreground">{t('provider')}</dt>
                                  <dd><ProviderBadge provider={model.provider} /></dd>
                                </div>
                                <div className="flex items-center justify-between gap-4">
                                  <dt className="text-muted-foreground">{t('modelId')}</dt>
                                  <dd className="flex min-w-0 items-center gap-1">
                                    <span className="truncate font-mono mono-id text-xs">
                                      {model.model_id}
                                    </span>
                                    <button
                                      type="button"
                                      onClick={() => handleCopyModelId(model)}
                                      aria-label={t('copyModelId')}
                                      className="pressable flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                                    >
                                      {copiedAlias === model.alias ? <Check size={12} /> : <Copy size={12} />}
                                    </button>
                                  </dd>
                                </div>
                                <div className="flex items-baseline justify-between gap-4">
                                  <dt className="text-muted-foreground">{t('context')}</dt>
                                  <dd className="num">
                                    {model.context_window != null
                                      ? `${fmtTokensCompact(model.context_window)} (${model.context_window.toLocaleString('en-US')})`
                                      : '—'}
                                  </dd>
                                </div>
                                <div className="flex items-baseline justify-between gap-4">
                                  <dt className="text-muted-foreground">{t('maxOutput')}</dt>
                                  <dd className="num">{fmtTokensCompact(model.max_tokens)}</dd>
                                </div>
                                <div className="flex items-baseline justify-between gap-4">
                                  <dt className="text-muted-foreground">{t('endpointUrl')}</dt>
                                  <dd className="min-w-0 truncate font-mono mono-id text-xs">
                                    {model.endpoint_url ?? '—'}
                                  </dd>
                                </div>
                                <div className="flex items-baseline justify-between gap-4">
                                  <dt className="text-muted-foreground">{t('registered')}</dt>
                                  <dd className="num">
                                    {model.created_at ? fmtDate(model.created_at, tz) : '—'}
                                  </dd>
                                </div>
                              </dl>
                            </section>
                            <section className="rounded-lg border border-border bg-card p-4">
                              <h3 className="mb-3 text-xs font-semibold tracking-wide text-foreground/80">
                                {t('sectionScope')}
                              </h3>
                              <div className="space-y-1.5 text-sm">
                                {model.allowed_clients === null ? (
                                  <p className="text-muted-foreground">{t('appScopeAll')}</p>
                                ) : model.allowed_clients.length === 0 ? (
                                  <Badge tone="pink">{t('appScopeBlocked')}</Badge>
                                ) : (
                                  <ul className="flex flex-wrap gap-1.5">
                                    {model.allowed_clients.map((client) => (
                                      <li key={client}>
                                        <Badge tone="amber">{client}</Badge>
                                      </li>
                                    ))}
                                  </ul>
                                )}
                                <p>
                                  <Link
                                    href="/apps"
                                    className="text-xs text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded"
                                  >
                                    {t('manageInApps')}
                                  </Link>
                                </p>
                              </div>
                            </section>
                          </div>
                          {model.description && (
                            <p className="mt-3 border-t border-border pt-3 text-sm text-muted-foreground">
                              {model.description}
                            </p>
                          )}
                        </Td>
                      </Tr>
                    )}
                  </Fragment>
                );
              })
            )}
          </TBody>
        </Table>
      </div>

      <CreateModelDialog
        isOpen={editDialogOpen}
        onClose={() => {
          setEditDialogOpen(false);
          setSelectedModel(null);
        }}
        editModel={selectedModel ?? undefined}
      />

      <DeactivateModelDialog
        isOpen={deactivateDialogOpen}
        onClose={() => {
          setDeactivateDialogOpen(false);
          setSelectedModel(null);
        }}
        model={selectedModel}
      />
    </>
  );
}
