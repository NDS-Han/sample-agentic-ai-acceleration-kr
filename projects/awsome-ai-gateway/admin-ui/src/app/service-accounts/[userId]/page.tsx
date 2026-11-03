// /service-accounts/{id} → /agents/{id} 이전 redirect stub.
import { redirect } from 'next/navigation';

export default async function ServiceAccountDetailRedirect(props: {
  params: Promise<{ userId: string }>;
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const { userId } = await props.params;
  const sp = await props.searchParams;
  const qs = new URLSearchParams(
    Object.entries(sp).filter((e): e is [string, string] => !!e[1]),
  ).toString();
  redirect(`/agents/${userId}${qs ? `?${qs}` : ''}`);
}
