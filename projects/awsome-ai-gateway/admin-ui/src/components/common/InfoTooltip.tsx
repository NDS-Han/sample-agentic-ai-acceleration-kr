'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Info } from 'lucide-react';

/** 뷰포트 가장자리와 띄울 간격(px). */
const EDGE = 8;
/** 트리거와 팝오버 사이 간격(px) — 종전 mt-1.5/mb-1.5. */
const GAP = 6;
/** w-64. 뷰포트가 이보다 좁으면 뷰포트 폭 - 2·EDGE 로 줄인다. */
const WIDTH = 256;

export interface TooltipPlacementInput {
  /** 트리거의 뷰포트 좌표(getBoundingClientRect). */
  trigger: { left: number; right: number; top: number; bottom: number };
  /** 팝오버 높이(px) — 위/아래 뒤집기 판단용. */
  height: number;
  viewport: { width: number; height: number };
  align: 'left' | 'center';
  side: 'top' | 'bottom';
}

/**
 * 팝오버 위치(position: fixed 좌표) — 뷰포트 안에 머문다.
 *  - 가로: 기본 앵커(좌측 또는 중앙)에서 시작해 [EDGE, 뷰포트 폭 - 폭 - EDGE] 로 민다.
 *    오른쪽 끝 트리거는 자연히 오른쪽 정렬이 된다.
 *  - 세로: 원하는 쪽에 자리가 없고 반대쪽이 더 넓으면 뒤집는다.
 */
export function placeTooltip({ trigger, height, viewport, align, side }: TooltipPlacementInput) {
  const width = Math.min(WIDTH, Math.max(0, viewport.width - 2 * EDGE));
  const preferred = align === 'center' ? (trigger.left + trigger.right) / 2 - width / 2 : trigger.left;
  const left = Math.max(EDGE, Math.min(preferred, viewport.width - width - EDGE));
  const below = viewport.height - trigger.bottom - GAP - EDGE;
  const above = trigger.top - GAP - EDGE;
  let placed: 'top' | 'bottom' = side;
  if (side === 'bottom' && height > below && above > below) placed = 'top';
  if (side === 'top' && height > above && below > above) placed = 'bottom';
  const top = placed === 'bottom' ? trigger.bottom + GAP : trigger.top - GAP - height;
  return { left, top: Math.max(EDGE, top), width, side: placed };
}

/**
 * ? / ⓘ 아이콘 + 팝오버 — 테이블 헤더·KPI 카드 등에서 용어를 즉석 설명한다.
 * title 어트리뷰트는 표시가 지연되고 키보드 포커스에서 열리지 않으므로 hover/focus 팝오버를 쓴다.
 *
 * 팝오버는 document.body 로 포털하고 position: fixed 로 그린다 — 닫혀 있어도 레이아웃을
 * 차지하던 absolute 팝오버가 오른쪽 끝 카드에서 main 을 가로 스크롤시키던 문제(뷰포트 밖
 * 46px)를 없애고, 열릴 때 placeTooltip 으로 뷰포트 안에 맞춘다. 내용은 닫혀 있어도 DOM 에
 * 남는다(aria-describedby·테스트).
 */
export function InfoTooltip({
  label,
  children,
  align = 'left',
  side = 'bottom',
  trigger,
}: {
  label: string;
  children: React.ReactNode;
  /** 선호 가로 앵커 — 뷰포트에 안 맞으면 placeTooltip 이 안쪽으로 민다. */
  align?: 'left' | 'center';
  /** 선호 방향 — 아이콘 바로 아래에 폼 컨트롤이 오면 'top'. 자리가 없으면 뒤집힌다. */
  side?: 'top' | 'bottom';
  /** ⓘ 아이콘 대신 보일 트리거(배지 등). 넘기면 그 요소 자체가 hover/focus 대상이 된다. */
  trigger?: React.ReactNode;
}) {
  const id = useId();
  const anchorRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLSpanElement>(null);
  const [mounted, setMounted] = useState(false);
  const [hover, setHover] = useState(false);
  const [focus, setFocus] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number; width: number } | null>(null);
  const open = hover || focus;

  useEffect(() => setMounted(true), []);

  const update = useCallback(() => {
    const a = anchorRef.current;
    const p = panelRef.current;
    if (!a || !p) return;
    const r = a.getBoundingClientRect();
    const next = placeTooltip({
      trigger: r,
      height: p.offsetHeight,
      viewport: { width: window.innerWidth, height: window.innerHeight },
      align,
      side,
    });
    setPos({ left: next.left, top: next.top, width: next.width });
  }, [align, side]);

  useLayoutEffect(() => {
    if (!open) return;
    update();
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [open, update]);

  const panel = (
    <span
      ref={panelRef}
      id={id}
      role="tooltip"
      data-state={open ? 'open' : 'closed'}
      style={{
        position: 'fixed',
        left: pos?.left ?? 0,
        top: pos?.top ?? 0,
        width: pos?.width ?? WIDTH,
        maxWidth: `calc(100vw - ${2 * EDGE}px)`,
      }}
      className={`pointer-events-none z-[60] whitespace-normal rounded-md border border-border bg-popover px-2.5 py-2 text-left text-xs font-normal leading-relaxed normal-case tracking-normal text-popover-foreground shadow-md transition-opacity duration-100 ${
        open && pos ? 'visible opacity-100' : 'invisible opacity-0'
      }`}
    >
      {children}
    </span>
  );

  return (
    <span
      ref={anchorRef}
      className="relative inline-flex align-middle"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onFocus={() => setFocus(true)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocus(false);
      }}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          setHover(false);
          setFocus(false);
        }
      }}
    >
      <button
        type="button"
        aria-label={label}
        aria-describedby={id}
        className={`${
          trigger ? 'inline-flex cursor-help' : 'text-muted-foreground/70 hover:text-muted-foreground'
        } rounded-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring`}
      >
        {trigger ?? <Info size={12} aria-hidden="true" />}
      </button>
      {/* SSR·첫 렌더는 제자리(숨김), 마운트 후 body 로 — 하이드레이션 불일치 없음. */}
      {mounted ? createPortal(panel, document.body) : panel}
    </span>
  );
}
