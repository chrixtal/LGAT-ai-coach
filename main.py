#!/usr/bin/env python3
"""
LGAT AI Coach - LINE Bot
完整整合版：Dify AI + Base44 資料庫 + 主動提醒
"""
import os
import sqlite3
import threading
import requests
import json
import re
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import uvicorn

# ============================
# 環境變數
# ============================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')
DB_PATH = os.environ.get('DB_PATH', '/data/lgat.db')

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("⚠️ 缺少必要環境變數！檢查 LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY")

# ============================
# 教練設定選項
# ============================
def parse_options(env_val):
    result = {}
    for i, raw in enumerate(env_val.split('|'), 1):
        parts = raw.strip().split(':')
        if len(parts) == 3:
            result[str(i)] = {'value': parts[0], 'label': parts[1], 'dify': parts[2]}
    return result

TONE_OPTIONS = parse_options(os.environ.get(
    'COACH_TONE_OPTIONS',
    'strict:嚴格督促型:嚴格督促|gentle:溫柔支持型:溫柔支持|balanced:平衡理性型:平衡理性'
))

STYLE_OPTIONS = parse_options(os.environ.get(
    'COACH_STYLE_OPTIONS',
    'direct:直接說重點:直接說重點|exploratory:循循善誘:循循善誘、引導探索'
))

QUOTE_OPTIONS = parse_options(os.environ.get(
    'COACH_QUOTE_OPTIONS',
    'often:多一點:頻繁引用名言、學術理論或研究數據來增加說服力|sometimes:偶爾就好:偶爾適時引用即可|never:不用:不需要引用，保持簡單直白'
))

# ============================
# FastAPI + LINE Bot 初始化
# ============================
app = FastAPI()
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ============================
# SQLite 初始化（本地快取）
# ============================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
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
        total_messages INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

init_db()

