'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import * as Dialog from '@radix-ui/react-dialog';
import { useTranslations } from 'next-intl';
import { X } from 'lucide-react';

interface AppDialogProps {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  /** 접근성 설명 — 생략 시 title 만 스크린리더에 노출. */
  description?: string;
  /** 폼/결과 등 임의 콘텐츠 — 스크롤은 내부에서 처리. */
  children: React.ReactNode;
  /** 와이드 폼 모달용 (기본 max-w-md). */
  wide?: boolean;
  /** 콘텐츠 컨테이너 추가 클래스 — 4xl 등 더 넓은 모달용. */
  contentClassName?: string;
}

/**
 * 임의 콘텐츠용 공용 모달 — ConfirmDialog 는 확인/취소 두 버튼 고정이라
 * 폼·결과 모달엔 못 쓴다. 수제 `fixed inset-0` 모달은 focus trap · Escape ·
 * aria-modal 이 없어 키보드/스크린리더에서 탈출 불가하므로 Radix 로 통일.
 */
export function AppDialog({
  isOpen,
  onClose,
  title,
  description,
  children,
  wide = false,
  contentClassName,
}: AppDialogProps) {
  const t = useTranslations('common');

  return (
    <Dialog.Root open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/50 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />
        <Dialog.Content
          className={[
            'fixed left-1/2 top-1/2 z-50 -translate-x-1/2 -translate-y-1/2 w-full rounded-lg bg-background border border-border shadow-xl p-6 focus:outline-none max-h-[90vh] overflow-y-auto data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
            wide ? 'max-w-3xl' : 'max-w-md',
            contentClassName ?? '',
          ].join(' ')}
        >
          <Dialog.Close
            className="absolute right-4 top-4 rounded-sm opacity-70 hover:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring transition-opacity"
            aria-label={t('close')}
          >
            <X size={16} aria-hidden="true" />
          </Dialog.Close>

          <Dialog.Title className="text-lg font-semibold text-foreground pr-6">
            {title}
          </Dialog.Title>
          {description ? (
            <Dialog.Description className="mt-1 text-sm text-muted-foreground">
              {description}
            </Dialog.Description>
          ) : (
            // Description 이 없으면 Radix 경고 — sr-only 로 Title 을 설명 대신 제공.
            <Dialog.Description className="sr-only">{title}</Dialog.Description>
          )}

          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
