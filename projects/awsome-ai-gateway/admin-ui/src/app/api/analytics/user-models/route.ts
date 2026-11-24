// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { NextRequest } from 'next/server';
import { pickParams, proxyUserModels } from './proxy';

const PARAMS = [
  'period',
  'start_date',
  'end_date',
  'scope',
  'client',
  'sort',
  'dir',
  'page',
  'page_size',
  'q',
] as const;

export async function GET(request: NextRequest) {
  return proxyUserModels(
    '/admin/analytics/user-models',
    pickParams(request.nextUrl.searchParams, PARAMS),
  );
}
