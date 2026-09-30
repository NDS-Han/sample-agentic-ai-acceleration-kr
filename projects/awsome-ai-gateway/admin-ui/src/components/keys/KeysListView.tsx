'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import { listKeysAction } from '@/lib/actions/keys';
import { useToast } from '@/components/common/ToastProvider';
import { KeysTable } from './KeysTable';
import type { VirtualKeyListItem } from '@/types/entities';

interface KeysListViewProps {
  initialItems: VirtualKeyListItem[];
  /** 다음 페이지 커서 — has_more 일 때만 유효. */
  initialCursor: string | null;
  hasMore: boolean;
  /** 현재 필터 — 더 보기 요청에 그대로 실어 같은 결과 집합을 유지. */
  email: string;
  status: string;
  limit: number;
}

/**
 * 키 목록 + 커서 페이지네이션 "더 보기".
 * 첫 페이지는 서버 컴포넌트가 가져오고, 이후 페이지는 listKeysAction 으로
 * 같은 필터/커서를 보내 누적한다. 서버 응답의 pagination.cursor 가 다음 커서.
 */
export function KeysListView({
  initialItems,
  initialCursor,
  hasMore: initialHasMore,
  email,
  status,
  limit,
}: KeysListViewProps) {
  const t = useTranslations('keys');
  const { toast } = useToast();
  const [items, setItems] = useState(initialItems);
  const [cursor, setCursor] = useState(initialCursor);
  const [hasMore, setHasMore] = useState(initialHasMore);
  const [isPending, startTransition] = useTransition();

  function handleLoadMore() {
    startTransition(async () => {
      const result = await listKeysAction({ cursor, email, status, limit });
      if (!result.success) {
        toast({
          type: 'error',
          message: result.error ?? t('loadMoreFailed'),
          auto_dismiss_ms: 5000,
        });
        return;
      }
      setItems((prev) => [...prev, ...result.data.items]);
      setCursor(result.data.pagination.cursor);
      setHasMore(result.data.pagination.has_more);
    });
  }

  return (
    <>
      <KeysTable keys={items} />
      {hasMore && (
        <div className="mt-4 flex justify-center">
          <button
            type="button"
            onClick={handleLoadMore}
            disabled={isPending}
            className="inline-flex items-center rounded-md border border-border bg-background px-4 py-2 text-sm font-medium text-foreground hover:bg-accent hover:text-accent-foreground transition-colors disabled:opacity-50"
          >
            {isPending ? t('loading') : t('loadMore')}
          </button>
        </div>
      )}
    </>
  );
}
