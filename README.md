# 北京 · 南京 空气质量与气象数据看板

在线看板：<https://mobiushuan-commits.github.io/bj-nj-data-/>

由 GitHub Actions 定时在线采集（不依赖任何本地机器），每 3 小时运行一次，采集后自动提交数据并刷新页面。

## 数据内容

- **AQI 日报**：北京 / 南京每日 AQI、质量等级、首要污染物、六项污染物浓度（24h 均值 + O₃-8h 日最大 8 小时滑动平均，HJ 633 日报口径）
- **气象日报**：最高/最低温、最低湿度、降水量、天气、风况、紫外线指数
- **正点数据（02/08/14/20 时）**：温度、湿度、风向风速、气压、能见度、天气、紫外线指数 + AQI 及六项小时浓度

## 数据源（页面数据源列均有如实标注）

| 数据 | 来源 |
|---|---|
| AQI 日报 | 中国环境监测总站城市日报（air.cnemc.cn，官方，近 14 天滚动） |
| AQI 正点 | 中国环境监测总站城市小时值（air.cnemc.cn，官方） |
| 气象日报 | 中国天气网历史日历（CMA 官方）；紫外线为 Open-Meteo 模型值；旧日降水/天气/风况为 ERA5 再分析（Open-Meteo）补位 |
| 气象正点 | 中国天气网实况（CMA 官方）；紫外线为 Open-Meteo 小时模型值；个别缺失时次为模型回填 |

## 文件结构

- `index.html` / `data.js` — 看板页面与数据
- `*.csv` — 原始数据（历史完整，随每次采集增量更新）
- `collect_aqi.py` — AQI 日报采集
- `weather_collect.py` — 气象日报 + 四正点实况 + 回填兜底（内含 `aqi_point_collect.py` 的正点 AQI 采样与回填）
- `render_dashboard.py` — CSV → data.js
- `.github/workflows/collect.yml` — 在线定时采集工作流

## 本地运行

```bash
python3 collect_aqi.py --nowait
python3 weather_collect.py --nowait
python3 render_dashboard.py
```

依赖：仅 Python 标准库（HTTPS 证书优先用 certifi，无则回退系统 CA 库）。
