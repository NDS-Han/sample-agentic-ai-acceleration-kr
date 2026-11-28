# 모델 관리

관리자로 로그인하고 왼쪽 메뉴에서 **Model Management**를 선택합니다. 모델별 상태, 컨텍스트 크기, 입력·출력 단가, 캐시 단가를 비교할 수 있습니다. 모델 행을 펼치면 상세 단가를 확인할 수 있습니다.

- **모델 추가** · **Add model**을 선택한 뒤 모델 식별자, 별칭, 단가 등 필요한 정보를 입력합니다. 단가는 화면에 표시된 `USD / 1M tokens` 기준으로 확인합니다.
- **모델 편집** · 해당 행에서 **Edit**를 선택합니다. 별칭으로 모델을 찾고 변경할 항목과 적용 단위를 확인합니다.
- **모델 비활성화** · [Deactivate 확인 창](screenshots/admin/67-model-deactivate-confirm.png)에서 대상을 다시 확인합니다. 비활성화가 적용되면 해당 모델의 새 요청이 거절됩니다.
- **모델 삭제** · [Delete 확인 창](screenshots/admin/29-model-delete-confirm.png)에서 가격 이력·허용 목록·제한·다운그레이드 규칙에 미치는 영향을 확인합니다. 사용량 이력은 보존됩니다. 다른 모델의 다운그레이드 목적지로 쓰이는 별칭은 관련 규칙을 정리한 뒤 삭제할 수 있습니다.
- **단가 동기화** · [Sync Pricing 창](screenshots/admin/66-model-pricing-sync.png)에서 소스를 선택해 차이를 미리 봅니다. 반영할 모델을 선택한 뒤 적용합니다.

[관리자 화면 모음](admin-screens.md)의 **Model Management**에서 모델 목록, 상세, 등록·편집 양식과 삭제 확인 창을 볼 수 있습니다.

팀·사용자별 모델 허용 정책은 **Users/Teams**의 상세 화면에서 확인합니다.

[가이드 목차](README.md) · [관리자 UI 및 기능](admin-ui.md)
