.PHONY: help up down logs bridge-logs cf-up cf-down cf-url cf-tunnel cf-go get-webhook

## 顯示所有指令
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

## 啟動 OpenClaw + line-bridge
up:
	@mkdir -p .openclaw
	@docker compose up -d --build openclaw line-bridge searxng
	@echo "Waiting for OpenClaw to be healthy..."
	@for i in $$(seq 1 30); do \
		if docker compose exec openclaw node -e "fetch('http://127.0.0.1:19001/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" 2>/dev/null; then \
			echo "OpenClaw is ready at http://localhost:19001"; break; \
		fi; \
		sleep 1; \
	done
	@echo "Waiting for line-bridge to be healthy..."
	@for i in $$(seq 1 30); do \
		if docker compose exec line-bridge python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)" >/dev/null 2>&1; then \
			echo "line-bridge is ready"; exit 0; \
		fi; \
		sleep 1; \
	done; \
	echo "Timed out waiting for line-bridge"; exit 1

## 停止所有容器
down:
	@docker compose --profile tunnel down

## 查看 OpenClaw 日誌
logs:
	@docker compose logs -f openclaw

## 查看 line-bridge 日誌
bridge-logs:
	@docker compose logs -f line-bridge

## 啟動 Cloudflare Quick Tunnel
cf-up:
	@docker compose --profile tunnel up -d cloudflared
	@echo "cloudflared started. Waiting for tunnel URL..."
	@for i in $$(seq 1 20); do \
		URL=$$(docker compose logs cloudflared 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1); \
		if [ -n "$$URL" ]; then echo "$$URL"; exit 0; fi; \
		sleep 1; \
	done; \
	echo "Timed out. Check: docker compose logs cloudflared"; exit 1

## 停止 Cloudflare Quick Tunnel
cf-down:
	@docker compose --profile tunnel stop cloudflared
	@docker compose --profile tunnel rm -f cloudflared
	@echo "cloudflared stopped"

## 顯示目前 Cloudflare Tunnel URL
cf-url:
	@docker compose logs cloudflared 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || \
		echo "cloudflared is not running (try: make cf-up)"

## 把目前 Cloudflare URL 設為 LINE webhook
cf-tunnel:
	@CF_URL=$$(docker compose logs cloudflared 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1) && \
	if [ -z "$$CF_URL" ]; then echo "cloudflared is not running (try: make cf-up)"; exit 1; fi && \
	$(MAKE) -s _sync-line URL="$$CF_URL"

## 一鍵：啟動所有服務 + tunnel + 設定 LINE webhook
cf-go: up cf-up
	@$(MAKE) -s cf-tunnel

## 查詢目前 LINE webhook 設定
get-webhook:
	@TOKEN=$$(grep '^LINE_CHANNEL_ACCESS_TOKEN=' .env | cut -d'=' -f2-) && \
	curl -s https://api.line.me/v2/bot/channel/webhook/endpoint \
		-H "Authorization: Bearer $$TOKEN" | python3 -m json.tool

# Internal: sync a public tunnel URL to LINE webhook endpoint
_sync-line:
	@if [ -z "$(URL)" ]; then echo "URL not provided"; exit 1; fi && \
	TOKEN=$$(grep '^LINE_CHANNEL_ACCESS_TOKEN=' .env | cut -d'=' -f2-) && \
	if [ -z "$$TOKEN" ]; then echo "LINE_CHANNEL_ACCESS_TOKEN not found in .env"; exit 1; fi && \
	WEBHOOK_URL="$(URL)/line/webhook" && \
	RESULT=$$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
		https://api.line.me/v2/bot/channel/webhook/endpoint \
		-H "Authorization: Bearer $$TOKEN" \
		-H "Content-Type: application/json" \
		-d "{\"endpoint\": \"$$WEBHOOK_URL\"}") && \
	if [ "$$RESULT" = "200" ]; then \
		echo "LINE webhook set: $$WEBHOOK_URL"; \
	else \
		echo "Failed to set webhook (HTTP $$RESULT). Check LINE_CHANNEL_ACCESS_TOKEN"; exit 1; \
	fi
