# 데이터 출처와 라이선스

이 저장소의 **코드는 MIT**(→ [LICENSE](LICENSE))지만, 함께 배포되는 **데이터는 각
제공처의 조건**을 따른다. 둘은 별개이고, 일부 데이터는 MIT 보다 **엄격하다.**

> **가장 중요한 한 줄:** `data/market.csv` 는 Coin Metrics 커뮤니티 데이터에서 왔고
> **CC BY-NC 4.0(출처표시 + 비상업적 사용만)** 이다. 이 저장소를 **상업적으로**
> 쓰려면 이 파일과 그 파생물(사이트의 차트·점수 포함)은 그대로 쓸 수 없다.

---

## 출처표시 (Attribution)

이 프로젝트는 다음 공개 데이터에 기대고 있다. 감사드린다.

| 데이터 | 제공처 | 라이선스 | 상업적 이용 |
|---|---|---|---|
| 비트코인 가격·실현시총·해시레이트·주소수·거래소 흐름 (`data/market.csv`) | **[Coin Metrics](https://coinmetrics.io)** 커뮤니티 티어 | **[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)** | ❌ **불가** |
| 나스닥·M2·DXY·실질금리·연준 유동성·국채·원자재 등 거시 전반 (`data/macro.csv`) | **[FRED](https://fred.stlouisfed.org)** (세인트루이스 연준) | 미국 정부 저작물 — 사실상 퍼블릭 도메인 | ✅ 가능 |
| S&P500 · VIX · 금(LBMA) | GitHub 공개 데이터셋(datahub 계열) | 데이터셋별 상이(대개 ODC-PDDL/CC0) | ⚠️ 개별 확인 |
| 중국 M2 (`m2_cn`) | **[China Data Portal](https://chinadata.live)** (PBoC 미러) | 명시 없음 | ⚠️ 확인 필요 |
| 펀딩비(검정 전용, `--extended`) | **[supervik/historical-funding-rates-fetcher](https://github.com/supervik/historical-funding-rates-fetcher)** | **MIT** | ✅ 가능 |
| 온체인 확장 지표(검정 전용, `--extended`) | **[coinmetrics/data](https://github.com/coinmetrics/data)** | **CC BY-NC 4.0** | ❌ 불가 |

## 무엇이 어디에 쓰이나

- **점수(BCS)에 실제로 들어가는 것**은 `data/market.csv`(Coin Metrics) 와
  중국 M2(LRS 크기 조절)뿐이다. → **NC 조건이 프로젝트의 핵심에 걸린다.**
- `data/macro.csv` 의 나머지 열(dxy·realyield·net_liq·kospi·hy_spread 등)은
  **연관성 검정에서 전부 기각**돼 점수·사이트에 들어가지 않는다(docs/25~29).
  전부 FRED 라 퍼블릭 도메인이다.
- `--extended` 로만 받는 데이터(펀딩비·NVT·수수료·주소수)는 저장소에 **커밋되지
  않는다.** 필요할 때 받아서 검정하고 버린다.

## 상업적으로 쓰려면

1. **Coin Metrics 상업 라이선스**를 확인한다(커뮤니티 티어는 NC).
2. 또는 `market.csv` 의 `price`·`realized_cap` 을 **상업 이용이 허용된 다른 소스**로
   교체한다. 대안 목록은 [docs/18 무료 데이터 경로](docs/18-무료-데이터-경로.md) 참고.
   나머지 열(supply·issuance 등)은 반감기 스케줄로 **계산**되므로 라이선스 문제가 없다.
3. 중국 M2 출처(chinadata) 조건도 함께 확인한다.

## 면책

이 문서는 다운스트림 사용자를 위한 **선의의 요약**이며 법률 자문이 아니다. 각 제공처의
약관이 갱신될 수 있으므로, 상업적 이용을 계획한다면 **원문을 직접 확인**해야 한다.
