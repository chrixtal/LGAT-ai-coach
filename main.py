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

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')

# Base44 Functions URLs
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')
BASE44_SYNC_URL = f"{BASE44_API_URL}/functions/syncUser"
BASE44_SAVE_URL = f"{BASE44_API_URL}/functions/saveGoalOrEvent"
BASE44_SEND_REMINDERS_URL = f"{BASE44_API_URL}/functions/sendReminders"

# --- Base44 Backend Functions URLs ---

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
    print("⚠️ 警告：缺少必要環境變數。請檢查 Zeabur Variables 設定。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- SQLite 初始化 ---
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
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
    c.execute('''INSERT INTO user_conversations (line_user_id, conversation_id, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(line_user_id) DO UPDATE SET conversation_id = excluded.conversation_id, updated_at = CURRENT_TIMESTAMP''',
        (line_user_id, conversation_id))
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
# LINE Helpers
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
# Base44 Backend Integration
# ============================

def sync_user_to_backend(line_user_id, profile, total_messages=0):
    """同步用戶資料到 Base44 後台"""
    try:
        payload = {
            "line_user_id": line_user_id,
            "display_name": profile.get("display_name", ""),
            "coach_tone": profile.get("coach_tone", "balanced"),
            "coach_style": profile.get("coach_style", "exploratory"),
            "quote_freq": profile.get("quote_freq", "sometimes"),
            "total_messages": total_messages,
            "reminder_enabled": profile.get("reminder_enabled", False),
            "reminder_time": profile.get("reminder_time", "08:00"),
            "plan": profile.get("plan", "free"),
        }
        resp = requests.post(BASE44_SYNC_URL, json=payload, timeout=5)
        if resp.ok:
            print(f"[syncUser] ✅ {line_user_id}")
        else:
            print(f"[syncUser] ❌ {resp.status_code}")
    except Exception as e:
        print(f"[syncUser] 錯誤: {e}")

def detect_and_save_goals_events(line_user_id, display_name, text):
    """偵測文本中的目標和事件，自動存到 Base44"""
    goal_patterns = [
        r'(我想|目標|我要|計畫|想要)([^。!?]*?)(?:,|。|!|$)',
        r'(今年|這個月|下個月|明年|本週).*?(學|做|完成|達成|跑|讀|運動)([^。!?]*)',
    ]
    
    event_patterns = [
        r'(完成了|做了|跑|讀|運動|健身|冥想|打卡)([^。!?]*)',
        r'(今天|剛才|昨天).*?(完成|做|跑|讀)([^。!?]*)',
    ]
    
    # 偵測目標
    for pattern in goal_patterns:
        try:
            matches = re.findall(pattern, text)
            for match in matches:
                goal_title = ''.join(match).strip()[:50]
                if len(goal_title) > 3:
                    try:
                        requests.post(BASE44_SAVE_URL, json={
                            "entity_type": "goal",
                            "line_user_id": line_user_id,
                            "display_name": display_name,
                            "title": goal_title,
                            "type": "short",
                        }, timeout=5)
                        print(f"[Goal] 記錄: {goal_title}")
                    except:
                        pass
        except:
            pass
    
    # 偵測事件
    for pattern in event_patterns:
        try:
            matches = re.findall(pattern, text)
            for match in matches:
                event_title = ''.join(match).strip()[:50]
                if len(event_title) > 2:
                    try:
                        requests.post(BASE44_SAVE_URL, json={
                            "entity_type": "event",
                            "line_user_id": line_user_id,
                            "display_name": display_name,
                            "title": event_title,
                            "type": "todo",
                        }, timeout=5)
                        print(f"[Event] 記錄: {event_title}")
                    except:
                        pass
        except:
            pass

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
    return "❹ 最後一個問題！你喜歡我在對話中引用名言或理論嗎？\n\n請輸入數字：\n" + _build_options_text(QUOTE_OPTIONS)

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
            return "👋 嗨！我是你的 AI 生活教練 澄若水 🌊\n\n在開始之前，你怎麼稱呼你自己呢？（輸入你的名字或暱稱）"

    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！請輸入你的名字或暱稱 😊"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return "很高興認識你！🙌\n\n" + _tone_question()

    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入有效的數字 😊\n\n" + _tone_question()
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入有效的數字 😊\n\n" + _style_question()
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入有效的數字 😊\n\n" + _quote_question()
        save_profile(line_user_id, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)
        p = get_profile(line_user_id)
        name = p['display_name'] or '你'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == p['coach_tone']), '')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == p['coach_style']), '')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == p['quote_freq']), '')
        return (
            f"太棒了，{name}！✨ 設定完成！\n\n"
            f"📋 你的教練風格：• 語氣：{tone_label}\n• 溝通方式：{style_label}\n• 引用頻率：{quote_label}\n\n"
            "從現在開始，我就是你的專屬教練了 💪\n有什麼想聊的，直接說吧！"
        )

    return None

