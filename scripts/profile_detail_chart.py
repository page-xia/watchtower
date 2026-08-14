"""一次性性能剖析：定位 detail/chart 慢在哪里。"""
from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.services import DashboardService  # noqa: E402

service = DashboardService(settings)

CODE = sys.argv[1] if len(sys.argv) > 1 else "300476"

# 预热上下文（模拟 dashboard 已加载）
t0 = time.perf_counter()
service._get_context()
print(f"context warm: {time.perf_counter()-t0:.2f}s")

# 第一次 detail/chart（冷）
t0 = time.perf_counter()
service.signal_detail_chart(CODE)
print(f"chart cold: {time.perf_counter()-t0:.2f}s")

# 第二次（热缓存）
t0 = time.perf_counter()
service.signal_detail_chart(CODE)
print(f"chart warm: {time.perf_counter()-t0:.2f}s")

# profile 热路径
profiler = cProfile.Profile()
profiler.enable()
service.signal_detail_chart(CODE)
profiler.disable()
buf = io.StringIO()
pstats.Stats(profiler, stream=buf).sort_stats("cumulative").print_stats(30)
print(buf.getvalue())
