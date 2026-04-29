# =============================================================================
# LGAT AI 教練 — LINE Bot 後端主程式
# =============================================================================
# 版本歷史：
#   v1.0  2026-04    初始版本：LINE Bot + Dify 串接，基本對話
#   v1.1  2026-04    加入 SQLite 用戶檔案、Onboarding 問卷、教練風格設定
#   v1.2  2026-04    加入 Base44 後台串接：syncUser / saveGoalOrEvent / sendReminders
#                    自動偵測目標/事件關鍵詞並存入資料庫
#   v1.3  2026-04    加入薩提爾冰山探索模式：/toggle、/satir、/exit 指令
#                    薩提爾模組使用獨立 Dify Chatflow（DIFY_SATIR_API_KEY）
#                    每個用戶的薩提爾對話 ID 獨立保存
#   v1.4  2026-04    加入維護模式（MAINTENANCE_MODE 環境變數）
#                    loading animation 改用 thread + push_message 避免 timeout
#                    防重複回應機制（replied_flag）
#   v1.5  2026-04    【重構整理】修復重複函數定義問題（主要 bug 來源）：
#                    - detect_goal_or_event：原本定義 2 次（回傳值不同導致 unpack 錯誤）
#                      → 統一保留回傳 (type, category, keyword) 三值的版本
#                    - sync_user_to_base44：原本定義 4 次（簽名不一致）
#                      → 統一保留 sync_user_to_base44(user_id, profile) 版本
#                    - detect_and_save_goal_or_event：原本定義 3 次（邏輯各異）
#                      → 統一保留最完整的版本（含 goal_progress 偵測）
#                    - call_backend_api / BASE44_API_URL 等常數：移除重複宣告
#                    其餘邏輯（Webhook、Onboarding、指令、Dify、排程）完全不動
#
# 環境變數（Zeabur 設定）：
#   LINE_CHANNEL_ACCESS_TOKEN  — LINE Bot channel access token
#   LINE_CHANNEL_SECRET        — LINE Bot channel secret
#   DIFY_API_KEY               — 主教練 Dify Chatflow API Key
#   DIFY_API_KEY_FALLBACK      — 備援 Dify API Key（選填）
#   DIFY_SATIR_API_KEY         — 薩提爾模組 Dify Chatflow API Key
#   DIFY_API_URL               — Dify API 位址（預設 https://api.dify.ai/v1）
#   DB_PATH                    — SQLite 路徑（預設 /data/lgat.db）
#   COACH_TONE_OPTIONS         — 教練語氣選項（選填，有預設值）
#   COACH_STYLE_OPTIONS        — 溝通方式選項（選填，有預設值）
#   COACH_QUOTE_OPTIONS        — 引用頻率選項（選填，有預設值）
#   MAINTENANCE_MODE           — 維護模式（true/false）
#   BASE44_SYNC_USER_URL       — syncUser function URL
#   BASE44_SAVE_GOAL_URL       — saveGoalOrEvent function URL
#
# 指令列表：
#   /help     — 顯示所有指令
#   /reset    — 清除對話記憶
#   /setting  — 重新設定教練風格
#   /profile  — 查看目前設定
#   /toggle   — 切換薩提爾 ↔ 一般教練模式
#   /satir    — 直接進入薩提爾冰山探索模式
#   /exit     — 離開薩提爾，回到一般教練模式
# =============================================================================

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
from datetime import datetime

