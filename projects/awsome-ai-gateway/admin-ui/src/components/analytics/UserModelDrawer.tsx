'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { useTranslations } from 'next-intl';
import { ArrowRight, X } from 'lucide-react';
import { fmtLatencyMs, fmtUsd, usdTitle } from '@/lib/utils/format';
import { modelDisplay } from '@/lib/utils/modelLabel';
import { ErrorState } from '@/components/common/ErrorState';
import { MixLegend, TokenMixBar, rowName, useUserModelDetail } from './UserModelTable';
import { fmtInt, fmtTok, type UserModelDetail, type UserModelTarget } from './userModel';

export interface DrawerTarget extends UserModelTarget {
  trigger?: HTMLElement | null;
}

interface UserModelDrawerProps {
  /** null 이면 닫힘. */
  target: DrawerTarget | null;
  onClose: () => void;
  fetchDetail: (_userId: string) => Promise<UserModelDetail>;
  /** user_id 가 없는 대상(구버전 API) — 이메일 검색으로 user_id 를 찾는다. null = 범위 안에 없음. */
  resolveUserId: (_target: UserModelTarget) => Promise<string | null>;
  onShowInTable: (_user: { user_id: string; email: string | null; name: string | null }) => void;
}

type Resolved = { status: 'resolving' } | { status: 'missing' } | { status: 'error' } | { status: 'ok'; userId: string };

/**
 * 한 사용자의 모델별 사용량 — 우측 드로어. Radix Dialog 로 Esc·바깥 클릭 닫기,
 * 포커스 트랩, aria-modal, 제목 라벨링을 받는다(AppDialog 와 같은 원칙). 닫힐 때
 * 포커스는 트리거(Cost by User 의 사용자 이름)로 돌아간다.
 */
