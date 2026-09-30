'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useTranslations } from 'next-intl';
import { RotateCcw } from 'lucide-react';
import { SpinnerButton } from './SpinnerButton';

/** dirty 섹션 1개 — 이름 + 개별 되돌리기. */
export interface ApplySectionItem {
  key: string;
  label: string;
  onRevert?: () => void;
}

interface UnsavedApplyBarProps {
  isPending: boolean;
  disabled?: boolean;
  onApply: () => void;
  /** dirty 섹션 목록 — 있으면 이름을 표시하고 섹션별 되돌리기를 제공한다. */
  items?: ApplySectionItem[];
}

/**
 * 미저장 변경 플로팅 Apply 바 — dirty 일 때만 렌더된다.
 *
 * UserPanel/TeamPanel 과 단독 패널(ScopeAppAccessPanel 등)의 두 가지
 * "미저장" UX(인라인 버튼 vs 플로팅 바)를 이 컴포넌트 하나로 통일한다.
 * 스크롤 위치와 무관하게 항상 보이는 것이 핵심 — 변경 순간 나타나므로
 * 발견 가능성도 자연히 해결된다.
 */
export function UnsavedApplyBar({ isPending, disabled, onApply, items }: UnsavedApplyBarProps) {
  const t = useTranslations('common');
  return (
    <>
      {/* 플로팅 바 높이만큼 스페이서 — 스크롤 끝 콘텐츠가 가려지지 않게. */}
      <div className="h-16" aria-hidden="true" />
      <div className="fixed bottom-6 left-1/2 z-40 -translate-x-1/2">
        <div className="flex flex-col gap-1 rounded-2xl border border-border bg-card/95 px-5 py-2.5 shadow-lg backdrop-blur">
          <div className="flex items-center gap-3">
            <span className="text-xs font-medium text-muted-foreground whitespace-nowrap">
              {t('unsavedChanges')}
            </span>
            {items && items.length > 0 && (
              <span className="flex items-center gap-1.5">
                {items.map((item) => (
                  <span
                    key={item.key}
                    className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-xs whitespace-nowrap"
                  >
                    {item.label}
                    {item.onRevert && (
                      <button
                        type="button"
                        onClick={item.onRevert}
                        disabled={isPending}
                        aria-label={`${t('revert')} ${item.label}`}
                        className="text-muted-foreground hover:text-foreground disabled:opacity-40"
                      >
                        <RotateCcw size={11} aria-hidden="true" />
                      </button>
                    )}
                  </span>
                ))}
              </span>
            )}
            <SpinnerButton
              type="button"
              isLoading={isPending}
              disabled={disabled || isPending}
              onClick={onApply}
            >
              {t('apply')}
            </SpinnerButton>
          </div>
          {/* 복수 섹션 저장은 원자적이지 않다 — 앞 섹션 성공·뒤 섹션 실패 시
              성공분은 저장된 채 남는다는 것을 명시한다. */}
          {items && items.length > 1 && (
            <p className="text-[10px] leading-tight text-muted-foreground">
              {t('applyPartialNote')}
            </p>
          )}
        </div>
      </div>
    </>
  );
}
