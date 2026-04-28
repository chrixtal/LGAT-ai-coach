import os
import sqlite3
import threading
import requests
import json
import httpx
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn
from base44_sync import sync_user, save_goal, save_event, detect_and_save_goal_or_event

# Base44 backend sync
from backend_sync import sync_user, save_goal_or_event, detect_goal_keywords

app = FastAPI()

# --- 環境變數 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("錯誤: 缺少必要的環境變數設定。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

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
    'often:多一點:頻繁引用名言、學術理論或研究數據|sometimes:偶爾就好:偶爾適時引用即可|never:不用:不需要引用，保持簡單直白'
))

# ============================
# SQLite 初始化
# ============================
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
# Backend 同步函數
# ============================

def sync_to_backend(user_id, profile):
    """呼叫 Base44 syncUser function"""
    try:
        url = f'{BASE44_API_URL}/functions/syncUser'
        data = {
            'line_user_id': user_id,
            'display_name': profile.get('display_name', ''),
            'coach_tone': profile.get('coach_tone', 'balanced'),
            'coach_style': profile.get('coach_style', 'exploratory'),
            'quote_freq': profile.get('quote_freq', 'sometimes'),
        }
        resp = requests.post(url, json=data, timeout=10)
        if resp.ok:
            print(f"[Sync] 用戶 {user_id} 已同步到 Base44")
        else:
            print(f"[Sync] 失敗: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[Sync] 錯誤: {e}")


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
    finally:
        # 無論成功或失敗，都嘗試同步用戶資料到 Base44
        sync_user_to_base44(user_id, profile)

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
# Backend API Bridge
# ============================


def sync_user_to_base44(user_id, display_name, coach_tone, coach_style, quote_freq, total_messages):
    """同步用戶資料到 Base44"""
    try:
        url = f"{BASE44_API_URL}/syncUser"
        payload = {
            "line_user_id": user_id,
            "display_name": display_name,
            "coach_tone": coach_tone,
            "coach_style": coach_style,
            "quote_freq": quote_freq,
            "total_messages": total_messages,
        }
        resp = httpx.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[Base44] syncUser 成功 | user={user_id}")
        else:
            print(f"[Base44] syncUser 失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] syncUser 錯誤: {e}")

