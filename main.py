import os
import sys
import requests
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn

app = FastAPI()

# --- 1. 從環境變數讀取金鑰 ---
# 在 Zeabur 的 Variables 頁面設定這些名稱
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')

# 安全檢查：確保必要的環境變數都有設定
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("錯誤: 缺少必要的環境變數設定。請檢查 Zeabur 的 Variables 設定。")
    # 在本機開發時可以正常啟動，但在伺服器上這會提醒你漏掉了設定
    # sys.exit(1) 

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 2. Dify API 呼叫函數 ---
def ask_dify(user_id, text):
    url = 'https://api.dify.ai/v1/chat-messages'
    headers = {
        'Authorization': f'Bearer {DIFY_API_KEY}',
        'Content-Type': 'application/json'
    }
    data = {
        "inputs": {},
        "query": text,
        "response_mode": "blocking",
        "user": user_id,  # 使用 Line userId 區分不同人的對話記憶
    }
    try:
        response = requests.post(url, headers=headers, json=json.dumps(data), timeout=30)
        return response.json().get('answer', "AI 教練暫時無法回應，請稍後再試。")
    except Exception as e:
        print(f"Dify API Error: {e}")
        return "對不起，我現在連不上大腦，請檢查 API 設定。"

# --- 3. Line Webhook 進入點 ---
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get('X-Line-Signature', '')
    body = await request.body()
    try:
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text
    
    # 取得 AI 回應
    ai_response = ask_dify(user_id, user_text)
    
    # 回傳給 Line
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=ai_response)
    )

# --- 4. 配合 Zeabur 的啟動設定 ---
if __name__ == "__main__":
    # Zeabur 會自動注入 PORT 環境變數，若無則預設 8080
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)