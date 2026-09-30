// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { useEffect, useRef } from 'react';
import { useTranslations } from 'next-intl';
import { Sparkles } from 'lucide-react';
import { MessageBubble } from './MessageBubble';
import type { ChatMessage } from './types';

interface Props {
  messages: ChatMessage[];
  onSuggestionClick: (text: string) => void;
  sessionId?: string | null;
  mode?: 'quick' | 'deep';
}

export function MessageList({ messages, onSuggestionClick, sessionId, mode = 'quick' }: Props) {
  const t = useTranslations('chat');
  const endRef = useRef<HTMLDivElement>(null);
  const suggestions = t.raw('suggestions') as string[];

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center px-6 text-center">
        <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-secondary">
          <Sparkles size={20} className="text-secondary-foreground" />
        </div>
        <h2 className="text-base font-semibold">
          {mode === 'deep' ? t('biInsight') : t('quickChat')}
        </h2>
        <p className="mt-1 text-sm text-muted-foreground max-w-md">
          {mode === 'deep' ? t('emptyDeep') : t('emptyQuick')}
        </p>

        <div className="mt-8 grid grid-cols-1 gap-2 sm:grid-cols-2 max-w-2xl w-full">
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => onSuggestionClick(s)}
              className="rounded-md border border-border bg-card px-3 py-2.5 text-left text-sm hover:bg-accent transition-colors"
            >
              {s}
            </button>
          ))}
        </div>
      </div>
    );
  }

  // 턴별 후속질문 칩(§55): 마지막 assistant 메시지의 suggestions 만 렌더
  // (이전 턴 칩은 숨김 — 대화가 진행되면 더 이상 유효하지 않을 수 있음).
  const last = messages[messages.length - 1];
  const followUps =
    last?.role === 'assistant' && !last.pending && last.suggestions?.length
      ? last.suggestions
      : null;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl">
        {messages.map((m, i) => (
          <MessageBubble
            key={m.id}
            message={m}
            sessionId={sessionId}
            // PlanCard [진행] — 마지막 assistant 메시지의 plan 에만 활성(§57).
            onPlanProceed={
              i === messages.length - 1 && m.plan && !m.pending
                ? () => onSuggestionClick(t('proceedMessage'))
                : undefined
            }
          />
        ))}
        {followUps && (
          <div className="flex flex-wrap items-center gap-2 px-4 pb-5 pt-1 pl-12">
            <span className="text-[11px] font-medium text-muted-foreground/70 mr-0.5">
              {t('relatedQuestions')}
            </span>
            {followUps.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => onSuggestionClick(s)}
                className="rounded-full border border-primary/30 bg-primary/5 px-3 py-1.5 text-xs text-primary hover:bg-primary/10 transition-colors"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        {/* 스크롤 영역 바닥 여백 — 마지막 칩/버블이 입력창 상단 테두리에 붙지 않게. */}
        <div ref={endRef} className="h-3" />
      </div>
    </div>
  );
}
