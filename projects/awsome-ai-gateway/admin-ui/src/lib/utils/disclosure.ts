// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 펼침 행·접힘 패널의 공용 클릭 대상 — "행/헤더 전체가 토글, 셰브런은 표시".
 *
 *   const d = disclosureProps({ expanded, onToggle, controlsId });
 *   <tr {...d.target} className={cn(d.target.className, '...')}>
 *     <td><button {...d.chevron} aria-label=...>{chevron}</button></td>
 *     <td>... <button onClick={edit}>Edit</button> ...</td>      ← 토글 안 됨
 *   </tr>
 *   {expanded && <tr id={controlsId}>...</tr>}
 *
 * 규칙
 *  - 행/헤더를 누르면 토글. 안쪽의 버튼·링크·입력·select·체크박스·라벨·[role=button…]·
 *    [data-no-toggle] 에서 시작한 클릭은 토글하지 않는다(그 요소 자신의 동작만).
 *  - 텍스트를 드래그로 선택했으면(선택 영역이 그 행 안) 토글하지 않는다 — 복사하려는 것.
 *  - 포털(행 버튼이 연 다이얼로그 등)에서 버블된 React 이벤트는 무시한다(DOM 상 행 밖).
 *  - 키보드: 행/헤더가 포커스를 받고 Enter/Space 로 토글. aria-expanded·aria-controls,
 *    포커스 링, cursor-pointer, hover 틴트는 이 target 에만 붙는다(공용 Tr 은 기본 hover 가
 *    없다 — 아무 일도 안 하는 행이 클릭할 수 있어 보이지 않게).
 *  - 셰브런은 시각 표시 겸 보조기기용 버튼으로 남되 탭 순서에서는 빠지고(행이 이미 포커스
 *    대상), 자기 클릭을 행으로 올리지 않아 두 번 토글되지 않는다.
 */

import type { KeyboardEvent, MouseEvent } from 'react';

/** 행 토글을 하지 않는 안쪽 요소 — 자기 동작이 있는 것들. */
export const INTERACTIVE_SELECTOR = [
  'a[href]',
  'button',
  'input',
  'select',
  'textarea',
  'label',
  'summary',
  '[contenteditable=""]',
  '[contenteditable="true"]',
  '[role="button"]',
  '[role="link"]',
  '[role="checkbox"]',
  '[role="switch"]',
  '[role="menuitem"]',
  '[role="option"]',
  '[role="tab"]',
  '[role="combobox"]',
  '[data-no-toggle]',
].join(',');

/** 클릭이 target(행) 자신이 아니라 그 안의 상호작용 요소에서 시작했는가. */
export function fromInteractiveChild(target: EventTarget | null, container: Element): boolean {
  if (!(target instanceof Element)) return false;
  const hit = target.closest(INTERACTIVE_SELECTOR);
  return !!hit && hit !== container && container.contains(hit);
}

/** container 안에 비어 있지 않은 텍스트 선택이 있는가(드래그로 복사 중). */
export function hasTextSelectionIn(container: Element): boolean {
  if (typeof window === 'undefined' || !window.getSelection) return false;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.toString().trim() === '') return false;
  const node = sel.anchorNode;
  return !!node && container.contains(node.nodeType === 1 ? node : node.parentNode);
}

export interface DisclosureOptions {
  expanded: boolean;
  onToggle: () => void;
  /** 펼쳐지는 영역의 id(aria-controls). 펼쳐진 동안만 DOM 에 있어도 된다. */
  controlsId?: string;
  /** 토글을 끈다(행 클릭·키보드 모두). 펼칠 내용이 없는 행 등. */
  disabled?: boolean;
}

/** 클릭 대상 행/헤더에 붙일 포커스 링·커서 — 표 행·카드 헤더 공통. */
export const DISCLOSURE_TARGET_CLASS =
  'cursor-pointer hover:bg-[--table-row-hover] outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring';

export function disclosureProps({ expanded, onToggle, controlsId, disabled }: DisclosureOptions) {
  const target = disabled
    ? { 'aria-expanded': expanded, 'aria-controls': controlsId }
    : {
        tabIndex: 0,
        'aria-expanded': expanded,
        'aria-controls': controlsId,
        'data-disclosure': expanded ? 'open' : 'closed',
        className: DISCLOSURE_TARGET_CLASS,
        onClick: (e: MouseEvent<HTMLElement>) => {
          const el = e.currentTarget;
          // 포털(다이얼로그·팝오버)에서 React 트리로 버블된 클릭 — DOM 상 이 행 밖.
          if (!(e.target instanceof Node) || !el.contains(e.target)) return;
          if (e.defaultPrevented) return;
          if (fromInteractiveChild(e.target, el)) return;
          if (hasTextSelectionIn(el)) return;
          onToggle();
        },
        onKeyDown: (e: KeyboardEvent<HTMLElement>) => {
          // 안쪽 입력·버튼의 Enter/Space 는 그 요소 몫.
          if (e.target !== e.currentTarget) return;
          if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
            e.preventDefault();
            onToggle();
          }
        },
      };
  const chevron = {
    type: 'button' as const,
    tabIndex: disabled ? undefined : -1,
    'aria-expanded': expanded,
    'aria-controls': controlsId,
    disabled,
    onClick: (e: MouseEvent<HTMLElement>) => {
      e.stopPropagation();
      onToggle();
    },
  };
  return { target, chevron };
}
