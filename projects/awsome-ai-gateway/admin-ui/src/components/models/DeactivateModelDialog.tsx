'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import type { ModelListItem } from '@/types/entities';
import { deactivateModelAction } from '@/lib/actions/models';
import { AppDialog } from '@/components/common/AppDialog';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';

interface DeactivateModelDialogProps {
  isOpen: boolean;
  onClose: () => void;
  model: ModelListItem | null;
}

export function DeactivateModelDialog({ isOpen, onClose, model }: DeactivateModelDialogProps) {
  const t = useTranslations('models');
  const tCommon = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!model) return;
    const alias = model.alias;
    setError(null);

    startTransition(async () => {
      const result = await deactivateModelAction({ alias });

      if (result.success) {
        toast({
          type: 'success',
          message: t('deactivateSuccess', { alias }),
          auto_dismiss_ms: 3000,
        });
        onClose();
      } else {
        setError(result.error);
      }
    });
  };

  return (
    <AppDialog isOpen={isOpen && !!model} onClose={onClose} title={t('deactivateConfirmTitle')}>
      {model && (
        <>
          <p className="text-sm text-muted-foreground mb-4">
            {t('deactivateTarget')} <span className="font-medium text-foreground font-mono">{model.alias}</span>{t('deactivateTargetSuffix')}
          </p>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="rounded-md bg-muted px-4 py-3 space-y-1">
              <p className="text-sm text-muted-foreground">
                {t('deactivateImmediateLead')} <span className="font-medium text-foreground">{t('deactivateImmediateEmphasis')}</span>{t('deactivateImmediateTail')}
              </p>
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
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              >
                {t('deactivate')}
              </SpinnerButton>
            </div>
          </form>
        </>
      )}
    </AppDialog>
  );
}