# ============================
# DB Helpers
# ============================
def get_conversation_id(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT conversation_id FROM user_conversations WHERE line_user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def save_conversation_id(user_id, conv_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO user_conversations (line_user_id, conversation_id, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(line_user_id) DO UPDATE SET conversation_id=excluded.conversation_id, updated_at=CURRENT_TIMESTAMP''',
        (user_id, conv_id))
    conn.commit()
    conn.close()

def reset_conversation(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM user_conversations WHERE line_user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def get_profile(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM user_profiles WHERE line_user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        'line_user_id': user_id, 'display_name': '', 'coach_tone': 'balanced',
        'coach_style': 'exploratory', 'quote_freq': 'sometimes',
        'onboarding_done': 0, 'onboarding_step': 0, 'total_messages': 0,
    }

def save_profile(user_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO user_profiles (line_user_id) VALUES (?)', (user_id,))
    for k, v in kwargs.items():
        c.execute(f'UPDATE user_profiles SET {k}=?, updated_at=CURRENT_TIMESTAMP WHERE line_user_id=?', (v, user_id))
    conn.commit()
    conn.close()

# ============================
# LINE Helpers
# ============================
def get_line_display_name(user_id):
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name or ''
    except:
        return ''

def send_loading_animation(user_id, seconds=20):
    try:
        requests.post(
            'https://api.line.me/v2/bot/chat/loading/start',
            headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}', 'Content-Type': 'application/json'},
            json={'chatId': user_id, 'loadingSeconds': seconds},
            timeout=5
        )
    except Exception as e:
        print(f"[LINE Loading] 失敗: {e}")

# ============================
# Base44 API 呼叫
# ============================
def call_base44_function(func_name, payload):
    """呼叫 Base44 backend function"""
    try:
        url = f'{BASE44_API_URL}/functions/{func_name}'
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[Base44] {func_name} 成功")
            return resp.json()
        else:
            print(f"[Base44] {func_name} 失敗: {resp.status_code}")
    except Exception as e:
        print(f"[Base44] {func_name} 錯誤: {e}")
    return None

# ============================
# 目標/事件偵測
# ============================
def detect_goal_or_event(text):
    """
    返回 (entity_type, subtype) 或 None
    entity_type: 'goal' 或 'event'
    subtype: 'short'/'medium'/'long' (for goal) 或 'habit'/'todo'/'milestone' (for event)
    """
    goal_keywords = ['目標', '想要', '計畫', '夢想', '希望', '想達成', '想學']
    habit_keywords = ['習慣', '每天', '每週', '每月', '打卡', '養成', '堅持']
    todo_keywords = ['待辦', '要做', '需要做', '必須', '得做', '要完成']
    
    for kw in goal_keywords:
        if kw in text:
            # 簡單啟發式：若提到月份或時間，判定為中期；3個月以上為長期
            if any(m in text for m in ['個月', '半年', '一年']) or '長期' in text:
                return ('goal', 'long')
            elif any(m in text for m in ['個月']) and int(re.search(r'(\d+)', text).group(1) or 1) >= 3:
                return ('goal', 'medium')
            return ('goal', 'short')
    
    for kw in habit_keywords:
        if kw in text:
            return ('event', 'habit')
    
    for kw in todo_keywords:
        if kw in text:
            return ('event', 'todo')
    
    return None

# ============================
# Onboarding 問卷
# ============================
def build_option_text(options):
    emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
    return '\n'.join(f"{emojis[i]} {v['label']}" for i, v in enumerate(options.values()))

def handle_onboarding(user_id, text, profile):
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']
    
    if step == 0:
        line_name = get_line_display_name(user_id)
        if line_name:
            save_profile(user_id, display_name=line_name, onboarding_step=2)
            return (f"👋 嗨，{line_name}！我是你的 AI 生活教練 澄若水 🌊\n\n"
                    "在開始之前，想先了解你喜歡什麼樣的教練風格！\n\n"
                    "❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n" + build_option_text(TONE_OPTIONS))
        else:
            save_profile(user_id, onboarding_step=1)
            return "👋 嗨！我是你的 AI 生活教練 澄若水 🌊\n\n在開始之前，我想先多了解你一點！\n\n❶ 你怎麼稱呼你自己呢？（輸入你的名字或暱稱就好）"
    
    if step == 1:
        answer = text.strip()
        if not answer:
            return "名字不能是空的喔！請輸入你的名字或暱稱 😊"
        save_profile(user_id, display_name=answer, onboarding_step=2)
        return "很高興認識你！🙌\n\n❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n" + build_option_text(TONE_OPTIONS)
    
    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 1-{len(TONE_OPTIONS)} 的數字 😊"
        save_profile(user_id, coach_tone=opt['value'], onboarding_step=3)
        return "❸ 你習慣哪種溝通方式？\n\n請輸入數字：\n" + build_option_text(STYLE_OPTIONS)
    
    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 1-{len(STYLE_OPTIONS)} 的數字 😊"
        save_profile(user_id, coach_style=opt['value'], onboarding_step=4)
        return "❹ 最後一個問題！你喜歡我在對話中引用名言、學術理論或研究嗎？\n\n請輸入數字：\n" + build_option_text(QUOTE_OPTIONS)
    
    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            return f"請輸入 1-{len(QUOTE_OPTIONS)} 的數字 😊"
        save_profile(user_id, quote_freq=opt['value'], onboarding_done=1)
        p = get_profile(user_id)
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == p['coach_tone']), '')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == p['coach_style']), '')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == p['quote_freq']), '')
        return (f"太棒了，{p.get('display_name') or '你'}！✨ 設定完成！\n\n"
                f"📋 你的教練風格：\n• 語氣：{tone_label}\n• 溝通方式：{style_label}\n• 引用頻率：{quote_label}\n\n"
                "從現在開始，我就是你的專屬教練了 💪 有什麼想聊的，直接說吧！")
    
    return None

# ============================
# 指令處理
# ============================
def handle_command(user_id, text, profile):
    cmd = text.strip().lower()
    
    if cmd == '/reset':
        reset_conversation(user_id)
        return "🔄 對話記憶已清除！我們重新開始吧～有什麼想聊的？😊"
    elif cmd == '/help':
        return "🤖 指令說明：\n\n🔄 /reset - 清除記憶\n⚙️ /setting - 重新設定\n📋 /profile - 查看設定\n❓ /help - 說明"
    elif cmd == '/setting':
        save_profile(user_id, onboarding_done=0, onboarding_step=2)
        return "⚙️ 好的！我們來重新調整一下～\n\n❷ 你喜歡什麼樣的教練語氣？\n\n請輸入數字：\n" + build_option_text(TONE_OPTIONS)
    elif cmd == '/profile':
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '未設定')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '未設定')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '未設定')
        return (f"📋 你的教練設定：\n\n👤 名字：{profile.get('display_name') or '未設定'}\n"
                f"🎯 語氣：{tone_label}\n💬 溝通方式：{style_label}\n📚 引用頻率：{quote_label}\n\n用 /setting 可以重新調整～")
    
    return None

# ============================
# Dify 呼叫
# ============================
def call_dify(api_key, user_id, text, conversation_id, inputs):
    url = f'{DIFY_API_URL}/chat-messages'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
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

def build_dify_inputs(profile):
    tone_dify = next((v['dify'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '平衡理性')
    style_dify = next((v['dify'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '循循善誘、引導探索')
    quote_dify = next((v['dify'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '偶爾適時引用即可')
    return {
        "user_name": profile.get('display_name') or '用戶',
        "coach_tone": tone_dify,
        "coach_style": style_dify,
        "quote_freq": quote_dify,
    }

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
        status = e.response.status_code if e.response else '未知'
        return f"🔧 我遇到了一點小問題（錯誤碼：{status}）\n\n先去找 Chris 修一下，請稍後再試！感謝你的耐心 💪"
    except Exception as e:
        print(f"[Dify] 未預期錯誤: {e}")
        return "😵 我剛才靈魂出竅了一下，請再問我一次！\n\n如果問題一直出現，麻煩聯絡開發者 Chris 看看，謝謝你的包容 🙏"

# ============================
# LINE Webhook Handler
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

    # 3. 正常對話：背景執行 loading + Dify + 同步資料 + 偵測目標
    replied_flag = threading.Event()
    
    def process_and_push():
        # 背景：同步用戶資料和偵測目標/事件（不堵塞）
        try:
            call_base44_function('syncUser', {
                'line_user_id': user_id,
                'display_name': profile.get('display_name'),
                'coach_tone': profile.get('coach_tone'),
                'coach_style': profile.get('coach_style'),
                'quote_freq': profile.get('quote_freq'),
                'total_messages': (profile.get('total_messages') or 0) + 1,
            })
        except:
            pass

        detected = detect_goal_or_event(user_text)
        if detected:
            try:
                entity_type, subtype = detected
                call_base44_function('saveGoalOrEvent', {
                    'entity_type': entity_type,
                    'line_user_id': user_id,
                    'display_name': profile.get('display_name', ''),
                    'title': user_text[:50],
                    'type': subtype,
                    'description': user_text,
                })
            except:
                pass

        # 主流程：loading + Dify
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        try:
            ai_response = ask_dify(user_id, user_text, current_profile)
        except Exception as e:
            print(f"[handle_message] 錯誤: {e}")
            ai_response = "😵 出了點小問題，請再試一次！"

        # 只 push 一次
        if not replied_flag.is_set():
            replied_flag.set()
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))
            except Exception as e:
                print(f"[push_message] 失敗: {e}")

    threading.Thread(target=process_and_push, daemon=True).start()

# ============================
# Health Check
# ============================
@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
