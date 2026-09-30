'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useState, type ReactNode } from 'react';

interface PolicySectionProps {
  title: string;
  /** 헤더 배지 — 상태(무제한/N개)·출처·수정됨·실패 마커. */
  badges?: ReactNode;
  /** dirty/실패가 true 로 바뀌면 자동으로 펼친다. 닫기는 허용 — 마커는 헤더에 남는다. */
  autoOpen?: boolean;
  /** 첫 렌더부터 열린 상태로 시작. */
  defaultOpen?: boolean;
  /**
   * true 면 최초 펼침까지 children 을 마운트하지 않는다(폴링·차트 같은 무거운
   * 콘텐츠용). 단, 이 모드에서는 헤더 배지 데이터를 자식이 아니라 부모가
   * 따로 조회해 넣어야 한다 — 마운트되지 않은 자식은 fetch 도 하지 않는다.
   */
  lazyMount?: boolean;
  children: ReactNode;
}

/**
 * 정책 섹션 접이식 컨테이너 — 헤더에 상태 배지를 얹어 "펼치지 않아도 읽히는"
 * 섹션을 만든다.
 *
 * 콘텐츠는 최초 펼침 시 lazy mount 되고 이후에는 unmount 되지 않는다 —
 * 접었다 펴도 섹션 안의 미저장 편집이 소실되지 않는다.
 */
export function PolicySection({
  title,
  badges,
  autoOpen,
  defaultOpen = false,
  lazyMount = false,
  children,
}: PolicySectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  // 한번 열리면 마운트 유지 — dirty 상태가 접힘으로 사라지는 것을 막는다.
  // lazyMount=false(기본)는 eager mount — 헤더 배지가 자식의 fetch 결과에
  // 의존하므로, 접힌 채로도 데이터를 채워야 한다.
  const [mounted, setMounted] = useState(defaultOpen || !lazyMount);

  useEffect(() => {
    if (autoOpen) setOpen(true);
  }, [autoOpen]);

  return (
    <details
      open={open}
      onToggle={(e) => {
        const next = e.currentTarget.open;
        setOpen(next);
        if (next) setMounted(true);
      }}
      className="border rounded-apple-md p-3 mb-3 group"
    >
      <summary className="flex items-center justify-between gap-2 cursor-pointer list-none text-sm font-medium [&::-webkit-details-marker]:hidden">
        <span className="flex items-center gap-2 min-w-0 flex-wrap">
          <span className="truncate">{title}</span>
          {badges}
        </span>
        <span
          aria-hidden="true"
          className="text-muted-foreground transition-transform group-open:rotate-180 flex-shrink-0"
        >
          ▾
        </span>
      </summary>
      {mounted && <div className="mt-2">{children}</div>}
    </details>
  );
}
