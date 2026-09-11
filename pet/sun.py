"""实时太阳位置 → 立绘地面阴影（v0.17 光影通道）。

用天文学近似公式（黄赤交角 → 太阳赤纬 / 均时差 / 时角）从「纬度 + 经度 +
时区 + 当前时刻」算出**太阳高度角**与**方位角**，再映射为桌面宠物脚下的
地面阴影（水平面 = 屏幕平面）。数据源可插拔：

* ``RealtimeSunSource``：纯本地计算（无网络），按系统时钟实时推进。
* ``StaticSunSource``：未启用 / 未配位置时的兜底（固定柔和阴影）。

设计铁律（对齐 wind 通道）：
* 太阳是**慢变量**——位置 ~15°/小时，每 tick 重算也是几毫秒级的纯三角，
  阴影参数平滑连续、不抖动。
* 展示层永不阻断启动：任何异常都退 StaticSunSource，不抛。
* 纯计算零网络，无「网络间隔 >5s」红线顾虑。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger("pet")

# 黄赤交角（地球自转轴倾角，度）——用户点名的参数之一，显式具名便于调参
OBLIQUITY_DEG = 23.44


@dataclass(frozen=True)
class SunPosition:
    """太阳位置（地平坐标系）。"""
    elevation_deg: float   # 太阳高度角（地平线上为正、下为负）
    azimuth_deg: float     # 太阳方位角（0=北，90=东，180=南，270=西，顺时针）


@dataclass(frozen=True)
class Shadow:
    """地面阴影参数（归一化到窗口尺寸，QML 直接套用）。"""
    alpha: float       # 阴影不透明度 0..1（夜晚→0）
    offset_x: float    # 归一化水平偏移（-0.5..0.5，×窗宽；正=屏幕右）
    scale_x: float     # 水平拉伸（阴影宽 = scale_x × 窗宽）
    scale_y: float     # 垂直厚度（阴影高 = scale_y × 窗高）


def solar_declination(day_of_year: int) -> float:
    """太阳赤纬 δ（度）：黄赤交角 × 周年相位。

    夏至（N≈172）≈ +23.44°，冬至（N≈355）≈ -23.44°，春/秋分 ≈ 0°。
    """
    return OBLIQUITY_DEG * math.sin(2.0 * math.pi * (284 + day_of_year) / 365.0)


def equation_of_time_minutes(day_of_year: int) -> float:
    """均时差（分钟）：真太阳时 − 平太阳时。

    来源：地球椭圆轨道（开普勒）+ 黄赤交角（两分点）。范围约 ±16 分钟。
    """
    b = 2.0 * math.pi * (day_of_year - 81) / 364.0
    return 9.87 * math.sin(2.0 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)


def solar_position(lat_deg: float, lon_deg: float, when: datetime,
                   tz_offset_hours: float) -> SunPosition:
    """给定本地钟表时刻与本地时区偏移，算太阳高度角 / 方位角。

    ``when`` 为**本地**钟表时间（``datetime.now()`` 的 naive 值即可）；
    ``tz_offset_hours`` 为本地相对 UTC 的偏移（如 UTC+8 → ``8.0``），用于
    「经度 − 时区中央经线」的真太阳时校正。
    """
    n = when.timetuple().tm_yday
    decl = solar_declination(n)
    eot = equation_of_time_minutes(n)

    # 真太阳时（小时）= 钟表时 + 均时差 + 经度校正（每 15° 时区中央经线）
    frac_hours = (when.hour + when.minute / 60.0
                  + when.second / 3600.0 + when.microsecond / 3.6e9)
    solar_hours = (frac_hours + eot / 60.0
                   + 4.0 * (lon_deg - 15.0 * tz_offset_hours) / 60.0)
    hour_angle = 15.0 * (solar_hours - 12.0)   # 度；上午为负、下午为正

    lat = math.radians(lat_deg)
    d = math.radians(decl)
    h = math.radians(hour_angle)

    # 太阳高度角：sin(α) = sinφ·sinδ + cosφ·cosδ·cos(H)
    sin_alt = (math.sin(lat) * math.sin(d)
               + math.cos(lat) * math.cos(d) * math.cos(h))
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.degrees(math.asin(sin_alt))

    # 方位角：cos 公式给出 0..180，下午（时角>0）翻到 180..360
    cos_lat = math.cos(lat)
    if abs(cos_lat) < 1e-9:                     # 极点退化：方位角无定义
        az = 180.0
    else:
        cos_alt = math.cos(math.radians(alt))
        if abs(cos_alt) < 1e-9:                 # 天顶/天底退化
            az = 180.0 if lat_deg >= 0 else 0.0
        else:
            cos_az = ((math.sin(d) - math.sin(math.radians(alt)) * math.sin(lat))
                      / (cos_alt * cos_lat))
            cos_az = max(-1.0, min(1.0, cos_az))
            az = math.degrees(math.acos(cos_az))
            if math.sin(h) > 0.0:
                az = 360.0 - az
    return SunPosition(elevation_deg=alt, azimuth_deg=az)


def sun_to_shadow(pos: SunPosition, max_alpha: float = 0.4) -> Shadow:
    """太阳高度角 / 方位角 → 地面阴影（2D 正面视角）。

    * 高度角越高影子越短（正午近圆形贴脚）、越低越长；地平线下（夜晚）无影。
    * 方位角决定东西向偏移：太阳在东 → 影子偏西（屏幕左侧，offset 为负）。
    """
    alt = pos.elevation_deg
    if alt <= 0.0:
        return Shadow(alpha=0.0, offset_x=0.0, scale_x=0.45, scale_y=0.07)

    intensity = min(1.0, max(0.0, math.sin(math.radians(alt))))
    # 影长 = 身高 / tan(高度角)；钳下限防正午除零、归一化到 [0,1]（α≈14° 饱和）
    length = 1.0 / max(math.tan(math.radians(alt)), 0.05)
    length_norm = min(1.0, length / 4.0)

    alpha = float(max_alpha) * intensity
    scale_x = 0.45 + 0.30 * length_norm            # 0.45（正午）→ 0.75（低太阳）
    scale_y = 0.07 * (1.0 - 0.25 * length_norm)
    offset_x = -math.sin(math.radians(pos.azimuth_deg)) * 0.22 * length_norm
    return Shadow(alpha=alpha, offset_x=offset_x, scale_x=scale_x, scale_y=scale_y)


def system_tz_offset_hours() -> float:
    """系统本地时区偏移（小时，UTC+8 → 8.0）；拿不到退 0。"""
    try:
        off = datetime.now().astimezone().utcoffset()
        return off.total_seconds() / 3600.0 if off is not None else 0.0
    except Exception:                              # pragma: no cover - 极端环境
        return 0.0


class SunSource:
    """阴影数据源抽象：``current() -> Shadow``。"""

    def current(self) -> Shadow:
        raise NotImplementedError

    def refresh(self) -> None:
        """按需重算；静态源为 no-op（主线程周期调用，纯计算极轻）。"""
        return None


class StaticSunSource(SunSource):
    """固定柔和阴影兜底（未启用 / 未配位置）。"""

    def __init__(self, alpha: float = 0.35):
        self._shadow = Shadow(alpha=float(alpha), offset_x=0.0,
                              scale_x=0.6, scale_y=0.07)

    def current(self) -> Shadow:
        return self._shadow


class RealtimeSunSource(SunSource):
    """本地实时太阳位置（纯计算，无网络）。"""

    def __init__(self, lat: float, lon: float,
                 tz_offset_hours: float | None = None,
                 max_alpha: float = 0.4):
        self._lat = float(lat)
        self._lon = float(lon)
        self._tz = (float(tz_offset_hours) if tz_offset_hours is not None
                    else system_tz_offset_hours())
        self._max_alpha = float(max_alpha)
        self._cache = self._compute()

    def _compute(self) -> Shadow:
        pos = solar_position(self._lat, self._lon, datetime.now(), self._tz)
        return sun_to_shadow(pos, self._max_alpha)

    def current(self) -> Shadow:
        return self._cache

    def refresh(self) -> None:
        self._cache = self._compute()


def build_sun_source(cfg: dict) -> SunSource:
    """按 config 装配太阳源；任何缺位都退 StaticSunSource（永不阻断启动）。

    位置解析：先看 ``sun.latitude/longitude``，缺位回退 ``wind.latitude/
    longitude``（与风共用位置）；仍无 → 静态兜底。
    """
    s = (cfg or {}).get("sun") or {}
    if not s.get("enabled"):
        return StaticSunSource(float(s.get("shadow_alpha", 0.35) or 0.35))

    lat = s.get("latitude")
    lon = s.get("longitude")
    if not lat or not lon:
        w = (cfg or {}).get("wind") or {}
        lat, lon = w.get("latitude"), w.get("longitude")
    if not lat or not lon:                        # 0.0 / None 视为未配
        return StaticSunSource(float(s.get("shadow_alpha", 0.35) or 0.35))

    tz = s.get("timezone_offset")
    src = RealtimeSunSource(
        float(lat), float(lon),
        tz_offset_hours=None if tz in (None, "") else float(tz),
        max_alpha=float(s.get("shadow_alpha", 0.4) or 0.4))
    log.info("光影源就绪：lat %.2f / lon %.2f / tz %s",
             float(lat), float(lon),
             "系统" if tz in (None, "") else tz)
    return src
