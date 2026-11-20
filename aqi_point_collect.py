#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
北京 / 南京 正点（02/08/14/20 时）空气质量采集（中国环境监测总站 cnEMC）
=========================================================================
数据源：https://air.cnemc.cn:18007（全国城市空气质量实时发布平台，官方）

接口（均为只读）：
  1) POST /HourChangesPublish/GetCityRealTimeAqiHistoryByCondition?citycode=NNN
     —— 城市级最近 24 小时逐时数据（AQI/等级/首要污染物/六项浓度），官方城市值
  2) POST /HourChangesPublish/GetAQIHistoryByConditionHis   body: date=YYYY-MM-DD HH:00
     —— 全国站点该时次数据（用于回填超出 24h 窗口的时次）
  3) GET  /CityData/GetAllCityRealTimeAQIModels（备用，未用）

城市代码：北京 110000，南京 320100。
注：站点自签名证书 → 关闭 SSL 验证（仅这两个只读接口）。

聚合口径（经与官方城市值交叉验证）：
  城市浓度 = 站点算术平均（与官方一致）；城市 AQI ≈ 站点 AQI 均值（±1，优级内无影响）；
  仅用于官方 24h 窗口之外的时次回填，数据源如实标注「站点聚合」。

用法：
  python3 aqi_point_collect.py --sample           # 当前正点采样（官方城市值）
  python3 aqi_point_collect.py --backfill         # 补缺失时次（官方优先，站点聚合兜底）
  python3 aqi_point_collect.py --backfill-hours 2026-09-20  # 回填指定日四正点

