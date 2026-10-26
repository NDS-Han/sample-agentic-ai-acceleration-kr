'use client';
// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState, useTransition, useEffect, useRef } from 'react';
import Link from 'next/link';
import { useRouter, usePathname, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { getAppPolicyAction, setAppDefaultModelAction, toggleAppModelAction } from '@/lib/actions/apps';
import { setClientWebSearchAction, setClientRoutingAction } from '@/lib/actions/routing';
import type { AppPolicy, AppModelRef } from '@/lib/actions/apps';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';
import { InfoTooltip } from '@/components/common/InfoTooltip';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { FormError } from '@/components/common/FormError';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { useToast } from '@/components/common/ToastProvider';
import {
  CLIENTS as GATEWAY_CLIENTS,
  CLIENT_LABELS,
  CLIENT_SHORT_LABELS,
  modelAllowsClient,
  modelAppScope,
} from '@/lib/constants/gateway';

// 이 패널이 routing_profiles.default_model 과 앱별 모델 허용을 바꿀 수 있는 유일한
// 화면이다. 목록에 앱이 빠지면 그 앱의 default_model 을 콘솔에서 전혀 설정할 수 없고,
// gateway 는 프로필의 default_model 로 라우팅하므로 사실상 그 앱이 동작하지 않는다.
const CLIENTS = GATEWAY_CLIENTS.map((value) => ({
  value: value as string,
  label: CLIENT_LABELS[value] ?? value,
}));

const APP_PARAM = 'app';

export function AppPolicyPanel() {
  const t = useTranslations('apps');
  const tc = useTranslations('common');
  const { toast } = useToast();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  // 선택 앱은 ?app= 쿼리가 정본이다 — useState 로 두면 mutation action 의 revalidatePath
  // 가 일으키는 RSC 리페치/리마운트 때 선택이 날아간다(실제로 그 버그가 있었다).
  // URL 에 두면 리마운트·새로고침·딥링크 모두에서 선택이 유지된다.
  // (APP_PARAM 상수: 쿼리명을 리터럴로 쓰면 i18n 키 스캔 테스트가 t() 호출로 오인한다.)
  // 쿼리가 없거나 이상하면 첫 앱(claude-code)을 기본 선택 — 빈 상태 문구를 띄우지 않는다.
  const rawClient = searchParams.get(APP_PARAM) ?? '';
  const selectedClient = CLIENTS.some((c) => c.value === rawClient)
    ? rawClient
    : (CLIENTS[0]?.value ?? '');
  const [policy, setPolicy] = useState<AppPolicy | null>(null);
  // 저장 응답이 탭 전환 뒤에 도착하면 구 앱의 결과로 신 앱 화면 상태를 덮어쓴다
  // (토글 응답은 앱 전체 policy 를 돌려주므로 codex 탭이 claude-code 값을 표시).
  // 각 비동기 완료 콜백은 호출 시점 앱과 현재 앱을 비교해 다르면 상태 반영을 건너뛴다.
  // ⚠️ ref 갱신을 useEffect 에 두면 pending async transition(React 19)이
  //    이펙트를 미루는 동안 응답이 도착해 가드가 구 값을 읽을 수 있다 —
  //    렌더 본문에서 갱신해 커밋된 렌더의 앱을 즉시 반영한다.
  const clientRef = useRef(selectedClient);
  clientRef.current = selectedClient;
  // 이펙트 deps 에 t 를 넣으면 참조가 바뀔 때마다 정책을 재조회한다 — 비동기
  // 콜백은 렌더 시점이 아니라 완료 시점의 t 를 ref 로 읽는다.
  const tRef = useRef(t);
  tRef.current = t;
  // 정책의 client 와 현재 선택이 어긋나면 렌더에서 제외한다 — ref 가드가 뚫리는
  // 마지막 한 프레임에도 구 앱 데이터가 절대 화면에 나오지 않는다.
  const shown = policy?.client === selectedClient ? policy : null;
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoadPending, startLoadTransition] = useTransition();
  // 저장 응답 유실(transport 거부) 후 실제 상태를 확인하려고 로드 이펙트를
  // 재실행시키는 트리거 — 의존성에 두면 setReloadNonce 만으로 재조회된다.
  const [reloadNonce, setReloadNonce] = useState(0);
  const [defaultModelInput, setDefaultModelInput] = useState('');
  const [saveError, setSaveError] = useState<string | null>(null);
  const [, startSaveTransition] = useTransition();
  // pending 은 transition 전역이라 탭을 넘어선다 — 앱별 in-flight 수를 세서
  // 저장이 진행 중인 앱의 컨트롤만 잠근다. A 저장 중 B 저장을 시작하고 A 로
  // 돌아와도 A 의 잠금은 유지된다(응답이 전체 정책을 덮어쓰므로 중복 전송을
  // 막아야 한다). finally 로 잡아 reject 경로의 카운트 누수도 없앤다.
  const [savingByClient, setSavingByClient] = useState<Record<string, number>>({});
  const [wsSavingByClient, setWsSavingByClient] = useState<Record<string, number>>({});
  const [routingSavingByClient, setRoutingSavingByClient] = useState<Record<string, number>>({});
  const [routingForm, setRoutingForm] = useState({
    client: '', arn: '', eid: '', region: '', dirty: false,
  });
  const [routingError, setRoutingError] = useState<string | null>(null);
  // 비동기 콜백이 읽는 최신 스냅샷 — state 클로저는 시작 시점 값이라 응답
  // 시점의 진행 수를 모른다. seq 는 저장이 끝날 때마다 오르며, 정책 로드
  // 응답이 저장 커밋과 경합해 구 값을 되살릴 수 있는지 판정하는 데 쓴다.
  const opRef = useRef<Record<string, { save: number; ws: number; routing: number; seq: number }>>({});
  const bumpOp = (kind: 'save' | 'ws' | 'routing', c: string, d: number) => {
    const cur = opRef.current[c] ?? { save: 0, ws: 0, routing: 0, seq: 0 };
    const next = {
      ...cur,
      [kind]: Math.max(0, cur[kind] + d),
      seq: d < 0 ? cur.seq + 1 : cur.seq,
    };
    opRef.current = { ...opRef.current, [c]: next };
    (kind === 'save'
      ? setSavingByClient
      : kind === 'ws'
        ? setWsSavingByClient
        : setRoutingSavingByClient)(
      (p) => ({ ...p, [c]: next[kind] }),
    );
  };
  const isSaving = (savingByClient[selectedClient] ?? 0) > 0;
  const isWsSaving = (wsSavingByClient[selectedClient] ?? 0) > 0;
  const isRoutingSaving = (routingSavingByClient[selectedClient] ?? 0) > 0;
  // kind 는 어느 확인 문구를 띄울지 고른다. 'all' = NULL(전체 허용) 을 명시 목록으로 굳히는
  // 전이, 'last' = 마지막 허용 앱을 빼서 `[]`(전면 거부) 로 가는 전이. 문구를 하나로 합치면
  // 둘 중 하나는 반드시 거짓말이 된다(전자는 "범위가 바뀔 수 있다", 후자는 "아무 앱도 못 쓴다").
  // client 도 함께 기억한다 — 다이얼로그가 떠 있는 동안 뒤로가기 등으로
  // ?app= 이 바뀌면 확인이 엉뚱한 앱에 저장된다(N1).
  const [pendingToggle, setPendingToggle] = useState<
    { alias: string; allowed: boolean; kind: 'all' | 'last'; client: string } | null
  >(null);
  const [, startWsTransition] = useTransition();

  // 선택 변경 → URL 갱신. policy fetch 는 아래 useEffect 가 selectedClient 를 보고 수행한다
  // (클릭 경로뿐 아니라 리마운트 복구 경로에서도 같은 코드를 타게 하기 위함).
  const prevClientForResetRef = useRef(selectedClient);
  useEffect(() => {
    // reloadNonce 재실행(저장 응답 유실 후 상태 재확인)은 폼 입력을 유지한다 —
    // 탭 전환일 때만 미저장 선택·인라인 에러·보류 중 확인을 지운다.
    const clientChanged = prevClientForResetRef.current !== selectedClient;
    prevClientForResetRef.current = selectedClient;
    setPolicy(null);
    setLoadError(null);
    if (clientChanged) {
      setDefaultModelInput('');
      setSaveError(null);
      setPendingToggle(null);
    }
    if (!selectedClient) return;
    let cancelled = false;
    const clientAtCall = selectedClient;
    startLoadTransition(async () => {
      // 이 앱의 저장이 로드와 겹치면 서버 읽기가 저장 커밋보다 빨라 응답이
      // 구 정책을 되살릴 수 있다 — 저장 진행 수/완료 seq 가 안정될 때까지
      // 기다렸다가 다시 읽는다. 로드 중에는 이 앱의 새 저장을 시작할 수 없으므로
      // (컨트롤이 isLoadPending 뒤에 숨는다) 드레인은 유한하고, 데드라인은
      // 그마저 오래 걸리는 비정상 저장에 대한 최후 방어선이다.
      const deadline = Date.now() + 10_000;
      for (;;) {
        const seq0 = opRef.current[clientAtCall]?.seq ?? 0;
        let result: Awaited<ReturnType<typeof getAppPolicyAction>> | null = null;
        try {
          result = await getAppPolicyAction(clientAtCall);
        } catch {
          // transport 거부 — 데드라인까지는 같은 간격으로 다시 읽는다.
        }
        // cleanup 이 미뤄지는 동안(pending transition) 도착한 응답도 ref 가드로 걸러낸다.
        if (cancelled || clientRef.current !== clientAtCall) return;
        const op = opRef.current[clientAtCall];
        const settled = !op || (op.save + op.ws + op.routing === 0 && op.seq === seq0);
        const expired = Date.now() >= deadline;
        if (result === null) {
          if (expired) {
            setLoadError(tRef.current('loadFailed'));
            return;
          }
        } else if (!result.success || settled || expired) {
          if (result.success) {
            setPolicy(result.data);
            // 미저장 선택이 새 허용 목록에도 유효하면 유지 — nonce 재조회에서도
            // 입력값이 날아가지 않는다(doToggle 의 재동기화와 같은 규칙).
            setDefaultModelInput((prev) =>
              result.data.allowed_models.includes(prev)
                ? prev
                : (result.data.default_model ?? ''),
            );
          } else {
            setLoadError(result.error);
          }
          return;
        }
        await new Promise((r) => setTimeout(r, 250));
        if (cancelled || clientRef.current !== clientAtCall) return;
      }
    });
    return () => {
      cancelled = true;
    };
  }, [selectedClient, reloadNonce]);

  // 라우팅 폼을 서버 값으로 맞춘다 — 사용자가 입력 중(dirty)이면 덮어쓰지 않는다.
  useEffect(() => {
    if (!shown) return;
    setRoutingForm((prev) =>
      prev.dirty && prev.client === shown.client
        ? prev
        : {
            client: shown.client,
            arn: shown.account_role_arn ?? '',
            eid: shown.external_id ?? '',
            region: shown.region ?? '',
            dirty: false,
          },
    );
    setRoutingError(null);
  }, [shown]);

  const handleSaveRouting = () => {
    setRoutingError(null);
    const clientAtCall = selectedClient;
    const payload = {
      account_role_arn: routingForm.arn.trim(),
      external_id: routingForm.eid.trim(),
      region: routingForm.region.trim(),
    };
    bumpOp('routing', clientAtCall, 1);
    startWsTransition(async () => {
      try {
        const result = await setClientRoutingAction(clientAtCall, payload);
        if (result.success) {
          toast({
            type: 'success',
            message: t('routingSaved', { app: CLIENT_LABELS[clientAtCall] ?? clientAtCall }),
            auto_dismiss_ms: 3000,
          });
          if (clientRef.current === clientAtCall) {
            setPolicy((prev) =>
              prev
                ? {
                    ...prev,
                    account_role_arn: payload.account_role_arn || null,
                    // ARN 해제 시 서버가 external_id 도 함께 지운다 — 같은 값을 반영.
                    external_id: payload.account_role_arn ? payload.external_id || null : null,
                    region: payload.region || null,
                  }
                : prev,
            );
            setRoutingForm((prev) => ({ ...prev, dirty: false }));
          }
        } else if (clientRef.current === clientAtCall) {
          setRoutingError(result.error);
        } else {
          toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
        }
      } catch {
        toast({ type: 'error', message: t('saveUnconfirmed'), auto_dismiss_ms: 4000 });
        if (clientRef.current === clientAtCall) setReloadNonce((n) => n + 1);
      } finally {
        bumpOp('routing', clientAtCall, -1);
      }
    });
  };

  const handleClientChange = (client: string) => {
    if (client === selectedClient) return;
    const sp = new URLSearchParams(searchParams.toString());
    if (client) sp.set(APP_PARAM, client);
    else sp.delete(APP_PARAM);
    const qs = sp.toString();
    router.push(qs ? `${pathname}?${qs}` : pathname);
  };

  const handleToggleWebSearch = (enabled: boolean) => {
    if (!shown) return;
    const clientAtCall = selectedClient;
    bumpOp('ws', clientAtCall, 1);
    startWsTransition(async () => {
      try {
        const result = await setClientWebSearchAction(clientAtCall, enabled);
        if (result.success) {
          // 탭이 바뀐 뒤 도착한 응답은 토스트만 — 사용자가 보는 앱이 아닌데
          // 그 앱의 상태를 쓰면 화면이 구 앱 값을 보여준다.
          if (clientRef.current === clientAtCall) {
            setPolicy((prev) => (prev ? { ...prev, web_search_enabled: enabled } : prev));
          }
          toast({ type: 'success', message: t('webSearchSaved', { app: CLIENT_LABELS[clientAtCall] ?? clientAtCall }), auto_dismiss_ms: 3000 });
        } else {
          toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
        }
      } catch {
        // transport 거부 — 서버 반영 여부를 알 수 없으므로 단정하지 않고
        // 아직 같은 앱을 보고 있으면 정책을 다시 읽어 실제 상태로 맞춘다.
        toast({ type: 'error', message: t('saveUnconfirmed'), auto_dismiss_ms: 4000 });
        if (clientRef.current === clientAtCall) setReloadNonce((n) => n + 1);
      } finally {
        bumpOp('ws', clientAtCall, -1);
      }
    });
  };

  const handleSaveDefaultModel = () => {
    setSaveError(null);
    const clientAtCall = selectedClient;
    bumpOp('save', clientAtCall, 1);
    startSaveTransition(async () => {
      try {
        const result = await setAppDefaultModelAction(clientAtCall, defaultModelInput.trim());
        if (result.success) {
          toast({ type: 'success', message: t('defaultModelSaved', { app: CLIENT_LABELS[clientAtCall] ?? clientAtCall }), auto_dismiss_ms: 3000 });
          if (clientRef.current === clientAtCall) {
            setPolicy((prev) => (prev ? { ...prev, default_model: defaultModelInput.trim() } : prev));
          }
        } else if (clientRef.current === clientAtCall) {
          setSaveError(result.error);
        } else {
          toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
        }
      } catch {
        // 같은 앱이면 result.error 경로와 같은 인라인 표면을 쓰고 — 아니면 토스트.
        // 어느 쪽이든 반영 여부를 모르니 같은 앱이면 실제 상태를 재조회한다
        // (nonce 재조회는 미저장 입력·인라인 에러를 보존한다).
        if (clientRef.current === clientAtCall) {
          setSaveError(t('saveUnconfirmed'));
          setReloadNonce((n) => n + 1);
        } else {
          toast({ type: 'error', message: t('saveUnconfirmed'), auto_dismiss_ms: 4000 });
        }
      } finally {
        bumpOp('save', clientAtCall, -1);
      }
    });
  };

  // MODEL 축(model_aliases.allowed_clients): null = 제한 없음 / [] = 허용 앱 없음 / 목록 = 그 앱만.
  // 판정은 손으로 쓰지 않고 `modelAllowsClient` 를 지난다 — 예전엔 여기서 `=== null || includes`
  // 를 직접 썼고(맞는 판정이었지만) 같은 판정이 세 파일에 흩어져 있어서, 그중 하나만
  // `!m.allowed_clients` 로 "간단히" 바뀌는 순간 `[]`(전면 거부) 가 전 앱 허용으로 보인다.
  // 정본은 admin-api 의 조회 SQL(`allowed_clients IS NULL OR :client = ANY(allowed_clients)`,
  // routers/apps.py) 과 게이트웨이의 `check_client_model_scope` 다.
  const isAllowed = (m: AppModelRef) => modelAllowsClient(m.allowed_clients, selectedClient);

  /** 이 앱을 빼면 허용 앱이 하나도 남지 않는가 = 이 클릭이 전면 거부를 만드는가. */
  const isLastAllowedApp = (m: AppModelRef) => {
    // NULL(제한 없음) 에서는 절대 참이 아니다 — 서버가 전체 목록으로 materialize 한 뒤 이 앱만
    // 빼므로 다른 앱이 남는다(`_next_allowed_clients`). 그래서 'list' 만 본다.
    const scope = modelAppScope(m.allowed_clients);
    return scope.kind === 'list' && scope.clients.every((c) => c === selectedClient);
  };

  const doToggle = (alias: string, allowed: boolean, client?: string) => {
    const clientAtCall = client ?? selectedClient;
    bumpOp('save', clientAtCall, 1);
    startSaveTransition(async () => {
      try {
        const result = await toggleAppModelAction(clientAtCall, alias, allowed);
        if (result.success) {
          // 탭 전환 후 도착한 응답은 저장은 됐지만 화면(다른 앱)에 반영하면 안 된다.
          if (clientRef.current === clientAtCall) {
            const next = result.data;
            setPolicy(next);
            // 허용 해제로 select 의 입력값이 허용 목록 밖이 되면 서버값으로 재동기화한다.
            // 백엔드는 toggle 시 routing_profiles.default_model 을 건드리지 않으므로
            // default 가 허용 목록에서 빠지면 아래 카드의 경고 문구가 안내를 담당한다.
            setDefaultModelInput((prev) =>
              next.allowed_models.includes(prev) ? prev : (next.default_model ?? ''),
            );
          }
          toast({ type: 'success', message: t('toggleSaved', { app: CLIENT_LABELS[clientAtCall] ?? clientAtCall }), auto_dismiss_ms: 2500 });
        } else {
          toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
        }
      } catch {
        toast({ type: 'error', message: t('saveUnconfirmed'), auto_dismiss_ms: 4000 });
        if (clientRef.current === clientAtCall) setReloadNonce((n) => n + 1);
      } finally {
        bumpOp('save', clientAtCall, -1);
      }
    });
  };

  const handleToggleClick = (m: AppModelRef, nextAllowed: boolean) => {
    // 해제 클릭 하나가 두 가지 서로 다른 사고를 만들 수 있어서 확인 문구를 갈라 쓴다.
    // 서버가 다음 값을 계산하므로(admin-api `routers/apps.py::_next_allowed_clients`)
    // 여기서 배열을 만들지는 않고, "무엇이 될지" 만 판정한다:
    //
    //  (1) NULL 해제 → sorted(VALID_CLIENTS) 에서 이 앱만 뺀 **명시 목록** 으로 굳는다.
    //      이후 추가되는 앱은 그 목록에 없으므로 자동으로 거부된다 = 다른 앱의 미래 범위 변화.
    //  (2) 마지막 남은 앱 해제 → `[]` = 전면 거부. 게이트웨이가
    //      `check_client_model_scope` 를 fail-closed 로 바꾼 뒤(`if allowed is None: return`)
    //      이 값은 모든 앱에서 모델을 막는다. 이 화면이 전면 거부를 만드는 유일한 원클릭
    //      경로인데 예전엔 확인도, 구별되는 토스트도 없었다(성공 토스트 하나뿐).
    if (!nextAllowed && m.allowed_clients === null) {
      setPendingToggle({ alias: m.alias, allowed: false, kind: 'all', client: selectedClient });
      return;
    }
    if (!nextAllowed && isLastAllowedApp(m)) {
      setPendingToggle({ alias: m.alias, allowed: false, kind: 'last', client: selectedClient });
      return;
    }
    doToggle(m.alias, nextAllowed);
  };

  return (
    <div className="space-y-6">
      <div className="glass rounded-apple p-4">
        <p className="text-xs font-medium text-muted-foreground mb-2">{t('selectApp')}</p>
        <div
          role="group"
          aria-label={t('selectApp')}
          className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1"
        >
          {CLIENTS.map(({ value, label }) => (
            <button
              key={value}
              type="button"
              onClick={() => handleClientChange(value)}
              aria-pressed={selectedClient === value}
              className={[
                'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
                selectedClient === value
                  ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
                  : 'text-muted-foreground interactive',
              ].join(' ')}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {!selectedClient && !isLoadPending && (
        <div className="glass rounded-apple p-4">
          <p className="text-sm text-muted-foreground">{t('selectPrompt')}</p>
        </div>
      )}
      {isLoadPending && <SkeletonCard count={3} />}
      {loadError && (
        <div className="flex items-center gap-3">
          <FormError error={loadError} />
          <button
            type="button"
            onClick={() => setReloadNonce((n) => n + 1)}
            className="text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded"
          >
            {tc('retry')}
          </button>
        </div>
      )}

      {shown && !isLoadPending && (
        <div className="space-y-6">
          {/* 기본 모델은 아래 폼이 현재 저장값을 그대로 보여주므로 카드는 중복 — 제외 (리뷰 MED) */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="glass glass-hover rounded-apple p-4 flex flex-col gap-2">
              <p className="text-sm font-medium text-muted-foreground">{t('allowedModels')}</p>
              <p className="text-2xl font-bold num">{shown.allowed_models.length}</p>
            </div>
            <div className="glass glass-hover rounded-apple p-4 flex flex-col gap-2">
              <p className="text-sm font-medium text-muted-foreground">{t('allowedUsers')}</p>
              <p className="text-2xl font-bold num">{shown.allowed_users.length}</p>
            </div>
          </div>

          <div className="glass rounded-apple p-4 space-y-3">
            <h2 className="text-sm font-semibold flex items-center gap-1">
              {t('defaultModel')}
              <InfoTooltip label={t('defaultModel')} side="top">{t('defaultModelHint')}</InfoTooltip>
            </h2>
            <div className="flex items-center gap-3">
              <select
                value={defaultModelInput}
                onChange={(e) => setDefaultModelInput(e.target.value)}
                disabled={shown.allowed_models.length === 0}
                className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
              >
                <option value="">{t('selectPlaceholder')}</option>
                {shown.allowed_models.map((alias) => (
                  <option key={alias} value={alias}>{alias}</option>
                ))}
              </select>
              <SpinnerButton
                type="button"
                isLoading={isSaving}
                onClick={handleSaveDefaultModel}
                disabled={!defaultModelInput || defaultModelInput === shown.default_model}
              >
                {tc('save')}
              </SpinnerButton>
            </div>
            {shown.allowed_models.length === 0 && (
              // select 가 disabled 인 이유가 없으면 고장으로 보인다 — 원인 안내.
              <p className="text-xs text-muted-foreground">{t('noAllowedModelsHint')}</p>
            )}
            {shown.default_model &&
              !shown.allowed_models.includes(shown.default_model) && (
                // 방금 허용 해제된 모델이 기본 모델로 남은 상태 — 백엔드는 그대로
                // 두므로 gateway 가 비허용 모델로 라우팅할 수 있다. 재설정 안내.
                <p className="text-xs font-medium text-warning">
                  {t('defaultModelNotAllowedHint')}
                </p>
              )}
            {saveError && <FormError error={saveError} />}
          </div>

          <div className="glass rounded-apple p-4 flex items-center justify-between">
            <div>
              <h2 className="text-sm font-semibold">{t('webSearchTitle')}</h2>
              <p className="text-xs text-muted-foreground mt-0.5">{t('webSearchHint')}</p>
            </div>
            <label className="flex items-center gap-2 cursor-pointer">
              <div className="relative inline-flex items-center">
                <input
                  type="checkbox"
                  checked={shown.web_search_enabled}
                  onChange={(e) => handleToggleWebSearch(e.target.checked)}
                  disabled={isWsSaving}
                  className="sr-only peer"
                  aria-label={t('webSearchTitle')}
                />
                <div className="w-9 h-5 bg-muted-foreground/30 peer-checked:bg-primary rounded-full transition-colors after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:w-4 after:h-4 after:bg-background after:rounded-full after:transition-transform peer-checked:after:translate-x-4 peer-focus-visible:ring-2 peer-focus-visible:ring-ring peer-focus-visible:ring-offset-1" />
              </div>
              <span className="text-sm">
                {shown.web_search_enabled ? t('webSearchOn') : t('webSearchOff')}
              </span>
            </label>
          </div>

          {/* 크로스계정 라우팅 — routing_profiles.account_role_arn/external_id/region.
              모델별 라우팅(모델 수정 화면)보다 낮은 우선순위의 앱 단위 기본값. */}
          <div className="glass rounded-apple p-4 space-y-3">
            <div>
              <h2 className="text-sm font-semibold">{t('routingTitle')}</h2>
              <p className="text-xs text-muted-foreground mt-0.5">{t('routingHint')}</p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1 sm:col-span-2">
                <label htmlFor="routing-arn" className="text-xs text-muted-foreground">
                  {t('routingArnLabel')}
                </label>
                <input
                  id="routing-arn"
                  type="text"
                  value={routingForm.arn}
                  onChange={(e) =>
                    setRoutingForm((p) => ({ ...p, arn: e.target.value, dirty: true }))
                  }
                  maxLength={512}
                  placeholder="arn:aws:iam::123456789012:role/name"
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                />
              </div>
              <div className="space-y-1">
                <label htmlFor="routing-external-id" className="text-xs text-muted-foreground">
                  {t('routingExternalIdLabel')}
                </label>
                <input
                  id="routing-external-id"
                  type="text"
                  value={routingForm.eid}
                  onChange={(e) =>
                    setRoutingForm((p) => ({ ...p, eid: e.target.value, dirty: true }))
                  }
                  maxLength={256}
                  placeholder={t('routingExternalIdPlaceholder')}
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                />
              </div>
              <div className="space-y-1">
                <label htmlFor="routing-region" className="text-xs text-muted-foreground">
                  {t('routingRegionLabel')}
                </label>
                <input
                  id="routing-region"
                  type="text"
                  value={routingForm.region}
                  onChange={(e) =>
                    setRoutingForm((p) => ({ ...p, region: e.target.value, dirty: true }))
                  }
                  maxLength={64}
                  placeholder="ap-south-1"
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                />
              </div>
            </div>
            {routingError && <FormError error={routingError} />}
            <div className="flex justify-end">
              <SpinnerButton
                type="button"
                onClick={handleSaveRouting}
                isLoading={isRoutingSaving}
                disabled={!routingForm.region.trim()}
              >
                {tc('save')}
              </SpinnerButton>
            </div>
          </div>

          <div className="space-y-3">
            <div className="flex items-baseline justify-between gap-2">
              <h2 className="text-sm font-semibold">{t('modelManagement')}</h2>
              <p className="text-xs text-muted-foreground">{t('chipLegend')}</p>
            </div>
            <div className="glass rounded-apple overflow-hidden">
              {/* 모델 수만큼 길어지는 표 — max-h + overflow-y 로 스크롤시키고
                  헤더는 sticky 로 고정(bg-card 로 아래 행을 가림). */}
              <Table wrapperClassName="max-h-96 overflow-y-auto">
                <THead className="[&_th]:sticky [&_th]:top-0 [&_th]:z-10 [&_th]:bg-card">
                  <Tr>
                    <Th>{t('colAlias')}</Th>
                    <Th>{t('colAllowAll')}</Th>
                    <Th>{t('colAllowThisApp')}</Th>
                  </Tr>
                </THead>
                <TBody>
                  {shown.all_models.length === 0 ? (
                    <TEmpty colSpan={3}>{t('noModels')}</TEmpty>
                  ) : (
                    shown.all_models.map((m) => (
                      <Tr key={m.alias}>
                        <Td emphasis className="font-mono mono-id text-xs">{m.alias}</Td>
                        <Td>
                          {/* 앱 상태 = 앱별 고정 칩 3개 — 켜짐(teal)/꺼짐(dim)으로 커버리지를
                              한눈에 읽게 한다. 예전 표시(전체 배지/전면 거부 배지/— 대시)는
                              부분 허용의 "어떤 앱인지" 를 숨기고 세 상태의 형식이 달라
                              행 비교가 안 됐다.
                              NULL(전체 허용) 은 세 칩이 모두 켜진 것과 같지만 "향후 추가 앱도
                              포함" 이라는 별도 의미가 있으므로 title/sr-only 로 구분한다. */}
                          {(() => {
                            const scope = modelAppScope(m.allowed_clients);
                            const hint =
                              scope.kind === 'unrestricted'
                                ? t('appStatusUnrestrictedHint')
                                : undefined;
                            return (
                              <span
                                className="inline-flex items-center gap-1"
                                title={hint}
                              >
                                {GATEWAY_CLIENTS.map((c) => {
                                  const on = modelAllowsClient(m.allowed_clients, c);
                                  return (
                                    <span
                                      key={c}
                                      title={CLIENT_LABELS[c] ?? c}
                                      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium ${
                                        on
                                          ? 'bg-teal-500/15 text-teal-700 dark:text-teal-300'
                                          : 'bg-muted/50 text-muted-foreground/50 line-through'
                                      }`}
                                    >
                                      <span aria-hidden="true">{CLIENT_SHORT_LABELS[c]}</span>
                                      {/* 칩은 색·취소선으로만 상태를 보여준다 — 스크린리더에는
                                          풀 앱 이름 + 상태를 읽어준다("CC 허용됨" 대신
                                          "Claude Code: 허용됨"). */}
                                      <span className="sr-only">
                                        {CLIENT_LABELS[c] ?? c}: {on ? t('chipAllowed') : t('chipBlocked')}
                                      </span>
                                    </span>
                                  );
                                })}
                                {hint && <span className="sr-only">{hint}</span>}
                              </span>
                            );
                          })()}
                        </Td>
                        <Td>
                          <input
                            type="checkbox"
                            className="h-4 w-4"
                            aria-label={m.alias}
                            checked={isAllowed(m)}
                            disabled={isSaving}
                            onChange={(e) => handleToggleClick(m, e.target.checked)}
                          />
                        </Td>
                      </Tr>
                    ))
                  )}
                </TBody>
              </Table>
            </div>
          </div>

          <div className="space-y-3">
            <h2 className="text-sm font-semibold">{t('allowedUsers')}</h2>
            <div className="glass rounded-apple overflow-hidden">
              {/* 유저 수만큼 길어진다 — 모델 표와 같은 스크롤+sticky 헤더. */}
              <Table wrapperClassName="max-h-96 overflow-y-auto">
                <THead className="[&_th]:sticky [&_th]:top-0 [&_th]:z-10 [&_th]:bg-card">
                  <Tr><Th>{t('colUser')}</Th><Th>{t('colAccess')}</Th><Th>ID</Th></Tr>
                </THead>
                <TBody>
                  {shown.allowed_users.length === 0 ? (
                    <TEmpty colSpan={3}>{t('noAllowedUsers')}</TEmpty>
                  ) : (
                    shown.allowed_users.map((u) => (
                      <Tr key={u.user_id}>
                        <Td emphasis>
                          {/* 유저 정책 편집으로 이어지는 딥링크 — OrgTreeView 가
                              ?node=<user_id> 로 선택 복원+조상 펼침을 한다. */}
                          <Link
                            href={`/users?node=${u.user_id}`}
                            className="hover:underline underline-offset-4 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded"
                          >
                            {u.email ?? (
                              <span className="text-muted-foreground">{t('noEmail')}</span>
                            )}
                          </Link>
                        </Td>
                        <Td>
                          <span
                            className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${
                              u.explicit
                                ? 'bg-teal-500/15 text-teal-700 dark:text-teal-300'
                                : 'bg-muted text-muted-foreground'
                            }`}
                          >
                            {u.explicit ? t('accessExplicit') : t('accessUnrestricted')}
                          </span>
                        </Td>
                        <Td className="font-mono mono-id text-xs text-muted-foreground">{u.user_id}</Td>
                      </Tr>
                    ))
                  )}
                </TBody>
              </Table>
            </div>
          </div>
        </div>
      )}
      <ConfirmDialog
        isOpen={pendingToggle !== null}
        onClose={() => setPendingToggle(null)}
        onConfirm={() => {
          if (pendingToggle) doToggle(pendingToggle.alias, pendingToggle.allowed, pendingToggle.client);
          setPendingToggle(null);
        }}
        title={pendingToggle?.kind === 'last' ? t('confirmLastTitle') : t('confirmAllTitle')}
        message={
          pendingToggle?.kind === 'last'
            ? t('confirmLastMessage', { alias: pendingToggle.alias })
            : t('confirmAllMessage')
        }
        confirmLabel={t('confirmAllLabel')}
        isDestructive
      />
    </div>
  );
}
