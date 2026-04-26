import os
import sqlite3
import threading
import requests
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn

app = FastAPI()

# ============================
# 環境變數
# ============================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')

# 教練設定（從環境變數讀取）
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
    'often:多一點:頻繁引用名言、學術理論|sometimes:偶爾就好:偶爾適時引用即可|never:不用:不需要引用'
))

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("錯誤: 缺少必要的環境變數")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

# ============================
# Base44 API 函數
# ============================

def call_backend_function(func_name, payload):
    """呼叫 Base44 backend function"""
    try:
        url = f"{BASE44_API_URL}/functions/{func_name}"
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"[Base44] {func_name} 失敗: {resp.status_code}")
            return None
    except Exception as e:
        print(f"[Base44] {func_name} 錯誤: {e}")
        return None

def sync_user_to_base44(line_user_id, profile):
    """同步用戶到 Base44 LgatUser"""
    return call_backend_function('syncUser', {
        'line_user_id': line_user_id,
        'display_name': profile.get('display_name', ''),
        'coach_tone': profile.get('coach_tone', 'balanced'),
        'coach_style': profile.get('coach_style', 'exploratory'),
        'quote_freq': profile.get('quote_freq', 'sometimes'),
        'total_messages': profile.get('total_messages', 0),
    })

def save_goal_to_base44(line_user_id, display_name, title, goal_type='short', description='', target_date=''):
    """儲存目標到 Base44 LgatGoal"""
    return call_backend_function('saveGoalOrEvent', {
        'entity_type': 'goal',
        'line_user_id': line_user_id,
        'display_name': display_name,
        'title': title,
        'type': goal_type,
        'description': description,
        'target_date': target_date,
    })

def save_event_to_base44(line_user_id, display_name, title, event_type='todo', due_date='', recurrence='none'):
    """儲存事件到 Base44 LgatEvent"""
    return call_backend_function('saveGoalOrEvent', {
        'entity_type': 'event',
        'line_user_id': line_user_id,
        'display_name': display_name,
        'title': title,
        'type': event_type,
        'due_date': due_date,
        'recurrence': recurrence,
    })

