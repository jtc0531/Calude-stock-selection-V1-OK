"""
台股全市場飆股偵測系統 - V3 改良版
更新項目：
1. 移除掃描上限，永遠掃當天 FinMind 抓到的完整清單
2. 自動過濾 ETF（代碼00開頭、名稱含ETF）
3. 自動過濾 DR 存託憑證
4. 自動過濾 特別股（代碼含英文字母）
5. 顯示資料日期（股價日期、營收月份），讓使用者知道資料新鮮度
6. 加入強制清除快取按鈕
7. 股價格式化為兩位小數
"""

import streamlit as st
import pandas as pd
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────
# 頁面設定
# ─────────────────────────────────────────
st.set_page_config(
    page_title="台股飆股偵測 Pro",
    page_icon="🇹🇼",
    layout="wide"
)

st.title("🇹🇼 台股全市場飆股偵測系統 V3")
st.caption("資料來源：FinMind API（台股專用）｜自動過濾ETF/DR/特別股｜全市場掃描無上限")

# ─────────────────────────────────────────
# 側邊欄設定
# ─────────────────────────────────────────
st.sidebar.header("⚙️ 策略設定")

total_capital = st.sidebar.number_input("總投資預算 (TWD)", value=1_000_000, step=100_000)
position_pct = st.sidebar.slider("單筆建倉比例 (%)", 1, 10, 3) / 100
min_vol_k = st.sidebar.slider("最低日均成交量門檻（張）", 100, 2000, 500)
vol_multiplier = st.sidebar.slider("量能放大倍數", 1.0, 3.0, 1.5)
min_rev_growth = st.sidebar.slider("最低月營收年增率 (%)", -20, 50, 10) / 100
score_threshold = st.sidebar.slider("最低入選總分", 30, 90, 50)
max_workers = st.sidebar.slider("並發執行緒數", 2, 10, 5)

st.sidebar.markdown("---")
st.sidebar.markdown("**FinMind API Token（選填）**")
st.sidebar.markdown("免費版有頻率限制，[申請Token](https://finmind.github.io/) 可大幅提升速度")
finmind_token = st.sidebar.text_input("Token", type="password", placeholder="貼上你的 FinMind Token")

st.sidebar.markdown("---")
if st.sidebar.button("🔄 清除快取，重新抓取清單"):
    st.cache_data.clear()
    st.sidebar.success("快取已清除！")

# ─────────────────────────────────────────
# ETF / DR / 特別股 過濾規則
# ─────────────────────────────────────────
ETF_PREFIXES = ("00",)

def is_common_stock(code: str, name: str) -> bool:
    """
    回傳 True 表示為一般普通股，應納入掃描。
    過濾條件：
    - 代碼非純4位數字 → 排除（特別股如2882P）
    - 代碼以 00 開頭 → ETF，排除
    - 名稱含 ETF、指數、基金 → ETF，排除
    - 名稱含 -DR、存託 → DR，排除
    - 名稱含 特別股、REIT → 排除
    """
    if not code.isdigit() or len(code) != 4:
        return False
    if code.startswith(ETF_PREFIXES):
        return False
    name_upper = name.upper()
    exclude_keywords = ["ETF", "指數", "基金", "-DR", "存託", "特別股", "REIT", "不動產"]
    if any(kw in name_upper for kw in exclude_keywords):
        return False
    return True

# ─────────────────────────────────────────
# FinMind API 工具函數
# ─────────────────────────────────────────
FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"

