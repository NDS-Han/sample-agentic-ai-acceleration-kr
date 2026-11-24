// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 사용자 × 모델 섹션 — 셀 렌더(미사용 '—'·분해 툴팁), 모드 전환은 재조회 없음,
 * 헤더 정렬 파라미터, 드로어(Esc/바깥 클릭 닫기·트리거로 포커스 복귀·표에서 보기).
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import { UserModelSection } from '@/components/analytics/UserModelSection';
import { requestUserModelDrawer, type UserModelResponse } from '@/components/analytics/userModel';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => '/analytics',
}));

const U1 = '11111111-1111-4111-8111-111111111111';
const U2 = '22222222-2222-4222-8222-222222222222';
const cell = (total: number, extra: Partial<Record<string, number>> = {}) => ({
  input_tokens: Math.round(total * 0.2),
  output_tokens: Math.round(total * 0.1),
  cache_read_tokens: Math.round(total * 0.6),
  cache_write_tokens: Math.round(total * 0.1),
  total_tokens: total,
  cost_usd: total / 1e6,
  requests: 7,
  ...extra,
});

const LIST: UserModelResponse = {
  period: '2026-11',
  timezone: 'Asia/Kolkata',
  start_date: '2026-11-01',
  end_date: '2026-11-30',
  top_models: [
    { model: 'claude-sonnet-4-6', display_name: 'Claude Sonnet 4.6', total_tokens: 3_000_000 },
    { model: 'claude-haiku', display_name: null, total_tokens: 500_000 },
  ],
  other_models: [{ model: 'nova-pro', display_name: 'Amazon Nova Pro', total_tokens: 10 }],
  total_users: 2,
  page: 1,
  page_size: 10,
  sort: 'total',
  dir: 'desc',
  q: '',
  max_cell: { total: 2_000_000, input: 400_000, output: 200_000, cache: 1_400_000 },
  rows: [
    {
      user_id: U1,
      name: 'Kim',
      email: 'kim@example.com',
      team_name: 'Platform',
      cells: { 'claude-sonnet-4-6': cell(2_000_000), 'claude-haiku': cell(500_000) },
      other: null,
      total: cell(2_500_000),
    },
    {
      user_id: U2,
      name: 'Lee',
      email: 'lee@example.com',
      team_name: 'Data',
      cells: { 'claude-sonnet-4-6': cell(1_000_000) },
      other: cell(10),
      total: cell(1_000_010),
    },
  ],
};

const DETAIL = {
  user_id: U2,
  name: 'Lee',
  email: 'lee@example.com',
  team_name: 'Data',
  period: '2026-11',
  timezone: 'Asia/Kolkata',
  start_date: '2026-11-01',
  end_date: '2026-11-30',
  models: [
    { model: 'claude-sonnet-4-6', display_name: 'Claude Sonnet 4.6', ...cell(1_000_000), avg_latency_ms: 812 },
    { model: 'nova-pro', display_name: null, ...cell(10), avg_latency_ms: 0 },
  ],
  total: { ...cell(1_000_010), avg_latency_ms: 800 },
};

let fetchMock: ReturnType<typeof vi.fn>;
const urls = () => fetchMock.mock.calls.map((c) => new URL(String(c[0]), 'http://x'));
const listCalls = () => urls().filter((u) => u.pathname === '/api/analytics/user-models');