# ============================
# 目標/事件關鍵詞偵測（唯一版本）
# 回傳：(entity_type, category, matched_keyword) 三值
# entity_type: 'goal' | 'event' | 'goal_progress' | None
# category:    goal → 'short'|'medium'|'long'
#              event → 'habit'|'todo'|'milestone'
#              goal_progress / None → None
# ============================
def detect_goal_or_event(text):
    """偵測文字中的目標/事件關鍵詞，回傳 (type, category, matched_keyword)"""
    # 進度更新（優先偵測）
    for kw in ['做了', '進度', '進展', '怎麼樣', '更新', '完成了']:
        if kw in text:
            return ('goal_progress', None, kw)

    # 長期目標
    for kw in ['一年', '明年', '長期', '五年', '終身']:
        if kw in text:
            return ('goal', 'long', kw)

    # 中期目標
    for kw in ['三個月', '半年', '中期', '季度']:
        if kw in text:
            return ('goal', 'medium', kw)

    # 短期目標
    for kw in ['希望', '想要', '打算', '這週', '本月', '計畫', '要做', '想達成', '最近', '近期']:
        if kw in text:
            return ('goal', 'short', kw)

    # 習慣
    for kw in ['習慣', '每天', '每週', '打卡', '堅持', '養成']:
        if kw in text:
            return ('event', 'habit', kw)

    # 里程碑
    for kw in ['達成', '通過', '拿到', '升職', '考上']:
        if kw in text:
            return ('event', 'milestone', kw)

    # 待辦
    for kw in ['需要', '今天', '明天', '任務', '完成']:
        if kw in text:
            return ('event', 'todo', kw)

    return (None, None, None)


# ============================
# Base44 共用常數
# ============================
BASE44_DOMAIN = os.environ.get('BASE44_DOMAIN', 'https://app-ffa38ee7.base44.app')
BASE44_API_BASE = f'{BASE44_DOMAIN}/functions'
BACKEND_SYNC_USER_URL = f'{BASE44_API_BASE}/syncUser'
BACKEND_SAVE_GOAL_URL = f'{BASE44_API_BASE}/saveGoalOrEvent'

# Base44 從環境變數取得（相容舊設定）
SYNC_USER_URL = os.environ.get('BASE44_SYNC_USER_URL', BACKEND_SYNC_USER_URL)
SAVE_GOAL_OR_EVENT_URL = os.environ.get('BASE44_SAVE_GOAL_URL', BACKEND_SAVE_GOAL_URL)


def call_backend_api(endpoint, data):
    """呼叫 Base44 backend function"""
    url = f'{BASE44_API_BASE}/{endpoint}'
    try:
        resp = requests.post(url, json=data, timeout=10)
        print(f'[Backend API] {endpoint}: {resp.status_code}')
        return resp.json() if resp.ok else None
    except Exception as e:
        print(f'[Backend API] {endpoint} 失敗: {e}')
        return None


# ============================
# Base44 用戶同步（唯一版本）
# 簽名：sync_user_to_base44(user_id, profile)
# ============================
def sync_user_to_base44(user_id, profile):
    """同步用戶資料到 Base44，非同步執行"""
    if not SYNC_USER_URL:
        return
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
        resp = requests.post(SYNC_USER_URL, json=data, timeout=5)
        if resp.ok:
            print(f"[Base44] syncUser OK | user={user_id}")
        else:
            print(f"[Base44] syncUser failed: {resp.status_code}")
    except Exception as e:
        print(f"[syncUser] 失敗: {e}")


# ============================
# Base44 目標/事件偵測並儲存（唯一版本）
# 使用上方統一的 detect_goal_or_event()
# ============================
def detect_and_save_goal_or_event(user_id, text, profile):
    """偵測對話中的目標/事件關鍵詞，自動存入 Base44"""
    if not SAVE_GOAL_OR_EVENT_URL:
        return
    try:
        display_name = profile.get('display_name', '')
        entity_type, category, keyword = detect_goal_or_event(text)

        if not entity_type:
            return

        payload = {
            'entity_type': entity_type,
            'line_user_id': user_id,
            'display_name': display_name,
        }

        if entity_type == 'goal':
            title = text.split('。')[0][:50] if '。' in text else text[:50]
            payload['title'] = title
            payload['description'] = text
            payload['type'] = category
        elif entity_type == 'goal_progress':
            payload['progress_note'] = text[:100]
        elif entity_type == 'event':
            title = text.split('。')[0][:50] if '。' in text else text[:50]
            payload['title'] = title
            payload['type'] = category
            payload['note'] = text

        resp = requests.post(SAVE_GOAL_OR_EVENT_URL, json=payload, timeout=5)
        if resp.ok:
            print(f"[Base44] 已儲存 {entity_type}/{category}: {payload.get('title', payload.get('progress_note', ''))[:30]}")
        else:
            print(f"[Base44] saveGoalOrEvent 失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] detect_and_save error: {e}")


