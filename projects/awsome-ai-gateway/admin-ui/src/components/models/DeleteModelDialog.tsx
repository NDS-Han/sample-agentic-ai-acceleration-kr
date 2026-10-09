'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 모델 hard delete 확인 — deprecated 카탈로그 정리용.
 *
 * 비활성화(DeactivateModelDialog)보다 한 단계 무거운 파괴 동작이라
 *  - 열릴 때 deletion-impact 를 조회해 함께 지워지는 설정 수를 먼저 보여주고
 *  - downgrade 목적지로 쓰이면 확인 버튼을 비활성화하고
 *  - alias 를 그대로 타이핑해야 삭제 버튼이 열린다.
 * usage_logs 는 남는다 — 패널에 '보존'으로 표시해 오해를 막는다.
 */

import { useEffect, useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import { AlertTriangle } from 'lucide-react';
import type { ModelListItem } from '@/types/entities';
import {
  deleteModelAction,
  getModelDeletionImpactAction,
  type ModelDeletionImpact,
} from '@/lib/actions/models';
import { AppDialog } from '@/components/common/AppDialog';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';

interface DeleteModelDialogProps {
  isOpen: boolean;
  onClose: () => void;
  model: ModelListItem | null;
}

export function DeleteModelDialog({ isOpen, onClose, model }: DeleteModelDialogProps) {
  const t = useTranslations('models');
  const tCommon = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [impact, setImpact] = useState<ModelDeletionImpact | null>(null);
  const [impactError, setImpactError] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState('');

  const alias = model?.alias ?? '';
  const open = isOpen && !!model;

  // 다이얼로그가 열릴 때마다 영향도를 새로 조회 — 이전 모델의 결과가
  // 잠깐 보이는 걸 막기 위해 닫힐 때 상태를 비운다.
  useEffect(() => {
    if (!open) {
      setImpact(null);
      setImpactError(null);
      setConfirmText('');
      setError(null);
      return;
    }
    let cancelled = false;
    getModelDeletionImpactAction(alias).then((result) => {
      if (cancelled) return;
      if (result.success) {
        setImpact(result.data);
      } else {
        setImpactError(result.error);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [open, alias]);

  const blocked = (impact?.downgrade_to ?? 0) > 0;
  const confirmed = confirmText === alias;
  const canDelete = !!impact && !blocked && confirmed && !isPending;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canDelete) return;
    setError(null);

    startTransition(async () => {
      const result = await deleteModelAction(alias);
      if (result.success) {
        toast({
          type: 'success',
          message: t('deleteSuccess', { alias }),
          auto_dismiss_ms: 3000,
        });
        onClose();
      } else {
        setError(result.error);
      }
    });
  };

  // 함께 정리되는 설정 행 — 0인 항목도 보여줘 "뭘 지우는지"가 완전하게 보이게.
  const impactRows: Array<[string, number]> = impact
    ? [
        [t('deleteImpactPricings'), impact.pricings],
        [t('deleteImpactTeamAllowed'), impact.team_allowed],
        [t('deleteImpactUserAllowed'), impact.user_allowed],
        [t('deleteImpactRateLimits'), impact.rate_limits],
        [t('deleteImpactDowngradeFrom'), impact.downgrade_from],
      ]
    : [];

  return (
    <AppDialog isOpen={open} onClose={onClose} title={t('deleteConfirmTitle')}>
      {model && (
        <form onSubmit={handleSubmit} className="space-y-4">
          <p className="text-sm text-muted-foreground">
            {t('deleteTarget')} <span className="font-medium text-foreground font-mono">{model.alias}</span>
          </p>

          {/* 경고 박스 — 비활성화의 "언제든 되돌릴 수 있다"와 대비되는 문구. */}
          <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 space-y-1.5">
            <p className="flex items-center gap-1.5 text-sm font-medium text-destructive">
              <AlertTriangle size={14} aria-hidden="true" />
              {t('deleteWarningTitle')}
            </p>
            <p className="text-sm text-muted-foreground">{t('deleteWarningBody')}</p>
          </div>

          {/* 영향도 — 조회 실패/진행 중/차단 상태를 같은 자리에서 표시. */}
          <div className="rounded-md bg-muted px-4 py-3">
            {impactError ? (
              <FormError error={impactError} />
            ) : !impact ? (
              <p className="text-sm text-muted-foreground">{t('deleteImpactLoading')}</p>
            ) : (
              <dl className="space-y-1.5 text-sm">
                <div className="flex items-baseline justify-between gap-4">
                  <dt className="text-muted-foreground">{t('deleteImpactUsageLogs')}</dt>
                  <dd className="num font-medium">
                    {impact.usage_logs.toLocaleString()}
                    <span className="ml-1 text-xs text-muted-foreground">{t('deleteImpactRetained')}</span>
                  </dd>
                </div>
                {impactRows.map(([label, n]) => (
                  <div key={label} className="flex items-baseline justify-between gap-4">
                    <dt className="text-muted-foreground">{label}</dt>
                    <dd className="num font-medium">
                      {n.toLocaleString()}
                      <span className="ml-1 text-xs text-destructive">{t('deleteImpactRemoved')}</span>
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </div>

          {blocked && (
            <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-700 dark:text-amber-400">
              {t('deleteBlockedDowngradeTo', { count: impact?.downgrade_to ?? 0 })}
            </p>
          )}

          {/* alias 재입력 — 실수 클릭/자동완성으로 지워지는 걸 막는다. */}
          <div>
            <label htmlFor="delete-model-confirm" className="mb-1.5 block text-sm text-muted-foreground">
              {t('deleteTypeAlias', { alias: model.alias })}
            </label>
            <input
              id="delete-model-confirm"
              type="text"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              disabled={isPending || blocked}
              autoComplete="off"
              spellCheck={false}
              className="w-full rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
              placeholder={model.alias}
            />
          </div>

          <FormError error={error} />

          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={isPending}
              className="inline-flex items-center justify-center rounded-md border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
            >
              {tCommon('cancel')}
            </button>
            <SpinnerButton
              type="submit"
              isLoading={isPending}
              disabled={!canDelete}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
            >
              {t('deleteButton')}
            </SpinnerButton>
          </div>
        </form>
      )}
    </AppDialog>
  );
}
