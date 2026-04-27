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

# --- \u74b0\u5883\u8b8a\u6578 ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DIFY_API_KEY = os.environ.get('DIFY_API_KEY')
DIFY_API_URL = os.environ.get('DIFY_API_URL', 'https://api.dify.ai/v1')
DIFY_API_KEY_FALLBACK = os.environ.get('DIFY_API_KEY_FALLBACK', '')
BASE44_APP_URL = os.environ.get('BASE44_APP_URL', 'https://app-ffa38ee7.base44.app')

# ============================
# \u6559\u7df4\u8a2d\u5b9a\uff08\u5f9e\u74b0\u5883\u8b8a\u6578\u8b80\u53d6\uff09
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
    'strict:\u56b4\u683c\u7763\u4fc3\u578b\uff08\u63a8\u4f60\u4e00\u628a\uff0c\u4e0d\u7559\u60c5\u9762\uff09:\u56b4\u683c\u7763\u4fc3|gentle:\u6eab\u67d4\u652f\u6301\u578b\uff08\u50cf\u671b\u53cb\u4e00\u6a23\u9678\u4f34\u4f60\uff09:\u6eab\u67d4\u652f\u6301|balanced:\u5e73\u8861\u7406\u6027\u578b\uff08\u8996\u60c5\u6cc1\u8abf\u6574\uff09:\u5e73\u8861\u7406\u6027'
))

STYLE_OPTIONS = _parse_options(os.environ.get(
    'COACH_STYLE_OPTIONS',
    'direct:\u76f4\u63a5\u8aaa\u91cd\u9ede\uff08\u6211\u8981\u7b54\u6848\uff0c\u4e0d\u8981\u7e9e\u5f4e\u5b50\uff09:\u76f4\u63a5\u8aaa\u91cd\u9ede|exploratory:\u5faa\u5faa\u5584\u8a95\uff08\u9663\u6211\u6162\u6162\u60f3\u6e05\u695a\uff09:\u5faa\u5faa\u5584\u8a95\u3001\u5f15\u5c0e\u63a2\u7d22'
))

QUOTE_OPTIONS = _parse_options(os.environ.get(
    'COACH_QUOTE_OPTIONS',
    'often:\u591a\u4e00\u9ede\uff0c\u6211\u559c\u6b61\u6709\u6839\u64da\u7684\u6771\u897f:\u9801\u7e41\u5f15\u7528\u540d\u8a00\u3001\u5b78\u8853\u7406\u8ad6\u6216\u7814\u7a76\u6578\u64da\u4f86\u5f4e\u52a0\u8aaa\u670d\u529b|sometimes:\u5076\u723e\u5c31\u597d\uff0c\u4e0d\u8981\u592a\u591a:\u5076\u723e\u9069\u6642\u5f15\u7528\u5373\u53ef|never:\u4e0d\u7528\uff0c\u6211\u6bd4\u8f03\u559c\u6b61\u7c21\u55ae\u76f4\u767d:\u4e0d\u9700\u8981\u5f15\u7528\uff0c\u4fdd\u6301\u7c21\u55ae\u76f4\u767d'
))

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, DIFY_API_KEY]):
    print("\u932f\u8aa4: \u7f3a\u5c11\u5fc5\u8981\u7684\u74b0\u5883\u8b8a\u6578\u8a2d\u5b9a\u3002\u8acb\u691c\u67e5 Zeabur \u7684 Variables \u8a2d\u5b9a\u3002")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- SQLite \u521d\u59cb\u5316 ---
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
            total_messages INTEGER DEFAULT 0,
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
# LINE helpers
# ============================

def get_line_display_name(user_id):
    """?\u5f9e LINE API \u62ac\u7528\u6236\u6232\u7a30\uff0c\u5931\u6557\u56de\u50b3\u7a7a\u5b57\u4e32"""
    try:
        profile = line_bot_api.get_profile(user_id)
        return profile.display_name or ''
    except Exception as e:
        print(f"[LINE] \u7121\u6cd5\u53d6\u5f97\u6232\u7a43: {e}")
        return ''

