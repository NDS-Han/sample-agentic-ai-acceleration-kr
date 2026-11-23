// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { fmtUsd, usdTitle } from '@/lib/utils/format';

/** USD 금액 — fmtUsd 규칙으로 그리고, '< $0.01' 이면 툴팁에 정확한 값을 단다. */
export function Usd({ value, className }: { value: number; className?: string }) {
  return (
    <span className={className} title={usdTitle(value)}>
      {fmtUsd(value)}
    </span>
  );
}