# ============================
# Dify Inputs & API
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
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    data = {"inputs": inputs, "query": text, "response_mode": "blocking", "user": user_id}
    if conversation_id:
        data["conversation_id"] = conversation_id
    response = requests.post(url, headers=headers, json=data, timeout=120)
    response.raise_for_status()
    return response.json()


# ============================
# Base44 API 橋接
# ============================

def sync_user_to_base44(line_user_id, profile):
    """同步用戶資料到 Base44 LgatUser"""
    try:
        payload = {
            'line_user_id': line_user_id,
            'display_name': profile.get('display_name') or '',
            'coach_tone': profile.get('coach_tone') or 'balanced',
            'coach_style': profile.get('coach_style') or 'exploratory',
            'quote_freq': profile.get('quote_freq') or 'sometimes',
            'total_messages': profile.get('total_messages') or 0,
            'reminder_enabled': profile.get('reminder_enabled') or False,
            'reminder_time': profile.get('reminder_time') or '08:00',
            'plan': 'free',  # 預設免費版
        }
        resp = requests.post(BASE44_SYNC_URL, json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44] 用戶 {line_user_id} 同步成功")
        else:
            print(f"[Base44] 同步失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44] 同步錯誤: {e}")

def save_goal_or_event_to_base44(line_user_id, display_name, entity_type, **fields):
    """儲存目標或事件到 Base44"""
    try:
        payload = {
            'line_user_id': line_user_id,
            'display_name': display_name,
            'entity_type': entity_type,  # goal / event / goal_progress
            **fields
        }
        resp = requests.post(BASE44_SAVE_URL, json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44] {entity_type} 儲存成功")
            return resp.json()
        else:
            print(f"[Base44] 儲存失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44] 儲存錯誤: {e}")

def detect_and_save_goal_or_event(line_user_id, display_name, text):
    """偵測文本中的目標/事件關鍵詞，自動儲存"""
    import re
    
    # 目標關鍵詞
    goal_patterns = [
        (r'(我的目標是|我想要|我要)(.*?)([。！？\n]|$)', 'goal'),
        (r'(短期目標|中期目標|長期目標)[是：]?(.*?)([。！？\n]|$)', 'goal'),
    ]
    
    # 事件關鍵詞
    event_patterns = [
        (r'(我要完成|我得|我必須)(.*?)([。！？\n]|$)', 'todo'),
        (r'(我的習慣是|每天都要|每週)(.*?)([。！？\n]|$)', 'habit'),
        (r'(我完成了|我做到了)(.*?)([。！？\n]|$)', 'event_done'),
    ]
    
    # 偵測目標
    for pattern, _ in goal_patterns:
        match = re.search(pattern, text)
        if match:
            title = match.group(2).strip() if len(match.groups()) >= 2 else '未命名目標'
            if title and len(title) > 2:
                save_goal_or_event_to_base44(
                    line_user_id, display_name,
                    'goal',
                    title=title,
                    description='',
                    type='short'
                )
            break
    
    # 偵測事件
    for pattern, event_type in event_patterns:
        match = re.search(pattern, text)
        if match:
            title = match.group(2).strip() if len(match.groups()) >= 2 else '未命名事件'
            if title and len(title) > 2:
                if event_type == 'event_done':
                    # 找最新的目標並標記完成
                    save_goal_or_event_to_base44(
                        line_user_id, display_name,
                        'goal_progress',
                        title='',
                        status='completed',
                        progress_note=title
                    )
                else:
                    save_goal_or_event_to_base44(
                        line_user_id, display_name,
                        'event',
                        title=title,
                        type=event_type,
                        due_date=''
                    )
            break

# ============================
# Base44 同步
# ============================

def sync_user_to_base44(user_id, profile):
    """同步用戶資料到 Base44，每次對話都呼叫"""
    try:
        base44_url = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app/functions/syncUser')
        resp = requests.post(
            base44_url,
            json={
                'line_user_id': user_id,
                'display_name': profile.get('display_name') or '',
                'coach_tone': profile.get('coach_tone') or 'balanced',
                'coach_style': profile.get('coach_style') or 'exploratory',
                'quote_freq': profile.get('quote_freq') or 'sometimes',
                'total_messages': profile.get('total_messages', 0),
                'reminder_enabled': profile.get('reminder_enabled', False),
                'reminder_time': profile.get('reminder_time', '08:00'),
                'plan': profile.get('plan', 'free'),
            },
            timeout=5
        )
        if not resp.ok:
            print(f"[Base44 Sync] 失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44 Sync] 錯誤: {e}")

