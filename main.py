import os
import re
import sqlite3
import threading
import requests
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
SATIR_API_KEY = os.environ.get('SATIR_API_KEY', '')  # 薩提爾冰山探索 Chatflow
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app/functions')

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
    print("警告: 缺少必要的環境變數設定。請檢查 Zeabur 的 Variables。")

# 用空字串兜底，避免環境變數 None 時 crash（會在 /health 顯示警告）
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN or 'MISSING')
handler = WebhookHandler(LINE_CHANNEL_SECRET or 'MISSING')

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
            total_messages INTEGER DEFAULT 0,
            onboarding_done INTEGER DEFAULT 0,
            onboarding_step INTEGER DEFAULT 0,
            satir_mode INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Migration：補缺少的欄位（舊 DB 升級用）
    for sql in [
        "ALTER TABLE user_profiles ADD COLUMN total_messages INTEGER DEFAULT 0",
        "ALTER TABLE user_profiles ADD COLUMN reminder_enabled INTEGER DEFAULT 0",
        "ALTER TABLE user_profiles ADD COLUMN reminder_time TEXT DEFAULT '08:00'",
        "ALTER TABLE user_profiles ADD COLUMN satir_mode INTEGER DEFAULT 0",
    ]:
        try:
            c.execute(sql)
        except Exception:
            pass  # 欄位已存在
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
        'total_messages': 0,
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
# Base44 API 呼叫
# ============================

def sync_user_to_base44(line_user_id, profile):
    """同步用戶資料到 Base44 後台"""
    try:
        resp = requests.post(
            f'{BASE44_API_URL}/syncUser',
            json={
                'line_user_id': line_user_id,
                'display_name': profile.get('display_name'),
                'coach_tone': profile.get('coach_tone'),
                'coach_style': profile.get('coach_style'),
                'quote_freq': profile.get('quote_freq'),
                'total_messages': profile.get('total_messages', 0),
            },
            timeout=5
        )
        if resp.ok:
            print(f"[Base44] syncUser 成功 | user={line_user_id}")
        else:
            print(f"[Base44] syncUser 狀態碼 {resp.status_code}")
    except Exception as e:
        print(f"[Base44] syncUser 失敗: {e}")

def save_goal_or_event(line_user_id, entity_type, title, description="", goal_type="short", target_date="", event_type="todo", recurrence="none"):
    """儲存目標或事件到 Base44"""
    try:
        payload = {
            'entity_type': entity_type,
            'line_user_id': line_user_id,
            'title': title,
            'description': description,
        }
        if entity_type == 'goal':
            payload['type'] = goal_type
            payload['target_date'] = target_date
        elif entity_type == 'event':
            payload['type'] = event_type
            payload['due_date'] = target_date
            payload['recurrence'] = recurrence
        
        resp = requests.post(f'{BASE44_API_URL}/saveGoalOrEvent', json=payload, timeout=5)
        if resp.ok:
            print(f"[Base44] 已儲存 {entity_type}: {title}")
        else:
            print(f"[Base44] 儲存失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] save 失敗: {e}")

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


# ============================
# Base44 API 呼叫
# ============================

BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_API_URL = f'https://app.base44.app/functions'

def call_backend_function(function_name, payload):
    """呼叫 Base44 backend function"""
    try:
        url = f'{BASE44_API_URL}/{function_name}'
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"[Base44] {function_name} 失敗: {response.status_code}")
            return None
    except Exception as e:
        print(f"[Base44] {function_name} 錯誤: {e}")
        return None

def sync_user_to_db(user_id, profile):
    """同步用戶資料到 Base44"""
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

