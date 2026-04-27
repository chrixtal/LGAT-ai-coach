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

# API gateway
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app/functions')
from base44_integration import sync_user_to_base44, detect_and_save_goal_or_event

app = FastAPI()

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
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
    print("❌ 缺少必要的環境變數設定")

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ============================
# Base44 資料同步
# ============================

def sync_user_to_base44(line_user_id, display_name, coach_tone, coach_style, quote_freq, total_messages=0):
    """呼叫 Base44 API 同步用戶資料"""
    try:
        payload = {
            "line_user_id": line_user_id,
            "display_name": display_name,
            "coach_tone": coach_tone,
            "coach_style": coach_style,
            "quote_freq": quote_freq,
            "total_messages": total_messages,
        }
        resp = requests.post(f'{BASE44_API_BASE}/syncUser', json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44] 用戶同步成功 | {line_user_id}")
        else:
            print(f"[Base44] 同步失敗 {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[Base44] 同步錯誤: {e}")

def save_goal_or_event(line_user_id, display_name, entity_type, **fields):
    """呼叫 Base44 API 儲存目標或事件"""
    try:
        payload = {
            "entity_type": entity_type,  # 'goal' / 'event' / 'goal_progress'
            "line_user_id": line_user_id,
            "display_name": display_name,
            **fields
        }
        resp = requests.post(f'{BASE44_API_BASE}/saveGoalOrEvent', json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44] {entity_type} 儲存成功 | {line_user_id}")
        else:
            print(f"[Base44] 儲存失敗 {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[Base44] 儲存錯誤: {e}")


def detect_goal_or_event(text):
    """從文本偵測目標或事件
    回傳 None，或 {'type': 'goal' | 'event', 'title': str, 'type': str, ...}
    """
    # 目標關鍵詞
    goal_keywords = ['目標', '想', '希望', '目い目指す', 'goal', 'dream', '計劃']
    # 事件關鍵詞
    event_keywords = ['做', '要', '待辦', 'todo', '習慣', 'habit', '完成', '今天', '明天']

    goal_match = any(k in text for k in goal_keywords)
    event_match = any(k in text for k in event_keywords)

    # 簡單的標題提取（取前 30 字）
    title = text.strip()[:30]

    if goal_match and '目標' in text:
        # 嘗試從「我想... 預計...」的格式提取
        if '預計' in text or '計畫' in text or '多久' in text:
            if '一個月' in text or '月' in text:
                return {'type': 'goal', 'title': title, 'goal_type': 'short'}
            elif '三個月' in text or '六個月' in text:
                return {'type': 'goal', 'title': title, 'goal_type': 'medium'}
            else:
                return {'type': 'goal', 'title': title, 'goal_type': 'long'}
        return {'type': 'goal', 'title': title, 'goal_type': 'short'}

    if event_match:
        if '習慣' in text or '每天' in text or '每週' in text:
            return {'type': 'event', 'title': title, 'event_type': 'habit', 'recurrence': 'daily'}
        return {'type': 'event', 'title': title, 'event_type': 'todo', 'recurrence': 'none'}

    return None

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
# Base44 API 呼叫
# ============================

def sync_user_to_base44(user_id: str, profile: dict):
    """同步用戶資料到 Base44"""
    try:
        payload = {
            'line_user_id': user_id,
            'display_name': profile.get('display_name', ''),
            'coach_tone': profile.get('coach_tone', ''),
            'coach_style': profile.get('coach_style', ''),
            'quote_freq': profile.get('quote_freq', ''),
        }
        resp = requests.post(
            f"{BASE44_API_BASE}/syncUser",
            json=payload,
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Base44] 同步用戶 {user_id}")
    except Exception as e:
        print(f"[Base44 syncUser] 失敗: {e}")