app = FastAPI()

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
DIFY_SATIR_API_KEY = os.environ.get('DIFY_SATIR_API_KEY', 'app-F7quRfiYD3cOvWYHDzpHueUs')

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
# 薩提爾模式 helpers
# ============================

def get_satir_mode(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT satir_mode, satir_conversation_id FROM user_profiles WHERE line_user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return bool(row[0]), row[1]
    return False, None

def set_satir_mode(user_id, active, conv_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """INSERT INTO user_profiles (line_user_id, satir_mode, satir_conversation_id)
            VALUES (?, ?, ?)
            ON CONFLICT(line_user_id) DO UPDATE SET
                satir_mode = excluded.satir_mode,
                satir_conversation_id = excluded.satir_conversation_id""",
        (user_id, 1 if active else 0, conv_id)
    )
    conn.commit()
    conn.close()

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

    # Step 0：第一次進來
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

    # Step 1：手動輸入名字
    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！請輸入你的名字或暱稱 😊"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return "很高興認識你！🙌\n\n" + _tone_question()

    # Step 2：語氣
    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            valid = '、'.join(TONE_OPTIONS.keys())
            return f"請輸入 {valid} 其中一個數字 😊\n\n" + _tone_question()
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    # Step 3：溝通方式
    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            valid = '、'.join(STYLE_OPTIONS.keys())
            return f"請輸入 {valid} 其中一個數字 😊\n\n" + _style_question()
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    # Step 4：引用頻率
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
# Dify API 呼叫
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


def ask_satir(user_id, text, profile, satir_conv_id=None):
    """呼叫薩提爾 Dify Chatflow"""
    # 只帶 Dify Chatflow 有定義的變數；若 Chatflow 沒有 input 變數則傳空 dict
    # 避免帶入未定義變數導致 400 Bad Request
    inputs = {}
    try:
        result = call_dify(DIFY_SATIR_API_KEY, user_id, text, satir_conv_id, inputs)
        new_conv_id = result.get('conversation_id')
        if new_conv_id:
            set_satir_mode(user_id, True, new_conv_id)
        answer = result.get('answer', '').strip()
        return answer if answer else "🌊 我正在幫你整理思路，請再說一次好嗎？"
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '未知'
        try:
            err_body = e.response.json()
        except Exception:
            err_body = e.response.text[:200] if e.response is not None else ''
        print(f"[Satir Dify] HTTP {status} 錯誤: {err_body}")
        return "😓 薩提爾模式暫時出了問題，請稍後再試，或輸入 /exit 回到一般模式。"
    except Exception as e:
        print(f"[Satir Dify] 錯誤: {e}")
        return "😓 薩提爾模式暫時出了問題，請稍後再試，或輸入 /exit 回到一般模式。"


def ask_dify(user_id, text, profile):
    conversation_id = get_conversation_id(user_id)
    inputs = build_dify_inputs(profile)

    try:
        result = call_dify(DIFY_API_KEY, user_id, text, conversation_id, inputs)
        new_conv_id = result.get('conversation_id')
        if new_conv_id:
            save_conversation_id(user_id, new_conv_id)
        answer = result.get('answer', '').strip()

        # 後台同步（背景執行，不阻擋回應）
        threading.Thread(target=_sync_to_backend, args=(user_id, text, profile, answer), daemon=True).start()

        return answer if answer else "🤔 我想到一半忘記說什麼了，請再問我一次！"

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

# ============================
# 指令處理
# ============================

HELP_TEXT = (
    "🤖 指令說明：\n\n"
    "🔄 /reset    — 清除對話記憶，重新開始\n"
    "⚙️ /setting  — 重新設定教練風格\n"
    "📋 /profile  — 查看目前的設定\n"
    "🔀 /toggle   — 切換薩提爾 ↔ 一般教練模式\n"
    "🌊 /satir    — 直接進入薩提爾冰山探索模式\n"
    "🚪 /exit     — 離開薩提爾，回到一般模式\n"
    "❓ /help     — 顯示這個說明\n\n"
    "直接輸入文字就能和我對話！"
)


def _sync_to_backend(user_id, text, profile, answer):
    """背景同步用戶資料和偵測目標/事件到 Base44 後台"""
    try:
        # 同步用戶資料
        sync_data = {
            'line_user_id': user_id,
            'display_name': profile.get('display_name', ''),
            'coach_tone': profile.get('coach_tone', 'balanced'),
            'coach_style': profile.get('coach_style', 'exploratory'),
            'quote_freq': profile.get('quote_freq', 'sometimes'),
            'total_messages': profile.get('total_messages', 0) + 1,
        }
        sync_resp = requests.post(BACKEND_SYNC_USER_URL, json=sync_data, timeout=5)
        if sync_resp.status_code == 200:
            print(f"[Backend Sync] User {user_id} synced")
        else:
            print(f"[Backend Sync] Failed: {sync_resp.status_code}")

        # 偵測目標/事件
        entity_type, category, keyword = detect_goal_or_event(text)
        if entity_type:
            goal_data = {
                'entity_type': entity_type,
                'line_user_id': user_id,
                'display_name': profile.get('display_name', ''),
            }

            if entity_type == 'goal':
                goal_data['type'] = category
                goal_data['title'] = text[:50]
                goal_data['description'] = f"用戶輸入：{text}"
            elif entity_type == 'event':
                goal_data['type'] = category
                goal_data['title'] = text[:50]
                goal_data['note'] = f"用戶輸入：{text}"
            elif entity_type == 'goal_progress':
                goal_data['progress_note'] = text

            goal_resp = requests.post(BACKEND_SAVE_GOAL_URL, json=goal_data, timeout=5)
            if goal_resp.status_code == 200:
                print(f"[Backend Goal] Saved {entity_type} for {user_id}")
            else:
                print(f"[Backend Goal] Failed: {goal_resp.status_code}")
    except Exception as e:
        print(f"[Backend Sync] Error: {e}")


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

    if cmd in ('/satir', '/exit', '/toggle'):
        in_satir, _ = get_satir_mode(user_id)
        if cmd == '/satir' or (cmd == '/toggle' and not in_satir):
            set_satir_mode(user_id, True, None)
            return (
                "🌊 切換到薩提爾冰山探索模式\n\n"
                "我會陪你一層一層往內看。\n"
                "先跟我說說，最近有什麼事讓你感到困擾或糾結嗎？\n\n"
                "（再次輸入 /toggle 或 /exit 可切回一般模式）"
            )
        else:
            set_satir_mode(user_id, False, None)
            return (
                "✅ 切換回一般教練模式。\n\n"
                "剛才的探索辛苦了，有什麼想繼續聊的嗎？😊"
            )

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

    # 3. 正常 AI 對話 + 後台同步
    replied_flag = threading.Event()

    def process_and_push():
        try:
            # (a) 同步用戶資料到 Base44
            call_backend_api('syncUser', {
                'line_user_id': user_id,
                'display_name': profile.get('display_name', ''),
                'coach_tone': profile.get('coach_tone', 'balanced'),
                'coach_style': profile.get('coach_style', 'exploratory'),
                'quote_freq': profile.get('quote_freq', 'sometimes'),
                'total_messages': (profile.get('total_messages', 0) or 0) + 1,
                'reminder_enabled': profile.get('reminder_enabled', False),
                'reminder_time': profile.get('reminder_time', '08:00'),
            })

            # (b) 偵測並儲存目標/事件
            entity_type, category, keyword = detect_goal_or_event(user_text)
            if entity_type:
                call_backend_api('saveGoalOrEvent', {
                    'entity_type': entity_type,
                    'line_user_id': user_id,
                    'display_name': profile.get('display_name', ''),
                    'title': user_text[:50],
                    'description': user_text,
                    'type': category,
                    'progress_note': user_text if entity_type == 'goal_progress' else '',
                })

            # (c) 送 loading animation + 等 Dify 回應
            send_loading_animation(user_id, seconds=60)
            current_profile = get_profile(user_id)
            in_satir, satir_conv_id = get_satir_mode(user_id)
            if in_satir:
                ai_response = ask_satir(user_id, user_text, current_profile, satir_conv_id)
            else:
                ai_response = ask_dify(user_id, user_text, current_profile)

            if not replied_flag.is_set():
                replied_flag.set()
                line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))
        except Exception as e:
            print(f"[handle_message] 未預期錯誤: {e}")
            if not replied_flag.is_set():
                replied_flag.set()
                line_bot_api.push_message(user_id, TextSendMessage(text="😵 出了點小問題，請再試一次！"))

    threading.Thread(target=process_and_push, daemon=True).start()


