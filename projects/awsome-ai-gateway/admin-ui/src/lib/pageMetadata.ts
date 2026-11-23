// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { Metadata } from 'next';
import { getTranslations } from 'next-intl/server';
import { brandName } from '@/lib/branding';

/** 탭 제목 형식 — "<페이지> — <브랜드> Admin". 루트 layout 의 title.template 과 같다. */
export function adminTabTitle(page: string): string {
  return `${page} — ${brandName()} Admin`;
}

/**
 * 페이지 탭 제목 — "<페이지> — <브랜드> Admin". 페이지명은 현재 로케일 메시지에서
 * 읽는다. 사이드바와 같은 nav.* 라벨을 써서 메뉴명과 탭 제목이 어긋나지 않게 한다.
 * 사용: `export const generateMetadata = pageTitle('nav', 'keys');`
 *
 * title.absolute 로 완성된 제목을 준다 — Next 의 title.template 은 그 layout 의
 * **하위** 세그먼트에만 적용돼, 루트 layout 과 같은 세그먼트인 대시보드(/)만
 * 접미사 없이 'Dashboard' 로 보였다.
 */
export function pageTitle(namespace: string, key: string): () => Promise<Metadata> {
  return async function generateMetadata(): Promise<Metadata> {
    const t = await getTranslations(namespace);
    return { title: { absolute: adminTabTitle(t(key)) } };
  };
}
