// sse-hold.js — Bedrock-native SSE 동시성 부하 시나리오 (k6)
//
// 목적: claude-code 경로의 executor 스레드 점유 모델을 재현한다.
//   k6 VU 1개 = SSE 연결 1개 — 스트림이 닫힐 때까지 VU 가 점유되므로
//   `vus` 수 = 동시 스트림 수. 각 VU 는 응답 바디가 끝날 때까지 블로킹된다.
//
// 사전 조건:
//   1) mock-bedrock 배포 + gatewayProxy.env 에
//      AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://mock-bedrock... 배선
//      (values-eks-fargate-prod-loadtest-sse.yaml)
//   2) 시나리오 모델 별칭 시드 (provider_model_id: mock-normal, mock-think-120 …)
//   3) VK 발급 → K6_API_KEY
//
// 실행:
//   k6 run -e BASE_URL=https://gw.example.com \
//          -e K6_API_KEY=sk-... \
//          -e MODEL=mock-normal \
//          -e VUS=200 -e HOLD=180s \
//          loadtest/k6/sse-hold.js
//
// 관측 포인트 (Prometheus/CloudWatch):
//   - gateway_bedrock_executor_inflight / _queue_depth (이번에 추가한 계측)
//   - TTFB — 큐 대기 시작 시점부터 선형으로 늘어나는 게 starvation 신호
//   - HPA 반응 여부 — CPU 낮은데 큐가 쌓이면 사각지대 실증
//   - stream_idle_timeout (dev 240s) 이 스트림을 끊는지 — think-N 은 N<240 유지
//
// 시나리오 혼합: MODEL 을 콤마로 주면 VU 가 라운드로빈 배정된다
//   -e MODEL=mock-normal,mock-think-120   (thinking 공백 스트림 섞기 — 스레드 장기 점유)

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const API_KEY = __ENV.K6_API_KEY || '';
const MODELS = (__ENV.MODEL || 'mock-normal').split(',');
const HOLD = __ENV.HOLD || '180s';       // VU 당 스트림 유지 목표 (mock hold/think 값과 맞출 것)
const VUS = parseInt(__ENV.VUS || '50', 10);

const sseTtfb = new Trend('sse_ttfb_ms', true);
const sseErrors = new Counter('sse_error_responses');
const sseCompleted = new Counter('sse_completed');

export const options = {
  scenarios: {
    hold: {
      executor: 'constant-vus',
      vus: VUS,
      duration: HOLD,
      // gracefulStop: VU 의 in-flight 요청이 끝날 때까지 기다림 — 스트림이
      // duration 안에 안 끝나도 자연 종료시켜 정상 경로를 본다.
      gracefulStop: '30s',
    },
  },
  // 개별 요청(=스트림) 최대 수명 — mock-think-N / idle timeout 보다 크게.
  // 이 시간을 넘기면 k6 이 연결을 끊는다 = 클라이언트 이탈 경로도 커버.
  thresholds: {
    sse_ttfb_ms: ['p(95)<30000'],   // 큐 대기 시작되면 TTFB 가 폭증 — 첫 임계치 후보
  },
};

export default function () {
  const model = MODELS[__VU % MODELS.length];
  const payload = JSON.stringify({
    model: model,
    max_tokens: 512,
    messages: [{ role: 'user', content: 'loadtest' }],
    stream: true,
  });

  const res = http.post(`${BASE_URL}/v1/messages`, payload, {
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': API_KEY,
      'anthropic-version': '2023-06-01',
    },
    // 스트림 수명보다 크게 — mock-hold-300 이면 >300s 필요.
    timeout: '600s',
    // 바디는 버린다 — 목적은 연결 점유지 데이터 수집이 아니다.
    // 단 k6 는 응답 종료까지 VU 를 잡아두므로 점유 모델은 유지된다.
    responseType: 'none',
    tags: { model: model },
  });

  sseTtfb.add(res.timings.waiting);
  if (res.status === 200) {
    sseCompleted.add(1);
  } else {
    sseErrors.add(1, { status: String(res.status), model: model });
  }
  check(res, { 'stream ok': (r) => r.status === 200 });
}
