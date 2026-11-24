'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { ChevronLeft, ChevronRight, Search } from 'lucide-react';
import { ErrorState } from '@/components/common/ErrorState';
import { Segmented } from '@/components/common/Segmented';
import { tzZoneText } from '@/lib/utils/timezone';
import { MAX_RANGE_DAYS } from '@/lib/utils/period';
import { UserModelTable } from './UserModelTable';
import { UserModelDrawer, type DrawerTarget } from './UserModelDrawer';
import {
  DEFAULT_SORT,
  USER_MODEL_OPEN_EVENT,
  fetchUserModelJson,
  filterParams,
  isRangeTooLongError,
  isUuid,
  nextSort,
  toSortParam,
  type SortColumn,
  type SortState,
  type TokenMode,
  type UserModelDetail,
  type UserModelFilters,
  type UserModelOpenDetail,
  type UserModelResponse,
  type UserModelTarget,
} from './userModel';

const LIST_URL = '/api/analytics/user-models';
const PAGE_SIZES = [10, 25, 50] as const;
const SEARCH_DEBOUNCE_MS = 300;
const MODES: TokenMode[] = ['total', 'input', 'output', 'cache'];

type ListState =
  | { status: 'loading'; data: UserModelResponse | null }
  | { status: 'ok'; data: UserModelResponse }
  | { status: 'error'; tooLong: boolean; data: null };

/**
 * 사용자 × 모델 토큰 사용량 — By User 모드 전용 섹션.
 *
 * 표 조작(정렬·페이지·검색·펼침·모드 전환)이 서버 페이지를 다시 그리거나 다른
 * analytics 조회를 다시 돌리지 않도록, 데이터는 클라이언트에서 Next 프록시
 * (/api/analytics/user-models) 로만 받는다. 필터가 바뀌면 페이지가 key 로 이
 * 섹션을 remount 하므로 정렬·펼침·상세 캐시도 함께 초기화된다.
 */
