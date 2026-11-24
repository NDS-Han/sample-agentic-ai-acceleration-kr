// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * InfoTooltip — 뷰포트 안 배치(placeTooltip)와 포털 렌더.
 * dev 실측: Analytics 5번째 KPI 카드의 ⓘ 팝오버가 main 오른쪽으로 46px 넘쳐 가로 스크롤이 생겼다.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { InfoTooltip, placeTooltip } from '@/components/common/InfoTooltip';
import { fmtSmallRatio } from '@/lib/utils/format';

const VP = { width: 1457, height: 900 };
const at = (left: number, top = 100) => ({ left, right: left + 12, top, bottom: top + 12 });

describe('placeTooltip', () => {
  it('왼쪽 앵커 기본 — 트리거 왼쪽에 맞춘다', () => {
    const p = placeTooltip({ trigger: at(300), height: 80, viewport: VP, align: 'left', side: 'bottom' });
    expect(p).toMatchObject({ left: 300, top: 118, width: 256, side: 'bottom' });
  });

  it('오른쪽 끝 트리거 — 뷰포트 안으로 민다(오른쪽 정렬)', () => {
    const p = placeTooltip({ trigger: at(1420), height: 80, viewport: VP, align: 'left', side: 'bottom' });
    expect(p.left + p.width).toBeLessThanOrEqual(VP.width - 8);
    expect(p.left).toBe(VP.width - 256 - 8);
  });

  it('왼쪽 끝·중앙 정렬 — 음수로 나가지 않는다', () => {
    const p = placeTooltip({ trigger: at(4), height: 80, viewport: VP, align: 'center', side: 'bottom' });
    expect(p.left).toBe(8);
  });

  it('아래 자리가 없으면 위로, 위 자리가 없으면 아래로 뒤집는다', () => {
    const down = placeTooltip({ trigger: at(300, 860), height: 120, viewport: VP, align: 'left', side: 'bottom' });
    expect(down.side).toBe('top');
    expect(down.top).toBe(860 - 6 - 120);
    const up = placeTooltip({ trigger: at(300, 20), height: 120, viewport: VP, align: 'left', side: 'top' });
    expect(up.side).toBe('bottom');
  });

  it('좁은 뷰포트 — 320px 은 256 폭 그대로 안쪽으로 밀고, 240px 은 폭을 줄여 양쪽 8px 안에', () => {
    const p320 = placeTooltip({ trigger: at(290), height: 80, viewport: { width: 320, height: 640 }, align: 'left', side: 'bottom' });
    expect(p320).toMatchObject({ width: 256, left: 320 - 256 - 8 });
    const p240 = placeTooltip({ trigger: at(200), height: 80, viewport: { width: 240, height: 640 }, align: 'left', side: 'bottom' });
    expect(p240).toMatchObject({ width: 224, left: 8 });
  });
});

describe('InfoTooltip', () => {
  it('팝오버는 body 로 포털 + fixed — 트리거 컨테이너의 스크롤 폭에 기여하지 않는다', () => {
    const { container } = render(
      <div data-testid="card">
        <InfoTooltip label="About">Explanation text</InfoTooltip>
      </div>,
    );
    const tip = screen.getByRole('tooltip');
    expect(container.contains(tip)).toBe(false);
    expect(tip.style.position).toBe('fixed');
    expect(tip.getAttribute('data-state')).toBe('closed');
    expect(screen.getByRole('button', { name: 'About' }).getAttribute('aria-describedby')).toBe(tip.id);
  });

  it('hover·focus 로 열리고 Esc·blur 로 닫힌다', () => {
    render(<InfoTooltip label="About">Explanation</InfoTooltip>);
    const btn = screen.getByRole('button', { name: 'About' });
    const tip = screen.getByRole('tooltip');
    fireEvent.mouseEnter(btn.parentElement!);
    expect(tip.getAttribute('data-state')).toBe('open');
    fireEvent.mouseLeave(btn.parentElement!);
    expect(tip.getAttribute('data-state')).toBe('closed');
    fireEvent.focus(btn);
    expect(tip.getAttribute('data-state')).toBe('open');
    fireEvent.keyDown(btn, { key: 'Escape' });
    expect(tip.getAttribute('data-state')).toBe('closed');
    fireEvent.focus(btn);
    fireEvent.blur(btn);
    expect(tip.getAttribute('data-state')).toBe('closed');
  });
});

describe('fmtSmallRatio', () => {
  it.each([
    [null, '—'],
    [0, '0.00'],
    [3 / 1818, '0.0017'],
    [0.000012, '0.000012'],
    [0.2, '0.20'],
    [3.33333333, '3.33'],
  ])('%s → %s', (n, want) => {
    expect(fmtSmallRatio(n as number | null)).toBe(want);
  });
});