def detect_and_save_goal_or_event(user_id, display_name, text):
    """偵測對話中的目標/事件關鍵詞並自動儲存"""
    # 簡易關鍵詞偵測（可在 Dify 端加入更複雜的邏輯）
    keywords_goal = ["目標", "想要", "計畫", "要達成", "設定目標", "我的目標"]
    keywords_event = ["習慣", "待辦", "完成", "已經做了", "做完", "記錄", "打卡"]
    
    # 先簡單偵測，後續可由 Dify 回應裡加 [GOAL] [EVENT] tag 來觸發
    for kw in keywords_goal:
        if kw in text:
            try:
                url = f"{BASE44_API_URL}/saveGoalOrEvent"
                payload = {
                    "entity_type": "goal",
                    "line_user_id": user_id,
                    "display_name": display_name,
                    "title": text[:30] + ("..." if len(text) > 30 else ""),
                    "description": text,
                    "type": "short",
                }
                resp = httpx.post(url, json=payload, timeout=10)
                print(f"[Base44] 自動儲存目標 | user={user_id}")
            except Exception as e:
                print(f"[Base44] 儲存目標失敗: {e}")
            break
    
    for kw in keywords_event:
        if kw in text:
            try:
                url = f"{BASE44_API_URL}/saveGoalOrEvent"
                payload = {
                    "entity_type": "event",
                    "line_user_id": user_id,
                    "display_name": display_name,
                    "title": text[:30] + ("..." if len(text) > 30 else ""),
                    "type": "todo",
                }
                resp = httpx.post(url, json=payload, timeout=10)
                print(f"[Base44] 自動儲存事件 | user={user_id}")
            except Exception as e:
                print(f"[Base44] 儲存事件失敗: {e}")
            break

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

    # 背景同步用戶資料到 Base44（非同步，不阻塞回應）
    def sync_user_bg():
        try:
            requests.post(
                'https://app-ffa38ee7.base44.app/functions/syncUser',
                json={
                    'line_user_id': user_id,
                    'display_name': profile.get('display_name', ''),
                    'coach_tone': profile.get('coach_tone', 'balanced'),
                    'coach_style': profile.get('coach_style', 'exploratory'),
                    'quote_freq': profile.get('quote_freq', 'sometimes'),
                    'total_messages': (profile.get('total_messages', 0) or 0) + 1,
                },
                timeout=5
            )
        except Exception as e:
            print(f"[syncUser] 背景同步失敗: {e}")

    threading.Thread(target=sync_user_bg, daemon=True).start()

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

    # 偵測並保存目標/事件（在 AI 對話前）
    auto_save_response = detect_and_save_goal_or_event(
        user_id,
        profile.get('display_name') or '用戶',
        user_text
    )
    if auto_save_response:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=auto_save_response))
        return

    # 3. 正常 AI 對話
    replied_flag = threading.Event()

    def process_and_push():
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        
        # 背景同步用戶資料到 Base44
        sync_user(
            user_id,
            display_name=current_profile.get('display_name'),
            coach_tone=current_profile.get('coach_tone'),
            coach_style=current_profile.get('coach_style'),
            quote_freq=current_profile.get('quote_freq'),
        )
        
        # 取得 AI 回應
        ai_response = ask_dify(user_id, user_text, current_profile)
        
        if not replied_flag.is_set():
            replied_flag.set()
            line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))

    threading.Thread(target=process_and_push, daemon=True).start()

    # 背景偵測目標/事件標籤並存檔
    def detect_and_save_bg():
        try:
            import re
            # 偵測 [GOAL] 和 [EVENT] 標籤
            ai_response = ask_dify(user_id, user_text, get_profile(user_id))
            
            goals = re.findall(r'\[GOAL\]([^\[]+)\[/GOAL\]', ai_response)
            for goal_text in goals:
                parts = [p.strip() for p in goal_text.split('|')]
                if len(parts) >= 2:
                    requests.post(
                        'https://app-ffa38ee7.base44.app/functions/saveGoalOrEvent',
                        json={
                            'entity_type': 'goal',
                            'line_user_id': user_id,
                            'display_name': get_profile(user_id).get('display_name', ''),
                            'title': parts[0],
                            'description': parts[1] if len(parts) > 1 else '',
                            'type': parts[2] if len(parts) > 2 else 'short',
                            'target_date': parts[3] if len(parts) > 3 else '',
                        },
                        timeout=5
                    )

            events = re.findall(r'\[EVENT\]([^\[]+)\[/EVENT\]', ai_response)
            for event_text in events:
                parts = [p.strip() for p in event_text.split('|')]
                if len(parts) >= 2:
                    requests.post(
                        'https://app-ffa38ee7.base44.app/functions/saveGoalOrEvent',
                        json={
                            'entity_type': 'event',
                            'line_user_id': user_id,
                            'display_name': get_profile(user_id).get('display_name', ''),
                            'title': parts[0],
                            'type': parts[1] if len(parts) > 1 else 'todo',
                            'due_date': parts[2] if len(parts) > 2 else '',
                            'recurrence': parts[3] if len(parts) > 3 else 'none',
                        },
                        timeout=5
                    )
        except Exception as e:
            print(f"[detect_and_save] 背景處理失敗: {e}")

    # threading.Thread(target=detect_and_save_bg, daemon=True).start()  # 暫時註解，避免雙重呼叫 Dify

    # 同步用戶資料到 Base44
    threading.Thread(
        target=sync_user_to_base44,
        args=(user_id, profile.get('display_name') or '', profile.get('coach_tone'), 
              profile.get('coach_style'), profile.get('quote_freq'), profile.get('total_messages', 0) + 1),
        daemon=True
    ).start()

    # 檢測並儲存目標/事件
    if not onboarding_response and command_response is None:
        threading.Thread(
            target=detect_and_save_goal_or_event,
            args=(user_id, profile.get('display_name') or '', user_text),
            daemon=True
        ).start()

# ============================
# 健康檢查
# ============================

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