@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)

# ============================
# 提醒排程器（背景執行緒）
# ============================

import time
from datetime import datetime, timezone, timedelta

def reminder_scheduler():
    """每分鐘檢查一次，看有沒有用戶需要提醒"""
    last_check_day = None
    checked_today = set()
    _endpoint_disabled = False      # sendReminders 不存在時暫停呼叫
    _disable_until = None           # 暫停到什麼時間
    DISABLE_MINUTES = 60            # 404 後暫停 60 分鐘再重試

    while True:
        try:
            time.sleep(60)  # 每分鐘檢查一次（原本 30 秒改為 60 秒）

            tw_tz = timezone(timedelta(hours=8))
            tw_now = datetime.now(tw_tz)
            current_time_str = tw_now.strftime("%H:%M")
            today_str = tw_now.strftime("%Y-%m-%d")

            if last_check_day != today_str:
                last_check_day = today_str
                checked_today = set()

            # 如果 endpoint 被標記為不存在，等暫停時間到了再重試
            if _disable_until is not None and tw_now < _disable_until:
                continue
            elif _disable_until is not None:
                # 暫停時間到，重置，再試一次
                _disable_until = None
                _endpoint_disabled = False
                print("[Reminder Scheduler] 重新嘗試 sendReminders endpoint...")

            send_url = os.environ.get(
                'BASE44_SEND_REMINDERS_URL',
                f'{BASE44_DOMAIN}/functions/sendReminders'
            )
            resp = requests.post(
                send_url,
                json={
                    "line_token": os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', ''),
                    "current_time": current_time_str,
                    "today": today_str
                },
                timeout=10
            )

            if resp.status_code == 404:
                # endpoint 尚未部署，靜默暫停，不要狂刷 log
                _disable_until = tw_now + timedelta(minutes=DISABLE_MINUTES)
                print(f"[Reminder Scheduler] sendReminders 尚未部署（404），"
                      f"暫停 {DISABLE_MINUTES} 分鐘後重試。")
                continue

            if not resp.ok:
                print(f"[Reminder Scheduler] API 呼叫失敗: {resp.status_code} {resp.text[:100]}")
                continue

            result = resp.json()
            sent = result.get('sent', 0)
            if sent > 0:
                print(f"[Reminder Scheduler] 時間 {current_time_str}，已發送 {sent} 筆提醒")
            # 沒有提醒要送時不印 log，避免 log 爆炸

        except Exception as e:
            print(f"[Reminder Scheduler] 錯誤: {e}")

# 啟動提醒背景執行緒
reminder_thread = threading.Thread(target=reminder_scheduler, daemon=True)
reminder_thread.start()
print("[Reminder Scheduler] 啟動")
