# journal/

판단 기록을 쌓는 곳. 원문 6.4의 실천이다.

## 쓰는 법

```bash
btc-core score --csv data/market.csv --format markdown --out journal/2026-07.md
```

그다음 [TEMPLATE.md](TEMPLATE.md)의 네 질문을 손으로 채운다.

1. 무엇을 근거로 어떻게 판단했나
2. 실제로 실행한 것 (안 한 것도 포함)
3. **이 판단이 틀렸다면 무엇 때문일 것 같은가**
4. 다음에 확인할 것

3번이 핵심이다. 나중에 앵커를 고칠 때 가장 유용한 자료가 된다.

## 파일명 규칙

`YYYY-MM.md` — 월 1회 정기 기록
`YYYY-MM-DD-이벤트.md` — 사다리 계단을 밟은 날 등 특별한 판단

## git

`journal/20*.md`는 `.gitignore`에 들어 있다. 개인 판단 기록이라 기본적으로
커밋되지 않는다. 남기고 싶으면 `.gitignore`에서 빼거나 `git add -f`로 넣는다.
