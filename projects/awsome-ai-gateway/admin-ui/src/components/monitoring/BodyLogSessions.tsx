'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useCallback, useEffect, useRef, useState, useTransition } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { ChevronDown, ChevronRight, Link2, RefreshCw } from 'lucide-react';

import {
  fetchBodyLog,
  fetchBodyLogSessions,
  fetchBodyLogBatch,
  type BodyLogBatchItem,
  type BodyLogResponse,
  type BodyLogResult,
  type BodyLogSession,
} from '@/lib/actions/body-logs';
import { Badge } from '@/components/common/Badge';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateTime, fmtTokensCompact, fmtUsd } from '@/lib/utils/format';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { BodyRecordContent, statusTone } from './bodyLogRender';

// 한 호출을 펼치면 그 호출만 단건 조회한다 — 클릭 한 번으로 50건 열람이 감사되는
// 구조(eager batch)를 피하기 위해서다. 실제로 읽은 것만 감사된다.
const LOAD_ALL_CHUNK = 50;

/** 배치 아이템 → 단건 뷰가 기대하는 BodyLogResponse 형태. */
function toBodyLogResponse(
  requestId: string,
  item: BodyLogBatchItem,
  incomplete: boolean,
): BodyLogResponse {
  return {
    found: true,
    request_id: requestId,
    source: item.source,
    key: item.key,
    duplicates: item.duplicates,
    oversized: false,
    incomplete,
    model_alias: item.model_alias ?? '',
    user_email: item.user_email,
    requested_at: item.requested_at ?? '',
    record: item.record,
    cost_usd: item.cost_usd,
    latency_ms: item.latency_ms,
    ttft_ms: item.ttft_ms,
    client: item.client,
    input_tokens: item.input_tokens,
    output_tokens: item.output_tokens,
    cache_read_tokens: item.cache_read_tokens,
    reasoning_tokens: item.reasoning_tokens,
  };
}

export interface SessionFilters {
  days: number;
  user: string;
  client: string;
  hasError: boolean;
}

