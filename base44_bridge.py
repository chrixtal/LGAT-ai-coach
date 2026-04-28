import requests
import os
from datetime import datetime

BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app/functions')

def sync_user_to_base44(line_user_id, display_name='', coach_tone='', coach_style='', quote_freq='', total_messages=0):
    """同步用戶資料到 Base44
    
    Args:
        line_user_id: LINE user ID
        display_name: 用戶暱稱
        coach_tone: 教練語氣
        coach_style: 溝通方式
        quote_freq: 引用頻率
        total_messages: 對話次數
    """
    try:
        resp = requests.post(
            f'{BASE44_API_URL}/syncUser',
            json={
                'line_user_id': line_user_id,
                'display_name': display_name,
                'coach_tone': coach_tone,
                'coach_style': coach_style,
                'quote_freq': quote_freq,
                'total_messages': total_messages,
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Base44] syncUser OK | {line_user_id}")
            return resp.json()
        else:
            print(f"[Base44] syncUser failed: {resp.status_code} {resp.text}")
            return None
    except Exception as e:
        print(f"[Base44] syncUser error: {e}")
        return None

def save_goal_or_event(entity_type, line_user_id, display_name='', **fields):
    """儲存目標或事件到 Base44
    
    Args:
        entity_type: 'goal' | 'event' | 'goal_progress'
        line_user_id: LINE user ID
        display_name: 用戶暱稱
        **fields: title, description, type, target_date, status, recurrence, etc.
    """
    try:
        resp = requests.post(
            f'{BASE44_API_URL}/saveGoalOrEvent',
            json={
                'entity_type': entity_type,
                'line_user_id': line_user_id,
                'display_name': display_name,
                **fields
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Base44] save {entity_type} OK | {line_user_id}")
            return resp.json()
        else:
            print(f"[Base44] save {entity_type} failed: {resp.status_code} {resp.text}")
            return None
    except Exception as e:
        print(f"[Base44] save {entity_type} error: {e}")
        return None

def detect_goal_or_event(text):
    """偵測用戶是否在設定目標或事件
    
    返回 (entity_type, fields) 或 (None, None)
    
    目標關鍵詞：目標、想要、計畫、打算、目的是、希望、夢想、決定要
    事件關鍵詞：完成、做了、今天、這週、明天、下週、要做、待辦、習慣
    """
    text_lower = text.lower()
    
    # 目標關鍵詞（優先度高）
    goal_keywords = ['目標', '想要', '計畫', '打算', '目的是', '希望', '夢想', '決定要']
    for keyword in goal_keywords:
        if keyword in text_lower:
            return ('goal', {'title': text[:30], 'description': text, 'type': 'short'})
    
    # 事件關鍵詞
    event_keywords = ['完成', '做了', '今天', '這週', '明天', '下週', '要做', '待辦', '習慣']
    for keyword in event_keywords:
        if keyword in text_lower:
            return ('event', {'title': text[:30], 'type': 'todo'})
    
    return (None, None)
