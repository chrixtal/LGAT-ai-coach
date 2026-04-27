import os
import sqlite3
import threading
import requests
import json
import re
from datetime import datetime, timedelta
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
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')
BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')

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
    'often:多一點:頻繁引用名言|sometimes:偶爾就好:偶爾適時引用即可|never:不用:不需要引用'
))

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("❌ 缺少必要的環境變數")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- SQLite 初始化（本地快取）---
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_conversations (
            line_user_id TEXT PRIMARY KEY,
            conversation_id TEXT,
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
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ============================
# DB Helpers
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
        INSERT INTO user_conversations (line_user_id, conversation_id)
        VALUES (?, ?)
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
# LINE Helpers
# ============================

def get_line_display_name(user_id):
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name or ''
    except Exception as e:
        print(f"[LINE] 無法取得暱稱: {e}")
        return ''

# ============================
# Base44 API 呼叫
# ============================

def call_base44_function(func_name, data):
    """呼叫 Base44 backend function"""
    try:
        url = f"{BASE44_API_URL}/functions/{func_name}"
        resp = requests.post(url, json=data, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"[Base44] {func_name} 失敗: {resp.status_code} {resp.text}")
            return None
    except Exception as e:
        print(f"[Base44] {func_name} 例外: {e}")
        return None

def sync_user_to_base44(line_user_id, display_name, profile, total_messages):
    """同步用戶資料到 Base44"""
    data = {
        "line_user_id": line_user_id,
        "display_name": display_name or "未知用戶",
        "coach_tone": profile.get('coach_tone', 'balanced'),
        "coach_style": profile.get('coach_style', 'exploratory'),
        "quote_freq": profile.get('quote_freq', 'sometimes'),
        "total_messages": total_messages,
        "reminder_enabled": profile.get('reminder_enabled', False),
        "reminder_time": profile.get('reminder_time', '08:00'),
        "plan": "free",
    }
    return call_base44_function('syncUser', data)

def detect_and_save_goal_or_event(line_user_id, display_name, text):
    """偵測用戶訊息中的目標或事件關鍵詞，自動儲存"""
    # 目標關鍵詞
    goal_keywords = ['目標', '想要', '計畫', '想', '希望', '期望', '設定', '定個']
    # 事件關鍵詞
    event_keywords = ['待辦', '待做', '任務', '習慣', '提醒', '里程碑', '完成', '做']
    
    text_lower = text.lower()
    entity_type = None
    
    # 簡單的關鍵詞偵測（可改為 NLP）
    for kw in goal_keywords:
        if kw in text_lower:
            entity_type = 'goal'
            break
    
    if not entity_type:
        for kw in event_keywords:
            if kw in text_lower:
                entity_type = 'event'
                break
    
    if entity_type:
        data = {
            "entity_type": entity_type,
            "line_user_id": line_user_id,
            "display_name": display_name or "未知用戶",
            "title": text[:30],  # 簡單的標題截取
            "description": text,
            "type": "short",  # 預設短期
        }
        return call_base44_function('saveGoalOrEvent', data)
    
    return None

# ============================
# LINE Loading Animation
# ============================

def send_loading_animation(user_id, seconds=20):
    try:
        resp = requests.post(
            'https://api.line.me/v2/bot/chat/loading/start',
            headers={
                'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}',
                'Content-Type': 'application/json'
            },
            json={'chatId': user_id, 'loadingSeconds': seconds},
            timeout=5
        )
        print(f"[LINE Loading] status={resp.status_code}")
    except Exception as e:
        print(f"[LINE Loading] 失敗: {e}")

# ============================
# Base44 API 呼叫
# ============================

def call_base44_function(func_name, payload):
    """呼叫 Base44 backend function（非同步，不等回應）"""
    try:
        url = f'{BASE44_API_URL}/functions/{func_name}'
        requests.post(url, json=payload, timeout=5)
        print(f"[Base44] {func_name} called")
    except Exception as e:
        print(f"[Base44] {func_name} failed: {e}")

# ============================
# 目標/事件偵測與儲存
# ============================

GOAL_KEYWORDS = {
    'short': ['明天', '本週', '這禮拜', '這個月', '一個月內', '1個月', '很快', '儘快'],
    'medium': ['3個月', '半年', '6個月', '今年年底', 'Q', '季度', '幾個月'],
    'long': ['一年', '年底', '明年', '長期', '5年', '10年'],
}

EVENT_KEYWORDS = {
    'habit': ['習慣', '每天', '每週', '規律', '養成', '打卡', '堅持'],
    'todo': ['做', '完成', '完畢', '待辦', '得', '需要', '要去', '要做'],
    'milestone': ['達成', '實現', '成就', '完工', '裡程碑', '目標'],
}

def detect_goal_or_event(text):
    """偵測用戶輸入是否包含目標或事件"""
    text_lower = text.lower()
    
    # 檢查是否有明確的指定關鍵詞
    goal_type = None
    for gtype, keywords in GOAL_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            goal_type = gtype
            break
    
    event_type = None
    for etype, keywords in EVENT_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            event_type = etype
            break
    
    # 優先返回找到的類型
    if goal_type:
        return ('goal', goal_type)
    elif event_type:
        return ('event', event_type)
    return None

# ============================
# Onboarding
# ============================

def _build_options_text(options):
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
    return '\n'.join(f"{emojis[i]} {v['label']}" for i, v in enumerate(options.values()))

def _tone_question():
    return "❷ 你喜歡什麼樣的教練語氣？\n\n" + _build_options_text(TONE_OPTIONS)

def _style_question():
    return "❸ 你習慣哪種溝通方式？\n\n" + _build_options_text(STYLE_OPTIONS)

def _quote_question():
    return "❹ 你喜歡我在對話中引用名言或理論嗎？\n\n" + _build_options_text(QUOTE_OPTIONS)

def handle_onboarding(line_user_id, text, profile):
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    if step == 0:
        line_name = get_line_display_name(line_user_id)
        if line_name:
            save_profile(line_user_id, display_name=line_name, onboarding_step=2)
            return (
                f"👋 嗨，{line_name}！我是你的 AI 生活教練 澄若水 🌊\n\n"
                "在開始之前，想先了解你喜歡什麼樣的教練風格！\n\n"
                + _tone_question()
            )
        else:
            save_profile(line_user_id, onboarding_step=1)
            return (
                "👋 嗨！我是你的 AI 生活教練 澄若水 🌊\n\n"
                "在開始之前，我想先多了解你一點！\n\n"
                "❶ 你怎麼稱呼你自己呢？"
            )

    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return "很高興認識你！🙌\n\n" + _tone_question()

    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 {'/'.join(TONE_OPTIONS.keys())} 其中一個數字 😊"
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 {'/'.join(STYLE_OPTIONS.keys())} 其中一個數字 😊"
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 {'/'.join(QUOTE_OPTIONS.keys())} 其中一個數字 😊"
        save_profile(line_user_id, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)

        p = get_profile(line_user_id)
        name = p['display_name'] or '你'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == p['coach_tone']), '')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == p['coach_style']), '')

        # 同步到 Base44
        call_base44_function('syncUser', {
            'line_user_id': line_user_id,
            'display_name': name,
            'coach_tone': p['coach_tone'],
            'coach_style': p['coach_style'],
            'quote_freq': p['quote_freq'],
            'plan': 'free',
        })

        return (
            f"太棒了，{name}！✨ 設定完成！\n\n"
            f"📋 你的教練風格：\n"
            f"• 語氣：{tone_label}\n"
            f"• 溝通方式：{style_label}\n\n"
            "從現在開始，我就是你的專屬教練了 💪"
        )

    return None

