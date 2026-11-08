// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { BadgeTone } from '@/components/common/Badge';
import type { MonitoringEventTypeFilter } from '@/lib/actions/monitoring';

/** 이벤트 유형 → 배지 톤. ERROR 는 danger(pink 와 분리), 경고 계열은 amber. */
export function eventTone(type: string): BadgeTone {
  switch (type) {
    case 'ERROR':
      return 'danger';
    case 'TIMEOUT':
    case 'SLOW_REQUEST':
      return 'amber';
    case 'SUCCESS':
      return 'teal';
    default:
      return 'neutral';
  }
}

/** 알려진 유형은 번역 라벨, 모르는 값은 원문 폴백(missing-key 방지). */
export function eventTypeLabels(t: (_key: string) => string): Record<string, string> {
  return {
    ERROR: t('types.ERROR'),
    TIMEOUT: t('types.TIMEOUT'),
    SLOW_REQUEST: t('types.SLOW_REQUEST'),
    SUCCESS: t('types.SUCCESS'),
  };
}

/** 유형 필터 표시 순서 — EventLog 와 본문 로그 최근 요청이 같은 순서를 쓴다. */
export const EVENT_TYPE_FILTERS: MonitoringEventTypeFilter[] = [
  'all',
  'success',
  'error',
  'timeout',
  'slow',
  'abnormal',
];
