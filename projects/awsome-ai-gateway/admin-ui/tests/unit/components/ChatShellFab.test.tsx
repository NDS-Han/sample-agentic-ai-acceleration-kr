// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * ChatShell — 우하단 FAB 표시 여부를 셸 루트 data-chat-fab 로 노출한다 (P2-6).
 * layout 의 <main>(group-data-[chat-fab]/shell:pb-24)과 토스트 스택이 이 속성으로
 * FAB 가 있을 때만 비켜 간다. /chat·채팅 비활성·패널 열림이면 속성이 없어야 한다.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ChatShell } from '@/components/chat/ChatShell';

let pathname = '/';
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => pathname,
}));
vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock('@/components/chat/ChatLayout', () => ({
  ChatLayout: () => <div data-testid="chat-layout" />,
}));

function shellRoot(container: HTMLElement) {
  return container.querySelector('.group\\/shell') as HTMLElement;
}

describe('ChatShell — data-chat-fab', () => {
  beforeEach(() => {
    pathname = '/';
    // jsdom 에는 matchMedia 가 없다 — 넓은 화면(분할뷰)으로 고정.
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia;
  });

  it('FAB 가 보이면 data-chat-fab 가 있고, 패널을 열면 사라진다', () => {
    const { container } = render(<ChatShell enabled>page</ChatShell>);
    expect(shellRoot(container).hasAttribute('data-chat-fab')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'shell.openQuickChat' }));
    expect(shellRoot(container).hasAttribute('data-chat-fab')).toBe(false);
  });

  it('/chat 페이지에서는 FAB·속성이 없다', () => {
    pathname = '/chat';
    const { container } = render(<ChatShell enabled>page</ChatShell>);
    expect(screen.queryByRole('button', { name: 'shell.openQuickChat' })).toBeNull();
    expect(shellRoot(container).hasAttribute('data-chat-fab')).toBe(false);
  });

  it('채팅 비활성(CHAT_ENABLED=false·비-ADMIN)이면 속성이 없다', () => {
    const { container } = render(<ChatShell enabled={false}>page</ChatShell>);
    expect(screen.queryByRole('button', { name: 'shell.openQuickChat' })).toBeNull();
    expect(shellRoot(container).hasAttribute('data-chat-fab')).toBe(false);
  });
});
