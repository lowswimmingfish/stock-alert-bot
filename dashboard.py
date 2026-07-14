#!/usr/bin/env python3
"""포트폴리오 웹 대시보드 (FastAPI).

- GET /              : 대시보드 페이지
- GET /api/overview  : 스냅샷 시계열 + 현재 보유/현금 JSON
DASHBOARD_PASSWORD 환경변수가 있으면 HTTP Basic 인증 (아이디는 아무거나).
"""

import os
import secrets
import logging
from datetime import date, timedelta

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import kis_api
from utils import get_usdkrw
from portfolio_tracker import _load_snapshots, _cost_krw

logger = logging.getLogger(__name__)

app = FastAPI(title="Stock Dashboard")
_basic = HTTPBasic(auto_error=False)


def _auth(credentials: HTTPBasicCredentials = Depends(_basic)):
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        return  # 로컬 개발: 인증 없음
    if credentials is None or not secrets.compare_digest(
        credentials.password.encode(), password.encode()
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


@app.get("/api/overview")
def api_overview(days: int = Query(default=90, ge=1, le=730), _=Depends(_auth)):
    snaps = _load_snapshots()
    if not snaps:
        return JSONResponse({"dates": [], "holdings": []})

    cutoff = str(date.today() - timedelta(days=days))
    filtered = sorted((d, s) for d, s in snaps.items() if d >= cutoff)
    if not filtered:
        filtered = sorted(snaps.items())[-1:]

    # 수익률: 매입 원가(첫 스냅샷 환율로 고정) 대비 — 텔레그램 성과차트와 동일 기준
    ref_fx = filtered[0][1].get("fx_rate", 1400)
    dates, stock_krw, return_pct = [], [], []
    for d, s in filtered:
        cost = _cost_krw(s, ref_fx=ref_fx)
        dates.append(d)
        stock_krw.append(s.get("total_krw", 0))
        return_pct.append(round((s["total_krw"] / cost - 1) * 100, 2) if cost > 0 else 0.0)

    # 현재 상태 (KIS 5분 캐시, 실패 시 마지막 스냅샷)
    fx = get_usdkrw()
    latest = filtered[-1][1]
    holdings, cash_krw, cash_usd = [], 0, 0.0
    try:
        if kis_api.is_configured():
            import time
            kr = kis_api.get_kr_balance_raw()
            if not kr.get("holdings") and not kr.get("total"):
                time.sleep(1)  # KIS 초당 요청 한도 초과 시 1회 재시도
                kr = kis_api.get_kr_balance_raw()
            us = kis_api.get_us_balance_raw()
            cash_krw = kr.get("total", {}).get("cash", 0)
            cash_usd = kis_api.get_us_cash()
            for h in kr.get("holdings", []):
                holdings.append({
                    "name": h["name"], "ticker": h["ticker"], "qty": h["qty"],
                    "price": h["curr_price"], "avg_price": h["avg_price"],
                    "value_krw": h.get("eval_amt", 0),
                    "profit_krw": h["profit"], "profit_pct": h["profit_pct"],
                    "market": "KR",
                })
            for h in us.get("holdings", []):
                value_krw = round(h["curr_price"] * h["qty"] * fx["rate"])
                holdings.append({
                    "name": h["ticker"], "ticker": h["ticker"], "qty": h["qty"],
                    "price": h["curr_price"], "avg_price": h["avg_price"],
                    "value_krw": value_krw,
                    "profit_krw": round(h["profit"] * fx["rate"]),
                    "profit_pct": h["profit_pct"],
                    "market": "US",
                })
    except Exception as e:
        logger.warning(f"Dashboard live holdings error: {e}")
    if not holdings:  # KIS 실패 시 마지막 스냅샷 기준
        for tkr, h in latest.get("holdings", {}).items():
            v = h.get("value_krw") or round(h.get("value_usd", 0) * fx["rate"])
            holdings.append({
                "name": tkr, "ticker": tkr, "qty": h["qty"],
                "price": h["price"], "avg_price": h["avg_price"],
                "value_krw": v, "profit_krw": None, "profit_pct": None,
                "market": "KR" if "value_krw" in h else "US",
            })
        cash_krw = latest.get("cash_krw", 0)
        cash_usd = latest.get("cash_usd", 0.0)

    stock_total = sum(h["value_krw"] for h in holdings)
    cash_total = round(cash_krw + cash_usd * fx["rate"])
    return {
        "dates": dates,
        "stock_krw": stock_krw,
        "return_pct": return_pct,
        "fx_rate": fx["rate"],
        "stock_total_krw": stock_total,
        "cash_krw": cash_krw,
        "cash_usd": cash_usd,
        "cash_total_krw": cash_total,
        "asset_total_krw": stock_total + cash_total,
        "current_return_pct": return_pct[-1] if return_pct else 0.0,
        "holdings": sorted(holdings, key=lambda h: -h["value_krw"]),
    }


_PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>📊 내 포트폴리오</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
:root {
  --surface-1: #fcfcfb; --page: #f9f9f7;
  --ink-1: #0b0b0b; --ink-2: #52514e; --ink-muted: #898781;
  --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --up: #006300; --down: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface-1: #1a1a19; --page: #0d0d0d;
    --ink-1: #ffffff; --ink-2: #c3c2b7; --ink-muted: #898781;
    --grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --up: #0ca30c; --down: #d03b3b;
  }
}
* { box-sizing: border-box; margin: 0; }
body {
  background: var(--page); color: var(--ink-1);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 20px; max-width: 960px; margin: 0 auto;
}
h1 { font-size: 1.15rem; margin-bottom: 16px; }
h1 small { color: var(--ink-muted); font-weight: 400; font-size: .8rem; margin-left: 8px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 18px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile .k { color: var(--ink-2); font-size: .75rem; margin-bottom: 4px; }
.tile .v { font-size: 1.25rem; font-weight: 650; }
.tile .s { font-size: .75rem; color: var(--ink-muted); margin-top: 2px; }
.up { color: var(--up); } .down { color: var(--down); }
.filters { display: flex; gap: 6px; margin-bottom: 10px; }
.filters button {
  background: var(--surface-1); color: var(--ink-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 5px 12px; font-size: .8rem; cursor: pointer;
}
.filters button.on { color: var(--ink-1); font-weight: 650; border-color: var(--series-1); }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px; margin-bottom: 14px; }
.card h2 { font-size: .85rem; color: var(--ink-2); font-weight: 600; margin-bottom: 10px; }
.chart-wrap { position: relative; height: 260px; }
table { width: 100%; border-collapse: collapse; font-size: .85rem; }
th { text-align: right; color: var(--ink-muted); font-weight: 500; padding: 6px 8px; border-bottom: 1px solid var(--grid); }
th:first-child, td:first-child { text-align: left; }
td { text-align: right; padding: 7px 8px; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }
.mkt { display: inline-block; font-size: .68rem; color: var(--ink-muted); border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; margin-right: 6px; }
</style>
</head>
<body>
<h1>📊 내 포트폴리오 <small id="asof"></small></h1>

<div class="tiles">
  <div class="tile"><div class="k">총자산</div><div class="v" id="t-asset">–</div><div class="s" id="t-asset-s"></div></div>
  <div class="tile"><div class="k">주식 평가금</div><div class="v" id="t-stock">–</div></div>
  <div class="tile"><div class="k">보유현금</div><div class="v" id="t-cash">–</div><div class="s" id="t-cash-s"></div></div>
  <div class="tile"><div class="k">수익률 (매입가 기준)</div><div class="v" id="t-ret">–</div><div class="s" id="t-fx"></div></div>
</div>

<div class="filters" id="filters">
  <button data-d="7">7일</button>
  <button data-d="30">30일</button>
  <button data-d="90" class="on">90일</button>
  <button data-d="730">전체</button>
</div>

<div class="card"><h2>주식 평가금 추이 (원)</h2><div class="chart-wrap"><canvas id="c-value"></canvas></div></div>
<div class="card"><h2>수익률 추이 (%, 매입가+환율 고정 기준)</h2><div class="chart-wrap"><canvas id="c-return"></canvas></div></div>

<div class="card"><h2>보유 종목</h2>
<table id="holdings">
<thead><tr><th>종목</th><th>수량</th><th>현재가</th><th>평단</th><th>평가금(원)</th><th>손익</th></tr></thead>
<tbody></tbody>
</table>
</div>

<script>
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const krw = n => n == null ? "–" : Math.round(n).toLocaleString("ko-KR") + "원";
let charts = {};

function lineChart(id, labels, data, fmt) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(document.getElementById(id), {
    type: "line",
    data: { labels, datasets: [{
      data, borderColor: css("--series-1"), borderWidth: 2,
      pointRadius: 0, pointHoverRadius: 5, pointHoverBackgroundColor: css("--series-1"),
      tension: 0.15, fill: false,
    }]},
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: css("--surface-1"), titleColor: css("--ink-2"),
          bodyColor: css("--ink-1"), borderColor: css("--border"), borderWidth: 1,
          displayColors: false, callbacks: { label: ctx => fmt(ctx.parsed.y) },
        },
      },
      scales: {
        x: { grid: { display: false }, border: { color: css("--baseline") },
             ticks: { color: css("--ink-muted"), maxTicksLimit: 6, maxRotation: 0 } },
        y: { grid: { color: css("--grid") }, border: { display: false },
             ticks: { color: css("--ink-muted"), maxTicksLimit: 5, callback: v => fmt(v) } },
      },
    },
  });
}

