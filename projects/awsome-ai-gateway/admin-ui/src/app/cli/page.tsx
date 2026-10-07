// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { CLIDownloadItem } from '@/types/entities';
import { adminAPI } from '@/lib/api-client';
import { CLIDownloadCard } from '@/components/cli/CLIDownloadCard';
import { CliEnvBlock } from '@/components/cli/CliEnvBlock';
import { ErrorState } from '@/components/common/ErrorState';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { getTranslations } from 'next-intl/server';

/**
 * 사전 준비 env 블록 — Helm 이 내려준 배포값으로 채우고, 없으면(로컬 dev·방식 A
 * ALB-DNS 직접 등) placeholder 를 보여 준다. 사용자가 카탈로그 값을 수동 변환
 * 하던 마찰(리뷰 TOP-4) 해소.
 */
function buildEnvText(): string {
  const env = (name: string, placeholder: string) =>
    (process.env[name] ?? '').trim() || placeholder;
  const issuer = env(
    'OIDC_ISSUER_URL',
    "https://cognito-idp.<region>.amazonaws.com/'<POOL_ID>'"
  );
  const clientId = env('OIDC_CLIENT_ID', "'<COGNITO_APP_CLIENT_ID>'");
  // Cognito 는 access_token 에 aud 가 없어 OIDC_AUDIENCE 를 비워 두는데, CLI 쪽
  // audience 는 client_id 와 같다 — 비어 있으면 clientId 로 채운다.
  const audience = env('OIDC_AUDIENCE', clientId);
  const gateway = env('GATEWAY_PUBLIC_URL', "https://'<gateway-host>'");
  const adminApi = env('ADMIN_API_PUBLIC_URL', "https://'<admin-api-host>'");
  return [
    `export OIDC_ISSUER_URL="${issuer}"`,
    `export OIDC_CLIENT_ID="${clientId}"`,
    `export OIDC_AUDIENCE="${audience}"`,
    `export GATEWAY_URL="${gateway}"`,
    `export GATEWAY_ADMIN_URL="${adminApi}"`,
  ].join('\n');
}

