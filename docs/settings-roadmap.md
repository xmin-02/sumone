# Settings 고도화 로드맵

> 현재 구현된 설정 항목을 기반으로 향후 추가할 만한 기능 목록.
> 우선순위 순 정렬.

---

## 1. 인라인 키보드 ↔ 웹 설정 동기화 (HIGH)

현재 인라인 키보드(fallback)에는 4개 토글만 있고, 웹 설정 페이지에는 훨씬 많은 항목이 있음.
웹을 사용할 수 없는 환경에서 인라인 키보드가 빈약함.

**추가 대상 토글:**
- `show_typing` — 타이핑 애니메이션
- `auto_viewer_link` — 파일 변경 시 뷰어 링크 자동 전송
- `viewer_link_fixed` — 고정 URL 사용

**작업:** `i18n/ko.json`, `i18n/en.json`의 `settings.keys` 배열에 항목 추가.

---

## 2. 큐 최대 크기 설정 (HIGH)

현재 메시지 큐는 무제한. 실수로 메시지가 쌓이는 상황 방지 필요.

```python
# config.py DEFAULT_SETTINGS에 추가
"max_queue_size": 10,
```

```python
# main.py handle_message()에서 큐 추가 전 체크
if len(state.message_queue) >= settings.get("max_queue_size", 10):
    send_html(f"<i>{t('queue_full')}</i>")
    return
```

**웹 설정:** System 섹션에 number input 추가.

---

## 3. 상태 메시지 compact 모드 (MEDIUM)

현재 `show_status`는 ON/OFF만 있음. 도구 사용을 한 줄 요약으로만 보여주는 compact 모드 추가.

```python
# DEFAULT_SETTINGS
"compact_status": False,
```

- OFF (기본): 현재처럼 각 도구 사용마다 메시지
- ON: 처리 완료 후 "🔧 Read×3 · Bash×1 · Edit×2" 형식 한 줄 요약

---

## 4. Footer 항목 개별 제어 (MEDIUM)

현재 `show_cost`가 ON이면 비용/시간/토큰 전부 표시됨. 항목별 토글 필요.

```python
# DEFAULT_SETTINGS
"footer_show_cost": True,
"footer_show_duration": True,
"footer_show_tokens": True,
```

또는 체크박스 형태의 단일 설정:
```python
"footer_items": ["cost", "duration", "tokens"],  # 표시할 항목 리스트
```

---

## 5. 큐 처리 시작 알림 (MEDIUM)

큐잉된 메시지가 처리 시작될 때 별도 메시지 전송.

```python
# DEFAULT_SETTINGS
"queue_notify": True,
```

```python
# main.py _process_queue()에서
if settings.get("queue_notify", True):
    send_html(f"<i>{t('queue_started')}</i>")
```

---

## 6. 세션 자동 초기화 (LOW)

N일 이상 대화가 없으면 세션 자동 클리어.

```python
# DEFAULT_SETTINGS
"auto_clear_days": 0,  # 0 = 비활성
```

봇 시작 시 마지막 활동 시간 체크 → 초과 시 session_id 초기화.

---

## 7. 응답 최대 길이 설정 (LOW)

현재 `MAX_MSG_LEN = 3900` 하드코딩. 사용자 환경에 따라 조정 가능하게.

```python
# DEFAULT_SETTINGS
"max_msg_length": 3900,
```

---

## 현재 설정 항목 현황

| 키 | 타입 | 웹 설정 | 인라인 키보드 |
|---|---|---|---|
| `show_cost` | toggle | ✅ | ✅ |
| `show_status` | toggle | ✅ | ✅ |
| `show_global_cost` | toggle | ✅ | ✅ |
| `show_remote_tokens` | toggle | ✅ | ✅ |
| `show_typing` | toggle | ✅ | ❌ |
| `auto_viewer_link` | toggle | ✅ | ❌ |
| `viewer_link_fixed` | toggle | ✅ | ❌ |
| `token_display` | select | ✅ | ✅ |
| `theme` | select | ✅ | ❌ |
| `token_ttl` | select | ✅ | ❌ |
| `snapshot_ttl_days` | number | ✅ | ❌ |
| `default_model` | select | ✅ | ❌ |
| `default_sub_model` | select | ✅ | ❌ |
| `settings_timeout_minutes` | number | ✅ | ❌ |
| `work_dir` | text | ✅ | ❌ |