def send_loading_animation(user_id, seconds=20):
    """?\u4f46\u7d46 LINE loading animation API\uff0c\u5c0d\u8a71\u6846\u986f\u793a\u4e09\u500b\u5f69\u8272\u9ede"""
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
        print(f"[LINE Loading] \u5931\u6557: {e}")

# ============================
# Base44 \u540c\u6b65 & \u81ea\u52d5\u5132\u5b58
# ============================

def sync_user_to_base44(line_user_id):
    """\u540c\u6b65\u7528\u6236\u8cc7\u6599\u5230 Base44 LgatUser \u8868"""
    profile = get_profile(line_user_id)
    payload = {
        "line_user_id": line_user_id,
        "display_name": profile.get('display_name', ''),
        "coach_tone": profile.get('coach_tone', 'balanced'),
        "coach_style": profile.get('coach_style', 'exploratory'),
        "quote_freq": profile.get('quote_freq', 'sometimes'),
        "total_messages": (profile.get('total_messages', 0) or 0) + 1,
    }
    try:
        resp = requests.post(
            f'{BASE44_APP_URL}/functions/syncUser',
            json=payload,
            timeout=5
        )
        if resp.ok:
            print(f"[syncUser] {line_user_id} \u540c\u6b65\u6210\u529f")
            save_profile(line_user_id, total_messages=payload["total_messages"])
        else:
            print(f"[syncUser] \u5931\u6557: {resp.status_code}")
    except Exception as e:
        print(f"[syncUser] \u7570\u5e38: {e}")

def detect_and_save_goal_or_event(line_user_id, user_text, profile):
    """\u81ea\u52d5\u507d\u6a19\u76ee\u6a19/\u4e8b\u4ef6\u95dc\u9375\u8a5e\u4e26\u5b58\u5230 Base44"""
    keywords_goal = ['\u76ee\u6a19', '\u60f3', '\u8a08\u7b56', '\u5922\u60f3', '\u5e0c\u671b', '\u60f3\u8981', '\u8a2d\u5b9a', '\u9054\u6210']
    keywords_event = ['\u5b8c\u6210', '\u505a', '\u7fd2\u6163', '\u6253\u5361', '\u5f85\u8fa6', '\u4efb\u52d9', '\u660e\u5929', '\u4eca\u5929']
    
    score_goal = sum(user_text.count(kw) for kw in keywords_goal)
    score_event = sum(user_text.count(kw) for kw in keywords_event)
    
    if score_goal >= 2:
        match = re.search(r'(\u6211\u60f3|\u8a08\u7b56|\u76ee\u6a19|\u5922\u60f3|\u5e0c\u671b)([^\u3002\n]+)', user_text)
        title = match.group(2).strip() if match else user_text[:30]
        
        payload = {
            "entity_type": "goal",
            "line_user_id": line_user_id,
            "display_name": profile.get('display_name', ''),
            "title": title,
            "description": user_text,
            "type": "short",
        }
        try:
            resp = requests.post(
                f'{BASE44_APP_URL}/functions/saveGoalOrEvent',
                json=payload,
                timeout=5
            )
            if resp.ok:
                print(f"[saveGoal] {line_user_id} \u5df2\u5132\u5b58: {title}")
            else:
                print(f"[saveGoal] \u5931\u6557: {resp.status_code}")
        except Exception as e:
            print(f"[saveGoal] \u7570\u5e38: {e}")
    
    if score_event >= 2:
        match = re.search(r'(\u5b8c\u6210|\u505a|\u7fd2\u6146|\u6253\u5361|\u5f85\u8fa6)([^\u3002\n]+)', user_text)
        title = match.group(2).strip() if match else user_text[:30]
        
        payload = {
            "entity_type": "event",
            "line_user_id": line_user_id,
            "display_name": profile.get('display_name', ''),
            "title": title,
            "type": "todo",
        }
        try:
            resp = requests.post(
                f'{BASE44_APP_URL}/functions/saveGoalOrEvent',
                json=payload,
                timeout=5
            )
            if resp.ok:
                print(f"[saveEvent] {line_user_id} \u5df2\u5132\u5b58: {title}")
            else:
                print(f"[saveEvent] \u5931\u6557: {resp.status_code}")
        except Exception as e:
            print(f"[saveEvent] \u7570\u5e38: {e}")

