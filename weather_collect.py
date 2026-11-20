#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
北京 / 南京 气象数据收集（中国天气网官方 + Open-Meteo 紫外线）
=============================================================
三个数据源：
  1) 中国天气网「历史日历」接口（CMA 官方，日粒度）——回填 9/1 起每日：
     最高/最低温、日最低湿度、降水量（近5天）、天气现象、风力风向描述（当日及以后）
     http://d1.weather.com.cn/calendar_new/{year}/{cityid}_{yyyymm}.html
  2) 中国天气网「实况」接口（CMA 官方，约每 10 分钟更新）——按 02/08/14/20
     四个正点采样：温度、湿度、风向、风速(km/h)、气压、能见度、1h/24h 降水、天气现象
     http://d1.weather.com.cn/sk_2d/{cityid}.html
  3) Open-Meteo（模型/再分析，非官方实测，看板标注来源）：
     - UV 指数（日最大）：https://api.open-meteo.com/v1/forecast?daily=uv_index_max（含过去日期）
     - 小时模型值（温度/湿度/风速/风向/紫外线/天气码）：
       https://api.open-meteo.com/v1/forecast?hourly=...
       —— 用于正点采样的紫外线时次值，及建站首日四正点回填
     - ERA5 再分析归档（补 CMA 日历接口不保留的旧日字段）：
       日降水量 / 天气码 / 日最大风速  https://archive-api.open-meteo.com/v1/era5

注：CMA 日历接口对「已过日期」只保留 hmax/hmin（最高/最低温）、hgl（日最低湿度）、
rainobs（降水量，仅最近约 5 天），天气现象/风况描述仅当日及以后（预报）有。
故旧日的 降水/天气/风况 用 ERA5 再分析补位，并在「数据源」列如实标注。

用法：
  python3 weather_collect.py                  # 全量：日历回填 + UV 回填 + 当前正点采样
  python3 weather_collect.py --sample         # 仅当前正点采样（02/08/14/20 定时任务用）
  python3 weather_collect.py 2026-09-01       # 回填从指定日期起
  python3 weather_collect.py --backfill-points 2026-09-20
                                               # 用小时模型值回填指定日的四正点（建站首日补位）

