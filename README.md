# 主動式ETF 共識持股雷達

統計所有台股主動式ETF的成分股，依「被幾檔主動式ETF持有」排名取前 30 名，
標示 **多頭排列（5MA＞20MA＞60MA）**，並特別提醒 **當日收盤站上月線** 的個股。
每天盤後由 GitHub Actions 自動更新、部署成 GitHub Pages 網頁，可直接嵌入 blog。

```
update.py                    每日更新程式（抓資料、計算、輸出 JSON、Telegram 推播）
docs/index.html              網頁本體
docs/data/latest.json        最新資料（網頁讀這個；目前是範例資料）
docs/data/history/           每日歷史資料
docs/embed-example.html      嵌入 blog 的 iframe 範例
.github/workflows/update.yml 排程（週一～五 19:30、21:30）
```

## 架設步驟（一次性，約 10 分鐘）

1. **建立 GitHub repo**：例如 `active-etf-tracker`（Public，GitHub Pages 免費版需要公開）。
   把這個資料夾全部檔案上傳（網頁上 *Add file → Upload files* 拖進去即可，`.github` 資料夾也要）。
2. **開啟 Pages**：repo → *Settings → Pages* → Source 選 *Deploy from a branch*，
   Branch 選 `main`、資料夾選 `/docs` → Save。
   幾分鐘後網址會是 `https://你的帳號.github.io/active-etf-tracker/`
3. **允許 Actions 寫入**：*Settings → Actions → General → Workflow permissions* 選
   *Read and write permissions* → Save。
4. **Secrets 設定**：*Settings → Secrets and variables → Actions*
   - **建議** 新增 `FINMIND_TOKEN`（到 finmindtrade.com 免費註冊取得，額度 300→600 次/小時；
     每次更新約用 230 次）
   - （選用）Telegram 推播：新增 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
   - Variables 新增 `PAGE_URL` = 你的網頁網址（推播訊息會附上）
5. **第一次手動執行**：*Actions → 每日盤後更新主動式ETF資料 → Run workflow*。
   跑完（約 2～4 分鐘）網頁就會從範例資料換成真實資料。之後每個交易日自動更新。

## 嵌入 blog

打開 `docs/embed-example.html`，把網址換成你的，整段貼到：
- **Blogger**：文章編輯切到「HTML 檢視」貼上
- **WordPress**：新增「自訂 HTML」區塊貼上
- **Medium / 痞客邦** 等不允許 script 的平台：只貼 `<iframe>` 那行，並把 `min-height` 調到約 2400px

## 計算規則

| 項目 | 定義 |
|---|---|
| 主動式ETF | FinMind 股票清單中代號 `00xxxA` 的主動式ETF（含上櫃；`D` 結尾債券型不納入） |
| 成分股 | MoneyDJ 每日全部持股（Basic0007b）中的台股；海外股、期貨（.TF）、現金不計；只計入「該股占ETF權重超過 1%」的持股（`MIN_WEIGHT` 可調） |
| 股價 | FinMind 日收盤（上市櫃皆可），抓不到的再用 Yahoo Finance 補 |
| 排名 | 持有檔數多者在前；同檔數時以各ETF權重加總較高者在前 |
| 多頭排列 | 5MA ＞ 20MA ＞ 60MA（日收盤價簡單均線） |
| 今日站上月線 | 昨日收盤 ≤ 昨日 20MA，且今日收盤 ＞ 今日 20MA（由下往上突破）；掃描**全部成分股**，Top30 內的會加 ⭐ |
| 站上20日最高點 | 今日收盤 ＞ 前 20 個交易日（不含今日）的最高價；掃描全部成分股 |
| 檔數變化 | 與上一次更新相比，持有該股的ETF檔數 +/-（網頁上小紅綠標籤） |

## 常見調整

- **加入／排除某檔 ETF**：Repo Variables 設 `EXTRA_ETFS=00999A,00403A` 或 `EXCLUDE_ETFS=00983A`
- **權重門檻**：Repo Variables 設 `MIN_WEIGHT`（預設 1，即權重超過 1% 才計入；設 0 則全部計入）
- **前 N 名**：改 `update.py` 的 `TOP_N`
- **更新時間**：改 `.github/workflows/update.yml` 的 cron（UTC 時間，台灣時間減 8 小時）
- **在自己電腦測試**：`pip install -r requirements.txt` 後 `python update.py`，
  再於 `docs` 資料夾執行 `python -m http.server`，瀏覽 http://localhost:8000

## 注意

- 持股資料來自 MoneyDJ 網頁，若對方改版導致抓不到，Actions 會失敗且**不會覆寫舊資料**；
  需要時調整 `update.py` 的 `parse_moneydj()`。網頁底部會列出本次沒抓到的 ETF。
- GitHub 排程在尖峰時段可能延遲 5～30 分鐘，屬正常現象。
- 本工具僅供資訊整理，不構成投資建議。
