# line_openclaw — 系統架構

## 概覽

用 Docker Compose 運行 OpenClaw AI gateway，透過 Python FastAPI bridge 接收 LINE Bot webhook，再由 OpenClaw 處理 AI 回覆，最後透過 LINE Messaging API 回覆使用者。

## 訊息流程

```
LINE App
  → LINE Platform (HTTPS POST)
  → Cloudflare Quick Tunnel
  → line-bridge:8000/line/webhook   (Python FastAPI)
       ├─ 驗證 X-Line-Signature (HMAC-SHA256)
       └─ docker exec openclaw node openclaw.mjs agent --json
              → github-copilot/claude-sonnet-4.6
  → LINE Reply API (HTTPS)
  → LINE App
```

## 容器架構

| 服務 | Image | 內部 Port | Host Port | 說明 |
|------|-------|-----------|-----------|------|
| `openclaw` | `ghcr.io/openclaw/openclaw:2026.5.6-slim` | 18789 | 127.0.0.1:19001 | AI Gateway |
| `line-bridge` | 本地 build (`line_bridge/`) | 8000 | 無（內部） | LINE webhook 處理 |
| `cloudflared` | `cloudflare/cloudflared:latest` | — | — | HTTPS tunnel（profile: tunnel） |

## 重要設定

### OpenClaw (`/.openclaw/openclaw.json`)
- **AI Model**: `github-copilot/claude-sonnet-4.6`
- **Auth**: Bearer token（見 `gateway.auth.token`）
- **LINE plugin**: 停用（`@openclaw/line` disabled）— LINE 由 Python bridge 處理
- **Session**: `agents/main/sessions/sessions.json`（bridge 啟動時自動讀取）

### line-bridge (`line_bridge/`)
- **Framework**: FastAPI + uvicorn，uv 管理依賴
- **Session 發現**: 啟動時從 `/readonly-openclaw/agents/main/sessions/sessions.json` 讀取 session UUID
- **OpenClaw 呼叫**: `docker exec line_openclaw-openclaw-1 node openclaw.mjs agent --session-id <uuid> --message <text> --json`
- **已知限制**: 所有 LINE 使用者共用同一個 OpenClaw session（對話記憶不隔離）

### Volumes
- `./.openclaw` → openclaw container `/home/node/.openclaw`（讀寫，持久化設定）
- `./.openclaw` → line-bridge container `/readonly-openclaw`（唯讀，讀取 session UUID）
- `/var/run/docker.sock` → line-bridge container（執行 docker exec）

## 環境變數（`.env`）

```
LINE_CHANNEL_ACCESS_TOKEN=   # LINE Bot channel access token
LINE_CHANNEL_SECRET=         # LINE Bot channel secret（用於 webhook 簽名驗證）
OPENCLAW_CONTAINER=line_openclaw-openclaw-1   # openclaw 容器名稱
```

## 常用指令（makefile）

```bash
make up            # 啟動 openclaw + line-bridge
make down          # 停止所有容器（含 tunnel）
make logs          # OpenClaw 日誌
make bridge-logs   # line-bridge 日誌
make cf-go         # 啟動 Cloudflare tunnel + 自動設定 LINE webhook
make cf-tunnel     # 只設定 LINE webhook（tunnel 已在跑時用）
make get-webhook   # 查詢目前 LINE webhook URL
```

## 檔案結構

```
line_openclaw/
├── docker-compose.yml      # 三個服務定義
├── makefile                # 操作指令
├── .env                    # 機密（不進 git）
├── .env.example            # 範本
├── .gitignore              # 排除 .env, .openclaw/
├── line_bridge/
│   ├── main.py             # FastAPI app（webhook 驗證、docker exec、LINE reply）
│   ├── pyproject.toml      # uv 依賴（fastapi, httpx, uvicorn）
│   ├── uv.lock             # 鎖定版本
│   ├── Dockerfile          # python3.12-bookworm-slim + docker.io CLI
│   └── .dockerignore
└── .openclaw/              # OpenClaw 狀態（不進 git）
    ├── openclaw.json       # gateway 設定、plugin 設定、model 設定
    ├── npm/                # 已安裝的 plugin（@openclaw/line，目前停用）
    └── agents/main/        # Agent session 狀態
```

## 重啟後的操作順序

1. `make up` — 啟動 openclaw + line-bridge
2. `make cf-go` — 啟動 tunnel，自動更新 LINE webhook
3. 在 LINE Developers Console 確認 webhook URL 並按 Verify

> Cloudflare Quick Tunnel URL 每次重啟都會改變，需重新執行 `make cf-go`。