export default async function CLIPage() {
  const t = await getTranslations('cli');
  // 목록 조회 실패를 "바이너리 없음"과 구분 — 둘을 합치면 네트워크 장애가
  // 빌드 누락처럼 보인다.
  const downloadsResult = await adminAPI
    .get<CLIDownloadItem[]>('/cli/downloads')
    .then((v) => ({ ok: true as const, value: v }))
    .catch(() => ({ ok: false as const }));
  const rawDownloads = downloadsResult.ok ? downloadsResult.value : [];

  // admin-api 가 주는 download_url 은 `/cli/download/{os}/{arch}` — **admin-api** 기준 경로다.
  // 브라우저에서 쓰려면 같은 오리진의 프록시를 거쳐야 하므로
  // app/api/cli-download/[os]/[arch] 로 바꿔 준다.
  //   * 이 앱의 라우트 핸들러 규약이 `app/api/**` 이고 미들웨어 허용목록도 `/api/` 기준이다.
  //   * 그 프록시는 스트리밍이고 상류 불통(502)과 상류 상태코드를 구분한다.
  // (정정: 예전 경로도 Next 404 는 아니었다 — app/cli/download/[os]/[arch]/route.ts 가
  //  같은 경로를 받고 있었다. 버튼이 404 였던 진짜 원인은 admin-api 의 CLI_DIST_DIR 이
  //  비어 있어서 **상류가** 404 였던 것. 그 중복 라우트는 스트리밍/502 구분이 없어 제거했다.)
  const downloads = (Array.isArray(rawDownloads) ? rawDownloads : []).map((item) => ({
    ...item,
    download_url: `/api/cli-download/${item.os}/${item.arch}`,
  }));

  const code = (chunks: React.ReactNode) => (
    <code className="bg-muted px-1 rounded">{chunks}</code>
  );

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">{t('title')}</h1>
        <p className="text-muted-foreground mt-2 text-sm">{t('subtitle')}</p>
      </header>

      {/* 사전 준비 */}
      <section className="glass glass-hover rounded-apple p-6 space-y-3">
        <h2 className="text-base font-semibold">{t('prereqTitle')}</h2>
        <p className="text-sm text-muted-foreground">{t('prereqDesc')}</p>
        <CliEnvBlock text={buildEnvText()} />
        <p className="text-xs text-muted-foreground">{t('prereqPsNote')}</p>
      </section>

      {/* 설치 가이드 — OS 병렬 */}
      <section className="space-y-3">
        <h2 className="text-base font-semibold">{t('installTitle')}</h2>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="glass glass-hover rounded-apple p-4 text-sm space-y-2">
            <p className="font-semibold">{t('installLinuxLabel')}</p>
            <pre className="bg-background rounded p-3 overflow-x-auto text-xs">
              {t('installLinux')}
            </pre>
          </div>
          <div className="glass glass-hover rounded-apple p-4 text-sm space-y-2">
            <p className="font-semibold">{t('installWindowsLabel')}</p>
            <pre className="bg-background rounded p-3 overflow-x-auto text-xs">
              {t('installWindows')}
            </pre>
          </div>
        </div>
      </section>

      {/* 커맨드 레퍼런스 */}
      <section className="space-y-3">
        <h2 className="text-base font-semibold">{t('cmdRefTitle')}</h2>
        <div className="glass rounded-apple overflow-hidden">
          <Table>
            <THead>
              <Tr>
                <Th className="w-48">{t('cmdColumn')}</Th>
                <Th>{t('descColumn')}</Th>
              </Tr>
            </THead>
            <TBody>
              <Tr>
                <Td className="font-mono text-xs">gateway-cli login</Td>
                <Td>
                  {t.rich('cmdLogin', { code })}
                  <div className="text-xs text-muted-foreground mt-1">
                    {t('cmdLoginOpts')}
                  </div>
                </Td>
              </Tr>
              <Tr>
                <Td className="font-mono text-xs">gateway-cli logout</Td>
                <Td>{t('cmdLogout')}</Td>
              </Tr>
              <Tr>
                <Td className="font-mono text-xs">gateway-cli setup</Td>
                <Td>
                  {t.rich('cmdSetup', { code })}
                  <div className="text-xs text-muted-foreground mt-1">
                    {t('cmdSetupOpts')}
                  </div>
                </Td>
              </Tr>
              <Tr>
                <Td className="font-mono text-xs">gateway-cli status</Td>
                <Td>{t('cmdStatus')}</Td>
              </Tr>
              <Tr>
                <Td className="font-mono text-xs">gateway-cli disable</Td>
                <Td>{t('cmdDisable')}</Td>
              </Tr>
              <Tr>
                <Td className="font-mono text-xs">gateway-cli version</Td>
                <Td>{t('cmdVersion')}</Td>
              </Tr>
            </TBody>
          </Table>
        </div>
      </section>

      {/* 자동 프로비저닝 안내 */}
      <section className="glass glass-hover rounded-apple p-6 space-y-2 text-sm">
        <h2 className="text-base font-semibold">{t('autoProvTitle')}</h2>
        <p className="text-muted-foreground">
          {t.rich('autoProvDesc', { code })}
        </p>
        <ul className="list-disc list-inside space-y-1 text-muted-foreground">
          <li>{t.rich('autoProvUser', { code })}</li>
          <li>{t.rich('autoProvTeam', { code })}</li>
          <li>{t.rich('autoProvRole', { code })}</li>
        </ul>
      </section>

      {/* 레거시 STS 안내 */}
      <section className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p>
          {t.rich('legacySts', {
            code,
            strong: (chunks) => (
              <strong className="text-foreground">{chunks}</strong>
            ),
          })}
        </p>
      </section>

      {/* 다운로드 카드 */}
      <section className="space-y-3">
        <h2 className="text-base font-semibold">{t('binaryTitle')}</h2>
        {!downloadsResult.ok ? (
          <ErrorState compact />
        ) : downloads.length === 0 ? (
          // admin-api 는 CLI_DIST_DIR 에 실제로 있는 패키지만 광고한다(예전엔 없는 파일도
          // 0.0 MB 카드로 광고해 누르면 404 였다). 비어 있으면 원인을 알려 준다.
          <p className="text-muted-foreground text-sm">
            {t.rich('emptyDownloads', { code })}
          </p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {downloads.map((item) => (
              <CLIDownloadCard key={`${item.os}-${item.arch}`} item={item} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
