// /service-accounts → /agents 이전. 기존 링크·북마크 호환을 위한 redirect stub.
import { redirect } from 'next/navigation';

export default function ServiceAccountsRedirect() {
  redirect('/agents');
}
