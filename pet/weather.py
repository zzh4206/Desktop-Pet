"""天气感知核心（v0.23 天气线）—— wiki 设计-天气模块技术方案.md。

纯共享层（mac/win 同代码，零平台库）：数据获取是 HTTP + 缓存 + 配置，
不碰任何平台 API。平台差异只有一个坑位——未来系统级天气源（如 mac
WeatherKit）经 ``platform.PlatformAdapter.get_weather_source()`` 注入
（本线不实装，基类返 None → 本模块 Open-Meteo 兜底）。

设计铁律（对齐 pet/wind.py v0.16 风通道）：
* **daemon 线程拉取 + 主线程读缓存**：``snapshot()`` 无网络无阻塞，
  主动提醒在 ProactiveScheduler.poll（主线程 30s QTimer）里消费。
* **离线诚实**：抓取失败 → 返回最后快照并置 ``stale=True``；消费方
  （提醒规则）见 stale 即跳过——数据不新鲜不提醒（诚实护栏）。
* **空值兜底**：Open-Meteo 部分模型线缺 precipitation_probability 时
  返回 null → 快照对应字段 None → 不触发带伞提醒，不猜。
* **永不阻断启动**：构建/拉取/解析任何异常都吞掉降级（快照=None）。
* 网络低频：TTL 默认 30min（``weather.refresh_min``），远松于
  「网络请求间隔 >5s」红线；429/5xx 指数退避 1→5→30min，连续 5 次
  失败本日放弃。

数据条款（资料-天气数据源调研.md）：Open-Meteo 免费档 = 非商用 +
CC BY 4.0 署名（README「Powered by Open-Meteo」）；商用分发前须重估
provider（和风天气备选位 ``weather.provider`` 预留，本线不实现）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict

import requests

log = logging.getLogger("pet")

_FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
_GEOCODE_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
# v0.23.7 IP 定位（opt-in 后单次调用）：免费档仅 HTTP（HTTPS 需付费 Pro），
# 返回城市级坐标。fields 限取所需字段，公网 IP 明文传输——诚实护栏见调研报告。
_IP_LOCATE_ENDPOINT = "http://ip-api.com/json/"
_IP_LOCATE_FIELDS = "status,message,country,city,lat,lon"
# v0.23.8 国内可达性兜底：Open-Meteo 偶发超时（实测 00:13 拉 10s 超时、
# 00:16 重启秒通）→ 短超时快速失败 + 立即重试 1 次 + wttr.in 备用源兜底
# （免 key；字段降级：无昨日温差 → colder 提醒自动跳过，诚实护栏不猜）。
_WTTR_ENDPOINT = "https://wttr.in/{city}"
_TIMEOUT_S = 6.0

# 429/5xx 指数退避阶梯（秒）；连续 _MAX_FAILURES 次后本日放弃
_BACKOFF_S = (60.0, 300.0, 1800.0)
_MAX_FAILURES = 5

# WMO weather code → condition 枚举（表驱动，单测锁定）
# 0 晴 / 1-3 云 / 45-48 雾 / 51-67 雨(毛毛-冻雨) / 71-77 雪 /
# 80-82 阵雨 / 85-86 阵雪 / 95-99 雷暴
_WMO_CONDITIONS = (
    (frozenset(range(0, 1)), "CLEAR"),
    (frozenset(range(1, 4)), "CLOUDY"),
    (frozenset(range(45, 49)), "FOG"),
    (frozenset(range(51, 68)) | frozenset(range(80, 83)), "RAIN"),
    (frozenset(range(71, 78)) | frozenset(range(85, 87)), "SNOW"),
    (frozenset(range(95, 100)), "THUNDER"),
)


def wmo_to_condition(code) -> str:
    """WMO weather code → condition 枚举；None/未知 → UNKNOWN（不猜）。"""
    if not isinstance(code, (int, float)):
        return "UNKNOWN"
    for codes, cond in _WMO_CONDITIONS:
        if int(code) in codes:
            return cond
    return "UNKNOWN"


# WWO（WorldWeatherOnline）weather code → condition 枚举，wttr.in 备用源用。
# wttr 的 weatherCode 是 WWO 体系（与 Open-Meteo 的 WMO 不同），单独表驱动。
_WWO_CONDITIONS = (
    (frozenset({113}), "CLEAR"),
    (frozenset({116, 119, 122}), "CLOUDY"),
    (frozenset({143, 248, 260}), "FOG"),
    (frozenset({176, 182, 185, 263, 266, 281, 284, 293, 296, 299,
                302, 305, 308, 311, 314, 317, 320, 350, 353, 356,
                359}), "RAIN"),
    (frozenset({179, 227, 230, 323, 326, 329, 332, 335, 338, 362,
                365, 368, 371, 374, 377}), "SNOW"),
    (frozenset({200, 386, 389, 392, 395}), "THUNDER"),
)


def _wttr_code_to_condition(code) -> str:
    """WWO weather code → condition 枚举；坏值/未知 → UNKNOWN（不猜）。

    wttr.in j1 格式所有字段都是字符串（实测 ``"weatherCode": "116"``），
    故接受数字字符串；解析失败一律 UNKNOWN。
    """
    try:
        c = int(str(code).strip())
    except (TypeError, ValueError):
        return "UNKNOWN"
    for codes, cond in _WWO_CONDITIONS:
        if c in codes:
            return cond
    return "UNKNOWN"


@dataclass
class WeatherSnapshot:
    """天气快照（契约见技术方案 §2）。

    ``None` 语义（诚实护栏）：字段缺失=数据未知，消费方据此跳过对应
    提醒，绝不默认 0/晴——降水概率缺 → 不发带伞；温差缺 → 不发降温。
    """

    city: str
    temp: float | None = None
    feels_like: float | None = None
    condition: str = "UNKNOWN"
    precip_prob_max: int | None = None      # 今日最大降水概率 0-100
    temp_max_today: float | None = None
    temp_min_today: float | None = None
    temp_yesterday_max: float | None = None  # past_days=1，降温对比用
    fetched_at: float = 0.0                 # epoch 秒
    stale: bool = False                     # 离线用最后快照时 True
    source: str = "open-meteo"              # open-meteo | wttr（备用源降级标注）

    def age_min(self, now: float) -> float:
        return max(0.0, (now - self.fetched_at) / 60.0)

    def check_alerts(self, alerts: dict) -> list[str]:
        """按阈值评估提醒规则（纯函数）→ 触发的 alert id 列表。

        id：umbrella（带伞）/ colder（降温）/ hot（高温）/ cold（寒潮）。
        stale 快照或对应字段 None → 一律不触发（数据不新鲜不提醒）。
        """
        if self.stale:
            return []
        fired: list[str] = []
        if self.precip_prob_max is not None and self.condition != "CLEAR":
            if self.precip_prob_max >= float(alerts.get("precip_prob", 50)):
                fired.append("umbrella")
        if (self.temp_max_today is not None
                and self.temp_yesterday_max is not None
                and self.temp_max_today - self.temp_yesterday_max
                < -float(alerts.get("drop_c", 5))):
            fired.append("colder")
        if (self.temp_max_today is not None
                and self.temp_max_today >= float(alerts.get("hot_c", 35))):
            fired.append("hot")
        if (self.temp_min_today is not None
                and self.temp_min_today <= float(alerts.get("cold_c", 0))):
            fired.append("cold")
        return fired


def _num(v):
    """None/空安全转 float；缺失返回 None（不猜 0）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class WeatherService:
    """Open-Meteo 天气服务：daemon 拉取 + 内存/磁盘双缓存 + 离线兜底。

    主线程消费面只有两个：``snapshot()``（读缓存，无网络）与
    ``refresh_async()``（到期才派 daemon 线程，未到期/在途立即返回）。
    """

    def __init__(self, cfg: dict, cache_path: str | None = None,
                 now_fn=time.time, fetch_json=None) -> None:
        c = cfg or {}
        self.city = str(c.get("city") or "").strip()
        self.refresh_min = max(5.0, float(c.get("refresh_min", 30) or 30))
        self.alerts = dict(c.get("alerts") or {})
        # v0.23.7 定位：city 优先；city 空且 locate=="ip" 且 consent 才 IP 定位
        self.locate = str(c.get("locate") or "none")
        self.locate_consent = bool(c.get("locate_consent"))
        self._ip_city = ""                      # IP 定位得到的城市名（展示用）
        self._cache_path = cache_path
        self._now = now_fn
        # fetch_json(url, params) -> dict；默认 requests.get（单测注入 mock）
        self._fetch_json = fetch_json or self._http_get_json
        self._lock = threading.Lock()
        self._snapshot: WeatherSnapshot | None = None
        self._coords: tuple | None = None      # (lat, lon)（geocode 缓存）
        self._geocode_city: str = ""           # 坐标对应的城市名（变更才重查）
        self._fetching = False
        self._failures = 0
        self._fail_day: str = ""               # 本日放弃标记（本地日期 iso）
        self._next_try_at = 0.0                # 退避解禁时间戳
        self._load_cache()

    # ---- 对外消费面（主线程） ----

    def snapshot(self) -> WeatherSnapshot | None:
        with self._lock:
            return self._snapshot

    def refresh_async(self, force: bool = False) -> None:
        """到期才派 daemon 线程拉新；未到期 / 在途 / 本日已放弃则返回。"""
        snap = self.snapshot()
        now = self._now()
        if self._given_up_today():
            return
        if (not force and snap is not None and not snap.stale
                and snap.age_min(now) < self.refresh_min):
            return
        with self._lock:
            if self._fetching or now < self._next_try_at:
                return
            self._fetching = True
        threading.Thread(target=self._do_refresh, daemon=True).start()

    # ---- 拉取（daemon 线程） ----

    def _do_refresh(self) -> None:
        try:
            snap = self._fetch_snapshot()
        except Exception as e:                     # 网络/解析任何异常都兜底
            log.warning("[天气] 抓取失败：%s", e)
            snap = self._stale_fallback()
            self._record_failure()
        else:
            self._record_success()
        with self._lock:
            # 成功必覆盖；失败只在尚无新快照时回填 stale（不倒退覆盖新数据）
            if snap is not None and (
                self._snapshot is None or snap.fetched_at
                >= self._snapshot.fetched_at or snap.stale
            ):
                self._snapshot = snap
                self._save_cache(snap)
            self._fetching = False

    def _fetch_snapshot(self) -> WeatherSnapshot:
        """主备双源抓取：Open-Meteo（偶发超时）→ 立即重试 1 次 → wttr 兜底。

        v0.23.8：实测 Open-Meteo 国内偶发超时（10s 拉空、重启秒通），故
        首败立即同步重试（偶发抖动第二次常即成功）；仍败切 wttr.in（免 key、
        字段降级：无昨日温差 → colder 提醒自动跳过）。全败才抛出走既有
        退避/放弃链路。
        """
        try:
            return self._fetch_open_meteo()
        except Exception as e1:
            log.warning("[天气] Open-Meteo 抓取失败（%s），立即重试", e1)
            try:
                return self._fetch_open_meteo()
            except Exception as e2:
                log.warning("[天气] Open-Meteo 重试仍失败（%s），切 wttr 备用源", e2)
                return self._fetch_wttr()

    def _fetch_open_meteo(self) -> WeatherSnapshot:
        coords = self._ensure_coords()
        now = self._now()
        params = {
            "latitude": coords[0], "longitude": coords[1],
            "current": ("temperature_2m,apparent_temperature,"
                        "precipitation_probability,weather_code"),
            "daily": ("temperature_2m_max,temperature_2m_min,"
                      "precipitation_probability_max"),
            "past_days": 1, "forecast_days": 1,
            "timezone": "auto",
        }
        data = self._fetch_json(_FORECAST_ENDPOINT, params)
        cur = data.get("current") or {}
        day = data.get("daily") or {}
        dmax = day.get("temperature_2m_max") or []
        dmin = day.get("temperature_2m_min") or []
        pmax = day.get("precipitation_probability_max") or []
        # past_days=1 → daily 数组 [昨日, 今日]；缺失日长为 1 时昨日=None
        yesterday = _num(dmax[0]) if len(dmax) > 1 else None
        today_max = _num(dmax[-1]) if dmax else None
        today_min = _num(dmin[-1]) if dmin else None
        snap = WeatherSnapshot(
            city=self.city or self._ip_city,
            temp=_num(cur.get("temperature_2m")),
            feels_like=_num(cur.get("apparent_temperature")),
            condition=wmo_to_condition(cur.get("weather_code")),
            precip_prob_max=_num(pmax[-1]) if pmax else None,
            temp_max_today=today_max,
            temp_min_today=today_min,
            temp_yesterday_max=yesterday,
            fetched_at=now,
        )
        if snap.precip_prob_max is not None:
            snap.precip_prob_max = int(snap.precip_prob_max)
        log.info("[天气] 刷新成功 %s：%.0f°C %s 降水概率 %s",
                 snap.city, snap.temp or 0, snap.condition,
                 snap.precip_prob_max)
        return snap

    def _fetch_wttr(self) -> WeatherSnapshot:
        """wttr.in 备用源（免 key）：实况 + 今日高低温 + hourly 降水概率。

        字段降级（诚实护栏）：无 past_days → temp_yesterday_max=None，
        colder（降温）提醒自动跳过；city 空时 wttr 按请求方 IP 自动定位。
        """
        from urllib.parse import quote

        city = (self.city or self._ip_city or "").strip()
        url = (_WTTR_ENDPOINT.format(city=quote(city)) if city
               else _WTTR_ENDPOINT.format(city=""))
        data = self._fetch_json(url, {"format": "j1"})
        cur = (data.get("current_condition") or [{}])[0]
        days = data.get("weather") or []
        today = days[0] if days else {}
        # 有效性校验：实况温度和今日高低温全缺 = 响应结构非预期（如被
        # 中间层回了错误页 JSON）→ 视为失败抛出，走退避链路，不缓存
        # 全 None 快照占 TTL（诚实护栏：垃圾数据不进缓存）。
        if _num(cur.get("temp_C")) is None and _num(today.get("maxtempC")) is None:
            raise ValueError("wttr 响应无有效字段")
        hourly = today.get("hourly") or []
        # 降水概率 = 今日 hourly chanceofrain 最大值（字符串字段）
        chances = []
        for h in hourly:
            try:
                chances.append(int(h.get("chanceofrain")))
            except (TypeError, ValueError):
                pass                      # 缺失/坏值 → 跳过（不猜）
        snap = WeatherSnapshot(
            city=city or str(cur.get("query") or "IP 定位"),
            temp=_num(cur.get("temp_C")),
            feels_like=_num(cur.get("FeelsLikeC")),
            condition=_wttr_code_to_condition(cur.get("weatherCode")),
            precip_prob_max=max(chances) if chances else None,
            temp_max_today=_num(today.get("maxtempC")),
            temp_min_today=_num(today.get("mintempC")),
            temp_yesterday_max=None,      # wttr 无 past → 降温提醒跳过
            fetched_at=self._now(),
            source="wttr",
        )
        log.info("[天气] wttr 备用源成功 %s：%.0f°C %s 降水概率 %s",
                 snap.city, snap.temp or 0, snap.condition,
                 snap.precip_prob_max)
        return snap

    def _ensure_coords(self) -> tuple:
        """城市名 → 坐标（geocoding）；city 空且同意 IP 定位 → IP→坐标。

        结果随缓存落盘；城市/定位源未变不重查。
        """
        use_ip = not self.city and self.locate == "ip" and self.locate_consent
        if not use_ip:
            with self._lock:
                if self._coords and self._geocode_city == self.city:
                    return self._coords
            return self._geocode_by_city()
        with self._lock:
            if self._coords and self._geocode_city == "":
                return self._coords          # IP 定位已缓存
        return self._locate_by_ip()

    def _geocode_by_city(self) -> tuple:
        data = self._fetch_json(_GEOCODE_ENDPOINT, {
            "name": self.city, "count": 1, "language": "zh", "format": "json",
        })
        results = data.get("results") or []
        if not results:
            raise ValueError(f"城市 {self.city!r} 无 geocoding 结果")
        r = results[0]
        coords = (float(r["latitude"]), float(r["longitude"]))
        with self._lock:
            self._coords = coords
            self._geocode_city = self.city
            self._save_cache(self._snapshot)
        return coords

    def _locate_by_ip(self) -> tuple:
        """IP → 坐标（ip-api.com，opt-in 后调用）。失败抛出走既有兜底。"""
        data = self._fetch_json(_IP_LOCATE_ENDPOINT,
                                {"fields": _IP_LOCATE_FIELDS})
        if data.get("status") != "success":
            raise ValueError(f"IP 定位失败: {data.get('message', '未知')}")
        lat, lon = float(data["lat"]), float(data["lon"])
        city = str(data.get("city") or "")
        with self._lock:
            self._coords = (lat, lon)
            self._geocode_city = ""          # 空 = IP 定位（区别于手动城市）
            self._ip_city = city
            self._save_cache(self._snapshot)
        log.info("[天气] IP 定位成功：%s (%.3f, %.3f)", city, lat, lon)
        return (lat, lon)

    def _stale_fallback(self) -> WeatherSnapshot | None:
        with self._lock:
            snap = self._snapshot
        if snap is None:
            return None
        stale = WeatherSnapshot(**{**asdict(snap), "stale": True})
        return stale

    # ---- 退避（daemon 线程内调用） ----

    def _record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._fail_day = ""
            self._next_try_at = 0.0

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            i = min(self._failures, len(_BACKOFF_S)) - 1
            self._next_try_at = self._now() + _BACKOFF_S[max(0, i)]
            if self._failures >= _MAX_FAILURES:
                self._fail_day = time.strftime("%Y-%m-%d",
                                               time.localtime(self._now()))
                log.warning("[天气] 连续 %d 次失败，本日放弃刷新",
                            self._failures)

    def _given_up_today(self) -> bool:
        return bool(self._fail_day) and self._fail_day == time.strftime(
            "%Y-%m-%d", time.localtime(self._now()))

    # ---- 磁盘缓存（tmp+replace 原子写，对齐 proactive_state 模式） ----

    def _load_cache(self) -> None:
        if not self._cache_path or not os.path.isfile(self._cache_path):
            return
        try:
            with open(self._cache_path, encoding="utf-8") as f:
                raw = json.load(f)
            geocode = raw.get("geocode") or {}
            if (isinstance(geocode.get("lat"), (int, float))
                    and isinstance(geocode.get("lon"), (int, float))):
                self._coords = (float(geocode["lat"]), float(geocode["lon"]))
                if geocode.get("ip"):
                    self._geocode_city = ""            # IP 定位模式哨兵
                    self._ip_city = str(geocode.get("city") or "")
                else:
                    self._geocode_city = str(geocode.get("city") or "")
            s = raw.get("snapshot")
            if isinstance(s, dict) and s.get("fetched_at"):
                # 磁盘回读的快照一律标 stale（重启后未验证新鲜度，须先刷新）
                s["stale"] = True
                if s.get("city") == (self.city or self._ip_city):
                    self._snapshot = WeatherSnapshot(**{
                        k: v for k, v in s.items()
                        if k in WeatherSnapshot.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            log.warning("[天气] 缓存档案损坏，忽略", exc_info=True)

    def _save_cache(self, snap: WeatherSnapshot | None) -> None:
        if not self._cache_path:
            return
        try:
            data: dict = {"version": 1}
            if self._coords:
                data["geocode"] = {
                    "city": self.city or self._ip_city,
                    "ip": not bool(self.city),
                    "lat": self._coords[0], "lon": self._coords[1],
                }
            if snap is not None:
                data["snapshot"] = asdict(snap)
            tmp = self._cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._cache_path)
        except (OSError, TypeError, ValueError):
            log.warning("[天气] 缓存写入失败", exc_info=True)

    @staticmethod
    def _http_get_json(url: str, params: dict) -> dict:
        resp = requests.get(url, params=params, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json() or {}


def weather_location_enabled(cfg: dict) -> bool:
    """定位同意是否开启：locate=="ip" 且 locate_consent。供权限页状态显示。"""
    w = (cfg or {}).get("weather") or {}
    return w.get("locate") == "ip" and bool(w.get("locate_consent"))


def build_weather_service(cfg: dict, cache_path: str | None = None,
                          ) -> WeatherService | None:
    """按 config 装配天气服务；未启用返 None（功能静默，拍板项②）。

    city 手动填了 → 用手动城市；city 空但 IP 定位已同意 → 用 IP 定位；
    两者皆无 → None（静默）。
    """
    w = (cfg or {}).get("weather") or {}
    if not w.get("enabled"):
        return None
    if (not str(w.get("city") or "").strip()
            and not weather_location_enabled(cfg)):
        return None
    return WeatherService(w, cache_path=cache_path)


# ---- LLM 工具（v0.23.3：get_weather，无危险级不打扰） ----

_CONDITION_ZH = {
    "CLEAR": "晴", "CLOUDY": "多云", "FOG": "雾", "RAIN": "雨",
    "SNOW": "雪", "THUNDER": "雷暴", "UNKNOWN": "未知",
}


def _snapshot_to_json(snap: WeatherSnapshot) -> dict:
    return {
        "city": snap.city, "temp": snap.temp,
        "feels_like": snap.feels_like, "condition": snap.condition,
        "precip_prob_max": snap.precip_prob_max,
        "temp_max_today": snap.temp_max_today,
        "temp_min_today": snap.temp_min_today,
        "fetched_at": snap.fetched_at, "stale": snap.stale,
        "source": snap.source,
    }


def _snapshot_summary(snap: WeatherSnapshot) -> str:
    """快照 → 给 LLM 的可读摘要（**message 通道**——聊天链路只回灌
    message，data 字段不透传给模型，v0.23.8 实测踩坑：message="ok" 致
    模型两次拿到 "ok" 无从作答）。None 字段如实标「未知」，不猜。
    """

    def _t(v):
        return f"{v:g}°C" if v is not None else "未知"

    parts = [
        f"{snap.city or '未知城市'}：当前 {_t(snap.temp)}"
        f"（体感 {_t(snap.feels_like)}），"
        f"{_CONDITION_ZH.get(snap.condition, snap.condition)}",
    ]
    if snap.precip_prob_max is not None:
        parts.append(f"今日降水概率 {snap.precip_prob_max}%")
    if snap.temp_max_today is not None or snap.temp_min_today is not None:
        parts.append(f"今日 {_t(snap.temp_max_today)} ~ {_t(snap.temp_min_today)}")
    if snap.stale:
        parts.append("（注意：数据可能不是最新的）")
    return "，".join(parts)


class _WeatherToolHandler:
    """``get_weather``：只读缓存快照，**不即时拉网**（技术方案 §3.2）。"""

    def __init__(self, service) -> None:
        self._service = service

    def execute(self, args: dict, ctx) -> "ToolResult":
        from pet.tools_schema import ToolResult  # 延迟避环 import

        if self._service is None:
            return ToolResult(
                False, "天气功能未启用（config weather.enabled/city）", {})
        snap = self._service.snapshot()
        if snap is None:
            return ToolResult(False, "暂无天气数据（等待首次刷新）", {})
        # stale 随数据透传：LLM 自行措辞「数据可能不是最新的」（诚实护栏）
        # message 带完整摘要（聊天链路只回灌 message，data 不透传给模型）
        return ToolResult(True, _snapshot_summary(snap),
                          _snapshot_to_json(snap))


def build_weather_tool(service) -> list:
    """天气工具清单（app 喂 ToolRegistry.register，与记忆工具并列）。"""
    from pet.tools_schema import ToolSchema

    schema = ToolSchema(
        name="get_weather",
        description=(
            "查询用户所在城市的当前天气（温度/体感/天气状况/今日降水概率/"
            "今日最高最低温）。读本地缓存，不发网络请求；stale=true 表示"
            "数据可能过期，回答时需向用户说明。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    return [(schema, _WeatherToolHandler(service))]
