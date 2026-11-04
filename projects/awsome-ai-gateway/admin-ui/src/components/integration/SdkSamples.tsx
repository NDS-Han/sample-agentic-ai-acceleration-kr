'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { CliEnvBlock } from '@/components/cli/CliEnvBlock';

export type SdkId = 'claude' | 'openai' | 'langchain';

interface SdkSamplesProps {
  samples: Record<SdkId, { curl: string; python: string }>;
}

const SDKS: SdkId[] = ['claude', 'openai', 'langchain'];

/**
 * 외부 앱 연동 SDK 선택 카드 — 클릭하면 해당 SDK 의 curl/Python 샘플을
 * 아래에 보여 준다. 샘플 문자열은 서버(gateway URL 실값)에서 만들어 넘긴다.
 */
export function SdkSamples({ samples }: SdkSamplesProps) {
  const t = useTranslations('integration');
  const [sdk, setSdk] = useState<SdkId>('claude');

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {SDKS.map((id) => (
          <button
            key={id}
            type="button"
            onClick={() => setSdk(id)}
            aria-pressed={sdk === id}
            className={`rounded-lg border p-3 text-left transition-colors ${
              sdk === id
                ? 'border-primary bg-primary/5'
                : 'border-border hover:border-primary/50 hover:bg-accent/40'
            }`}
          >
            <p className="text-sm font-semibold">{t(`sdk.${id}.name`)}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {t(`sdk.${id}.desc`)}
            </p>
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="space-y-2">
          <p className="text-xs font-semibold text-muted-foreground">
            curl — {t(`sdk.${sdk}.endpoint`)}
          </p>
          <CliEnvBlock text={samples[sdk].curl} />
        </div>
        <div className="space-y-2">
          <p className="text-xs font-semibold text-muted-foreground">
            Python — {t(`sdk.${sdk}.lib`)}
          </p>
          <CliEnvBlock text={samples[sdk].python} />
        </div>
      </div>
    </div>
  );
}
