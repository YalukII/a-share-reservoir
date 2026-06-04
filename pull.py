#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pull.py — A股「蓄水池」监控仪表盘 取数脚本
================================================

按《取数需求文档.md》的数据契约，拉取真实宏观/资金数据并写回 data.json。
前端 HTML 只读 data.json 渲染，本脚本不改动 HTML 结构（仅可选同步内置兜底数据）。

设计原则
--------
1. 数据与展示分离：脚本只更新 §2 指定的字段，其余人工字段原样保留。
   可更新：snapshot[].value/series/note/period、checklist[].now、
           assessment、meta.updated/snapshotLabel/dataNote、groups[].rows[].current(可选)
   不动：  snapshot[].label/desc/tier、groups 文案、tools、sources
2. 鲁棒：每个指标独立 try/except，失败【保留旧值】并打 warning，绝不让整脚本崩。
3. 幂等：可重复运行，结果只取决于当前数据源。
4. 列名/函数名按版本漂移 → 用【关键词匹配列名】而非硬编码，尽量自适应。

⚠️ akshare 接口名与列名随版本变化。本脚本对常用列做了关键词匹配，
   但首次运行仍建议核对官方文档：https://akshare.akfamily.xyz/data/macro/macro.html
"""

from __future__ import annotations

import json
import re
import sys
import datetime as dt
from pathlib import Path

# akshare 是可选依赖：缺失时脚本不崩，全部指标走"保留旧值"分支。
try:
    import akshare as ak
except Exception as e:  # pragma: no cover
    ak = None
    print(f"[WARN] 未能导入 akshare（{e}）。所有指标将保留 data.json 旧值。")

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None

HERE = Path(__file__).resolve().parent
DATA_PATH = HERE / "data.json"
HTML_PATH = HERE / "index.html"  # 可选同步兜底数据；若文件名不同请改这里

# ------------------------------------------------------------------ 配置 ----
# 龙头宽基 ETF（checklist #3 份额净申赎）。代码为 6 位，沪深均可。
BROAD_ETFS = ["510300", "510050", "588000", "159845", "510500", "159919"]

# 自动判定阈值（§5 建议初值，可调）
MARGIN_SURGE_PCT = 15.0     # #2 两融近 ~20 交易日涨幅 > 15% 视为过热
ETF_WINDOW = 10             # #3 ETF 份额观察窗口（交易日）
IPO_MOM_PCT = 50.0          # #5 当月 IPO 募资额环比 > 50% 视为提速
N_SNAP = 12                 # snapshot 折线固定 12 个点

# 运行摘要：记录每项 ok / warn
REPORT: list[str] = []
def ok(msg: str):   REPORT.append(f"  ✓ {msg}");  print(f"[OK]   {msg}")
def warn(msg: str): REPORT.append(f"  ⚠ {msg}");  print(f"[WARN] {msg}")


# =====================================================================
#  通用工具
# =====================================================================
def load_data() -> dict:
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def snap(data: dict, sid: str) -> dict | None:
    for s in data.get("snapshot", []):
        if s.get("id") == sid:
            return s
    return None


def find_col(df, *keyword_groups):
    """返回第一个【列名包含某一组里全部关键词】的列名；找不到返回 None。
    用法：find_col(df, ['M2','同比']) 或 find_col(df, ['今值'], ['close'])。"""
    cols = [str(c) for c in df.columns]
    for kws in keyword_groups:
        for c in cols:
            if all(k.lower() in c.lower() for k in kws):
                return c
    return None


def parse_ym(text) -> tuple[int, int] | None:
    """从 '2025年04月份' / '2025-04' / '202504' 等解析出 (年, 月)。"""
    s = str(text)
    m = re.search(r"(\d{4})\D*(\d{1,2})", s)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if 1 <= mo <= 12:
        return (y, mo)
    return None


def last_n_by_month(df, month_col, value_col, n=N_SNAP, ndigits=1):
    """把 (月份, 数值) 整理成按月升序、去重、取最后 n 个的数值列表。"""
    rows = []
    for _, r in df.iterrows():
        ym = parse_ym(r[month_col])
        v = r[value_col]
        if ym is None or pd is None or pd.isna(v):
            continue
        try:
            rows.append((ym, round(float(v), ndigits)))
        except (TypeError, ValueError):
            continue
    # 同月去重保留最后一次出现；再按月排序
    dedup = {}
    for ym, v in rows:
        dedup[ym] = v
    ordered = sorted(dedup.items(), key=lambda kv: kv[0])
    tail = ordered[-n:]
    months = [ym for ym, _ in tail]
    values = [v for _, v in tail]
    return months, values


# =====================================================================
#  snapshot 取数
# =====================================================================
def update_money_supply(data: dict) -> tuple[int, int] | None:
    """更新 m2 / m1 同比 series。返回 m2 最新月份 (y, m)，供 meta.updated 用。"""
    if ak is None:
        warn("m2/m1：akshare 不可用，保留旧值")
        return None
    try:
        df = ak.macro_china_money_supply()
        month_col = find_col(df, ["月份"], ["日期"]) or str(df.columns[0])
        m2_col = find_col(df, ["M2", "同比"])
        m1_col = find_col(df, ["M1", "同比"])
        if not (m2_col and m1_col):
            raise RuntimeError(f"未匹配到 M2/M1 同比列；现有列={list(df.columns)}")

        m2_months, m2_vals = last_n_by_month(df, month_col, m2_col)
        m1_months, m1_vals = last_n_by_month(df, month_col, m1_col)
        if len(m2_vals) < 2 or len(m1_vals) < 2:
            raise RuntimeError("有效月份不足")

        s2 = snap(data, "m2")
        if s2:
            s2["series"] = m2_vals
            s2["value"] = m2_vals[-1]
        s1 = snap(data, "m1")
        if s1:
            s1["series"] = m1_vals
            s1["value"] = m1_vals[-1]
        ok(f"m2={m2_vals[-1]}% / m1={m1_vals[-1]}%（截至 {m2_months[-1][0]}-{m2_months[-1][1]:02d}）")
        return m2_months[-1]
    except Exception as e:
        warn(f"m2/m1 取数失败，保留旧值：{e}")
        return None


def update_scissor(data: dict):
    """M2–M1 剪刀差 = 逐月 m2 - m1（依赖上面已更新的 series）。"""
    try:
        s2, s1, sc = snap(data, "m2"), snap(data, "m1"), snap(data, "scissor")
        if not (s2 and s1 and sc):
            return
        a, b = s2["series"], s1["series"]
        n = min(len(a), len(b))
        diff = [round(a[-n + i] - b[-n + i], 1) for i in range(n)]
        sc["series"] = diff
        sc["value"] = diff[-1]
        ok(f"scissor（M2-M1 剪刀差）={diff[-1]} pct")
    except Exception as e:
        warn(f"scissor 计算失败，保留旧值：{e}")


def update_tsf(data: dict):
    """社融存量(余额)。akshare 主要提供【增量】，存量需官方值/另接口。
    策略：尝试可用接口；拿不到则【保留旧值】并提示人工更新（符合 §3/§9）。"""
    if ak is None:
        warn("tsf：akshare 不可用，保留旧值")
        return
    s = snap(data, "tsf")
    if not s:
        return
    # 已知坑：社融要"存量(余额)"不是"增量"。akshare 的 macro_china_shrzgm 是【增量】，
    # 不能直接当存量用。这里不做错误替换——保留人工维护的央行实际存量值，仅打提示。
    warn("tsf（社融存量余额）：akshare 无稳定的「存量」接口（其社融为增量），保留旧值。"
         "请按央行月度金融数据手工更新 value/series/note（同比%）。")


def update_team(data: dict):
    """国家队 ETF 持仓（季报，前十大持有人汇总）。
    质性/季度滞后，难以单接口稳定自动化 → 保留旧值（符合 §4）。"""
    warn("team（国家队 ETF 持仓·季报）：滞后/质性，保留旧值，按基金季报人工更新。")


# =====================================================================
#  checklist 信号源（§5）
# =====================================================================
def fetch_margin_surge() -> dict:
    """#2 两融过热：沪市融资融券余额近 ~20 交易日涨幅。
    以 SSE 汇总时间序列为主信号（市场两融趋势的稳健代理）。"""
    out = {"now": False, "detail": "未取到"}
    if ak is None:
        return out
    try:
        today = dt.date.today()
        start = (today - dt.timedelta(days=90)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        df = ak.stock_margin_sse(start_date=start, end_date=end)
        date_col = find_col(df, ["日期"]) or str(df.columns[0])
        bal_col = find_col(df, ["融资融券余额"], ["rzrqye"], ["余额"])
        if not bal_col:
            raise RuntimeError(f"未匹配到余额列；列={list(df.columns)}")
        df = df.copy()
        df["_ymd"] = df[date_col].astype(str).str.replace(r"\D", "", regex=True)
        df = df.sort_values("_ymd")
        ser = pd.to_numeric(df[bal_col], errors="coerce").dropna().tolist()
        if len(ser) < 21:
            raise RuntimeError("交易日不足 21 天")
        latest, ref = ser[-1], ser[-21]
        pct = (latest - ref) / ref * 100 if ref else 0.0
        out["now"] = pct > MARGIN_SURGE_PCT
        out["pct"] = round(pct, 1)
        out["latest"] = round(latest / 1e8, 0)  # 亿元
        out["detail"] = f"沪市两融近20交易日 {pct:+.1f}%（阈值>{MARGIN_SURGE_PCT}%）"
        ok(f"两融趋势：{out['detail']}")
    except Exception as e:
        warn(f"#2 两融取数失败：{e}")
    return out


ETF_HIST_PATH = HERE / "etf_share_history.json"

def fetch_etf_redemption() -> dict:
    """#3 宽基 ETF 持续净赎回：龙头宽基 ETF 合计份额近 N 个采样点净变化 < 0。

    akshare 没有 ETF 份额【历史序列】接口，只有当日快照 fund_etf_spot_em()
    的「最新份额」列。因此本函数每次运行【抓当日合计份额并追加到
    etf_share_history.json】，再与约 ETF_WINDOW 个采样点之前对比。
    → 由 GitHub Actions 按日/周持续跑来积累历史；首次运行只播种、无法判定。
    （注意：采样点是"运行次"，非严格交易日；按周跑则窗口对应约 N 周。）
    """
    out = {"now": False, "detail": "未取到"}
    if ak is None:
        return out
    try:
        df = ak.fund_etf_spot_em()
        code_col = find_col(df, ["代码"]) or "代码"
        share_col = find_col(df, ["最新份额"], ["份额"])
        date_col = find_col(df, ["数据日期"], ["日期"])
        if not share_col:
            raise RuntimeError(f"未匹配到份额列；列={list(df.columns)}")
        sub = df[df[code_col].astype(str).isin(BROAD_ETFS)]
        # 最新份额原始单位为「份」，换算成「亿份」便于阅读（判定只看变化符号，不受影响）
        total = float(pd.to_numeric(sub[share_col], errors="coerce").dropna().sum()) / 1e8
        if total <= 0:
            raise RuntimeError("龙头 ETF 份额合计为 0（代码列表或列名需核对）")
        date = str(sub[date_col].iloc[0]) if date_col and len(sub) else dt.date.today().isoformat()

        # 读历史 → 按日期去重追加 → 截断保留最近 60 个点
        hist = []
        if ETF_HIST_PATH.exists():
            try:
                hist = json.loads(ETF_HIST_PATH.read_text(encoding="utf-8"))
            except Exception:
                hist = []
        hist = [h for h in hist if h.get("date") != date]
        hist.append({"date": date, "total": round(total, 2)})
        hist.sort(key=lambda h: h["date"])
        hist = hist[-60:]
        ETF_HIST_PATH.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")

        if len(hist) < 2:
            out["detail"] = f"已播种份额历史（合计 {total:.1f}亿份），需多次运行后才能判定净赎回"
            warn(f"#3 ETF份额：{out['detail']}")
            return out
        ref = hist[-min(ETF_WINDOW + 1, len(hist))]
        change = hist[-1]["total"] - ref["total"]
        out["now"] = change < 0
        out["detail"] = (f"{len(BROAD_ETFS)} 只龙头ETF 合计份额 "
                         f"{ref['date']}→{hist[-1]['date']} 净变化 {change:+.2f}亿份")
        ok(f"ETF 份额：{out['detail']}")
    except Exception as e:
        warn(f"#3 ETF份额取数失败（保持未触发）：{e}")
    return out


def fetch_money_tightening(data: dict) -> dict:
    """#4 货币转紧：M2 同比连续 2 个月下行（政策利率上调难自动，略）。"""
    out = {"now": False, "detail": "未取到"}
    try:
        s2 = snap(data, "m2")
        ser = s2["series"] if s2 else []
        if len(ser) >= 3:
            down = ser[-1] < ser[-2] < ser[-3]
            out["now"] = bool(down)
            out["detail"] = f"M2 近三月 {ser[-3]}→{ser[-2]}→{ser[-1]}%（连降2月={down}）"
            ok(f"货币松紧：{out['detail']}")
    except Exception as e:
        warn(f"#4 货币转紧判断失败：{e}")
    return out


