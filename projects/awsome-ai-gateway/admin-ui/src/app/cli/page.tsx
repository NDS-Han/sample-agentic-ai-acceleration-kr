// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { CLIDownloadItem } from '@/types/entities';
import { adminAPI } from '@/lib/api-client';
import { CLIDownloadCard } from '@/components/cli/CLIDownloadCard';

export default async function CLIPage() {
  const rawDownloads = await adminAPI
    .get<CLIDownloadItem[]>('/cli/downloads')
    .catch(() => []);

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

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">CLI 다운로드</h1>
        <p className="text-muted-foreground mt-2 text-sm">
          AWSome AI Gateway CLI를 설치하고 Cognito(OIDC) 로그인으로 Claude Code를 사내 게이트웨이에 연결합니다.
          최초 로그인 시 사용자·팀이 자동으로 프로비저닝됩니다.
        </p>
      </header>

      {/* 사전 준비 */}
      <section className="glass glass-hover rounded-apple p-6 space-y-3">
        <h2 className="text-base font-semibold">사전 준비</h2>
        <p className="text-sm text-muted-foreground">
          사내 관리자에게 아래 3개 값을 받아 쉘 환경에 export 해두세요. Cognito User Pool에서 발급되는 OIDC 설정값입니다.
        </p>
        <pre className="bg-muted rounded p-3 text-xs overflow-x-auto">
{`export OIDC_ISSUER_URL="https://cognito-idp.ap-northeast-2.amazonaws.com/<POOL_ID>"
export OIDC_CLIENT_ID="<COGNITO_APP_CLIENT_ID>"
export OIDC_AUDIENCE="<COGNITO_APP_CLIENT_ID>"
export GATEWAY_URL="http://<gateway-host>:8000"
export GATEWAY_ADMIN_URL="http://<admin-api-host>:8080"`}
        </pre>
      </section>

      {/* 설치 가이드 — OS 병렬 */}
      <section className="space-y-3">
        <h2 className="text-base font-semibold">설치 및 활성화</h2>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="glass glass-hover rounded-apple p-4 text-sm space-y-2">
            <p className="font-semibold">Linux / macOS</p>
            <pre className="bg-background rounded p-3 overflow-x-auto text-xs">
{`# 1. 다운로드 후 압축 해제
tar -xzf gateway-cli-*.tar.gz
cd gateway-cli-*/

# 2. 설치 (sudo 필요 — managed-settings.d 쓰기)
./install.sh

# 3. Cognito 로그인 (브라우저 PKCE 플로우 자동 실행)
gateway-cli login
#   → 브라우저가 열리고 Cognito 로그인 창 표시
#   → 인증 성공 시 토큰이 ~/.gateway-cli/oidc-tokens.json (0600) 에 저장
#   → 최초 로그인이면 admin-api가 자동으로 사용자·팀 프로비저닝

# 4. 게이트웨이 활성화
gateway-cli setup --gateway-url "$GATEWAY_URL"

# 5. 상태 확인
gateway-cli status

# 6. 로그아웃 / 비활성화
gateway-cli logout         # OIDC 토큰 + VK 캐시 삭제
gateway-cli disable        # managed-settings 제거 (sudo)`}
            </pre>
          </div>
          <div className="glass glass-hover rounded-apple p-4 text-sm space-y-2">
            <p className="font-semibold">Windows (PowerShell 관리자 권한)</p>
            <pre className="bg-background rounded p-3 overflow-x-auto text-xs">
{`# 1. ZIP 압축 해제 후 폴더 진입

# 2. 설치
.\\install.ps1

# 3. 새 PowerShell 세션에서 Cognito 로그인
gateway-cli login

# 4. 게이트웨이 활성화
gateway-cli setup --gateway-url $env:GATEWAY_URL

# 5. 상태 확인
gateway-cli status

# 6. 로그아웃 / 비활성화
gateway-cli logout
gateway-cli disable`}
            </pre>
          </div>
        </div>
      </section>

      {/* 커맨드 레퍼런스 */}
      <section className="space-y-3">
        <h2 className="text-base font-semibold">CLI 커맨드 레퍼런스</h2>
        <div className="glass rounded-apple overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/50">
                  <th className="px-4 py-2 text-left font-medium w-48">커맨드</th>
                  <th className="px-4 py-2 text-left font-medium">설명</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-b border-border">
                  <td className="px-4 py-2 font-mono text-xs">gateway-cli login</td>
                  <td className="px-4 py-2">
                    Cognito(OIDC) PKCE 플로우로 브라우저 로그인. 토큰은 <code className="bg-muted px-1 rounded">~/.gateway-cli/oidc-tokens.json</code> (0600).
                    <div className="text-xs text-muted-foreground mt-1">
                      옵션: <code>--issuer-url</code>, <code>--client-id</code>, <code>--audience</code>, <code>--redirect-port</code> (기본 8090), <code>--timeout</code> (기본 300s)
                    </div>
                  </td>
                </tr>
                <tr className="border-b border-border">
                  <td className="px-4 py-2 font-mono text-xs">gateway-cli logout</td>
                  <td className="px-4 py-2">OIDC 토큰 + Virtual Key 캐시 삭제.</td>
                </tr>
                <tr className="border-b border-border">
                  <td className="px-4 py-2 font-mono text-xs">gateway-cli setup</td>
                  <td className="px-4 py-2">
                    Claude Code의 <code className="bg-muted px-1 rounded">managed-settings.d</code>에 게이트웨이 설정 기록. sudo 필요.
                    <div className="text-xs text-muted-foreground mt-1">
                      옵션: <code>--gateway-url</code>, <code>--admin-api-url</code> (기본은 gateway-url 호스트의 :8080), <code>--api-key-helper</code>, <code>--otel-endpoint</code>
                    </div>
                  </td>
                </tr>
                <tr className="border-b border-border">
                  <td className="px-4 py-2 font-mono text-xs">gateway-cli status</td>
                  <td className="px-4 py-2">현재 managed-settings 상태 확인 (ON/OFF + 경로 + 설정값).</td>
                </tr>
                <tr className="border-b border-border">
                  <td className="px-4 py-2 font-mono text-xs">gateway-cli disable</td>
                  <td className="px-4 py-2">managed-settings 파일 제거 (게이트웨이 비활성화). sudo 필요.</td>
                </tr>
                <tr>
                  <td className="px-4 py-2 font-mono text-xs">gateway-cli version</td>
                  <td className="px-4 py-2">설치된 CLI 버전 출력.</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* 자동 프로비저닝 안내 */}
      <section className="glass glass-hover rounded-apple p-6 space-y-2 text-sm">
        <h2 className="text-base font-semibold">자동 프로비저닝</h2>
        <p className="text-muted-foreground">
          처음 <code className="bg-muted px-1 rounded">gateway-cli login</code>에 성공하면 admin-api가 Cognito 클레임을 기반으로 사용자와 팀을 자동 생성합니다.
        </p>
        <ul className="list-disc list-inside space-y-1 text-muted-foreground">
          <li>
            사용자: Cognito <code className="bg-muted px-1 rounded">sub</code> 클레임으로 식별. 이메일·이름은 ID 토큰 클레임에서 동기화.
          </li>
          <li>
            팀: Cognito 그룹 중 <code className="bg-muted px-1 rounded">Claude_&lt;TEAM&gt;</code> 패턴 매칭 시 해당 팀에 배치. 신규 팀은 예산 $0 + HARD_BLOCK으로 자동 생성 → 관리자가 예산 설정 전까지 호출 차단.
          </li>
          <li>
            역할: <code className="bg-muted px-1 rounded">ADMIN_EMAILS</code> 또는 <code className="bg-muted px-1 rounded">ADMIN_GROUPS</code> 매칭 시 ADMIN, 그 외 DEVELOPER.
          </li>
        </ul>
      </section>

      {/* 레거시 STS 안내 */}
      <section className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p>
          <strong className="text-foreground">STS 기반 인증 (레거시)</strong>:
          Cognito 이전 AWS SSO 자격증명으로 직접 Virtual Key를 발급받던 경로는 호환을 위해 유지됩니다.
          신규 사용자는 <code>gateway-cli login</code>(OIDC) 경로를 사용하세요.
        </p>
      </section>

      {/* 다운로드 카드 */}
      <section className="space-y-3">
        <h2 className="text-base font-semibold">바이너리 다운로드</h2>
        {downloads.length === 0 ? (
          // admin-api 는 CLI_DIST_DIR 에 실제로 있는 패키지만 광고한다(예전엔 없는 파일도
          // 0.0 MB 카드로 광고해 누르면 404 였다). 비어 있으면 원인을 알려 준다.
          <p className="text-muted-foreground text-sm">
            다운로드 가능한 패키지가 없습니다. 관리자가{' '}
            <code className="bg-muted px-1 rounded">gateway-cli/package.sh</code> 로 빌드한 뒤
            admin-api 의 <code className="bg-muted px-1 rounded">CLI_DIST_DIR</code> 에 마운트해야
            합니다. 그동안은 위의 소스 설치 절차를 사용하세요.
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
