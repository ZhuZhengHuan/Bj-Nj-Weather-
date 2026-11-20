#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 aqi_daily.csv / met_daily.csv / met_points.csv 渲染成 data.js（window.AQI_DATA），
供 index.html 看板读取。纯标准库，可独立运行：python3 render_dashboard.py
"""
import csv
import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "aqi_daily.csv")
MET_SRC = os.path.join(HERE, "met_daily.csv")
PTS_SRC = os.path.join(HERE, "met_points.csv")
AQI_PTS_SRC = os.path.join(HERE, "aqi_points.csv")
OUT = os.path.join(HERE, "data.js")

# HJ 633—2012 空气质量指数级别（上限, 名称, 国标色）
LEVELS = [
    (50, "优", "#00E400"),
    (100, "良", "#FFFF00"),
    (150, "轻度污染", "#FF7E00"),
    (200, "中度污染", "#FF0000"),
    (300, "重度污染", "#99004C"),
    (10 ** 9, "严重污染", "#7E0023"),
]


def level_of(aqi):
    for lim, name, color in LEVELS:
        if aqi is not None and aqi <= lim:
            return name, color
    return "—", "#898781"


def num(v):
    try:
        return float(v) if v not in ("", None) else None
    except (TypeError, ValueError):
        return None


def iso(d):
    """YYYYMMDD -> YYYY-MM-DD（与 AQI 行日期格式统一）。"""
    d = d.strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and "-" not in d else d


def main():
    if not os.path.exists(SRC):
        print(f"[render] 未找到 {SRC}，跳过")
        return
    rows = []
    with open(SRC, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            aqi = num(r.get("AQI"))
            name, color = level_of(aqi)
            rows.append({
                "date": r["日期"],
                "city": r["城市"],
                "aqi": aqi,
                "level": r.get("质量等级") or name,
                "color": color,
                "primary": (r.get("首要污染物") or "").strip(),
                "pm25": num(r.get("PM2.5")),
                "pm10": num(r.get("PM10")),
                "so2": num(r.get("SO2")),
                "no2": num(r.get("NO2")),
                "co": num(r.get("CO")),
                "o3": num(r.get("O3_8h")),
                "src": r.get("数据源", ""),
            })
    sources = sorted({r["src"] for r in rows if r.get("src")})

    # ---- 气象日报（met_daily.csv）----
    mets = []
    if os.path.exists(MET_SRC):
        with open(MET_SRC, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                mets.append({
                    "date": iso(r["日期"]),
                    "city": r["城市"],
                    "tmax": num(r.get("最高温")),
                    "tmin": num(r.get("最低温")),
                    "rhmin": num(r.get("最低湿度")),
                    "rain": num(r.get("降水量")),
                    "weather": r.get("天气", ""),
                    "wind": r.get("风况", ""),
                    "uv": num(r.get("紫外线指数")),
                    "src": r.get("数据源", ""),
                })
    met_sources = sorted({m["src"] for m in mets if m.get("src")})

    # ---- 四正点实况（met_points.csv，02/08/14/20）----
    pts = []
    if os.path.exists(PTS_SRC):
        with open(PTS_SRC, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                pts.append({
                    "date": iso(r["日期"]),
                    "city": r["城市"],
                    "hour": r["时次"],
                    "temp": num(r.get("温度")),
                    "rh": num(r.get("湿度")),
                    "winddir": r.get("风向", ""),
                    "windspeed": num(r.get("风速kmh")),
                    "pressure": num(r.get("气压")),
                    "vis": num(r.get("能见度")),
                    "rain1h": num(r.get("降水1h")),
                    "rain24h": num(r.get("降水24h")),
                    "weather": r.get("天气", ""),
                    "uv": num(r.get("紫外线指数")),
                    "obstime": r.get("观测时间", ""),
                    "src": r.get("数据源", ""),
                })
    pt_sources = sorted({p.get("obstime") and "中国天气网实况(CMA官方)"
                         for p in pts if p.get("obstime")})

    # ---- 正点 AQI（aqi_points.csv，02/08/14/20，cnEMC 官方）----
    aqi_pts = []
    if os.path.exists(AQI_PTS_SRC):
        with open(AQI_PTS_SRC, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                aqi = num(r.get("AQI"))
                name, color = level_of(aqi)
                aqi_pts.append({
                    "date": iso(r["日期"]),
                    "city": r["城市"],
                    "hour": r["时次"],
                    "aqi": aqi,
                    "level": r.get("质量等级") or name,
                    "color": color,
                    "primary": (r.get("首要污染物") or "—").strip(),
                    "pm25": num(r.get("PM2.5")),
                    "pm10": num(r.get("PM10")),
                    "so2": num(r.get("SO2")),
                    "no2": num(r.get("NO2")),
                    "co": num(r.get("CO")),
                    "o3": num(r.get("O3")),
                    "obstime": r.get("观测时间", ""),
                    "src": r.get("数据源", ""),
                })
    aqi_pt_sources = sorted({p["src"] for p in aqi_pts if p.get("src")})

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sources": sources,
        "rows": sorted(rows, key=lambda x: (x["date"], x["city"])),
        "met": sorted(mets, key=lambda x: (x["date"], x["city"])),
        "metSources": met_sources,
        "points": sorted(pts, key=lambda x: (x["date"], x["city"], x["hour"])),
        "pointSources": pt_sources,
        "aqiPoints": sorted(aqi_pts, key=lambda x: (x["date"], x["city"], x["hour"])),
        "aqiPointSources": aqi_pt_sources,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("/* 由 render_dashboard.py 自动生成，勿手改 */\n")
        f.write("window.AQI_DATA = " +
                json.dumps(payload, ensure_ascii=False, indent=1) + ";\n")
    print(f"[render] AQI {len(rows)} 行 + 气象日值 {len(mets)} 行"
          f" + 正点实况 {len(pts)} 行 + 正点AQI {len(aqi_pts)} 行 -> {OUT}")


if __name__ == "__main__":
    main()
