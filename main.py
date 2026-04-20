import os
import sys
import requests
import json
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn

app = FastAPI()

# --- 1. 從環境變數讀取金鑰 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("錯誤: 缺少必要的環境變數設定。請檢查 Zeabur 的 Variables 設定。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 2. Dify API 呼叫函數 ---
def ask_dify(user_id: str, text: str) -> str:
    url = 'https://api.dify.ai/v1/chat-messages'
    headers = {
        'Authorization': f'Bearer {DIFY_API_KEY}',
        'Content-Type': 'application/json'
    }
    data = {
        "inputs": {},
        "query": text,
        "response_mode": "blocking",
        "user": user_id,
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()
        answer = response.json().get('answer', '').strip()
        return answer if answer else "我想到一半忘記說什麼了，請再問我一次！"

    except requests.exceptions.Timeout:
        print(f"[Dify] 請求逾時 | user={user_id}")
        return (
            "☕ 我剛剛去泡了杯茶回來，結果忘記你問什麼了...\n\n"
            "請稍等一下再試試看！如果一直這樣，可以聯絡開發者 Chris 幫你看看哦 🙏"
        )

    except requests.exceptions.ConnectionError:
        print(f"[Dify] 連線失敗 | user={user_id}")
        return (
            "😵 哎呀，我的大腦好像暫時斷網了！\n\n"
            "請等一下再問我，若持續發生請聯絡開發者 Chris 處理，謝謝你的耐心 🙇"
        )

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "未知"
        error_msg = ""
        try:
            error_msg = e.response.json().get('message', '')
        except Exception:
            pass
        print(f"[Dify] HTTP 錯誤 {status}: {error_msg} | user={user_id}")
        return (
            f"🔧 我遇到了一點小問題（錯誤碼：{status}），先去找 Chris 修一下！\n\n"
            "請稍後再試，或直接聯絡開發者 Chris 回報這個問題，感謝你 💪"
        )

    except Exception as e:
        print(f"[Dify] 未預期錯誤: {e} | user={user_id}")
        return (
            "🤔 我剛才靈魂出竅了一下，請再問我一次！\n\n"
            "如果問題一直出現，麻煩聯絡開發者 Chris 看看，謝謝你的包容 😊"
        )


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

    ai_response = ask_dify(user_id, user_text)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=ai_response)
    )


# --- 4. 配合 Zeabur 的啟動設定 ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
