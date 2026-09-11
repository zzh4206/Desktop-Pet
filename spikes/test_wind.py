# -*- coding: utf-8 -*-
"""test_wind.py —— v0.16 风通道单测（纯 Python，无 Qt）。

覆盖：风速/风向 → gain/bias 映射、静态兜底、WeatherWindSource 拉取
（fake requests，不碰真实网络）、失败回退、build_wind_source 装配。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pet.wind as wind_mod
from pet.wind import (build_wind_source, StaticWindSource, WeatherWindSource,
                      wind_direction_to_bias, wind_speed_to_gain)

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


# W1 风速 → 幅度倍率（Beaufort 分档）
check("W1a 无风 0.3m/s → 0.2", wind_speed_to_gain(0.3) == 0.2)
check("W1b 轻风 3m/s → 0.6", wind_speed_to_gain(3.0) == 0.6)
check("W1c 微风 6m/s → 1.0（基准）", wind_speed_to_gain(6.0) == 1.0)
check("W1d 劲风 10m/s → 1.6", wind_speed_to_gain(10.0) == 1.6)
check("W1e 大风 15m/s → 2.2", wind_speed_to_gain(15.0) == 2.2)

# W2 风向 → 顺风偏置（只取水平分量）
check("W2a 西风 270° → 正（推右）", wind_direction_to_bias(270.0, 1.0) > 0)
check("W2b 东风 90° → 负（推左）", wind_direction_to_bias(90.0, 1.0) < 0)
check("W2c 北风 0° → 0", abs(wind_direction_to_bias(0.0, 1.0)) < 1e-9)
check("W2d gain 缩放线性", abs(wind_direction_to_bias(270.0, 2.0)
                               - 2.0 * wind_direction_to_bias(270.0, 1.0)) < 1e-9)

# W3 静态兜底
s = StaticWindSource(gain=0.8)
check("W3a 静态源返回固定 (0.8, 0)", s.current() == (0.8, 0.0))
check("W3b 静态源 refresh 是 no-op", s.refresh() is None)


# W4 WeatherWindSource 拉取（fake requests，不碰真实网络）
class _FakeResp:
    def __init__(self, data=None, exc=None):
        self._data, self._exc = data, exc

    def raise_for_status(self):
        if self._exc is not None:
            raise self._exc

    def json(self):
        return self._data


class _FakeRequests:
    def __init__(self):
        self.get_fn = lambda *a, **k: _FakeResp({})

    def get(self, *a, **k):
        return self.get_fn(*a, **k)


_fake = _FakeRequests()
wind_mod.requests = _fake


def _run_fetch(src):
    src.refresh()
    for _ in range(300):
        if not src._fetching:
            return
        time.sleep(0.01)
    raise AssertionError("拉取线程超时未落地")


_fake.get_fn = lambda *a, **k: _FakeResp(
    {"current": {"wind_speed_10m": 6.0, "wind_direction_10m": 270.0}})
w = WeatherWindSource(39.9, 116.4, poll_seconds=60.0)
_run_fetch(w)
g, b = w.current()
check(f"W4a 真实风速→gain/bias（{g:.2f}, {b:.2f}）",
      g == 1.0 and abs(b - wind_direction_to_bias(270.0, 1.0)) < 1e-9)

_fake.get_fn = lambda *a, **k: (_ for _ in ()).throw(OSError("net down"))
w2 = WeatherWindSource(39.9, 116.4, poll_seconds=60.0,
                       fallback=StaticWindSource(0.8))
_run_fetch(w2)
g2, b2 = w2.current()
check(f"W4b 网络失败回退兜底（{g2:.2f}, {b2:.2f}）", g2 == 0.8 and b2 == 0.0)

_fake.get_fn = lambda *a, **k: _FakeResp(
    {"current": {"wind_speed_10m": 15.0, "wind_direction_10m": 90.0}})
w3 = WeatherWindSource(39.9, 116.4, poll_seconds=60.0)
_run_fetch(w3)
first = w3.current()
w3.refresh()                                   # 未到期 → 不覆盖缓存
check("W4c 未到期 refresh 不覆盖缓存", w3.current() == first)

# W5 build_wind_source 装配
check("W5a 禁用 → 静态源",
      isinstance(build_wind_source({"wind": {"enabled": False}}),
                 StaticWindSource))
check("W5b enabled 但 0/0 → 静态源",
      isinstance(build_wind_source({"wind": {"enabled": True, "latitude": 0.0,
                                             "longitude": 0.0}}),
                 StaticWindSource))
check("W5c enabled+经纬度 → 天气源",
      isinstance(build_wind_source({"wind": {"enabled": True, "latitude": 39.9,
                                             "longitude": 116.4}}),
                 WeatherWindSource))
check("W5d 无 wind 段 → 静态源",
      isinstance(build_wind_source({}), StaticWindSource))

print(f"\nwind 风通道: {len(PASS)} 通过, {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