def fetch_ipo_surge() -> dict:
    """#5 IPO 提速：当月 IPO 募资额环比 > 50%。
    ⚠️ akshare 月度募资额接口随版本而异，best-effort；失败则不触发并告警。"""
    out = {"now": False, "detail": "未取到"}
    if ak is None:
        return out
    try:
        df = None
        for fn in ("stock_ipo_summary_cninfo", "stock_zh_a_new"):
            if hasattr(ak, fn):
                try:
                    df = getattr(ak, fn)()
                    break
                except Exception:
                    continue
        if df is None:
            raise RuntimeError("无可用 IPO 接口（接口名需按版本核对）")
        # 募资额列：尝试常见命名
        amt_col = find_col(df, ["募资"], ["募集"], ["实际募集"])
        date_col = find_col(df, ["上市日期"], ["日期"], ["发行日期"])
        if not (amt_col and date_col):
            raise RuntimeError(f"未匹配到募资额/日期列；列={list(df.columns)}")
        df = df.copy()
        df["_ym"] = df[date_col].astype(str).str.replace(r"\D", "", regex=True).str[:6]
        df["_amt"] = pd.to_numeric(df[amt_col], errors="coerce")
        g = df.dropna(subset=["_amt"]).groupby("_ym")["_amt"].sum().sort_index()
        if len(g) < 2:
            raise RuntimeError("可比月份不足")
        cur, prev = g.iloc[-1], g.iloc[-2]
        mom = (cur - prev) / prev * 100 if prev else 0.0
        out["now"] = mom > IPO_MOM_PCT
        out["detail"] = f"IPO 募资额环比 {mom:+.0f}%（阈值>{IPO_MOM_PCT}%）"
        ok(f"IPO 节奏：{out['detail']}")
    except Exception as e:
        warn(f"#5 IPO 取数失败（保持未触发）：{e}")
    return out


