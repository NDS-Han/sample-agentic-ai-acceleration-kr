# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import os
import socket

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def init_otel(settings: Settings) -> None:
    global _tracer_provider, _meter_provider

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": settings.app_version,
            # uvicorn 멀티워커: 워커 2개가 동일 레이블셋으로 remote_write 하면
            # 시리즈가 1개로 붕괴해 마지막 writer 값만 남는다(실측:
            # gateway_active_connections count=1). pod-pid 조합으로 워커별
            # 시리즈를 갈라 sum by (pod) 가 파드 총량을 맞게 한다 —
            # executor inflight 가 custom-metric HPA 신호로 쓰이므로 정확도가
            # 곧 스케일링 정확도다. HOSTNAME 은 k8s 가 파드명으로 채운다.
            "service.instance.id": (
                f"{os.environ.get('HOSTNAME') or socket.gethostname()}-{os.getpid()}"
            ),
        }
    )

    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
        )
    )
    trace.set_tracer_provider(_tracer_provider)

    _meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
            )
        ],
    )
    metrics.set_meter_provider(_meter_provider)


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def get_meter(name: str) -> metrics.Meter:
    return metrics.get_meter(name)


async def shutdown_otel() -> None:
    if _tracer_provider:
        _tracer_provider.shutdown()
    if _meter_provider:
        _meter_provider.shutdown()
