'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useRouter, usePathname, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { Segmented } from '@/components/common/Segmented';

interface ClientFilterProps {
  current: string; // 'all' | 'claude-code' | 'cowork' | 'codex' | 'other'
}

const OPTIONS: Array<{ value: string; labelKey: string | null }> = [
  { value: 'all', labelKey: 'clientAll' },
  { value: 'claude-code', labelKey: null }, // keep as-is (brand name)
  { value: 'cowork', labelKey: null },
  { value: 'codex', labelKey: null },
  { value: 'other', labelKey: 'clientOther' },
];

/**
 * 앱(client) 필터 — ?client=all|claude-code|cowork|codex|other URL 쿼리로 구동.
 * period 등 기존 쿼리는 보존한 채 client 만 교체한다.
 */
export function ClientFilter({ current }: ClientFilterProps) {
  const t = useTranslations('dashboard');
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  function go(client: string) {
    if (client === current) return;
    const sp = new URLSearchParams(searchParams.toString());
    if (client === 'all') sp.delete('client');
    else sp.set('client', client);
    router.push(`${pathname}?${sp.toString()}`);
  }

  return (
    <Segmented
      label={t('appFilter')}
      value={current}
      onSelect={go}
      items={OPTIONS.map((o) => ({
        value: o.value,
        label: o.labelKey ? t(o.labelKey) : o.value === 'claude-code' ? 'Claude Code' : o.value === 'cowork' ? 'Cowork' : 'Codex',
      }))}
    />
  );
}