# ============================
# \u554f\u5377 Onboarding
# ============================

def _build_options_text(options):
    emojis = ['1\ufe0f\u20e3', '2\ufe0f\u20e3', '3\ufe0f\u20e3', '4\ufe0f\u20e3', '5\ufe0f\u20e3']
    return '\n'.join(f"{emojis[i]} {v['label']}" for i, v in enumerate(options.values()))

def _tone_question():
    return "\u2772 \u4f60\u559c\u6b61\u4ec0\u9ebc\u6a23\u7684\u6559\u7df4\u8a9e\u6c23\uff1f\n\n\u8acb\u8f38\u5165\u6578\u5b57\uff1a\n" + _build_options_text(TONE_OPTIONS)

def _style_question():
    return "\u2773 \u4f60\u7fd2\u6146\u54ea\u7a2e\u6eab\u901a\u65b9\u5f0f\uff1f\n\n\u8acb\u8f38\u5165\u6578\u5b57\uff1a\n" + _build_options_text(STYLE_OPTIONS)

def _quote_question():
    return "\u2774 \u6700\u5f8c\u4e00\u500b\u554f\u984c\uff01\n\n\u4f60\u559c\u6b61\u6211\u5728\u5c0d\u8a71\u4e2d\u5f15\u7528\u540d\u8a00\u3001\u5b78\u8853\u7406\u8ad6\u6216\u7814\u7a76\u55ce\uff1f\n\n\u8acb\u8f38\u5165\u6578\u5b57\uff1a\n" + _build_options_text(QUOTE_OPTIONS)

