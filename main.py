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

def sync_user_to_base44(line_user_id, display_name, coach_tone, coach_style, quote_freq, total_messages=0):
    """呼叫 syncUser function 把用戶資料存到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_API_URL}/functions/syncUser',
            json={
                'line_user_id': line_user_id,
                'display_name': display_name,
                'coach_tone': coach_tone,
                'coach_style': coach_style,
                'quote_freq': quote_freq,
                'total_messages': total_messages,
            },
            timeout=10
        )
        if resp.ok:
            print(f"[Base44] syncUser: {line_user_id} ✓")
        else:
            print(f"[Base44] syncUser 失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] syncUser 錯誤: {e}")

def save_goal_or_event(line_user_id, display_name, entity_type, **fields):
    """呼叫 saveGoalOrEvent 存目標或事件"""
    try:
        resp = requests.post(
            f'{BASE44_API_URL}/functions/saveGoalOrEvent',
            json={
                'line_user_id': line_user_id,
                'display_name': display_name,
                'entity_type': entity_type,
                **fields
            },
            timeout=10
        )
        if resp.ok:
            result = resp.json()
            print(f"[Base44] {entity_type} 已儲存: {line_user_id}")
            return result.get('result')
        else:
            print(f"[Base44] saveGoalOrEvent 失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] saveGoalOrEvent 錯誤: {e}")

def detect_goals_and_events(user_text):
    """從用戶的對話中偵測目標/事件關鍵詞，回傳 [(type, fields), ...]"""
    results = []
    text = user_text.lower()
    
    # 目標相關關鍵詞
    goal_keywords = ['目標', '想要', '計畫', '夢想', '希望', '想達到', '想完成', '準備']
    # 事件/習慣相關關鍵詞
    event_keywords = ['完成', '做了', '跑步', '閱讀', '冥想', '睡眠', '喝水', '運動', '學習', '待辦', '提醒']
    
    # 簡單的關鍵詞匹配（可以用 NLP 更精準，但先用簡單的）
    if any(kw in text for kw in goal_keywords):
        # 嘗試抽取目標標題（用簡單的啟發式方法）
        title = user_text[:30] if len(user_text) <= 30 else user_text[:30] + '...'
        results.append(('goal', {
            'title': title,
            'description': user_text,
            'type': 'short'  # 預設短期
        }))
    
    if any(kw in text for kw in event_keywords):
        title = user_text[:30] if len(user_text) <= 30 else user_text[:30] + '...'
        results.append(('event', {
            'title': title,
            'type': 'todo',
            'note': user_text
        }))
    
    return results


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
            sync_user_to_base44(
                    user_id,
                    current_profile.get('display_name', '未知'),
                    current_profile.get('coach_tone', 'balanced'),
                    current_profile.get('coach_style', 'exploratory'),
                    current_profile.get('quote_freq', 'sometimes'),
                    current_profile.get('total_messages', 0)
                )
            
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
