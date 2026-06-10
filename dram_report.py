#!/usr/bin/env python3
"""
DRAM ETF 每日收盘播报脚本 v3 - SCF 云函数版
- 使用 yfinance 获取 Roundhill Memory ETF (DRAM) 数据（纯 Python，无 Node 依赖）
- 做技术分析（MA/RSI/成交量）
- 给出买入/卖出/持有建议
- 推送到企业微信群 Webhook
- 兼容本地运行 和 腾讯云函数 SCF 入口

部署到 SCF：
  1. 函数入口：handler(event, context)
  2. 运行时：Python 3.9+
  3. 依赖：yfinance, pandas, numpy（通过层或在线安装）
  4. 触发方式：定时触发器（cron）
"""

import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from collections import namedtuple

import yfinance as yf
import numpy as np

# ── 配置 ─────────────────────────────────────────────
WEBHOOK_KEY = os.environ.get("WECOM_KEY", "74823155-4f41-4116-acee-405a8e839156")
TICKER = "DRAM"          # Yahoo Finance 美股代码
LOOKBACK_DAYS = 90       # 拉取历史天数
# ────────────────────────────────────────────────────────

DayData = namedtuple("DayData", ["date", "open", "close", "high", "low", "volume"])


def fetch_kline(ticker: str, days: int = 90) -> list:
    """使用 yfinance 拉取日K线，返回 DayData 列表（日期升序）"""
    end = datetime.today()
    start = end - timedelta(days=days + 30)   # 多拉一些，确保足够历史
    ticker_obj = yf.Ticker(ticker)
    df = ticker_obj.history(start=start.strftime("%Y-%m-%d"),
                           end=end.strftime("%Y-%m-%d"),
                           interval="1d", auto_adjust=True)
    if df is None or df.empty:
        raise RuntimeError(f"yfinance 未返回数据，代码={ticker}")

    rows = []
    for idx, row in df.iterrows():
        date_str = idx.strftime("%Y-%m-%d")
        rows.append(DayData(
            date=date_str,
            open=round(float(row["Open"]), 2),
            close=round(float(row["Close"]), 2),
            high=round(float(row["High"]), 2),
            low=round(float(row["Low"]), 2),
            volume=int(row["Volume"]),
        ))
    return rows   # 已经是日期升序


def calc_ma(rows: list, window: int) -> float:
    if len(rows) < window:
        return None
    return round(sum(r.close for r in rows[-window:]) / window, 2)


