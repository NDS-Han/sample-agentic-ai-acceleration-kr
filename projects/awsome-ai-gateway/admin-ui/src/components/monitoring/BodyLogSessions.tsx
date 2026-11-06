'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useCallback, useEffect, useRef, useState, useTransition } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';

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

interface Filters {
  days: number;
  user: string;
  client: string;
  hasError: boolean;
}

export function BodyLogSessions({
  initialSessionKey,
  onSessionChange,
}: {
  /** ?session= 딥링크 — 로드 후 자동 오픈 대상 세션 key. */
  initialSessionKey?: string | null;
  /** 세션 오픈/닫힘을 부모(URL)에 알린다. */
  onSessionChange?: (_key: string | null) => void;
}) {
  const t = useTranslations('monitoring.bodyLogs');
  const locale = useLocale();
  const tz = useReportingTz();
  const { toast } = useToast();

  const [filters, setFilters] = useState<Filters>({
    days: 1,
    user: '',
    client: '',
    hasError: false,
  });
  const [sessions, setSessions] = useState<BodyLogSession[] | null>(null);
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
  const [loadingIds, setLoadingIds] = useState<Set<string>>(new Set());
  const [openCalls, setOpenCalls] = useState<Set<string>>(new Set());
  // ?session= 딥링크 자동 오픈은 마운트 후 목록 로드 완료 시 한 번만.
  const deepLinkTried = useRef(false);

  const [userTruncated, setUserTruncated] = useState(false);
  // "더 보기"는 적용 시점의 필터로 가져와야 한다 — 입력창의 미적용 값과
  // 섞이면 1페이지와 다른 조건으로 2페이지를 긁는다.
  const [appliedFilters, setAppliedFilters] = useState<Filters>({
    days: 1,
    user: '',
    client: '',
    hasError: false,
  });

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
    setSessions((prev) => (append && prev ? [...prev, ...data.sessions] : data.sessions));
    setTruncated(data.truncated);
    setCallsTruncated(data.calls_truncated);
    setUserTruncated(data.user_filter_truncated ?? false);
    setHasMore(data.has_more);
    setNextBefore(data.next_before);
  };

  const load = useCallback(
    (f: Filters, before?: string) => {
      const run = before ? startLoadMore : startTransition;
      run(async () => {
        const res = await fetchBodyLogSessions({
          days: f.days,
          user: f.user || undefined,
          client: f.client || undefined,
          hasError: f.hasError || undefined,
          before,
        });
        if (res.success && res.data) {
          applyResult(res.data, Boolean(before));
        } else if (res.status === 403) {
          setToggledOff(true);
        } else {
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

  // ── 딥링크 자동 오픈 — 목록 로드 후 key 매칭, 없으면 sid 직행 재조회 ──
  useEffect(() => {
    if (!initialSessionKey || deepLinkTried.current || sessions === null) return;
    deepLinkTried.current = true;
    const found = sessions.find((s) => s.key === initialSessionKey);
    if (found) {
      openSession(found);
      return;
    }
    // 목록 범위 밖이면 sid 직행 — key 형식은 sid|<session_id>|<user>|<client>.
    // session_id 는 클라이언트 제어라 소유자가 갈라질 수 있으므로 첫 행이 아니라
    // URL 의 복합키와 정확히 일치하는 세션만 연다 — 불일치는 링크가 낡은 것.
    const sid = initialSessionKey.startsWith('sid|')
      ? initialSessionKey.split('|')[1]
      : null;
    if (!sid) {
      toast({ type: 'info', message: t('sessionNotFound'), auto_dismiss_ms: 5000 });
      return;
    }
    startTransition(async () => {
      // days=0 — 기간 제한 없음(딥링크는 필터 창과 무관하게 찾아야 한다).
      const res = await fetchBodyLogSessions({ sid, days: 0 });
      const s = res.success
        ? res.data?.sessions.find((x) => x.key === initialSessionKey)
        : undefined;
      if (s) openSession(s);
      else toast({ type: 'info', message: t('sessionNotFound'), auto_dismiss_ms: 5000 });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessions, initialSessionKey]);

  const openSession = (s: BodyLogSession) => {
    setSelected(s);
    setBodies({});
    setLoadingIds(new Set());
    setOpenCalls(new Set());
    onSessionChange?.(s.key);
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
          user: appliedFilters.user || undefined,
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
    if (!force && (bodies[rid] || loadingIds.has(rid))) return;
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
    setLoadingIds((prev) => new Set([...prev, ...pending]));
    fetchBodyLogBatch(pending)
      .then((res) => {
        setBodies((prev) => {
          const next = { ...prev };
          for (const rid of pending) {
            const item = res.success ? res.data?.records[rid] : undefined;
            if (item) {
              // found — batch incomplete 은 "못 찾은 구간" 얘기이므로
              // 찾은 항목에는 붙이지 않는다(오안내 방지).
              next[rid] = { success: true, data: toBodyLogResponse(rid, item, false) };
            } else if (res.success && res.data?.oversized.includes(rid)) {
              next[rid] = { success: false, error: t('bodyOversized'), status: 200 };
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
    setSessions(null);
    setNextBefore(null);
    load(filters);
  };

  // ── 세션 trace 뷰 ────────────────────────────────────────────────────────────
  if (selected) {
    const remaining = selected.calls.filter(
      (c) => !bodies[c.request_id] && !loadingIds.has(c.request_id),
    ).length;
    const callsCut = selected.call_count > selected.calls.length;
    // historyStart — 직전 호출의 요청 메시지가 이번 호출의 접두일 때만 그 길이를
    // 넘긴다(msgIndex 단위). 서브에이전트 호출이 끼거나 /compact 로 history 가
    // 재작성되면 접두가 깨지는데, 그때 턴을 숨기면 새 대화가 가려진다.
    let prevMsgs: unknown[] | null = null;
    return (
      <div className="space-y-4">
        <button
          type="button"
          onClick={closeSession}
          className="text-sm text-primary hover:underline"
        >
          {t('backToSessions')}
        </button>

        {/* 세션 헤더 */}
        <div className="glass rounded-apple p-4">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{selected.user_email ?? selected.user_id ?? '-'}</span>
            {selected.client && <Badge tone="neutral">{selected.client}</Badge>}
            {selected.session_id === null && (
              <Badge tone="amber">{t('heuristicSession')}</Badge>
            )}
            <span className="text-muted-foreground">
              {fmtDateTime(selected.started_at, locale, tz)} → {fmtDateTime(selected.ended_at, locale, tz)}
            </span>
            <Badge tone="neutral">{t('callsCount', { count: selected.call_count })}</Badge>
            {selected.error_count > 0 && (
              <Badge tone="pink">{t('errorsCount', { count: selected.error_count })}</Badge>
            )}
            {selected.total_cost_usd != null && (
              <Badge tone="neutral">{fmtUsd(selected.total_cost_usd)}</Badge>
            )}
            {callsCut && (
              <Badge tone="amber">
                {t('callsTruncated', { shown: selected.calls.length, total: selected.call_count })}
              </Badge>
            )}
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
          {selected.calls.map((call, i) => {
            const res = bodies[call.request_id];
            const isLoading = loadingIds.has(call.request_id);
            const isOpen = openCalls.has(call.request_id);
            // 요청 messages 는 호출마다 전체 history 를 싣는다 — 직전 호출의 턴
            // 수를 historyStart 로 넘겨 이번 호출의 새 턴만 기본으로 보인다.
            // (messages[] 와 responses API input[] 두 방언 모두 센다)
            const rb = res?.data?.record?.request_body as
              | { messages?: unknown[]; input?: unknown }
              | undefined;
            const msgs = Array.isArray(rb?.messages)
              ? rb.messages
              : Array.isArray(rb?.input)
                ? (rb.input as unknown[])
                : null;
            // 접두 검증: 직전 호출 msgs 가 이번 호출의 앞부분과 정확히 이어질
            // 때만 그 길이를 숨긴다 — 경계 메시지 하나만 같으면 충분하다
            // (전체 비교는 메시지 수백 개 × stringify 라 비싸다).
            let historyStart = 0;
            if (
              msgs != null &&
              prevMsgs != null &&
              prevMsgs.length < msgs.length &&
              JSON.stringify(msgs[prevMsgs.length - 1]) ===
                JSON.stringify(prevMsgs[prevMsgs.length - 1])
            ) {
              historyStart = prevMsgs.length;
            }
            if (msgs != null) prevMsgs = msgs;
            return (
              <div key={call.request_id} className="glass rounded-apple">
                <button
                  type="button"
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
                      <BodyRecordContent data={res.data} historyStart={historyStart} />
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
          className="w-44 rounded-md border border-border bg-background px-2 py-1.5 text-xs"
        />
        <input
          type="text"
          value={filters.client}
          onChange={(e) => setFilters((f) => ({ ...f, client: e.target.value }))}
          placeholder={t('filterClientPlaceholder')}
          className="w-32 rounded-md border border-border bg-background px-2 py-1.5 text-xs"
        />
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

      {sessions === null ? (
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
                <Th>{t('colEnded')}</Th>
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
                  <Td className="whitespace-nowrap">{fmtDateTime(s.ended_at, locale, tz)}</Td>
                  <Td className="max-w-40 truncate text-sm" title={s.user_email ?? s.user_id ?? ''}>
                    {s.user_email ?? s.user_id ?? '-'}
                  </Td>
                  <Td className="text-sm">
                    {s.client ?? '-'}
                    {s.session_id === null && (
                      <Badge tone="amber" className="ml-1">{t('heuristicShort')}</Badge>
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
              onClick={() => load(appliedFilters, nextBefore)}
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