beforeEach(() => {
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const u = new URL(String(input), 'http://x');
    const body = u.pathname.endsWith(`/${U2}`) || u.pathname.endsWith(`/${U1}`) ? DETAIL : LIST;
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function renderSection() {
  return render(
    <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>
      <button type="button">Lee trigger</button>
      <UserModelSection filters={{ period: '2026-11', scope: 'all', client: 'codex' }} />
    </NextIntlClientProvider>,
  );
}

async function ready() {
  renderSection();
  await screen.findByText('Kim');
}

describe('UserModelSection — 표', () => {
  it('필터·기본 정렬로 조회하고 캡션에 TZ·구간', async () => {
    await ready();
    const u = listCalls()[0];
    expect(Object.fromEntries(u.searchParams)).toMatchObject({
      period: '2026-11', scope: 'all', client: 'codex', sort: 'total', dir: 'desc', page: '1', page_size: '10',
    });
    expect(screen.getByTestId('user-model-caption').textContent).toBe(
      '2026-11-01 – 2026-11-30 · Dates in IST (Asia/Kolkata)',
    );
    expect(screen.getByTestId('user-model-range').textContent).toBe('1–2 of 2');
  });

  it('미사용 셀은 —, 사용 셀은 분해 툴팁·히트 배경', async () => {
    await ready();
    const lee = screen.getByText('Lee').closest('tr')!;
    const unused = lee.querySelector('[data-unused="true"]')!;
    expect(unused.textContent).toBe('—');
    expect(unused.getAttribute('title')).toBe('claude-haiku: not used');

    const kim = screen.getByText('Kim').closest('tr')!;
    const sonnet = within(kim).getByText('2M').closest('td')!;
    expect(sonnet.getAttribute('title')).toBe(
      [
        'Claude Sonnet 4.6 · Kim',
        'Input 400,000 · Output 200,000',
        'Cache read 1,200,000 · Cache write 200,000',
        'Total 2M',
        '$2.00 · 7 requests',
      ].join('\n'),
    );
    // max_cell.total 과 같은 값 → 농도 1.
    expect((sonnet.firstChild as HTMLElement).style.background).toContain('0.460');
    // 1센트 미만 비용은 정확값(유효숫자 2개)
    const lee1 = within(lee).getByText('10').closest('td')!;
    expect(lee1.getAttribute('title')!.split('\n').at(-1)).toBe('$0.000010 · 7 requests');
  });

  it("Other 에 든 모델이 있으면 'Other (n)' 열을 보이고, 없으면 열을 통째로 숨긴다", async () => {
    await ready();
    expect(screen.getByRole('columnheader', { name: 'Other (1)' })).toBeTruthy();
    expect(screen.getAllByRole('columnheader')).toHaveLength(7);
  });

  it("Other 가 비면 'Other (0)' 열이 없다 — 헤더·셀 모두", async () => {
    const noOther: UserModelResponse = {
      ...LIST,
      other_models: [],
      rows: LIST.rows.map((r) => ({ ...r, other: null })),
    };
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify(noOther), { status: 200, headers: { 'content-type': 'application/json' } }),
    );
    await ready();
    expect(screen.queryByRole('columnheader', { name: /Other/ })).toBeNull();
    expect(screen.getAllByRole('columnheader')).toHaveLength(6);
    const lee = screen.getByText('Lee').closest('tr')!;
    expect(lee.querySelectorAll('td')).toHaveLength(6);
  });

  it('모드 전환은 셀 값만 바꾸고(Total 정렬일 때만 sort 파라미터가 따라감)', async () => {
    await ready();
    const user = userEvent.setup();
    const before = listCalls().length;
    // 모델 열로 정렬 → 이후 모드 전환은 재조회 없음
    await user.click(screen.getByRole('button', { name: /Claude Sonnet 4.6/ }));
    await waitFor(() => expect(listCalls().length).toBe(before + 1));
    expect(listCalls().at(-1)!.searchParams.get('sort')).toBe('model:claude-sonnet-4-6');
    expect(listCalls().at(-1)!.searchParams.get('dir')).toBe('desc');

    await user.click(screen.getByRole('button', { name: 'Input' }));
    const kim = screen.getByText('Kim').closest('tr')!;
    expect(within(kim).getByText('400K')).toBeTruthy();
    expect(listCalls().length).toBe(before + 1);

    // Total 열 정렬은 활성 모드 키로
    await user.click(within(screen.getByRole('columnheader', { name: /Total/ })).getByRole('button'));
    await waitFor(() => expect(listCalls().at(-1)!.searchParams.get('sort')).toBe('input'));
  });

  it('검색은 디바운스 후 q 로', async () => {
    await ready();
    const user = userEvent.setup();
    await user.type(screen.getByRole('searchbox'), 'lee');
    await waitFor(() => expect(listCalls().at(-1)!.searchParams.get('q')).toBe('lee'), { timeout: 2000 });
    expect(listCalls().filter((u) => u.searchParams.has('q')).length).toBe(1);
  });

  it('행 펼침은 상세를 조회해 모든 모델을 보인다', async () => {
    await ready();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Expand Lee' }));
    expect(await screen.findByText('Lee · all models (2)')).toBeTruthy();
    expect(screen.getByText('812ms')).toBeTruthy();
    expect(urls().some((u) => u.pathname === `/api/analytics/user-models/${U2}` && u.searchParams.get('client') === 'codex')).toBe(true);
  });

  it('행 전체가 펼침 대상 — 이름 클릭·Enter 로 펼치고, 셰브런은 한 번만 토글', async () => {
    await ready();
    const user = userEvent.setup();
    const row = screen.getByText('Lee').closest('tr')!;
    expect(row.getAttribute('tabindex')).toBe('0');
    expect(row.className).toContain('cursor-pointer');
    await user.click(screen.getByText('Lee'));
    expect(row.getAttribute('aria-expanded')).toBe('true');
    expect(document.getElementById(row.getAttribute('aria-controls')!)).toBeTruthy();
    expect(await screen.findByText('Lee · all models (2)')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Collapse Lee' }));
    expect(row.getAttribute('aria-expanded')).toBe('false');
    row.focus();
    await user.keyboard('{Enter}');
    expect(row.getAttribute('aria-expanded')).toBe('true');
  });

  it('422 는 기간 초과 안내', async () => {
    fetchMock.mockImplementation(async () =>
      new Response(JSON.stringify({ error: { code: 'date_range_too_long' } }), { status: 422 }),
    );
    renderSection();
    expect(await screen.findByText(/at most 366 days/)).toBeTruthy();
  });
});

describe('UserModelDrawer', () => {
  async function openFor(target: Parameters<typeof requestUserModelDrawer>[0]) {
    await ready();
    const trigger = screen.getByRole('button', { name: 'Lee trigger' });
    trigger.focus();
    act(() => requestUserModelDrawer({ ...target, trigger }));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('Lee · all models (2)').catch(() => null);
    await within(dialog).findByText('Show in table');
    return { dialog, trigger };
  }

  it('role=dialog·aria-modal·제목으로 라벨, Esc 로 닫고 트리거로 포커스 복귀', async () => {
    const { dialog, trigger } = await openFor({ user_id: U2, email: 'lee@example.com', name: 'Lee' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    const labelledBy = dialog.getAttribute('aria-labelledby')!;
    expect(document.getElementById(labelledBy)!.textContent).toBe('Lee');
    // 동명이인 구분 — 이름 아래 표 행과 같은 "email · team"
    expect(within(dialog).getByTestId('user-model-drawer-sub').textContent).toBe('lee@example.com · Data');
    expect(dialog.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });

  it('바깥(backdrop) 클릭으로 닫힌다', async () => {
    await openFor({ user_id: U2 });
    const user = userEvent.setup();
    await user.click(screen.getByTestId('user-model-drawer-backdrop'));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('user_id 가 없으면 이메일로 표를 검색해 찾는다', async () => {
    await openFor({ email: 'lee@example.com', name: 'Lee' });
    expect(listCalls().some((u) => u.searchParams.get('q') === 'lee@example.com')).toBe(true);
    expect(urls().some((u) => u.pathname === `/api/analytics/user-models/${U2}`)).toBe(true);
  });

  it('표에서 보기 — 드로어를 닫고 검색=이메일, 그 행을 펼친다', async () => {
    const { dialog } = await openFor({ user_id: U2 });
    const user = userEvent.setup();
    await user.click(within(dialog).getByText('Show in table'));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect((screen.getByRole('searchbox') as HTMLInputElement).value).toBe('lee@example.com');
    await waitFor(() => expect(listCalls().at(-1)!.searchParams.get('q')).toBe('lee@example.com'));
    const toggle = await screen.findByRole('button', { name: 'Collapse Lee' });
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    // 포커스는 펼친 행 자체(행이 키보드 펼침 대상)
    await waitFor(() => expect(document.activeElement).toBe(toggle.closest('tr')));
  });
});

describe('ExportButton — 사용자 × 모델 CSV', () => {
  async function menu(groupBy: 'user' | 'model') {
    const { ExportButton } = await import('@/components/analytics/ExportButton');
    const { ReportingTimezoneProvider } = await import('@/components/common/ReportingTimezoneProvider');
    render(
      <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>
        <ReportingTimezoneProvider tz="Asia/Kolkata">
          <ExportButton filter={{ period: '2026-11', group_by: groupBy } as never} latestMonth="2026-11" />
        </ReportingTimezoneProvider>
      </NextIntlClientProvider>,
    );
    await userEvent.setup().click(screen.getByRole('button', { name: /Export/ }));
  }

  it('By User 모드에만 노출, view=user_model 로 요청', async () => {
    await menu('user');
    const item = screen.getByRole('menuitem', { name: 'User × model (CSV)' });
    URL.createObjectURL = vi.fn(() => 'blob:x');
    URL.revokeObjectURL = vi.fn();
    await userEvent.setup().click(item);
    await waitFor(() => expect(urls().some((u) => u.pathname === '/api/analytics-export')).toBe(true));
    const u = urls().find((x) => x.pathname === '/api/analytics-export')!;
    expect(u.searchParams.get('view')).toBe('user_model');
    expect(u.searchParams.get('format')).toBe('csv');
  });

  it('다른 모드에는 없다', async () => {
    await menu('model');
    expect(screen.queryByRole('menuitem', { name: 'User × model (CSV)' })).toBeNull();
    expect(screen.getAllByRole('menuitem')).toHaveLength(2);
  });
});
