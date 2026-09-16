'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Search, X, Users, Folder, User as UserIcon } from 'lucide-react';
import type { OrgTreeNode, UserSearchItem } from '@/types/entities';
import { searchUsersAction } from '@/lib/actions/users';
import { searchOrgNodes, SEARCH_MIN_LENGTH, type OrgMatch } from '@/lib/utils/orgSearch';

/** 검색어 입력 후 서버 조회까지의 지연. 타이핑 중 불필요한 왕복을 막는다. */
const DEBOUNCE_MS = 250;

interface OrgSearchBoxProps {
  root: OrgTreeNode | null;
  /** 부서/팀 결과 선택 — 조상 경로를 펼치고 해당 노드를 선택한다. */
  onSelectOrgNode: (match: OrgMatch) => void;
  /** 사용자 결과 선택 — 소속 팀을 펼쳐 멤버를 로드한 뒤 해당 사용자를 선택한다. */
  onSelectUser: (user: UserSearchItem) => void;
}

/** 드롭다운에서 ↑↓ 로 순회하는 평탄화된 항목. */
type FlatItem =
  | { kind: 'org'; key: string; match: OrgMatch }
  | { kind: 'user'; key: string; user: UserSearchItem };

export function OrgSearchBox({ root, onSelectOrgNode, onSelectUser }: OrgSearchBoxProps) {
  const t = useTranslations('users.search');
  const [term, setTerm] = useState('');
  const [open, setOpen] = useState(false);
  const [users, setUsers] = useState<UserSearchItem[]>([]);
  const [usersTruncated, setUsersTruncated] = useState(false);
  const [isSearching, setIsSearching] = useState(false);
  const [searchFailed, setSearchFailed] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  // 늦게 도착한 응답이 최신 검색 결과를 덮어쓰지 않게 하는 순번(out-of-order 방어).
  const requestSeqRef = useRef(0);

  const trimmed = term.trim();
  const tooShort = trimmed.length > 0 && trimmed.length < SEARCH_MIN_LENGTH;

  // 부서/팀은 트리 payload 로 즉시 매칭 — 서버 호출 없음.
  const orgResult = useMemo(() => searchOrgNodes(root, trimmed), [root, trimmed]);

  // 사용자는 서버 검색. debounce 후 1회 호출.
  useEffect(() => {
    if (trimmed.length < SEARCH_MIN_LENGTH) {
      setUsers([]);
      setUsersTruncated(false);
      setIsSearching(false);
      setSearchFailed(false);
      // 진행 중인 응답이 빈 검색어 상태에 뒤늦게 반영되지 않도록 순번을 올린다.
      requestSeqRef.current += 1;
      return;
    }

    setIsSearching(true);
    setSearchFailed(false);
    const seq = ++requestSeqRef.current;
    const timer = setTimeout(async () => {
      const res = await searchUsersAction(trimmed);
      // 이 응답이 더 이상 최신이 아니면 버린다.
      if (seq !== requestSeqRef.current) return;
      setIsSearching(false);
      if (res.success) {
        setUsers(res.data.items);
        setUsersTruncated(res.data.truncated);
      } else {
        setUsers([]);
        setUsersTruncated(false);
        setSearchFailed(true);
      }
    }, DEBOUNCE_MS);

    return () => clearTimeout(timer);
  }, [trimmed]);

  const flatItems = useMemo<FlatItem[]>(
    () => [
      ...orgResult.matches.map((m) => ({
        kind: 'org' as const,
        key: `org:${m.node.id}`,
        match: m,
      })),
      ...users.map((u) => ({ kind: 'user' as const, key: `user:${u.id}`, user: u })),
    ],
    [orgResult.matches, users],
  );

  // 결과가 바뀌면 하이라이트를 첫 항목으로 되돌린다 —
  // 이전 index 가 새 목록 범위를 벗어나 아무것도 선택되지 않는 상태 방지.
  useEffect(() => {
    setActiveIndex(0);
  }, [flatItems.length, trimmed]);

  // 하이라이트된 항목이 스크롤 영역 밖이면 시야로 끌어온다.
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>('[data-active="true"]');
    // jsdom 등 일부 환경에는 scrollIntoView 가 없다 — 스크롤은 부가 기능이므로
    // 없으면 조용히 건너뛴다(키보드 순회 자체는 계속 동작해야 한다).
    el?.scrollIntoView?.({ block: 'nearest' });
  }, [activeIndex, open]);

  // 바깥 클릭 시 닫기.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  const commit = (item: FlatItem) => {
    if (item.kind === 'org') onSelectOrgNode(item.match);
    else onSelectUser(item.user);
    // 선택 후 드롭다운만 닫고 검색어는 남긴다 — 인접 결과를 이어서 확인하기 쉽게.
    setOpen(false);
  };

  const reset = () => {
    setTerm('');
    setUsers([]);
    setUsersTruncated(false);
    setSearchFailed(false);
    setOpen(false);
    inputRef.current?.focus();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      // 드롭다운이 열려있으면 닫기만, 이미 닫혀있으면 검색어까지 지운다.
      if (open) setOpen(false);
      else reset();
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); // 캐럿 이동 대신 목록 순회
      if (!open) {
        setOpen(true);
        return;
      }
      if (flatItems.length === 0) return;
      setActiveIndex((prev) => {
        const next = e.key === 'ArrowDown' ? prev + 1 : prev - 1;
        // 양 끝에서 순환 — 긴 목록 끝에서 되돌아오기 쉽게.
        return (next + flatItems.length) % flatItems.length;
      });
      return;
    }
    if (e.key === 'Enter') {
      const item = flatItems[activeIndex];
      if (open && item) {
        e.preventDefault();
        commit(item);
      }
    }
  };

  const hasResults = flatItems.length > 0;
  const showDropdown = open && trimmed.length > 0;
  const truncated = orgResult.truncated || usersTruncated;

  return (
    <div ref={containerRef} className="relative p-2 border-b">
      <div className="relative">
        <Search
          size={14}
          className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground pointer-events-none"
          aria-hidden="true"
        />
        <input
          ref={inputRef}
          type="search"
          role="combobox"
          value={term}
          onChange={(e) => {
            setTerm(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          placeholder={t('placeholder')}
          aria-label={t('ariaLabel')}
          aria-expanded={showDropdown}
          aria-controls="org-search-results"
          aria-activedescendant={
            showDropdown && flatItems[activeIndex]
              ? `org-search-opt-${flatItems[activeIndex].key}`
              : undefined
          }
          aria-autocomplete="list"
          autoComplete="off"
          className="glass w-full rounded-apple-sm border pl-8 pr-8 py-1.5 text-sm [&::-webkit-search-cancel-button]:hidden"
        />
        {term && (
          <button
            type="button"
            onClick={reset}
            aria-label={t('clear')}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
          >
            <X size={14} aria-hidden="true" />
          </button>
        )}
      </div>

      {showDropdown && (
        <div className="absolute left-2 right-2 top-full z-30 mt-1 rounded-apple-md border bg-background shadow-lg">
          {tooShort ? (
            <p className="px-3 py-2 text-xs text-muted-foreground">{t('minLength')}</p>
          ) : (
            <>
              <ul
                ref={listRef}
                id="org-search-results"
                role="listbox"
                aria-label={t('ariaLabel')}
                className="max-h-80 overflow-y-auto py-1"
              >
                {orgResult.matches.length > 0 && (
                  <li role="presentation">
                    <p className="px-3 pt-1 pb-0.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                      {t('groupOrg')} · {t('resultsCount', { count: orgResult.matches.length })}
                    </p>
                  </li>
                )}
                {flatItems.map((item, i) => {
                  const isActive = i === activeIndex;
                  // 사용자 섹션 헤더는 첫 사용자 항목 앞에 한 번만.
                  const isFirstUser =
                    item.kind === 'user' && i === orgResult.matches.length;
                  return (
                    // listbox 의 직계 자식은 li 여야 하므로 Fragment 로 감싼다
                    // (div 를 끼우면 role 구조가 깨진다).
                    <Fragment key={item.key}>
                      {isFirstUser && (
                        <li role="presentation">
                          <p className="px-3 pt-2 pb-0.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                            {t('groupUser')} · {t('resultsCount', { count: users.length })}
                          </p>
                        </li>
                      )}
                      <li
                        id={`org-search-opt-${item.key}`}
                        role="option"
                        aria-selected={isActive}
                        data-active={isActive}
                      >
                        <button
                          type="button"
                          // mousedown 이 input blur 로 드롭다운을 닫기 전에 선택되도록
                          // onMouseDown 에서 기본 동작을 막는다.
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={() => commit(item)}
                          onMouseEnter={() => setActiveIndex(i)}
                          className={[
                            'w-full px-3 py-1.5 text-left text-sm transition-colors',
                            isActive ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50',
                          ].join(' ')}
                        >
                          {item.kind === 'org' ? (
                            <OrgRow match={item.match} />
                          ) : (
                            <UserRow user={item.user} unassignedLabel={t('unassigned')} />
                          )}
                        </button>
                      </li>
                    </Fragment>
                  );
                })}
              </ul>

              {isSearching && (
                <p className="border-t px-3 py-1.5 text-xs text-muted-foreground">
                  {t('searching')}
                </p>
              )}
              {!isSearching && searchFailed && (
                <p className="border-t px-3 py-1.5 text-xs text-destructive">{t('failed')}</p>
              )}
              {!isSearching && !searchFailed && !hasResults && (
                <p className="px-3 py-2 text-xs text-muted-foreground">{t('noResults')}</p>
              )}
              {truncated && (
                <p className="border-t px-3 py-1.5 text-xs text-muted-foreground">
                  {t('truncated')}
                </p>
              )}
              {hasResults && (
                <p className="border-t px-3 py-1 text-[11px] text-muted-foreground">
                  {t('hint')}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── 결과 행 ───────────────────────────────────────────────────────────────────

function OrgRow({ match }: { match: OrgMatch }) {
  const t = useTranslations('users.search');
  const isTeam = match.node.type === 'TEAM';
  const userCount = match.node.meta.member_count ?? 0;
  // 팀은 "N명", 부서는 "M팀 · N명" — 숫자만 쓰면 부서에서 팀 수로 오독된다.
  // (이전에 부서 상세가 사용자 수를 "팀 수" 로 표시하던 버그와 같은 종류)
  const teamCount = match.node.meta.team_count ?? match.node.children?.length ?? 0;
  const counts = isTeam
    ? t('userCountShort', { count: userCount })
    : `${t('teamCountShort', { count: teamCount })} · ${t('userCountShort', { count: userCount })}`;
  return (
    <span className="flex items-center gap-2">
      {isTeam ? (
        <Users size={14} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
      ) : (
        <Folder size={14} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
      )}
      <span className="truncate font-medium">{match.node.name}</span>
      <span className="ml-auto flex-shrink-0 text-xs text-muted-foreground">
        {match.parentName ? `${match.parentName} · ` : ''}
        {counts}
      </span>
    </span>
  );
}

function UserRow({
  user,
  unassignedLabel,
}: {
  user: UserSearchItem;
  unassignedLabel: string;
}) {
  return (
    <span className="flex items-start gap-2">
      <UserIcon
        size={14}
        className="mt-0.5 flex-shrink-0 text-muted-foreground"
        aria-hidden="true"
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">{user.display_name}</span>
        <span className="block truncate text-xs text-muted-foreground">
          {user.email} · {user.team_name ?? unassignedLabel}
        </span>
      </span>
    </span>
  );
}
