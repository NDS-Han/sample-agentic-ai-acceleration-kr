// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { describe, it, expect } from 'vitest';
import { alertLevelOf } from '@/lib/utils/alertLevel';
import { AlertLevel } from '@/types/enums';

// 임계값의 단일 출처 — admin-api 의 _alert_level 서비스 로직(>=90 CRITICAL /
// >=70 WARNING)과 1:1 로 대응한다. 옛 회귀: budgets 페이지는 >=100/>=80,
// 대시보드는 >=95/>=80 을 써서 같은 수치에 다른 심각도를 표시했다.
describe('alertLevelOf — backend _alert_level 과 동일 임계', () => {
  it('null 은 NORMAL (한도 미설정 — 비율이 정의되지 않는다)', () => {
    expect(alertLevelOf(null)).toBe(AlertLevel.NORMAL);
  });

  it('경계값: 69/70/89/90/100', () => {
    expect(alertLevelOf(69)).toBe(AlertLevel.NORMAL);
    expect(alertLevelOf(70)).toBe(AlertLevel.WARNING);
    expect(alertLevelOf(89)).toBe(AlertLevel.WARNING);
    expect(alertLevelOf(90)).toBe(AlertLevel.CRITICAL);
    expect(alertLevelOf(100)).toBe(AlertLevel.CRITICAL);
  });
});
