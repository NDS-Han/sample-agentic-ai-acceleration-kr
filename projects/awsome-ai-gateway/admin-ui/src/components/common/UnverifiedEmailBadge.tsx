// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { ReactNode } from 'react';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/common/Badge';
import { InfoTooltip } from '@/components/common/InfoTooltip';
import { isSyntheticEmail } from '@/lib/userLabel';

/**
 * 합성 이메일(`<sub>@unknown`) 자리에 uuid 대신 보여주는 "이메일 미인증" 배지.
 *
 * 툴팁은 기본적으로 title 어트리뷰트(BudgetSummaryTable 의 zeroBudgetHint 와 같은
 * 방식) — 링크·버튼·검색 결과 option 안에서도 쓸 수 있다. 상호작용 요소 밖(상세
 * 패널 등)에서는 `info` 로 InfoTooltip 아이콘을 덧붙여 키보드 포커스로도 열리게 한다.
 * InfoTooltip 은 <button> 이라 링크/버튼 안에서는 info 를 켜지 않는다.
 */
export function UnverifiedEmailBadge({ info = false }: { info?: boolean }) {
  const t = useTranslations('common');
  const badge = (
    <Badge tone="amber" className="whitespace-nowrap">
      <span title={t('unverifiedEmailHint')}>{t('unverifiedEmail')}</span>
    </Badge>
  );
  if (!info) return badge;
  return (
    <span className="inline-flex items-center gap-1">
      {badge}
      <InfoTooltip label={t('unverifiedEmail')}>{t('unverifiedEmailHint')}</InfoTooltip>
    </span>
  );
}

/**
 * 이메일 표시 — 합성 이메일이면 배지, 아니면 이메일 문자열, 없으면 fallback.
 */
export function EmailText({
  email,
  fallback = null,
  info = false,
}: {
  email: string | null | undefined;
  fallback?: ReactNode;
  info?: boolean;
}) {
  if (isSyntheticEmail(email)) return <UnverifiedEmailBadge info={info} />;
  return <>{email || fallback}</>;
}
