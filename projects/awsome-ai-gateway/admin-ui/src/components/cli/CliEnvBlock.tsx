'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Check, Copy } from 'lucide-react';

/**
 * /cli 사전 준비 env 블록 — 텍스트는 서버가 실값(배포 env)으로 채워 넘긴다.
 * 복사 버튼은 클라이언트에서만 동작하므로 이 컴포넌트만 'use client'.
 */
export function CliEnvBlock({ text }: { text: string }) {
  const t = useTranslations('cli');
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="relative">
      <pre className="bg-muted rounded p-3 pr-10 text-xs overflow-x-auto">{text}</pre>
      <button
        type="button"
        onClick={handleCopy}
        aria-label={t('copyEnv')}
        className="absolute right-2 top-2 inline-flex items-center gap-1 rounded px-1.5 py-1 text-[11px] text-muted-foreground hover:bg-background hover:text-foreground transition-colors"
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
        {copied ? t('copied') : t('copyEnv')}
      </button>
    </div>
  );
}