def finmind_get(dataset: str, stock_id: str, start: str, token: str = "") -> pd.DataFrame:
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start,
        "token": token,
    }
    try:
        resp = requests.get(FINMIND_BASE, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == 200 and data.get("data"):
            return pd.DataFrame(data["data"])
    except Exception:
        pass
    return pd.DataFrame()

# ─────────────────────────────────────────
# 抓取全市場股票清單（無上限，自動過濾）
# ─────────────────────────────────────────
@st.cache_data(ttl=3600)
def fetch_all_tw_tickers():
    """
    從 FinMind 抓取當天最新台股清單。
    - 無掃描上限：抓到多少掃多少
    - 自動過濾 ETF、DR、特別股
    - 快取1小時，可手動清除
    回傳 (filtered_df, raw_total)
    """
    raw_total = 0

    # 方法一：FinMind TaiwanStockInfo
    try:
        resp = requests.get(
            FINMIND_BASE,
            params={"dataset": "TaiwanStockInfo"},
            timeout=20
        )
        data = resp.json()
        if data.get("status") == 200:
            df = pd.DataFrame(data["data"])
            raw_total = len(df)
            df = df.rename(columns={"stock_id": "code", "stock_name": "name", "type": "market"})
            mask = df.apply(lambda r: is_common_stock(str(r["code"]), str(r["name"])), axis=1)
            df = df[mask][["code", "name", "market"]].reset_index(drop=True)
            return df, raw_total
    except Exception:
        pass

    # 備援：TWSE ISIN 頁面
    tickers = []
    urls = [
        ("https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", "上市"),
        ("https://isin.twse.com.tw/isin/C_public.jsp?strMode=4", "上櫃"),
    ]
    headers = {"User-Agent": "Mozilla/5.0"}
    for url, market in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            tables = pd.read_html(resp.text)
            df_tmp = tables[0]
            df_tmp.columns = df_tmp.iloc[0]
            for item in df_tmp["有價證券代號及名稱"].iloc[1:]:
                if isinstance(item, str):
                    parts = item.replace("\u3000", " ").split(" ")
                    code = parts[0]
                    name = parts[1] if len(parts) > 1 else ""
                    raw_total += 1
                    if is_common_stock(code, name):
                        tickers.append({"code": code, "name": name, "market": market})
        except Exception:
            continue

    if tickers:
        return pd.DataFrame(tickers), raw_total

    # 最終備援
    base = [
        ("2330","台積電","上市"),("2317","鴻海","上市"),("2454","聯發科","上市"),
        ("2308","台達電","上市"),("2382","廣達","上市"),("3231","緯創","上市"),
        ("6669","緯穎","上市"),("2376","技嘉","上市"),("2357","華碩","上市"),
        ("2881","富邦金","上市"),("2882","國泰金","上市"),("2886","兆豐金","上市"),
    ]
    df = pd.DataFrame(base, columns=["code", "name", "market"])
    return df, len(df)

# ─────────────────────────────────────────
# 技術指標計算
# ─────────────────────────────────────────
def calc_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return round(rsi.iloc[-1], 1) if not rsi.empty else float("nan")

def calc_macd(series: pd.Series):
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd.iloc[-1], signal.iloc[-1]

def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

# ─────────────────────────────────────────
# 核心分析：單股評分
# ─────────────────────────────────────────
def analyze_stock(row: dict, token: str = "", min_vol: int = 500,
                  vol_mult: float = 1.5, rev_threshold: float = 0.10,
                  capital: int = 1_000_000, pos_pct: float = 0.03) -> dict | None:
    code = row["code"]
    name = row.get("name", code)

    try:
        # ── 1. 抓取日K資料（近14個月）──
        start_date = (datetime.today() - timedelta(days=430)).strftime("%Y-%m-%d")
        price_df = finmind_get("TaiwanStockPrice", code, start_date, token)
        if price_df.empty or len(price_df) < 60:
            return None

        price_df = price_df.sort_values("date").reset_index(drop=True)
        price_date = str(price_df["date"].iloc[-1])[:10]
        close = price_df["close"].astype(float)
        high_s = price_df["max"].astype(float)
        low_s = price_df["min"].astype(float)
        volume = price_df["Trading_Volume"].astype(float) / 1000  # 張

        # ── 2. 流動性門檻 ──
        vol_20avg = volume.tail(20).mean()
        if vol_20avg < min_vol:
            return None

        curr_price = close.iloc[-1]
        prev_price = close.iloc[-2]

        # ── 3. 技術指標 ──
        ma250 = close.rolling(250).mean().iloc[-1] if len(close) >= 250 else None

        # 120日真實突破（昨日未過、今日過）
        high_120 = close.tail(121).iloc[:-1].max()
        is_breakout = (curr_price >= high_120) and (prev_price < high_120)

        curr_vol = volume.iloc[-1]
        vol_surge = curr_vol > vol_20avg * vol_mult

        rsi = calc_rsi(close)
        macd_val, macd_sig = calc_macd(close)
        macd_golden = (macd_val > macd_sig) and (macd_val - macd_sig > 0)

        atr = calc_atr(high_s, low_s, close)
        atr_stop = round(curr_price - 2 * atr, 2)
        atr_target = round(curr_price + 3 * atr, 2)

        # ── 4. 月營收年增率 ──
        rev_start = (datetime.today() - timedelta(days=400)).strftime("%Y-%m-%d")
        rev_df = finmind_get("TaiwanStockMonthRevenue", code, rev_start, token)
        rev_growth = float("nan")
        rev_label = "無資料"
        rev_date = "—"
        if not rev_df.empty and "revenue" in rev_df.columns:
            rev_df = rev_df.sort_values("date").reset_index(drop=True)
            rev_df["revenue"] = pd.to_numeric(rev_df["revenue"], errors="coerce")
            if len(rev_df) >= 13:
                latest = rev_df["revenue"].iloc[-1]
                year_ago = rev_df["revenue"].iloc[-13]
                rev_date = str(rev_df["date"].iloc[-1])[:7]
                if year_ago and year_ago > 0:
                    rev_growth = (latest - year_ago) / year_ago
                    rev_label = f"{round(rev_growth * 100, 1)}%"

        # ── 5. 評分邏輯 ──
        score = 0
        reasons = []

        # 基本面（最高 30 分）
        if not pd.isna(rev_growth):
            if rev_growth > rev_threshold:
                score += 30
                reasons.append(f"月營收年增{rev_label}")
            elif rev_growth > 0:
                score += 10
                reasons.append(f"月營收微增{rev_label}")
        else:
            score += 5

        # 技術面（最高 70 分）
        if is_breakout:
            score += 30
            reasons.append("120日真實突破")
        elif curr_price >= high_120 * 0.97:
            score += 10
            reasons.append("逼近120日高點")

        if vol_surge:
            score += 20
            reasons.append(f"量能{round(curr_vol/vol_20avg,1)}倍放大")

        if 50 < rsi < 75:
            score += 10
            reasons.append(f"RSI健康({rsi})")
        elif rsi >= 75:
            score -= 5
            reasons.append(f"RSI超買({rsi})")

        if macd_golden:
            score += 10
            reasons.append("MACD金叉")

        if ma250 and curr_price > ma250:
            score += 10
            reasons.append("站上年線")

        return {
            "代碼": code,
            "名稱": name,
            "總分": score,
            "股價": round(curr_price, 2),
            "股價日期": price_date,
            "月營收年增率": rev_label,
            "營收月份": rev_date,
            "RSI": rsi,
            "MACD金叉": "✅" if macd_golden else "❌",
            "日均量(張)": int(vol_20avg),
            "動態停損": atr_stop,
            "停利目標": atr_target,
            "建倉金額": int(capital * pos_pct),
            "特徵分析": " | ".join(reasons),
        }

    except Exception:
        return None


# ─────────────────────────────────────────
# 並發掃描
# ─────────────────────────────────────────
def parallel_scan(stock_list: list, token: str, min_vol: int,
                  vol_mult: float, rev_threshold: float, workers: int,
                  score_thresh: int, capital: int, pos_pct: float,
                  progress_bar, status_text) -> list:
    results = []
    total = len(stock_list)
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                analyze_stock, row, token, min_vol, vol_mult,
                rev_threshold, capital, pos_pct
            ): row["code"]
            for row in stock_list
        }
        for future in as_completed(futures):
            done += 1
            progress_bar.progress(done / total)
            status_text.text(f"已分析 {done}/{total}｜發現 {len(results)} 檔候選")
            try:
                res = future.result()
                if res and res["總分"] >= score_thresh:
                    results.append(res)
            except Exception:
                pass
    return results


