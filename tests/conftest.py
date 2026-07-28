"""pytest 가 src/ 와 tests/ 를 찾을 수 있게 한다. 설치 없이 바로 실행되도록."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
