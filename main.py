import os
import sqlite3
import threading
import requests
import json
import re
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn
import json
import datetime
import zoneinfo

app = FastAPI()

# --- Base44 API 設定 ---
BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_API_BASE = f'https://app-ffa38ee7.base44.app/functions'

def call_base44_function(func_name, payload):
    """呼叫 Base44 backend function"""
    try:
        url = f'{BASE44_API_BASE}/{func_name}'
        print(f"[Base44] POST {url}")
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        print(f"[Base44] {func_name} 成功: {result}")
        return result
    except Exception as e:
        print(f"[Base44] {func_name} 失敗: {e}")
        return None

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_API_BASE = os.environ.get('BASE44_API_BASE', 'https://app-ffa38ee7.base44.app/functions')

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
    'strict:嚴格督促型:嚴格督促|gentle:溫柔支持型:溫柔支持|balanced:平衡理性型:平衡理性'
))

STYLE_OPTIONS = _parse_options(os.environ.get(
    'COACH_STYLE_OPTIONS',
    'direct:直接說重點:直接說重點|exploratory:循循善誘:循循善誘、引導探索'
))

QUOTE_OPTIONS = _parse_options(os.environ.get(
    'COACH_QUOTE_OPTIONS',
    'often:多一點:頻繁引用|sometimes:偶爾就好:偶爾適時引用|never:不用:保持簡單直白'
))

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("錯誤: 缺少必要的環境變數")

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
# Backend Function 呼叫
# ============================

def sync_user_to_backend(line_user_id, profile):
    """同步用戶資料到 Base44 LgatUser"""
    try:
        payload = {
            'line_user_id': line_user_id,
            'display_name': profile.get('display_name', ''),
            'coach_tone': profile.get('coach_tone', 'balanced'),
            'coach_style': profile.get('coach_style', 'exploratory'),
            'quote_freq': profile.get('quote_freq', 'sometimes'),
            'total_messages': profile.get('total_messages', 0),
        }
        resp = requests.post(f'{BASE44_API_URL}/syncUser', json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Backend] syncUser OK: {line_user_id}")
            return True
        else:
            print(f"[Backend] syncUser 失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Backend] syncUser 異常: {e}")
    return False

def save_goal_or_event(line_user_id, display_name, entity_type, title, extra=None):
    """儲存目標或事件到 Base44"""
    try:
        payload = {
            'entity_type': entity_type,  # 'goal' or 'event'
            'line_user_id': line_user_id,
            'display_name': display_name,
            'title': title[:100],
        }
        if extra:
            payload.update(extra)
        resp = requests.post(f'{BASE44_API_URL}/saveGoalOrEvent', json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Backend] 儲存 {entity_type}: {title[:30]}")
            return True
    except Exception as e:
        print(f"[Backend] saveGoalOrEvent 異常: {e}")
    return False

def detect_keywords(text):
    """從文本中偵測目標/事件關鍵詞"""
    goal_keywords = ['目標', '想要', '計畫', '達成', '想完成', '目的', '夢想', '希望']
    event_keywords = ['習慣', '待辦', '做', '每天', '每週', '打卡', '里程碑']
    
    detected_goal = any(kw in text for kw in goal_keywords)
    detected_event = any(kw in text for kw in event_keywords)
    
    return detected_goal, detected_event

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
    return {'line_user_id': line_user_id, 'display_name': '', 'coach_tone': 'balanced', 'coach_style': 'exploratory', 'quote_freq': 'sometimes', 'onboarding_done': 0, 'onboarding_step': 0, 'total_messages': 0}

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
    except Exception as e:
        print(f"[LINE] 無法取得暱稱: {e}")
        return ''

def send_loading_animation(user_id, seconds=20):
    try:
        resp = requests.post(
            'https://api.line.me/v2/bot/chat/loading/start',
            headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
            json={'chatId': user_id, 'loadingSeconds': seconds},
            timeout=5
        )
        print(f"[LINE Loading] status={resp.status_code}")
    except Exception as e:
        print(f"[LINE Loading] 失敗: {e}")

# ============================
# 問卷 Onboarding
# ============================

def _build_options_text(options):
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
    return '\n'.join(f"{emojis[i]} {v['label']}" for i, v in enumerate(options.values()))

def _tone_question():
    return "❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n" + _build_options_text(TONE_OPTIONS)

def _style_question():
    return "❸ 你習慣哪種溝通方式？\n\n請輸入數字：\n" + _build_options_text(STYLE_OPTIONS)

def _quote_question():
    return "❹ 最後一個問題！\n\n你喜歡我引用名言或研究嗎？\n\n請輸入數字：\n" + _build_options_text(QUOTE_OPTIONS)

def handle_onboarding(line_user_id, text, profile):
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    if step == 0:
        line_name = get_line_display_name(line_user_id)
        if line_name:
            save_profile(line_user_id, display_name=line_name, onboarding_step=2)
            return f"👋 嗨，{line_name}！我是澄若水。\n\n{_tone_question()}"
        else:
            save_profile(line_user_id, onboarding_step=1)
            return "👋 嗨！我是澄若水。\n\n你怎麼稱呼你自己呢？"

    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return f"很高興認識你！🙌\n\n{_tone_question()}"

    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入數字 😊\n\n{_tone_question()}"
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入數字 😊\n\n{_style_question()}"
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入數字 😊\n\n{_quote_question()}"
        save_profile(line_user_id, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)
        p = get_profile(line_user_id)
        return f"設定完成！✨ 我們開始吧 💪"

    return None

# ============================
# Base44 資料同步
# ============================

BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app.base44.com/api')

