import os
import sqlite3
import threading
import requests
import re
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

# ============================
# 教練設定
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
    'strict:嚴格督促型（推你一把，不留情面）:嚴格督促|gentle:溫柔支持型（像朋友一樣陪伴你）:溫柔支持|balanced:平衡理性型（視情況調整）:平衡理性'
))

STYLE_OPTIONS = _parse_options(os.environ.get(
    'COACH_STYLE_OPTIONS',
    'direct:直接說重點（我要答案，不要繞彎子）:直接說重點|exploratory:循循善誘（陪我慢慢想清楚）:循循善誘、引導探索'
))

QUOTE_OPTIONS = _parse_options(os.environ.get(
    'COACH_QUOTE_OPTIONS',
    'often:多一點，我喜歡有根據的東西:頻繁引用名言、學術理論或研究數據來增加說服力|sometimes:偶爾就好，不要太多:偶爾適時引用即可|never:不用，我比較喜歡簡單直白:不需要引用，保持簡單直白'
))

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("錯誤: 缺少必要的環境變數設定。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- SQLite 初始化 ---
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

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
# Base44 API Bridge
# ============================

def sync_user_to_base44(line_user_id, profile):
    """把用戶資料同步到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_API_URL}/functions/syncUser',
            json={
                'line_user_id': line_user_id,
                'display_name': profile.get('display_name') or '',
                'coach_tone': profile.get('coach_tone') or 'balanced',
                'coach_style': profile.get('coach_style') or 'exploratory',
                'quote_freq': profile.get('quote_freq') or 'sometimes',
                'total_messages': profile.get('total_messages') or 0,
                'plan': profile.get('plan') or 'free',
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[syncUser] ✅ | user={line_user_id}")
        else:
            print(f"[syncUser] ❌ ({resp.status_code}): {resp.text[:100]}")
    except Exception as e:
        print(f"[syncUser] 錯誤: {e}")

def save_goal_or_event_to_base44(line_user_id, display_name, entity_type, **fields):
    """儲存目標或事件到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_API_URL}/functions/saveGoalOrEvent',
            json={
                'line_user_id': line_user_id,
                'display_name': display_name or '',
                'entity_type': entity_type,
                **fields
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[saveGoalOrEvent] ✅ {entity_type} | user={line_user_id}")
        else:
            print(f"[saveGoalOrEvent] ❌ ({resp.status_code}): {resp.text[:100]}")
    except Exception as e:
        print(f"[saveGoalOrEvent] 錯誤: {e}")

def detect_and_save_goals_events(line_user_id, display_name, text):
    """自動偵測用戶對話中的目標/事件，存進 Base44"""
    # 簡易關鍵詞偵測
    goal_keywords = ['目標', '想要', '計畫', '打算', '決定', 'goal', 'want to', 'plan']
    event_keywords = ['完成', '做了', '待辦', '習慣', '打卡', 'done', 'completed', 'todo']
    progress_keywords = ['進度', '完成了', '做到', '達成', 'progress', 'achieve']

    is_goal = any(kw in text for kw in goal_keywords)
    is_event = any(kw in text for kw in event_keywords)
    is_progress = any(kw in text for kw in progress_keywords)

    # 簡單提取標題（句子的前 20 個字）
    title = text[:30].strip()

    if is_progress:
        save_goal_or_event_to_base44(
            line_user_id, display_name,
            'goal_progress',
            progress_note=text[:100]
        )
    elif is_goal:
        save_goal_or_event_to_base44(
            line_user_id, display_name,
            'goal',
            title=title,
            description=text[:100],
            type='short'
        )
    elif is_event:
        save_goal_or_event_to_base44(
            line_user_id, display_name,
            'event',
            title=title,
            type='todo',
            note=text[:100]
        )

# ============================
# LINE helpers
# ============================

def get_line_display_name(user_id):
    """從 LINE API 抓用戶暱稱"""
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name or ''
    except Exception as e:
        print(f"[LINE] 無法取得暱稱: {e}")
        return ''

def send_loading_animation(user_id, seconds=20):
    """LINE loading animation（三個彩色點）"""
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
        print(f"[LINE Loading] {e}")

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
    return "❹ 最後一個問題！\n\n你喜歡我引用名言或學術理論嗎？\n\n請輸入數字：\n" + _build_options_text(QUOTE_OPTIONS)

def handle_onboarding(line_user_id, text, profile):
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    if step == 0:
        line_name = get_line_display_name(line_user_id)
        if line_name:
            save_profile(line_user_id, display_name=line_name, onboarding_step=2)
            return f"👋 嗨，{line_name}！我是你的 AI 生活教練 澄若水 🌊\n\n" + _tone_question()
        else:
            save_profile(line_user_id, onboarding_step=1)
            return "👋 嗨！我是你的 AI 生活教練 澄若水 🌊\n\n❶ 你怎麼稱呼你自己呢？"

    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return "很高興認識你！🙌\n\n" + _tone_question()

    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 1-{len(TONE_OPTIONS)} 的數字\n\n" + _tone_question()
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 1-{len(STYLE_OPTIONS)} 的數字\n\n" + _style_question()
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 1-{len(QUOTE_OPTIONS)} 的數字\n\n" + _quote_question()
        save_profile(line_user_id, quote_freq=opt['value'], onboarding_done=1)

        p = get_profile(line_user_id)
        name = p['display_name'] or '你'
        return f"太棒了，{name}！✨ 設定完成！\n\n從現在開始我是你的專屬教練了 💪"

    return None
# ============================
# Backend Function 呼叫
# ============================

BACKEND_URL = os.environ.get('BACKEND_URL', 'https://app-ffa38ee7.base44.app')

def call_backend_function(function_name, payload):
    """呼叫 Base44 backend function"""
    try:
        url = f'{BACKEND_URL}/functions/{function_name}'
        response = requests.post(url, json=payload, timeout=10)
        return response.json() if response.ok else None
    except Exception as e:
        print(f"[Backend] {function_name} 失敗: {e}")
        return None

def sync_user_to_backend(user_id, profile):
    """將用戶資料同步到 Base44"""
    payload = {
        "line_user_id": user_id,
        "display_name": profile.get('display_name', ''),
        "coach_tone": profile.get('coach_tone', 'balanced'),
        "coach_style": profile.get('coach_style', 'exploratory'),
        "quote_freq": profile.get('quote_freq', 'sometimes'),
        "total_messages": profile.get('total_messages', 0),
        "reminder_enabled": profile.get('reminder_enabled', False),
        "reminder_time": profile.get('reminder_time', '08:00'),
    }
    return call_backend_function('syncUser', payload)


# ============================
# Dify
# ============================

def build_dify_inputs(profile):
    tone_dify = next((v['dify'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '平衡理性')
    style_dify = next((v['dify'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '循循善誘、引導探索')
    quote_dify = next((v['dify'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '偶爾適時引用即可')
    return {
        "user_name": profile.get('display_name') or '用戶',
        "coach_tone": tone_dify,
        "coach_style": style_dify,
        "quote_freq": quote_dify,
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
# 關鍵詞偵測 & 目標/事件儲存
# ============================

def detect_goal_or_event(text):
    """
    簡單的關鍵詞偵測，回傳 (type, title, description) 或 None
    type: 'goal' / 'event' / None
    """
    text_lower = text.lower()

    # 目標關鍵詞（設定長期、中期、短期目標）
    goal_keywords = [
        r'(我想|我要|我的目標|我計畫)(是)?([\w\-、，。，]*[達成|完成|學會|達到|達成])?([\w\-、，。，]*)',
        r'(目標|目的)[是:|]+([\w\-、，。，]*)',
        r'(這個月|這週|今年|未來)(想|要)([\w\-、，。，]*)',
    ]

    for pattern in goal_keywords:
        match = re.search(pattern, text_lower)
        if match:
            title = match.group(-1) or match.group(0)
            title = title.strip('，。、')[:50]  # 限制 50 字
            if len(title) > 3:
                return ('goal', title, '')

    # 事件關鍵詞（待辦、習慣、提醒）
    event_keywords = [
        (r'(待辦|待做|要做|明天|後天)([\w\-、，。，]*)', 'todo'),
        (r'(每天|每週|每月|日常)([\w\-、，。，]*)', 'habit'),
        (r'(提醒我|別忘)([\w\-、，。，]*)', 'reminder'),
    ]

    for pattern, etype in event_keywords:
        match = re.search(pattern, text_lower)
        if match:
            title = match.group(-1) or match.group(0)
            title = title.strip('，。、')[:50]
            if len(title) > 2:
                return ('event', title, etype)

    return None

def call_backend_function(func_name, payload):
    """呼叫 Base44 backend function"""
    try:
        url = f'https://app-ffa38ee7.base44.app/functions/{func_name}'
        response = requests.post(url, json=payload, timeout=10)
        print(f"[Backend] {func_name} status={response.status_code}")
        return response.json() if response.ok else None
    except Exception as e:
        print(f"[Backend] {func_name} 呼叫失敗: {e}")
        return None

def sync_user_to_backend(line_user_id, profile):
    """同步用戶資料到 Base44"""
    payload = {
        'line_user_id': line_user_id,
        'display_name': profile.get('display_name') or '',
        'coach_tone': profile.get('coach_tone') or 'balanced',
        'coach_style': profile.get('coach_style') or 'exploratory',
        'quote_freq': profile.get('quote_freq') or 'sometimes',
        'total_messages': profile.get('total_messages', 0),
    }
    return call_backend_function('syncUser', payload)

def save_goal_or_event_to_backend(line_user_id, entity_type, title, description, extra=None):
    """儲存目標或事件到 Base44"""
    payload = {
        'line_user_id': line_user_id,
        'entity_type': entity_type,
        'title': title,
        'description': description,
        **(extra or {}),
    }
    return call_backend_function('saveGoalOrEvent', payload)


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
        return "☕ 我剛剛去泡了杯茶回來，結果忘記你問什麼了...\n\n請稍等一下再試試看！"

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '未知'
        print(f"[Dify] HTTP 錯誤 {status}")
        return f"🔧 我遇到了一點小問題（錯誤碼：{status}）\n\n先去找 Chris 修一下，請稍後再試！"

    except Exception as e:
        print(f"[Dify] 錯誤: {e}")
        return "😵 我剛才靈魂出竅了一下，請再問我一次！"


# ============================
# Base44 Backend 串接
# ============================

def sync_user_to_base44(user_id, display_name="", coach_tone="", coach_style="", quote_freq="", total_messages=0):
    """呼叫 syncUser function，同步用戶資料到 Base44"""
    try:
        payload = {
            "line_user_id": user_id,
            "display_name": display_name,
            "coach_tone": coach_tone,
            "coach_style": coach_style,
            "quote_freq": quote_freq,
            "total_messages": total_messages,
        }
        resp = requests.post(BASE44_SYNC_USER_URL, json=payload, timeout=10)
        if resp.ok:
            print(f"[Base44] syncUser ✓ | user={user_id}")
            return resp.json()
        else:
            print(f"[Base44] syncUser fail: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] syncUser error: {e}")
    return None

def save_goal_or_event_to_base44(user_id, display_name, entity_type, **kwargs):
    """儲存目標或事件到 Base44 LgatGoal / LgatEvent"""
    try:
        payload = {
            "line_user_id": user_id,
            "display_name": display_name,
            "entity_type": entity_type,  # 'goal' / 'event' / 'goal_progress'
            **kwargs
        }
        resp = requests.post(BASE44_GOAL_EVENT_URL, json=payload, timeout=10)
        if resp.ok:
            print(f"[Base44] save {entity_type} ✓ | user={user_id}")
            return resp.json()
        else:
            print(f"[Base44] save {entity_type} fail: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] save {entity_type} error: {e}")
    return None

def detect_and_save_goal_or_event(user_id, display_name, text):
    """偵測用戶訊息中的目標或事件關鍵詞，自動儲存"""
    # 目標關鍵詞（短中長期）
    goal_patterns = [
        (r'(?:我想|我要|我的目標是|希望能|想要)([\w\s、。，]+?)(?:在|到底|by|$)', 'short'),  # 短期
        (r'(?:未來|接下來|計畫)([\w\s、。，]+?)(?:要|做|完成)?(?:在|內|$)', 'medium'),  # 中期
        (r'(?:人生|長期|一直想)([\w\s、。，]+?)(?:$)', 'long'),  # 長期
    ]
    
    # 事件關鍵詞
    event_patterns = {
        'habit': r'(?:每天|每週|習慣|堅持)([\w\s、。，]+?)(?:打卡|完成|$)',
        'todo': r'(?:待辦|待做|要做|需要做)([\w\s、。，]+?)(?:$)',
        'milestone': r'(?:里程碑|重要|達成)([\w\s、。，]+?)(?:$)',
    }
    
    # 偵測目標
    for pattern, goal_type in goal_patterns:
        match = re.search(pattern, text)
        if match:
            title = match.group(1).strip()
            if len(title) > 2 and len(title) < 50:
                print(f"[Detect] Goal: {title} ({goal_type})")
                threading.Thread(
                    target=save_goal_or_event_to_base44,
                    args=(user_id, display_name, 'goal'),
                    kwargs={'title': title, 'type': goal_type},
                    daemon=True
                ).start()
                return
    
    # 偵測事件
    for event_type, pattern in event_patterns.items():
        match = re.search(pattern, text)
        if match:
            title = match.group(1).strip()
            if len(title) > 2 and len(title) < 50:
                print(f"[Detect] Event: {title} ({event_type})")
                threading.Thread(
                    target=save_goal_or_event_to_base44,
                    args=(user_id, display_name, 'event'),
                    kwargs={'title': title, 'type': event_type},
                    daemon=True
                ).start()
                return


# ============================
# Commands
# ============================

HELP_TEXT = (
    "🤖 指令說明：\n\n"
    "🔄 /reset    — 清除對話記憶\n"
    "⚙️ /setting  — 重新設定\n"
    "📋 /profile  — 查看設定\n"
    "❓ /help     — 說明"
)

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
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '')
        return f"📋 你的設定：\n👤 {name}\n語氣：{tone_label}"

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
    
    # 即時同步用戶資料到 Base44
    sync_user_to_base44(
        user_id,
        display_name=profile.get('display_name', ''),
        coach_tone=profile.get('coach_tone', ''),
        coach_style=profile.get('coach_style', ''),
        quote_freq=profile.get('quote_freq', ''),
    )

    # 背景執行：同步用戶到 Base44
    def sync_bg():
        try:
            sync_user_to_backend(user_id, profile)
        except Exception as e:
            print(f"[Sync] 同步失敗: {e}")
    threading.Thread(target=sync_bg, daemon=True).start()

    # 背景執行：偵測並儲存目標/事件
    def detect_and_save():
        try:
            result = detect_goal_or_event(user_text)
            if result:
                entity_type, title, extra = result
                if entity_type == 'goal':
                    save_goal_or_event_to_backend(user_id, 'goal', title, user_text, {'type': 'short'})
                    print(f"[Goal] 偵測到目標: {title}")
                elif entity_type == 'event':
                    save_goal_or_event_to_backend(user_id, 'event', title, user_text, {'type': extra})
                    print(f"[Event] 偵測到事件: {title} (type={extra})")
        except Exception as e:
            print(f"[Detect] 偵測失敗: {e}")
    threading.Thread(target=detect_and_save, daemon=True).start()

    # 0. 同步用戶資料到 Base44（背景執行）
    def sync_user_bg():
        try:
            requests.post(
                'https://app-dd7dd6e1.base44.app/functions/syncUser',
                json={
                    "line_user_id": user_id,
                    "display_name": profile.get('display_name') or get_line_display_name(user_id),
                    "coach_tone": profile.get('coach_tone'),
                    "coach_style": profile.get('coach_style'),
                    "quote_freq": profile.get('quote_freq'),
                    "total_messages": (profile.get('total_messages') or 0) + 1,
                },
                timeout=5
            )
        except Exception as e:
            print(f"[syncUser] 失敗: {e}")
    threading.Thread(target=sync_user_bg, daemon=True).start()

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

    # 3. 正常對話（背景執行）
    replied_flag = threading.Event()

    def process_and_push():
        # 3a. 更新訊息次數 + 同步到 Base44
        current_profile = get_profile(user_id)
        new_count = (current_profile.get('total_messages') or 0) + 1
        save_profile(user_id, total_messages=new_count)
        current_profile['total_messages'] = new_count
        sync_user_to_base44(user_id, current_profile)

        # 3b. 自動偵測和儲存目標/事件
        detect_and_save_goals_events(user_id, current_profile.get('display_name', ''), user_text)

        # 3c. Loading animation + Dify
        send_loading_animation(user_id, seconds=60)
        try:
            ai_response = ask_dify(user_id, user_text, current_profile)
        except Exception as e:
            print(f"[handle_message] 錯誤: {e}")
            ai_response = "😵 出了點小問題，請再試一次！"

        # 3d. Push 回應
        if not replied_flag.is_set():
            replied_flag.set()
            line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))

    threading.Thread(target=process_and_push, daemon=True).start()

# ============================
# Health check
# ============================

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