export function UserModelSection({ filters }: { filters: UserModelFilters }) {
  const t = useTranslations('analytics.userModel');
  const ta = useTranslations('analytics');
  const tc = useTranslations('common');

  const [mode, setMode] = useState<TokenMode>('total');
  const [sort, setSort] = useState<SortState>(DEFAULT_SORT);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(10);
  const [searchInput, setSearchInput] = useState('');
  const [q, setQ] = useState('');
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const [focusUserId, setFocusUserId] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<DrawerTarget | null>(null);
  const [list, setList] = useState<ListState>({ status: 'loading', data: null });
  const [attempt, setAttempt] = useState(0);

  const base = useMemo(() => filterParams(filters).toString(), [filters]);

  // 검색 디바운스 — 타이핑마다 조회하지 않는다.
  useEffect(() => {
    const next = searchInput.trim();
    if (next === q) return;
    const timer = setTimeout(() => {
      setQ(next);
      setPage(1);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchInput, q]);

  const sortParam = toSortParam(sort.col, mode);
  useEffect(() => {
    const ctrl = new AbortController();
    const p = new URLSearchParams(base);
    p.set('sort', sortParam);
    p.set('dir', sort.dir);
    p.set('page', String(page));
    p.set('page_size', String(pageSize));
    if (q) p.set('q', q);
    setList((prev) => ({ status: 'loading', data: prev.data }));
    fetchUserModelJson<UserModelResponse>(`${LIST_URL}?${p}`, ctrl.signal).then(
      (data) => setList({ status: 'ok', data }),
      (e) => {
        if (ctrl.signal.aborted) return;
        setList({ status: 'error', tooLong: isRangeTooLongError(e), data: null });
      },
    );
    return () => ctrl.abort();
  }, [base, sortParam, sort.dir, page, pageSize, q, attempt]);

  // 상세 캐시 — 펼친 행과 드로어가 같은 사용자를 두 번 조회하지 않는다. 실패는 지워 재시도 가능.
  const detailCache = useRef(new Map<string, Promise<UserModelDetail>>());
  const fetchDetail = useCallback(
    (userId: string) => {
      const hit = detailCache.current.get(userId);
      if (hit) return hit;
      const pr = fetchUserModelJson<UserModelDetail>(
        `${LIST_URL}/${encodeURIComponent(userId)}?${base}`,
      ).catch((e) => {
        detailCache.current.delete(userId);
        throw e;
      });
      detailCache.current.set(userId, pr);
      return pr;
    },
    [base],
  );

  // Cost by User 에 user_id 가 없을 때(구버전 API) — 이메일(없으면 이름)로 표를 검색해 찾는다.
  const resolveUserId = useCallback(
    async (target: UserModelTarget): Promise<string | null> => {
      const needle = (target.email || target.name || '').trim();
      if (!needle) return null;
      const p = new URLSearchParams(base);
      p.set('q', needle);
      p.set('page_size', '50');
      const res = await fetchUserModelJson<UserModelResponse>(`${LIST_URL}?${p}`);
      const lower = needle.toLowerCase();
      const hit =
        res.rows.find((r) => (r.email ?? '').toLowerCase() === lower) ??
        res.rows.find((r) => (r.name ?? '').toLowerCase() === lower) ??
        (res.rows.length === 1 ? res.rows[0] : undefined);
      return hit?.user_id ?? null;
    },
    [base],
  );

  useEffect(() => {
    const onOpen = (e: Event) => {
      const d = (e as CustomEvent<UserModelOpenDetail>).detail ?? {};
      setDrawer({
        user_id: d.user_id ?? null,
        email: d.email ?? null,
        name: d.name ?? null,
        trigger: d.trigger ?? (document.activeElement as HTMLElement | null),
      });
    };
    window.addEventListener(USER_MODEL_OPEN_EVENT, onOpen);
    return () => window.removeEventListener(USER_MODEL_OPEN_EVENT, onOpen);
  }, []);

  // 딥링크 — /analytics?group_by=user&user=<uuid> (Users/Teams 사용자 상세의 'Analytics')는
  // 그 사용자의 모델별 드로어를 바로 연다.
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get('user');
    if (isUuid(id)) setDrawer({ user_id: id, email: null, name: null, trigger: null });
  }, []);

  const onSort = (col: SortColumn) => {
    setSort((prev) => nextSort(prev, col));
    setPage(1);
  };
  const onToggle = (userId: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(userId)) next.delete(userId);
      else next.add(userId);
      return next;
    });

  const showInTable = (user: { user_id: string; email: string | null; name: string | null }) => {
    const needle = (user.email || user.name || '').trim();
    setDrawer(null);
    setSearchInput(needle);
    setQ(needle);
    setPage(1);
    setExpanded(new Set([user.user_id]));
    setFocusUserId(user.user_id);
  };
  const onFocused = useCallback(() => setFocusUserId(null), []);

  const data = list.data;
  const total = data?.total_users ?? 0;
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  const caption = data
    ? t('caption', { start: data.start_date, end: data.end_date, zone: tzZoneText(data.timezone) })
    : ' ';

  const navBtn =
    'inline-flex h-8 min-w-8 items-center justify-center rounded-md border border-border bg-background px-2 text-sm hover:bg-muted disabled:pointer-events-none disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring';

  return (
    <section aria-labelledby="user-model-title" className="glass rounded-apple p-4 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2 id="user-model-title" className="text-sm font-semibold">{t('title')}</h2>
          <p className="text-[11px] text-muted-foreground" data-testid="user-model-caption">{caption}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Segmented
            label={t('modeLabel')}
            value={mode}
            onSelect={(v) => setMode(v as TokenMode)}
            items={MODES.map((m) => ({ value: m, label: t(`modes.${m}`) }))}
          />
          <label className="relative">
            <span className="sr-only">{t('searchLabel')}</span>
            <Search
              size={14}
              aria-hidden="true"
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
            />
            <input
              type="search"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder={t('searchPlaceholder')}
              className="h-9 w-56 rounded-md border border-border bg-background pl-8 pr-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </label>
        </div>
      </div>

      {list.status === 'error' ? (
        <ErrorState
          compact
          message={list.tooLong ? ta('rangeTooLong', { max: MAX_RANGE_DAYS }) : t('loadFailed')}
          onRetry={() => setAttempt((n) => n + 1)}
        />
      ) : !data ? (
        <UserModelSkeleton label={tc('loadingLabel')} />
      ) : (
        <div className={list.status === 'loading' ? 'opacity-60 transition-opacity' : undefined} aria-busy={list.status === 'loading'}>
          <UserModelTable
            data={data}
            mode={mode}
            sort={sort}
            onSort={onSort}
            expanded={expanded}
            onToggle={onToggle}
            fetchDetail={fetchDetail}
            focusUserId={focusUserId}
            onFocused={onFocused}
            emptyText={q ? t('noMatch', { q }) : t('empty')}
          />
          <div className="flex flex-wrap items-center justify-between gap-3 pt-3">
            <span className="text-xs text-muted-foreground num" data-testid="user-model-range">
              {t('range', { from, to, total })}
            </span>
            <nav aria-label={t('pagination')} className="flex items-center gap-2">
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                {t('pageSize')}
                <select
                  value={pageSize}
                  onChange={(e) => {
                    setPageSize(Number(e.target.value));
                    setPage(1);
                  }}
                  className="h-8 rounded-md border border-border bg-background px-2 text-sm text-foreground"
                >
                  {PAGE_SIZES.map((n) => (
                    <option key={n} value={n}>{n}</option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className={navBtn}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
                aria-label={t('prev')}
              >
                <ChevronLeft size={16} aria-hidden="true" />
              </button>
              <span className="text-xs text-muted-foreground num">
                {page} / {pageCount}
              </span>
              <button
                type="button"
                className={navBtn}
                onClick={() => setPage((p) => Math.min(pageCount, p + 1))}
                disabled={page >= pageCount}
                aria-label={t('next')}
              >
                <ChevronRight size={16} aria-hidden="true" />
              </button>
            </nav>
          </div>
        </div>
      )}

      <UserModelDrawer
        target={drawer}
        onClose={() => setDrawer(null)}
        fetchDetail={fetchDetail}
        resolveUserId={resolveUserId}
        onShowInTable={showInTable}
      />
    </section>
  );
}

export function UserModelSkeleton({ label }: { label?: string }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label={label}>
      {Array.from({ length: 6 }).map((_, i) => (
        <div
          key={i}
          className="h-9 animate-pulse rounded-md bg-muted/60"
          style={{ animationDelay: `${i * 60}ms` }}
        />
      ))}
    </div>
  );
}