export function BodyLogSessions({
  initialSessionKey,
  initialFilters,
  onSessionChange,
  onFiltersApplied,
}: {
  /** ?session= 딥링크 — 로드 후 자동 오픈 대상 세션 key. */
  initialSessionKey?: string | null;
  /** URL 에 실려 온 적용 필터 초기값 — 공유 링크가 같은 목록을 연다. */
  initialFilters?: SessionFilters;
  /** 세션 오픈/닫힘을 부모(URL)에 알린다. */
  onSessionChange?: (_key: string | null) => void;
  /** 적용된 필터를 부모(URL)에 동기화 — 공유 가능한 목록 링크. */
  onFiltersApplied?: (_f: SessionFilters) => void;
}) {
  const t = useTranslations('monitoring.bodyLogs');
  const locale = useLocale();
  const tz = useReportingTz();
  const { toast } = useToast();

  const [filters, setFilters] = useState<SessionFilters>(
    initialFilters ?? { days: 1, user: '', client: '', hasError: false },
  );
  const [sessions, setSessions] = useState<BodyLogSession[] | null>(null);
  // 목록 조회 실패 — sessions=null(로딩)과 구분해야 실패 시 "불러오는 중…"이
  // 무한히 뜨지 않는다.
  const [loadFailed, setLoadFailed] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [callsTruncated, setCallsTruncated] = useState(false);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, startTransition] = useTransition();
  const [loadingMore, startLoadMore] = useTransition();

  // 페이지를 연 채 토글이 꺼지면 백엔드가 403 을 준다 — 토스트만으로 넘기면
  // 화면은 살아있는 것처럼 보이므로 비활성 안내로 전환한다.
  const [toggledOff, setToggledOff] = useState(false);

  const [selected, setSelected] = useState<BodyLogSession | null>(null);
  const [bodies, setBodies] = useState<Record<string, BodyLogResult>>({});
  // 마지막 loadAll 배치가 incomplete 였는지 — 못 찾은 구간이 있음을 헤더에 표시.
  const [batchIncomplete, setBatchIncomplete] = useState(false);
  const [loadingIds, setLoadingIds] = useState<Set<string>>(new Set());
  const [openCalls, setOpenCalls] = useState<Set<string>>(new Set());
  // ?session= 딥링크 자동 오픈은 마운트 후 목록 로드 완료 시 한 번만.


  const [userTruncated, setUserTruncated] = useState(false);
  // "더 보기"는 적용 시점의 필터로 가져와야 한다 — 입력창의 미적용 값과
  // 섞이면 1페이지와 다른 조건으로 2페이지를 긁는다.
  const [appliedFilters, setAppliedFilters] = useState<SessionFilters>(
    initialFilters ?? { days: 1, user: '', client: '', hasError: false },
  );

  const applyResult = (
    data: {
      sessions: BodyLogSession[];
      truncated: boolean;
      calls_truncated: boolean;
      has_more: boolean;
      next_before: string | null;
      user_filter_truncated?: boolean;
    },
    append: boolean,
  ) => {
    setSessions((prev) => {
      if (!append || !prev) return data.sessions;
      const seen = new Set(prev.map((s) => s.key));
      return [...prev, ...data.sessions.filter((s) => !seen.has(s.key))];
    });
    setTruncated(data.truncated);
    setCallsTruncated(data.calls_truncated);
    setUserTruncated(data.user_filter_truncated ?? false);
    setHasMore(data.has_more);
    setNextBefore(data.next_before);
  };

  const load = useCallback(
    (f: SessionFilters, before?: string, beforeKeys?: string[]) => {
      const run = before ? startLoadMore : startTransition;
      run(async () => {
        const res = await fetchBodyLogSessions({
          days: f.days,
          user: f.user || undefined,
          client: f.client || undefined,
          hasError: f.hasError || undefined,
          before,
          beforeKeys,
        });
        if (res.success && res.data) {
          setLoadFailed(false);
          applyResult(res.data, Boolean(before));
        } else if (res.status === 403) {
          setToggledOff(true);
        } else {
          // 실패를 토스트만으로 넘기면 sessions=null 이 유지돼 "불러오는 중"이
          // 영구 표시된다 — 명시 실패 상태로 전환해 재시도를 제공한다.
          // 단 더보기(before) 실패는 기존 목록을 유지한 채 토스트만 띄운다.
          if (!before) setLoadFailed(true);
          toast({ type: 'error', message: res.error ?? t('fetchFailed'), auto_dismiss_ms: 6000 });
        }
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t],
  );

  useEffect(() => {
    load(filters);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 뒤로가기로 ?session= 이 사라지면 trace 도 닫는다 — 세션 오픈은 pushState
  // 라 뒤로가기가 "목록으로"로 읽혀야 일관된다.
  const deepLinkKey = useRef<string | null>(null);
  const prevSessionKey = useRef(initialSessionKey);
  useEffect(() => {
    if (prevSessionKey.current && !initialSessionKey && selected) {
      setSelected(null);
      deepLinkKey.current = null;
    }
    prevSessionKey.current = initialSessionKey;
  }, [initialSessionKey, selected]);

  // ── 딥링크 자동 오픈 — 목록 로드 후 key 매칭, 없으면 재조회 ──
  // 뒤로가기/앞으로가기로 같은 key 가 나갔다 들어와도 다시 시도하게,
  // "시도한 key"를 추적한다(한 번만이 아니라 key 당 한 번).
  useEffect(() => {
    if (!initialSessionKey) {
      deepLinkKey.current = null;
      return;
    }
    if (deepLinkKey.current === initialSessionKey || sessions === null) return;
    deepLinkKey.current = initialSessionKey;
    const found = sessions.find((s) => s.key === initialSessionKey);
    if (found) {
      openSession(found, false);
      return;
    }
    startTransition(async () => {
      // key 형식: 명시 세션은 sid|<session_id>|<user>|<client>,
      // 휴리스틱은 <user>|<client>|<ended_at iso>. 둘 다 소유자 검증을 위해
      // 복합키 정확 일치만 연다 — 부분 일치는 링크가 낡은 것.
      // days=0 — 기간 제한 없음(딥링크는 필터 창과 무관하게 찾아야 한다).
      if (initialSessionKey.startsWith('sid|')) {
        const sid = initialSessionKey.split('|')[1];
        const res = await fetchBodyLogSessions({ sid, days: 0 });
        const s = res.success
          ? res.data?.sessions.find((x) => x.key === initialSessionKey)
          : undefined;
        if (s) openSession(s, false);
        else toast({ type: 'info', message: t('sessionNotFound'), auto_dismiss_ms: 5000 });
        return;
      }
      // 휴리스틱 key — codex/cowork 는 session_id 가 NULL 이라 전부 이 경로.
      // 소유자(user, client)로 좁혀 재조회 — 기본 1일·20건 목록 밖이어도 찾는다.
      const parts = initialSessionKey.split('|');
      if (parts.length !== 3) {
        toast({ type: 'info', message: t('sessionNotFound'), auto_dismiss_ms: 5000 });
        return;
      }
      const [u, c] = parts;
      const res = await fetchBodyLogSessions({
        days: 0,
        user: u === 'none' ? undefined : u,
        client: c || undefined,
      });
      const s = res.success
        ? res.data?.sessions.find((x) => x.key === initialSessionKey)
        : undefined;
      if (s) openSession(s, false);
      else toast({ type: 'info', message: t('sessionNotFound'), auto_dismiss_ms: 5000 });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions, initialSessionKey]);

  const openSession = (s: BodyLogSession, syncUrl = true) => {
    setSelected(s);
    setBodies({});
    setLoadingIds(new Set());
    setOpenCalls(new Set());
    setBatchIncomplete(false);
    setErrorsOnly(false);
    // 딥링크 effect 가 여는 경우 URL 에 이미 ?session= 이 있다 — pushState 가
    // 한 번 더 돌면 같은 주소가 히스토리에 중복으로 쌓인다.
    if (syncUrl) onSessionChange?.(s.key);
  };

  const closeSession = () => {
    setSelected(null);
    onSessionChange?.(null);
  };

  // trace 새로고침 — 명시 세션은 sid 직행(복합키 재매칭), 휴리스틱은
  // (user, client, started_at) 재매칭 — ended_at 기반 key 는 세션이 자라면
  // 바뀌어 정작 새 호출이 생겼을 때 찾지 못한다.
  const refreshTrace = () => {
    if (!selected) return;
    startTransition(async () => {
      if (selected.session_id) {
        const res = await fetchBodyLogSessions({
          sid: selected.session_id,
          days: 0,
        });
        const s = res.success
          ? res.data?.sessions.find((x) => x.key === selected.key)
          : undefined;
        if (s) {
          setSelected(s);
          return;
        }
      } else {
        const res = await fetchBodyLogSessions({
          days: appliedFilters.days,
          // 세션 소유자로 좁혀 1페이지 결과로 찾는다 — "더 보기"로 연 세션도
          // 사용자 필터가 걸리면 상위에 올라온다(user_id uuid 부분일치).
          user: appliedFilters.user || selected.user_id || undefined,
          client: appliedFilters.client || undefined,
          hasError: appliedFilters.hasError || undefined,
        });
        const s = res.success
          ? res.data?.sessions.find(
              (x) =>
                x.session_id === null &&
                x.user_id === selected.user_id &&
                x.client === selected.client &&
                x.started_at === selected.started_at,
            )
          : undefined;
        if (s) {
          setSelected(s);
          return;
        }
      }
      toast({ type: 'info', message: t('sessionNotFound'), auto_dismiss_ms: 4000 });
    });
  };

  const ensureBody = (rid: string, force = false) => {
    // 진행 중이면 force 여도 중복 조회·감사가 생기므로 가드는 유지한다.
    if (loadingIds.has(rid) || (!force && bodies[rid])) return;
    setLoadingIds((prev) => new Set(prev).add(rid));
    fetchBodyLog(rid)
      .then((res) => {
        setBodies((prev) => ({ ...prev, [rid]: res }));
      })
      .catch((err) => {
        // server action 자체가 reject 되면 실패 카드로 남긴다 — 아니면
        // loadingIds 에 영구히 남아 스피너가 멈추지 않는다.
        setBodies((prev) => ({
          ...prev,
          [rid]: { success: false, error: err instanceof Error ? err.message : String(err) },
        }));
      })
      .finally(() => {
        setLoadingIds((prev) => {
          const n = new Set(prev);
          n.delete(rid);
          return n;
        });
      });
  };

  const toggleCall = (rid: string) => {
    // updater 안에서 fetch 를 부르면 StrictMode 이중 호출로 열람·감사가 두 번
    // 생긴다 — 펼치는 경우에만 updater 밖에서 조회한다.
    if (!openCalls.has(rid)) ensureBody(rid);
    setOpenCalls((prev) => {
      const next = new Set(prev);
      if (next.has(rid)) next.delete(rid);
      else next.add(rid);
      return next;
    });
  };

  // 명시적 일괄 열람 — 클릭 = 50건 열람 의사로 감사된다(정직한 감사).
  const loadAll = () => {
    if (!selected) return;
    const pending = selected.calls
      .map((c) => c.request_id)
      .filter((rid) => !bodies[rid] && !loadingIds.has(rid))
      .slice(0, LOAD_ALL_CHUNK);
    if (!pending.length) return;
    // 클릭 결과가 화면에 보이게 불러오는 호출을 전부 펼친다 — 그냥 두면
    // 50건 열람 감사가 나가는 클릭인데 아무 변화가 없어 재클릭을 유도한다.
    setOpenCalls((prev) => new Set([...prev, ...pending]));
    setLoadingIds((prev) => new Set([...prev, ...pending]));
    fetchBodyLogBatch(pending)
      .then((res) => {
        if (res.success) setBatchIncomplete(res.data?.incomplete ?? false);
        setBodies((prev) => {
          const next = { ...prev };
          for (const rid of pending) {
            const item = res.success ? res.data?.records[rid] : undefined;
            if (item) {
              // found — batch incomplete 은 "못 찾은 구간" 얘기이므로
              // 찾은 항목에는 붙이지 않는다(오안내 방지).
              next[rid] = { success: true, data: toBodyLogResponse(rid, item, false) };
            } else if (res.success && res.data?.oversized.includes(rid)) {
              // oversized — 재시도해도 같은 결과라 성공 경로 카드로 렌더한다.
              next[rid] = {
                success: true,
                data: {
                  found: false,
                  request_id: rid,
                  source: null,
                  key: null,
                  duplicates: 0,
                  oversized: true,
                  model_alias:
                    selected.calls.find((c) => c.request_id === rid)?.model_alias ?? '',
                  requested_at:
                    selected.calls.find((c) => c.request_id === rid)?.requested_at ?? '',
                  record: null,
                },
              };
            } else if (res.success) {
              // missing — "미수집"이 아니라 "못 찾음"(수집대기/미스캔 가능).
              next[rid] = { success: false, error: t('bodyMissing'), status: 404 };
            } else {
              next[rid] = { success: false, error: res.error ?? t('fetchFailed'), status: res.status };
            }
          }
          return next;
        });
      })
      .catch((err) => {
        setBodies((prev) => {
          const next = { ...prev };
          for (const rid of pending) {
            next[rid] = {
              success: false,
              error: err instanceof Error ? err.message : String(err),
            };
          }
          return next;
        });
      })
      .finally(() => {
        setLoadingIds((prev) => {
          const n = new Set(prev);
          for (const rid of pending) n.delete(rid);
          return n;
        });
      });
  };

  const applyFilters = () => {
    setAppliedFilters(filters);
    onFiltersApplied?.(filters);
    setSessions(null);
    setLoadFailed(false);
    setNextBefore(null);
    load(filters);
  };

  // trace 헤더의 에러 배지를 누르면 에러 호출만 — 500행을 스크롤하지 않고
  // 실패 지점으로 바로 간다. 이미 로드된 목록의 클라이언트 필터라 추가
  // 열람·감사가 생기지 않는다(에러 판정은 백엔드와 같이 status != SUCCESS).
  const [errorsOnly, setErrorsOnly] = useState(false);

  // ── 세션 trace 뷰 ────────────────────────────────────────────────────────────
  if (selected) {
    const remaining = selected.calls.filter(
      (c) => !bodies[c.request_id] && !loadingIds.has(c.request_id),
    ).length;
    const callsCut = selected.call_count > selected.calls.length;
    // 원본 호출 번호(#n)를 유지한 채 필터한다 — 에러만 보여도 번호가 세션
    // 타임라인 위치를 가리키게. 표시는 최신 호출이 위로 오게 뒤집는다 —
    // 조사는 보통 최근 실패/응답부터 시작하기 때문(번호는 원본 위치 유지).
    const shownCalls = selected.calls
      .map((call, idx) => ({ call, idx }))
      .filter(({ call }) => !errorsOnly || (call.status && call.status !== 'SUCCESS'))
      .reverse();
    // historyStart — 이전 호출의 요청 메시지가 이번 호출의 접두일 때만 그 길이를
    // 넘긴다(msgIndex 단위). Claude Code 의 사이드 호출(haiku 판정/서브에이전트)이
    // 끼면 바로 앞 호출과 접두가 어긋나므로, 접두가 맞는 가장 최근 호출을
    // 뒤로 찾는다. /compact 로 history 가 재작성되면 어느 호출과도 접두가 안
    // 맞아 0 — 그때 숨기면 새 대화가 가려지므로 안전한 기본값이다.
    // cache_control 은 호출마다 위치가 바뀔 수 있어 비교에서 제외한다.
    // 표시는 최신순으로 뒤집지만 접두 검증은 시간 오름차순으로만 성립하므로
    // 오름차순 선행 패스로 계산해 request_id 별로 저장한다.
    const historyStartByRid = new Map<string, number>();
    {
      const prevCallsMsgs: (unknown[] | null)[] = [];
      const normMsg = (m: unknown) =>
        JSON.stringify(m, (k, v) => (k === 'cache_control' ? undefined : v));
      for (const call of selected.calls) {
        const rb = bodies[call.request_id]?.data?.record?.request_body as
          | { messages?: unknown[]; input?: unknown }
          | undefined;
        // (messages[] 와 responses API input[] 두 방언 모두 센다)
        const msgs = Array.isArray(rb?.messages)
          ? rb.messages
          : Array.isArray(rb?.input)
            ? (rb.input as unknown[])
            : null;
        // 접두 검증: 앞쪽 호출 중 msgs 가 이번 호출의 접두인 가장 최근 것을
        // 찾는다 — 경계 메시지 하나만 같으면 충분하다(전체 비교는 비싸다).
        let historyStart = 0;
        if (msgs != null) {
          for (
            let j = prevCallsMsgs.length - 1;
            j >= Math.max(0, prevCallsMsgs.length - 20);
            j--
          ) {
            const pm = prevCallsMsgs[j];
            if (
              pm != null &&
              pm.length < msgs.length &&
              normMsg(msgs[pm.length - 1]) === normMsg(pm[pm.length - 1])
            ) {
              historyStart = pm.length;
              break;
            }
          }
        }
        prevCallsMsgs.push(msgs);
        historyStartByRid.set(call.request_id, historyStart);
      }
    }
    return (
      <div className="space-y-4">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={closeSession}
            className="text-sm text-primary hover:underline"
          >
            {t('backToSessions')}
          </button>
          {/* 이 trace 는 ?session= 딥링크로 열린 상태라 현재 URL 이 그대로
              공유 링크다 — 수동으로 주소창을 복사할 필요가 없게 버튼을 둔다. */}
          <button
            type="button"
            onClick={() => {
              navigator.clipboard
                .writeText(window.location.href)
                .then(() =>
                  toast({ type: 'success', message: t('linkCopied'), auto_dismiss_ms: 2500 }),
                )
                .catch(() => {});
            }}
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <Link2 size={12} aria-hidden="true" />
            {t('copyLink')}
          </button>
        </div>

        {/* 세션 헤더 */}
        <div className="glass rounded-apple p-4">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{selected.user_email ?? selected.user_id ?? '-'}</span>
            {selected.client && <Badge tone="neutral">{selected.client}</Badge>}
            {selected.session_id === null && (
              <span title={t('heuristicSessionTitle')}>
                <Badge tone="amber">{t('heuristicSession')}</Badge>
              </span>
            )}
            <span className="text-muted-foreground">
              {fmtDateTime(selected.started_at, locale, tz)} → {fmtDateTime(selected.ended_at, locale, tz)}
            </span>
            <Badge tone="neutral">{t('callsCount', { count: selected.call_count })}</Badge>
            {selected.error_count > 0 && (
              <button
                type="button"
                aria-pressed={errorsOnly}
                title={t('errorsOnlyTitle')}
                onClick={() => setErrorsOnly((v) => !v)}
                className={errorsOnly ? 'rounded-full ring-1 ring-pink-500' : ''}
              >
                <Badge tone="pink">{t('errorsCount', { count: selected.error_count })}</Badge>
              </button>
            )}
            {errorsOnly && (
              <button
                type="button"
                onClick={() => setErrorsOnly(false)}
                className="text-xs text-primary hover:underline"
              >
                {t('errorsOnlyOff')}
              </button>
            )}
            {selected.total_cost_usd != null && (
              <Badge tone="neutral">{fmtUsd(selected.total_cost_usd)}</Badge>
            )}
            {callsCut && (
              <Badge tone="amber">
                {t('callsTruncated', { shown: selected.calls.length, total: selected.call_count })}
              </Badge>
            )}
            {batchIncomplete && <Badge tone="amber">{t('scanIncomplete')}</Badge>}
            <button
              type="button"
              onClick={refreshTrace}
              disabled={loading}
              className="ml-auto inline-flex items-center gap-1 text-xs text-primary hover:underline disabled:opacity-50"
            >
              <RefreshCw size={12} aria-hidden="true" />
              {t('refresh')}
            </button>
          </div>
          {remaining > 0 && (
            <button
              type="button"
              onClick={loadAll}
              className="mt-2 text-xs text-primary hover:underline"
            >
              {t('loadAllBodies', { count: Math.min(remaining, LOAD_ALL_CHUNK) })}
            </button>
          )}
        </div>

        {/* 호출 타임라인 */}
        <div className="space-y-2">
          {shownCalls.map(({ call, idx: i }) => {
            const res = bodies[call.request_id];
            const isLoading = loadingIds.has(call.request_id);
            const isOpen = openCalls.has(call.request_id);
            // 요청 messages 는 호출마다 전체 history 를 싣는다 — 선행 패스에서
            // 계산한 직전 호출까지의 턴 수를 넘겨 이번 호출의 새 턴만 보인다.
            const historyStart = historyStartByRid.get(call.request_id) ?? 0;
            return (
              <div key={call.request_id} className="glass rounded-apple">
                <button
                  type="button"
                  aria-expanded={isOpen}
                  onClick={() => toggleCall(call.request_id)}
                  className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left text-sm"
                >
                  {isOpen ? (
                    <ChevronDown size={14} aria-hidden="true" />
                  ) : (
                    <ChevronRight size={14} aria-hidden="true" />
                  )}
                  <span className="w-8 text-xs text-muted-foreground">#{i + 1}</span>
                  <span className="whitespace-nowrap">
                    {fmtDateTime(call.requested_at, locale, tz)}
                  </span>
                  <span>{call.model_alias ?? '-'}</span>
                  <Badge tone={statusTone(call.status ?? '')}>{call.status ?? '-'}</Badge>
                  {call.latency_ms != null && (
                    <span className="text-xs text-muted-foreground">
                      {(call.latency_ms / 1000).toFixed(1)}s
                    </span>
                  )}
                  {call.cost_usd != null && (
                    <span className="text-xs text-muted-foreground">
                      {fmtUsd(call.cost_usd)}
                    </span>
                  )}
                  {(call.input_tokens != null || call.output_tokens != null) && (
                    <span className="text-xs text-muted-foreground">
                      {fmtTokensCompact(call.input_tokens)}→{fmtTokensCompact(call.output_tokens)}
                    </span>
                  )}
                  {res?.data?.duplicates ? (
                    <Badge tone="neutral">
                      {t('duplicates', { count: res.data.duplicates })}
                    </Badge>
                  ) : null}
                  {res?.data?.incomplete && <Badge tone="amber">{t('scanIncomplete')}</Badge>}
                </button>
                {isOpen && (
                  <div className="border-t border-border px-4 py-3">
                    {isLoading ? (
                      <p className="text-sm text-muted-foreground">{t('loadingBody')}</p>
                    ) : res?.success && res.data ? (
                      res.data.oversized ? (
                        // 대형 레코드 — 재시도해도 같으므로 안내만.
                        <p className="text-xs text-amber-600 dark:text-amber-400">
                          {t('bodyOversized')}
                        </p>
                      ) : (
                        <BodyRecordContent
                          data={res.data}
                          historyStart={historyStart}
                          usageStatus={call.status}
                        />
                      )
                    ) : (
                      // 404/스캔미스 — 백엔드 hint 가 수집대기/미수집/미스캔을
                      // 구분해 주므로 그대로 보여주고 재시도를 둔다.
                      <div className="flex items-center gap-3 text-xs">
                        <p className="text-muted-foreground">
                          {res?.error ?? t('fetchFailed')}
                        </p>
                        <button
                          type="button"
                          onClick={() => ensureBody(call.request_id, true)}
                          className="text-primary hover:underline"
                        >
                          {t('retry')}
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  // ── 세션 목록 ────────────────────────────────────────────────────────────────
  if (toggledOff) {
    return (
      <div className="glass rounded-apple p-6">
        <p className="text-sm text-muted-foreground">{t('disabledNotice')}</p>
      </div>
    );
  }
  return (
    <div className="glass rounded-apple p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">{t('sessionsTitle')}</h2>
        <div className="ml-auto flex items-center gap-1">
          <button
            type="button"
            onClick={() => load(appliedFilters)}
            disabled={loading}
            className="inline-flex items-center gap-1 text-xs text-primary hover:underline disabled:opacity-50"
          >
            <RefreshCw size={12} aria-hidden="true" />
            {t('refresh')}
          </button>
        </div>
      </div>

      {/* 필터 바 — 1000명 규모에서 목적 세션을 찾는 유일한 경로 */}
      <form
        className="mb-3 flex flex-wrap items-center gap-2 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          applyFilters();
        }}
      >
        <select
          value={filters.days}
          onChange={(e) =>
            setFilters((f) => ({ ...f, days: Number(e.target.value) }))
          }
          className="rounded-md border border-border bg-background px-2 py-1.5 text-xs"
          aria-label={t('filterDays')}
        >
          {[1, 7, 30, 90].map((d) => (
            <option key={d} value={d}>
              {t('filterDaysValue', { days: d })}
            </option>
          ))}
        </select>
        <input
          type="text"
          value={filters.user}
          onChange={(e) => setFilters((f) => ({ ...f, user: e.target.value }))}
          placeholder={t('filterUserPlaceholder')}
          aria-label={t('filterUserPlaceholder')}
          className="w-44 rounded-md border border-border bg-background px-2 py-1.5 text-xs"
        />
        {/* 백엔드는 client 를 정확 일치로 거른다 — 자유 입력은 맞춰지지 않고
            조용히 0건이 되므로 실제 값 도메인을 select 로 고정한다.
            'other' 는 client 가 NULL 인 요청을 가리키는 백엔드 규칙이다. */}
        <select
          value={filters.client}
          onChange={(e) => setFilters((f) => ({ ...f, client: e.target.value }))}
          aria-label={t('filterClientPlaceholder')}
          className="rounded-md border border-border bg-background px-2 py-1.5 text-xs"
        >
          <option value="">{t('filterClientAll')}</option>
          {['claude-code', 'cowork', 'codex'].map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
          <option value="other">{t('filterClientOther')}</option>
        </select>
        <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={filters.hasError}
            onChange={(e) => setFilters((f) => ({ ...f, hasError: e.target.checked }))}
          />
          {t('filterErrorsOnly')}
        </label>
        <button
          type="submit"
          disabled={loading}
          className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
        >
          {t('applyFilters')}
        </button>
      </form>

      {loadFailed ? (
        <div className="flex items-center justify-center gap-2 p-4 text-sm text-muted-foreground">
          <span>{t('listLoadFailed')}</span>
          <button
            type="button"
            onClick={() => {
              setLoadFailed(false);
              setSessions(null);
              load(appliedFilters);
            }}
            className="inline-flex items-center gap-1 text-primary hover:underline underline-offset-2"
          >
            <RefreshCw size={12} aria-hidden="true" />
            {t('retry')}
          </button>
        </div>
      ) : sessions === null ? (
        <p className="text-sm text-muted-foreground">{t('loadingTrace')}</p>
      ) : sessions.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t('sessionsEmpty')}</p>
      ) : (
        <>
          {truncated && (
            <p className="mb-2 text-xs text-amber-600 dark:text-amber-400">
              {t('sessionsTruncated')}
            </p>
          )}
          {callsTruncated && (
            <p className="mb-2 text-xs text-amber-600 dark:text-amber-400">
              {t('sessionsCallsTruncated')}
            </p>
          )}
          {userTruncated && (
            <p className="mb-2 text-xs text-amber-600 dark:text-amber-400">
              {t('sessionsUserTruncated')}
            </p>
          )}
          <Table>
            <THead>
              <Tr>
                <Th className="w-6"></Th>
                <Th>{t('colEnded')}</Th>
                <Th>{t('colStarted')}</Th>
                <Th>{t('colUser')}</Th>
                <Th>{t('colClient')}</Th>
                <Th>{t('colCalls')}</Th>
                <Th>{t('colModels')}</Th>
                <Th>{t('colCost')}</Th>
                <Th>{t('colErrors')}</Th>
              </Tr>
            </THead>
            <TBody>
              {sessions.map((s) => (
                <Tr
                  key={s.key}
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => openSession(s)}
                >
                  {/* 행 클릭은 마우스 전용 편의 — 키보드·스크린리더는 이
                      버튼으로 연다(tr 에 keydown 을 달면 내부 요소의
                      Enter 까지 가로챈다 — AuditLogListView 와 같은 패턴). */}
                  <Td className="w-6 text-muted-foreground">
                    <button
                      type="button"
                      aria-label={t('openSession')}
                      onClick={(e) => {
                        e.stopPropagation();
                        openSession(s);
                      }}
                      className="rounded p-0.5 hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    >
                      <ChevronRight size={14} aria-hidden="true" />
                    </button>
                  </Td>
                  <Td className="whitespace-nowrap">{fmtDateTime(s.ended_at, locale, tz)}</Td>
                  <Td className="whitespace-nowrap text-muted-foreground">
                    {fmtDateTime(s.started_at, locale, tz)}
                  </Td>
                  <Td className="max-w-40 truncate text-sm" title={s.user_email ?? s.user_id ?? ''}>
                    {s.user_email ?? s.user_id ?? '-'}
                  </Td>
                  <Td className="text-sm">
                    {s.client ?? '-'}
                    {s.session_id === null && (
                      <span title={t('heuristicSessionTitle')}>
                        <Badge tone="amber" className="ml-1">{t('heuristicShort')}</Badge>
                      </span>
                    )}
                  </Td>
                  <Td className="text-sm">{s.call_count}</Td>
                  <Td className="max-w-48 truncate text-sm" title={s.models.join(', ')}>
                    {s.models.join(', ')}
                  </Td>
                  <Td className="text-sm">
                    {s.total_cost_usd != null ? fmtUsd(s.total_cost_usd) : '—'}
                  </Td>
                  <Td>
                    {s.error_count > 0 ? (
                      <Badge tone="pink">{s.error_count}</Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">0</span>
                    )}
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
          {hasMore && nextBefore && (
            <button
              type="button"
              onClick={() =>
                load(
                  appliedFilters,
                  nextBefore,
                  // 커서 시각에 끝난 세션은 이미 보유 — 서버가 타이로 다시 주지 않게 key를 보낸다.
                  (sessions ?? [])
                    .filter((s) => s.ended_at === nextBefore)
                    .map((s) => s.key),
                )
              }
              disabled={loadingMore}
              className="mt-3 text-sm text-primary hover:underline disabled:opacity-50"
            >
              {loadingMore ? t('loadingTrace') : t('loadMore')}
            </button>
          )}
        </>
      )}
    </div>
  );
}