# ─────────────────────────────────────────
# 主介面
# ─────────────────────────────────────────
col1, col2, col3 = st.columns(3)
col1.metric("總預算", f"NT$ {total_capital:,.0f}")
col2.metric("單筆建倉", f"NT$ {int(total_capital * position_pct):,.0f}（{int(position_pct*100)}%）")
col3.metric("入選門檻", f"{score_threshold} 分")

st.markdown("---")

# 資料新鮮度提示
now = datetime.now()
market_close = now.replace(hour=13, minute=30, second=0, microsecond=0)
if now.weekday() >= 5:
    data_note = "⚠️ 今日為假日，股價資料為上一個交易日收盤價"
elif now < market_close:
    data_note = "⚠️ 目前盤中（收盤前），股價資料為**昨日**收盤價"
else:
    data_note = "✅ 已收盤，股價資料應為今日最新收盤價（FinMind 約收盤後1小時更新）"

st.info(
    f"🕐 現在時間：{now.strftime('%Y-%m-%d %H:%M')}　|　{data_note}\n\n"
    f"📅 月營收：每月10日前後公告上月數據，請查看結果表格的「營收月份」欄位確認資料所屬月份"
)

if st.button("🚀 啟動全市場深度掃描（無上限）", type="primary", use_container_width=True):

    with st.spinner("📡 從 FinMind 抓取當天最新台股清單中..."):
        ticker_df, raw_total = fetch_all_tw_tickers()

    filtered_count = len(ticker_df)
    excluded_count = raw_total - filtered_count

    # 全市場掃描，不截斷
    stock_list = ticker_df.to_dict("records")

    st.info(
        f"📊 當天清單共 **{raw_total}** 檔　｜　"
        f"過濾 ETF/DR/特別股後剩 **{filtered_count}** 檔（排除 {excluded_count} 檔）　｜　"
        f"全部納入掃描　｜　並發執行緒：{max_workers}"
    )

    progress_bar = st.progress(0)
    status_text = st.empty()

    start_time = time.time()
    results = parallel_scan(
        stock_list,
        token=finmind_token,
        min_vol=min_vol_k,
        vol_mult=vol_multiplier,
        rev_threshold=min_rev_growth,
        workers=max_workers,
        score_thresh=score_threshold,
        capital=total_capital,
        pos_pct=position_pct,
        progress_bar=progress_bar,
        status_text=status_text,
    )
    elapsed = round(time.time() - start_time, 1)

    progress_bar.progress(1.0)
    status_text.empty()

    if results:
        df = (
            pd.DataFrame(results)
            .sort_values("總分", ascending=False)
            .head(50)
            .reset_index(drop=True)
        )
        df.index += 1

        st.success(f"✅ 掃描完成！耗時 {elapsed} 秒，從 {filtered_count} 檔中篩選出 {len(df)} 檔候選標的")

        # 資料新鮮度摘要
        latest_price_date = df["股價日期"].max()
        latest_rev_month = df["營收月份"].replace("—", pd.NA).dropna().max()
        st.markdown(
            f"📌 結果中最新股價日期：`{latest_price_date}`　｜　"
            f"最新營收月份：`{latest_rev_month if pd.notna(latest_rev_month) else '無資料'}`"
        )

        # 色彩標記高分股
        def highlight_score(val):
            if val >= 80:
                return "background-color: #1a472a; color: white"
            elif val >= 60:
                return "background-color: #2d6a4f; color: white"
            return ""

        styled = df.style.map(highlight_score, subset=["總分"])
        st.dataframe(styled, use_container_width=True)

        # 下載
        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 下載篩選報告 (CSV)",
            csv,
            f"TW_Stock_Radar_{datetime.today().strftime('%Y%m%d')}.csv",
            "text/csv",
            use_container_width=True
        )

        # 評分 & 欄位說明
        st.markdown("### 📋 評分說明")
        st.markdown("""
| 分數範圍 | 說明 |
|---|---|
| 🟢 80分以上 | 基本面+技術面共振，強烈關注 |
| 🟡 60–79分 | 技術面良好，可列入觀察 |
| ⚪ 50–59分 | 符合門檻，需進一步研究 |

| 欄位 | 說明 |
|---|---|
| 股價日期 | 股價資料最新日期，可確認是否為最新收盤 |
| 營收月份 | 月營收資料對應月份（如 2025-01 = 1月營收） |
| 動態停損 | 股價 − 2×ATR（依波動度動態調整，非固定比例） |
| 停利目標 | 股價 + 3×ATR（風報比約 1:1.5） |
        """)

    else:
        st.warning(
            "⚠️ 目前設定下未發現符合標的，建議：\n"
            "1. 將「最低總分」調低至 40\n"
            "2. 將「最低月營收年增率」調低\n"
            "3. 將「日均成交量門檻」調低至 200 張"
        )

st.markdown("---")
st.markdown("""
**V3 更新說明**
- 🔍 **全市場掃描**：不設上限，永遠掃當天 FinMind 最新清單，家數變動自動反映
- 🚫 **自動過濾**：ETF（00開頭）、DR 存託憑證、特別股、REITs 全部自動排除
- 📅 **資料透明度**：每檔顯示「股價日期」與「營收月份」，清楚知道資料新鮮度
- 🔄 **快取控制**：側邊欄可手動清除快取，強制重抓最新清單
- ⚡ **效能**：ThreadPoolExecutor 並發掃描，參數傳遞改為函式引數（執行緒安全）
""")