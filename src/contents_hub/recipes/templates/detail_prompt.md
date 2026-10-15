# Detail Prompt

contents-hub가 결과를 저장하고 중복을 판별하므로 파일이나 DB에 직접 쓰지 마세요.

당신은 recipe 의 **CONTENT_STRATEGY + METADATA 만** 실행하는 에이전트입니다. URL 목록을 받아 각 URL 의 본문/메타를 추출합니다. **LIST_STRATEGY (피드 페이지네이션 / 더보기 클릭) 는 절대 실행하지 마세요** — 입력으로 받은 URL 외에 추가 URL 을 발견하거나 수집하지 않습니다.

{browser_instructions}

## 입력
- subscription URL (참고용): {url}
- source_type: {source_type}
- recipe (자연어 지시문):

```
{recipe}
```

- user collection guidance (subscription-specific, optional):

```
{collection_prompt}
```

- 추출 대상 URL 목록 ({n_urls} 개):

```json
{urls_json}
```

## 해야 할 일

1. 입력 URL 목록의 **각 URL 에 대해** recipe 의 `CONTENT_STRATEGY` 와 `METADATA` 를 적용해 본문/메타를 수집한다. user collection guidance 가 상세 본문 추출 방식에도 관련되면 함께 반영한다.
2. 각 URL 별로 브라우저 세션 한 개를 재사용해도 되고, 새로 열어도 됨. 끝나면 닫는다.
3. 한 URL 의 추출이 실패해도 **다른 URL 의 추출은 계속 진행**. 실패한 URL 은 `errors` 에 사유를 기록하되 `items` 에는 포함하지 않는다.
   단, 페이지 제목/description/게시일/첫 의미 있는 본문 일부라도 확인되면 실패로 처리하지 말고 `body_status="partial"` 인 item 을 반환한다. 본문 전체를 못 찾았다는 이유만으로 0 items 를 반환하지 않는다.
4. **추가 URL 발견 / 피드 페이지네이션 / 더보기 클릭 절대 금지.** 입력 목록 그대로만 처리.
5. 아래 JSON 스키마로 결과를 반환한다. **응답 본문에 직접 출력**.

```json
{{
  "items": [
    {{
      "url": "https://...",
      "title": "...",
      "author": "...",
      "published_at": "ISO 8601 UTC",
      "body_markdown": "...",
      "body_status": "full | partial | empty"
    }}
  ],
  "errors": [],
  "failure_reason": null
}}
```

## 규칙

- 모든 URL 에 대해 추출이 실패하면 `failure_reason` 을 명시 (성공이면 `null`):
  - `login_required`, `blocked`, `not_found`, `structure_changed`, `timeout`, `network`, `unknown`
- 일부 성공 / 일부 실패는 `failure_reason=null` 로 두고 실패 항목만 `errors` 에 기록.
- `published_at` 은 반드시 ISO 8601 UTC.
- 본문(`body_markdown`) 이 길면 아이템당 20000자로 잘라서 포함. recipe 가 더 작은 상한을 지정하면 그걸 우선.
- full body selector 가 깨졌거나 페이지가 일부만 보이면 meta description, og:title/og:description, article 첫 문단, visible main text 중 가능한 조합으로 `partial` item 을 구성한다.
- 결과 JSON 의 `items` 순서는 입력 URL 순서를 따른다.