def fetch_team_cut() -> dict:
    """#6 汇金季报减持：季度滞后，需季报数据。保持未触发并告警。"""
    warn("#6 汇金季报减持：季度滞后，未自动判定（按季报人工核对）。")
    return {"now": False, "detail": "季报滞后，人工核对"}


def _yearly_value_series(df, ndigits=2):
    """金十系 macro_china_*_yearly 返回的时间序列：取 (日期, 今值) 按时间升序。"""
    date_col = find_col(df, ["日期"]) or str(df.columns[0])
    val_col = find_col(df, ["今值"], ["close"], ["value"])
    if not val_col:
        # 退而取最后一个数值列
        num = [c for c in df.columns if c != date_col]
        val_col = num[-1] if num else None
    if not val_col:
        return []
    tmp = df[[date_col, val_col]].copy()
    tmp[val_col] = pd.to_numeric(tmp[val_col], errors="coerce")
    tmp = tmp.dropna()
    tmp["_d"] = tmp[date_col].astype(str)
    tmp = tmp.sort_values("_d")
    return [round(float(v), ndigits) for v in tmp[val_col].tolist()]


def fetch_deflation_end() -> dict:
    """#7 通缩结束：CPI 同比 > 0 且 PPI 同比 > 0，连续 3 个月。"""
    out = {"now": False, "detail": "未取到"}
    if ak is None:
        return out
    try:
        cpi = _yearly_value_series(ak.macro_china_cpi_yearly())
        ppi = _yearly_value_series(ak.macro_china_ppi_yearly())
        if len(cpi) < 3 or len(ppi) < 3:
            raise RuntimeError("CPI/PPI 月份不足 3")
        cpi3, ppi3 = cpi[-3:], ppi[-3:]
        cond = all(x > 0 for x in cpi3) and all(x > 0 for x in ppi3)
        out["now"] = bool(cond)
        out["cpi3"], out["ppi3"] = cpi3, ppi3
        out["detail"] = f"CPI近3月{cpi3}、PPI近3月{ppi3}（均>0={cond}）"
        ok(f"通胀：{out['detail']}")
    except Exception as e:
        warn(f"#7 CPI/PPI 取数失败：{e}")
    return out


