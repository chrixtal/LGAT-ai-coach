import os
import sqlite3
import threading
import requests
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn
import re

app = FastAPI()

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')

# ============================
# 教練設定（從環境變數讀取）
# ============================
def _parse_options(env_val):
    result = {}
    for i, raw in enumerate(env_val.split('|'), 1):
        parts = raw.strip().split(':')
        if len(parts) == 3:
            result[str(i)] = {'value': parts[0], 'label': parts[1], 'dify': parts[2]}
    return result

TONE_OPTIONS = _parse_options(os.environ.get(
    'COACH_TONE_OPTIONS',
    'strict:嚴格督促型:嚴格督促|gentle:溫柔支持型:溫柔支持|balanced:平衡理性型:平衡理性'
))
STYLE_OPTIONS = _parse_options(os.environ.get(
    'COACH_STYLE_OPTIONS',
    'direct:直接說重點:直接說重點|exploratory:循循善誘:循循善誘、引導探索'
))
QUOTE_OPTIONS = _parse_options(os.environ.get(
    'COACH_QUOTE_OPTIONS',
    'often:多一點:頻繁引用|sometimes:偶爾:偶爾適時引用|never:不用:不需要引用'
))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- SQLite 初始化 ---
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_conversations (
        line_user_id TEXT PRIMARY KEY,
        conversation_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
        line_user_id TEXT PRIMARY KEY,
        display_name TEXT,
        coach_tone TEXT DEFAULT 'balanced',
        coach_style TEXT DEFAULT 'exploratory',
        quote_freq TEXT DEFAULT 'sometimes',
        onboarding_done INTEGER DEFAULT 0,
        onboarding_step INTEGER DEFAULT 0,
        total_messages INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db()

# ============================
# DB helpers
# ============================

def get_conversation_id(line_user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT conversation_id FROM user_conversations WHERE line_user_id = ?', (line_user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_conversation_id(line_user_id, conversation_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO user_conversations (line_user_id, conversation_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(line_user_id) DO UPDATE SET
            conversation_id = excluded.conversation_id,
            updated_at = CURRENT_TIMESTAMP
    ''', (line_user_id, conversation_id))
    conn.commit()
    conn.close()

def reset_conversation(line_user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_conversations WHERE line_user_id = ?', (line_user_id,))
    conn.commit()
    conn.close()

def get_profile(line_user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM user_profiles WHERE line_user_id = ?', (line_user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {'line_user_id': line_user_id, 'display_name': '', 'coach_tone': 'balanced',
            'coach_style': 'exploratory', 'quote_freq': 'sometimes',
            'onboarding_done': 0, 'onboarding_step': 0, 'total_messages': 0}

def save_profile(line_user_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO user_profiles (line_user_id) VALUES (?)', (line_user_id,))
    for k, v in kwargs.items():
        c.execute(f'UPDATE user_profiles SET {k} = ?, updated_at = CURRENT_TIMESTAMP WHERE line_user_id = ?', (v, line_user_id))
    conn.commit()
    conn.close()

# ============================
# LINE helpers
# ============================

def get_line_display_name(user_id):
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name or ''
    except:
        return ''

def send_loading_animation(user_id, seconds=20):
    try:
        requests.post(
            'https://api.line.me/v2/bot/chat/loading/start',
            headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
            json={'chatId': user_id, 'loadingSeconds': seconds},
            timeout=5
        )
    except Exception as e:
        print(f"[LINE Loading] 失敗: {e}")

# ============================
# Base44 API 同步
# ============================

def detect_goal_keywords(text):
    keywords = ['目標', '想要', '計畫', '希望', '達成', '完成', '目的', 'goal']
    return any(kw in text for kw in keywords)

def detect_event_keywords(text):
    keywords = ['習慣', '待辦', '提醒', '打卡', '每日', '每週', '事件', 'todo', 'habit']
    return any(kw in text for kw in keywords)

def extract_goal_info(text):
    lines = text.split('\n')
    title = lines[0][:100] if lines else '未命名'
    goal_type = 'short'
    if any(kw in text for kw in ['一個月', '近期', '這週']):
        goal_type = 'short'
    elif any(kw in text for kw in ['三個月', '六個月', '這季']):
        goal_type = 'medium'
    else:
        goal_type = 'long'
    return {'title': title, 'description': text, 'type': goal_type}

def sync_user_to_base44(user_id, profile, total_messages):
    try:
        payload = {
            'line_user_id': user_id,
            'display_name': profile.get('display_name', ''),
            'coach_tone': profile.get('coach_tone', 'balanced'),
            'coach_style': profile.get('coach_style', 'exploratory'),
            'quote_freq': profile.get('quote_freq', 'sometimes'),
            'total_messages': total_messages,
            'plan': 'free'
        }
        resp = requests.post(f'{BASE44_API_URL}/functions/syncUser', json=payload, timeout=5)
        if resp.status_code != 200:
            print(f"[Base44 syncUser] {resp.status_code}")
    except Exception as e:
        print(f"[Base44 syncUser] {e}")

def save_goal_to_base44(user_id, display_name, goal_info):
    try:
        payload = {
            'line_user_id': user_id,
            'display_name': display_name,
            'entity_type': 'goal',
            'title': goal_info['title'],
            'description': goal_info['description'],
            'type': goal_info['type']
        }
        resp = requests.post(f'{BASE44_API_URL}/functions/saveGoalOrEvent', json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44] 儲存目標 | user={user_id}")
    except Exception as e:
        print(f"[Base44 save] {e}")

def save_event_to_base44(user_id, display_name, event_title, event_type):
    try:
        payload = {
            'line_user_id': user_id,
            'display_name': display_name,
            'entity_type': 'event',
            'title': event_title[:50],
            'type': event_type
        }
        resp = requests.post(f'{BASE44_API_URL}/functions/saveGoalOrEvent', json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44] 儲存事件 | user={user_id}")
    except Exception as e:
        print(f"[Base44 save] {e}")

# ============================
# Onboarding
# ============================

def _build_options_text(options):
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
    return '\n'.join(f"{emojis[i]} {v['label']}" for i, v in enumerate(options.values()))

def _tone_question():
    return "❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n" + _build_options_text(TONE_OPTIONS)

def _style_question():
    return "❸ 你習慣哪種溝通方式？\n\n請輸入數字：\n" + _build_options_text(STYLE_OPTIONS)

def _quote_question():
    return "❹ 最後一個問題！\n\n你喜歡我引用名言、理論或研究嗎？\n\n請輸入數字：\n" + _build_options_text(QUOTE_OPTIONS)

def handle_onboarding(line_user_id, text, profile):
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    if step == 0:
        line_name = get_line_display_name(line_user_id)
        if line_name:
            save_profile(line_user_id, display_name=line_name, onboarding_step=2)
            return f"👋 嗨，{line_name}！我是你的 AI 生活教練 澄若水 🌊\n\n在開始之前，想先了解你喜歡什麼樣的教練風格！\n\n" + _tone_question()
        else:
            save_profile(line_user_id, onboarding_step=1)
            return "👋 嗨！我是你的 AI 生活教練 澄若水 🌊\n\n在開始之前，我想先多了解你一點！\n\n❶ 你怎麼稱呼你自己呢？"

    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return "很高興認識你！🙌\n\n" + _tone_question()

    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 1-{len(TONE_OPTIONS)} 😊\n\n" + _tone_question()
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 1-{len(STYLE_OPTIONS)} 😊\n\n" + _style_question()
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 1-{len(QUOTE_OPTIONS)} 😊\n\n" + _quote_question()
        save_profile(line_user_id, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)
        p = get_profile(line_user_id)
        name = p['display_name'] or '你'
        return f"太棒了，{name}！✨ 設定完成！\n\n從現在開始，我就是你的專屬教練了 💪"

    return None

# ============================
# Dify
# ============================

def build_dify_inputs(profile):
    import datetime, zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Taipei")
    now = datetime.datetime.now(tz)
    current_time = now.strftime("%Y年%m月%d日 %H:%M（%A）")

    tone_dify = next((v['dify'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '平衡理性')
    style_dify = next((v['dify'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '循循善誘')
    quote_dify = next((v['dify'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '偶爾')
    
    return {
        "user_name": profile.get('display_name') or '用戶',
        "coach_tone": tone_dify,
        "coach_style": style_dify,
        "quote_freq": quote_dify,
        "current_time": current_time,
    }

def call_dify(api_key, user_id, text, conversation_id, inputs):
    url = f'{DIFY_API_URL}/chat-messages'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    data = {"inputs": inputs, "query": text, "response_mode": "blocking", "user": user_id}
    if conversation_id:
        data["conversation_id"] = conversation_id
    response = requests.post(url, headers=headers, json=data, timeout=120)
    response.raise_for_status()
    return response.json()


# ============================
# Base44 API 呼叫
# ============================

def sync_user_to_base44(user_id, display_name, profile):
    """同步用戶資料到 Base44 後台"""
    try:
        api_url = "https://app-ffa38ee7.base44.app/functions/syncUser"
        payload = {
            "line_user_id": user_id,
            "display_name": display_name,
            "coach_tone": profile.get('coach_tone', 'balanced'),
            "coach_style": profile.get('coach_style', 'exploratory'),
            "quote_freq": profile.get('quote_freq', 'sometimes'),
            "total_messages": profile.get('total_messages', 0) + 1,
        }
        resp = requests.post(api_url, json=payload, timeout=5)
        if resp.ok:
            print(f"[Base44] 用戶 {user_id} 資料同步成功")
        else:
            print(f"[Base44] syncUser 失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] syncUser 例外: {e}")

def detect_and_save_goal_or_event(user_id, display_name, text):
    """偵測目標/事件關鍵詞並儲存"""
    goal_keywords = ['想要', '目標', '計畫', '打算', '準備', '要達成', '想達到']
    event_keywords = ['習慣', '待辦', '提醒', '記得', '別忘了', '提醒我']
    
    text_lower = text.lower()
    
    # 偵測目標
    if any(kw in text_lower for kw in goal_keywords):
        try:
            api_url = "https://app-ffa38ee7.base44.app/functions/saveGoalOrEvent"
            payload = {
                "entity_type": "goal",
                "line_user_id": user_id,
                "display_name": display_name,
                "title": text[:50],  # 用訊息的前 50 字作為標題
                "description": text,
                "type": "short",  # 預設短期，用戶後續可在後台修改
            }
            resp = requests.post(api_url, json=payload, timeout=5)
            if resp.ok:
                print(f"[Base44] 目標已儲存: {text[:30]}")
            else:
                print(f"[Base44] saveGoalOrEvent 失敗: {resp.status_code}")
        except Exception as e:
            print(f"[Base44] saveGoalOrEvent 例外: {e}")
    
    # 偵測事件
    if any(kw in text_lower for kw in event_keywords):
        try:
            api_url = "https://app-ffa38ee7.base44.app/functions/saveGoalOrEvent"
            payload = {
                "entity_type": "event",
                "line_user_id": user_id,
                "display_name": display_name,
                "title": text[:50],
                "type": "todo",
                "recurrence": "none",
            }
            resp = requests.post(api_url, json=payload, timeout=5)
            if resp.ok:
                print(f"[Base44] 事件已儲存: {text[:30]}")
            else:
                print(f"[Base44] saveGoalOrEvent 失敗: {resp.status_code}")
        except Exception as e:
            print(f"[Base44] saveGoalOrEvent 例外: {e}")


def ask_dify(user_id, text, profile):
    conversation_id = get_conversation_id(user_id)
    inputs = build_dify_inputs(profile)

    try:
        result = call_dify(DIFY_API_KEY, user_id, text, conversation_id, inputs)
        new_conv_id = result.get('conversation_id')
        if new_conv_id:
            save_conversation_id(user_id, new_conv_id)
        answer = result.get('answer', '').strip()
        return answer if answer else "🤔 我想到一半忘記說什麼了，請再問我一次！"
    except Exception as e:
        print(f"[Dify] {e}")
        return "😵 暫時沒回應，請稍後再試！"

# ============================
# 指令
# ============================

HELP_TEXT = "🤖 指令說明：\n\n🔄 /reset - 清除記憶\n⚙️ /setting - 重新設定\n📋 /profile - 查看設定\n❓ /help - 說明"

def handle_command(user_id, text, profile):
    cmd = text.strip().lower()
    if cmd == '/reset':
        reset_conversation(user_id)
        return "🔄 對話記憶已清除！"
    if cmd == '/help':
        return HELP_TEXT
    if cmd == '/setting':
        save_profile(user_id, onboarding_done=0, onboarding_step=2)
        return "⚙️ 重新設定～\n\n" + _tone_question()
    if cmd == '/profile':
        name = profile.get('display_name') or '未設定'
        return f"📋 你的設定：\n👤 {name}\n\n用 /setting 可以重新調整～"
    return None

# ============================
# LINE Webhook
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

    # 3. 正常 AI 對話 + Base44 同步
    replied_flag = threading.Event()

    def process_and_push():
        try:
            # 同步用戶資料到 Base44（每次對話都同步）
            current_profile = get_profile(user_id)
            sync_user_to_base44(user_id, current_profile.get('display_name', '未知'), current_profile)
            
            # 偵測並儲存目標/事件
            detect_and_save_goal_or_event(user_id, current_profile.get('display_name', '未知'), user_text)
            
            # 送 loading animation
            send_loading_animation(user_id, seconds=60)
            
            # 呼叫 Dify
            ai_response = ask_dify(user_id, user_text, current_profile)
            
            if not replied_flag.is_set():
                replied_flag.set()
                line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))
        except Exception as e:
            print(f"[handle_message] 未預期錯誤: {e}")
            if not replied_flag.is_set():
                replied_flag.set()
                line_bot_api.push_message(user_id, TextSendMessage(text="😵 出了點小問題，請再試一次！"))

    threading.Thread(target=process_and_push, daemon=True).start()
@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
