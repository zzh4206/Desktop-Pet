"""实时风力 → 立绘摆动幅度（v0.16 风通道）。

把"当前风力"翻译成引擎的 ``wind_gain``（sway 幅度倍率）+ ``wind_bias_deg``
（顺风偏置，世界空间，度）。数据源可插拔：

* ``WeatherWindSource``：Open-Meteo 免 key 实时风（10m 风速/风向），
  daemon 线程拉取 + 本地缓存，轮询周期由 config 控制。
* ``StaticWindSource``：无网络 / 未配位置 / 抓取失败时的兜底（固定微风）。

设计铁律（对齐 docs/立绘动效引擎设计.md）：
* 风是**慢变量**——只更新"幅度"，摆动本身仍 33ms 连续（引擎正弦/弹簧照跑）。
* 展示层永不阻断启动：构建/拉取任何异常都退 StaticWindSource，不抛。
* 网络请求低频（默认 20min、下限 5min），守住"网络间隔>5s"红线。
"""

from __future__ import annotations

import logging
import math
import threading
import time

import requests

log = logging.getLogger("pet")

_WIND_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
_WIND_TIMEOUT_S = 8.0


def wind_speed_to_gain(mps: float) -> float:
    """风速(m/s) → sway 幅度倍率（Beaufort 近似，1.0=清单基准）。"""
    if mps < 1.0:
        return 0.2
    if mps < 5.0:
        return 0.6
    if mps < 8.0:
        return 1.0
    if mps < 13.0:
        return 1.6
    return 2.2


def wind_direction_to_bias(deg: float, gain: float) -> float:
    """风向（气象角：0=北风=风从北来）→ 顺风偏置（世界空间，度）。

    只取水平分量：西风(270°)推右(+)、东风(90°)推左(-)；北/南风无水平分量。
    幅度 = 3°·gain（gain=1 时 ±3°，大风约 ±6.6°）。
    """
    horizontal = -math.sin(math.radians(deg))
    return 3.0 * float(gain) * horizontal


class WindSource:
    """风力数据源抽象：``current() -> (gain, bias_deg)``。"""

    def current(self) -> tuple[float, float]:
        raise NotImplementedError

    def refresh(self) -> None:
        """按需拉新；无网络源为 no-op（主线程周期调用，实现内部节流）。"""
        return None


class StaticWindSource(WindSource):
    """固定微风兜底（无网络 / 未配位置 / 抓取失败）。"""

    def __init__(self, gain: float = 1.0):
        self._gain = float(gain)

    def current(self) -> tuple[float, float]:
        return (self._gain, 0.0)


class WeatherWindSource(WindSource):
    """Open-Meteo 实时风：daemon 线程拉取 + 本地缓存，主线程读缓存不卡。"""

    def __init__(self, lat: float, lon: float, poll_seconds: float = 1200.0,
                 fallback: WindSource | None = None,
                 timeout_s: float = _WIND_TIMEOUT_S):
        self._lat = float(lat)
        self._lon = float(lon)
        self._poll_s = max(60.0, float(poll_seconds))
        self._fallback = fallback or StaticWindSource()
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._cache = self._fallback.current()
        # -1e9 哨兵 = 从未拉取（time.monotonic 近 0 起算，0.0 会让首拍误判
        # "未到期"而永不拉取——首拍必须立即触发）
        self._last_fetch = -1e9
        self._fetching = False

    def current(self) -> tuple[float, float]:
        with self._lock:
            return self._cache

    def refresh(self) -> None:
        """到期才拉新（网络在 daemon 线程跑）；未到期 / 在途则立即返回。"""
        now = time.monotonic()
        with self._lock:
            if self._fetching or (now - self._last_fetch) < self._poll_s:
                return
            self._fetching = True
        threading.Thread(target=self._do_fetch, daemon=True).start()

    def _do_fetch(self) -> None:
        result = None
        try:
            resp = requests.get(_WIND_ENDPOINT, params={
                "latitude": self._lat, "longitude": self._lon,
                "current": "wind_speed_10m,wind_direction_10m",
                "wind_speed_unit": "ms",
            }, timeout=self._timeout_s)
            resp.raise_for_status()
            cur = (resp.json() or {}).get("current") or {}
            mps = float(cur.get("wind_speed_10m") or 0.0)
            deg = float(cur.get("wind_direction_10m") or 0.0)
            gain = wind_speed_to_gain(mps)
            result = (gain, wind_direction_to_bias(deg, gain))
            log.info("风力更新：%.1f m/s → gain %.2f / bias %.1f°",
                     mps, gain, result[1])
        except Exception as e:                       # 网络/解析任何异常都兜底
            result = self._fallback.current()
            log.warning("风力抓取失败，回退静态值：%s", e)
        with self._lock:
            self._cache = result
            self._last_fetch = time.monotonic()
            self._fetching = False


def build_wind_source(cfg: dict) -> WindSource:
    """按 config 装配风源；任何缺位都退 StaticWindSource（永不阻断启动）。"""
    w = (cfg or {}).get("wind") or {}
    fb = StaticWindSource(float(w.get("fallback_gain", 1.0) or 1.0))
    if not w.get("enabled"):
        return fb
    lat = w.get("latitude")
    lon = w.get("longitude")
    if not lat or not lon:                        # 0.0 / None 视为未配
        return fb
    return WeatherWindSource(float(lat), float(lon),
                             poll_seconds=float(w.get("poll_minutes", 20) or 20)
                                          * 60.0,
                             fallback=fb)