# =====================================================================
#  自动判定：checklist[].now 与 assessment（§5）
# =====================================================================
def compute_assessment(data: dict, signals: list[dict]):
    """signals 按 checklist 顺序（7 条）给出 {'now':bool,'detail':str}。"""
    cl = data.get("checklist", [])
    for i, sig in enumerate(signals):
        if i < len(cl):
            cl[i]["now"] = bool(sig.get("now"))

    n = sum(1 for c in cl if c.get("now"))

    if n <= 2:
        summary = "仍在蓄水 · 水位偏高"
    elif n <= 4:
        summary = "攻守转换中"
    else:
        summary = "退潮信号密集"

    # rationale：按当前读数模板化生成
    s2, s1, sc = snap(data, "m2"), snap(data, "m1"), snap(data, "scissor")
    tsf, team = snap(data, "tsf"), snap(data, "team")
    rationale = []
    if s2:
        trend = "走高" if len(s2["series"]) > 1 and s2["series"][-1] >= s2["series"][0] else "回落"
        rationale.append(f"M2 同比 {s2['value']}%（近12月{trend}）→ 放水节奏的总水位")
    if s1:
        rationale.append(f"M1 同比 {s1['value']}% → 活钱活跃度（M1 领先，回升=资金在动）")
    if sc:
        rationale.append(f"M2–M1 剪刀差 {sc['value']}pct → 收窄=资金活化、循环加快")
    if tsf:
        rationale.append(f"社融存量 {tsf['value']}万亿（{tsf.get('note','')}）→ 实体输血力度")
    if team:
        rationale.append(f"国家队 ETF 持仓 {team['value']}万亿 → 托底仓位（季报滞后确认）")

    # watch：列出尚未触发、最该盯的领先信号
    lead_names = []
    for i, c in enumerate(cl):
        if not c.get("now") and "领先" in c.get("idx", ""):
            # 用 ct 去标签化做个短名
            short = re.sub(r"<[^>]+>", "", c.get("ct", "")).strip()
            lead_names.append(short)
    watch = (f"{n} 项退潮信号触发（共 {len(cl)} 项）。"
             + ("最该盯的领先信号：" + "；".join(lead_names[:3]) if lead_names else "暂无未触发的领先信号。"))

    asof = data.get("meta", {}).get("updated", "")
    data["assessment"] = {
        "asOf": asof,
        "summary": summary,
        "rationale": rationale or data.get("assessment", {}).get("rationale", []),
        "watch": watch,
    }
    ok(f"自动判定：{summary}（触发 {n}/{len(cl)}）")


