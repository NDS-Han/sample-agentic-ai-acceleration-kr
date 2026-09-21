'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { Fragment, useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import type { ModelListItem } from '@/types/entities';
import { activateModelAction } from '@/lib/actions/models';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { InfoTooltip } from '@/components/common/InfoTooltip';
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
                  <Td numeric>${model.input_price_per_1k.toFixed(4)}/1K</Td>
                  <Td numeric>${model.output_price_per_1k.toFixed(4)}/1K</Td>
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
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-3 text-xs">
                        <dl className="space-y-2">
                          <div>
                            <dt className="text-muted-foreground">{t('modelIdLabel')}</dt>
                            <dd className="font-mono mono-id mt-0.5">{model.model_id}</dd>
                          </div>
                          {model.endpoint_url && (
                            <div>
                              <dt className="text-muted-foreground">{t('endpointUrlLabel')}</dt>
                              <dd className="font-mono mono-id mt-0.5 break-all">{model.endpoint_url}</dd>
                            </div>
                          )}
                          <div>
                            <dt className="text-muted-foreground">{t('contextWindowLabel')}</dt>
                            <dd className="mt-0.5 tabular-nums">
                              {model.context_window > 0
                                ? `${formatNumber(model.context_window)} tokens · ${t('maxOutputTokens')} ${formatNumber(model.max_tokens)}`
                                : t('specUnknown')}
                            </dd>
                          </div>
                          {model.description && (
                            <div>
                              <dt className="text-muted-foreground">{t('descriptionLabel')}</dt>
                              <dd className="mt-0.5">{model.description}</dd>
                            </div>
                          )}
                        </dl>
                        <div>
                          <p className="text-muted-foreground mb-1.5">{t('pricingDetailTitle')}</p>
                          <dl className="rounded-md border border-border divide-y divide-border overflow-hidden">
                            <div className="flex justify-between px-3 py-1.5">
                              <dt className="text-muted-foreground">{t('inputPriceShort')}</dt>
                              <dd className="tabular-nums">${model.input_price_per_1k.toFixed(4)}/1K</dd>
                            </div>
                            <div className="flex justify-between px-3 py-1.5">
                              <dt className="text-muted-foreground">{t('outputPriceShort')}</dt>
                              <dd className="tabular-nums">${model.output_price_per_1k.toFixed(4)}/1K</dd>
                            </div>
                            <div className="flex justify-between px-3 py-1.5">
                              <dt className="text-muted-foreground">{t('cache5min')}</dt>
                              <dd className="tabular-nums">
                                {model.cache_creation_5m_price_per_1k > 0
                                  ? `$${model.cache_creation_5m_price_per_1k.toFixed(5)}/1K`
                                  : '—'}
                              </dd>
                            </div>
                            <div className="flex justify-between px-3 py-1.5">
                              <dt className="text-muted-foreground">{t('cache1h')}</dt>
                              <dd className="tabular-nums">
                                {model.cache_creation_1h_price_per_1k > 0
                                  ? `$${model.cache_creation_1h_price_per_1k.toFixed(5)}/1K`
                                  : '—'}
                              </dd>
                            </div>
                            <div className="flex justify-between px-3 py-1.5">
                              <dt className="text-muted-foreground">{t('cacheRead')}</dt>
                              <dd className="tabular-nums">
                                {model.cache_read_price_per_1k > 0
                                  ? `$${model.cache_read_price_per_1k.toFixed(5)}/1K`
                                  : '—'}
                              </dd>
                            </div>
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