def detect_and_save_goal(user_id, display_name, text):
    """偵測用戶是否提到目標，並儲存"""
    # 簡單的關鍵字偵測
    goal_keywords = ['目標', '目指す', '達成', '計畫', '想要', '要', '決定', '開始']
    text_lower = text.lower()
    
    # 檢查是否包含目標相關關鍵字
    has_goal_keyword = any(kw in text for kw in goal_keywords)
    
    if not has_goal_keyword:
        return None
    
    # 簡單的目標偵測（可以後續改進）
    # 尋找「我想...」「我要...」的句式
    patterns = [
        r'(我想|我要|我決定)(做|達成|完成|學|開始)(.+)',
        r'(目標|計畫)是(.+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            title = match.group(match.lastindex) if match.lastindex else text[:30]
            payload = {
                "entity_type": "goal",
                "line_user_id": user_id,
                "display_name": display_name,
                "title": title.strip(),
                "description": text[:100],
                "type": "short",  # 預設短期，後續可優化
            }
            result = call_backend_function('saveGoalOrEvent', payload)
            if result and result.get('ok'):
                print(f"[Goal] 已儲存目標: {title}")
            return result
    
    return None

def detect_and_save_progress(user_id, display_name, text):
    """偵測用戶是否更新進度"""
    progress_keywords = ['完成', '做完', '達成', '進展', '進度', '正在', '已經']
    
    if not any(kw in text for kw in progress_keywords):
        return None
    
    payload = {
        "entity_type": "goal_progress",
        "line_user_id": user_id,
        "display_name": display_name,
        "progress_note": text[:100],
    }
    return call_backend_function('saveGoalOrEvent', payload)


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
                "❶ 你怎麼稱呼你自己呢？（輸入你的名字或暱稱就好）"
            )

    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！請輸入你的名字或暱稱 😊"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return "很高興認識你！🙌\n\n" + _tone_question()

    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            valid = '、'.join(TONE_OPTIONS.keys())
            return f"請輸入 {valid} 其中一個數字 😊\n\n" + _tone_question()
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            valid = '、'.join(STYLE_OPTIONS.keys())
            return f"請輸入 {valid} 其中一個數字 😊\n\n" + _style_question()
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

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
# Dify inputs 組裝
# ============================

# ============================
# Base44 Backend Functions 呼叫
# ============================

def sync_user_to_base44(user_id, profile):
    """同步用戶資料到 Base44 資料庫"""
    try:
        url = 'https://app-ffa38ee7.base44.app/functions/syncUser'
        data = {
            'line_user_id': user_id,
            'display_name': profile.get('display_name', ''),
            'coach_tone': profile.get('coach_tone', ''),
            'coach_style': profile.get('coach_style', ''),
            'quote_freq': profile.get('quote_freq', ''),
            'total_messages': profile.get('total_messages', 0),
            'reminder_enabled': profile.get('reminder_enabled', False),
            'reminder_time': profile.get('reminder_time', '08:00'),
        }
        resp = requests.post(url, json=data, timeout=5)
        if resp.ok:
            print(f"[syncUser] 同步成功 | user={user_id}")
        else:
            print(f"[syncUser] 失敗: {resp.text}")
    except Exception as e:
        print(f"[syncUser] 錯誤: {e}")

def save_goal_or_event_to_base44(user_id, display_name, entity_type, **fields):
    """儲存目標或事件到 Base44"""
    try:
        url = 'https://app-ffa38ee7.base44.app/functions/saveGoalOrEvent'
        data = {
            'entity_type': entity_type,
            'line_user_id': user_id,
            'display_name': display_name,
            **fields
        }
        resp = requests.post(url, json=data, timeout=5)
        if resp.ok:
            print(f"[saveGoalOrEvent] 儲存成功 | user={user_id} | type={entity_type}")
        else:
            print(f"[saveGoalOrEvent] 失敗: {resp.text}")
    except Exception as e:
        print(f"[saveGoalOrEvent] 錯誤: {e}")


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
# Dify API 呼叫（含備援）
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

# ============================
# Base44 同步 & 目標/事件解析
# ============================

BASE44_FUNCTIONS_URL = os.environ.get('BASE44_FUNCTIONS_URL', 'https://app-ffa38ee7.base44.app/functions')