输出：aqi_points.csv（日期,城市,时次,AQI,质量等级,首要污染物,PM2.5,PM10,SO2,NO2,CO,O3,观测时间,数据源）
"""
import csv
import json
import os
import socket
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from collect_aqi import iaqi  # 复用 HJ 633 IAQI 计算

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "aqi_points.csv")
BASE = "https://air.cnemc.cn:18007"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CTX = ssl._create_unverified_context()  # 站点自签名证书，仅这两个只读接口

CITIES = {"北京": 110000, "南京": 320100}
HOURS = ("02", "08", "14", "20")

FIELDS = ["日期", "城市", "时次", "AQI", "质量等级", "首要污染物",
          "PM2.5", "PM10", "SO2", "NO2", "CO", "O3", "观测时间", "数据源"]

POLS = {"PM2_5": "PM2.5", "PM10": "PM10", "SO2": "SO2", "NO2": "NO2", "CO": "CO", "O3": "O3"}
QUALITY = [(50, "优"), (100, "良"), (150, "轻度污染"), (200, "中度污染"),
           (300, "重度污染"), (10 ** 9, "严重污染")]


def http_post(path, body=None):
    req = urllib.request.Request(BASE + path, data=(body or "{}").encode(),
                                 headers={"User-Agent": UA,
                                          "X-Requested-With": "XMLHttpRequest",
                                          "Referer": BASE + "/HourChangesPublish/Index"})
    with urllib.request.urlopen(req, timeout=25, context=CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ms_to_dt(s):
    """'/Date(1789873200000)/' → 北京时 datetime。"""
    ms = int("".join(ch for ch in s if ch.isdigit()))
    return datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8)))


def fetch_city_history(city):
    """城市最近 24 小时官方逐时值 → {hour('HH'): 行}（行内带日期，用前需校验）。"""
    rows = http_post(f"/HourChangesPublish/GetCityRealTimeAqiHistoryByCondition"
                     f"?citycode={CITIES[city]}")
    out = {}
    for r in rows:
        dt = ms_to_dt(r["TimePoint"])
        out[dt.strftime("%H")] = {"dt": dt, "row": r}
    return out


def quality_of(aqi):
    for lim, name in QUALITY:
        if aqi <= lim:
            return name
    return "严重污染"


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def aggregate_station(city, dt):
    """站点数据聚合为城市小时值（浓度=均值，AQI=站点AQI均值，仅官方窗口外兜底用）。"""
    rows = http_post("/HourChangesPublish/GetAQIHistoryByConditionHis",
                     f"date={dt.strftime('%Y-%m-%d %H:00')}")
    sts = [r for r in rows if r["Area"] == city + "市"]
    if not sts:
        return None
    # 接口对无数据的时次会静默返回最新时次的数据 → 必须校验实际 TimePoint
    tps = {r.get("TimePoint") for r in rows}
    if tps and ms_to_dt(next(iter(tps))).replace(tzinfo=None) != dt:
        print(f"[AQI回填] {city} 站点接口无 {dt:%H} 时数据（返回 {tps}），跳过")
        return None
    means = {}
    for k, name in POLS.items():
        vals = [fnum(r[k]) for r in sts if fnum(r[k]) is not None]
        if vals:
            means[name] = round(sum(vals) / len(vals), 1)
    aqis = [int(r["AQI"]) for r in sts if str(r["AQI"]).isdigit()]
    if not aqis:
        return None
    aqi = round(sum(aqis) / len(aqis))
    primary = "—"  # AQI≤50 无首要污染物，与官方口径一致
    if aqi > 50:
        # O3 小时值借用 O3_8h 分级限值表（与实时平台口径一致）
        iaqis = {k: iaqi("O3_8h" if k == "O3" else k, v) for k, v in means.items()}
        if iaqis:
            primary = max(iaqis, key=iaqis.get)
            primary = {"O3_8h": "O3"}.get(primary, primary)
    return {
        "AQI": aqi, "质量等级": quality_of(aqi), "首要污染物": primary,
        "PM2.5": means.get("PM2.5", ""), "PM10": means.get("PM10", ""),
        "SO2": means.get("SO2", ""), "NO2": means.get("NO2", ""),
        "CO": means.get("CO", ""), "O3": means.get("O3", ""),
        "观测时间": dt.strftime("%Y-%m-%d %H:00"),
        "数据源": "cnEMC站点聚合(官方站点数据)",
    }


def row_from_city(r, city):
    """官方城市行 → CSV 行。"""
    return {
        "AQI": r["row"]["AQI"], "质量等级": r["row"]["Quality"],
        "首要污染物": r["row"]["PrimaryPollutant"] or "—",
        "PM2.5": r["row"]["PM2_5"], "PM10": r["row"]["PM10"],
        "SO2": r["row"]["SO2"], "NO2": r["row"]["NO2"],
        "CO": r["row"]["CO"], "O3": r["row"]["O3"],
        "观测时间": r["dt"].strftime("%Y-%m-%d %H:00"),
        "数据源": "cnEMC城市小时值(官方)",
    }


def load_out():
    out = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[(r["日期"], r["城市"], r["时次"])] = r
    return out


def write_out(out):
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for k in sorted(out):
            w.writerow({x: out[k].get(x, "") for x in FIELDS})
    print(f"[AQI正点] 已写入 {OUT}（{len(out)} 行）")


def sample_now(now=None):
    """当前正点采样：官方城市 24h 接口取目标时次行（launchd 在 02/08/14/20 各调一次）。"""
    now = now or datetime.now()
    if now.hour not in (2, 8, 14, 20):
        print(f"[AQI正点] 当前 {now.hour} 时非正点（02/08/14/20），跳过")
        return
    out = load_out()
    dstr = now.strftime("%Y%m%d")
    hour = f"{now.hour:02d}"
    for city in CITIES:
        hist = None
        for attempt in range(3):
            try:
                hist = fetch_city_history(city)
                break
            except Exception as e:
                print(f"[AQI正点] {city} 拉取失败（第 {attempt + 1} 次）: {e}")
                time.sleep(60)
        r = hist.get(hour) if hist else None
        if r is None or r["dt"].date() != now.date():
            print(f"[AQI正点] {dstr} {hour}时 {city}: 官方值尚未发布，留待回填兜底")
            continue
        out[(dstr, city, hour)] = {
            "日期": dstr, "城市": city, "时次": hour,
            **row_from_city(r, city),
        }
        rr = out[(dstr, city, hour)]
        print(f"[AQI正点] {dstr} {hour}时 {city}: AQI={rr['AQI']} {rr['质量等级']} "
              f"首要={rr['首要污染物'] or '-'} PM2.5={rr['PM2.5']} PM10={rr['PM10']} "
              f"SO2={rr['SO2']} NO2={rr['NO2']} CO={rr['CO']} O3={rr['O3']}")
    write_out(out)


def fill_hour(out, city, dt, official_first=True):
    """补一个时次：官方 24h 窗口优先（日期需吻合），超出则站点聚合。"""
    if dt > datetime.now():
        return  # 未来时次不补
    dstr = dt.strftime("%Y%m%d")
    hour = dt.strftime("%H")
    key = (dstr, city, hour)
    try:
        hist = fetch_city_history(city)
        r = hist.get(hour)
        if r is not None and r["dt"].date() == dt.date():
            if key not in out or "站点聚合" in out[key].get("数据源", ""):
                out[key] = {"日期": dstr, "城市": city, "时次": hour,
                            **row_from_city(r, city)}
                print(f"[AQI回填] {dstr} {hour}时 {city}: 官方 AQI={out[key]['AQI']} {out[key]['质量等级']}")
            return
    except Exception as e:
        print(f"[AQI回填] {city} 官方24h拉取失败: {e}")
    if official_first and key not in out:
        try:
            agg = aggregate_station(city, dt)
        except Exception as e:
            print(f"[AQI回填] {city} 站点聚合失败: {e}")
            return
        if agg:
            out[key] = {"日期": dstr, "城市": city, "时次": hour, **agg}
            print(f"[AQI回填] {dstr} {hour}时 {city}: 站点聚合 AQI={agg['AQI']} {agg['质量等级']}")


def backfill(days=2):
    """补最近 N 天所有缺失正点时次（官方优先，站点聚合兜底）。"""
    out = load_out()
    today = datetime.now().replace(minute=0, second=0, microsecond=0)
    for d in range(days):
        day = today - timedelta(days=d)
        for hour in HOURS:
            dt = day.replace(hour=int(hour))
            for city in CITIES:
                try:
                    fill_hour(out, city, dt)
                except Exception as e:
                    print(f"[AQI回填] {dt:%Y%m%d} {hour}时 {city} 失败: {e}")
    write_out(out)


def wait_for_network(max_wait=600):
    """Mac 睡醒后网络常未就绪（DNS 解析失败）→ 循环等待，最长 max_wait 秒。"""
    deadline = time.time() + max_wait
    n = 0
    while time.time() < deadline:
        try:
            socket.gethostbyname("air.cnemc.cn")
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
    if "--nowait" not in argv:
        wait_for_network()
    if "--sample" in argv:
        sample_now()
        return
    if "--backfill-hours" in argv:
        i = argv.index("--backfill-hours")
        d = datetime.strptime(argv[i + 1], "%Y-%m-%d")
        out = load_out()
        for hour in HOURS:
            dt = d.replace(hour=int(hour))
            for city in CITIES:
                fill_hour(out, city, dt)
        write_out(out)
        return
    backfill()


if __name__ == "__main__":
    main(sys.argv[1:])
