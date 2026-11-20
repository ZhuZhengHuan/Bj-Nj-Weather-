#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
北京 / 南京 空气质量日报数据收集（cnEMC 官方城市日报版）
========================================================
数据源：中国环境监测总站 城市空气质量实时发布平台「城市日报」（官方）
  POST https://air.cnemc.cn:18007/HourChangesPublish/GetCityDayAqiHistoryByCondition?citycode=NNN
  返回近 14 天城市日报：AQI / 质量等级 / 首要污染物 / 六项浓度（PM2_5_24h 等为 24h 均值，
  O3_8h_24h 为日最大 8 小时滑动平均，即 HJ 633 日报口径）

原数据源 aqistudy.cn 于 2026-10-15 改版下线（旧 API 404，新站历史数据需登录付费会员），
2026-10-17 切换为本官方接口；经与存量数据交叉验证，两者逐日逐项完全一致（同一官方口径）。

用法：
  python3 collect_aqi.py                  # 抓取近 14 天官方城市日报（05:00/10:00 由 LaunchAgent 调用）
  python3 collect_aqi.py 202610           # 只写入该月行（日期仍需在近 14 天窗口内）
  python3 collect_aqi.py --nowait         # 跳过睡醒后网络等待

输出：aqi_daily.csv（日期+城市 去重，只补缺、不覆盖既有行，保留历史来源标注）
"""
import csv
import json
import os
import socket
import ssl
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "raw")
OUT_CSV = os.path.join(HERE, "aqi_daily.csv")
BASE = "https://air.cnemc.cn:18007"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CTX = ssl._create_unverified_context()  # 平台自签名证书，仅此只读接口

CITIES = {"北京": 110000, "南京": 320100}
SRC_LABEL = "cnEMC城市日报(官方)"
CITY_DAY_URL = BASE + "/HourChangesPublish/GetCityDayAqiHistoryByCondition?citycode={}"

# IAQI 分级浓度限值 (HJ 633—2012)：污染物 -> (浓度断点, IAQI断点)
IAQI_BP = {
    "PM2.5": ([0, 35, 75, 115, 150, 250, 350, 500],
              [0, 50, 100, 150, 200, 300, 400, 500]),
    "PM10":  ([0, 50, 150, 250, 350, 420, 500, 600],
              [0, 50, 100, 150, 200, 300, 400, 500]),
    "SO2":   ([0, 50, 150, 475, 800, 1600, 2100, 2620],
              [0, 50, 100, 150, 200, 300, 400, 500]),
    "NO2":   ([0, 40, 80, 180, 280, 565, 750, 940],
              [0, 50, 100, 150, 200, 300, 400, 500]),
    "CO":    ([0, 2, 4, 14, 24, 36, 48, 60],
              [0, 50, 100, 150, 200, 300, 400, 500]),
    "O3_8h": ([0, 100, 160, 215, 265, 800],
              [0, 50, 100, 150, 200, 300]),
}
# 接口字段 -> 标准名
POL_MAP = {"PM2_5_24h": "PM2.5", "PM10_24h": "PM10", "SO2_24h": "SO2",
           "NO2_24h": "NO2", "CO_24h": "CO", "O3_8h_24h": "O3_8h"}
# 平台首要污染物文案 -> 标准名
PRIMARY_MAP = {
    "颗粒物(PM10)": "PM10", "细颗粒物(PM2.5)": "PM2.5", "臭氧8小时(O3_8h)": "O3",
    "二氧化硫(SO2)": "SO2", "二氧化氮(NO2)": "NO2", "一氧化碳(CO)": "CO",
}


def iaqi(pol, c):
    """单污染物 IAQI，四舍五入（半进位），超过上限按 500 封顶。"""
    if c is None:
        return None
    bp, ip = IAQI_BP[pol]
    if c >= bp[-1]:
        return 500
    for i in range(len(bp) - 1):
        if bp[i] <= c < bp[i + 1]:
            val = (ip[i + 1] - ip[i]) / (bp[i + 1] - bp[i]) * (c - bp[i]) + ip[i]
            return int(val + 0.5)
    return None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ms_to_dt(s):
    """'/Date(1789873200000)/' → 北京时 datetime。"""
    ms = int("".join(ch for ch in s if ch.isdigit()))
    return datetime.fromtimestamp(ms / 1000, tz=timezone(timedelta(hours=8)))


def compute_row(item):
    """官方日报条目 → CSV 行，并复算 AQI 交叉验证（口径与平台一致为佳）。"""
    conc = {POL_MAP[k]: fnum(item.get(k)) for k in POL_MAP}
    iaqis = {k: iaqi(k, v) for k, v in conc.items() if v is not None}
    calc_aqi = max(iaqis.values()) if iaqis else None
    primary = PRIMARY_MAP.get(item.get("PrimaryPollutant") or "", "")
    site_aqi = fnum(item.get("AQI"))
    if site_aqi is not None:
        site_aqi = int(round(site_aqi))
    warn = ""
    if calc_aqi is not None and site_aqi is not None and calc_aqi != site_aqi:
        warn = f"复算AQI={calc_aqi}≠平台AQI={site_aqi}"
    # 与历史行一致：五项取整、CO 保留 1 位小数（平台发布口径即如此）
    disp = {k: (int(round(v)) if v is not None and k != "CO" else
                (round(v, 1) if v is not None else None))
            for k, v in conc.items()}
    return {
        "aqi": site_aqi, "quality": item.get("Quality", ""), "primary": primary,
        "pm25": disp["PM2.5"], "pm10": disp["PM10"], "so2": disp["SO2"],
        "no2": disp["NO2"], "co": disp["CO"], "o3": disp["O3_8h"],
        "warn": warn,
    }


def fetch_city_daily(city):
    """城市近 14 天官方日报 → {ISO日期: 行}，raw/ 留档。"""
    code = CITIES[city]
    req = urllib.request.Request(CITY_DAY_URL.format(code), data=b"{}",
                                 headers={"User-Agent": UA,
                                          "X-Requested-With": "XMLHttpRequest",
                                          "Referer": BASE + "/CityPublish/Index"})
    with urllib.request.urlopen(req, timeout=25, context=CTX) as resp:
        items = json.loads(resp.read().decode("utf-8"))
    os.makedirs(RAW_DIR, exist_ok=True)
    raw_path = os.path.join(RAW_DIR, f"cnemc_daily_{city}_{datetime.now():%Y%m%d%H%M}.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    rows = {}
    for it in items:
        d = ms_to_dt(it["TimePoint"]).strftime("%Y-%m-%d")
        rows[d] = compute_row(it)
    return rows


def load_out():
    if not os.path.exists(OUT_CSV):
        return {}
    out = {}
    with open(OUT_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[(r["日期"], r["城市"])] = r
    return out


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
    print(f"[网络] 等待 {max_wait} 秒仍未就绪，本轮放弃（留待下次兜底）")
    return False


FIELDS = ["日期", "城市", "AQI", "质量等级", "首要污染物",
          "PM2.5", "PM10", "SO2", "NO2", "CO", "O3_8h", "数据源"]


def main(argv):
    months = {a for a in argv if not a.startswith("--")}
    if "--nowait" not in argv:
        wait_for_network()

    out = load_out()
    added = 0
    for city in CITIES:
        rows = None
        for attempt in range(6):
            try:
                rows = fetch_city_daily(city)
                break
            except Exception as e:
                if attempt == 5:
                    print(f"{city} 拉取失败（重试 6 次仍失败）: {e}")
                else:
                    print(f"{city} 拉取失败（第 {attempt + 1} 次）: {e}，60 秒后重试")
                    time.sleep(60)
        if rows is None:
            continue
        for d, row in sorted(rows.items()):
            if months and d[:7].replace("-", "") not in months:
                continue
            key = (d, city)
            if key in out:
                continue  # 只补缺，不覆盖既有行（保留历史来源标注）
            out[key] = {
                "日期": d, "城市": city,
                "AQI": row["aqi"], "质量等级": row["quality"],
                "首要污染物": row["primary"],
                "PM2.5": row["pm25"], "PM10": row["pm10"],
                "SO2": row["so2"], "NO2": row["no2"],
                "CO": row["co"], "O3_8h": row["o3"],
                "数据源": SRC_LABEL,
            }
            added += 1
            flag = f"  [警告 {row['warn']}]" if row["warn"] else ""
            print(f"[{d}] {city}: AQI={row['aqi']} {row['quality']} "
                  f"首要={row['primary'] or '-'} "
                  f"PM2.5={row['pm25']} PM10={row['pm10']} "
                  f"SO2={row['so2']} NO2={row['no2']} CO={row['co']} "
                  f"O3-8h={row['o3']}{flag}")

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for key in sorted(out):
            w.writerow({k: out[key].get(k, "") for k in FIELDS})
    print(f"\n已写入 {OUT_CSV}（共 {len(out)} 行，本轮补缺 {added} 行）")

    # 刷新看板 data.js（渲染失败不影响数据收集本身）
    try:
        import subprocess
        subprocess.run([sys.executable, os.path.join(HERE, "render_dashboard.py")],
                       check=True, timeout=60)
    except Exception as e:
        print(f"[render] 看板渲染失败（数据已保存）: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
