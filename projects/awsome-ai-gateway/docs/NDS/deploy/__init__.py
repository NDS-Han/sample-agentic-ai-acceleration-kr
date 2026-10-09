# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""gateway.yaml 기반 통합 배포 도구.

한 파일(gateway.yaml)이 배포의 source of truth. 세 backend(compose/ecs/eks)가
같은 스키마를 읽고 각자의 산출물을 렌더한다 — "generate, don't mutate".
"""