def detect_and_save_goals_events(user_id, user_text, dify_response, profile):
    """從用戶輸入和 AI 回應中偵測並儲存目標/事件"""
    BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')
    
    # 關鍵詞定義
    goal_keywords = ['目標', '想要', '計畫', '希望', '夢想', '要達成', '完成']
    event_keywords = ['習慣', '待辦', '今天', '每天', '完成了', '做到', '任務']
    
    text_combined = (user_text + dify_response).lower()
    
    # 簡單的目標偵測
    if any(kw in text_combined for kw in goal_keywords) and len(user_text) > 10:
        try:
            requests.post(
                f'{BASE44_API_URL}/functions/saveGoalOrEvent',
                json={
                    'entity_type': 'goal',
                    'line_user_id': user_id,
                    'display_name': profile.get('display_name', ''),
                    'title': user_text[:50],
                    'description': user_text,
                    'type': 'short',
                },
                timeout=5
            )
            print(f"[Base44] 儲存目標: {user_text[:30]}")
        except Exception as e:
            print(f"[Base44] saveGoalOrEvent 失敗: {e}")
    
    # 事件偵測
    if any(kw in text_combined for kw in event_keywords) and len(user_text) > 5:
        try:
            requests.post(
                f'{BASE44_API_URL}/functions/saveGoalOrEvent',
                json={
                    'entity_type': 'event',
                    'line_user_id': user_id,
                    'display_name': profile.get('display_name', ''),
                    'title': user_text[:50],
                    'type': 'todo',
                    'recurrence': 'daily' if '每天' in user_text else 'none',
                },
                timeout=5
            )
            print(f"[Base44] 儲存事件: {user_text[:30]}")
        except Exception as e:
            print(f"[Base44] saveGoalOrEvent 失敗: {e}")


def ask_dify(user_id, text, profile):
    # 同步用戶資料到 Base44
    sync_user_to_base44(user_id, profile)
    
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
        print(f"[Dify] 連線問題: {e}")
        if DIFY_API_KEY_FALLBACK:
            try:
                result = call_dify(DIFY_API_KEY_FALLBACK, user_id, text, None, inputs)
                answer = result.get('answer', '').strip()
                if answer:
                    return "⚡️ 我暫時切換到備用系統回答你：\n\n" + answer
                return "😓 備援系統也沒回應，請稍後再試！"
            except:
                pass
        return "☕ 我剛剛去泡了杯茶回來，結果忘記你問什麼了...\n\n請稍等一下再試試看！"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '未知'
        print(f"[Dify] HTTP {status}: {e}")
        return f"🔧 我遇到了一點小問題（錯誤碼：{status}）\n\n先去找 Chris 修一下，請稍後再試！"
    except Exception as e:
        print(f"[Dify] 未預期錯誤: {e}")
        return "😵 我剛才靈魂出竅了一下，請再問我一次！"

# ============================
# Commands
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
        tone = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        quote = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '未設定')
        return f"📋 你的教練設定：\n\n👤 名字：{name}\n🎯 語氣：{tone}\n💬 溝通方式：{style}\n📚 引用：{quote}"

    return None

# ============================
# Webhook & Handler
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
    cmd_resp = handle_command(user_id, user_text, profile)
    if cmd_resp:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=cmd_resp))
        return

    # 2. Onboarding
    onb_resp = handle_onboarding(user_id, user_text, profile)
    if onb_resp is not None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=onb_resp))
        return

    # 3. AI 對話 + 後台同步
    replied_flag = threading.Event()

    def process_and_push():
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        try:
            ai_response = ask_dify(user_id, user_text, current_profile)
        except Exception as e:
            print(f"[ask_dify] 錯誤: {e}")
            ai_response = "😵 出了點小問題，請再試一次！"
        if not replied_flag.is_set():
            replied_flag.set()
            line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))
        
        # 背景同步
        try:
            sync_user_to_backend(user_id, current_profile, (current_profile.get('total_messages') or 0) + 1)
        except:
            pass
        
        # 背景偵測
        if current_profile['onboarding_done']:
            try:
                detect_and_save_goals_events(user_id, current_profile.get('display_name', ''), user_text)
            except:
                pass

    threading.Thread(target=process_and_push, daemon=True).start()

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
