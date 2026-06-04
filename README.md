# A股「蓄水池」监控仪表盘 · 取数与自动部署

数据与展示分离：`pull.py` 取数 → 写入 `data.json` → `index.html` 只读渲染。
GitHub Actions 定时跑 `pull.py` 并提交，GitHub Pages 公开网页，实现自动刷新。

## 文件

| 文件 | 作用 | 谁维护 |
|---|---|---|
| `index.html` | 前端仪表盘（原 `A股蓄水池监控仪表盘.html`，**勿改结构**） | 人工 |
| `data.json` | 数据，唯一数据源 | `pull.py` 自动 + 少量人工 |
| `pull.py` | 取数脚本（akshare 实时 + 自动判定） | — |
| `requirements.txt` | 依赖：akshare、pandas | — |
| `.github/workflows/update-data.yml` | 定时取数 + 自动提交 | — |
| `取数需求文档.md` | 原始需求 | 人工 |

> GitHub Pages 默认首页是 `index.html`，所以前端已用此文件名。前端通过相对路径
> `fetch('data.json')` 读数，二者同目录即可。

## 本地运行

```bash
pip install -r requirements.txt
python pull.py            # 拉数 → 更新 data.json（并同步 index.html 内置兜底）

# 本地预览（必须经 HTTP，file:// 读不到 data.json）
python -m http.server 8000
# 浏览器打开 http://localhost:8000/index.html
```

`pull.py` 每项指标独立容错：某源失败会**保留旧值并打 warning**，不会让整脚本崩；
运行结束打印一行 ✓/⚠ 摘要。可重复运行（幂等）。

## 部署到 GitHub Pages

1. 新建仓库，把本目录全部文件推上去。
2. **Settings → Pages**：Source 选 `Deploy from a branch`，分支 `main` / 根目录 `/`。
   几分钟后得到 `https://<用户名>.github.io/<仓库名>/`。
3. **Settings → Actions → General → Workflow permissions**：选 **Read and write permissions**
   （workflow 要 commit 回 `data.json`）。
4. 到 **Actions** 页手动触发一次 `更新 data.json`，确认跑通并提交。
5. 之后按 `update-data.yml` 的 cron 自动运行（每月 16 日 + 每周一，UTC 01:30 / 北京 09:30）。

> 若用到 Tushare 备选源：把 token 放 **Settings → Secrets and variables → Actions** 里的
> `TUSHARE_TOKEN`，**绝不要**写进代码或 `data.json`。本仪表盘无敏感信息，页面公开无碍。

## 自动化程度（重要 · 实事求是）

`pull.py` 按需求 §5 计算 7 条退潮信号与 `assessment`。各项现状：

| 项 | 内容 | 状态 |
|---|---|---|
| snapshot `m2`/`m1` | 货币供应同比 | ✅ 自动（`macro_china_money_supply`，列名按关键词匹配） |
| snapshot `scissor` | M2−M1 剪刀差 | ✅ 自动（逐月相减派生） |
| snapshot `tsf` | 社融**存量(余额)** | ⚠️ **保留人工值**：akshare 只有社融**增量**，无稳定存量接口。按央行月度金融数据手工更新 `value/series/note` |
| snapshot `team` | 国家队 ETF 持仓 | ⚠️ 保留人工值：季报、严重滞后，按基金季报更新 |
| 信号#2 两融过热 | 沪市两融近 ~20 交易日涨幅 >15% | ✅ 自动（`stock_margin_sse`；以沪市为市场趋势代理，沪+深合并可按需扩展） |
| 信号#3 ETF 净赎回 | 龙头宽基 ETF 合计份额近 N 个采样点净变化 <0 | ✅ 自动（**累积式**）：akshare 无份额历史，每次跑抓 `fund_etf_spot_em` 当日「最新份额」存入 `etf_share_history.json`，与 N 点前对比。**首次只播种、需多次运行后才出判定**；采样点=运行次，按周跑则窗口≈N 周 |
| 信号#4 货币转紧 | M2 同比连续 2 月下行 | ✅ 自动（基于 m2 series） |
| 信号#5 IPO 提速 | 当月 IPO 募资额环比 >50% | ❌ 未自动：免费 akshare 无干净的「月度 IPO 募资额」源（`stock_zh_a_new` 无上市日期/募资额），保持未触发。需 Wind/Choice 或证监会月报，或人工 |
| 信号#6 汇金减持 | 季报持仓环比下降 | ⚠️ 季报滞后，未自动 |
| 信号#7 通缩结束 | CPI 与 PPI 同比连续 3 月 >0 | ✅ 自动（`macro_china_cpi_yearly` / `_ppi_yearly`） |
| 信号#1 政策口风 | 措辞转“防过热/防风险” | ⚠️ 质性/事件型，未自动（可选 NLP 关键词，见下） |

`assessment.summary` 由触发数映射：0–2「仍在蓄水·水位偏高」/ 3–4「攻守转换中」/ 5–7「退潮信号密集」。
`rationale` 按当前读数模板化生成；`watch` 列出尚未触发的领先信号。

## 首次运行必做：核对 akshare 接口/列名

⚠️ **akshare 函数名与返回列名随版本变化**。`pull.py` 已用关键词匹配列名来抗漂移，
但首次跑请对照官方文档确认，重点是标 🔶 的两项：

```bash
python - <<'PY'
import akshare as ak
print(ak.macro_china_money_supply().columns.tolist())   # 确认含 M2/M1 同比列
print(ak.stock_margin_sse(start_date="20260101", end_date="20260401").columns.tolist())
print(ak.macro_china_cpi_yearly().columns.tolist())
# ETF 份额、IPO 募资额接口名可能已改，按文档调整 pull.py 顶部对应函数
PY
```

- ETF 份额（#3）：依赖 `fund_etf_spot_em()` 的「最新份额」列与 `BROAD_ETFS` 代码列表；
  历史靠 `etf_share_history.json` 累积，**该文件需随 Actions 一起提交**（workflow 已配）。
- IPO 募资额（#5）：免费源缺口，`fetch_ipo_surge` 默认取不到即不触发；如有 Wind/Choice 可自行补。
- 阈值与 ETF 代码（涨幅 15% / 窗口 10 / `BROAD_ETFS`）在 `pull.py` 顶部配置区，可调。

## 可选增强

- **政策口风 NLP（#1）**：抓央行/证监会公告、新华社/政治局会议公报，做关键词匹配
  （「维护稳定/活跃市场」vs「防风险/防过热/挤泡沫」）作为弱信号写入 `checklist[0].now`。
- **沪深两融合并**：当前 #2 以沪市为代理；如需严格“沪+深”，对深市按日循环
  `stock_margin_szse`/`stock_margin_detail_szse` 求和再合并。

---
免责声明：本仪表盘仅为信息梳理与“水温/姿态”启发式读法，非涨跌预测，不构成投资建议。
