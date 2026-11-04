// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { SdkSamples } from '@/components/integration/SdkSamples';
import { highlightCode } from '@/lib/highlight';
import { getTranslations } from 'next-intl/server';

function gatewayUrl(): string {
  return ((process.env['GATEWAY_INGRESS_URL'] ?? '').trim()) || "https://'<gateway-host>'";
}

/** 외부 앱 연동 샘플 — 실제 배포 gateway URL 을 심어서 복사만으로 동작하게 한다. */
async function buildAppSamples() {
  const g = gatewayUrl();
  const claude = {
    curl: [
      `curl -s "${g}/v1/messages" \\`,
      `  -H "Authorization: Bearer $SERVICE_VK" \\`,
      `  -H "Content-Type: application/json" \\`,
      `  -d '{`,
      `    "model": "<model-alias>",`,
      `    "max_tokens": 256,`,
      `    "messages": [{"role": "user", "content": "Hello"}]`,
      `  }'`,
    ].join('\n'),
    python: [
      `import os`,
      `from anthropic import Anthropic`,
      ``,
      `client = Anthropic(`,
      `    base_url="${g}",`,
      `    api_key=os.environ["SERVICE_VK"],`,
      `)`,
      `resp = client.messages.create(`,
      `    model="<model-alias>",`,
      `    max_tokens=256,`,
      `    messages=[{"role": "user", "content": "Hello"}],`,
      `)`,
    ].join('\n'),
  };
  const openai = {
    curl: [
      `curl -s "${g}/v1/chat/completions" \\`,
      `  -H "Authorization: Bearer $SERVICE_VK" \\`,
      `  -H "Content-Type: application/json" \\`,
      `  -d '{`,
      `    "model": "<model-alias>",`,
      `    "max_tokens": 256,`,
      `    "messages": [{"role": "user", "content": "Hello"}]`,
      `  }'`,
    ].join('\n'),
    python: [
      `import os`,
      `from openai import OpenAI`,
      ``,
      `client = OpenAI(`,
      `    base_url="${g}/v1",`,
      `    api_key=os.environ["SERVICE_VK"],`,
      `)`,
      `resp = client.chat.completions.create(`,
      `    model="<model-alias>",`,
      `    max_tokens=256,`,
      `    messages=[{"role": "user", "content": "Hello"}],`,
      `)`,
    ].join('\n'),
  };
  const langchain = {
    curl: [
      `# LangChain 은 HTTP 직접 호출이 아니라 SDK 래퍼를 쓴다 —`,
      `# OpenAI 계열 alias: ChatOpenAI + /v1/chat/completions`,
      `# Claude 계열 alias:  ChatAnthropic + /v1/messages`,
    ].join('\n'),
    python: [
      `import os`,
      `from langchain_openai import ChatOpenAI`,
      `from langchain_anthropic import ChatAnthropic`,
      ``,
      `# OpenAI 계열 alias (gpt-*, codex-* 등)`,
      `gpt = ChatOpenAI(`,
      `    model="<model-alias>",`,
      `    api_key=os.environ["SERVICE_VK"],`,
      `    base_url="${g}/v1",`,
      `    max_tokens=256,`,
      `)`,
      ``,
      `# Claude 계열 alias (claude-*)`,
      `claude = ChatAnthropic(`,
      `    model="<model-alias>",`,
      `    api_key=os.environ["SERVICE_VK"],`,
      `    base_url="${g}",`,
      `    max_tokens=256,`,
      `)`,
      ``,
      `resp = gpt.invoke("Hello")`,
    ].join('\n'),
  };
  async function block(text: string, lang: 'bash' | 'python', filename: string) {
    return { html: await highlightCode(text, lang), text, filename };
  }
  return {
    claude: {
      curl: await block(claude.curl, 'bash', 'request.sh'),
      python: await block(claude.python, 'python', 'agent.py'),
    },
    openai: {
      curl: await block(openai.curl, 'bash', 'request.sh'),
      python: await block(openai.python, 'python', 'app.py'),
    },
    langchain: {
      curl: await block(langchain.curl, 'bash', 'note.sh'),
      python: await block(langchain.python, 'python', 'agent.py'),
    },
  };
}

export default async function IntegrationPage() {
  const t = await getTranslations('integration');
  const code = (chunks: React.ReactNode) => (
    <code className="bg-muted px-1 rounded">{chunks}</code>
  );

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">{t('title')}</h1>
        <p className="text-muted-foreground mt-2 text-sm">{t('subtitle')}</p>
      </header>

      {/* 외부 앱 연동 — 서비스 VK 로 gateway 호출하는 샘플 */}
      <section className="glass glass-hover rounded-apple p-6 space-y-3">
        <h2 className="text-base font-semibold">{t('appIntegTitle')}</h2>
        <p className="text-sm text-muted-foreground">{t('appIntegDesc')}</p>
        <SdkSamples samples={await buildAppSamples()} />
        <ul className="list-disc list-inside space-y-1 text-xs text-muted-foreground">
          <li>{t('appIntegNoteVk')}</li>
          <li>{t('appIntegNoteAskAdmin')}</li>
          <li>{t('appIntegNoteAlias')}</li>
          <li>{t('appIntegNote401')}</li>
          <li>{t('appIntegNoteEndpoint')}</li>
          <li>{t.rich('appIntegNoteUa', { code })}</li>
        </ul>
      </section>
    </div>
  );
}