# ============================
# SQLite 初始化
# ============================

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_conversations (
            line_user_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
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
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ============================
# DB 輔助函數
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
    c.execute('''
        INSERT INTO user_conversations (line_user_id, conversation_id, updated_at)
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
    return {
        'line_user_id': line_user_id,
        'display_name': '',
        'coach_tone': 'balanced',
        'coach_style': 'exploratory',
        'quote_freq': 'sometimes',
        'onboarding_done': 0,
        'onboarding_step': 0,
        'total_messages': 0,
    }

def save_profile(line_user_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO user_profiles (line_user_id) VALUES (?)', (line_user_id,))
    for k, v in kwargs.items():
        c.execute(
            f'UPDATE user_profiles SET {k} = ?, updated_at = CURRENT_TIMESTAMP WHERE line_user_id = ?',
            (v, line_user_id)
        )
    conn.commit()
    conn.close()

# ============================
# LINE Loading Animation
# ============================

def send_loading_animation(user_id, seconds=20):
    """呼叫 LINE loading animation API"""
    try:
        requests.post(
            'https://api.line.me/v2/bot/chat/loading/start',
            headers={
                'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
                'Content-Type': 'application/json'
            },
            json={'chatId': user_id, 'loadingSeconds': seconds},
            timeout=5
        )
    except Exception as e:
        print(f"[LINE Loading] 失敗: {e}")

# ============================
# Onboarding 問卷
# ============================

def _tone_question():
    return "❷ 你喜歡什麼樣的教練語氣？\n\n" + '\n'.join(
        f"{i} {v['label']}" for i, v in TONE_OPTIONS.items()
    )

def _style_question():
    return "❸ 你習慣哪種溝通方式？\n\n" + '\n'.join(
        f"{i} {v['label']}" for i, v in STYLE_OPTIONS.items()
    )

def _quote_question():
    return "❹ 你喜歡引用嗎？\n\n" + '\n'.join(
        f"{i} {v['label']}" for i, v in QUOTE_OPTIONS.items()
    )

def handle_onboarding(line_user_id, text, profile):
    """Onboarding 邏輯，回傳 None 表示完成，回傳字串表示進行中"""
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    if step == 0:
        try:
            line_name = line_bot_api.get_profile(line_user_id).display_name
        except:
            line_name = ''
        
        if line_name:
            save_profile(line_user_id, display_name=line_name, onboarding_step=2)
            return f"👋 嗨 {line_name}！我是澄若水 🌊\n\n" + _tone_question()
        else:
            save_profile(line_user_id, onboarding_step=1)
            return "👋 我是澄若水 🌊\n\n❶ 你怎麼稱呼你自己？"

    if step == 1:
        answer = text.strip()
        if not answer:
            return "請輸入你的名字 😊"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return "很高興認識你！\n\n" + _tone_question()

    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return "請輸入數字 😊\n\n" + _tone_question()
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return "請輸入數字 😊\n\n" + _style_question()
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return "請輸入數字 😊\n\n" + _quote_question()
        save_profile(line_user_id, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)
        p = get_profile(line_user_id)
        return f"✨ 設定完成！\n\n{p['display_name']} 的教練設定已準備就緒 💪"

    return None

# ============================
# Dify 處理
# ============================

def build_dify_inputs(profile):
    import datetime, zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Taipei")
    now = datetime.datetime.now(tz)
    current_time = now.strftime("%Y年%m月%d日 %H:%M（%A）")

    tone_dify = next((v['dify'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '平衡理性')
    style_dify = next((v['dify'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '循循善誘')
    quote_dify = next((v['dify'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '偶爾適時引用')
    
    return {
        "user_name": profile.get('display_name') or '用戶',
        "coach_tone": tone_dify,
        "coach_style": style_dify,
        "quote_freq": quote_dify,
        "current_time": current_time,
    }

def call_dify(api_key, user_id, text, conversation_id, inputs):
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
    response = requests.post(url, headers=headers, json=data, timeout=120)
    response.raise_for_status()
    return response.json()


# ============================
# Base44 Backend Function 呼叫
# ============================

def call_backend_function(endpoint: str, payload: dict):
    """呼叫 Base44 backend function"""
    try:
        BASE44_APP_URL = os.environ.get('BASE44_APP_URL', 'https://app-ffa38ee7.base44.app')
        url = f'{BASE44_APP_URL}/functions/{endpoint}'
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[Base44] {endpoint} 失敗: {e}")
        return None

def sync_user_to_base44(line_user_id: str, display_name: str, profile: dict, total_messages: int):
    """同步用戶資料到 Base44"""
    payload = {
        "line_user_id": line_user_id,
        "display_name": display_name,
        "coach_tone": profile.get('coach_tone'),
        "coach_style": profile.get('coach_style'),
        "quote_freq": profile.get('quote_freq'),
        "total_messages": total_messages,
        "reminder_enabled": profile.get('reminder_enabled', False),
        "reminder_time": profile.get('reminder_time', '08:00'),
    }
    return call_backend_function("syncUser", payload)

def save_goal_to_base44(line_user_id: str, display_name: str, **kwargs):
    """儲存目標/事件到 Base44"""
    payload = {
        "line_user_id": line_user_id,
        "display_name": display_name,
        **kwargs
    }
    return call_backend_function("saveGoalOrEvent", payload)

# 智能偵測目標/事件關鍵字
def detect_goal_in_response(response_text: str) -> dict:
    """從 Dify 回應中偵測是否提到了目標設定
    回傳格式: {"has_goal": bool, "goal_info": {...}}
    """
    # 簡單的關鍵字偵測（後續可用 LLM 改進）
    keywords = {
        'goal': ['目標', '目指', '達成', '完成', '想要', '計畫'],
        'event': ['事件', '待辦', '提醒', '習慣', '里程碑', '截止', '期限'],
    }
    
    for entity_type, words in keywords.items():
        if any(w in response_text for w in words):
            return {"detected": entity_type, "confidence": 0.5}
    
    return {"detected": None, "confidence": 0}

def ask_dify(user_id, text, profile):
    conversation_id = get_conversation_id(user_id)
    inputs = build_dify_inputs(profile)

    try:
        result = call_dify(DIFY_API_KEY, user_id, text, conversation_id, inputs)
        new_conv_id = result.get('conversation_id')
        if new_conv_id:
            save_conversation_id(user_id, new_conv_id)
        answer = result.get('answer', '').strip()
        return answer if answer else "🤔 我想到一半忘記了，請再問我一次！"

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[Dify Primary] 連線問題: {e}")
        if DIFY_API_KEY_FALLBACK:
            try:
                result = call_dify(DIFY_API_KEY_FALLBACK, user_id, text, None, inputs)
                answer = result.get('answer', '').strip()
                if answer:
                    return "⚡️ 備用系統回答：\n\n" + answer
                return "😓 備援系統也沒回應，請稍後再試！"
            except Exception as fe:
                print(f"[Dify Fallback] 失敗: {fe}")
        return "☕ 我去泡茶忘記了... 請稍等再試！"

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '未知'
        return f"🔧 出錯了（{status}），請稍後再試"

    except Exception as e:
        print(f"[Dify] 錯誤: {e}")
        return "😵 我靈魂出竅了，請再問一次！"

# ============================
# 指令處理
# ============================

HELP_TEXT = (
    "🤖 指令說明：\n\n"
    "🔄 /reset - 清除記憶\n"
    "⚙️ /setting - 重新設定\n"
    "📋 /profile - 查看設定\n"
    "❓ /help - 說明"
)

def handle_command(user_id, text, profile):
    cmd = text.strip().lower()

    if cmd == '/reset':
        reset_conversation(user_id)
        return "🔄 記憶已清除！"

    if cmd == '/help':
        return HELP_TEXT

    if cmd == '/setting':
        save_profile(user_id, onboarding_done=0, onboarding_step=2)
        return "⚙️ 重新設定～\n\n" + _tone_question()

    if cmd == '/profile':
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '未設定')
        return f"📋 你的設定：\n👤 {profile.get('display_name')}\n🎯 {tone_label}\n💬 {style_label}\n📚 {quote_label}"

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

    # 2. Onboarding
    onboarding_response = handle_onboarding(user_id, user_text, profile)
    if onboarding_response is not None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=onboarding_response))
        return

    # 3. AI 對話：背景執行 + push_message
    def process_and_push():
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        
        # 同步用戶到 Base44
        current_profile["total_messages"] = current_profile.get("total_messages", 0) + 1
        save_profile(user_id, total_messages=current_profile["total_messages"])
        sync_user_to_base44(user_id, current_profile)
        
        # 偵測目標/事件關鍵字
        entity_type, _ = _detect_entity(user_text)
        if entity_type == 'goal':
            save_goal_to_base44(user_id, current_profile.get('display_name', ''), user_text, goal_type='short')
        elif entity_type in ['habit', 'todo']:
            save_event_to_base44(user_id, current_profile.get('display_name', ''), user_text, event_type=entity_type or 'todo')
        
        ai_response = ask_dify(user_id, user_text, current_profile)
        line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))

    threading.Thread(target=process_and_push, daemon=True).start()

def _detect_entity(text):
    """簡單的關鍵字偵測"""
    keywords_goal = ['目標', '想要', '希望', '計畫', '打算']
    keywords_habit = ['習慣', '每天', '每週', '養成']
    keywords_todo = ['要做', '待辦', '提醒']
    
    for kw in keywords_goal:
        if kw in text:
            return ('goal', text)
    for kw in keywords_habit:
        if kw in text:
            return ('habit', text)
    for kw in keywords_todo:
        if kw in text:
            return ('todo', text)
    return (None, None)

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