export function UserModelDrawer({ target, onClose, fetchDetail, resolveUserId, onShowInTable }: UserModelDrawerProps) {
  const t = useTranslations('analytics.userModel');
  const tc = useTranslations('common');
  const [resolved, setResolved] = useState<Resolved>({ status: 'resolving' });
  // "표에서 보기"로 닫으면 포커스는 트리거가 아니라 표의 그 행으로 간다.
  const skipReturnFocus = useRef(false);
  // 닫힌 뒤에도 돌려줄 대상을 기억한다(target 은 닫힐 때 null 이 된다).
  const triggerRef = useRef<HTMLElement | null>(null);
  if (target) triggerRef.current = target.trigger ?? null;

  const targetUserId = target?.user_id ?? null;
  const targetEmail = target?.email ?? null;
  const targetName = target?.name ?? null;
  const open = !!target;
  useEffect(() => {
    if (!open) return;
    let alive = true;
    if (targetUserId) {
      setResolved({ status: 'ok', userId: targetUserId });
      return;
    }
    setResolved({ status: 'resolving' });
    resolveUserId({ email: targetEmail, name: targetName }).then(
      (id) => alive && setResolved(id ? { status: 'ok', userId: id } : { status: 'missing' }),
      () => alive && setResolved({ status: 'error' }),
    );
    return () => {
      alive = false;
    };
  }, [open, targetUserId, targetEmail, targetName, resolveUserId]);

  const userId = resolved.status === 'ok' ? resolved.userId : null;
  const { state, retry } = useUserModelDetail(open ? userId : null, fetchDetail);
  const detail = state.status === 'ok' && userId ? state.data : null;
  const name = detail ? rowName(detail) : rowName({ name: targetName, email: targetEmail });

  return (
    <Dialog.Root open={open} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay
          data-testid="user-model-drawer-backdrop"
          className="fixed inset-0 z-50 bg-black/30 dark:bg-black/50 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0"
        />
        <Dialog.Content
          aria-describedby={undefined}
          // Radix 는 aria-modal 대신 바깥을 aria-hidden 으로 가린다 — 보조기기 호환을 위해 명시.
          aria-modal="true"
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            if (skipReturnFocus.current) {
              skipReturnFocus.current = false;
              return;
            }
            const el = triggerRef.current;
            if (el && el.isConnected) el.focus();
          }}
          className="fixed inset-y-0 right-0 z-50 flex w-[440px] max-w-full flex-col overflow-y-auto border-l border-border bg-background shadow-xl focus:outline-none data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right"
        >
          <div className="flex flex-col gap-5 p-6">
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 flex-col gap-1">
                <span className="text-xs text-muted-foreground">{t('drawerSubtitle')}</span>
                <Dialog.Title className="break-all text-lg font-bold text-foreground">
                  {name}
                </Dialog.Title>
                {detail?.team_name && (
                  <span className="text-xs text-muted-foreground">{detail.team_name}</span>
                )}
              </div>
              <Dialog.Close
                aria-label={tc('close')}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <X size={16} aria-hidden="true" />
              </Dialog.Close>
            </div>

            {resolved.status === 'missing' ? (
              <p className="text-sm text-muted-foreground">{t('notFound')}</p>
            ) : resolved.status === 'error' || state.status === 'error' ? (
              <ErrorState compact message={t('detailFailed')} onRetry={retry} />
            ) : !detail ? (
              <div className="space-y-3" aria-busy="true">
                <div className="h-16 animate-pulse rounded-apple-sm bg-muted/60" />
                <div className="h-24 animate-pulse rounded-apple-sm bg-muted/60" />
              </div>
            ) : (
              <DrawerBody detail={detail} />
            )}

            {detail && (
              <button
                type="button"
                onClick={() => {
                  skipReturnFocus.current = true;
                  onShowInTable({ user_id: detail.user_id, email: detail.email, name: detail.name });
                }}
                className="inline-flex items-center gap-1.5 self-start rounded py-1 text-sm font-semibold text-primary hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {t('showInTable')}
                <ArrowRight size={14} aria-hidden="true" />
              </button>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function DrawerBody({ detail }: { detail: UserModelDetail }) {
  const t = useTranslations('analytics.userModel');
  const maxTot = Math.max(1, ...detail.models.map((m) => Number(m.total_tokens) || 0));
  const stat = 'flex flex-col gap-1 rounded-apple-sm bg-muted/60 px-3 py-2.5';
  return (
    <>
      <div className="grid grid-cols-3 gap-2.5">
        <div className={stat}>
          <span className="text-xs text-muted-foreground">{t('statTokens')}</span>
          <span className="text-lg font-bold num">{fmtTok(detail.total.total_tokens)}</span>
        </div>
        <div className={stat}>
          <span className="text-xs text-muted-foreground">{t('statCost')}</span>
          <span className="text-lg font-bold num" title={usdTitle(Number(detail.total.cost_usd))}>
            {fmtUsd(Number(detail.total.cost_usd) || 0)}
          </span>
        </div>
        <div className={stat}>
          <span className="text-xs text-muted-foreground">{t('statRequests')}</span>
          <span className="text-lg font-bold num">{fmtInt(detail.total.requests)}</span>
        </div>
      </div>
      <MixLegend />
      <ul className="flex flex-col gap-4">
        {detail.models.map((m) => {
          // 막대 길이 = 이 사용자의 최대 모델 대비 토큰(최소 4%), 안쪽 = 버킷 구성.
          const width = Math.max(4, Math.round(((Number(m.total_tokens) || 0) / maxTot) * 100));
          return (
            <li key={m.model} className="flex flex-col gap-1.5">
              <div className="flex justify-between gap-3 text-sm">
                <span className="font-semibold" title={m.model}>{modelDisplay(m.model, m.display_name)}</span>
                <span className="text-muted-foreground num">
                  {fmtTok(m.total_tokens)} · {fmtUsd(Number(m.cost_usd) || 0)}
                </span>
              </div>
              <div style={{ width: `${width}%` }}>
                <TokenMixBar cell={m} className="h-2.5" />
              </div>
              <span className="text-xs text-muted-foreground">
                {t('drawerModelLine', {
                  input: fmtTok(m.input_tokens),
                  output: fmtTok(m.output_tokens),
                  cache: fmtTok((Number(m.cache_read_tokens) || 0) + (Number(m.cache_write_tokens) || 0)),
                  requests: fmtInt(m.requests),
                  latency: fmtLatencyMs(m.avg_latency_ms),
                })}
              </span>
            </li>
          );
        })}
      </ul>
    </>
  );
}