def handle_onboarding(line_user_id, text, profile):
    """\u56de\u50b3 None \u8868\u793a onboarding \u5df2\u5b8c\u6210\uff1b\u56de\u50b3\u5b57\u4e32\u8868\u793a\u9032\u884c\u4e2d"""
    if profile['onboarding_done']:
        return None

    step = profile['onboarding_step']

    # Step 0\uff1a\u7b2c\u4e00\u6b21\u9032\u4f86
    if step == 0:
        line_name = get_line_display_name(line_user_id)
        if line_name:
            save_profile(line_user_id, display_name=line_name, onboarding_step=2)
            return (
                f"\U0001f44b \u5669\uff0c{line_name}\uff01\u6211\u662f\u4f60\u7684 AI \u751f\u6d3b\u6559\u7df4 \u6fb3\u82e5\u6c34 \U0001f30a\n\n"
                "\u5728\u958b\u59cb\u4e4b\u524d\uff0c\u60f3\u5148\u4e86\u89e3\u4f60\u559c\u6b61\u4ec0\u9ebc\u6a23\u7684\u6559\u7df4\u98a8\u683c\uff01\n\n"
                + _tone_question()
            )
        else:
            save_profile(line_user_id, onboarding_step=1)
            return (
                "\U0001f44b \u5022\uff01\u6211\u662f\u4f60\u7684 AI \u751f\u6d3b\u6559\u7df4 \u6fb3\u82e5\u6c34 \U0001f30a\n\n"
                "\u5728\u958b\u59cb\u4e4b\u524d\uff0c\u6211\u60f3\u5148\u591a\u4e86\u89e3\u4f60\u4e00\u9ede\uff01\n\n"
                "\u2460 \u4f60\u600e\u9ebc\u7a31\u547c\u4f60\u81ea\u5df1\u5462\uff1f\uff08\u8f38\u5165\u4f60\u7684\u540d\u5b57\u6216\u6232\u7a3c\u5c31\u597d\uff09"
            )

    # Step 1\uff1a\u624b\u52d5\u8f38\u5165\u540d\u5b57\uff08\u62ac\u4e0d\u5230\u6232\u7a3c\u624d\u6703\u5230\u9019\u88e1\uff09
    if step == 1:
        answer = text.strip()
        if not answer:
            return "\u540d\u5b57\u4e0d\u80fd\u662f\u7a7a\u7684\u564a\uff01\u8acb\u8f38\u5165\u4f60\u7684\u540d\u5b57\u6216\u6232\u7a3c \U0001f60a"
        save_profile(line_user_id, display_name=answer, onboarding_step=2)
        return "\u5f88\u9ad8\u8208\u8a8d\u8b58\u4f60\uff01\U0001f64f\n\n" + _tone_question()

    # Step 2\uff1a\u8a9e\u6c23
    if step == 2:
        opt = TONE_OPTIONS.get(text.strip())
        if not opt:
            valid = '\u3001'.join(TONE_OPTIONS.keys())
            return f"\u8acb\u8f38\u5165 {valid} \u5176\u4e2d\u4e00\u500b\u6578\u5b57 \U0001f60a\n\n" + _tone_question()
        save_profile(line_user_id, coach_tone=opt['value'], onboarding_step=3)
        return _style_question()

    # Step 3\uff1a\u6eab\u901a\u65b9\u5f0f
    if step == 3:
        opt = STYLE_OPTIONS.get(text.strip())
        if not opt:
            valid = '\u3001'.join(STYLE_OPTIONS.keys())
            return f"\u8acb\u8f38\u5165 {valid} \u5176\u4e2d\u4e00\u500b\u6578\u5b57 \U0001f60a\n\n" + _style_question()
        save_profile(line_user_id, coach_style=opt['value'], onboarding_step=4)
        return _quote_question()

    # Step 4\uff1a\u5f15\u7528\u9891\u7387
    if step == 4:
        opt = QUOTE_OPTIONS.get(text.strip())
        if not opt:
            valid = '\u3001'.join(QUOTE_OPTIONS.keys())
            return f"\u8acb\u8f38\u5165 {valid} \u5176\u4e2d\u4e00\u500b\u6578\u5b57 \U0001f60a\n\n" + _quote_question()
        save_profile(line_user_id, quote_freq=opt['value'], onboarding_done=1, onboarding_step=5)

        p = get_profile(line_user_id)
        name = p['display_name'] or '\u4f60'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == p['coach_tone']), '')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == p['coach_style']), '')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == p['quote_freq']), '')

        return (
            f"\u592a\u68d2\u4e86\uff0c{name}\uff01\u2728 \u8a2d\u5b9a\u5b8c\u6210\uff01\n\n"
            f"\U0001f4cb \u4f60\u7684\u6559\u7df4\u98a8\u683c\uff1a\n"
            f"\u2022 \u8a9e\u6c23\uff1a{tone_label}\n"
            f"\u2022 \u6eab\u901a\u65b9\u5f0f\uff1a{style_label}\n"
            f"\u2022 \u5f15\u7528\u9891\u7387\uff1a{quote_label}\n\n"
            "\u5f9e\u73fe\u5728\u958b\u59cb\uff0c\u6211\u5c31\u662f\u4f60\u7684\u5c08\u5c6c\u6559\u7df4\u4e86 \U0001f4aa\n"
            "\u6709\u4ec0\u9ebc\u60f3\u8058\u7684\uff0c\u76f4\u63a5\u8aaa\u5427\uff01\n\n"
            "\uff08\u96a8\u6642\u53ef\u4ee5\u8f38\u5165 \u2699\ufe0f /setting \u91cd\u65b0\u8abf\u6574\u6559\u7df4\u98a8\u683c\uff09"
        )

    return None

# ============================
# Dify inputs \u7d44\u88dd
# ============================

