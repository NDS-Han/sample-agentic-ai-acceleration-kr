// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { Metadata } from 'next';
import localFont from 'next/font/local';
import { GeistMono } from 'geist/font/mono';
import { cookies } from 'next/headers';
import { getMessages } from 'next-intl/server';
import { NextIntlClientProvider } from 'next-intl';
import '@/app/globals.css';

// 본문/UI 기본 — Pretendard Variable(한/영 메트릭 호환, self-host). 시스템 기본
// 폰트를 쓰던 것을 고정해 OS 별 자형 차이로 '짜임새'가 깨지던 근본 원인 해소(§60).
const pretendard = localFont({
  src: './fonts/PretendardVariable.woff2',
  variable: '--font-sans',
  display: 'swap',
  weight: '45 920', // variable axis 범위
});
import { parseJWT, isSessionExpired } from '@/lib/auth';
import { resolveLocale } from '@/i18n/locale';
import { Sidebar } from '@/components/layout/Sidebar';
import { Header } from '@/components/layout/Header';
import { ToastProvider } from '@/components/common/ToastProvider';
import { ThemeProvider } from '@/components/common/ThemeProvider';
import { ChatShell } from '@/components/chat/ChatShell';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import { reportingTimezone } from '@/lib/utils/period';
import type { AdminSession } from '@/types/entities';

export const metadata: Metadata = {
  title: 'AWSome AI Gateway Admin',
  description: 'AWSome AI Gateway 관리자 대시보드 — API 키, 예산, 모델, 사용량 분석 통합 관리',
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Read JWT cookie — session may be null if unauthenticated (middleware handles redirect)
  const cookieStore = cookies();
  const token = cookieStore.get('admin_jwt')?.value;

  let session: AdminSession | null = null;
  if (token) {
    try {
      const parsed = parseJWT(token);
      // 만료된 쿠키도 파싱은 성공한다. '/login'·'/403' 은 public 이라 middleware 가
      // 만료 검사 없이 통과시키므로, 여기서 만료를 세션 무효로 처리하지 않으면
      // 만료된 쿠키 위에 Sidebar+Header 가 그려진다.
      if (!isSessionExpired(parsed)) {
        session = parsed;
      }
    } catch {
      // Malformed token — middleware will redirect to login
    }
  }

  const messages = await getMessages();
  // request.ts 와 같은 규칙 — 쿠키 원시값을 그대로 쓰면 lang="fr" + ko 메시지 조합이 된다.
  const locale = resolveLocale(cookieStore.get('locale')?.value);

  // BI Insight(admin-chat-agent, AgentCore Runtime)는 별도 배포다 — NDS-02로
  // 배포되지 않은 환경에서는 사이드바 메뉴·퀵챗 패널·/chat 페이지를 전부 숨긴다.
  // 값은 adminUi.env.CHAT_ENABLED (NDS-02가 values에 주입) — 런타임 env라
  // 이미지 재빌드 없이 helm upgrade 만으로 켜고 끌 수 있다.
  const chatDeployed = process.env.CHAT_ENABLED === 'true';

  return (
    <html
      lang={locale}
      suppressHydrationWarning
      className={`${pretendard.variable} ${GeistMono.variable}`}
    >
      <body>
        <ThemeProvider>
          <NextIntlClientProvider messages={messages} locale={locale}>
            {/* 리포팅 TZ — 서버 env(REPORTING_TIMEZONE, 백엔드 집계와 동일 값)를
                클라이언트 트리로. 월 경계 계산이 모든 클라이언트에서 같은 TZ 를 쓰게 한다. */}
            <ReportingTimezoneProvider tz={reportingTimezone()}>
            <ToastProvider>
              <div className="flex h-screen bg-background">
                <Sidebar role={session?.role} chatEnabled={chatDeployed} />
                {/* ChatShell: 본문과 퀵챗 패널을 flex 형제로 배치(분할뷰). 채팅
                    열리면 본문이 자동으로 좁아짐(overlay 아님). enabled=ADMIN +
                    BI Insight 배포된 환경에서만 채팅 UI 노출 — Provider 는 항상
                    감싸 페이지 hook 안전성 유지. */}
                <ChatShell enabled={session?.role === 'ADMIN' && chatDeployed}>
                  <div className="flex flex-col flex-1 overflow-hidden">
                    <Header session={session} />
                    <main className="aurora-bg flex-1 overflow-auto p-6">
                      {children}
                    </main>
                  </div>
                </ChatShell>
              </div>
            </ToastProvider>
            </ReportingTimezoneProvider>
          </NextIntlClientProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