def save_goal_or_event_to_base44(user_id: str, display_name: str, entity_type: str, **fields):
    """儲存目標或事件到 Base44"""
    try:
        payload = {
            'entity_type': entity_type,
            'line_user_id': user_id,
            'display_name': display_name,
            **fields
        }
        resp = requests.post(
            f"{BASE44_API_BASE}/saveGoalOrEvent",
            json=payload,
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get('ok'):
                print(f"[Base44] 儲存 {entity_type}: {fields.get('title', '')} | user={user_id}")
    except Exception as e:
        print(f"[Base44 save] 失敗: {e}")

# ============================
# 目標/事件偵測
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
    """LINE loading animation"""
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
    return "❹ 最後一個問題！\n\n你喜歡我在對話中引用名言、學術理論或研究嗎？\n\n請輸入數字：\n" + _build_options_text(QUOTE_OPTIONS)

def handle_onboarding(line_user_id, text, profile):
    """Onboarding 問卷邏輯"""
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    # Step 0
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
                "❶ 你怎麼稱呼你自己呢？（輸入你的名字或暱稱就好）"
            )

    # Step 1
    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！請輸入你的名字或暱稱 😊"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return "很高興認識你！🙌\n\n" + _tone_question()

    # Step 2
    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            valid = '、'.join(TONE_OPTIONS.keys())
            return f"請輸入 {valid} 其中一個數字 😊\n\n" + _tone_question()
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    # Step 3
    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            valid = '、'.join(STYLE_OPTIONS.keys())
            return f"請輸入 {valid} 其中一個數字 😊\n\n" + _style_question()
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    # Step 4
    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            valid = '、'.join(QUOTE_OPTIONS.keys())
            return f"請輸入 {valid} 其中一個數字 😊\n\n" + _quote_question()
        save_profile(line_user_id, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)

        p = get_profile(line_user_id)
        name = p['display_name'] or '你'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == p['coach_tone']), '')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == p['coach_style']), '')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == p['quote_freq']), '')

        return (
            f"太棒了，{name}！✨ 設定完成！\n\n"
            f"📋 你的教練風格：\n"
            f"• 語氣：{tone_label}\n"
            f"• 溝通方式：{style_label}\n"
            f"• 引用頻率：{quote_label}\n\n"
            "從現在開始，我就是你的專屬教練了 💪\n"
            "有什麼想聊的，直接說吧！\n\n"
            "（隨時可以輸入 ⚙️ /setting 重新調整教練風格）"
        )

    return None

# ============================
# Dify 相關
# ============================

def build_dify_inputs(profile):
    import datetime
    import zoneinfo
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
        return "☕ 我剛剛去泡了杯茶回來，結果忘記你問什麼了...\n\n請稍等一下再試試看！如果一直這樣，請聯絡開發者 Chris 看看哦 🙏"

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '未知'
        return f"🔧 我遇到了一點小問題（錯誤碼：{status}）\n\n先去找 Chris 修一下，請稍後再試！感謝你的耐心 💪"

    except Exception as e:
        print(f"[Dify] 未預期錯誤: {e}")
        return "😵 我剛才靈魂出竅了一下，請再問我一次！\n\n如果問題一直出現，麻煩聯絡開發者 Chris 看看，謝謝你的包容 🙏"

# ============================
# 指令處理
# ============================

HELP_TEXT = (
    "🤖 指令說明：\n\n"
    "🔄 /reset    — 清除對話記憶，重新開始\n"
    "⚙️ /setting  — 重新設定教練風格\n"
    "📋 /profile  — 查看目前的設定\n"
    "❓ /help     — 顯示這個說明\n\n"
    "直接輸入文字就能和我對話！"
)

