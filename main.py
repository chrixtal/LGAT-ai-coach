import os
import sqlite3
import threading
import requests
import json
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn

# Backend function API
BASE44_APP_URL = os.environ.get('BASE44_APP_URL', 'https://app-ffa38ee7.base44.app')

app = FastAPI()

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
BASE44_APP_ID = "69e35caa4e5d9a67dd7dd6e1"
BASE44_API_URL = "https://app-ffa38ee7.base44.app/functions"

def sync_user_to_base44(user_id, display_name, coach_tone, coach_style, quote_freq, total_messages):
    """每次對話時同步用戶資料到 Base44"""
    try:
        resp = requests.post(
            f"{BASE44_API_URL}/syncUser",
            json={
                "line_user_id": user_id,
                "display_name": display_name,
                "coach_tone": coach_tone,
                "coach_style": coach_style,
                "quote_freq": quote_freq,
                "total_messages": total_messages,
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Base44] 用戶 {user_id} 同步成功")
        else:
            print(f"[Base44] 同步失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44] 同步異常: {e}")

def save_goal_to_base44(user_id, display_name, goal_title, goal_type="short"):
    """儲存目標到 Base44"""
    try:
        resp = requests.post(
            f"{BASE44_API_URL}/saveGoalOrEvent",
            json={
                "entity_type": "goal",
                "line_user_id": user_id,
                "display_name": display_name,
                "title": goal_title,
                "type": goal_type,
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Base44] 目標 '{goal_title}' 已儲存")
        else:
            print(f"[Base44] 儲存失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] 儲存異常: {e}")
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("❌ 缺少必要環境變數")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- SQLite ---
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_conversations
        (line_user_id TEXT PRIMARY KEY, conversation_id TEXT,
         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles
        (line_user_id TEXT PRIMARY KEY, display_name TEXT,
         coach_tone TEXT DEFAULT 'balanced', coach_style TEXT DEFAULT 'exploratory',
         quote_freq TEXT DEFAULT 'sometimes',
         onboarding_done INTEGER DEFAULT 0, onboarding_step INTEGER DEFAULT 0,
         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

# ============================
# 教練設定（環境變數）
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
    'often:多一點:頻繁引用名言、學術理論或研究數據|sometimes:偶爾就好:偶爾適時引用即可|never:不用:不需要引用'
))

# ============================
# DB Helper Functions
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
        ON CONFLICT(line_user_id) DO UPDATE SET conversation_id = excluded.conversation_id,
        updated_at = CURRENT_TIMESTAMP''', (line_user_id, conversation_id))
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
    }

def save_profile(line_user_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO user_profiles (line_user_id) VALUES (?)', (line_user_id,))
    for k, v in kwargs.items():
        c.execute(f'UPDATE user_profiles SET {k} = ?, updated_at = CURRENT_TIMESTAMP WHERE line_user_id = ?', (v, line_user_id))
    conn.commit()
    conn.close()

# ============================
# LINE API Helpers
# ============================

def get_line_display_name(user_id):
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name or ''
    except Exception as e:
        print(f"[LINE] 無法取得暱稱: {e}")
        return ''

def send_loading_animation(user_id, seconds=20):
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
# Base44 Sync Functions
# ============================

def sync_user_to_base44(user_id, profile, total_messages=0):
    """同步用戶資料到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_APP_URL}/functions/syncUser',
            json={
                'line_user_id': user_id,
                'display_name': profile.get('display_name', ''),
                'coach_tone': profile.get('coach_tone', 'balanced'),
                'coach_style': profile.get('coach_style', 'exploratory'),
                'quote_freq': profile.get('quote_freq', 'sometimes'),
                'total_messages': total_messages,
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Sync] {user_id} 同步成功")
        else:
            print(f"[Sync] {user_id} 失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Sync] {user_id} 錯誤: {e}")

def save_goal_to_base44(user_id, display_name, title, goal_type='short'):
    """儲存目標到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_APP_URL}/functions/saveGoalOrEvent',
            json={
                'entity_type': 'goal',
                'line_user_id': user_id,
                'display_name': display_name,
                'title': title,
                'type': goal_type,
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Goal] {user_id} 目標已儲存: {title}")
        else:
            print(f"[Goal] 儲存失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Goal] 錯誤: {e}")

def save_event_to_base44(user_id, display_name, title, event_type='todo'):
    """儲存事件到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_APP_URL}/functions/saveGoalOrEvent',
            json={
                'entity_type': 'event',
                'line_user_id': user_id,
                'display_name': display_name,
                'title': title,
                'type': event_type,
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Event] {user_id} 事件已儲存: {title}")
        else:
            print(f"[Event] 儲存失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Event] 錯誤: {e}")

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
    return "❹ 最後一個問題！你喜歡我引用名言、學術理論嗎？\n\n請輸入數字：\n" + _build_options_text(QUOTE_OPTIONS)

def handle_onboarding(user_id, text, profile):
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    if step == 0:
        line_name = get_line_display_name(user_id)
        if line_name:
            save_profile(user_id, display_name=line_name, onboarding_step=2)
            return f"👋 嗨，{line_name}！我是澄若水 🌊\n\n先了解你喜歡什麼樣的教練風格～\n\n" + _tone_question()
        else:
            save_profile(user_id, onboarding_step=1)
            return "👋 嗨！我是澄若水 🌊\n\n❶ 你怎麼稱呼你自己呢？"

    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！"
        save_profile(user_id, display_name=answer, onboarding_step=2)
        return f"很高興認識你！🙌\n\n" + _tone_question()

    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入有效的數字～\n\n" + _tone_question()
        save_profile(user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入有效的數字～\n\n" + _style_question()
        save_profile(user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入有效的數字～\n\n" + _quote_question()
        save_profile(user_id, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)
        p = get_profile(user_id)
        return f"太棒了！✨ 設定完成～\n\n從現在開始，我就是你的專屬教練了 💪\n\n有什麼想聊的，直接說吧！"

    return None

# ============================
# Dify Integration
# ============================

def build_dify_inputs(profile):
    import datetime, zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Taipei")
    now = datetime.datetime.now(tz)
    current_time = now.strftime("%Y年%m月%d日 %H:%M（%A）")

    tone_dify = next((v['dify'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '平衡理性')
    style_dify = next((v['dify'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '循循善誘、引導探索')
    quote_dify = next((v['dify'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '偶爾適時引用即可')
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
# Base44 API Helpers
# ============================

BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app/functions')

def call_base44_function(func_name, payload):
    """呼叫 Base44 backend function"""
    try:
        url = f'{BASE44_API_URL}/{func_name}'
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[Base44] {func_name} 失敗: {e}")
        return None

def sync_user_to_base44(line_user_id, profile):
    """將用戶資料同步到 Base44"""
    payload = {
        "line_user_id": line_user_id,
        "display_name": profile.get('display_name') or '',
        "coach_tone": profile.get('coach_tone') or 'balanced',
        "coach_style": profile.get('coach_style') or 'exploratory',
        "quote_freq": profile.get('quote_freq') or 'sometimes',
        "total_messages": profile.get('total_messages', 0) + 1,  # 加上本次
        "reminder_enabled": profile.get('reminder_enabled', False),
        "reminder_time": profile.get('reminder_time') or '08:00',
    }
    return call_base44_function('syncUser', payload)

def save_goal_to_base44(line_user_id, display_name, title, desc='', goal_type='short', target_date=''):
    """儲存目標到 Base44"""
    payload = {
        "entity_type": "goal",
        "line_user_id": line_user_id,
        "display_name": display_name,
        "title": title,
        "description": desc,
        "type": goal_type,
        "target_date": target_date,
    }
    return call_base44_function('saveGoalOrEvent', payload)

def save_event_to_base44(line_user_id, display_name, title, event_type='todo', due_date=''):
    """儲存事件到 Base44"""
    payload = {
        "entity_type": "event",
        "line_user_id": line_user_id,
        "display_name": display_name,
        "title": title,
        "type": event_type,
        "due_date": due_date,
    }
    return call_base44_function('saveGoalOrEvent', payload)

def detect_goal_or_event(text):
    """簡單的關鍵字偵測，看是否在設定目標/事件"""
    goal_keywords = ['目標', '我想', '計畫', '達成', '完成', '想要']
    event_keywords = ['待辦', '習慣', '打卡', '任務', '要做']
    
    for kw in goal_keywords:
        if kw in text:
            return 'goal'
    for kw in event_keywords:
        if kw in text:
            return 'event'
    return None


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
                    return "⚡️ 我暫時切換到備用系統回答你：\n\n" + answer
                return "😓 備援系統也沒回應，請稍後再試！"
            except Exception as fe:
                print(f"[Dify Fallback] 失敗: {fe}")
        return "☕ 我剛剛去泡了杯茶回來，請稍等一下再試試看！"

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '未知'
        print(f"[Dify] HTTP 錯誤 {status}")
        return f"🔧 出了點小問題（{status}），請稍後再試！"

    except Exception as e:
        print(f"[Dify] 未預期錯誤: {e}")
        return "😵 靈魂出竅了一下，請再問我一次！"

# ============================
# 指令
# ============================

HELP_TEXT = "🤖 指令：\n/reset - 清除對話\n/help - 說明\n/profile - 查看設定"

def handle_command(user_id, text, profile):
    cmd = text.strip().lower()

    if cmd == '/reset':
        reset_conversation(user_id)
        return "🔄 對話記憶已清除！"

    if cmd == '/help':
        return HELP_TEXT

    if cmd == '/setting':
        save_profile(user_id, onboarding_done=0, onboarding_step=2)
        return "⚙️ 我們來重新調整一下～\n\n" + _tone_question()

    if cmd == '/profile':
        name = profile.get('display_name') or '未設定'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '未設定')
        return f"📋 你的設定：\n👤 {name}\n🎯 {tone_label}\n💬 {style_label}\n📚 {quote_label}"

    return None


# ============================
# Backend Functions Integration
# ============================

BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_API_URL = f'https://app-ffa38ee7.base44.app/functions'

def sync_user_to_backend(user_id, profile):
    """同步用戶資料到 Base44"""
    try:
        payload = {
            'line_user_id': user_id,
            'display_name': profile.get('display_name') or '',
            'coach_tone': profile.get('coach_tone') or 'balanced',
            'coach_style': profile.get('coach_style') or 'exploratory',
            'quote_freq': profile.get('quote_freq') or 'sometimes',
            'total_messages': profile.get('total_messages', 0) + 1,
            'reminder_enabled': profile.get('reminder_enabled', False),
            'reminder_time': profile.get('reminder_time', '08:00'),
        }
        resp = requests.post(f'{BASE44_API_URL}/syncUser', json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44] User {user_id} synced")
        else:
            print(f"[Base44] Sync failed: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] sync_user error: {e}")

def try_save_goal_or_event(user_id, user_input, ai_response, profile):
    """偵測用戶輸入和 AI 回應，嘗試提取並儲存目標/事件"""
    try:
        # 簡單的關鍵字偵測
        name = profile.get('display_name') or '用戶'
        
        # 目標偵測
        goal_keywords = ['目標', '想要', '要達成', '計畫', '目的']
        if any(kw in user_input for kw in goal_keywords):
            # 如果用戶提到目標，嘗試從 AI 回應中提取信息
            # 這裡可以做更複雜的 NLP，現在先簡單偵測
            if '短期' in user_input or '1個月' in user_input or '一個月' in user_input:
                goal_type = 'short'
            elif '中期' in user_input or '3-6個月' in user_input:
                goal_type = 'medium'
            else:
                goal_type = 'long'
            
            payload = {
                'entity_type': 'goal',
                'line_user_id': user_id,
                'display_name': name,
                'title': user_input[:30],  # 取前 30 字作標題
                'description': ai_response[:100],
                'type': goal_type,
            }
            resp = requests.post(f'{BASE44_API_URL}/saveGoalOrEvent', json=payload, timeout=5)
            if resp.status_code == 200:
                print(f"[Base44] Goal saved for {user_id}")
        
        # 事件偵測
        event_keywords = ['待辦', '要做', '提醒我', '習慣', '打卡']
        if any(kw in user_input for kw in event_keywords):
            event_type = 'habit' if '習慣' in user_input else 'todo'
            payload = {
                'entity_type': 'event',
                'line_user_id': user_id,
                'display_name': name,
                'title': user_input[:30],
                'type': event_type,
                'recurrence': 'daily' if '每天' in user_input or '每日' in user_input else 'none',
            }
            resp = requests.post(f'{BASE44_API_URL}/saveGoalOrEvent', json=payload, timeout=5)
            if resp.status_code == 200:
                print(f"[Base44] Event saved for {user_id}")
    except Exception as e:
        print(f"[Base44] try_save_goal_or_event error: {e}")

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

    # 1. 指令
    command_response = handle_command(user_id, user_text, profile)
    if command_response:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=command_response))
        return

    # 2. Onboarding
    onboarding_response = handle_onboarding(user_id, user_text, profile)
    if onboarding_response is not None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=onboarding_response))
        return

    # 3. AI 對話（背景執行）
    replied_flag = threading.Event()

    def process_and_push():
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        msg_count = (current_profile.get('total_messages') or 0) + 1
        save_profile(user_id, total_messages=msg_count)
        current_profile['total_messages'] = msg_count

        # 簡單的意圖偵測：如果訊息包含特定關鍵字，自動存入對應的目標/事件
        text_lower = user_text.lower()
        if any(kw in text_lower for kw in ['目標', '想要', '計畫', '準備']):
            save_goal_to_base44(user_id, current_profile.get('display_name', ''), user_text[:30], goal_type='short')
        elif any(kw in text_lower for kw in ['完成', '打卡', '做', '習慣', '待辦']):
            save_event_to_base44(user_id, current_profile.get('display_name', ''), user_text[:30], 'todo')

        # 呼叫 Dify
        try:
            ai_response = ask_dify(user_id, user_text, current_profile)
        except Exception as e:
            print(f"[ask_dify] 錯誤: {e}")
            ai_response = "😵 出了點小問題，請再試一次！"

        # 同步用戶資料到 Base44
        sync_user_to_base44(user_id, current_profile)

        # 發送回應
        if not replied_flag.is_set():
            replied_flag.set()
            line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))

    threading.Thread(target=process_and_push, daemon=True).start()

    # 同步用戶資料到 Base44（非同步，不影響 LINE 回應速度）
    def sync_async():
        try:
            current_profile = get_profile(user_id)
            current_profile['total_messages'] = (current_profile.get('total_messages', 0) or 0) + 1
            sync_user_to_base44(
                user_id,
                current_profile.get('display_name', ''),
                current_profile.get('coach_tone', 'balanced'),
                current_profile.get('coach_style', 'exploratory'),
                current_profile.get('quote_freq', 'sometimes'),
                current_profile['total_messages']
            )
        except Exception as e:
            print(f"[sync] 失敗: {e}")
    threading.Thread(target=sync_async, daemon=True).start()

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
