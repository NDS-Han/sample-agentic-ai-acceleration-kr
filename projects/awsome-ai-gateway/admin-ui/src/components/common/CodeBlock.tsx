'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Check, Copy } from 'lucide-react';

interface CodeBlockProps {
  /** 서버에서 shiki 로 하이라이팅된 HTML — dangerouslySetInnerHTML 로 렌더. */
  html: string;
  /** 복사용 원문 */
  text: string;
  /** 헤더 표시용 파일명 */
  filename: string;
}

/**
 * 문서 스타일 코드 블록 — 파일명 헤더 + 복사 버튼 + shiki 하이라이트.
 * 하이라이트 HTML 은 서버 렌더 결과물이고 copy 만 클라이언트에서 동작한다.
 */
export function CodeBlock({ html, text, filename }: CodeBlockProps) {
  const t = useTranslations('integration');
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
    <div className="overflow-hidden rounded-lg border border-border">
      <div className="flex items-center justify-between border-b border-border bg-muted px-3 py-1.5">
        <span className="font-mono text-[11px] text-muted-foreground">{filename}</span>
        <button
          type="button"
          onClick={handleCopy}
          aria-label={t('copy')}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? t('copied') : t('copy')}
        </button>
      </div>
      <div
        className="codeblock-body text-xs"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}