输出：met_daily.csv（日粒度）、met_points.csv（四正点采样）、raw/ 存档
"""
import csv
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta

# python.org 版 Python 默认不带 CA 证书，显式指定证书库
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "raw")
OUT_DAILY = os.path.join(HERE, "met_daily.csv")
OUT_POINTS = os.path.join(HERE, "met_points.csv")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CITIES = {
    "北京": {"id": "101010100", "lat": 39.9042, "lon": 116.4074},
    "南京": {"id": "101190101", "lat": 32.0603, "lon": 118.7969},
}
SAMPLE_HOURS = [2, 8, 14, 20]          # 四个正点时次（北京时）
START_DATE = date(2026, 9, 1)          # 回填起点


def http_get(url, referer=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": referer or "http://www.weather.com.cn/"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _js_var(text, name):
    m = re.search(r"var\s+" + name + r"\s*=\s*(\{.*?\}|\[.*?\]);", text, re.S)
    return m.group(1) if m else None


def fetch_calendar(city, yyyymm):
    """CMA 历史日历：当月每日 Tmax/Tmin/RHmax/RHmin/降水/天气/风况描述。"""
    cityid = CITIES[city]["id"]
    year, month = yyyymm[:4], yyyymm[4:6]
    url = f"http://d1.weather.com.cn/calendar_new/{year}/{cityid}_{yyyymm}.html"
    text = http_get(url)
    data = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
    out = {}
    for d in data:
        if d.get("date", "").startswith(yyyymm):
            out[d["date"]] = d
    return out


def fetch_sk(city):
    """CMA 实况：当前时点观测值。"""
    url = f"http://d1.weather.com.cn/sk_2d/{CITIES[city]['id']}.html"
    text = http_get(url)
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def fetch_uv(city, start, end):
    """Open-Meteo 每日最大紫外线指数（模型数据）。"""
    c = CITIES[city]
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={c['lat']}&longitude={c['lon']}"
           f"&daily=uv_index_max&timezone=Asia%2FShanghai"
           f"&start_date={start}&end_date={end}")
    return json.loads(http_get(url, referer="", timeout=60))


def fetch_hourly(city, start, end):
    """Open-Meteo 小时模型值：{ 'YYYY-MM-DDTHH:00': {各要素} }（模型数据）。"""
    c = CITIES[city]
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={c['lat']}&longitude={c['lon']}"
           f"&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,"
           f"wind_direction_10m,uv_index,weather_code"
           f"&timezone=Asia%2FShanghai&start_date={start}&end_date={end}")
    r = json.loads(http_get(url, referer="", timeout=60))
    h = r["hourly"]
    return {t: {k: h[k][i] for k in h if k != "time"} for i, t in enumerate(h["time"])}


WIND_DIRS = ["北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风"]


def deg_to_cn(deg):
    """风向角度 -> 八方位中文名。"""
    if deg is None:
        return ""
    return WIND_DIRS[int((deg + 22.5) // 45) % 8]


# ---------- 日粒度 ----------
def load_daily():
    out = {}
    if os.path.exists(OUT_DAILY):
        with open(OUT_DAILY, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[(r["日期"], r["城市"])] = r
    return out


DAILY_FIELDS = ["日期", "城市", "最高温", "最低温", "最低湿度",
                "降水量", "天气", "风况", "紫外线指数", "数据源"]

WMO_TEXT = {  # Open-Meteo weather_code -> 中文天气现象
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴",
}


def month_list(from_date, today):
    """从 from_date 所在月到 today 所在月（含）的 YYYYMM 列表。"""
    y, m = from_date.year, from_date.month
    out = []
    while (y, m) <= (today.year, today.month):
        out.append(f"{y}{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def fetch_era5(city, start, end):
    """ERA5 再分析归档（补 CMA 不保留的旧日字段）。"""
    c = CITIES[city]
    url = (f"https://archive-api.open-meteo.com/v1/era5?latitude={c['lat']}&longitude={c['lon']}"
           f"&start_date={start}&end_date={end}"
           f"&daily=precipitation_sum,weather_code,wind_speed_10m_max"
           f"&timezone=Asia%2FShanghai")
    r = json.loads(http_get(url, referer="", timeout=60))
    return {d: {"precip": p, "code": wc, "windmax": ws}
            for d, p, wc, ws in zip(r["daily"]["time"],
                                    r["daily"]["precipitation_sum"],
                                    r["daily"]["weather_code"],
                                    r["daily"]["wind_speed_10m_max"])}


def backfill_daily(from_date, today):
    out = load_daily()
    cal_map = {}  # city -> {日期: 该日 CMA 日历原始条目}
    # 1) CMA 日历接口：高低温/最低湿度/降水(近5天)/天气/风况(当日及以后)
    for city in CITIES:
        cal_map[city] = {}
        for ym in month_list(from_date, today):
            try:
                cal = fetch_calendar(city, ym)
            except Exception as e:
                print(f"[日历] {city} {ym} 拉取失败: {e}")
                continue
            for dstr, d in cal.items():
                cal_map[city][dstr] = d
                if not (from_date <= datetime.strptime(dstr, "%Y%m%d").date() <= today):
                    continue
                prev = out.get((dstr, city), {})
                # CMA 字段为空时保留既有值（ERA5 补位/UV 跨运行不丢失）
                out[(dstr, city)] = {
                    "日期": dstr, "城市": city,
                    "最高温": d.get("hmax") or d.get("max") or prev.get("最高温", ""),
                    "最低温": d.get("hmin") or d.get("min") or prev.get("最低温", ""),
                    "最低湿度": (d.get("hgl") or prev.get("最低湿度", "") or "").rstrip("%"),
                    "降水量": d.get("rainobs") or prev.get("降水量", ""),
                    "天气": d.get("w1") or prev.get("天气", ""),
                    "风况": d.get("wd1") or prev.get("风况", ""),
                    "紫外线指数": prev.get("紫外线指数", ""),
                    "数据源": "中国天气网(CMA官方)",
                }
    # 2) UV 回填（Open-Meteo 模型值）
    for city in CITIES:
        try:
            uv = fetch_uv(city, from_date.isoformat(), today.isoformat())
            days = uv["daily"]["time"]
            vals = uv["daily"]["uv_index_max"]
            for dstr, v in zip(days, vals):
                key = (dstr.replace("-", ""), city)
                if key in out and v is not None:
                    out[key]["紫外线指数"] = round(v, 1)
        except Exception as e:
            print(f"[UV] {city} 拉取失败: {e}")
    # 3) ERA5 补位：CMA 不保留的旧日 降水/天气/风况
    # 注：ERA5 归档不含当天（昨天起可用），end 截到 today-1
    era_end = min(today, today - timedelta(days=1))
    for city in CITIES:
        try:
            era = fetch_era5(city, from_date.isoformat(), era_end.isoformat())
        except Exception as e:
            print(f"[ERA5] {city} 拉取失败: {e}")
            continue
        for dstr, e5 in era.items():
            key = (dstr.replace("-", ""), city)
            if key not in out:
                continue
            row = out[key]
            if not row["降水量"] and e5["precip"] is not None:
                row["降水量"] = round(e5["precip"], 1)
            if not row["天气"] and e5["code"] is not None:
                row["天气"] = WMO_TEXT.get(e5["code"], "")
            if not row["风况"] and e5["windmax"] is not None:
                row["风况"] = f"最大风速{e5['windmax']:.0f}km/h"
    # 4) 重算数据源标注：该日 CMA 无官方值的字段即为 ERA5 补位（跨运行保持准确）
    for (dstr, city), row in out.items():
        d = cal_map.get(city, {}).get(dstr)
        era_parts = []
        if row["降水量"] != "" and not (d and d.get("rainobs")):
            era_parts.append("降水")
        if row["天气"] and not (d and d.get("w1")):
            era_parts.append("天气")
        if row["风况"] and not (d and d.get("wd1")):
            era_parts.append("风况")
        row["数据源"] = "中国天气网(CMA官方)" + (
            "；" + "/".join(era_parts) + ":ERA5再分析(Open-Meteo)" if era_parts else "")
    with open(OUT_DAILY, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DAILY_FIELDS)
        w.writeheader()
        for k in sorted(out):
            w.writerow({x: out[k].get(x, "") for x in DAILY_FIELDS})
    print(f"[日粒度] 已写入 {OUT_DAILY}（{len(out)} 行）")


# ---------- 四正点采样 ----------
def load_points():
    out = {}
    if os.path.exists(OUT_POINTS):
        with open(OUT_POINTS, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[(r["日期"], r["城市"], r["时次"])] = r
    return out


POINT_FIELDS = ["日期", "城市", "时次", "温度", "湿度", "风向", "风速kmh",
                "气压", "能见度", "降水1h", "降水24h", "天气", "紫外线指数",
                "观测时间", "数据源"]


def write_points(out):
    os.makedirs(os.path.dirname(OUT_POINTS), exist_ok=True)
    with open(OUT_POINTS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=POINT_FIELDS)
        w.writeheader()
        for k in sorted(out):
            w.writerow({x: out[k].get(x, "") for x in POINT_FIELDS})
    print(f"[正点] 已写入 {OUT_POINTS}（{len(out)} 行）")


def sample_now(now=None):
    """抓当前正点两城实况并落盘（LaunchAgent 在 02/08/14/20 各调用一次）。
    紫外线为 Open-Meteo 小时模型值（CMA 实况接口无紫外线，已在数据源标注）。"""
    now = now or datetime.now()
    if now.hour not in (2, 8, 14, 20):
        print(f"[正点] 当前 {now.hour} 时非正点（02/08/14/20），跳过采样")
        return
    out = load_points()
    dstr = now.strftime("%Y%m%d")
    hour = f"{now.hour:02d}"
    iso = now.strftime("%Y-%m-%d")
    for city in CITIES:
        # 网络重试 2 次（Mac 睡眠唤醒后网络可能未就绪）
        sk = None
        for attempt in range(2):
            try:
                sk = fetch_sk(city)
                break
            except Exception as e:
                print(f"[实况] {city} 拉取失败（第 {attempt + 1} 次）: {e}")
                time.sleep(60)
        if sk is None:
            continue
        raw = os.path.join(RAW_DIR, f"sk2d_{city}_{dstr}_{hour}.json")
        os.makedirs(RAW_DIR, exist_ok=True)
        with open(raw, "w", encoding="utf-8") as f:
            json.dump(sk, f, ensure_ascii=False, indent=1)
        # 紫外线时次值（Open-Meteo 小时模型）
        uv = ""
        try:
            hv = fetch_hourly(city, iso, iso)
            cell = hv.get(f"{iso}T{hour}:00", {})
            uv = round(cell["uv_index"], 1) if cell.get("uv_index") is not None else ""
        except Exception as e:
            print(f"[UV时次] {city} 拉取失败: {e}")
        out[(dstr, city, hour)] = {
            "日期": dstr, "城市": city, "时次": hour,
            "温度": sk.get("temp", ""),
            "湿度": (sk.get("SD") or "").rstrip("%"),
            "风向": sk.get("WD", ""),
            "风速kmh": (sk.get("wse") or "").rstrip("km/h"),
            "气压": sk.get("qy", ""),
            "能见度": (sk.get("njd") or "").rstrip("km"),
            "降水1h": sk.get("rain", ""),
            "降水24h": sk.get("rain24h", ""),
            "天气": sk.get("weather", ""),
            "紫外线指数": uv,
            "观测时间": f"{dstr} {sk.get('time', '')}",
            "数据源": "中国天气网实况(CMA官方)；紫外线:Open-Meteo模型",
        }
        print(f"[实况] {dstr} {hour}时 {city}: {sk.get('temp')}℃ "
              f"湿度{sk.get('SD')} {sk.get('WD')}{sk.get('wse')} "
              f"能见度{sk.get('njd')} 降水{sk.get('rain')}mm {sk.get('weather')} "
              f"紫外线{uv}")
    write_points(out)


def backfill_points_for(dstr, cutoff=None):
    """用 Open-Meteo 小时模型值补指定日的四正点行（如建站首日回填、睡眠错过时次的兜底）。
    仅填补尚缺的时次，不覆盖已有数据；数据源标注模型。
    cutoff：只补早于该时刻（含）的时次——防止把当天还没到的 14/20 时提前用模型占位，
    导致之后真正的 CMA 实况采样无法写入。"""
    out = load_points()
    dd = dstr.replace("-", "")
    for city in CITIES:
        try:
            hv = fetch_hourly(city, dstr, dstr)
        except Exception as e:
            print(f"[正点回填] {city} {dstr} 拉取失败: {e}")
            continue
        for hour in ("02", "08", "14", "20"):
            key = (dd, city, hour)
            t = f"{dstr}T{hour}:00"
            hdt = datetime.strptime(t, "%Y-%m-%dT%H:%M")
            if cutoff and hdt > cutoff:
                continue  # 该时次还没到（或采样尚未开始），留给实况采样
            if key in out or t not in hv:
                continue
            v = hv[t]
            out[key] = {
                "日期": dd, "城市": city, "时次": hour,
                "温度": round(v["temperature_2m"], 1) if v.get("temperature_2m") is not None else "",
                "湿度": round(v["relative_humidity_2m"]) if v.get("relative_humidity_2m") is not None else "",
                "风向": deg_to_cn(v.get("wind_direction_10m")),
                "风速kmh": round(v["wind_speed_10m"], 1) if v.get("wind_speed_10m") is not None else "",
                "气压": "", "能见度": "", "降水1h": "", "降水24h": "",
                "天气": WMO_TEXT.get(v["weather_code"], "") if v.get("weather_code") is not None else "",
                "紫外线指数": round(v["uv_index"], 1) if v.get("uv_index") is not None else "",
                "观测时间": "",
                "数据源": "Open-Meteo模型(小时值)",
            }
            print(f"[正点回填] {dd} {hour}时 {city}: "
                  f"{out[key]['温度']}℃ 湿度{out[key]['湿度']} "
                  f"{out[key]['风向']}{out[key]['风速kmh']}km/h "
                  f"紫外线{out[key]['紫外线指数']} {out[key]['天气']}")
    write_points(out)


def aqi_sample():
    """正点 AQI 采样（cnEMC 官方城市小时值，见 aqi_point_collect.py）。"""
    try:
        import aqi_point_collect
        aqi_point_collect.sample_now()
    except Exception as e:
        print(f"[AQI正点] 采集失败: {e}")


def aqi_backfill():
    """补缺失的正点 AQI 时次（官方 24h 优先，站点聚合兜底）。"""
    try:
        import aqi_point_collect
        aqi_point_collect.backfill()
    except Exception as e:
        print(f"[AQI回填] 失败: {e}")


def wait_for_network(max_wait=600):
    """Mac 睡醒后网络常未就绪（DNS 解析失败）→ 循环等待，最长 max_wait 秒。
    网络正常时第一次检查即通过，几乎无开销；--nowait 可跳过。"""
    deadline = time.time() + max_wait
    n = 0
    while time.time() < deadline:
        try:
            socket.gethostbyname("d1.weather.com.cn")
            if n:
                print(f"[网络] 等待 {n * 30} 秒后 DNS 恢复，继续采集")
            return True
        except OSError:
            n += 1
            print(f"[网络] DNS 未就绪（睡醒后常见），30 秒后重试（第 {n} 次）")
            time.sleep(30)
    print(f"[网络] 等待 {max_wait} 秒仍未就绪，本轮放弃（留待下次回填兜底）")
    return False


def main(argv):
    today = date.today()
    if "--nowait" not in argv:
        wait_for_network()
    if "--sample" in argv:
        sample_now()
        aqi_sample()
        refresh_dashboard()
        return
    if "--backfill-points" in argv:
        i = argv.index("--backfill-points")
        backfill_points_for(argv[i + 1] if i + 1 < len(argv) else today.isoformat())
        refresh_dashboard()
        return
    from_date = START_DATE
    for a in argv:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", a)
        if m:
            from_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    backfill_daily(from_date, today)
    sample_now()
    # 兜底：Mac 睡眠睡过正点、launchd 补跑被守卫跳过 → 时次空缺。
    # 用小时模型值补「已过且缺失」的时次（昨天整天 + 今天已过时次），不覆盖实况采样。
    cutoff = datetime.now() - timedelta(hours=1)
    backfill_points_for((today - timedelta(days=1)).isoformat(), cutoff)
    backfill_points_for(today.isoformat(), cutoff)
    aqi_backfill()
    refresh_dashboard()


def refresh_dashboard():
    import subprocess
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "render_dashboard.py")],
                       check=True, timeout=60)
    except Exception as e:
        print(f"[render] 看板渲染失败: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
