'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import type { ModelListItem } from '@/types/entities';
import { createModelAction, updateModelAction } from '@/lib/actions/models';
import { AppDialog } from '@/components/common/AppDialog';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { InfoTooltip } from '@/components/common/InfoTooltip';
import { useToast } from '@/components/common/ToastProvider';
import { NumberInput } from '@/components/common/NumberInput';

interface CreateModelDialogProps {
  isOpen: boolean;
  onClose: () => void;
  editModel?: ModelListItem;
}

interface FormState {
  alias: string;
  provider: string;
  model_id: string;
  endpoint_url: string;
  // 운영자가 보는 외부 카탈로그는 USD/1M 기준 — 폼도 1M 으로 받고 API 전송 시 ÷1000 한다.
  // (DB/API 컬럼은 *_per_1k_tokens Numeric(12,8) — 최소 단위 $0.00001/M.
  //  Haiku 5.5 캐시 쓰기 $0.1375/M 처럼 1/1000 달러보다 고운 단가가 있다 — US-19)
  input_price_per_1m: string;
  output_price_per_1m: string;
  cache_creation_5m_price_per_1m: string;
  cache_creation_1h_price_per_1m: string;
  cache_read_price_per_1m: string;
  description: string;
  display_name: string;
  context_window: string;
  max_output_tokens: string;
}

/** 1K 단가 → 1M 표시값. DB 는 6자리 소수라 ×1000 은 3자리까지 의미가 있고,
    *  부동소수점 찌꺼기(0.00465*1000=4.6499…)는 toFixed(6) 로 잘라낸다. */
function perKtoM(v: number): string {
  return String(parseFloat((v * 1000).toFixed(6)));
}

/** 1M 입력값 → API/DB 의 1K 단가. */
function perMtoK(v: string): number {
  return parseFloat(v) / 1000;
}

function getInitialState(editModel?: ModelListItem): FormState {
  if (editModel) {
    return {
      alias: editModel.alias,
      provider: editModel.provider,
      model_id: editModel.model_id,
      endpoint_url: editModel.endpoint_url ?? '',
      input_price_per_1m: perKtoM(editModel.input_price_per_1k),
      output_price_per_1m: perKtoM(editModel.output_price_per_1k),
      cache_creation_5m_price_per_1m: perKtoM(editModel.cache_creation_5m_price_per_1k),
      cache_creation_1h_price_per_1m: perKtoM(editModel.cache_creation_1h_price_per_1k),
      cache_read_price_per_1m: perKtoM(editModel.cache_read_price_per_1k),
      description: editModel.description ?? '',
      display_name: editModel.display_name ?? '',
      context_window: editModel.context_window != null ? String(editModel.context_window) : '',
      max_output_tokens: editModel.max_tokens != null ? String(editModel.max_tokens) : '',
    };
  }
  return {
    alias: '',
    provider: '',
    model_id: '',
    endpoint_url: '',
    input_price_per_1m: '',
    output_price_per_1m: '',
    cache_creation_5m_price_per_1m: '',
    cache_creation_1h_price_per_1m: '',
    cache_read_price_per_1m: '',
    description: '',
    display_name: '',
    context_window: '',
    max_output_tokens: '',
  };
}