def sync_user_to_base44(line_user_id, profile):
    """同步用戶資料到 Base44 的 LgatUser entity"""
    url = f'{BASE44_API_URL}/apps/{BASE44_APP_ID}/functions/syncUser'
    payload = {
        'line_user_id': line_user_id,
        'display_name': profile.get('display_name', ''),
        'coach_tone': profile.get('coach_tone', 'balanced'),
        'coach_style': profile.get('coach_style', 'exploratory'),
        'quote_freq': profile.get('quote_freq', 'sometimes'),
    }
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[syncUser] OK | {line_user_id}")
        else:
            print(f"[syncUser] HTTP {resp.status_code}")
    except Exception as e:
        print(f"[syncUser] Error: {e}")

# ============================
# Dify
# ============================

def detect_goal_or_event(text, line_user_id, display_name):
    """偵測用戶輸入中的目標或事件，自動保存到 Base44"""
    # 目標關鍵詞：「想」、「計畫」、「目標」、「要」、「希望」
    goal_patterns = [
        r'(想|計畫|目標|要|希望|決定|打算).{0,20}(跑步|健身|閱讀|學習|工作|旅遊|存錢|減肥|戒菸|早睡)',
        r'(我的夢想|我想).*',
    ]
    
    # 事件關鍵詞：「完成」、「做了」、「打卡」、「習慣」、「待辦」
    event_patterns = [
        r'(完成|做了|打卡|達成|做到).{0,20}',
        r'(今天|昨天|明天).{0,10}(跑步|健身|閱讀|冥想|運動)',
    ]

    # 檢查目標
    for pattern in goal_patterns:
        if re.search(pattern, text):
            title = text[:50].strip()
            payload = {
                'entity_type': 'goal',
                'line_user_id': line_user_id,
                'display_name': display_name,
                'title': title,
                'type': 'short',
            }
            result = call_base44_function('saveGoalOrEvent', payload)
            if result:
                print(f"[Goal] 儲存目標: {title} | user={line_user_id}")
            break

    # 檢查事件
    for pattern in event_patterns:
        if re.search(pattern, text):
            title = text[:50].strip()
            payload = {
                'entity_type': 'event',
                'line_user_id': line_user_id,
                'display_name': display_name,
                'title': title,
                'type': 'todo',
            }
            result = call_base44_function('saveGoalOrEvent', payload)
            if result:
                print(f"[Event] 儲存事件: {title} | user={line_user_id}")
            break

def build_dify_inputs(profile):
    tz = zoneinfo.ZoneInfo("Asia/Taipei")
    now = datetime.datetime.now(tz)
    current_time = now.strftime("%Y年%m月%d日 %H:%M")

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
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    data = {"inputs": inputs, "query": text, "response_mode": "blocking", "user": user_id}
    if conversation_id:
        data["conversation_id"] = conversation_id
    response = requests.post(url, headers=headers, json=data, timeout=120)
    response.raise_for_status()
    return response.json()

def ask_dify(user_id, text, profile):
    # 同步用戶資料到 Base44
    sync_payload = {
        'line_user_id': user_id,
        'display_name': profile.get('display_name', ''),
        'coach_tone': profile.get('coach_tone', ''),
        'coach_style': profile.get('coach_style', ''),
        'quote_freq': profile.get('quote_freq', ''),
        'total_messages': (profile.get('total_messages', 0) or 0) + 1,
    }
    call_base44_function('syncUser', sync_payload)
    
    # 偵測並保存目標/事件
    detect_goal_or_event(text, user_id, profile.get('display_name', ''))
    
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
        print(f"[Dify] 連線問題: {e}")
        if DIFY_API_KEY_FALLBACK:
            try:
                result = call_dify(DIFY_API_KEY_FALLBACK, user_id, text, None, inputs)
                answer = result.get('answer', '').strip()
                return "⚡️ 備用系統：\n\n" + answer if answer else "😓 備援也沒回應，請稍後再試！"
            except Exception as fe:
                print(f"[Dify] 備援失敗: {fe}")
        return "☕ 我去泡茶了，請稍等一下再試！"

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else '未知'
        return f"🔧 我遇到了小問題（{status}），請稍後再試！"

    except Exception as e:
        print(f"[Dify] 錯誤: {e}")
        return "😵 出了點小問題，請再試一次！"

# ============================
# 指令
# ============================

HELP_TEXT = (
    "🤖 指令說明：\n\n"
    "🔄 /reset   — 清除對話記憶\n"
    "⚙️ /setting — 重新設定風格\n"
    "📋 /profile — 查看設定\n"
    "❓ /help    — 顯示說明"
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
        return "⚙️ 重新調整風格～\n\n" + _tone_question()

    if cmd == '/profile':
        name = profile.get('display_name') or '未設定'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        return f"📋 你的設定：\n\n👤 {name}\n🎯 {tone_label}\n💬 {style_label}"

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

    # 3. 正常 AI 對話 + 後台同步
    profile = get_profile(user_id)
    total_msgs = (profile.get('total_messages') or 0) + 1
    save_profile(user_id, total_messages=total_msgs)

    # 背景執行：loading animation + Dify + Backend sync
    def process_and_push():
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        
        # 呼叫 Dify
        ai_response = ask_dify(user_id, user_text, current_profile)
        
        # 同步用戶資料
        sync_user_to_backend(user_id, current_profile)
        
        # 偵測並儲存目標/事件
        goal_detected, event_detected = detect_keywords(user_text)
        display_name = current_profile.get('display_name') or ''
        
        if goal_detected:
            # 簡單提取標題：取第一句話
            title = user_text.split('。')[0][:80]
            save_goal_or_event(user_id, display_name, 'goal', title)
        
        if event_detected:
            title = user_text.split('。')[0][:80]
            save_goal_or_event(user_id, display_name, 'event', title)
        
        # 推送回應
        line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))

    threading.Thread(target=process_and_push, daemon=True).start()

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
