"""pytest 가 src/ · tests/ · tools/ 를 찾을 수 있게 한다. 설치 없이 바로 실행되도록.

tools/ 를 여기 넣는 이유는, 예전엔 시험 함수마다 ``sys.path.insert`` 를 되풀이하고
있었고 새 시험을 쓸 때마다 그 줄을 잊었기 때문이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests", ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
