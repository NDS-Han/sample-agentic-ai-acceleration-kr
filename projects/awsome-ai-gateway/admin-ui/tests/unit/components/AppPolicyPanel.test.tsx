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
// 모델 표시명 카탈로그 — 기본은 빈 목록(라벨 = alias). 라벨 테스트만 값을 넣는다.
const listActiveModelsAction = vi.fn();
vi.mock('@/lib/actions/models', () => ({
  listActiveModelsAction: (...a: unknown[]) => listActiveModelsAction(...a),
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
    account_role_arn: null,
    external_id: null,
    region: 'ap-northeast-2',
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
    listActiveModelsAction.mockResolvedValue({ success: true, data: [] });
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

describe('AppPolicyPanel — web search 미배포 표시 (P1-5)', () => {
  beforeEach(() => {
    appParam = 'claude-code';
    vi.clearAllMocks();
  });

  it('미배포면 저장값(On) 대신 "사용 불가" 라벨 + 저장값 보조 문구를 보인다', async () => {
    getAppPolicyAction.mockImplementation(async (client: string) => ({
      success: true,
      data: { ...policyFor(client), web_search_enabled: true, web_search_deployed: false },
    }));
    render(<AppPolicyPanel />);
    expect(await screen.findByText('webSearchUnavailable')).toBeInTheDocument();
    expect(screen.getByText('webSearchSavedValue')).toBeInTheDocument();
    expect(screen.queryByText('webSearchOn')).not.toBeInTheDocument();
    // 배포 설정 키는 툴팁으로
    expect(screen.getByText('webSearchNotDeployedHint')).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: 'webSearchTitle' })).not.toBeInTheDocument();
  });

  it('배포돼 있으면 스위치와 저장값 라벨을 그대로 보인다', async () => {
    getAppPolicyAction.mockImplementation(async (client: string) => ({
      success: true,
      data: { ...policyFor(client), web_search_enabled: true, web_search_deployed: true },
    }));
    render(<AppPolicyPanel />);
    const sw = await screen.findByRole('checkbox', { name: 'webSearchTitle' });
    expect(sw).toBeChecked();
    expect(screen.getByText('webSearchOn')).toBeInTheDocument();
    expect(screen.queryByText('webSearchUnavailable')).not.toBeInTheDocument();
  });
});

describe('AppPolicyPanel — 모델 표기·표 스크롤 (P2-7, P2-8)', () => {
  beforeEach(() => {
    appParam = 'claude-code';
    vi.clearAllMocks();
  });

  it('기본 모델 드롭다운·Model access 표는 display_name 우선, alias 는 보조로 표시한다', async () => {
    listActiveModelsAction.mockResolvedValue({
      success: true,
      data: [{ alias: 'claude-haiku-4-5-20251001', display_name: 'Haiku 4.5' }],
    });
    getAppPolicyAction.mockResolvedValue({
      success: true,
      data: {
        ...policyFor('claude-code', ['claude-haiku-4-5-20251001', 'plain']),
        all_models: [
          { alias: 'claude-haiku-4-5-20251001', allowed_clients: null },
          { alias: 'plain', allowed_clients: null },
        ],
      },
    });
    render(<AppPolicyPanel />);

    expect(
      await screen.findByRole('option', { name: 'Haiku 4.5 (claude-haiku-4-5-20251001)' }),
    ).toBeInTheDocument();
    // 표시명이 없으면 alias 하나만(괄호 중복 없음).
    expect(screen.getByRole('option', { name: 'plain' })).toBeInTheDocument();
    // 표: 표시명 줄 + alias 보조 줄, 체크박스 라벨도 표시명.
    expect(screen.getByText('Haiku 4.5')).toBeInTheDocument();
    expect(screen.getByText('claude-haiku-4-5-20251001')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Haiku 4.5' })).toBeInTheDocument();
  });

  it('표에 내부 스크롤(max-h/overflow-y-auto)이 없고 헤더는 sticky 다', async () => {
    listActiveModelsAction.mockResolvedValue({ success: true, data: [] });
    getAppPolicyAction.mockResolvedValue({ success: true, data: policyFor('claude-code') });
    const { container } = render(<AppPolicyPanel />);
    await screen.findByText('colModel');

    expect(container.querySelector('.max-h-96')).toBeNull();
    expect(container.querySelector('.overflow-y-auto')).toBeNull();
    // sticky 가 <main> 스크롤에 붙으려면 카드가 스크롤 컨테이너(overflow-hidden)면 안 된다.
    const table = screen.getByText('colModel').closest('table')!;
    expect(table.closest('.overflow-hidden')).toBeNull();
    expect(table.querySelector('thead')!.className).toContain('[&_th]:sticky');
  });

  it('허용 사용자가 50명을 넘으면 처음 50명만 보이고 전체 보기로 펼친다', async () => {
    listActiveModelsAction.mockResolvedValue({ success: true, data: [] });
    const users = Array.from({ length: 60 }, (_, i) => ({
      user_id: `u-${i}`,
      email: `u${i}@example.com`,
      explicit: true,
    }));
    getAppPolicyAction.mockResolvedValue({
      success: true,
      data: { ...policyFor('claude-code'), allowed_users: users },
    });
    render(<AppPolicyPanel />);

    await screen.findByText('u-0');
    expect(screen.getByText('u-49')).toBeInTheDocument();
    expect(screen.queryByText('u-50')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'showAllUsers' }));
    expect(screen.getByText('u-59')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'showAllUsers' })).toBeNull();
  });
});
