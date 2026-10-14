// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import createNextIntlPlugin from 'next-intl/plugin';

const withNextIntl = createNextIntlPlugin('./src/i18n/request.ts');

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  experimental: {},
  // 보안 헤더는 middleware.ts 가 단일 소스다 (R17-S7) — 여기와 이중 정의 시
  // 브라우저가 두 CSP 의 교집합을 적용해 실효 정책 추적이 불가능했다.
};

export default withNextIntl(nextConfig);
