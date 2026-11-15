"""
主動式 ETF 成分股追蹤 — 每日盤後更新程式

流程：
  1. 從 FinMind TaiwanStockInfo 找出所有主動式股票 ETF（代號 00xxxA；備援：證交所/櫃買 OpenAPI）
  2. 從 MoneyDJ「全部持股」頁（Basic0007b，每日更新）抓每檔 ETF 的成分股
  3. 統計每檔個股被幾檔主動式 ETF 持有，取前 30 名
  4. 用 FinMind 日收盤（備援：Yahoo Finance）計算 5MA / 20MA / 60MA
       - 多頭排列：5MA > 20MA > 60MA
       - 今日站上月線：今日收盤 > 20MA 且 昨日收盤 <= 昨日 20MA
  5. 輸出 docs/data/latest.json（網頁讀取）與 docs/data/history/日期.json
  6. 若有設定 Telegram，推播「今日站上月線」名單

環境變數（皆可省略）：
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   Telegram 推播
  PAGE_URL                                推播訊息附上的網頁連結
  EXTRA_ETFS                              額外加入的 ETF 代號，逗號分隔，例如 "00999A,00403A"
  EXCLUDE_ETFS                            排除的 ETF 代號，逗號分隔
  FINMIND_TOKEN                           FinMind 免費會員 token（額度 300→600 次/小時，建議設定）
  MIN_WEIGHT                              權重低於此值(%)的零星部位不計入持有，預設 0.01
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

TZ = dt.timezone(dt.timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "docs" / "data"
HIST_DIR = DATA_DIR / "history"
TOP_N = 30
MIN_WEIGHT = float(os.getenv("MIN_WEIGHT") or "0.01")
FINMIND = "https://api.finmindtrade.com/api/v4/data"
STOCK_MARKET: dict[str, str] = {}   # 代號 -> TW / TWO（由 FinMind 股票清單得到）
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
}
ACTIVE_CODE = re.compile(r"^00\d{3,4}A$")          # 主動式「股票型」ETF（D 結尾為債券型，不納入）
STOCK_IN_CELL = re.compile(r"^(.*?)\s*\(\s*([0-9]{4}[0-9A-Z]?)\.(TW|TWO)\s*\)\s*$")
NUM = re.compile(r"-?[\d,]+(?:\.\d+)?")

session = requests.Session()
session.headers.update(UA)


def log(*a):
    print(*a, flush=True)


def get(url: str, retries: int = 3, **kw) -> requests.Response:
    last = None
    for i in range(retries):
        try:
            r = session.get(url, timeout=30, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET 失敗 {url}: {last}")


# ---------------------------------------------------------------- 1. ETF 清單
def finmind(params: dict) -> list[dict]:
    p = dict(params)
    if os.getenv("FINMIND_TOKEN"):
        p["token"] = os.getenv("FINMIND_TOKEN")
    j = get(FINMIND, params=p).json()
    if j.get("status") != 200:
        raise RuntimeError(f"FinMind: {j.get('msg')}")
    return j["data"]


def list_active_etfs() -> dict[str, str]:
    etfs: dict[str, str] = {}
    try:
        for d in finmind({"dataset": "TaiwanStockInfo"}):
            code = str(d.get("stock_id", ""))
            STOCK_MARKET[code] = "TWO" if d.get("type") == "tpex" else "TW"
            if ACTIVE_CODE.match(code):
                etfs[code] = d.get("stock_name", code)
    except Exception as e:  # noqa: BLE001
        log("⚠ FinMind 清單讀取失敗，改用證交所/櫃買：", e)
    if etfs:
        return _apply_overrides(etfs)
    try:
        for d in get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL").json():
            code = str(d.get("Code", "")).strip()
            if ACTIVE_CODE.match(code):
                etfs[code] = str(d.get("Name", "")).strip()
    except Exception as e:  # noqa: BLE001
        log("⚠ 證交所清單讀取失敗：", e)
    try:
        for d in get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes").json():
            code = str(d.get("SecuritiesCompanyCode", "")).strip()
            if ACTIVE_CODE.match(code):
                etfs.setdefault(code, str(d.get("CompanyName", "")).strip())
    except Exception as e:  # noqa: BLE001
        log("⚠ 櫃買清單讀取失敗：", e)

    return _apply_overrides(etfs)


def _apply_overrides(etfs: dict[str, str]) -> dict[str, str]:
    for code in filter(None, os.getenv("EXTRA_ETFS", "").replace(" ", "").split(",")):
        etfs.setdefault(code.upper(), code.upper())
    for code in filter(None, os.getenv("EXCLUDE_ETFS", "").replace(" ", "").split(",")):
        etfs.pop(code.upper(), None)
    return dict(sorted(etfs.items()))


# ---------------------------------------------------------------- 2. ETF 持股
def _to_float(x) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    m = NUM.search(str(x))
    return float(m.group().replace(",", "")) if m else None


def parse_moneydj(html: str) -> tuple[list[dict], str | None]:
    """解析 MoneyDJ 全部持股頁。以「名稱(代號.TW)」辨識台股，忽略海外股與現金。"""
    date = None
    plain = re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ")
    m = re.search(r"資料日期\s*[:：]?\s*(\d{4}/\d{1,2}/\d{1,2})", plain)
    if m:
        date = m.group(1).replace("/", "-")
    tables = None
    for flavor in ("lxml", "bs4"):          # lxml 解析失敗時改用 BeautifulSoup/html5lib
        try:
            tables = pd.read_html(StringIO(html), flavor=flavor)
            break
        except ValueError:                  # 頁面上沒有表格
            return [], date
        except Exception as e:  # noqa: BLE001
            log(f"  read_html({flavor}) 失敗：{e}")
    if tables is None:
        return [], date

    rows: list[dict] = []
    seen = set()
    for t in tables:
        cols = [str(c) for c in t.columns]
        w_idx = next((i for i, c in enumerate(cols) if "比例" in c or "權重" in c), None)
        s_idx = next((i for i, c in enumerate(cols) if "股數" in c), None)
        for rec in t.itertuples(index=False):
            cells = list(rec)
            for i, cell in enumerate(cells):
                mm = STOCK_IN_CELL.match(str(cell).strip())
                if not mm:
                    continue
                name, code, mkt = mm.group(1).strip(), mm.group(2), mm.group(3)
                if code in seen:
                    break
                weight = _to_float(cells[w_idx]) if w_idx is not None else None
                shares = _to_float(cells[s_idx]) if s_idx is not None else None
                if weight is None:  # 欄名對不到時：名稱後第一個數字當權重、第二個當股數
                    nums = [_to_float(c) for c in cells[i + 1:]]
                    nums = [n for n in nums if n is not None]
                    weight = nums[0] if nums else None
                    shares = nums[1] if len(nums) > 1 else shares
                rows.append({"code": code, "name": name, "market": mkt,
                             "weight": weight, "shares": shares})
                seen.add(code)
                break
    return rows, date


def fetch_holdings(etf: str) -> tuple[list[dict], str | None]:
    order = ("TWO", "TW") if STOCK_MARKET.get(etf) == "TWO" else ("TW", "TWO")
    for suffix in order:
        url = f"https://www.moneydj.com/ETF/X/Basic/Basic0007b.xdjhtm?etfid={etf}.{suffix}"
        try:
            html = get(url).text
        except Exception as e:  # noqa: BLE001
            log(f"  {etf}.{suffix} 讀取失敗：{e}")
            continue
        rows, date = parse_moneydj(html)
        if rows:
            return rows, date
    return [], None


# ---------------------------------------------------------------- 3. 股價與均線
def download_prices(stocks: dict[str, str]) -> dict[str, pd.Series]:
    """stocks: code -> market(TW/TWO)。回傳 code -> 收盤價 Series。FinMind 為主，Yahoo 補缺。"""
    out: dict[str, pd.Series] = {}
    start = (dt.datetime.now(TZ) - dt.timedelta(days=200)).strftime("%Y-%m-%d")
    fails = 0
    for code in stocks:
        if fails >= 5:   # 可能被限流，剩下的交給 Yahoo
            break
        try:
            rows = [r for r in finmind({"dataset": "TaiwanStockPrice", "data_id": code,
                                        "start_date": start}) if r.get("close")]
            if len(rows) >= 2:
                out[code] = pd.Series([float(r["close"]) for r in rows],
                                      index=pd.to_datetime([r["date"] for r in rows]))
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"  FinMind {code} 失敗：{e}")
    missing = {c: m for c, m in stocks.items() if c not in out}
    if missing:
        log(f"  改用 Yahoo Finance 補 {len(missing)} 檔")
        out.update(_yahoo_prices(missing))
    return out


def _yahoo_prices(stocks: dict[str, str]) -> dict[str, pd.Series]:
    import yfinance as yf

    out: dict[str, pd.Series] = {}

    def batch(tickers: list[str]):
        if not tickers:
            return
        for i in range(0, len(tickers), 80):
            chunk = tickers[i:i + 80]
            df = yf.download(chunk, period="9mo", interval="1d", auto_adjust=False,
                             group_by="ticker", threads=True, progress=False)
            for t in chunk:
                try:
                    s = (df[t]["Close"] if len(chunk) > 1 else df["Close"]).dropna()
                    if isinstance(s, pd.DataFrame):
                        s = s.iloc[:, 0]
                except Exception:  # noqa: BLE001
                    continue
                if len(s) >= 2:
                    out[t.split(".")[0]] = s

    batch([f"{c}.{m}" for c, m in stocks.items()])
    # 市場別抓錯（上市/上櫃）時換另一個後綴再試
    missing = [c for c in stocks if c not in out]
    batch([f"{c}.{'TWO' if stocks[c] == 'TW' else 'TW'}" for c in missing])
    return out


def indicators(close: pd.Series) -> dict:
    ma5, ma20, ma60 = (close.rolling(n).mean() for n in (5, 20, 60))
    c, p = float(close.iloc[-1]), float(close.iloc[-2])
    res = {
        "close": round(c, 2),
        "chg": round((c / p - 1) * 100, 2) if p else None,
        "ma5": _r(ma5.iloc[-1]), "ma20": _r(ma20.iloc[-1]), "ma60": _r(ma60.iloc[-1]),
        "price_date": close.index[-1].strftime("%Y-%m-%d"),
        "spark": [round(float(x), 2) for x in close.iloc[-60:]],
        "spark_ma20": [_r(x) for x in ma20.iloc[-60:]],
    }
    ok = None not in (res["ma5"], res["ma20"], res["ma60"])
    res["bull"] = bool(ok and res["ma5"] > res["ma20"] > res["ma60"])
    m20_prev = ma20.iloc[-2]
    res["above_ma20"] = bool(res["ma20"] is not None and c > res["ma20"])
    res["cross_up"] = bool(res["above_ma20"] and pd.notna(m20_prev) and p <= float(m20_prev))
    return res


def _r(x):
    return None if x is None or pd.isna(x) else round(float(x), 2)


# ---------------------------------------------------------------- 4. 推播
def notify_telegram(text: str):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("（未設定 Telegram，略過推播）")
        return
    r = session.post(f"https://api.telegram.org/bot{token}/sendMessage",
                     data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": "true"}, timeout=30)
    log("Telegram：", r.status_code, r.text[:200])


def build_message(data: dict) -> str:
    lines = [f"<b>📊 主動式ETF成分股 盤後報告 {data['price_date']}</b>",
             f"追蹤 {data['etf_count']} 檔主動式ETF／{data['stock_count']} 檔台股成分股", ""]
    cross = data["cross_up_all"]
    if cross:
        lines.append(f"⚡ <b>今日站上月線（{len(cross)} 檔）</b>")
        for s in cross:
            star = "⭐" if s.get("in_top") else "•"
            bull = "｜多頭排列" if s.get("bull") else ""
            lines.append(f"{star} {s['code']} {s['name']}  {s['close']} ({s['chg']:+.2f}%)"
                         f"  {s['holders']}檔持有{bull}")
    else:
        lines.append("今日無成分股由下往上站上月線")
    top_bull = [s for s in data["top"] if s.get("bull")]
    lines += ["", f"🔥 前{TOP_N}名中多頭排列：{len(top_bull)} 檔"]
    lines.append("、".join(f"{s['code']}{s['name']}" for s in top_bull[:15]) or "無")
    if os.getenv("PAGE_URL"):
        lines += ["", os.getenv("PAGE_URL")]
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def main():
    now = dt.datetime.now(TZ)
    etfs = list_active_etfs()
    if not etfs:
        sys.exit("找不到任何主動式 ETF，請檢查網路或 EXTRA_ETFS 設定")
    log(f"主動式ETF共 {len(etfs)} 檔：{', '.join(etfs)}")

    holders: dict[str, dict] = {}
    etf_info, failed, hold_dates = [], [], []
    for code, name in etfs.items():
        rows, date = fetch_holdings(code)
        tw_rows = [r for r in rows if r["weight"] is None or r["weight"] >= MIN_WEIGHT]
        log(f"  {code} {name}: {len(tw_rows)} 檔台股 (資料日 {date})")
        if not tw_rows:
            failed.append(code)
            continue
        if date:
            hold_dates.append(date)
        etf_info.append({"code": code, "name": name, "n": len(tw_rows), "date": date})
        for r in tw_rows:
            mkt = STOCK_MARKET.get(r["code"], r["market"])
            h = holders.setdefault(r["code"], {"code": r["code"], "name": r["name"],
                                               "market": mkt, "etfs": []})
            h["etfs"].append({"code": code, "name": name, "weight": r["weight"]})
        time.sleep(0.8)  # 對來源網站客氣一點

    if not holders:
        sys.exit("所有 ETF 持股都抓不到，結束（不覆寫舊資料）")

    prices = download_prices({c: h["market"] for c, h in holders.items()})
    log(f"取得股價 {len(prices)}/{len(holders)} 檔")

    prev = {}
    prev_file = DATA_DIR / "latest.json"
    if prev_file.exists():
        try:
            old = json.loads(prev_file.read_text("utf-8"))
            if not old.get("sample"):
                prev = old
        except Exception:  # noqa: BLE001
            pass
    prev_count = prev.get("holder_counts", {})

    stocks = []
    for code, h in holders.items():
        h["etfs"].sort(key=lambda e: -(e["weight"] or 0))
        h["holders"] = len(h["etfs"])
        h["weight_sum"] = round(sum(e["weight"] or 0 for e in h["etfs"]), 2)
        h["delta"] = h["holders"] - prev_count[code] if code in prev_count else None
        if code in prices:
            h.update(indicators(prices[code]))
        stocks.append(h)

    stocks.sort(key=lambda s: (-s["holders"], -s["weight_sum"]))
    top_codes = {s["code"] for s in stocks[:TOP_N]}
    for i, s in enumerate(stocks):
        s["rank"] = i + 1
        s["in_top"] = s["code"] in top_codes

    def slim(s):
        return {k: s.get(k) for k in ("code", "name", "holders", "close", "chg", "ma5", "ma20",
                                      "ma60", "bull", "cross_up", "in_top", "rank")}

    price_dates = [s["price_date"] for s in stocks if s.get("price_date")]
    price_date = max(price_dates) if price_dates else now.strftime("%Y-%m-%d")
    data = {
        "sample": False,
        "updated_at": now.strftime("%Y-%m-%d %H:%M"),
        "price_date": price_date,
        "holdings_date": max(hold_dates) if hold_dates else None,
        "etf_count": len(etf_info),
        "stock_count": len(stocks),
        "etfs": etf_info,
        "failed_etfs": failed,
        "top": stocks[:TOP_N],
        "bull_all": [slim(s) for s in stocks if s.get("bull")],
        "cross_up_all": [slim(s) for s in stocks if s.get("cross_up")],
        "holder_counts": {s["code"]: s["holders"] for s in stocks},
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (DATA_DIR / "latest.json").write_text(text, "utf-8")
    (HIST_DIR / f"{price_date}.json").write_text(text, "utf-8")
    log(f"完成：前{TOP_N}名中多頭 {sum(s.get('bull', False) for s in data['top'])} 檔，"
        f"今日站上月線 {len(data['cross_up_all'])} 檔")

    # 同一交易日重複執行（或假日）不重複推播
    if prev.get("price_date") == price_date and not os.getenv("FORCE_NOTIFY"):
        log("交易日與上次相同，不重複推播")
        return
    notify_telegram(build_message(data))


if __name__ == "__main__":
    main()