def calc_rsi(rows: list, period: int = 14) -> float:
    if len(rows) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(rows)):
        delta = rows[i].close - rows[i - 1].close
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def send_wecom(content: str):
    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WEBHOOK_KEY}"
    payload = json.dumps({"msgtype": "text", "text": {"content": content}},
                        ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("errcode") != 0:
                print(f"⚠️ 推送失败: {result}", file=sys.stderr)
            else:
                print("✅ 推送成功")
    except Exception as e:
        print(f"⚠️ 推送异常: {e}", file=sys.stderr)


def analyze(rows: list) -> dict:
    """综合技术分析，返回建议"""
    latest = rows[-1]
    prev = rows[-2]
    change = latest.close - prev.close
    change_pct = round(change / prev.close * 100, 2)

    ma5 = calc_ma(rows, 5)
    ma20 = calc_ma(rows, 20)
    ma60 = calc_ma(rows, 60)
    rsi = calc_rsi(rows, 14)

    vol_recent = [r.volume for r in rows[-5:]]
    avg_vol_5d = int(sum(vol_recent) / len(vol_recent))
    vol_ratio = round(latest.volume / avg_vol_5d * 100, 1) if avg_vol_5d > 0 else 100

    all_high = max(r.high for r in rows)
    all_low = min(r.low for r in rows)
    position = round((latest.close - all_low) / (all_high - all_low) * 100, 1) \
        if all_high > all_low else 50

    signals = []
    score = 0

    # 1. 均线趋势
    if ma5 and ma20:
        if latest.close > ma5 > ma20:
            signals.append("📈 价格 > MA5 > MA20，多头排列，趋势向上")
            score += 2
        elif latest.close < ma5 < ma20:
            signals.append("📉 价格 < MA5 < MA20，空头排列，趋势向下")
            score -= 2
        else:
            signals.append(f"➡️ 价格 ${latest.close} 夹在 MA5(${ma5})/MA20({ma20}) 之间，震荡")

    # 2. MA金叉/死叉
    if ma5 and ma20:
        if ma5 > ma20:
            signals.append("🔹 MA5 > MA20（金叉区域），偏多")
            score += 1
        else:
            signals.append("🔸 MA5 < MA20（死叉区域），偏空")
            score -= 1

    # 3. RSI
    if rsi:
        if rsi < 30:
            signals.append(f"🟢 RSI={rsi} 超卖区，反弹概率高")
            score += 2
        elif rsi > 70:
            signals.append(f"🔴 RSI={rsi} 超买区，注意回调风险")
            score -= 2
        else:
            signals.append(f"⚪ RSI={rsi}，中性区间")

    # 4. 成交量
    if vol_ratio > 150:
        if change_pct > 0:
            signals.append(f"💰 成交量放大至 {vol_ratio}%，价涨量增，资金流入")
            score += 1
        else:
            signals.append(f"🚨 成交量放大至 {vol_ratio}%，价跌量增，资金流出！注意风险")
            score -= 2
    elif vol_ratio < 70:
        signals.append(f"😴 成交量萎缩至 {vol_ratio}%，市场观望")
    else:
        signals.append(f"📊 成交量 {vol_ratio}%，正常水平")

    # 5. 区间位置
    signals.append(f"📍 当前价位于区间 {position}%（低=${round(all_low, 2)}，高=${round(all_high, 2)}）")
    if position > 80:
        signals.append("⚠️ 接近区间高点，注意高位风险")
        score -= 1
    elif position < 20:
        signals.append("✅ 接近区间低点，具备低吸价值")
        score += 1

    # 综合建议
    if score >= 2:
        recommendation = "🔵 综合建议：持有 / 适量买入"
        reason = "多项指标偏多，趋势向上"
    elif score <= -2:
        recommendation = "🔴 综合建议：考虑减仓 / 卖出"
        reason = "多项指标偏空，注意风险"
    else:
        recommendation = "⚪ 综合建议：观望 / 持有现有仓位"
        reason = "多空信号混杂，建议观望"

    return {
        "latest": latest,
        "prev": prev,
        "change": change,
        "change_pct": change_pct,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "rsi": rsi,
        "volume": latest.volume,
        "avg_vol_5d": avg_vol_5d,
        "vol_ratio": vol_ratio,
        "week52_high": round(all_high, 2),
        "week52_low": round(all_low, 2),
        "position": position,
        "signals": signals,
        "score": score,
        "recommendation": recommendation,
        "reason": reason,
    }


def format_msg(d: dict) -> str:
    arrow = "🔺" if d["change"] >= 0 else "🔻"
    sign = "+" if d["change"] >= 0 else ""
    latest = d["latest"]

    lines = []
    lines.append("📊 DRAM ETF 收盘播报")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"📅 {latest.date} 美股收盘")
    lines.append("")
    lines.append(f"💰 收盘价    ${d['latest'].close}")
    lines.append(f"{arrow} {sign}{round(d['change'], 2)} ({sign}{d['change_pct']}%)")
    lines.append(f"📂 开盘      ${d['latest'].open}")
    lines.append(f"📈 最高      ${d['latest'].high}")
    lines.append(f"📉 最低      ${d['latest'].low}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 成交量    {d['volume']:,} 股")
    lines.append(f"   (5日均量 {d['avg_vol_5d']:,}，今日 {d['vol_ratio']}%)")
    lines.append("")
    if d["ma5"]:
        lines.append(f"📏 MA5   ${d['ma5']}")
    if d["ma20"]:
        lines.append(f"📏 MA20  ${d['ma20']}")
    if d["ma60"]:
        lines.append(f"📏 MA60  ${d['ma60']}")
    if d["rsi"]:
        lines.append(f"⚡ RSI   {d['rsi']}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("🔍 技术分析：")
    for sig in d["signals"]:
        lines.append(f"  {sig}")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"{d['recommendation']}")
    lines.append(f"💡 {d['reason']}")
    lines.append("")
    lines.append("⚠️ 以上为技术面参考，不构成投资建议")

    return "\n".join(lines)


def run_report():
    """主逻辑：拉取数据 → 分析 → 推送"""
    print(f"[{datetime.now()}] 开始获取 DRAM ETF 数据...")
    rows = fetch_kline(TICKER, LOOKBACK_DAYS)
    if not rows:
        raise RuntimeError("未获取到K线数据")

    print(f"✅ 获取到 {len(rows)} 条K线数据，最新：{rows[-1].date} 收盘 ${rows[-1].close}")

    if len(rows) < 20:
        msg = f"⚠️ DRAM ETF 数据不足（仅{len(rows)}条），技术指标可能不准确。\n最新收盘：${rows[-1].close}"
        send_wecom(msg)
        return msg

    analysis = analyze(rows)
    message = format_msg(analysis)

    print("── 播报内容 ────────────────")
    print(message)
    print("─────────────────────────────")

    send_wecom(message)
    print(f"[{datetime.now()}] 完成")
    return message


# ── 本地运行入口 ───────────────────────────────────────
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    run_report()


# ── 腾讯云函数 SCF 入口 ───────────────────────────────
def handler(event, context):
    """
    SCF 函数入口
    event: 触发器传入的参数（定时触发时为空 dict）
    context: 运行时上下文
    """
    try:
        result = run_report()
        return {"status": "ok", "message": result[:200]}
    except Exception as e:
        err_msg = f"⚠️ DRAM ETF 播报失败\n\n原因：{str(e)}"
        print(err_msg, file=sys.stderr)
        try:
            send_wecom(err_msg[:4000])
        except Exception:
            pass
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    main()