def build_dify_inputs(profile):
    import datetime, zoneinfo
    tz = zoneinfo.ZoneInfo("Asia/Taipei")
    now = datetime.datetime.now(tz)
    current_time = now.strftime("%Y\u5e74%m\u6708%d\u65e5 %H:%M\uff08%A\uff09")

    tone_dify = next((v['dify'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '\u5e73\u8861\u7406\u6027')
    style_dify = next((v['dify'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '\u5faa\u5faa\u5584\u8a95\u3001\u5f15\u5c0e\u63a2\u7d22')
    quote_dify = next((v['dify'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '\u5076\u723e\u9069\u6642\u5f15\u7528\u5373\u53ef')
    return {
        "user_name": profile.get('display_name') or '\u7528\u6236',
        "coach_tone": tone_dify,
        "coach_style": style_dify,
        "quote_freq": quote_dify,
        "current_time": current_time,
    }

# ============================
# Dify API \u8a2a\u4f55\uff08\u542b\u5099\u63f4\uff09
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
        return answer if answer else "\U0001f914 \u6211\u60f3\u5230\u4e00\u534a\u5fd8\u8a18\u8aaa\u4ec0\u9ebc\u4e86\uff0c\u8acb\u518d\u554f\u6211\u4e00\u6b21\uff01"

    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[Dify Primary] \u9023\u7dda\u554f\u984c: {e} | user={user_id}")
        if DIFY_API_KEY_FALLBACK:
            try:
                print(f"[Dify Fallback] \u555f\u52d5\u5099\u63f4 | user={user_id}")
                result = call_dify(DIFY_API_KEY_FALLBACK, user_id, text, None, inputs)
                answer = result.get('answer', '').strip()
                if answer:
                    return "\u26a1\ufe0f \u6211\u6682\u6642\u5207\u63db\u5230\u5099\u7528\u7cfb\u7d71\u56de\u7b54\u4f60\uff1a\n\n" + answer
                return "\U0001f613 \u5099\u63f4\u7cfb\u7d71\u4e5f\u6c92\u56de\u61c9\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\uff01"
            except Exception as fe:
                print(f"[Dify Fallback] \u5931\u6557: {fe}")
        return (
            "\u2615 \u6211\u5269\u5015\u53bb\u6ce1\u4e86\u676f\u8336\u56de\u4f86\uff0c\u7d50\u679c\u5fd8\u8a18\u4f60\u554f\u4ec0\u9ebc\u4e86...\n\n"
            "\u8acb\u7a0d\u7b49\u4e00\u4e0b\u518d\u8a66\u8a66\u770b\uff01\u5982\u679c\u4e00\u76f4\u9019\u6a23\uff0c\u8acb\u8054\u7d61\u958b\u767c\u8005 Chris \u770b\u770b\u55b7 \U0001f64f"
        )

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else '\u672a\u77e5'
        error_msg = ''
        try:
            error_msg = e.response.json().get('message', '')
        except Exception:
            pass
        print(f"[Dify] HTTP \u932f\u8aa4 {status}: {error_msg} | user={user_id}")
        return (
            f"\U0001f527 \u6211\u9047\u5230\u4e86\u4e00\u9ede\u5c0f\u554f\u984c\uff08\u9519\u8aa4\u78bc\uff1a{status}\uff09\n\n"
            "\u5148\u53bb\u627e Chris \u4fee\u4e00\u4e0b\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66\uff01\u611f\u8b1d\u4f60\u7684\u8010\u5fc3 \U0001f4aa"
        )

    except Exception as e:
        print(f"[Dify] \u672a\u9810\u671f\u932f\u8aa4: {e} | user={user_id}")
        return (
            "\U0001f635 \u6211\u525a\u624d\u9748\u9b42\u51fa\u7ac5\u4e86\u4e00\u4e0b\uff0c\u8acb\u518d\u554f\u6211\u4e00\u6b21\uff01\n\n"
            "\u5982\u679c\u554f\u984c\u4e00\u76f4\u51fa\u73fe\uff0c\u9ebb\u7159\u8054\u7d61\u958b\u767c\u8005 Chris \u770b\u770b\uff0c\u8b1d\u8b1d\u4f60\u7684\u5305\u5bb9 \U0001f64f"
        )

# ============================
# \u6307\u4ee4\u8655\u7406
# ============================

HELP_TEXT = (
    "\U0001f916 \u6307\u4ee4\u8aaa\u660e\uff1a\n\n"
    "\U0001f504 /reset    \u2014 \u6e05\u9664\u5c0d\u8a71\u8a18\u61b6\uff0c\u91cd\u65b0\u958b\u59cb\n"
    "\u2699\ufe0f /setting  \u2014 \u91cd\u65b0\u8a2d\u5b9a\u6559\u7df4\u98a8\u683c\n"
    "\U0001f4cb /profile  \u2014 \u67e5\u770b\u76ee\u524d\u7684\u8a2d\u5b9a\n"
    "\u2753 /help     \u2014 \u986f\u793a\u9019\u500b\u8aaa\u660e\n\n"
    "\u76f4\u63a5\u8f38\u5165\u6587\u5b57\u5c31\u80fd\u548c\u6211\u5c0d\u8a71\uff01"
)

def handle_command(user_id, text, profile):
    cmd = text.strip().lower()

    if cmd == '/reset':
        reset_conversation(user_id)
        return "\U0001f504 \u5c0d\u8a71\u8a18\u61b6\u5df2\u6e05\u9664\uff01\n\n\u6211\u5011\u91cd\u65b0\u958b\u59cb\u5427\uff5e\u6709\u4ec0\u9ebc\u60f3\u8058\u7684\uff1f\U0001f60a"

    if cmd == '/help':
        return HELP_TEXT

    if cmd == '/setting':
        save_profile(user_id, onboarding_done=0, onboarding_step=2)
        return "\u2699\ufe0f \u597d\u7684\uff01\u6211\u5011\u4f86\u91cd\u65b0\u8abf\u6574\u4e00\u4e0b\uff5e\n\n" + _tone_question()

    if cmd == '/profile':
        name = profile.get('display_name') or '\u672a\u8a2d\u5b9a'
        tone_label = next((v['label'] for v in TONE_OPTIONS.values() if v['value'] == profile.get('coach_tone')), '\u672a\u8a2d\u5b9a')
        style_label = next((v['label'] for v in STYLE_OPTIONS.values() if v['value'] == profile.get('coach_style')), '\u672a\u8a2d\u5b9a')
        quote_label = next((v['label'] for v in QUOTE_OPTIONS.values() if v['value'] == profile.get('quote_freq')), '\u672a\u8a2d\u5b9a')
        return (
            f"\U0001f4cb \u4f60\u7684\u6559\u7df4\u8a2d\u5b9a\uff1a\n\n"
            f"\U0001f464 \u540d\u5b57\uff1a{name}\n"
            f"\U0001f3af \u8a9e\u6c23\uff1a{tone_label}\n"
            f"\U0001f4ac \u6eab\u901a\u65b9\u5f0f\uff1a{style_label}\n"
            f"\U0001f4da \u5f15\u7528\u9891\u7387\uff1a{quote_label}\n\n"
            "\u7528 /setting \u53ef\u4ee5\u91cd\u65b0\u8abf\u6574\uff5e"
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

    # 1. \u6307\u4ee4\u512a\u5148
    command_response = handle_command(user_id, user_text, profile)
    if command_response:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=command_response))
        return

    # 2. Onboarding \u554f\u5377\uff08\u65b0\u7528\u6236\uff09
    onboarding_response = handle_onboarding(user_id, user_text, profile)
    if onboarding_response is not None:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=onboarding_response))
        return

    # 3. \u6b63\u5e38 AI \u5c0d\u8a71
    replied_flag = threading.Event()

    def process_and_push():
        try:
            # 1. \u540c\u6b65\u7528\u6236\u8cc7\u6599\u5230 Base44
            sync_user_to_base44(user_id)
        except Exception as sync_err:
            print(f"[sync] \u5931\u6557: {sync_err}")

        # 2. \u9001 loading animation\uff08\u6700\u591a 60 \u79d2\uff09
        send_loading_animation(user_id, seconds=60)
        current_profile = get_profile(user_id)
        try:
            ai_response = ask_dify(user_id, user_text, current_profile)
        except Exception as e:
            print(f"[handle_message] \u672a\u9810\u671f\u932f\u8aa4: {e}")
            ai_response = "\U0001f635 \u51fa\u4e86\u9ede\u5c0f\u554f\u984c\uff0c\u8acb\u518d\u8a66\u4e00\u6b21\uff01"
        
        # 3. \u507d\u6a19\u4e26\u5132\u5b58\u76ee\u6a19/\u4e8b\u4ef6
        try:
            detect_and_save_goal_or_event(user_id, user_text, current_profile)
        except Exception as detect_err:
            print(f"[detect] \u5931\u6557: {detect_err}")
        
        if not replied_flag.is_set():
            replied_flag.set()
            line_bot_api.push_message(user_id, TextSendMessage(text=ai_response))

    threading.Thread(target=process_and_push, daemon=True).start()

# ============================
# \u5065\u5eb7\u6aa2\u67e5
# ============================

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
