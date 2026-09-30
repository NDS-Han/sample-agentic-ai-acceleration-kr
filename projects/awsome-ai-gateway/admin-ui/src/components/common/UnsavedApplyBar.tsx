'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useTranslations } from 'next-intl';
import { SpinnerButton } from './SpinnerButton';

interface UnsavedApplyBarProps {
  isPending: boolean;
  disabled?: boolean;
  onApply: () => void;
}

/**
 * 미저장 변경 플로팅 Apply 바 — dirty 일 때만 렌더된다.
 *
 * UserPanel/TeamPanel 과 단독 패널(ScopeAppAccessPanel 등)의 두 가지
 * "미저장" UX(인라인 버튼 vs 플로팅 바)를 이 컴포넌트 하나로 통일한다.
 * 스크롤 위치와 무관하게 항상 보이는 것이 핵심 — 변경 순간 나타나므로
 * 발견 가능성도 자연히 해결된다.
 */
export function UnsavedApplyBar({ isPending, disabled, onApply }: UnsavedApplyBarProps) {
  const t = useTranslations('common');
  return (
    <>
      {/* 플로팅 바 높이만큼 스페이서 — 스크롤 끝 콘텐츠가 가려지지 않게. */}
      <div className="h-16" aria-hidden="true" />
      <div className="fixed bottom-6 left-1/2 z-40 -translate-x-1/2">
        <div className="flex items-center gap-3 rounded-full border border-border bg-card/95 px-5 py-2.5 shadow-lg backdrop-blur">
          <span className="text-xs font-medium text-muted-foreground whitespace-nowrap">
            {t('unsavedChanges')}
          </span>
          <SpinnerButton
            type="button"
            isLoading={isPending}
            disabled={disabled || isPending}
            onClick={onApply}
          >
            {t('apply')}
          </SpinnerButton>
        </div>
      </div>
    </>
  );
}