def handle_command(user_id, text, profile):
    cmd = text.strip().lower()

    if cmd == '/reset':
        reset_conversation(user_id)
        return "🔄 對話記憶已清除！\n\n我們重新開始吧～有什麼想聊的？😊"

    if cmd == '/help':
        return HELP_TEXT

    if cmd == '/setting':
        save_profile(user_id, onboarding_done=0, onboarding_step=2)
        return "⚙️ 好的！我們來重新調整一下～\n\n" + _tone_question()

    if cmd == '/profile':
        name = profile.get('display_name') or '未設定'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '未設定')
        return (
            f"📋 你的教練設定：\n\n"
            f"👤 名字：{name}\n"
            f"🎯 語氣：{tone_label}\n"
            f"💬 溝通方式：{style_label}\n"
            f"📚 引用頻率：{quote_label}\n\n"
            "用 /setting 可以重新調整～"
        )

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

    # 2. Onboarding 問卷
    onboarding_response = handle_onboarding(user_id, user_text, profile)
    if onboarding_response is not None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=onboarding_response))
        return

    # 3. 正常 AI 對話（背景執行）
    replied_flag = threading.Event()

    def process_and_push():
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        try:
            # 同步用戶到 Base44
            sync_user_to_base44(user_id, {
                'display_name': current_profile.get('display_name', ''),
                'coach_tone': current_profile.get('coach_tone', 'balanced'),
                'coach_style': current_profile.get('coach_style', 'exploratory'),
                'quote_freq': current_profile.get('quote_freq', 'sometimes'),
                'total_messages': current_profile.get('total_messages', 0),
            })
            # 偵測並儲存目標/事件
            detect_and_save_goal_or_event(user_id, current_profile.get('display_name', ''), user_text)
            # 取得 AI 回應
            ai_response = ask_dify(user_id, user_text, current_profile)
        except Exception as e:
            print(f"[handle_message] 未預期錯誤: {e}")
            ai_response = "😵 出了點小問題，請再試一次！"
        
        if not replied_flag.is_set():
            replied_flag.set()
            line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))

    threading.Thread(target=process_and_push, daemon=True).start()

# ============================
# 健康檢查
# ============================

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)


# ============================
# Base44 API 整合
# ============================

import json

BASE44_APP_ID = '69e35caa4e5d9a67dd7dd6e1'
BASE44_API_URL = 'https://app-ffa38ee7.base44.app/functions'

def call_base44_function(func_name, payload):
    """呼叫 Base44 backend function"""
    try:
        url = f'{BASE44_API_URL}/{func_name}'
        headers = {'Content-Type': 'application/json'}
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[Base44] {func_name} 失敗: {e}")
        return None

def sync_user_to_base44(user_id, profile):
    """同步用戶資料到 Base44"""
    payload = {
        'line_user_id': user_id,
        'display_name': profile.get('display_name') or '',
        'coach_tone': profile.get('coach_tone'),
        'coach_style': profile.get('coach_style'),
        'quote_freq': profile.get('quote_freq'),
        'total_messages': profile.get('total_messages', 0),
        'reminder_enabled': profile.get('reminder_enabled', False),
        'reminder_time': profile.get('reminder_time', '08:00'),
    }
    result = call_base44_function('syncUser', payload)
    if result:
        print(f"[Base44] syncUser 成功: {user_id}")
    return result

def detect_and_save_goal_or_event(user_id, text, profile):
    """偵測訊息中的目標/事件關鍵詞，自動儲存"""
    # 簡單的關鍵詞比對
    goal_keywords = ['目標', '想要', '計畫', '決定', '設定', '希望', '夢想', '目的', '想達成']
    event_keywords = ['習慣', '待辦', '提醒', '打卡', '完成', '做', '明天', '今天', '每天', '每週']
    milestone_keywords = ['里程碑', '重要', '關鍵', '達成', '完成', '成就']

    text_lower = text.lower()
    display_name = profile.get('display_name') or '用戶'

    # 檢查是否包含目標關鍵詞
    if any(kw in text_lower for kw in goal_keywords):
        # 嘗試提取目標標題（簡單版，取前 20 字）
        title = text[:50].strip() if len(text) > 0 else '未命名目標'
        payload = {
            'entity_type': 'goal',
            'line_user_id': user_id,
            'display_name': display_name,
            'title': title,
            'description': text,
            'type': 'short',  # 預設短期，用戶後續可在後台調整
        }
        call_base44_function('saveGoalOrEvent', payload)
        print(f"[Base44] 已記錄目標: {title}")

    # 檢查是否包含事件關鍵詞
    if any(kw in text_lower for kw in event_keywords):
        event_type = 'habit' if '習慣' in text_lower or '每' in text_lower else 'todo'
        if any(mk in text_lower for mk in milestone_keywords):
            event_type = 'milestone'

        title = text[:50].strip() if len(text) > 0 else '未命名事件'
        payload = {
            'entity_type': 'event',
            'line_user_id': user_id,
            'display_name': display_name,
            'title': title,
            'type': event_type,
            'note': text,
        }
        call_base44_function('saveGoalOrEvent', payload)
        print(f"[Base44] 已記錄事件: {title}")

