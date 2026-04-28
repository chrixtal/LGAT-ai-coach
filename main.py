import os
import sqlite3
import threading
import requests
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn

# Base44 同步
import requests
import re as regex_re

app = FastAPI()

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
DIFY_SATIR_API_KEY = os.environ.get('DIFY_SATIR_API_KEY', '')   # 薩提爾模式 Dify App
DIFY_SATIR_API_URL = os.environ.get('DIFY_SATIR_API_URL', DIFY_API_URL)
BASE44_APP_ID = os.environ.get('BASE44_APP_ID')
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://api.base44.com')
BASE44_API_BASE = os.environ.get('BASE44_API_BASE', 'https://app-ffa38ee7.base44.app/functions')
DEVELOPER_CONTACT = os.environ.get('DEVELOPER_CONTACT', 'https://line.me/ti/p/your_id')  # 設定開發者聯絡方式

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
    print("⚠️ 缺少必要的環境變數設定")

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
            satir_conversation_id TEXT,
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
            mode TEXT DEFAULT 'coach',
            terms_accepted INTEGER DEFAULT 0,
            onboarding_done INTEGER DEFAULT 0,
            onboarding_step INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 補欄位（若舊資料庫缺少）
    for col, default in [
        ('mode', "'coach'"),
        ('terms_accepted', '0'),
        ('satir_conversation_id', "''"),
    ]:
        try:
            c.execute(f"ALTER TABLE user_profiles ADD COLUMN {col} TEXT DEFAULT {default}")
        except Exception:
            pass
    try:
        c.execute("ALTER TABLE user_conversations ADD COLUMN satir_conversation_id TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

# ============================
# DB helpers
# ============================

def get_conversation_id(line_user_id, mode='coach'):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    col = 'satir_conversation_id' if mode == 'satir' else 'conversation_id'
    c.execute(f'SELECT {col} FROM user_conversations WHERE line_user_id = ?', (line_user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_conversation_id(line_user_id, conversation_id, mode='coach'):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    col = 'satir_conversation_id' if mode == 'satir' else 'conversation_id'
    c.execute(f'''
        INSERT INTO user_conversations (line_user_id, {col}, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(line_user_id) DO UPDATE SET
            {col} = excluded.{col},
            updated_at = CURRENT_TIMESTAMP
    ''', (line_user_id, conversation_id))
    conn.commit()
    conn.close()

def reset_conversation(line_user_id, mode=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if mode == 'satir':
        c.execute('UPDATE user_conversations SET satir_conversation_id = NULL WHERE line_user_id = ?', (line_user_id,))
    elif mode == 'coach':
        c.execute('UPDATE user_conversations SET conversation_id = NULL WHERE line_user_id = ?', (line_user_id,))
    else:
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
        'mode': 'coach',
        'terms_accepted': 0,
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
# LINE API helpers
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
# 免責聲明
# ============================

TERMS_TEXT = (
    "📋 使用前請閱讀說明\n\n"
    "澄若水 是一款 AI 深度覺知輔助工具，提供：\n"
    "• 日常目標追蹤與反思引導\n"
    "• 薩提爾冰山探索（情緒覺察練習）\n"
    "• 個人化教練風格對話\n\n"
    "⚠️ 免責聲明：\n"
    "本工具不提供醫療、心理治療或法律建議。"
    "若您有心理健康相關困擾，請尋求專業人士協助。"
    "AI 回應僅供參考，請以自身判斷為主。\n\n"
    "開發者不對使用本工具所產生的任何後果負責。\n\n"
    "━━━━━━━━━━━━━━━\n"
    "✅ 輸入「同意」即表示你已閱讀並接受上述說明，開始使用。"
)

def handle_terms(line_user_id, text):
    t = text.strip()
    if t in ['同意', '我同意', 'agree', 'yes', 'ok', '好', '好的', '確認']:
        save_profile(line_user_id, terms_accepted=1)
        return None  # 繼續進入 onboarding
    return TERMS_TEXT  # 持續顯示直到同意

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
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    if step == 0:
        line_name = get_line_display_name(line_user_id)
        if line_name:
            save_profile(line_user_id, display_name=line_name, onboarding_step=2)
            return (
                f"👋 嗨，{line_name}！歡迎來到 澄若水 🌊\n\n"
                "這是一個陪你深度覺察自己的空間。\n"
                "在開始之前，想先了解你喜歡什麼樣的風格！\n\n"
                + _tone_question()
            )
        else:
            save_profile(line_user_id, onboarding_step=1)
            return (
                "👋 嗨！歡迎來到 澄若水 🌊\n\n"
                "這是一個陪你深度覺察自己的空間。\n"
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
            f"📋 你的風格設定：\n"
            f"• 語氣：{tone_label}\n"
            f"• 溝通方式：{style_label}\n"
            f"• 引用頻率：{quote_label}\n\n"
            "從現在開始，我陪著你一起覺察成長 🌱\n"
            "有什麼想聊的，直接說吧！\n\n"
            "💡 目前是「深度教練模式」，若想進行薩提爾冰山探索，輸入 /satir 即可切換。"
        )

    return None

# ============================
# Dify inputs 組裝
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
# Base44 整合
# ============================

def call_base44_api(function_name, payload):
    try:
        url = f'{BASE44_API_URL}/apps/{BASE44_APP_ID}/functions/{function_name}'
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[Base44] {function_name} 失敗: {e}")
        return None

def sync_user_to_base44(line_user_id, profile, total_messages=None):
    try:
        url = f'{BASE44_API_URL}/apps/{BASE44_APP_ID}/functions/syncUser'
        payload = {
            "line_user_id": line_user_id,
            "display_name": profile.get('display_name', ''),
            "coach_tone": profile.get('coach_tone', ''),
            "coach_style": profile.get('coach_style', ''),
            "quote_freq": profile.get('quote_freq', ''),
            "total_messages": total_messages or (profile.get('total_messages') or 0),
            "reminder_enabled": profile.get('reminder_enabled', False),
            "reminder_time": profile.get('reminder_time', '08:00'),
        }
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            print(f"[Base44 Sync] ✅ {line_user_id} 已同步")
        else:
            print(f"[Base44 Sync] ❌ {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[Base44 Sync] 錯誤: {e}")

def detect_and_save_goal_or_event(user_id, display_name, user_text):
    keywords_goal = ['目標', '我想', '計畫', '希望', '想要', '夢想', '目的', '預計', '打算']
    keywords_event = ['完成', '做了', '達成', '習慣', '提醒', '待辦', '記下', '追蹤']

    detected = None
    if any(kw in user_text for kw in keywords_goal):
        detected = 'goal'
    elif any(kw in user_text for kw in keywords_event):
        detected = 'event'

    if not detected:
        return

    try:
        payload = {
            'entity_type': detected,
            'line_user_id': user_id,
            'display_name': display_name,
            'title': user_text[:30],
            'description': user_text,
            'type': 'short' if detected == 'goal' else 'todo',
        }
        requests.post(
            f'{BASE44_API_BASE}/saveGoalOrEvent',
            json=payload,
            timeout=5
        )
        print(f"[saveGoalOrEvent] 儲存 {detected} 成功: {user_text[:30]}")
    except Exception as e:
        print(f"[saveGoalOrEvent] 失敗: {e}")

# ============================
# Dify API 呼叫
# ============================

def call_dify(api_key, api_url, user_id, text, conversation_id, inputs):
    url = f'{api_url}/chat-messages'
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
# Base44 資料庫同步
# ============================

BASE44_URL = "https://app-ffa38ee7.base44.app/functions"

def sync_user_to_base44(line_user_id, profile):
    """同步用戶資料到 Base44"""
    try:
        payload = {
            "line_user_id": line_user_id,
            "display_name": profile.get('display_name', ''),
            "coach_tone": profile.get('coach_tone', 'balanced'),
            "coach_style": profile.get('coach_style', 'exploratory'),
            "quote_freq": profile.get('quote_freq', 'sometimes'),
            "total_messages": profile.get('total_messages', 0),
            "reminder_enabled": profile.get('reminder_enabled', False),
            "reminder_time": profile.get('reminder_time', '08:00'),
            "plan": profile.get('plan', 'free'),
        }
        resp = requests.post(f"{BASE44_URL}/syncUser", json=payload, timeout=5)
        if resp.ok:
            print(f"[Base44] syncUser OK for {line_user_id}")
        else:
            print(f"[Base44] syncUser failed: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] syncUser error: {e}")

def detect_and_save_goal_or_event(line_user_id, display_name, text):
    """偵測用戶輸入中的目標/事件關鍵詞，自動儲存"""
    text_lower = text.lower()
    
    goal_patterns = {
        'short': [r'想要\w{2,6}', r'打算\w{2,6}', r'想在\d*天內', r'下個月'],
        'medium': [r'3[到-]6個月', r'半年內', r'今年\w{2,4}'],
        'long': [r'明年', r'長期', r'今年底', r'一年內'],
    }
    
    event_patterns = {
        'habit': [r'每天\w{2,6}', r'每週\w{2,6}', r'堅持\w{2,6}', r'養成\w{4}'],
        'todo': [r'要\w{2,6}', r'需要\w{2,6}', r'今天\w{2,6}', r'明天\w{2,6}'],
        'milestone': [r'完成\w{2,6}', r'達到\w{2,6}', r'達成\w{2,6}', r'通過\w{4}'],
    }
    
    detected = []
    
    for goal_type, patterns in goal_patterns.items():
        for pattern in patterns:
            matches = regex_re.findall(pattern, text_lower)
            if matches:
                for match in matches:
                    detected.append(('goal', goal_type, match, text))
                break
    
    for event_type, patterns in event_patterns.items():
        for pattern in patterns:
            matches = regex_re.findall(pattern, text_lower)
            if matches:
                for match in matches:
                    detected.append(('event', event_type, match, text))
                break
    
    for item_type, sub_type, keyword, full_text in detected:
        try:
            if item_type == 'goal':
                payload = {
                    "entity_type": "goal",
                    "line_user_id": line_user_id,
                    "display_name": display_name,
                    "title": keyword,
                    "description": full_text[:100],
                    "type": sub_type,
                }
            else:
                payload = {
                    "entity_type": "event",
                    "line_user_id": line_user_id,
                    "display_name": display_name,
                    "title": keyword,
                    "type": sub_type,
                    "note": full_text[:100],
                }
            
            resp = requests.post(f"{BASE44_URL}/saveGoalOrEvent", json=payload, timeout=5)
            if resp.ok:
                print(f"[Base44] Saved {item_type} ({sub_type}): {keyword}")
        except Exception as e:
            print(f"[Base44] Save error: {e}")

def ask_dify(user_id, text, profile):
    # 同步用戶資料到 Base44
    sync_user_to_base44(user_id, profile)
    
    mode = profile.get('mode', 'coach')

    # 決定使用哪個 Dify App
    if mode == 'satir' and DIFY_SATIR_API_KEY:
        api_key = DIFY_SATIR_API_KEY
        api_url = DIFY_SATIR_API_URL
    else:
        api_key = DIFY_API_KEY
        api_url = DIFY_API_URL

    conversation_id = get_conversation_id(user_id, mode=mode)
    inputs = build_dify_inputs(profile)

    try:
        result = call_dify(api_key, api_url, user_id, text, conversation_id, inputs)
        new_conv_id = result.get('conversation_id')
        if new_conv_id:
            save_conversation_id(user_id, new_conv_id, mode=mode)
        answer = result.get('answer', '').strip()

        # 非同步更新 Base44 訊息計數
        def sync_async():
            try:
                requests.post(
                    f'{BASE44_API_URL}/apps/{BASE44_APP_ID}/functions/syncUser',
                    json={
                        'line_user_id': user_id,
                        'display_name': profile.get('display_name', ''),
                        'coach_tone': profile.get('coach_tone', ''),
                        'coach_style': profile.get('coach_style', ''),
                        'quote_freq': profile.get('quote_freq', ''),
                        'total_messages': (profile.get('total_messages') or 0) + 1,
                    },
                    timeout=5
                )
            except Exception as e:
                print(f"[syncUser] 失敗: {e}")
        threading.Thread(target=sync_async, daemon=True).start()

        return answer if answer else "🤔 我想到一半忘記說什麼了，請再問我一次！"

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[Dify Primary] 連線問題: {e} | user={user_id}")
        if DIFY_API_KEY_FALLBACK and mode != 'satir':
            try:
                print(f"[Dify Fallback] 啟動備援 | user={user_id}")
                result = call_dify(DIFY_API_KEY_FALLBACK, DIFY_API_URL, user_id, text, None, inputs)
                answer = result.get('answer', '').strip()
                if answer:
                    return "⚡️ 我暫時切換到備用系統回答你：\n\n" + answer
            except Exception as fe:
                print(f"[Dify Fallback] 失敗: {fe}")
        return (
            "☕ 我剛剛去泡了杯茶回來，結果忘記你問什麼了...\n\n"
            "請稍等一下再試試看！如果一直這樣，請聯絡開發者 🙏"
        )

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '未知'
        print(f"[Dify] HTTP 錯誤 {status} | user={user_id}")
        return (
            f"🔧 我遇到了一點小問題（錯誤碼：{status}）\n\n"
            "先去找開發者修一下，請稍後再試！感謝你的耐心 💪"
        )

    except Exception as e:
        print(f"[Dify] 未預期錯誤: {e} | user={user_id}")
        return (
            "😵 我剛才靈魂出竅了一下，請再問我一次！\n\n"
            "如果問題一直出現，麻煩聯絡開發者看看，謝謝你的包容 🙏"
        )

# ============================
# 指令處理
# ============================

HELP_TEXT = (
    "📖 指令說明\n"
    "━━━━━━━━━━━━━━━\n"
    "🔄 /reset     — 清除對話記憶，重新開始\n"
    "⚙️ /setting   — 重新設定教練風格\n"
    "📋 /profile   — 查看目前設定\n"
    "🌊 /satir     — 切換薩提爾冰山探索模式\n"
    "🎯 /coach     — 切換深度教練模式\n"
    "❓ /help      — 顯示這個說明\n"
    "📞 /contact   — 聯絡開發者\n"
    "━━━━━━━━━━━━━━━\n"
    "直接輸入文字就能和我對話！"
)

SATIR_INTRO = (
    "🌊 已切換至【薩提爾冰山探索模式】\n\n"
    "這個模式會陪你慢慢探索內心深處——\n"
    "從表面的行為、說出口的話，\n"
    "一層一層往下，看到感受、想法和真正的期望。\n\n"
    "準備好了嗎？\n"
    "告訴我最近讓你印象深刻、或讓你有些困惑的一件事吧 🌿\n\n"
    "（輸入 /coach 可切換回教練模式）"
)

COACH_INTRO = (
    "🎯 已切換至【深度教練模式】\n\n"
    "我們回到目標導向的對話模式。\n"
    "有什麼目標、計畫或困境想聊聊？直接說吧 💪\n\n"
    "（輸入 /satir 可切換薩提爾模式）"
)

def handle_command(user_id, text, profile):
    cmd = text.strip().lower()

    if cmd in ['/reset', 'reset']:
        reset_conversation(user_id)
        return "🔄 對話記憶已清除！\n\n我們重新開始吧～有什麼想聊的？😊"

    if cmd in ['/help', 'help']:
        return HELP_TEXT

    if cmd in ['/setting', 'setting']:
        save_profile(user_id, onboarding_done=0, onboarding_step=2)
        return "⚙️ 好的！我們來重新調整一下～\n\n" + _tone_question()

    if cmd in ['/profile', 'profile']:
        name = profile.get('display_name') or '未設定'
        mode = profile.get('mode', 'coach')
        mode_label = '🌊 薩提爾冰山探索' if mode == 'satir' else '🎯 深度教練'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '未設定')
        return (
            f"📋 你的設定\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 名字：{name}\n"
            f"🔀 目前模式：{mode_label}\n"
            f"🎯 語氣：{tone_label}\n"
            f"💬 溝通方式：{style_label}\n"
            f"📚 引用頻率：{quote_label}\n"
            f"━━━━━━━━━━━━━━━\n"
            "用 /setting 可以重新調整風格"
        )

    if cmd in ['/satir', 'satir', '薩提爾', '薩提爾模式', '冰山']:
        current_mode = profile.get('mode', 'coach')
        if current_mode == 'satir':
            return "🌊 你目前已經在薩提爾冰山探索模式了～直接說你想探索的事情吧！"
        save_profile(user_id, mode='satir')
        return SATIR_INTRO

    if cmd in ['/coach', 'coach', '教練', '教練模式']:
        current_mode = profile.get('mode', 'coach')
        if current_mode == 'coach':
            return "🎯 你目前已經在教練模式了～有什麼目標想聊？"
        save_profile(user_id, mode='coach')
        return COACH_INTRO

    if cmd in ['/toggle', 'toggle', '切換模式', '切換']:
        current_mode = profile.get('mode', 'coach')
        if current_mode == 'satir':
            save_profile(user_id, mode='coach')
            return COACH_INTRO
        else:
            save_profile(user_id, mode='satir')
            return SATIR_INTRO

    if cmd in ['/contact', 'contact', '聯絡', '聯絡開發者']:
        return (
            "📞 如有問題或建議，歡迎聯絡開發者：\n\n"
            f"👨‍💻 Chris\n"
            f"🔗 {DEVELOPER_CONTACT}\n\n"
            "感謝你的使用與支持 🙏"
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

    # ── 0. 指令優先（任何時候都能用）
    command_response = handle_command(user_id, user_text, profile)
    if command_response:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=command_response))
        return

    # ── 1. 免責聲明確認（全新用戶，尚未同意）
    if not profile.get('terms_accepted'):
        if profile['onboarding_step'] == 0:
            # 第一次進來：顯示免責聲明
            save_profile(user_id)  # 確保記錄存在
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=TERMS_TEXT))
            return
        else:
            # 用戶已看過聲明，等待輸入「同意」
            result = handle_terms(user_id, user_text)
            if result is not None:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=result))
                return
            # 同意了，繼續到 onboarding
            profile = get_profile(user_id)

    # ── 2. Onboarding 問卷（已同意但未完成設定）
    onboarding_response = handle_onboarding(user_id, user_text, profile)
    if onboarding_response is not None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=onboarding_response))
        return

    # ── 3. 正常 AI 對話（非同步）
    replied_flag = threading.Event()

    def process_and_push():
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        try:
            sync_user_to_base44(user_id, current_profile)
            detect_and_save_goal_or_event(user_id, current_profile.get('display_name', ''), user_text)
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
