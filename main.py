import os, sqlite3, threading, requests, re, json, datetime, zoneinfo
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn

app = FastAPI()

# ====== 環境變數 ======
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
BASE44_URL = os.environ.get('BASE44_URL', 'https://app-ffa38ee7.base44.app')

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("❌ 缺少必要環境變數")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

# ====== 教練設定 ======
def _parse_options(env_val):
    result = {}
    for i, raw in enumerate(env_val.split('|'), 1):
        parts = raw.strip().split(':')
        if len(parts) == 3:
            result[str(i)] = {'value': parts[0], 'label': parts[1], 'dify': parts[2]}
    return result

TONE_OPTIONS = _parse_options(os.environ.get('COACH_TONE_OPTIONS', 'strict:嚴格督促型:嚴格督促|gentle:溫柔支持型:溫柔支持|balanced:平衡理性型:平衡理性'))
STYLE_OPTIONS = _parse_options(os.environ.get('COACH_STYLE_OPTIONS', 'direct:直接說重點:直接說重點|exploratory:循循善誘:循循善誘、引導探索'))
QUOTE_OPTIONS = _parse_options(os.environ.get('COACH_QUOTE_OPTIONS', 'often:多一點:頻繁引用|sometimes:偶爾:偶爾適時引用|never:不用:不需要引用'))

# ====== SQLite ======
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_conversations (
        line_user_id TEXT PRIMARY KEY,
        conversation_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
        line_user_id TEXT PRIMARY KEY,
        display_name TEXT,
        coach_tone TEXT DEFAULT 'balanced',
        coach_style TEXT DEFAULT 'exploratory',
        quote_freq TEXT DEFAULT 'sometimes',
        onboarding_done INTEGER DEFAULT 0,
        onboarding_step INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

def get_conversation_id(uid):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT conversation_id FROM user_conversations WHERE line_user_id = ?', (uid,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_conversation_id(uid, conv_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT INTO user_conversations (line_user_id, conversation_id, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(line_user_id) DO UPDATE SET conversation_id = excluded.conversation_id, updated_at = CURRENT_TIMESTAMP', (uid, conv_id))
    conn.commit()
    conn.close()

