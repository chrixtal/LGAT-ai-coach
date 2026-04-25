import os
import sqlite3
import json
import requests
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn

app = FastAPI()

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')  # 備援 App Key

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("錯誤: 缺少必要的環境變數設定。請檢查 Zeabur 的 Variables 設定。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- SQLite 初始化 ---
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 對話記錄
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_conversations (
            line_user_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 用戶資料（基本資訊 + 教練偏好）
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
            line_user_id TEXT PRIMARY KEY,
            display_name TEXT,
            coach_tone TEXT DEFAULT 'balanced',       -- strict / gentle / balanced
            coach_style TEXT DEFAULT 'exploratory',   -- direct / exploratory
            quote_freq TEXT DEFAULT 'sometimes',      -- often / sometimes / never
            onboarding_done INTEGER DEFAULT 0,        -- 0=未完成問卷, 1=已完成
            onboarding_step INTEGER DEFAULT 0,        -- 問卷進度
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()

init_db()

# ============================
# DB helpers
# ============================

def get_conversation_id(line_user_id: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT conversation_id FROM user_conversations WHERE line_user_id = ?', (line_user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_conversation_id(line_user_id: str, conversation_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO user_conversations (line_user_id, conversation_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(line_user_id) DO UPDATE SET
            conversation_id = excluded.conversation_id,
            updated_at = CURRENT_TIMESTAMP
    ''', (line_user_id, conversation_id))
    conn.commit()
    conn.close()

def reset_conversation(line_user_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_conversations WHERE line_user_id = ?', (line_user_id,))
    conn.commit()
    conn.close()

def get_profile(line_user_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM user_profiles WHERE line_user_id = ?', (line_user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        'line_user_id': line_user_id,
        'display_name': '',
        'coach_tone': 'balanced',
        'coach_style': 'exploratory',
        'quote_freq': 'sometimes',
        'onboarding_done': 0,
        'onboarding_step': 0,
    }

def save_profile(line_user_id: str, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 確保 row 存在
    c.execute('INSERT OR IGNORE INTO user_profiles (line_user_id) VALUES (?)', (line_user_id,))
    for k, v in kwargs.items():
        c.execute(f'UPDATE user_profiles SET {k} = ?, updated_at = CURRENT_TIMESTAMP WHERE line_user_id = ?', (v, line_user_id))
    conn.commit()
    conn.close()

# ============================
# 問卷 Onboarding
# ============================

ONBOARDING_STEPS = [
    {
        "field": "display_name",
        "question": (
            "👋 嗨！我是你的 AI 生活教練。\n\n"
            "在開始之前，我想先多了解你一點！\n\n"
            "❶ 你怎麼稱呼你自己呢？（輸入你的名字或暱稱就好）"
        ),
    },
    {
        "field": "coach_tone",
        "question": (
            "很高興認識你！🙌\n\n"
            "❷ 你喜歡什麼樣的教練語氣？\n\n"
            "請輸入數字：\n"
            "1️⃣ 嚴格督促型（推你一把，不留情面）\n"
            "2️⃣ 溫柔支持型（像朋友一樣陪伴你）\n"
            "3️⃣ 平衡理性型（視情況調整）"
        ),
        "choices": {"1": "strict", "2": "gentle", "3": "balanced"},
    },
    {
        "field": "coach_style",
        "question": (
            "❸ 你習慣哪種溝通方式？\n\n"
            "請輸入數字：\n"
            "1️⃣ 直接說重點（我要答案，不要繞彎子）\n"
            "2️⃣ 循循善誘（陪我慢慢想清楚）"
        ),
        "choices": {"1": "direct", "2": "exploratory"},
    },
    {
        "field": "quote_freq",
        "question": (
            "❹ 最後一個問題！\n\n"
            "你喜歡我在對話中引用名言、學術理論或研究嗎？\n\n"
            "請輸入數字：\n"
            "1️⃣ 多一點，我喜歡有根據的東西\n"
            "2️⃣ 偶爾就好，不要太多\n"
            "3️⃣ 不用，我比較喜歡簡單直白"
        ),
        "choices": {"1": "often", "2": "sometimes", "3": "never"},
    },
]

def handle_onboarding(line_user_id: str, text: str, profile: dict) -> str | None:
    """
    回傳 None 表示 onboarding 已完成，交給正常對話處理。
    回傳字串表示 onboarding 進行中，直接回傳給用戶。
    """
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    # 第一步：還沒開始，送出第一個問題
    if step == 0:
        save_profile(line_user_id, onboarding_step=1)
        return ONBOARDING_STEPS[0]['question']

    # 處理回答
    current_step = ONBOARDING_STEPS[step - 1]
    field = current_step['field']

    if 'choices' in current_step:
        answer = current_step['choices'].get(text.strip())
        if not answer:
            # 輸入不合法，重問
            valid = '、'.join(current_step['choices'].keys())
            return f"請輸入 {valid} 其中一個數字 😊\n\n" + current_step['question']
    else:
        # 自由輸入（名字）
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！請輸入你的名字或暱稱 😊"

    save_profile(line_user_id, **{field: answer, 'onboarding_step': step + 1})

    # 還有下一步？
    if step < len(ONBOARDING_STEPS):
        return ONBOARDING_STEPS[step]['question']

    # 全部完成！
    save_profile(line_user_id, onboarding_done=1)
    profile_updated = get_profile(line_user_id)
    name = profile_updated['display_name'] or '你'

    tone_label = {"strict": "嚴格督促", "gentle": "溫柔支持", "balanced": "平衡理性"}.get(profile_updated['coach_tone'], '')
    style_label = {"direct": "直接說重點", "exploratory": "循循善誘"}.get(profile_updated['coach_style'], '')
    quote_label = {"often": "常常引用", "sometimes": "偶爾引用", "never": "不引用"}.get(profile_updated['quote_freq'], '')

    return (
        f"太棒了，{name}！✨ 設定完成！\n\n"
        f"📋 你的教練風格：\n"
        f"• 語氣：{tone_label}\n"
        f"• 溝通方式：{style_label}\n"
        f"• 引用頻率：{quote_label}\n\n"
        f"從現在開始，我就是你的專屬教練了 💪\n"
        f"有什麼想聊的，直接說吧！\n\n"
        f"（隨時可以用 /setting 重新調整教練風格）"
    )

# ============================
# Dify inputs 組裝
# ============================

def build_dify_inputs(profile: dict) -> dict:
    tone_map = {"strict": "嚴格督促", "gentle": "溫柔支持", "balanced": "平衡理性"}
    style_map = {"direct": "直接說重點", "exploratory": "循循善誘、引導探索"}
    quote_map = {"often": "頻繁引用名言、學術理論或研究數據來增加說服力", "sometimes": "偶爾適時引用即可", "never": "不需要引用，保持簡單直白"}

    return {
        "user_name": profile.get('display_name') or '用戶',
        "coach_tone": tone_map.get(profile.get('coach_tone', 'balanced'), '平衡理性'),
        "coach_style": style_map.get(profile.get('coach_style', 'exploratory'), '循循善誘'),
        "quote_freq": quote_map.get(profile.get('quote_freq', 'sometimes'), '偶爾適時引用即可'),
    }

# ============================
# Dify API 呼叫（含備援）
# ============================

def call_dify(api_key: str, user_id: str, text: str, conversation_id: str | None, inputs: dict) -> dict:
    url = f'{DIFY_API_URL}/chat-messages'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }
    data = {
        "inputs": inputs,
        "query": text,
        "response_mode": "blocking",
        "user": user_id,
    }
    if conversation_id:
        data["conversation_id"] = conversation_id

    response = requests.post(url, headers=headers, json=data, timeout=60)
    response.raise_for_status()
    return response.json()

def ask_dify(user_id: str, text: str, profile: dict) -> str:
    conversation_id = get_conversation_id(user_id)
    inputs = build_dify_inputs(profile)

    # 主要 API
    try:
        result = call_dify(DIFY_API_KEY, user_id, text, conversation_id, inputs)
        new_conv_id = result.get('conversation_id')
        if new_conv_id:
            save_conversation_id(user_id, new_conv_id)
        answer = result.get('answer', '').strip()
        return answer if answer else "我想到一半忘記說什麼了，請再問我一次！"

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[Dify Primary] 連線問題: {e} | user={user_id}")
        # 嘗試備援
        if DIFY_API_KEY_FALLBACK:
            try:
                print(f"[Dify Fallback] 啟動備援 | user={user_id}")
                result = call_dify(DIFY_API_KEY_FALLBACK, user_id, text, None, inputs)  # 備援不帶 conversation_id（不同 app）
                answer = result.get('answer', '').strip()
                return ("⚡️ 我暫時切換到備用系統回答你：\n\n" + answer) if answer else "備援系統也沒回應，請稍後再試！"
            except Exception as fe:
                print(f"[Dify Fallback] 失敗: {fe}")

        return (
            "☕ 我剛剛去泡了杯茶回來，結果忘記你問什麼了...\n\n"
            "請稍等一下再試試看！如果一直這樣，可以聯絡開發者 Chris 幫你看看哦 🙏"
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

# ============================
# 指令處理
# ============================

HELP_TEXT = """🤖 指令說明：

/reset    — 清除對話記憶，重新開始
/setting  — 重新設定教練風格
/profile  — 查看目前的設定
/help     — 顯示這個說明

直接輸入文字就能和我對話！"""

def handle_command(user_id: str, text: str, profile: dict) -> str | None:
    cmd = text.strip().lower()

    if cmd == '/reset':
        reset_conversation(user_id)
        return "🔄 對話記憶已清除！我們重新開始吧，有什麼想聊的？"

    if cmd == '/help':
        return HELP_TEXT

    if cmd == '/setting':
        # 重置問卷（保留名字），重新走教練偏好問卷
        save_profile(user_id, onboarding_done=0, onboarding_step=2)  # 從語氣開始問
        return ONBOARDING_STEPS[1]['question']

    if cmd == '/profile':
        name = profile.get('display_name') or '未設定'
        tone_label = {"strict": "嚴格督促", "gentle": "溫柔支持", "balanced": "平衡理性"}.get(profile.get('coach_tone', ''), '未設定')
        style_label = {"direct": "直接說重點", "exploratory": "循循善誘"}.get(profile.get('coach_style', ''), '未設定')
        quote_label = {"often": "常常引用", "sometimes": "偶爾引用", "never": "不引用"}.get(profile.get('quote_freq', ''), '未設定')
        return (
            f"📋 你的教練設定：\n\n"
            f"👤 名字：{name}\n"
            f"🎯 語氣：{tone_label}\n"
            f"💬 溝通方式：{style_label}\n"
            f"📚 引用頻率：{quote_label}\n\n"
            f"用 /setting 可以重新調整～"
        )

    return None

# ============================
# Line Webhook
# ============================

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
    profile = get_profile(user_id)

    # 1. 指令優先
    command_response = handle_command(user_id, user_text, profile)
    if command_response:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=command_response))
        return

    # 2. Onboarding 問卷（新用戶）
    onboarding_response = handle_onboarding(user_id, user_text, profile)
    if onboarding_response is not None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=onboarding_response))
        return

    # 3. 正常 AI 對話
    profile = get_profile(user_id)  # 重新讀取（可能剛被 onboarding 更新）
    ai_response = ask_dify(user_id, user_text, profile)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=ai_response))

# ============================
# 健康檢查
# ============================

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
