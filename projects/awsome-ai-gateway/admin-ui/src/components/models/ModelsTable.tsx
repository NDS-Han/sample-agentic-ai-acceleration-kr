'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { Fragment, useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import type { ModelListItem } from '@/types/entities';
import { activateModelAction } from '@/lib/actions/models';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { InfoTooltip } from '@/components/common/InfoTooltip';
import { fmtPricePerM } from '@/lib/utils/pricing';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { CreateModelDialog } from './CreateModelDialog';
import { DeactivateModelDialog } from './DeactivateModelDialog';
import { DeleteModelDialog } from './DeleteModelDialog';

interface ModelsTableProps {
  models: ModelListItem[];
}

function ProviderBadge({ provider }: { provider: string }) {
  const toneMap: Record<string, BadgeTone> = {
    bedrock: 'sky',
    'on-prem': 'pink',
    bedrock_mantle: 'amber',       // Cowork → Mantle Opus (Tokyo)
    bedrock_mantle_openai: 'teal', // Mantle GPT-5.5 (Ohio)
  };
  return <Badge tone={toneMap[provider.toLowerCase()] ?? 'neutral'}>{provider}</Badge>;
}

function StatusBadge({ isActive, activeLabel, inactiveLabel }: { isActive: boolean; activeLabel: string; inactiveLabel: string }) {
  return <Badge tone={isActive ? 'teal' : 'neutral'}>{isActive ? activeLabel : inactiveLabel}</Badge>;
}

function formatNumber(n: number): string {
  return new Intl.NumberFormat('ko-KR').format(n);
}

