// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AppPolicyPanel — 탭 전환 중 도착한 저장 응답이 새 앱 화면을 덮어쓰지 않아야 한다.
 *
 * 배경(kiro R19-S1): doToggle/handleToggleWebSearch/handleSaveDefaultModel 의
 * 응답은 호출 시점의 selectedClient 로 완료된다. 앱 탭을 바꾼 뒤 응답이 도착하면
 * 응답의 setPolicy(구 앱 정책 전체)가 새 앱 화면에 들어가 — 다음 저장은 엉뚱한
 * 앱의 표시 상태를 기준으로 쓰인다. 클라이언트 ref 가드로 차단한다.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { AppPolicyPanel } from '@/components/apps/AppPolicyPanel';
import type { AppPolicy } from '@/lib/actions/apps';

const getAppPolicyAction = vi.fn();
const setAppDefaultModelAction = vi.fn();
const toggleAppModelAction = vi.fn();
const setClientWebSearchAction = vi.fn();

vi.mock('@/lib/actions/apps', () => ({
  getAppPolicyAction: (...a: unknown[]) => getAppPolicyAction(...a),
  setAppDefaultModelAction: (...a: unknown[]) => setAppDefaultModelAction(...a),
  toggleAppModelAction: (...a: unknown[]) => toggleAppModelAction(...a),
}));
vi.mock('@/lib/actions/routing', () => ({
  setClientWebSearchAction: (...a: unknown[]) => setClientWebSearchAction(...a),
}));

// ?app= 쿼리가 정본 — 테스트가 바꿀 수 있는 mutable 값으로 돌려준다.
let appParam = 'claude-code';
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => '/apps',
  useSearchParams: () => new URLSearchParams(appParam ? `app=${appParam}` : ''),
}));

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));

vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast }),
}));

function policyFor(client: string, allowed: string[] = ['m1']): AppPolicy {
  return {
    client,
    allowed_models: allowed,
    default_model: allowed[0] ?? null,
    allowed_users: [],
    all_models: [{ alias: 'm1', allowed_clients: null }],
    web_search_enabled: false,
  };
}

