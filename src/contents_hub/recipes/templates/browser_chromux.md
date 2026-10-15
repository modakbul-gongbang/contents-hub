## 브라우저 도구 (중요)

브라우저 탐색이 필요하면 **반드시 `chromux_navigate` / `chromux_extract` 도구를 먼저 사용하고**, 다른 browse 도구(gstack browse, playwright 등)는 절대 쓰지 마세요. 로그인 세션은 `contents-hub` 프로필에 보존됩니다.

도구가 `ok=false` 를 반환해 진행할 수 없을 때만 Bash fallback 을 사용합니다. 일반적인 탐색/속성 추출/링크 추출에는 Bash 를 쓰지 마세요. 이 경우에도 `chromux launch` 는 호출하지 마세요. `chromux open` 이 필요한 프로필을 자동으로 시작합니다.

```bash
CHROMUX_PROFILE=contents-hub CHROMUX_LAUNCH_MODE=headed CHROMUX_OPEN_BACKGROUND=1 chromux open <session> <url>
CHROMUX_PROFILE=contents-hub chromux snapshot <session>
CHROMUX_PROFILE=contents-hub chromux click <session> @<ref>
CHROMUX_PROFILE=contents-hub chromux run <session> "return await js('<js expression>')"
CHROMUX_PROFILE=contents-hub chromux cdp <session> Runtime.evaluate '{{"expression":"location.href","returnByValue":true}}'
CHROMUX_PROFILE=contents-hub chromux close <session>
```

- 프로필 지정은 `CHROMUX_PROFILE=<name>` env var 또는 `chromux --profile <name> ...` 둘 다 가능하다. 이 repo의 기본 예시는 env var를 사용한다.
- 새 로그인은 `contents-hub` 프로필을 사용한다.
- `chromux launch` 는 profile lock 에서 멈출 수 있으므로 실행하지 말 것.
- `chromux_extract` 는 `selector`, `mode`, `attributes`, `multiple`, `limit` 를 지원한다. DOM 속성 수집은 Bash `run` 대신 `chromux_extract(..., attributes=[...], multiple=true)` 로 한다.
- 피드/검색 결과처럼 스크롤하며 같은 카드 구조를 반복 수집할 때는 `chromux_scroll_extract(session_id=<session>, selector=<card_selector>, attributes=[...], max_scrolls=<n>, max_items=<limit>, unique_by=<field>)` 를 먼저 사용한다. 직접 Bash/AppleScript 로 스크롤 루프를 만들지 않는다.
- 단순 위치 이동만 필요하면 `chromux_scroll` 을 사용한다.
- 로그인이 필요한 사이트(LinkedIn, X, Substack 등)에서 `/login` / `/uas/login` 으로 리다이렉트되면 인증 실패로 즉시 중단하고 `errors` 에 `"Login required: ..."` 로 기록.
- 작업 끝나면 반드시 `close` 로 세션 정리.