# =====================================================================
#  六组核心指标·逐行当前方向（§4 的 row.current：蓄水/中性/退潮）
# =====================================================================
def set_row_current(data: dict, letter: str, name_kw: str, value: str | None):
    """给某组(letter)里 name 含 name_kw 的行写 current；value=None 表示不判定(留空)。"""
    for g in data.get("groups", []):
        if g.get("letter") != letter:
            continue
        for r in g.get("rows", []):
            if name_kw in r.get("name", ""):
                if value:
                    r["current"] = value
                else:
                    r.pop("current", None)


def fetch_valuation_dir():
    """C 组·大盘估值：用全 A 中位 PE 的近10年历史分位。高分位=过热=退潮。"""
    if ak is None:
        return None
    try:
        df = ak.stock_a_ttm_lyr()
        qcol = find_col(df, ["quantileInRecent10YearsMiddlePeTtm"])
        pecol = find_col(df, ["middlePETTM"])
        q = pd.to_numeric(df[qcol], errors="coerce").dropna()
        pe = pd.to_numeric(df[pecol], errors="coerce").dropna()
        if not len(q):
            raise RuntimeError("无分位数据")
        qv = float(q.iloc[-1]); qv = qv / 100 if qv > 1 else qv
        pev = float(pe.iloc[-1]) if len(pe) else float("nan")
        cur = "退潮" if qv > 0.8 else ("蓄水" if qv < 0.5 else "中性")
        ok(f"C组估值：全A PE {pev:.1f}、近10年 {qv*100:.0f}% 分位 → {cur}")
        return cur
    except Exception as e:
        warn(f"C组估值取数失败：{str(e)[:90]}")
        return None