def get_profile(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM user_profiles WHERE line_user_id = ?', (uid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else {'line_user_id': uid, 'display_name': '', 'coach_tone': 'balanced', 'coach_style': 'exploratory', 'quote_freq': 'sometimes', 'onboarding_done': 0, 'onboarding_step': 0}

def save_profile(uid, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO user_profiles (line_user_id) VALUES (?)', (uid,))
    for k, v in kwargs.items():
        c.execute(f'UPDATE user_profiles SET {k} = ?, updated_at = CURRENT_TIMESTAMP WHERE line_user_id = ?', (v, uid))
    conn.commit()
    conn.close()

# ====== 後台同步 ======

def save_goal_event_to_base44(uid, display_name, entity_type, **fields):
    """儲存目標或事件到後台"""
    try:
        payload = {'entity_type': entity_type, 'line_user_id': uid, 'display_name': display_name, **fields}
        requests.post(f'{BASE44_URL}/functions/saveGoalOrEvent', json=payload, timeout=5)
    except Exception as e:
        print(f"[Save] 失敗: {e}")

# ====== LINE helpers ======
def get_line_display_name(uid):
    try:
        profile = line_bot_api.get_profile(uid)
        return profile.display_name or ''
    except:
        return ''

def send_loading_animation(uid, seconds=20):
    try:
        requests.post('https://api.line.me/v2/bot/chat/loading/start',
            headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
            json={'chatId': uid, 'loadingSeconds': seconds}, timeout=5)
    except Exception as e:
        print(f"[Loading] 失敗: {e}")

# ====== Onboarding ======
def _build_opts_text(opts):
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
    return '\n'.join(f"{emojis[i]} {v['label']}" for i, v in enumerate(opts.values()))

def handle_onboarding(uid, text, profile):
    if profile['onboarding_done']:
        return None
    
    step = profile['onboarding_step']
    
    if step == 0:
        line_name = get_line_display_name(uid)
        if line_name:
            save_profile(uid, display_name=line_name, onboarding_step=2)
            return f"👋 嗨，{line_name}！我是 澄若水 🌊\n\n❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n{_build_opts_text(TONE_OPTIONS)}"
        else:
            save_profile(uid, onboarding_step=1)
            return "👋 嗨！我是 澄若水 🌊\n\n❶ 你怎麼稱呼你自己呢？"
    
    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！"
        save_profile(uid, display_name=answer, onboarding_step=2)
        return f"很高興認識你！🙌\n\n❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n{_build_opts_text(TONE_OPTIONS)}"
    
    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入有效的數字\n\n{_build_opts_text(TONE_OPTIONS)}"
        save_profile(uid, coach_tone=opt['value'], onboarding_step=3)
        return f"❸ 你習慣哪種溝通方式？\n\n請輸入數字：\n{_build_opts_text(STYLE_OPTIONS)}"
    
    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入有效的數字\n\n{_build_opts_text(STYLE_OPTIONS)}"
        save_profile(uid, coach_style=opt['value'], onboarding_step=4)
        return f"❹ 你喜歡我在對話中引用名言嗎？\n\n請輸入數字：\n{_build_opts_text(QUOTE_OPTIONS)}"
    
    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入有效的數字\n\n{_build_opts_text(QUOTE_OPTIONS)}"
        save_profile(uid, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)
        return "✨ 設定完成！我是你的專屬教練，有什麼想聊的？\n\n（輸入 /setting 重新調整）"
    
    return None

# ====== 指令 ======
def handle_command(uid, text, profile):
    cmd = text.strip().lower()
    
    if cmd == '/reset':
        from sqlite3 import connect
        conn = connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM user_conversations WHERE line_user_id = ?', (uid,))
        conn.commit()
        conn.close()
        return "🔄 對話記憶已清除！"
    
    if cmd == '/help':
        return "🤖 指令說明：\n\n🔄 /reset - 清除記憶\n⚙️ /setting - 重新設定\n📋 /profile - 查看設定\n❓ /help - 本說明"
    
    if cmd == '/setting':
        save_profile(uid, onboarding_done=0, onboarding_step=2)
        return f"⚙️ 好的！\n\n❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n{_build_opts_text(TONE_OPTIONS)}"
    
    if cmd == '/profile':
        name = profile.get('display_name') or '未設定'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '未設定')
        return f"📋 你的教練設定：\n\n👤 名字：{name}\n🎯 語氣：{tone_label}\n💬 方式：{style_label}\n📚 引用：{quote_label}"
    
    return None

# ====== Dify ======
def build_dify_inputs(profile):
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

def call_dify(api_key, uid, text, conv_id, inputs):
    url = f'{DIFY_API_URL}/chat-messages'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    data = {"inputs": inputs, "query": text, "response_mode": "blocking", "user": uid}
    if conv_id:
        data["conversation_id"] = conv_id
    response = requests.post(url, headers=headers, json=data, timeout=120)
    response.raise_for_status()
    return response.json()

# ============================
# Base44 API 串接
# ============================

def sync_user_to_base44(line_user_id, profile, total_messages=None):
    """同步用戶資料到 Base44"""
    base44_url = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')
    url = f'{base44_url}/functions/syncUser'
    
    payload = {
        'line_user_id': line_user_id,
        'display_name': profile.get('display_name', ''),
        'coach_tone': profile.get('coach_tone', 'balanced'),
        'coach_style': profile.get('coach_style', 'exploratory'),
        'quote_freq': profile.get('quote_freq', 'sometimes'),
        'total_messages': total_messages if total_messages is not None else (profile.get('total_messages', 0) + 1),
        'reminder_enabled': profile.get('reminder_enabled', False),
        'reminder_time': profile.get('reminder_time', '08:00'),
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.ok:
            print(f"[Base44] User synced: {line_user_id}")
        else:
            print(f"[Base44] Sync failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44] Sync error: {e}")

def detect_and_save_goal_or_event(line_user_id, display_name, user_message, ai_response):
    """偵測用戶訊息中的目標/事件並儲存"""
    base44_url = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')
    url = f'{base44_url}/functions/saveGoalOrEvent'
    
    # 簡單的關鍵詞偵測
    # 更進階的方式是用 LLM 判斷，但先用簡單規則
    
    goal_keywords = ['目標', '想要', '計畫', '目標是', '想達成', '希望', '願景']
    event_keywords = ['完成', '做', '習慣', '待辦', '里程碑', '打卡', '紀錄']
    
    msg_lower = user_message.lower()
    
    # 偵測目標
    if any(kw in user_message for kw in goal_keywords):
        try:
            resp = requests.post(url, json={
                'entity_type': 'goal',
                'line_user_id': line_user_id,
                'display_name': display_name,
                'title': user_message[:50],  # 簡單起見，用訊息前 50 字作為標題
                'description': user_message,
                'type': 'short',  # 預設短期，可由 AI 判斷
            }, timeout=5)
            if resp.ok:
                print(f"[Base44] Goal saved for {line_user_id}")
        except Exception as e:
            print(f"[Base44] Save goal error: {e}")
    
    # 偵測事件（完成相關訊息）
    if any(kw in user_message for kw in event_keywords):
        try:
            resp = requests.post(url, json={
                'entity_type': 'event',
                'line_user_id': line_user_id,
                'display_name': display_name,
                'title': user_message[:50],
                'type': 'todo',
                'status': 'done' if '完成' in user_message else 'pending',
            }, timeout=5)
            if resp.ok:
                print(f"[Base44] Event saved for {line_user_id}")
        except Exception as e:
            print(f"[Base44] Save event error: {e}")

def ask_dify(uid, text, profile):
    # 同步用戶資料到 Base44
    try:
        requests.post(
            'https://app-ffa38ee7.base44.app/functions/syncUser',
            json={
                'line_user_id': uid,
                'display_name': profile.get('display_name', ''),
                'coach_tone': profile.get('coach_tone', 'balanced'),
                'coach_style': profile.get('coach_style', 'exploratory'),
                'quote_freq': profile.get('quote_freq', 'sometimes'),
            },
            timeout=5
        )
    except Exception as e:
        print(f"[syncUser] 失敗: {e}")

    conv_id = get_conversation_id(uid)
    inputs = build_dify_inputs(profile)
    
    try:
        result = call_dify(DIFY_API_KEY, uid, text, conv_id, inputs)
        if result.get('conversation_id'):
            save_conversation_id(uid, result['conversation_id'])
        answer = result.get('answer', '').strip()
        return answer if answer else "🤔 我想到一半忘記說什麼了，請再問我一次！"
    except requests.exceptions.Timeout:
        print(f"[Dify] Timeout")
        if DIFY_API_KEY_FALLBACK:
            try:
                result = call_dify(DIFY_API_KEY_FALLBACK, uid, text, None, inputs)
                answer = result.get('answer', '').strip()
                return ("⚡️ 我暫時切換到備用系統回答你：\n\n" + answer) if answer else "😓 備援系統也沒回應，請稍後再試！"
            except:
                pass
        return "☕ 我剛剛去泡了杯茶回來，結果忘記你問什麼了...\n\n請稍等一下再試試看！"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else '未知'
        return f"🔧 我遇到了一點小問題（錯誤碼：{status}）\n\n先去找 Chris 修一下，請稍後再試！感謝你的耐心 💪"
    except Exception as e:
        print(f"[Dify] 錯誤: {e}")
        return "😵 我剛才靈魂出竅了一下，請再問我一次！\n\n如果問題一直出現，麻煩聯絡開發者 Chris 看看，謝謝你的包容 🙏"

# ====== 目標/事件偵測 ======
def detect_and_save_goal_or_event(uid, display_name, user_input, ai_response):
    """從 Dify 回應中偵測 [GOAL:...] 和 [EVENT:...] 標記，自動儲存到後台"""
    try:
        # 偵測 [GOAL:title|description|type] 標記
        goal_pattern = r'\[GOAL:([^|]+)\|([^|]*)\|([^|]*)\]'
        goal_matches = re.finditer(goal_pattern, ai_response)
        for match in goal_matches:
            title, desc, goal_type = match.groups()
            save_goal_event_to_base44(uid, display_name, 'goal',
                title=title.strip(),
                description=desc.strip(),
                type=goal_type.strip() or 'short'
            )
            print(f"[Auto Save Goal] {title} ({goal_type or 'short'})")
        
        # 偵測 [EVENT:title|type] 標記
        event_pattern = r'\[EVENT:([^|]+)\|([^|]*)\]'
        event_matches = re.finditer(event_pattern, ai_response)
        for match in event_matches:
            title, event_type = match.groups()
            save_goal_event_to_base44(uid, display_name, 'event',
                title=title.strip(),
                type=event_type.strip() or 'todo'
            )
            print(f"[Auto Save Event] {title} ({event_type or 'todo'})")
    except Exception as e:
        print(f"[Detect] 偵測失敗: {e}")


def handle_message(event):
    uid = event.source.user_id
    text = event.message.text
    profile = get_profile(uid)
    
    # 1. 指令
    cmd_resp = handle_command(uid, text, profile)
    if cmd_resp:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=cmd_resp))
        return
    
    # 2. Onboarding
    onb_resp = handle_onboarding(uid, text, profile)
    if onb_resp is not None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=onb_resp))
        return
    
    # 3. AI 對話（背景執行）
    replied_flag = threading.Event()
    
    def process_and_push():
        send_loading_animation(uid, seconds=60)
        current_profile = get_profile(uid)
        
        # 呼叫 Dify
        ai_response = ask_dify(uid, text, current_profile)
        
        # 計數訊息 + 同步用戶到 Base44
        message_count = (current_profile.get('total_messages', 0) or 0) + 1
        sync_user_to_base44(uid, current_profile, total_messages=message_count)
        
        # 偵測並儲存目標/事件（只在有回應時）
        if ai_response:
            detect_and_save_goal_or_event(uid, current_profile.get('display_name', ''), text, ai_response)
        
        if not replied_flag.is_set():
            replied_flag.set()
            line_bot_api.push_message(uid, TextSendMessage(text=ai_response))
    
    threading.Thread(target=process_and_push, daemon=True).start()

# ====== Health Check ======
@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
