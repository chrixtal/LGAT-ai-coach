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

# ============================
# Base44 Backend API
# ============================
BASE44_API_URL = 'https://app-ffa38ee7.base44.app/functions'

def call_base44(func_name, payload):
    """呼叫 Base44 backend function"""
    try:
        resp = requests.post(f'{BASE44_API_URL}/{func_name}', json=payload, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"[Base44] {func_name}: {e}")
    return None

# ============================
# 環境變數
# ============================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')

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
    'often:多一點:頻繁引用名言、學術理論或研究數據來增加說服力|sometimes:偶爾就好:偶爾適時引用即可|never:不用:不需要引用，保持簡單直白'
))

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ============================
# SQLite (本地 onboarding & 對話記憶)
# ============================
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_conversations (
        line_user_id TEXT PRIMARY KEY,
        conversation_id TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
        line_user_id TEXT PRIMARY KEY,
        display_name TEXT,
        coach_tone TEXT,
        coach_style TEXT,
        quote_freq TEXT,
        onboarding_done INTEGER DEFAULT 0,
        onboarding_step INTEGER DEFAULT 0,
        total_messages INTEGER DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db()

def get_profile(line_user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM user_profiles WHERE line_user_id = ?', (line_user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else {
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
        c.execute(f'UPDATE user_profiles SET {k} = ?, updated_at = CURRENT_TIMESTAMP WHERE line_user_id = ?', (v, line_user_id))
    conn.commit()
    conn.close()

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
    c.execute('INSERT OR REPLACE INTO user_conversations (line_user_id, conversation_id, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (line_user_id, conversation_id))
    conn.commit()
    conn.close()

def reset_conversation(line_user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_conversations WHERE line_user_id = ?', (line_user_id,))
    conn.commit()
    conn.close()

# ============================
# LINE 輔助函數
# ============================

def get_line_display_name(user_id):
    try:
        return line_bot_api.get_profile(user_id).display_name or ''
    except:
        return ''

def send_loading_animation(user_id, seconds=20):
    try:
        requests.post('https://api.line.me/v2/bot/chat/loading/start',
            headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
            json={'chatId': user_id, 'loadingSeconds': seconds}, timeout=5)
    except Exception as e:
        print(f"[LINE Loading] {e}")

# ============================
# Onboarding
# ============================

def _options_text(options):
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
    return '\n'.join(f"{emojis[i]} {v['label']}" for i, v in enumerate(options.values()))

def handle_onboarding(line_user_id, text, profile):
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    if step == 0:
        line_name = get_line_display_name(line_user_id)
        if line_name:
            save_profile(line_user_id, display_name=line_name, onboarding_step=2)
            return f"👋 嗨，{line_name}！我是澄若水 🌊\n\n❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n{_options_text(TONE_OPTIONS)}"
        else:
            save_profile(line_user_id, onboarding_step=1)
            return "👋 嗨！我是澄若水 🌊\n\n❶ 你怎麼稱呼自己？（輸入名字或暱稱）"

    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！😊"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return f"很高興認識你！🙌\n\n❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n{_options_text(TONE_OPTIONS)}"

    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 {', '.join(TONE_OPTIONS.keys())} 其中一個 😊\n\n{_options_text(TONE_OPTIONS)}"
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return f"❸ 你習慣哪種溝通方式？\n\n請輸入數字：\n{_options_text(STYLE_OPTIONS)}"

    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 {', '.join(STYLE_OPTIONS.keys())} 其中一個 😊"
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return f"❹ 你喜歡我引用名言嗎？\n\n請輸入數字：\n{_options_text(QUOTE_OPTIONS)}"

    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 {', '.join(QUOTE_OPTIONS.keys())} 其中一個 😊"
        save_profile(line_user_id, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)
        p = get_profile(line_user_id)
        # 異步同步到 Base44
        threading.Thread(target=lambda: call_base44('syncUser', {
            'line_user_id': line_user_id,
            'display_name': p['display_name'],
            'coach_tone': p['coach_tone'],
            'coach_style': p['coach_style'],
            'quote_freq': p['quote_freq'],
            'total_messages': 0,
        }), daemon=True).start()
        return f"✨ 設定完成！我是你的教練澄若水。有什麼想聊的，直接說吧！💪"

    return None

# ============================
# Dify AI
# ============================

def build_dify_inputs(profile):
    import datetime, zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Taipei")
    now = datetime.datetime.now(tz)
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
    data = {
        "inputs": inputs,
        "query": text,
        "response_mode": "blocking",
        "user": user_id,
    }
    if conversation_id:
        data["conversation_id"] = conversation_id
    response = requests.post(url, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json=data, timeout=120)
    response.raise_for_status()
    return response.json()

def ask_dify(user_id, text, profile):
    conversation_id = get_conversation_id(user_id)
    inputs = build_dify_inputs(profile)

    try:
        result = call_dify(DIFY_API_KEY, user_id, text, conversation_id, inputs)
        if result.get('conversation_id'):
            save_conversation_id(user_id, result['conversation_id'])
        answer = result.get('answer', '').strip()
        return answer or "🤔 我想到一半忘記了，請再問我一次！"

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[Dify] 連線問題: {e}")
        if DIFY_API_KEY_FALLBACK:
            try:
                result = call_dify(DIFY_API_KEY_FALLBACK, user_id, text, None, inputs)
                answer = result.get('answer', '').strip()
                return f"⚡️ 我切換到備用系統回答你：\n\n{answer}" if answer else "😓 備援系統也沒回應，請稍後再試！"
            except:
                pass
        return "☕ 我剛去泡茶回來，忘記你問什麼了...\n\n請稍後再試！如果一直這樣，請聯絡 Chris 🙏"

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else '未知'
        print(f"[Dify] HTTP {status}")
        return f"🔧 我遇到小問題（錯誤 {status}）\n\n請稍後再試！感謝你的耐心 💪"

    except Exception as e:
        print(f"[Dify] 錯誤: {e}")
        return "😵 我靈魂出竅了，請再問我一次！\n\n感謝你的包容 🙏"

# ============================
# 指令處理
# ============================

HELP_TEXT = "🤖 指令：\n\n🔄 /reset — 清除對話記憶\n⚙️ /setting — 重新設定\n📋 /profile — 查看設定\n❓ /help — 說明"

def handle_command(user_id, text, profile):
    cmd = text.strip().lower()
    if cmd == '/reset':
        reset_conversation(user_id)
        return "🔄 對話記憶已清除！重新開始吧～ 😊"
    if cmd == '/help':
        return HELP_TEXT
    if cmd == '/setting':
        save_profile(user_id, onboarding_done=0, onboarding_step=2)
        return f"⚙️ 重新調整～\n\n❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n{_options_text(TONE_OPTIONS)}"
    if cmd == '/profile':
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '未設定')
        return f"📋 你的教練設定：\n\n👤 {profile.get('display_name') or '未設定'}\n🎯 {tone_label}\n💬 {style_label}\n📚 {quote_label}\n\n用 /setting 重新調整～"
    return None

# ============================
# LINE Webhook
# ============================

app = FastAPI()

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
    cmd_response = handle_command(user_id, user_text, profile)
    if cmd_response:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=cmd_response))
        return

    # 2. Onboarding
    onboarding_response = handle_onboarding(user_id, user_text, profile)
    if onboarding_response is not None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=onboarding_response))
        return

    # 3. 正常對話（背景執行）
    replied_flag = threading.Event()

    def process_and_push():
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)

        # 呼叫 Dify
        ai_response = ask_dify(user_id, user_text, current_profile)

        # 更新訊息計數並同步到 Base44
        current_profile['total_messages'] = (current_profile.get('total_messages') or 0) + 1
        save_profile(user_id, total_messages=current_profile['total_messages'])
        call_base44('syncUser', {
            'line_user_id': user_id,
            'display_name': current_profile.get('display_name'),
            'coach_tone': current_profile.get('coach_tone'),
            'coach_style': current_profile.get('coach_style'),
            'quote_freq': current_profile.get('quote_freq'),
            'total_messages': current_profile['total_messages'],
        })

        # 解析目標/事件標籤（[GOAL:...] [EVENT:...]）
        goal_match = re.search(r'\[GOAL:([^\]]+)\]', ai_response)
        event_match = re.search(r'\[EVENT:([^\]]+)\]', ai_response)

        if goal_match:
            goal_title = goal_match.group(1)
            call_base44('saveGoalOrEvent', {
                'entity_type': 'goal',
                'line_user_id': user_id,
                'display_name': current_profile.get('display_name'),
                'title': goal_title,
                'type': 'short',
            })
            print(f"[Goal] {goal_title} | {user_id}")

        if event_match:
            event_title = event_match.group(1)
            call_base44('saveGoalOrEvent', {
                'entity_type': 'event',
                'line_user_id': user_id,
                'display_name': current_profile.get('display_name'),
                'title': event_title,
                'type': 'todo',
            })
            print(f"[Event] {event_title} | {user_id}")

        # 移除標籤後發送
        clean_response = re.sub(r'\[GOAL:[^\]]*\]|\[EVENT:[^\]]*\]', '', ai_response).strip()
        if not clean_response:
            clean_response = ai_response

        if not replied_flag.is_set():
            replied_flag.set()
            line_bot_api.push_message(user_id, TextSendMessage(text=clean_response))

    threading.Thread(target=process_and_push, daemon=True).start()

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