def fetch_fx_dir():
    """F 组·人民币汇率：USD/CNY 近 ~20 交易日趋势。
    F 组语义相反：人民币偏弱(需稳)=蓄水侧；走强(理由淡化)=退潮侧。"""
    if ak is None:
        return None
    try:
        today = dt.date.today()
        start = (today - dt.timedelta(days=45)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        df = ak.currency_boc_sina(symbol="美元", start_date=start, end_date=end)
        col = find_col(df, ["中行汇买价"], ["折算价"], ["中间价"])
        s = pd.to_numeric(df[col], errors="coerce").dropna().tolist()
        if len(s) < 6:
            raise RuntimeError("汇率样本不足")
        latest, ref = s[-1], s[-min(20, len(s))]
        chg = (latest - ref) / ref * 100  # >0 = 人民币贬值(偏弱)
        cur = "蓄水" if chg > 0.3 else ("退潮" if chg < -0.3 else "中性")
        ok(f"F组汇率：USD/CNY 近期 {chg:+.2f}%（>0=人民币偏弱）→ {cur}")
        return cur
    except Exception as e:
        warn(f"F组汇率取数失败：{str(e)[:90]}")
        return None


def _monthly_sum_dir(df, date_col_kws, amt_expr, hi=1.5, lo=0.7):
    """通用：按月汇总金额，比较最新月 vs 上一月，返回方向。amt_expr 已是数值 Series。"""
    dcol = find_col(df, date_col_kws)
    if not dcol:
        raise RuntimeError(f"未找到日期列；列={list(df.columns)}")
    g = df.assign(_ym=df[dcol].astype(str).str.replace(r"\D", "", regex=True).str[:6],
                  _amt=amt_expr)
    g = g.dropna(subset=["_amt"])
    gg = g[g["_ym"].str.len() == 6].groupby("_ym")["_amt"].sum().sort_index()
    if len(gg) < 2:
        raise RuntimeError("可比月份不足")
    cur_m, prev_m = float(gg.iloc[-1]), float(gg.iloc[-2])
    ratio = cur_m / prev_m if prev_m else 1.0
    cur = "退潮" if ratio > hi else ("蓄水" if ratio < lo else "中性")
    return cur, ratio, gg.index[-1]


def fetch_refin_dir():
    """D 组·再融资(定增)：月度募资额(发行总数×价格)环比。放量=抽水=退潮。"""
    if ak is None:
        return None
    try:
        df = ak.stock_qbzf_em()
        ncol = find_col(df, ["发行总数"]); pcol = find_col(df, ["发行价格"])
        amt = pd.to_numeric(df[ncol], errors="coerce") * pd.to_numeric(df[pcol], errors="coerce")
        cur, ratio, ym = _monthly_sum_dir(df, ["发行日期"], amt)
        ok(f"D组再融资：定增募资 {ym} 环比 ×{ratio:.1f} → {cur}")
        return cur
    except Exception as e:
        warn(f"D组再融资取数失败：{str(e)[:90]}")
        return None


def fetch_unlock_dir():
    """D 组·解禁/减持：近月解禁市值 vs 前几月均值。放量=供给压力=退潮。"""
    if ak is None:
        return None
    try:
        today = dt.date.today()
        start = (today - dt.timedelta(days=150)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        df = ak.stock_restricted_release_summary_em(start_date=start, end_date=end)
        vcol = find_col(df, ["实际解禁市值"], ["解禁市值"])
        tcol = find_col(df, ["解禁时间"])
        g = df.assign(_ym=df[tcol].astype(str).str.replace(r"\D", "", regex=True).str[:6],
                      _v=pd.to_numeric(df[vcol], errors="coerce")).dropna(subset=["_v"])
        gg = g.groupby("_ym")["_v"].sum().sort_index()
        if len(gg) < 2:
            raise RuntimeError("可比月份不足")
        latest, avg = float(gg.iloc[-1]), float(gg.iloc[:-1].mean())
        cur = "退潮" if latest > avg * 1.3 else ("蓄水" if latest < avg * 0.7 else "中性")
        ok(f"D组解禁：近月解禁市值/均值 ×{(latest/avg):.1f} → {cur}")
        return cur
    except Exception as e:
        warn(f"D组解禁取数失败：{str(e)[:90]}")
        return None


def update_group_current(data: dict, signals: list[dict]):
    """只给【有真实数据支撑】的行打方向，其余行留空（前端不显示）。
    注意 F 组语义相反：『蓄水』侧=病未愈仍需托底（au=⚠），故通缩=蓄水、通胀回正=退潮。"""
    n_set = 0

    # A 组 · 货币与流动性
    s2, sc = snap(data, "m2"), snap(data, "scissor")
    if s2 and s2.get("series"):
        m2 = s2["value"]
        cur = "蓄水" if m2 >= 8 else ("退潮" if m2 < 7 else "中性")
        set_row_current(data, "A", "M2 / M1", cur); n_set += 1
    if sc and len(sc.get("series", [])) >= 2:
        s = sc["series"]
        cur = "蓄水" if s[-1] < s[0] else ("退潮" if s[-1] > s[0] else "中性")
        set_row_current(data, "A", "剪刀差", cur); n_set += 1

    # C 组 · 资金面与情绪（两融）：温和上升=蓄水(健康)，急升过热=退潮
    margin = signals[1] if len(signals) > 1 else {}
    if "pct" in margin:
        p = margin["pct"]
        cur = "退潮" if p > MARGIN_SURGE_PCT else ("蓄水" if p >= 0 else "中性")
        set_row_current(data, "C", "两融", cur); n_set += 1

    # C 组 · 大盘 PE/PB 估值（近10年分位）
    cval = fetch_valuation_dir()
    if cval:
        set_row_current(data, "C", "PE/PB", cval); n_set += 1

    # F 组 · 基本面（CPI/PPI）：仍通缩=病未愈=蓄水侧；双双回正=理由淡化=退潮侧
    defl = signals[6] if len(signals) > 6 else {}
    if "cpi3" in defl and "ppi3" in defl:
        both_pos = all(x > 0 for x in defl["cpi3"]) and all(x > 0 for x in defl["ppi3"])
        set_row_current(data, "F", "CPI / PPI", "退潮" if both_pos else "蓄水"); n_set += 1

    # F 组 · 人民币汇率
    fval = fetch_fx_dir()
    if fval:
        set_row_current(data, "F", "人民币汇率", fval); n_set += 1

    # D 组 · 供给端（再融资、解禁）
    rval = fetch_refin_dir()
    if rval:
        set_row_current(data, "D", "再融资", rval); n_set += 1
    uval = fetch_unlock_dir()
    if uval:
        set_row_current(data, "D", "解禁", uval); n_set += 1

    # B 组 · 中央汇金持仓（用 team 季报序列趋势：增持/维持=蓄水，减持=退潮）
    team = snap(data, "team")
    if team and len(team.get("series", [])) >= 2:
        s = team["series"]
        set_row_current(data, "B", "中央汇金", "蓄水" if s[-1] >= s[-2] else "退潮"); n_set += 1

    ok(f"六组方向：已标注 {n_set} 行 current（其余无数据行留空）")


# =====================================================================
#  meta 与可选的 HTML 兜底同步
# =====================================================================
def update_meta(data: dict, latest_ym: tuple[int, int] | None):
    meta = data.setdefault("meta", {})
    if latest_ym:
        y, m = latest_ym
        meta["updated"] = f"{y:04d}-{m:02d}"
        meta["snapshotLabel"] = f"SNAPSHOT · {y}年{m}月"
    today = dt.date.today().isoformat()
    meta["dataNote"] = (f"由 pull.py 于 {today} 自动更新；社融存量/国家队持仓为央行/季报口径，"
                        f"如接口缺失则保留人工值。其余为 akshare 实时取数。")
    ok(f"meta.updated={meta.get('updated')}")


def sync_html_fallback(data: dict):
    """可选：把最新数据同步进 index.html 的 <script id='fallback-data'> 兜底块，
    使 file:// 直接打开也能看到最新数（HTTP 部署时以外部 data.json 为准）。"""
    if not HTML_PATH.exists():
        return
    try:
        html = HTML_PATH.read_text(encoding="utf-8")
        pat = re.compile(
            r'(<script type="application/json" id="fallback-data">)(.*?)(</script>)',
            re.S,
        )
        if not pat.search(html):
            warn("HTML 未找到 fallback-data 块，跳过同步")
            return
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        html2 = pat.sub(lambda m: m.group(1) + payload + m.group(3), html, count=1)
        if html2 != html:
            HTML_PATH.write_text(html2, encoding="utf-8")
            ok("已同步 index.html 内置兜底数据")
    except Exception as e:
        warn(f"同步 HTML 兜底数据失败（不影响 data.json）：{e}")


# =====================================================================
#  主流程
# =====================================================================
def main():
    print("=" * 64)
    print("A股「蓄水池」取数 pull.py 开始")
    print("=" * 64)

    data = load_data()

    # --- snapshot ---
    latest_ym = update_money_supply(data)   # m2 / m1
    update_scissor(data)                     # 派生 m2 - m1
    update_tsf(data)                         # 社融存量（保留旧值）
    update_team(data)                        # 国家队持仓（保留旧值）

    # --- checklist 信号（顺序须对应 §5 的 7 条）---
    signals = [
        {"now": False, "detail": "政策口风（NLP 选做/人工）"},  # 1 口风转防过热
        fetch_margin_surge(),                                    # 2 两融过热
        fetch_etf_redemption(),                                  # 3 ETF 净赎回
        fetch_money_tightening(data),                            # 4 货币转紧
        fetch_ipo_surge(),                                       # 5 IPO 提速
        fetch_team_cut(),                                        # 6 汇金减持（季报）
        fetch_deflation_end(),                                   # 7 通缩结束
    ]

    # --- meta（必须在 assessment 前，asOf 取 meta.updated）---
    update_meta(data, latest_ym)

    # --- 六组核心指标·逐行当前方向 ---
    update_group_current(data, signals)

    # --- 自动判定 ---
    compute_assessment(data, signals)

    # --- 写回 ---
    save_data(data)
    ok(f"已写回 {DATA_PATH}")

    # --- 可选：同步 HTML 兜底 ---
    sync_html_fallback(data)

    print("-" * 64)
    print("运行摘要：")
    for line in REPORT:
        print(line)
    print("-" * 64)
    print("完成。")


if __name__ == "__main__":
    sys.exit(main())