/** resolve 를 밖에서 잡는 지연 프로미스. */
function deferred<T>() {
  let resolve!: (_v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe('AppPolicyPanel — 탭 전환 중 도착한 저장 응답', () => {
  beforeEach(() => {
    appParam = 'claude-code';
    vi.clearAllMocks();
    getAppPolicyAction.mockImplementation(async (client: string) => ({
      success: true,
      data: policyFor(client),
    }));
  });

  it('구 앱 토글 응답이 새 앱의 화면 상태를 덮어쓰지 않는다', async () => {
    const toggle = deferred<{ success: true; data: AppPolicy }>();
    toggleAppModelAction.mockReturnValue(toggle.promise);

    const { rerender } = render(<AppPolicyPanel />);
    // claude-code 정책 로드 완료 대기 (alias 는 select 옵션+표 셀에 중복 표시)
    await waitFor(() => expect(screen.getAllByText('m1').length).toBeGreaterThan(0));

    // 모델 토글 클릭 — allowed_clients=null 이라 해제는 'all' 확인 경로.
    const modelCheckbox = screen.getAllByRole('checkbox').at(-1)!;
    fireEvent.click(modelCheckbox);
    // 확인 다이얼로그에서 진행 → doToggle 이 저장을 시작한다(응답은 보류).
    fireEvent.click(await screen.findByRole('button', { name: 'confirmAllLabel' }));
    await waitFor(() => expect(toggleAppModelAction).toHaveBeenCalled());
    expect(toggleAppModelAction).toHaveBeenCalledWith('claude-code', 'm1', false);

    // 응답 도착 전에 탭을 codex 로 전환한다. React 19 에서 pending async
    // transition(진행 중인 저장) 동안은 이 컴포넌트의 새 렌더 커밋이 미뤄진다 —
    // 이펙트·fetch 는 실행되지만 그 결과 setPolicy 는 pending 이 풀린 뒤에야
    // 화면에 반영된다. 따라서 마지막에 도착한 응답의 setPolicy 가 최종 화면을
    // 결정하며, 가드가 없으면 stale 응답이 새 앱 정책을 덮어쓴다.
    const codexFetch = deferred<{ success: true; data: AppPolicy }>();
    appParam = 'codex';
    getAppPolicyAction.mockReturnValueOnce(codexFetch.promise);
    await act(async () => {
      rerender(<AppPolicyPanel />);
    });

    // 두 응답을 순서대로 완료시킨다 — codex 정책 로드가 먼저, 구 앱의 stale
    // 토글 응답이 마지막. pending transition 이 있는 동안 렌더 커밋이 미뤄지므로
    // 마지막에 도착한 응답의 setPolicy 가 최종 화면을 결정한다. 가드가 없으면
    // m2 가 codex 정책을 덮어써 최종 화면에 m2 가 남는다.
    await act(async () => {
      codexFetch.resolve({
        success: true,
        data: {
          ...policyFor('codex', ['m-codex']),
          all_models: [{ alias: 'm-codex', allowed_clients: null }],
        },
      });
    });
    await act(async () => {
      toggle.resolve({
        success: true,
        data: {
          ...policyFor('claude-code', ['m2']),
          all_models: [{ alias: 'm2', allowed_clients: null }],
        },
      });
    });
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(expect.objectContaining({ type: 'success' }))
    );
    expect(screen.getAllByText('m-codex').length).toBeGreaterThan(0);
    expect(screen.queryByText('m2')).not.toBeInTheDocument();
  });

  it('저장과 경합한 로드 응답은 버리고 재조회해 최신 상태를 적용한다', async () => {
    // kiro R19-N1: 저장이 진행 중인 앱을 다시 로드하면 서버 읽기가 저장 커밋보다
    // 빨라 응답이 구 정책을 되살릴 수 있다. 로드는 fetch 발행 시점의 완료 seq 와
    // 응답 시점 seq 를 비교해 경합을 감지하고, 안정될 때까지 재조회한다.
    const toggle = deferred<{ success: true; data: AppPolicy }>();
    toggleAppModelAction.mockReturnValue(toggle.promise);

    const { rerender } = render(<AppPolicyPanel />);
    await waitFor(() => expect(screen.getAllByText('m1').length).toBeGreaterThan(0));

    // 저장 시작(응답 보류) → 다른 탭 갔다가 돌아와 재로드를 유발한다.
    const modelCheckbox = screen.getAllByRole('checkbox').at(-1)!;
    fireEvent.click(modelCheckbox);
    fireEvent.click(await screen.findByRole('button', { name: 'confirmAllLabel' }));
    await waitFor(() => expect(toggleAppModelAction).toHaveBeenCalled());

    const codexFetch = deferred<{ success: true; data: AppPolicy }>();
    appParam = 'codex';
    getAppPolicyAction.mockReturnValueOnce(codexFetch.promise);
    await act(async () => {
      rerender(<AppPolicyPanel />);
    });
    await act(async () => {
      codexFetch.resolve({
        success: true,
        data: {
          ...policyFor('codex', ['m-codex']),
          all_models: [{ alias: 'm-codex', allowed_clients: null }],
        },
      });
    });

    // claude-code 복귀 → 저장이 아직 진행 중이라 재로드는 경합 상태다.
    const staleReload = deferred<{ success: true; data: AppPolicy }>();
    appParam = 'claude-code';
    getAppPolicyAction.mockReturnValueOnce(staleReload.promise);
    await act(async () => {
      rerender(<AppPolicyPanel />);
    });
    await waitFor(() => expect(getAppPolicyAction).toHaveBeenCalledTimes(3));

    // 저장 응답(m3, 최신)을 먼저 적용하고, 재로드 응답은 커밋 전에 읽힌
    // 구 정책(m1)으로 늦게 도착한다 — 가드가 없으면 m1 이 m3 를 덮어쓴다.
    await act(async () => {
      toggle.resolve({
        success: true,
        data: {
          ...policyFor('claude-code', ['m3']),
          all_models: [{ alias: 'm3', allowed_clients: null }],
        },
      });
    });
    await act(async () => {
      staleReload.resolve({ success: true, data: policyFor('claude-code') });
    });

    // seq 불일치로 재조회가 발생한다(4번째 fetch) — 최신 응답을 적용.
    const freshFetch = deferred<{ success: true; data: AppPolicy }>();
    getAppPolicyAction.mockReturnValueOnce(freshFetch.promise);
    await waitFor(() => expect(getAppPolicyAction).toHaveBeenCalledTimes(4));
    await act(async () => {
      freshFetch.resolve({
        success: true,
        data: {
          ...policyFor('claude-code', ['m3']),
          all_models: [{ alias: 'm3', allowed_clients: null }],
        },
      });
    });

    await waitFor(() => expect(screen.getAllByText('m3').length).toBeGreaterThan(0));
    expect(screen.queryByText('m1')).not.toBeInTheDocument();
  });
});
