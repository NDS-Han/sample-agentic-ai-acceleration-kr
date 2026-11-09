'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { classifyChanges } from '@/lib/audit/changes';
import type { AuditLogItem } from '@/types/entities';

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

interface Props {
  item: AuditLogItem;
}

/** 펼친 감사 행의 상세 — changes diff + 요청 메타 + 원본 JSON 토글. */
export function ChangesPanel({ item }: Props) {
  const t = useTranslations('audit');
  const [showRaw, setShowRaw] = useState(false);
  const shape = classifyChanges(item.changes);

  return (
    <div className="space-y-3 py-2 text-xs">
      {shape.kind === 'empty' && (
        <p className="text-muted-foreground">{t('changesEmpty')}</p>
      )}
      {shape.kind === 'unserializable' && (
        <p className="text-muted-foreground">{t('changesUnserializable')}</p>
      )}

      {shape.kind === 'fieldDiff' && (
        <table className="w-full">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 pr-3 font-medium">{t('diffField')}</th>
              <th className="py-1 pr-3 font-medium">{t('diffBefore')}</th>
              <th className="py-1 font-medium">{t('diffAfter')}</th>
            </tr>
          </thead>
          <tbody>
            {shape.fields.map((f) => (
              <tr
                key={f.key}
                className={f.changed ? 'text-foreground' : 'text-muted-foreground'}
              >
                <td className="py-1 pr-3 font-mono">{f.key}</td>
                <td className="max-w-56 truncate py-1 pr-3" title={formatValue(f.before)}>
                  {formatValue(f.before)}
                </td>
                <td
                  className={`max-w-56 truncate py-1 ${f.changed ? 'font-medium' : ''}`}
                  title={formatValue(f.after)}
                >
                  {formatValue(f.after)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {shape.kind === 'setDiff' && (
        <div className="flex flex-wrap gap-1.5">
          {shape.added.map((v, i) => (
            <span
              key={`a-${i}`}
              className="rounded border border-teal-500/40 bg-teal-500/10 px-1.5 py-0.5 font-mono text-teal-600 dark:text-teal-400"
            >
              +{formatValue(v)}
            </span>
          ))}
          {shape.removed.map((v, i) => (
            <span
              key={`r-${i}`}
              className="rounded border border-destructive/40 bg-destructive/10 px-1.5 py-0.5 font-mono text-destructive"
            >
              −{formatValue(v)}
            </span>
          ))}
          {shape.kept.map((v, i) => (
            <span
              key={`k-${i}`}
              className="rounded border border-border px-1.5 py-0.5 font-mono text-muted-foreground"
            >
              {formatValue(v)}
            </span>
          ))}
        </div>
      )}

      {shape.kind === 'scalar' && (
        <p className="font-mono">
          {formatValue(shape.before)} →{' '}
          <span className="font-medium">{formatValue(shape.after)}</span>
        </p>
      )}

      {shape.kind === 'afterOnly' && (
        <div>
          <p className="mb-1 font-medium text-muted-foreground">
            {t('afterOnlyTitle')}
          </p>
          <table className="w-full">
            <tbody>
              {shape.fields.map((f) => (
                <tr key={f.key}>
                  <td className="w-48 py-1 pr-3 font-mono text-muted-foreground">
                    {f.key}
                  </td>
                  <td
                    className="max-w-96 truncate py-1"
                    title={formatValue(f.value)}
                  >
                    {formatValue(f.value)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {shape.kind === 'kv' && (
        <table className="w-full">
          <tbody>
            {shape.entries.map((e) => (
              <tr key={e.key}>
                <td className="w-48 py-1 pr-3 font-mono text-muted-foreground">
                  {e.key}
                </td>
                <td className="max-w-96 truncate py-1" title={formatValue(e.value)}>
                  {formatValue(e.value)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* before/after 형제 키 — cascade 정보(deleted_children 등). */}
      {'extra' in shape && shape.extra.length > 0 && (
        <table className="w-full">
          <tbody>
            {shape.extra.map((e) => (
              <tr key={e.key}>
                <td className="w-48 py-1 pr-3 font-mono text-muted-foreground">
                  {e.key}
                </td>
                <td className="max-w-96 truncate py-1" title={formatValue(e.value)}>
                  {formatValue(e.value)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* 요청 메타 — 조사 시 필요하지만 표 컬럼 폭을 쓸 값은 아님. */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-border pt-2 text-muted-foreground">
        <span>
          IP <span className="font-mono">{item.ip_address}</span>
        </span>
        {item.request_id && (
          <span>
            request_id{' '}
            <span className="font-mono" title={item.request_id}>
              {item.request_id.slice(0, 24)}
            </span>
            <button
              className="ml-1 underline underline-offset-2"
              // clipboard API 는 비보안 컨텍스트·권한 거부에서 throw — 조용히 무시.
              onClick={() =>
                void navigator.clipboard
                  ?.writeText(item.request_id)
                  .catch(() => {})
              }
            >
              {t('copy')}
            </button>
          </span>
        )}
        <button
          className="underline underline-offset-2"
          onClick={() => setShowRaw((v) => !v)}
        >
          {showRaw ? t('rawHide') : t('rawShow')}
        </button>
      </div>
      {showRaw && (
        <pre className="max-h-64 overflow-auto rounded bg-muted p-2 font-mono text-[11px]">
          {JSON.stringify(item.changes, null, 2)}
        </pre>
      )}
    </div>
  );
}
