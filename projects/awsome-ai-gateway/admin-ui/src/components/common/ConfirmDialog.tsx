'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import * as Dialog from '@radix-ui/react-dialog';
import { useTranslations } from 'next-intl';
import { X } from 'lucide-react';
import { Button } from '@/components/common/Button';

interface ConfirmDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: string;
  confirmLabel?: string;
  isDestructive?: boolean;
}

export function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel,
  isDestructive = false,
}: ConfirmDialogProps) {
  const t = useTranslations('common');
  const resolvedConfirmLabel = confirmLabel ?? t('confirm');

  const handleConfirm = () => {
    onConfirm();
    onClose();
  };

  return (
    <Dialog.Root open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        {/* Overlay */}
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/50 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0" />

        {/* Content */}
        <Dialog.Content
          className="fixed left-1/2 top-1/2 z-50 -translate-x-1/2 -translate-y-1/2 w-full max-w-md rounded-lg bg-background border border-border shadow-xl p-6 focus:outline-none data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95"
          aria-describedby="confirm-dialog-description"
        >
          {/* Close button */}
          <Dialog.Close
            className="absolute right-4 top-4 rounded-sm opacity-70 hover:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring transition-opacity"
            aria-label={t('close')}
          >
            <X size={16} aria-hidden="true" />
          </Dialog.Close>

          {/* Title */}
          <Dialog.Title className="text-lg font-semibold text-foreground pr-6">
            {title}
          </Dialog.Title>

          {/* Message */}
          <p
            id="confirm-dialog-description"
            className="mt-3 text-sm text-muted-foreground leading-relaxed"
          >
            {message}
          </p>

          {/* Actions */}
          <div className="mt-6 flex items-center justify-end gap-3">
            <Button variant="ghost" onClick={onClose}>
              {t('cancel')}
            </Button>
            <Button variant={isDestructive ? 'danger' : 'primary'} onClick={handleConfirm}>
              {resolvedConfirmLabel}
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}