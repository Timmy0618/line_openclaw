# line_openclaw

用 Docker Compose 運行 OpenClaw AI gateway，透過 Python FastAPI bridge 接收 LINE Bot webhook，再由 OpenClaw 處理 AI 回覆。

## 架構

```
LINE App
  → LINE Platform
  → Cloudflare Quick Tunnel
  → line-bridge:8000/line/webhook  (Python FastAPI)
       └─ docker exec openclaw node openclaw.mjs agent --json
  → LINE Reply API
  → LINE App
```

## 前置需求

- Docker + Docker Compose
- LINE Messaging API channel（[LINE Developers Console](https://developers.line.biz/console/)）
- OpenClaw 授權

---

## 快速開始（第一個 bot）

### 1. Clone 專案

```bash
git clone https://github.com/Timmy0618/line_openclaw.git my-bot
cd my-bot
```

### 2. 建立 `.env`

```bash
cp .env.example .env
```

編輯 `.env`，填入必要欄位：

```env
COMPOSE_PROJECT_NAME=my_bot        # 唯一名稱，不能有空白
LINE_CHANNEL_ACCESS_TOKEN=...      # LINE Bot channel access token
LINE_CHANNEL_SECRET=...            # LINE Bot channel secret
```

### 3. 啟動服務

```bash
make up
```

OpenClaw 首次啟動會自動初始化設定，啟動後透過 OpenClaw CLI 或 Web UI 設定 AI model 與 auth。

### 4. 開啟 Cloudflare Tunnel 並設定 LINE webhook

```bash
make cf-go
```

執行後會輸出 tunnel URL，並自動設定到 LINE webhook。

> Cloudflare Quick Tunnel URL 每次重啟都會改變，需重新執行 `make cf-go`。

---

## 同一台機器跑多個 bot

每個 bot 各自 clone 到獨立目錄，透過 `COMPOSE_PROJECT_NAME` 與 `OPENCLAW_HOST_PORT` 做區分。

```bash
# Bot A
git clone https://github.com/Timmy0618/line_openclaw.git bot-a
cd bot-a
cp .env.example .env
# 編輯 .env：
#   COMPOSE_PROJECT_NAME=bot_a
#   OPENCLAW_HOST_PORT=19001
#   LINE_CHANNEL_ACCESS_TOKEN=...（Bot A 的 token）
#   LINE_CHANNEL_SECRET=...
make up && make cf-go

# Bot B（另一個終端）
git clone https://github.com/Timmy0618/line_openclaw.git bot-b
cd bot-b
cp .env.example .env
# 編輯 .env：
#   COMPOSE_PROJECT_NAME=bot_b
#   OPENCLAW_HOST_PORT=19002    ← 不同 port
#   LINE_CHANNEL_ACCESS_TOKEN=...（Bot B 的 token）
#   LINE_CHANNEL_SECRET=...
make up && make cf-go
```

每個 bot 的 OpenClaw 狀態（session、memory）存在各自目錄的 `.openclaw/` 下，完全隔離。

---

## 環境變數

| 變數 | 必填 | 預設值 | 說明 |
|------|------|--------|------|
| `COMPOSE_PROJECT_NAME` | ✅ | `line_openclaw` | 容器名稱 prefix，同機多 bot 時須唯一 |
| `LINE_CHANNEL_ACCESS_TOKEN` | ✅ | — | LINE Bot channel access token |
| `LINE_CHANNEL_SECRET` | ✅ | — | LINE Bot channel secret |
| `OPENCLAW_HOST_PORT` | | `19001` | OpenClaw Web UI 的 host port，同機多 bot 時須不同 |
| `OPENCLAW_TIMEOUT_SECONDS` | | `25` | 等待 OpenClaw 回覆的超時秒數 |
| `LOG_LEVEL` | | `INFO` | log 等級（DEBUG / INFO / WARNING / ERROR） |
| `GROUP_BUFFER_SIZE` | | `20` | 每個群組保留的近期訊息則數（被動監聽用） |
| `ALLOWED_USER_IDS` | | 空（不限制） | 允許 DM 互動的 LINE userId，逗號分隔 |
| `ALLOWED_GROUP_IDS` | | 空（不限制） | 允許回應的群組 ID，逗號分隔 |

---

## 常用指令

```bash
make up            # 啟動 openclaw + line-bridge
make down          # 停止所有容器（含 tunnel）
make logs          # 查看 OpenClaw 日誌
make bridge-logs   # 查看 line-bridge 日誌
make cf-go         # 啟動 Cloudflare tunnel + 自動設定 LINE webhook
make cf-tunnel     # 只更新 LINE webhook（tunnel 已在跑時用）
make cf-url        # 顯示目前 tunnel URL
make get-webhook   # 查詢目前 LINE webhook 設定
```

---

## 群組訊息行為

- **沒有 @mention**：bot 靜默讀取訊息，存入本地 buffer（不呼叫 AI，不消耗 token）
- **@mention bot**：bot 回覆，並自動帶入 buffer 中的近期訊息作為上下文

每個群組與每個 1-on-1 DM 使用者各自擁有獨立的 OpenClaw session，對話記憶完全隔離。

---

## 存取控制

在 `.env` 設定白名單：

```env
# 只允許指定使用者的 DM（userId 以 U 開頭，可在 bridge-logs 中看到）
ALLOWED_USER_IDS=Uxxxxxxxxx,Uyyyyyyyyy

# 只允許指定群組回應（groupId 以 C 開頭，roomId 以 R 開頭）
ALLOWED_GROUP_IDS=Cxxxxxxxxx
```

留空表示不限制。
