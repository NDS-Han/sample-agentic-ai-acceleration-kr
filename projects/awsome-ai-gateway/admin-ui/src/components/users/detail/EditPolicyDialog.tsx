'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /users 정책 편집 대화상자의 공통 틀 — 항목 하나만 저장한다(통합 Apply 바 없음).
 *
 *   머리: 제목 + "대상: <이름>"   본문: 결과를 설명하는 안내 줄 + 편집기
 *   바닥: 변경 요약 · [상속으로 되돌리기](DELETE) · [취소] · [저장](바뀐 게 있을 때만)
 *
 * Esc·바깥 클릭 닫기, 포커스 트랩은 AppDialog(Radix Dialog)가 맡는다. 저장 중에는 닫히지 않는다.
 */

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Info } from 'lucide-react';

import { AppDialog } from '@/components/common/AppDialog';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';

export interface EditPolicyDialogProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  /** 대상 이름(팀 표시 이름 또는 사용자). */
  target: string;
  /** 저장하면 무슨 일이 생기는지 — 상속이 끊기는지, 팀원 모두에게 적용되는지 등. */
  info: React.ReactNode;
  /** 경고(예: 상속 중 저장 시 전체 목록 복사). */
  warning?: React.ReactNode;
  /** 바닥 왼쪽 변경 요약. */
  summary: string;
  dirty: boolean;
  /** 입력이 유효하지 않으면 저장 불가(안내는 본문에서). */
  invalid?: boolean;
  loading?: boolean;
  loadFailed?: boolean;
  wide?: boolean;
  /**
   * 긴 목록 대화상자 — 모달은 스크롤하지 않고 본문의 `flex-1 min-h-0 overflow-y-auto` 목록만
   * 스크롤한다(스크롤바 두 개 방지). 목록 요소는 이 모드에서 남은 높이를 채운다.
   */
  fillHeight?: boolean;
  onSave: () => Promise<{ ok: true } | { ok: false; error: string }>;
  /** 없으면 '상속으로 되돌리기'를 숨긴다. */
  onRevert?: () => Promise<{ ok: true } | { ok: false; error: string }>;
  revertLabel?: string;
  revertDisabled?: boolean;
  /** 저장·되돌리기 성공 후 — 부모가 요약·트리를 다시 읽는다. */
  onSaved: () => void;
  children: React.ReactNode;
}

export function EditPolicyDialog({
  isOpen,
  onClose,
  title,
  target,
  info,
  warning,
  summary,
  dirty,
  invalid,
  loading,
  loadFailed,
  wide,
  fillHeight,
  onSave,
  onRevert,
  revertLabel,
  revertDisabled,
  onSaved,
  children,
}: EditPolicyDialogProps) {
  const t = useTranslations('usersDetail.dialog');
  const { toast } = useToast();
  const [busy, setBusy] = useState<null | 'save' | 'revert'>(null);

  const run = async (kind: 'save' | 'revert', fn: () => Promise<{ ok: true } | { ok: false; error: string }>) => {
    setBusy(kind);
    try {
      const r = await fn();
      if (r.ok) {
        toast({ type: 'success', message: kind === 'save' ? t('saved') : t('reverted'), auto_dismiss_ms: 3000 });
        onSaved();
        onClose();
      } else {
        toast({ type: 'error', message: `${t('saveFailed')} ${r.error}`, auto_dismiss_ms: 5000 });
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <AppDialog
      isOpen={isOpen}
      onClose={() => {
        if (!busy) onClose();
      }}
      title={title}
      description={t('target', { target })}
      wide={wide}
      contentClassName={fillHeight ? 'flex flex-col overflow-hidden' : undefined}
    >
      <form
        className={fillHeight ? 'mt-4 flex min-h-0 flex-1 flex-col gap-4' : 'mt-4 space-y-4'}
        onSubmit={(e) => {
          e.preventDefault();
          if (dirty && !invalid && !busy && !loading) void run('save', onSave);
        }}
      >
        <p className="flex items-start gap-2 rounded-md bg-muted/50 px-3 py-2 text-xs text-muted-foreground" data-testid="policy-dialog-info">
          <Info size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>{info}</span>
        </p>
        {warning && (
          <p role="note" className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
            {warning}
          </p>
        )}
        {loadFailed ? (
          <p className="text-sm text-destructive">{t('loadFailed')}</p>
        ) : loading ? (
          <div className="space-y-2" aria-busy="true">
            <div className="h-9 animate-pulse rounded-md bg-muted/60" />
            <div className="h-9 animate-pulse rounded-md bg-muted/60" />
          </div>
        ) : (
          children
        )}
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
          <span className="text-xs text-muted-foreground" data-testid="policy-dialog-summary">
            {summary}
          </span>
          <div className="flex flex-wrap items-center gap-2">
            {onRevert && (
              <button
                type="button"
                onClick={() => void run('revert', onRevert)}
                disabled={!!busy || revertDisabled || loading || loadFailed}
                className="rounded-md px-3 py-2 text-sm text-muted-foreground hover:text-foreground hover:underline disabled:opacity-40 disabled:no-underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {revertLabel ?? t('revert')}
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              disabled={!!busy}
              className="inline-flex items-center justify-center rounded-md border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
            >
              {t('cancel')}
            </button>
            <SpinnerButton
              type="submit"
              isLoading={busy === 'save'}
              disabled={!dirty || !!invalid || !!busy || !!loading || !!loadFailed}
            >
              {t('save')}
            </SpinnerButton>
          </div>
        </div>
      </form>
    </AppDialog>
  );
}
