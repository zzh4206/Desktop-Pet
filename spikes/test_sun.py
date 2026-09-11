# -*- coding: utf-8 -*-
"""test_sun.py —— v0.17 光影通道单测（纯 Python，无 Qt）。

覆盖：黄赤交角→太阳赤纬、均时差、太阳高度角/方位角（时区+经度校正）、
高度角/方位角→阴影映射、静态兜底、build_sun_source 装配。
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.sun import (OBLIQUITY_DEG, StaticSunSource, RealtimeSunSource,
                     SunPosition, build_sun_source, equation_of_time_minutes,
                     solar_declination, solar_position, sun_to_shadow)

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


# S1 黄赤交角 → 太阳赤纬
check(f"S1a 黄赤交角常数 ≈ 23.44°", abs(OBLIQUITY_DEG - 23.44) < 1e-9)
check(f"S1b 夏至赤纬（{solar_declination(172):+.2f}° ≈ +23.44）",
      abs(solar_declination(172) - 23.44) < 0.5)
check(f"S1c 冬至赤纬（{solar_declination(355):+.2f}° ≈ -23.44）",
      abs(solar_declination(355) + 23.44) < 0.5)
check(f"S1d 春分赤纬（{solar_declination(80):+.2f}° ≈ 0）",
      abs(solar_declination(80)) < 2.0)

# S2 均时差有界（±16 分钟量级）
_eot = [equation_of_time_minutes(n) for n in range(1, 366)]
check(f"S2 均时差全年 ±17min 内（{min(_eot):.1f}..{max(_eot):.1f}）",
      min(_eot) > -17.0 and max(_eot) < 17.0)

# S3 太阳高度角/方位角（北京 lat39.9 lon116.4 tz+8）
noon_summer = solar_position(39.9, 116.4, datetime(2025, 6, 21, 12, 0, 0), 8.0)
noon_winter = solar_position(39.9, 116.4, datetime(2025, 12, 21, 12, 0, 0), 8.0)
midnight = solar_position(39.9, 116.4, datetime(2025, 6, 21, 0, 0, 0), 8.0)
am = solar_position(39.9, 116.4, datetime(2025, 6, 21, 9, 0, 0), 8.0)
pm = solar_position(39.9, 116.4, datetime(2025, 6, 21, 15, 0, 0), 8.0)
check(f"S3a 夏至正午高度角（{noon_summer.elevation_deg:.1f}° > 60）",
      noon_summer.elevation_deg > 60.0)
check(f"S3b 冬至正午高度角（{noon_winter.elevation_deg:.1f}° ∈ (15,40)）",
      15.0 < noon_winter.elevation_deg < 40.0)
check(f"S3c 夏至午夜在地平线下（{midnight.elevation_deg:.1f}° < 0）",
      midnight.elevation_deg < 0.0)
check(f"S3d 上午太阳在东（az {am.azimuth_deg:.1f}° < 180）",
      am.azimuth_deg < 180.0)
check(f"S3e 下午太阳在西（az {pm.azimuth_deg:.1f}° > 180）",
      pm.azimuth_deg > 180.0)

# S4 高度角/方位角 → 阴影映射
check("S4a 夜晚（alt≤0）无影 alpha=0",
      sun_to_shadow(SunPosition(-5.0, 180.0)).alpha == 0.0)
_over = sun_to_shadow(SunPosition(90.0, 180.0))
check(f"S4b 天顶（alt=90）短圆影（scale_x={_over.scale_x:.3f} ≈ 0.45）",
      abs(_over.scale_x - 0.45) < 1e-9)
check("S4c 正南太阳影子居中（offset≈0）",
      abs(sun_to_shadow(SunPosition(45.0, 180.0)).offset_x) < 1e-9)
check("S4d 太阳在东 → 影子偏西（offset<0）",
      sun_to_shadow(SunPosition(45.0, 90.0)).offset_x < 0.0)
check("S4e 太阳在西 → 影子偏东（offset>0）",
      sun_to_shadow(SunPosition(45.0, 270.0)).offset_x > 0.0)
check("S4f 低太阳影子更长（scale_x 随高度角单调递减）",
      sun_to_shadow(SunPosition(15.0, 180.0)).scale_x
      > sun_to_shadow(SunPosition(45.0, 180.0)).scale_x
      > sun_to_shadow(SunPosition(90.0, 180.0)).scale_x)
_a30 = sun_to_shadow(SunPosition(30.0, 180.0)).alpha
check(f"S4g alpha ∝ sin(高度角)（alt=30 → {_a30:.3f} ≈ 0.2）",
      abs(_a30 - 0.4 * 0.5) < 1e-9)

# S5 时区/经度校正：同时刻更东（更大经度）真太阳时更早 → 上午太阳更高
east = solar_position(39.9, 135.0, datetime(2025, 6, 21, 8, 0, 0), 8.0)
west = solar_position(39.9, 120.0, datetime(2025, 6, 21, 8, 0, 0), 8.0)
check(f"S5 经度校正（东 {east.elevation_deg:.1f}° > 西 {west.elevation_deg:.1f}°）",
      east.elevation_deg > west.elevation_deg)

# S6 静态兜底 + 实时源
s = StaticSunSource(alpha=0.3)
sh = s.current()
check(f"S6a 静态源固定阴影（alpha={sh.alpha}）", sh.alpha == 0.3 and sh.offset_x == 0.0)
check("S6b 静态源 refresh 是 no-op", s.refresh() is None)
rt = RealtimeSunSource(39.9, 116.4, tz_offset_hours=8.0)
sh2 = rt.current()
check(f"S6c 实时源产出合法阴影（alpha={sh2.alpha:.3f} ∈ [0,0.4]）",
      0.0 <= sh2.alpha <= 0.4 + 1e-9 and 0.0 < sh2.scale_x < 1.0)
rt.refresh()
check("S6d 实时源 refresh 重算不抛", isinstance(rt.current().alpha, float))

# S7 build_sun_source 装配
check("S7a 禁用 → 静态源",
      isinstance(build_sun_source({"sun": {"enabled": False}}), StaticSunSource))
check("S7b enabled 但 0/0 → 静态源",
      isinstance(build_sun_source({"sun": {"enabled": True, "latitude": 0.0,
                                             "longitude": 0.0}}),
                 StaticSunSource))
check("S7c enabled+经纬度 → 实时源",
      isinstance(build_sun_source({"sun": {"enabled": True, "latitude": 39.9,
                                             "longitude": 116.4}}),
                 RealtimeSunSource))
check("S7d 无 sun 段 → 静态源", isinstance(build_sun_source({}), StaticSunSource))
check("S7e sun 缺经纬度 → 回退 wind 经纬度",
      isinstance(build_sun_source({"sun": {"enabled": True},
                                    "wind": {"latitude": 39.9,
                                             "longitude": 116.4}}),
                 RealtimeSunSource))

print(f"\nsun 光影通道: {len(PASS)} 通过, {len(FAIL)} 失败")
sys.exit(1 if FAIL else 0)
