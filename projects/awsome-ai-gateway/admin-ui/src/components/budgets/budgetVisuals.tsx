'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 예산 표 표현 공용 — BudgetSummaryTable(관리자)과 TeamAllocationView(팀 리더)가
 * 같은 행 구성(타입 뱃지·사용률 게이지·경보 뱃지)을 쓰도록 추출했다.
 */

import { Badge, type BadgeTone } from '@/components/common/Badge';
import { AlertLevel, BudgetScope } from '@/types/enums';
import { fmtUsd } from '@/lib/utils/format';
import { useTranslations } from 'next-intl';
import { alertLevelOf, type AlertLevelValue } from '@/lib/utils/alertLevel';

type BudgetScopeValue = (typeof BudgetScope)[keyof typeof BudgetScope];

export { alertLevelOf };

export function AlertBadge({ level, labels }: { level: AlertLevelValue | string; labels: Record<string, string> }) {
  const tones: Record<string, BadgeTone> = {
    [AlertLevel.NORMAL]: 'teal',
    [AlertLevel.WARNING]: 'amber',
    [AlertLevel.CRITICAL]: 'pink',
  };
  return <Badge tone={tones[level] ?? 'neutral'}>{labels[level] ?? level}</Badge>;
}

export function TypeBadge({ type, labels }: { type: BudgetScopeValue | string; labels: Record<string, string> }) {
  return <Badge tone={type === BudgetScope.TEAM ? 'sky' : 'neutral'}>{labels[type] ?? type}</Badge>;
}

/** 멤버 행의 계정 역할 뱃지 — ADMIN/TEAM_LEADER/DEVELOPER 를 색으로 구분한다. */
export function RoleBadge({ role, labels }: { role: string | null | undefined; labels: Record<string, string> }) {
  const tones: Record<string, BadgeTone> = {
    ADMIN: 'pink',
    TEAM_LEADER: 'sky',
    DEVELOPER: 'neutral',
  };
  const key = role ?? 'DEVELOPER';
  return <Badge tone={tones[key] ?? 'neutral'}>{labels[key] ?? key}</Badge>;
}

export function UsageBar({ pct, level, label }: { pct: number; level: AlertLevelValue | string; label?: string }) {
  // 임계 기반 시맨틱색(테마 토큰 — 다크/라이트 자동): 정상 teal / 경고 amber / 위험 destructive.
  const colorMap: Record<string, string> = {
    [AlertLevel.NORMAL]: 'hsl(var(--chart-1))',
    [AlertLevel.WARNING]: 'hsl(38 92% 50%)',
    [AlertLevel.CRITICAL]: 'hsl(var(--destructive))',
  };
  const color = colorMap[level] ?? 'hsl(var(--muted-foreground))';
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.min(Math.round(pct), 100)}
      aria-label={label}
      className="w-full h-1.5 rounded-full overflow-hidden bg-[--table-progress-track]"
    >
      <div
        className="h-full rounded-full"
        style={{ width: `${Math.min(pct, 100)}%`, background: color }}
      />
    </div>
  );
}

/** 라벨 + "사용 / 한도" + 게이지 한 줄 — 요약 카드(사용자 상세·유효 정책)에서
    예산 상태를 표 형태가 아니라 게이지로 보여줄 때 쓴다. */
export function BudgetGaugeRow({
  label,
  max,
  used,
  unsetLabel,
}: {
  label: string;
  max: string | number | null;
  used: string | number | null;
  unsetLabel: string;
}) {
  const t = useTranslations('budgets');
  const maxN = max != null ? Number(max) : null;
  const usedN = used != null ? Number(used) : 0;
  const pct = maxN != null && maxN > 0 ? (usedN / maxN) * 100 : null;
  const level = pct != null ? alertLevelOf(pct) : null;
  const levelLabel = level != null ? t(`alertLevels.${level}`) : null;
  return (
    <div>
      <div className="flex items-center justify-between text-xs mb-1">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium tabular-nums">
          {maxN != null ? (
            `${fmtUsd(usedN)} / ${fmtUsd(maxN)}`
          ) : (
            <span className="text-muted-foreground italic font-normal">{unsetLabel}</span>
          )}
        </span>
      </div>
      {pct != null && level != null && (
        <UsageBar
          pct={pct}
          level={level}
          label={`${label} ${Math.round(pct)}% · ${levelLabel}`}
        />
      )}
    </div>
  );
}