# ============================
# Dify inputs
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

# ============================
# Dify API
# ============================

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
        return (
            "☕ 我剛剛去泡了杯茶回來，結果忘記你問什麼了...\n\n"
            "請稍等一下再試試看！"
        )

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '未知'
        return f"🔧 我遇到了一點小問題（錯誤碼：{status}）\n\n請稍後再試！"

    except Exception as e:
        print(f"[Dify] 未預期錯誤: {e}")
        return "😵 我剛才靈魂出竅了一下，請再問我一次！"

# ============================
# 指令處理
# ============================

HELP_TEXT = (
    "🤖 指令說明：\n\n"
    "🔄 /reset    — 清除對話記憶\n"
    "⚙️ /setting  — 重新設定教練風格\n"
    "📋 /profile  — 查看設定\n"
    "❓ /help     — 顯示說明"
)

def handle_command(user_id, text, profile):
    cmd = text.strip().lower()

    if cmd == '/reset':
        reset_conversation(user_id)
        return "🔄 對話記憶已清除！我們重新開始吧～"

    if cmd == '/help':
        return HELP_TEXT

    if cmd == '/setting':
        save_profile(user_id, onboarding_done=0, onboarding_step=2)
        return "⚙️ 好的！我們來重新調整一下～\n\n" + _tone_question()

    if cmd == '/profile':
        name = profile.get('display_name') or '未設定'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '')
        return f"📋 你的設定：\n👤 {name}\n🎯 {tone_label}\n💬 {style_label}"

    return None

# ============================
# Webhook
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

    # 3. AI 對話（背景執行）
    def process_and_push():
        current_profile = get_profile(user_id)
        
        # 同步用戶到 Base44
        new_msg_count = (current_profile.get('total_messages') or 0) + 1
        save_profile(user_id, total_messages=new_msg_count)
        call_base44_function('syncUser', {
            'line_user_id': user_id,
            'display_name': current_profile.get('display_name'),
            'coach_tone': current_profile.get('coach_tone'),
            'coach_style': current_profile.get('coach_style'),
            'quote_freq': current_profile.get('quote_freq'),
            'total_messages': new_msg_count,
        })

        # 偵測並儲存目標/事件
        detected = detect_goal_or_event(user_text)
        if detected:
            entity_type, subtype = detected
            call_base44_function('saveGoalOrEvent', {
                'entity_type': entity_type,
                'line_user_id': user_id,
                'display_name': current_profile.get('display_name', ''),
                'title': user_text[:50],  # 使用用戶輸入的文本作為標題
                'type': subtype,
                'description': user_text,
            })

        # Loading animation + Dify
        send_loading_animation(user_id, seconds=60)
        try:
            ai_response = ask_dify(user_id, user_text, current_profile)
        except Exception as e:
            print(f"[handle_message] 錯誤: {e}")
            ai_response = "😵 出了點小問題，請再試一次！"

        # Push 回應
        line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))

    threading.Thread(target=process_and_push, daemon=True).start()

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