def sync_user_to_base44(line_user_id, profile):
    """同步用戶資料到 Base44 LgatUser"""
    try:
        resp = requests.post(
            f'{BASE44_FUNCTIONS_URL}/syncUser',
            json={
                'line_user_id': line_user_id,
                'display_name': profile.get('display_name'),
                'coach_tone': profile.get('coach_tone'),
                'coach_style': profile.get('coach_style'),
                'quote_freq': profile.get('quote_freq'),
                'total_messages': profile.get('total_messages', 0),
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Base44] 同步用戶成功: {line_user_id}")
        else:
            print(f"[Base44] 同步失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44] 同步錯誤: {e}")

def parse_and_save_goal_or_event(line_user_id, display_name, ai_response):
    """解析 AI 回應中的目標/事件標記並儲存"""
    try:
        # 檢查是否有 [GOAL] 標記
        if '[GOAL]' in ai_response:
            # 格式: [GOAL]type=short&title=XXX&description=YYY&target_date=YYYY-MM-DD[/GOAL]
            import re
            goal_match = re.search(r'\[GOAL\](.*?)\[/GOAL\]', ai_response)
            if goal_match:
                params = {}
                for kv in goal_match.group(1).split('&'):
                    k, v = kv.split('=', 1)
                    params[k] = v
                
                resp = requests.post(
                    f'{BASE44_FUNCTIONS_URL}/saveGoalOrEvent',
                    json={
                        'entity_type': 'goal',
                        'line_user_id': line_user_id,
                        'display_name': display_name,
                        **params
                    },
                    timeout=5
                )
                if resp.status_code == 200:
                    print(f"[Base44] 目標已保存: {params.get('title', '?')}")
                else:
                    print(f"[Base44] 保存目標失敗: {resp.status_code}")
        
        # 檢查是否有 [EVENT] 標記
        if '[EVENT]' in ai_response:
            # 格式: [EVENT]type=habit&title=XXX&recurrence=daily&due_date=YYYY-MM-DD[/EVENT]
            import re
            event_match = re.search(r'\[EVENT\](.*?)\[/EVENT\]', ai_response)
            if event_match:
                params = {}
                for kv in event_match.group(1).split('&'):
                    k, v = kv.split('=', 1)
                    params[k] = v
                
                resp = requests.post(
                    f'{BASE44_FUNCTIONS_URL}/saveGoalOrEvent',
                    json={
                        'entity_type': 'event',
                        'line_user_id': line_user_id,
                        'display_name': display_name,
                        **params
                    },
                    timeout=5
                )
                if resp.status_code == 200:
                    print(f"[Base44] 事件已保存: {params.get('title', '?')}")
                else:
                    print(f"[Base44] 保存事件失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] 解析標記失敗: {e}")

def _parse_and_save_entities(user_id, answer, profile):
    """偵測回應中的 [GOAL] 和 [EVENT] 標記，自動儲存"""
    goal_pattern = r'\[GOAL\](.*?)\[/GOAL\]'
    event_pattern = r'\[EVENT\](.*?)\[/EVENT\]'
    
    for match in re.finditer(goal_pattern, answer):
        fields = {}
        for pair in match.group(1).split(';'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                fields[k.strip()] = v.strip()
        if 'title' in fields:
            save_goal_or_event(
                user_id,
                'goal',
                title=fields.get('title', ''),
                description=fields.get('description', ''),
                goal_type=fields.get('type', 'short'),
                target_date=fields.get('target_date', '')
            )
    
    for match in re.finditer(event_pattern, answer):
        fields = {}
        for pair in match.group(1).split(';'):
            if '=' in pair:
                k, v = pair.split('=', 1)
                fields[k.strip()] = v.strip()
        if 'title' in fields:
            save_goal_or_event(
                user_id,
                'event',
                title=fields.get('title', ''),
                description=fields.get('description', ''),
                event_type=fields.get('type', 'todo'),
                recurrence=fields.get('recurrence', 'none'),
                target_date=fields.get('due_date', '')
            )


def sync_user_to_base44(line_user_id, profile):
    """非同步同步用戶資料到 Base44"""
    try:
        base44_url = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app/functions/syncUser')
        resp = requests.post(base44_url, json={
            'line_user_id': line_user_id,
            'display_name': profile.get('display_name') or '',
            'coach_tone': profile.get('coach_tone') or 'balanced',
            'coach_style': profile.get('coach_style') or 'exploratory',
            'quote_freq': profile.get('quote_freq') or 'sometimes',
            'total_messages': profile.get('total_messages', 0) + 1,
            'reminder_enabled': profile.get('reminder_enabled', False),
            'reminder_time': profile.get('reminder_time', '08:00'),
            'plan': profile.get('plan', 'free'),
        }, timeout=5)
        if resp.status_code == 200:
            print(f"[syncUser] 同步成功 | user={line_user_id}")
        else:
            print(f"[syncUser] 同步失敗 status={resp.status_code}")
    except Exception as e:
        print(f"[syncUser] 錯誤: {e}")

def ask_dify(user_id, text, profile, api_key_override=None):
    conversation_id = get_conversation_id(user_id)
    inputs = build_dify_inputs(profile)

    try:
        result = call_dify(api_key_override or DIFY_API_KEY, user_id, text, conversation_id, inputs)
        new_conv_id = result.get('conversation_id')
        if new_conv_id:
            save_conversation_id(user_id, new_conv_id)
        answer = result.get('answer', '').strip()
        
        if answer:
            # 追蹤訊息數
            current_total = profile.get('total_messages', 0)
            save_profile(user_id, total_messages=current_total + 1)
            
            # 偵測 [GOAL] 和 [EVENT] 標記，自動儲存
            _parse_and_save_entities(user_id, answer, profile)
            
            # 非同步同步用戶資料到 Base44（不阻擋回應）
            threading.Thread(
                target=lambda: sync_to_base44(user_id, profile, add_message_count=1),
                daemon=True
            ).start()
            
            return answer
        return "🤔 我想到一半忘記說什麼了，請再問我一次！"

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[Dify Primary] 連線問題: {e} | user={user_id}")
        if DIFY_API_KEY_FALLBACK:
            try:
                print(f"[Dify Fallback] 啟動備援 | user={user_id}")
                result = call_dify(DIFY_API_KEY_FALLBACK, user_id, text, None, inputs)
                answer = result.get('answer', '').strip()
                if answer:
                    return "⚡️ 我暫時切換到備用系統回答你：\n\n" + answer
                return "😓 備援系統也沒回應，請稍後再試！"
            except Exception as fe:
                print(f"[Dify Fallback] 失敗: {fe}")
        return (
            "☕ 我剛剛去泡了杯茶回來，結果忘記你問什麼了...\n\n"
            "請稍等一下再試試看！如果一直這樣，請聯絡開發者 Chris 看看哦 🙏"
        )

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '未知'
        error_msg = ''
        try:
            error_msg = e.response.json().get('message', '')
        except Exception:
            pass
        print(f"[Dify] HTTP 錯誤 {status}: {error_msg} | user={user_id}")
        return (
            f"🔧 我遇到了一點小問題（錯誤碼：{status}）\n\n"
            "先去找 Chris 修一下，請稍後再試！感謝你的耐心 💪"
        )

    except Exception as e:
        print(f"[Dify] 未預期錯誤: {e} | user={user_id}")
        return (
            "😵 我剛才靈魂出竅了一下，請再問我一次！\n\n"
            "如果問題一直出現，麻煩聯絡開發者 Chris 看看，謝謝你的包容 🙏"
        )

def sync_to_base44(user_id: str, profile: dict, add_message_count=1):
    """非同步呼叫 syncUser function，保持用戶資料最新"""
    def _sync():
        try:
            import requests as req
            url = "https://app-ffa38ee7.base44.app/functions/syncUser"
            payload = {
                "line_user_id": user_id,
                "display_name": profile.get("display_name", ""),
                "coach_tone": profile.get("coach_tone", "balanced"),
                "coach_style": profile.get("coach_style", "exploratory"),
                "quote_freq": profile.get("quote_freq", "sometimes"),
                "total_messages": (profile.get("total_messages", 0) or 0) + add_message_count,
                "reminder_enabled": profile.get("reminder_enabled", False),
                "reminder_time": profile.get("reminder_time", "08:00"),
                "plan": profile.get("plan", "free"),
            }
            resp = req.post(url, json=payload, timeout=5)
            if resp.status_code == 200:
                print(f"[Base44 Sync] ✅ {user_id}")
            else:
                print(f"[Base44 Sync] ❌ status={resp.status_code}")
        except Exception as e:
            print(f"[Base44 Sync] 錯誤: {e}")
    
    threading.Thread(target=_sync, daemon=True).start()

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

    if cmd in ['/satir', '冰山探索', '薩提爾']:
        save_profile(line_user_id, satir_mode=1)
        reset_conversation(line_user_id)
        p = get_profile(line_user_id)
        name = p.get('display_name') or '你'
        return f'🌊 進入冰山探索模式，我是澄若水。\n\n{name}，最近有什麼讓你有情緒的事嗎？（輸入 /exit 可離開）'
    if cmd in ['/exit', '結束探索']:
        save_profile(line_user_id, satir_mode=0)
        reset_conversation(line_user_id)
        return '✅ 已回到一般教練模式 💪'
    if cmd == '/profile':
        name = profile.get('display_name') or '未設定'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '未設定')
        reminder_info = f"🔔 晨間提醒：{profile.get('reminder_time', '未設定')}" if profile.get('reminder_enabled') else "🔔 晨間提醒：未開啟"
        return (
            f"📋 你的教練設定：\n\n"
            f"👤 名字：{name}\n"
            f"🎯 語氣：{tone_label}\n"
            f"💬 溝通方式：{style_label}\n"
            f"📚 引用頻率：{quote_label}\n"
            f"{reminder_info}\n\n"
            "用 /setting 重新調整風格，/remind HH:MM 設定提醒時間～"
        )

    if cmd.startswith('/remind '):
        # 解析 /remind HH:MM 格式
        time_str = text.strip().split(' ', 1)[1].strip()
        if ':' in time_str:
            try:
                hour, minute = map(int, time_str.split(':'))
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    save_profile(user_id, reminder_enabled=True, reminder_time=time_str)
                    # 非同步同步到 Base44
                    threading.Thread(
                        target=lambda: sync_to_base44(user_id, profile),
                        daemon=True
                    ).start()
                    name = profile.get('display_name') or '你'
                    return f"✅ 設定完成！我每天 {time_str} 會傳晨間問候給 {name} 🌅"
            except:
                pass
        return "⏰ 請輸入正確的時間格式，例如：/remind 08:00"

    if cmd == '/reminder_off':
        save_profile(user_id, reminder_enabled=False)
        threading.Thread(target=lambda: sync_to_base44(user_id, profile), daemon=True).start()
        return "🔕 已關閉晨間提醒"

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

    # 3. 正常 AI 對話：loading animation + 背景 Dify + Base44 同步 + 標記解析
    replied_flag = threading.Event()

    def process_and_push():
        # 同步用戶資料
        profile["total_messages"] = profile.get("total_messages", 0) + 1
        sync_user_to_db(user_id, profile)
        
        # 偵測並儲存目標/進度
        detect_and_save_goal(user_id, profile.get("display_name", ""), user_text)
        detect_and_save_progress(user_id, profile.get("display_name", ""), user_text)
        
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        
        try:
            use_satir = current_profile.get('satir_mode', 0) and SATIR_API_KEY
            ai_response = ask_dify(user_id, user_text, current_profile,
                                   api_key_override=SATIR_API_KEY if use_satir else None)
        except Exception as e:
            print(f"[handle_message] 未預期錯誤: {e}")
            ai_response = "😵 出了點小問題，請再試一次！"
            if not replied_flag.is_set():
                replied_flag.set()
                line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))
            return

        # 更新本地訊息計數
        new_count = (current_profile.get('total_messages') or 0) + 1
        save_profile(user_id, total_messages=new_count)
        current_profile['total_messages'] = new_count

        # 同步用戶資料到 Base44
        sync_user_to_base44(user_id, current_profile)

        # 解析 AI 回應中的目標/事件標記
        # 格式: [GOAL:標題] [EVENT:標題]
        goal_matches = re.findall(r'\[GOAL:([^\]]+)\]', ai_response)
        for goal_title in goal_matches:
            save_goal_or_event(user_id, 'goal', goal_title.strip())

        event_matches = re.findall(r'\[EVENT:([^\]]+)\]', ai_response)
        for event_title in event_matches:
            save_goal_or_event(user_id, 'event', event_title.strip())

        # 移除標記，發送乾淨的回應給用戶
        clean_response = re.sub(r'\[GOAL:[^\]]*\]|\[EVENT:[^\]]*\]', '', ai_response).strip()
        if not clean_response:
            clean_response = ai_response

        if not replied_flag.is_set():
            replied_flag.set()
            line_bot_api.push_message(user_id, TextSendMessage(text=clean_response))

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

BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app/functions')

def sync_user_to_base44(user_id: str, profile: dict):
    """同步用戶資料到 Base44"""
    try:
        data = {
            'line_user_id': user_id,
            'display_name': profile.get('display_name', ''),
            'coach_tone': profile.get('coach_tone', 'balanced'),
            'coach_style': profile.get('coach_style', 'exploratory'),
            'quote_freq': profile.get('quote_freq', 'sometimes'),
            'total_messages': profile.get('total_messages', 0),
            'reminder_enabled': profile.get('reminder_enabled', False),
            'reminder_time': profile.get('reminder_time', '08:00'),
        }
        resp = requests.post(f'{BASE44_API_URL}/syncUser', json=data, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44] 同步用戶 {user_id} 成功")
        else:
            print(f"[Base44] syncUser 失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44] syncUser 異常: {e}")

def extract_goal_or_event(text: str, profile: dict) -> dict | None:
    """
    簡單啟發式偵測對話中是否提到目標或事件
    回傳 {'entity_type': 'goal'/'event', 'title': ..., 'type': ..., ...} 或 None
    """
    text_lower = text.lower()
    
    # 目標關鍵字
    goal_keywords = ['目標', '想要', '想達成', '計畫', 'goal', 'want to', 'plan']
    if any(kw in text_lower for kw in goal_keywords):
        # 簡單啟發：取前 30 字作為標題
        return {
            'entity_type': 'goal',
            'line_user_id': profile.get('line_user_id', ''),
            'display_name': profile.get('display_name', ''),
            'title': text[:30],
            'description': text,
            'type': 'short',  # 預設短期，需要 AI 判斷
        }
    
    # 事件關鍵字
    event_keywords = ['習慣', '待辦', '要做', '明天', '今天', 'habit', 'todo', 'do']
    if any(kw in text_lower for kw in event_keywords):
        return {
            'entity_type': 'event',
            'line_user_id': profile.get('line_user_id', ''),
            'display_name': profile.get('display_name', ''),
            'title': text[:30],
            'type': 'habit' if '習慣' in text else 'todo',
        }
    
    return None

def save_goal_or_event_to_base44(payload: dict):
    """儲存目標或事件到 Base44"""
    try:
        resp = requests.post(f'{BASE44_API_URL}/saveGoalOrEvent', json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44] 儲存 {payload.get('entity_type')} 成功")
        else:
            print(f"[Base44] saveGoalOrEvent 失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44] saveGoalOrEvent 異常: {e}")

