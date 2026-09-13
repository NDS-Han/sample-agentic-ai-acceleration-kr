// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { SkeletonTable } from '@/components/common/SkeletonTable';

export default function AppsLoading() {
  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between mb-6">
        <div className="h-8 w-40 bg-muted rounded animate-pulse" />
      </div>
      <SkeletonTable rows={4} columns={3} />
    </div>
  );
}
