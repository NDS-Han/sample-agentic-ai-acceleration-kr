'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 팀장 변경 — 현재 팀장 해제, 구성원 중 지정. 기존 API 그대로
 * (PUT /admin/teams/{id}/leader · DELETE /admin/teams/{id}/leaders/{user_id}).
 * 관리자(ADMIN)는 팀장으로 지정할 수 없다(기존 규칙).
 */

import { useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';

import { AppDialog } from '@/components/common/AppDialog';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';
import { setTeamLeaderAction, unsetTeamLeaderAction } from '@/lib/actions/users';
import { EmailText } from '@/components/common/UnverifiedEmailBadge';
import { isSyntheticEmail, userLabel } from '@/lib/userLabel';
import type { OrgTreeNode } from '@/types/entities';

export function LeaderDialog({
  isOpen,
  onClose,
  team,
  target,
  onChanged,
}: {
  isOpen: boolean;
  onClose: () => void;
  team: OrgTreeNode;
  target: string;
  onChanged: () => void;
}) {
  const t = useTranslations('usersDetail.leader');
  const tc = useTranslations('common');
  const { toast } = useToast();
  const [pick, setPick] = useState('');
  const [pending, start] = useTransition();
  const members = team.children ?? [];
  const leaders = members.filter((m) => m.meta.role === 'TEAM_LEADER');
  const candidates = members.filter((m) => m.meta.role !== 'TEAM_LEADER');
  const label = (m: OrgTreeNode) => userLabel({ email: m.meta.email, display_name: m.name });

  const assign = () =>
    start(async () => {
      const r = await setTeamLeaderAction(pick, team.id);
      if (r.success) {
        toast({ type: 'success', message: t('assigned'), auto_dismiss_ms: 3000 });
        setPick('');
        onChanged();
      } else toast({ type: 'error', message: r.error, auto_dismiss_ms: 4000 });
    });
  const remove = (id: string) =>
    start(async () => {
      const r = await unsetTeamLeaderAction(team.id, id);
      if (r.success) {
        toast({ type: 'success', message: t('removed'), auto_dismiss_ms: 3000 });
        onChanged();
      } else toast({ type: 'error', message: r.error, auto_dismiss_ms: 4000 });
    });

  return (
    <AppDialog isOpen={isOpen} onClose={() => !pending && onClose()} title={t('title')} description={t('target', { target })}>
      <div className="mt-4 space-y-4">
        <div>
          <p className="mb-1.5 text-xs font-medium text-muted-foreground">{t('current')}</p>
          {leaders.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t('none')}</p>
          ) : (
            <ul className="space-y-1.5">
              {leaders.map((l) => (
                <li key={l.id} className="flex items-center justify-between gap-2 rounded-md border border-border px-3 py-1.5">
                  <span className="flex min-w-0 items-center gap-1.5 text-sm">
                    <span className="truncate">{label(l)}</span>
                    {/* 합성 이메일(<sub>@unknown)은 미인증 배지로 — 식별이 안 되는 주소를 그대로 보이지 않는다. */}
                    {isSyntheticEmail(l.meta.email) ? (
                      <EmailText email={l.meta.email} />
                    ) : (
                      l.meta.email &&
                      l.name &&
                      l.name !== l.meta.email && <span className="truncate text-xs text-muted-foreground">{l.meta.email}</span>
                    )}
                  </span>
                  <button
                    type="button"
                    onClick={() => remove(l.id)}
                    disabled={pending}
                    className="shrink-0 rounded px-1.5 py-0.5 text-xs text-destructive hover:underline disabled:opacity-50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  >
                    {t('remove')}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        {candidates.length > 0 && (
          <div className="space-y-1.5">
            <label htmlFor={`leader-pick-${team.id}`} className="text-xs font-medium text-muted-foreground">
              {t('assignLabel')}
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <select
                id={`leader-pick-${team.id}`}
                value={pick}
                onChange={(e) => setPick(e.target.value)}
                disabled={pending}
                className="min-w-0 flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
              >
                <option value="">{t('placeholder')}</option>
                {candidates.map((m) => (
                  <option key={m.id} value={m.id} disabled={m.meta.role === 'ADMIN'}>
                    {label(m)}
                    {isSyntheticEmail(m.meta.email) ? ` (${tc('unverifiedEmail')})` : ''}
                    {m.meta.role === 'ADMIN' ? ` · ${t('adminNotAllowed')}` : ''}
                  </option>
                ))}
              </select>
              <SpinnerButton type="button" isLoading={pending} disabled={!pick} onClick={assign}>
                {t('assign')}
              </SpinnerButton>
            </div>
          </div>
        )}
        <div className="flex justify-end border-t border-border pt-4">
          <button
            type="button"
            onClick={onClose}
            disabled={pending}
            className="rounded-md border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            {t('close')}
          </button>
        </div>
      </div>
    </AppDialog>
  );
}
