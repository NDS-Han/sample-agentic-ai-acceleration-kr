// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { ChatLayout } from '@/components/chat/ChatLayout';
import { Sparkles } from 'lucide-react';

export const metadata = {
  title: 'BI Insight — AWSome AI Gateway Admin',
};

export default async function ChatPage() {
  // BI Insight(admin-chat-agent)는 별도 배포(NDS-02)다 — 미배포 환경에서는
  // 메뉴를 숨기지만 직접 URL 접근이 남으므로 페이지도 안내 화면으로 대체한다.
  if (process.env.CHAT_ENABLED !== 'true') {
    const t = await getTranslations('chat');
    return (
      <div className="flex h-full items-center justify-center">
        <div className="max-w-md rounded-apple border border-border/60 bg-muted/20 p-8 text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10 text-primary">
            <Sparkles size={22} />
          </div>
          <h1 className="text-base font-semibold">{t('unavailableTitle')}</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {t('unavailableDesc')}
          </p>
        </div>
      </div>
    );
  }
  // 사이드바 Chat = 심층 분석 모드(§55): plan-first + 항상 검증 + insight-first.
  // 퀵챗(드로어, ChatShell variant="drawer")은 quick 기본값 그대로.
  return <ChatLayout mode="deep" />;
}
