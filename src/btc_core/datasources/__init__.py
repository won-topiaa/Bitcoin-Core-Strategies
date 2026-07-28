"""데이터 소스 어댑터.

무료로 얻을 수 있는 것과 아닌 것의 경계가 이 패키지의 설계를 결정한다.

- 자동(auto): 가격, 시가총액, 실현시총, 발행량, 해시레이트
  → CoinMetrics 커뮤니티 API 하나로 전부 커버된다.
- 수동(manual): RHODL, Reserve Risk, LTH 실현가격, CVDD, 거시 지표
  → 무료 API가 없다. BM Pro 차트를 눈으로 읽어 YAML에 적는다.

수동 입력을 부끄러워할 필요는 없다. 이 지표들은 월 1~2회 확인하면 충분한
느린 지표다. 매일 자동화할 대상이 아니다.
"""

from .base import DataBundle, FetchError
from .csv_source import load_csv_bundle
from .manual import ManualInput, load_manual

__all__ = ["DataBundle", "FetchError", "load_csv_bundle", "ManualInput", "load_manual"]
