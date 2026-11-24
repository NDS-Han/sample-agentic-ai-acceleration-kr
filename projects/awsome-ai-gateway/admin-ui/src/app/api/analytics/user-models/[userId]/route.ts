// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { NextRequest } from 'next/server';
import { isUuid } from '@/components/analytics/userModel';
import { pickParams, proxyUserModels } from '../proxy';

const PARAMS = ['period', 'start_date', 'end_date', 'scope', 'client'] as const;

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> },
) {
  const { userId } = await params;
  // 경로 세그먼트를 상류 URL 에 그대로 붙이므로 UUID 만 통과('../' 등 경로 조작 차단).
  if (!isUuid(userId)) {
    return Response.json(
      { error: { type: 'validation_error', message: 'invalid user id', code: 'invalid_user_id' } },
      { status: 400 },
    );
  }
  return proxyUserModels(
    `/admin/analytics/user-models/${encodeURIComponent(userId)}`,
    pickParams(request.nextUrl.searchParams, PARAMS),
  );
}
