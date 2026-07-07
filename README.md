# 每日市場觀察

自動更新的市場日報：加密貨幣＋台股技術面、法人籌碼、量化 DOM 市場深度指標。

- **網站**：https://chasen8.github.io/market-daily/
- **更新頻率**：GitHub Actions 每小時執行（DOM 快照＋重建頁面）。
  台股區塊在證交所公布資料後（收盤日 15:00 台北後的下一輪）自動刷新。
- **資料來源**：Binance 公開 API（備援 data-api.binance.vision）、
  台灣證交所 OpenAPI 與 T86。全部免費、無金鑰。
- **DOM 歷史**：`data/dom_history.csv`，每小時一筆快照（本欄位 schema
  改動時要砍掉重建整個 CSV）。

## 結構

```
scripts/dom_snapshot.py   # 訂單簿快照 → data/dom_history.csv
scripts/build_report.py   # 產生 docs/index.html（GitHub Pages 服務目錄）
.github/workflows/build.yml
```

本 repo 由交易機器人專案（本機）維護；制度與決策記錄在本機專案的
`docs/project-charter.md`。

> 免責聲明：本站為程式自動彙整的技術面統計，不構成投資建議。
