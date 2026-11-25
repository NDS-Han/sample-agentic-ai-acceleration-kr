// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * LiveRpmCard 폴링 — 탭이 숨겨지면 멈추고 보이면 즉시 재개, 401 이면 영구 중단 + 로그인 이동.
 */

import { act, render } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import { LIVE_RPM_POLL_MS, LiveRpmCard } from '@/components/rate-limits/LiveRpmCard';

let visibility: DocumentVisibilityState = 'visible';

function setVisibility(v: DocumentVisibilityState) {
  visibility = v;
  document.dispatchEvent(new Event('visibilitychange'));
}

function stubFetch(status = 200) {
  const f = vi.fn(async () =>
    new Response(JSON.stringify({ available: true, window_sec: 60, tracked: true, rpm_used_total: 1, by_model: [] }), {
      status,
      headers: { 'content-type': 'application/json' },
    }),
  );
  vi.stubGlobal('fetch', f);
  return f;
}

function mount() {
  return render(
    <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>
      <LiveRpmCard scope="USER" scopeId="u1" rpmLimit={10} />
    </NextIntlClientProvider>,
  );
}

const flush = () => act(async () => {
  await Promise.resolve();
  await Promise.resolve();
});

beforeEach(() => {
  vi.useFakeTimers();
  visibility = 'visible';
  vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('LiveRpmCard polling', () => {
  it('polls every 10 s while visible, pauses while hidden, resumes immediately', async () => {
    const f = stubFetch();
    mount();
    await flush();
    expect(f).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(LIVE_RPM_POLL_MS); });
    expect(f).toHaveBeenCalledTimes(2);

    act(() => setVisibility('hidden'));
    await act(async () => { vi.advanceTimersByTime(LIVE_RPM_POLL_MS * 6); });
    expect(f).toHaveBeenCalledTimes(2);

    act(() => setVisibility('visible'));
    await flush();
    expect(f).toHaveBeenCalledTimes(3);
    await act(async () => { vi.advanceTimersByTime(LIVE_RPM_POLL_MS); });
    expect(f).toHaveBeenCalledTimes(4);
  });

  it('does not start polling in a hidden tab', async () => {
    visibility = 'hidden';
    const f = stubFetch();
    mount();
    await act(async () => { vi.advanceTimersByTime(LIVE_RPM_POLL_MS * 3); });
    expect(f).not.toHaveBeenCalled();
  });

  it('stops for good on 401 and sends the user to login', async () => {
    const assign = vi.fn();
    vi.stubGlobal('location', { ...window.location, assign });
    const f = stubFetch(401);
    mount();
    await flush();
    await flush();
    expect(f).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith('/api/auth/login');
    await act(async () => { vi.advanceTimersByTime(LIVE_RPM_POLL_MS * 5); });
    act(() => setVisibility('hidden'));
    act(() => setVisibility('visible'));
    await flush();
    expect(f).toHaveBeenCalledTimes(1);
  });
});
