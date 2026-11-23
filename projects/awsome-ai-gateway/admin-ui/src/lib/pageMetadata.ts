// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { Metadata } from 'next';
import { getTranslations } from 'next-intl/server';

/**
 * 페이지 탭 제목 — 루트 layout 의 title.template("%s — <브랜드> Admin")에 넣을 %s 를
 * 현재 로케일 메시지에서 읽는다. 사이드바와 같은 nav.* 라벨을 써서 메뉴명과 탭 제목이
 * 어긋나지 않게 한다. 사용: `export const generateMetadata = pageTitle('nav', 'keys');`
 */
export function pageTitle(namespace: string, key: string): () => Promise<Metadata> {
  return async function generateMetadata(): Promise<Metadata> {
    const t = await getTranslations(namespace);
    return { title: t(key) };
  };
}