export function ModelsTable({ models }: ModelsTableProps) {
  const t = useTranslations('models');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [selectedModel, setSelectedModel] = useState<ModelListItem | null>(null);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [deactivateDialogOpen, setDeactivateDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());

  const toggleExpand = (alias: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(alias)) next.delete(alias);
      else next.add(alias);
      return next;
    });
  };

  const handleEdit = (model: ModelListItem) => {
    setSelectedModel(model);
    setEditDialogOpen(true);
  };

  const handleDeactivate = (model: ModelListItem) => {
    setSelectedModel(model);
    setDeactivateDialogOpen(true);
  };

  const handleDelete = (model: ModelListItem) => {
    setSelectedModel(model);
    setDeleteDialogOpen(true);
  };

  const handleActivate = (model: ModelListItem) => {
    startTransition(async () => {
      const result = await activateModelAction(model.alias);
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

  return (
    <>
      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th className="w-8" />
              <Th>
                Alias <InfoTooltip label={t('aliasHelpTitle')}>{t('aliasHelp')}</InfoTooltip>
              </Th>
              <Th>
                {t('displayName')}{' '}
                <InfoTooltip label={t('displayNameHelpTitle')}>{t('displayNameHelp')}</InfoTooltip>
              </Th>
              <Th>Provider</Th>
              <Th numeric>{t('inputPriceShort')}</Th>
              <Th numeric>{t('outputPriceShort')}</Th>
              <Th>{t('status')}</Th>
              <Th>{t('actions')}</Th>
            </Tr>
          </THead>
          <TBody>
            {models.length === 0 ? (
              <TEmpty colSpan={8}>{t('noModels')}</TEmpty>
            ) : (
              models.map((model) => (
                <Fragment key={model.alias}>
                <Tr>
                  <Td>
                    <button
                      type="button"
                      onClick={() => toggleExpand(model.alias)}
                      aria-expanded={expanded.has(model.alias)}
                      aria-label={t('detailExpandAria', { alias: model.alias })}
                      className="inline-flex items-center justify-center rounded-sm p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    >
                      {expanded.has(model.alias) ? (
                        <ChevronDown size={14} aria-hidden="true" />
                      ) : (
                        <ChevronRight size={14} aria-hidden="true" />
                      )}
                    </button>
                  </Td>
                  <Td
                    emphasis
                    className={`font-mono mono-id text-xs${!model.is_active ? ' text-muted-foreground' : ''}`}
                  >
                    {model.alias}
                  </Td>
                  <Td className={!model.is_active ? 'text-muted-foreground' : ''}>
                    {model.display_name ?? <span className="text-muted-foreground">—</span>}
                  </Td>
                  <Td>
                    <ProviderBadge provider={model.provider} />
                  </Td>
                  <Td numeric>{fmtPricePerM(model.input_price_per_1k)}</Td>
                  <Td numeric>{fmtPricePerM(model.output_price_per_1k)}</Td>
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
                        <button
                          onClick={() => handleActivate(model)}
                          disabled={isPending}
                          className="inline-flex items-center justify-center rounded-md border border-primary/40 bg-background px-3 py-1.5 text-xs font-medium text-primary hover:bg-primary/10 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                        >
                          {t('activate')}
                        </button>
                      )}
                      <button
                        onClick={() => handleDelete(model)}
                        disabled={isPending}
                        aria-label={t('deleteAria', { alias: model.alias })}
                        className="inline-flex items-center justify-center rounded-md border border-destructive/40 bg-background px-2 py-1.5 text-xs font-medium text-destructive hover:bg-destructive/15 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                      >
                        {t('delete')}
                      </button>
                    </div>
                  </Td>
                </Tr>
                {expanded.has(model.alias) && (
                  <Tr>
                    <Td colSpan={8} className="bg-muted/30 !py-4">
                      {/* LiteLLM 카탈로그 카드 레이아웃 — TOKEN PRICING 카드 그리드 +
                          MODEL INFO 목록. mode/features 는 게이트웨이가 추적하지
                          않는 데이터라 표시하지 않는다. */}
                      <div className="grid grid-cols-1 lg:grid-cols-[3fr_2fr] gap-6 text-xs">
                        <div>
                          <p className="text-[11px] font-medium tracking-wider text-muted-foreground mb-2">
                            {t('pricingDetailTitle')}
                          </p>
                          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                            {[
                              { label: t('cardInput'), v: fmtPricePerM(model.input_price_per_1k) },
                              { label: t('cardOutput'), v: fmtPricePerM(model.output_price_per_1k) },
                              { label: t('cardCacheRead'), v: model.cache_read_price_per_1k > 0 ? fmtPricePerM(model.cache_read_price_per_1k) : '—' },
                              { label: t('cardCacheWrite5m'), v: model.cache_creation_5m_price_per_1k > 0 ? fmtPricePerM(model.cache_creation_5m_price_per_1k) : '—' },
                              { label: t('cardCacheWrite1h'), v: model.cache_creation_1h_price_per_1k > 0 ? fmtPricePerM(model.cache_creation_1h_price_per_1k) : '—' },
                            ].map((c) => (
                              <div key={c.label} className="rounded-md border border-border px-3 py-2">
                                <p className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">{c.label}</p>
                                <p className="mt-1 text-sm font-semibold tabular-nums">{c.v}</p>
                              </div>
                            ))}
                          </div>
                        </div>
                        <div>
                          <p className="text-[11px] font-medium tracking-wider text-muted-foreground mb-2">
                            {t('modelInfoTitle')}
                          </p>
                          <dl className="space-y-2">
                            <div className="flex items-center justify-between gap-3">
                              <dt className="text-muted-foreground">Provider</dt>
                              <dd><ProviderBadge provider={model.provider} /></dd>
                            </div>
                            <div className="flex items-center justify-between gap-3">
                              <dt className="text-muted-foreground">{t('maxInputLabel')}</dt>
                              <dd className="tabular-nums">
                                {model.context_window > 0
                                  ? `${formatNumber(model.context_window)} tokens`
                                  : <span className="text-muted-foreground">{t('specUnknown')}</span>}
                              </dd>
                            </div>
                            <div className="flex items-center justify-between gap-3">
                              <dt className="text-muted-foreground">{t('maxOutputLabel')}</dt>
                              <dd className="tabular-nums">
                                {model.max_tokens > 0
                                  ? `${formatNumber(model.max_tokens)} tokens`
                                  : <span className="text-muted-foreground">{t('specUnknown')}</span>}
                              </dd>
                            </div>
                            <div className="flex items-start justify-between gap-3">
                              <dt className="text-muted-foreground shrink-0">{t('modelIdLabel')}</dt>
                              <dd className="font-mono mono-id break-all text-right">{model.model_id}</dd>
                            </div>
                            {model.endpoint_url && (
                              <div className="flex items-start justify-between gap-3">
                                <dt className="text-muted-foreground shrink-0">{t('endpointUrlLabel')}</dt>
                                <dd className="font-mono mono-id break-all text-right">{model.endpoint_url}</dd>
                              </div>
                            )}
                            {model.description && (
                              <div className="flex items-start justify-between gap-3">
                                <dt className="text-muted-foreground shrink-0">{t('descriptionLabel')}</dt>
                                <dd className="text-right">{model.description}</dd>
                              </div>
                            )}
                          </dl>
                        </div>
                      </div>
                    </Td>
                  </Tr>
                )}
                </Fragment>
              ))
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

      <DeleteModelDialog
        isOpen={deleteDialogOpen}
        onClose={() => {
          setDeleteDialogOpen(false);
          setSelectedModel(null);
        }}
        model={selectedModel}
      />
    </>
  );
}