export function CreateModelDialog({ isOpen, onClose, editModel }: CreateModelDialogProps) {
  const t = useTranslations('models');
  const tCommon = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [form, setForm] = useState<FormState>(() => getInitialState(editModel));

  const isEditMode = !!editModel;

  useEffect(() => {
    // 열릴 때마다 리셋 — 생성 후 다시 열면 이전 값/에러가 남아 있던 문제.
    if (isOpen) {
      setForm(getInitialState(editModel));
      setError(null);
      setFieldErrors({});
    }
  }, [isOpen, editModel]);

  const handleChange = (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>
  ) => {
    const { name, value } = e.target;
    setForm((prev) => ({ ...prev, [name]: value }));
    if (fieldErrors[name]) {
      setFieldErrors((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setFieldErrors({});

    const payload = {
      alias: form.alias,
      provider: form.provider,
      model_id: form.model_id,
      endpoint_url: form.endpoint_url,
      input_price_per_1k: perMtoK(form.input_price_per_1m),
      output_price_per_1k: perMtoK(form.output_price_per_1m),
      cache_creation_5m_price_per_1k: perMtoK(form.cache_creation_5m_price_per_1m || '0'),
      cache_creation_1h_price_per_1k: perMtoK(form.cache_creation_1h_price_per_1m || '0'),
      cache_read_price_per_1k: perMtoK(form.cache_read_price_per_1m || '0'),
      description: form.description || undefined,
      display_name: form.display_name || undefined,
      // 빈 문자열 → 키 생략(생성: 서버 default null / 편집: action 이 null 로 바꿔
      // "미상으로 되돌림"). 숫자 아닌 값은 NaN 으로 보내 zod 가 필드 에러로 돌려준다.
      ...(form.context_window.trim() !== '' && { context_window: Number(form.context_window) }),
      ...(form.max_output_tokens.trim() !== '' && { max_output_tokens: Number(form.max_output_tokens) }),
      // 편집 모드: 가격이 안 바뀌면 pricing PUT 을 건너뛰라는 힌트(불필요한 버전 생성 방지).
      ...(isEditMode && {
        pricing_changed: (
          [
            ['input_price_per_1m', 'input_price_per_1k'],
            ['output_price_per_1m', 'output_price_per_1k'],
            ['cache_creation_5m_price_per_1m', 'cache_creation_5m_price_per_1k'],
            ['cache_creation_1h_price_per_1m', 'cache_creation_1h_price_per_1k'],
            ['cache_read_price_per_1m', 'cache_read_price_per_1k'],
          ] as const
        ).some(([formKey, apiKey]) => {
          const orig = editModel[apiKey];
          return Math.abs(perMtoK(form[formKey] || '0') - (Number(orig) || 0)) > 1e-9;
        }),
      }),
    };

    startTransition(async () => {
      const result = isEditMode
        ? await updateModelAction(editModel.alias, payload)
        : await createModelAction(payload);

      if (result.success) {
        toast({
          type: 'success',
          message: isEditMode
            ? t('editSuccess', { alias: form.alias })
            : t('createSuccess', { alias: form.alias }),
          auto_dismiss_ms: 3000,
        });
        onClose();
      } else {
        // 필드 에러 중 이 폼에 대응 입력란이 없는 키는 아래 JSX 로 렌더될 수 없어 그냥 사라진다.
        // 실제로 그래서 max_tokens/context_window 스키마 드리프트가 원인 없는 "Validation failed"
        // 로만 보였다. 렌더 불가한 키는 상단 에러 메시지에 합쳐 항상 화면에 노출한다.
        const fe = (!result.success && result.fieldErrors) || {};
        // API/zod 는 *_per_1k 키로 에러를 돌려준다 — 폼은 *_per_1m 이므로 되돌려
        // 필드 아래 인라인으로 붙인다(안 하면 orphan 키로 상단 에러에만 합산됨).
        for (const k of Object.keys(fe)) {
          if (k.endsWith('_per_1k')) {
            fe[k.replace(/_per_1k$/, '_per_1m')] = fe[k];
            delete fe[k];
          }
        }
        const orphanKeys = Object.keys(fe).filter((k) => !(k in form));
        setError(
          orphanKeys.length
            ? `${result.error}: ${orphanKeys.map((k) => `${k} (${fe[k]})`).join(', ')}`
            : result.error
        );
        setFieldErrors(fe);
      }
    });
  };

  return (
    <AppDialog
      isOpen={isOpen}
      onClose={onClose}
      title={isEditMode ? t('editModel') : t('createModel')}
    >
      <form onSubmit={handleSubmit} className="space-y-4 mt-4">
          {/* Alias */}
          <div className="space-y-1">
            <div className="flex items-center gap-1">
              <label htmlFor="alias" className="text-sm font-medium">
                Alias <span className="text-destructive">*</span>
              </label>
              {/* label 안에 두면 버튼이 필드 라벨로 연결돼 접근성 쿼리가
                  'Alias' 를 버튼에도 잡는다 — label 밖 형제로 둔다 */}
              <InfoTooltip label={t('fieldHelp')} side="top">{t('aliasTooltip')}</InfoTooltip>
            </div>
            <input
              id="alias"
              name="alias"
              type="text"
              value={form.alias}
              onChange={handleChange}
              required
              disabled={isEditMode}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
              placeholder="e.g. claude-3-5-sonnet"
            />
            <p className="text-xs text-muted-foreground">
              {t(isEditMode ? 'aliasReadonly' : 'aliasHintInline')}
            </p>
            {fieldErrors.alias && <FormError error={fieldErrors.alias} />}
          </div>

          {/* Provider — ⚠️ 편집 모드에서는 alias 와 마찬가지로 읽기 전용이다.
              PUT /admin/models/{alias} 의 ModelUpdateRequest 에는 provider/api_format 이
              없고(admin-api/src/app/schemas/models.py) update 서비스·리포지토리에도 변경
              경로가 없어 admin API 로는 바꿀 수 없는 불변 필드다. 예전엔 여기서 드롭다운을
              바꿀 수 있었고 updateModelAction 은 provider 를 아예 보내지도 않으므로
              (lib/actions/models.ts:95~) 성공 토스트만 뜨고 값은 반영되지 않았다 —
              화면과 DB 가 갈라지는 조용한 실패. 지금은 스키마가 422 로 거부하기도 하지만,
              애초에 저장 못 하는 컨트롤을 열어 두지 않는다. */}
          <div className="space-y-1">
            <label htmlFor="provider" className="text-sm font-medium">
              Provider <span className="text-destructive">*</span>
            </label>
            <select
              id="provider"
              name="provider"
              value={form.provider}
              onChange={handleChange}
              required
              disabled={isEditMode}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-60 disabled:cursor-not-allowed"
            >
              <option value="">{t('selectProvider')}</option>
              <option value="BEDROCK">BEDROCK</option>
              <option value="OPENMODEL">OPENMODEL</option>
              {/* Mantle 계열은 endpoint_url·api_format 이 필요해 보통 마이그레이션으로 시드되지만,
                  기존 cowork-opus / codex-gpt 모델 편집(단가 등) 시 provider 드롭다운이 값과
                  매칭되도록 옵션을 노출한다. */}
              <option value="BEDROCK_MANTLE">BEDROCK_MANTLE (Cowork · Opus)</option>
              <option value="BEDROCK_MANTLE_OPENAI">BEDROCK_MANTLE_OPENAI (Codex · GPT)</option>
              {/* 표준 bedrock-runtime plane (SigV4 + CRIS). Mantle 과 달리 endpoint_url 이
                  **필수**다 — 어댑터가 endpoint 호스트에서 서명 리전을 뽑아내므로
                  (bedrock-runtime.{region}.amazonaws.com) 비워 두면 서명할 수 없다.
                  provider_model_id 는 us./global. 접두사가 붙은 추론 프로파일 ID 여야 한다. */}
              <option value="BEDROCK_RUNTIME_OPENAI">BEDROCK_RUNTIME_OPENAI (GPT-5.6 · CRIS)</option>
            </select>
            {isEditMode && (
              <p className="text-xs text-muted-foreground">
                {t('providerReadonly')}
              </p>
            )}
            {fieldErrors.provider && <FormError error={fieldErrors.provider} />}
          </div>

          {/* Model ID */}
          <div className="space-y-1">
            <div className="flex items-center gap-1">
              <label htmlFor="model_id" className="text-sm font-medium">
                Model ID <span className="text-destructive">*</span>
              </label>
              <InfoTooltip label={t('fieldHelp')} side="top">{t('modelIdTooltip')}</InfoTooltip>
            </div>
            <input
              id="model_id"
              name="model_id"
              type="text"
              value={form.model_id}
              onChange={handleChange}
              required
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              placeholder="e.g. anthropic.claude-3-5-sonnet-20241022-v2:0"
            />
            {fieldErrors.model_id && <FormError error={fieldErrors.model_id} />}
          </div>

          {/* Endpoint URL — BEDROCK(native) 외 provider 는 필수: 어댑터가
              호스트/리전을 이 값에서 뽑으므로 비우면 런타임에만 깨진다.
              편집 시 비워 저장하면 서버가 무시해 "지워진 줄 아는" 조용한 불일치가
              생기므로, endpoint 필요 provider 에서는 지울 수 없게 한다. */}
          {(() => {
            const endpointRequired = isEditMode
              ? editModel.provider !== 'BEDROCK'
              : form.provider !== 'BEDROCK' && form.provider !== '';
            return (
              <div className="space-y-1">
                <label htmlFor="endpoint_url" className="text-sm font-medium">
                  {t('endpointUrl')}{' '}
                  {endpointRequired ? (
                    <span className="text-destructive">*</span>
                  ) : (
                    <span className="text-muted-foreground text-xs">{t('endpointUrlOptional')}</span>
                  )}
                </label>
                <input
                  id="endpoint_url"
                  name="endpoint_url"
                  type="text"
                  value={form.endpoint_url}
                  onChange={handleChange}
                  required={endpointRequired}
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  placeholder={t('endpointUrlPlaceholder')}
                />
                {fieldErrors.endpoint_url && <FormError error={fieldErrors.endpoint_url} />}
              </div>
            );
          })()}

          {/* Price fields — 수직 일렬 배치 */}
          <div className="space-y-3">
            <div>
              <span className="text-sm font-medium">{t('priceLabel')}</span>
              <p className="text-xs text-muted-foreground mt-0.5">{t('priceHint')}</p>
            </div>

            <div className="space-y-1">
              <label htmlFor="input_price_per_1m" className="text-xs text-muted-foreground">{t('priceInput')}</label>
              <NumberInput
                id="input_price_per_1m"
                name="input_price_per_1m"
                min={0}
                step={0.00001}
                value={form.input_price_per_1m}
                onChange={handleChange}
                required
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="e.g. 3.00"
              />
              {fieldErrors.input_price_per_1m && <FormError error={fieldErrors.input_price_per_1m} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="output_price_per_1m" className="text-xs text-muted-foreground">{t('priceOutput')}</label>
              <NumberInput
                id="output_price_per_1m"
                name="output_price_per_1m"
                min={0}
                step={0.00001}
                value={form.output_price_per_1m}
                onChange={handleChange}
                required
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="e.g. 3.00"
              />
              {fieldErrors.output_price_per_1m && <FormError error={fieldErrors.output_price_per_1m} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="cache_creation_5m_price_per_1m" className="text-xs text-muted-foreground">{t('priceCacheCreate5m')}</label>
              <NumberInput
                id="cache_creation_5m_price_per_1m"
                name="cache_creation_5m_price_per_1m"
                min={0}
                step={0.00001}
                value={form.cache_creation_5m_price_per_1m}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0"
              />
              {fieldErrors.cache_creation_5m_price_per_1m && <FormError error={fieldErrors.cache_creation_5m_price_per_1m} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="cache_creation_1h_price_per_1m" className="text-xs text-muted-foreground">{t('priceCacheCreate1h')}</label>
              <NumberInput
                id="cache_creation_1h_price_per_1m"
                name="cache_creation_1h_price_per_1m"
                min={0}
                step={0.00001}
                value={form.cache_creation_1h_price_per_1m}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0"
              />
              {fieldErrors.cache_creation_1h_price_per_1m && <FormError error={fieldErrors.cache_creation_1h_price_per_1m} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="cache_read_price_per_1m" className="text-xs text-muted-foreground">{t('priceCacheRead')}</label>
              <NumberInput
                id="cache_read_price_per_1m"
                name="cache_read_price_per_1m"
                min={0}
                step={0.00001}
                value={form.cache_read_price_per_1m}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="0"
              />
              {fieldErrors.cache_read_price_per_1m && <FormError error={fieldErrors.cache_read_price_per_1m} />}
            </div>
          </div>

          {/* Description */}
          <div className="space-y-1">
            <label htmlFor="description" className="text-sm font-medium">
              {t('descriptionLabel')} <span className="text-muted-foreground text-xs">({t('descriptionOptional')})</span>
            </label>
            <textarea
              id="description"
              name="description"
              value={form.description}
              onChange={handleChange}
              rows={3}
              maxLength={512}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring resize-none"
              placeholder={t('descriptionPlaceholder')}
            />
            {fieldErrors.description && <FormError error={fieldErrors.description} />}
          </div>

          {/* Display Name */}
          <div className="space-y-1">
            <label htmlFor="display_name" className="text-sm font-medium">
              {t('displayNameLabel')}
            </label>
            <input
              id="display_name"
              name="display_name"
              type="text"
              maxLength={128}
              value={form.display_name}
              onChange={handleChange}
              placeholder={t('displayNamePlaceholder')}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
            {fieldErrors.display_name && <FormError error={fieldErrors.display_name} />}
          </div>

          {/* Spec — optional. 카탈로그 동기화가 못 채운 모델을 수동 등록할 때
              쓰고, 비우면 NULL(미상)로 저장된다. */}
          <div className="space-y-3">
            <div>
              <span className="text-sm font-medium">
                {t('specLabel')}{' '}
                <span className="text-muted-foreground text-xs">({t('specOptional')})</span>
              </span>
              <p className="text-xs text-muted-foreground mt-0.5">{t('specHint')}</p>
            </div>

            <div className="space-y-1">
              <label htmlFor="context_window" className="text-xs text-muted-foreground">{t('specContextWindow')}</label>
              <NumberInput
                id="context_window"
                name="context_window"
                min={1}
                step={1}
                value={form.context_window}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="e.g. 200000"
              />
              {fieldErrors.context_window && <FormError error={fieldErrors.context_window} />}
            </div>

            <div className="space-y-1">
              <label htmlFor="max_output_tokens" className="text-xs text-muted-foreground">{t('specMaxOutput')}</label>
              <NumberInput
                id="max_output_tokens"
                name="max_output_tokens"
                min={1}
                step={1}
                value={form.max_output_tokens}
                onChange={handleChange}
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder="e.g. 64000"
              />
              {fieldErrors.max_output_tokens && <FormError error={fieldErrors.max_output_tokens} />}
            </div>
          </div>

          <FormError error={error} />

          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              disabled={isPending}
              className="inline-flex items-center justify-center rounded-md border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
            >
              {tCommon('cancel')}
            </button>
            <SpinnerButton type="submit" isLoading={isPending}>
              {isEditMode ? t('edit') : t('register')}
            </SpinnerButton>
          </div>
        </form>
    </AppDialog>
  );
}