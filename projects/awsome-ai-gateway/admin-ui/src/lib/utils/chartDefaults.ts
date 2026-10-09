// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

import { Chart } from 'chart.js';

/**
 * Chart.js canvas 는 CSS 의 font-family 를 상속하지 않는다 — 자체 기본값
 * (Helvetica/Arial 스택)으로 텍스트를 그려 한글 제목·축·범례가 □□□ 로
 * 깨졌다. canvas font 문자열에서 var() 도 평가되지 않으므로 --font-sans 를
 * 직접 넣을 수 없다. body 에 계산된 최종 폰트 스택을 getComputedStyle 로
 * 읽어(var() 는 해석된 family 명으로 돌아온다) Chart.defaults 에 주입한다.
 *
 * 차트를 쓰는 클라이언트 모듈이 `import '@/lib/utils/chartDefaults'` 하면
 * 모듈 평가 시 1회 적용된다.
 */
if (typeof document !== 'undefined') {
  const stack = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.family = stack || 'system-ui, sans-serif';
}