async function load(days) {
  const r = await fetch("/api/overview?days=" + days);
  const d = await r.json();
  document.getElementById("asof").textContent = d.dates.at(-1) ? d.dates.at(-1) + " 기준" : "";

  document.getElementById("t-asset").textContent = krw(d.asset_total_krw);
  document.getElementById("t-asset-s").textContent = "주식 + 현금";
  document.getElementById("t-stock").textContent = krw(d.stock_total_krw);
  document.getElementById("t-cash").textContent = krw(d.cash_total_krw);
  document.getElementById("t-cash-s").textContent =
    "₩" + Math.round(d.cash_krw).toLocaleString() + " + $" + d.cash_usd.toFixed(2);
  const ret = d.current_return_pct;
  const el = document.getElementById("t-ret");
  el.textContent = (ret >= 0 ? "▲ +" : "▼ ") + ret.toFixed(2) + "%";
  el.className = "v " + (ret >= 0 ? "up" : "down");
  document.getElementById("t-fx").textContent = "USD/KRW " + d.fx_rate.toLocaleString();

  lineChart("c-value", d.dates, d.stock_krw,
    v => (v / 10000).toLocaleString("ko-KR", { maximumFractionDigits: 0 }) + "만원");
  lineChart("c-return", d.dates, d.return_pct, v => (+v).toFixed(2) + "%");

  const tb = document.querySelector("#holdings tbody");
  tb.innerHTML = "";
  for (const h of d.holdings) {
    const us = h.market === "US";
    const p = us ? "$" + h.price.toLocaleString(undefined, {minimumFractionDigits: 2}) : h.price.toLocaleString() + "원";
    const a = us ? "$" + h.avg_price.toLocaleString(undefined, {minimumFractionDigits: 2}) : h.avg_price.toLocaleString() + "원";
    let profit = "–";
    if (h.profit_pct != null) {
      const cls = h.profit_pct >= 0 ? "up" : "down";
      profit = `<span class="${cls}">${h.profit_pct >= 0 ? "+" : ""}${h.profit_pct.toFixed(1)}%<br>` +
               `${h.profit_krw >= 0 ? "+" : ""}${Math.round(h.profit_krw).toLocaleString()}원</span>`;
    }
    tb.insertAdjacentHTML("beforeend",
      `<tr><td><span class="mkt">${h.market}</span>${h.name}</td><td>${h.qty}</td>` +
      `<td>${p}</td><td>${a}</td><td>${h.value_krw.toLocaleString()}</td><td>${profit}</td></tr>`);
  }
}

document.getElementById("filters").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  document.querySelectorAll("#filters button").forEach(x => x.classList.toggle("on", x === b));
  load(+b.dataset.d);
});
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () =>
  load(+document.querySelector("#filters .on").dataset.d));
load(90);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index(_=Depends(_auth)):
    return _PAGE
