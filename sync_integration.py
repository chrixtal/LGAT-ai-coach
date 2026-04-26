"""
與 Base44 後台的資料同步整合
"""
import os
import requests
import json
from datetime import datetime

BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_API_URL = f'https://app-ffa38ee7.base44.app/functions'

def sync_user_to_base44(line_user_id, display_name, coach_tone, coach_style, quote_freq, total_messages, reminder_enabled, reminder_time):
    """同步用戶資料到 Base44"""
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
                'reminder_enabled': reminder_enabled,
                'reminder_time': reminder_time,
            },
            timeout=10
        )
        if resp.status_code == 200:
            print(f"[Base44] 用戶資料同步成功: {line_user_id}")
            return True
        else:
            print(f"[Base44] syncUser 失敗: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"[Base44] syncUser 例外: {e}")
        return False

def save_goal_to_base44(line_user_id, display_name, title, description='', goal_type='short', target_date=''):
    """儲存目標到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_API_URL}/saveGoalOrEvent',
            json={
                'entity_type': 'goal',
                'line_user_id': line_user_id,
                'display_name': display_name,
                'title': title,
                'description': description,
                'type': goal_type,
                'target_date': target_date,
            },
            timeout=10
        )
        if resp.status_code == 200:
            print(f"[Base44] 目標儲存成功: {title}")
            return True
        else:
            print(f"[Base44] saveGoal 失敗: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"[Base44] saveGoal 例外: {e}")
        return False

def save_event_to_base44(line_user_id, display_name, title, event_type='todo', due_date='', recurrence='none', note=''):
    """儲存事件到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_API_URL}/saveGoalOrEvent',
            json={
                'entity_type': 'event',
                'line_user_id': line_user_id,
                'display_name': display_name,
                'title': title,
                'type': event_type,
                'due_date': due_date,
                'recurrence': recurrence,
                'note': note,
            },
            timeout=10
        )
        if resp.status_code == 200:
            print(f"[Base44] 事件儲存成功: {title}")
            return True
        else:
            print(f"[Base44] saveEvent 失敗: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"[Base44] saveEvent 例外: {e}")
        return False

def detect_goal_or_event(text):
    """
    偵測用戶輸入中的目標或事件關鍵字
    回傳 (type, entity_type, title, info_dict) 或 None
    
    type: 'goal' 或 'event'
    entity_type: 'short', 'medium', 'long' (for goal) 或 'habit', 'todo', 'milestone' (for event)
    """
    text_lower = text.lower()
    
    # 目標關鍵字（簡易版，可以擴充）
    goal_keywords = {
        'short': ['今天', '明天', '這週', '下週', '一個月', '1個月'],
        'medium': ['三個月', '6個月', '半年', '一季', '一年內'],
        'long': ['明年', '兩年', '三年', '長期'],
    }
    
    # 檢查是否是目標
    for goal_type, keywords in goal_keywords.items():
        for kw in keywords:
            if kw in text:
                # 簡單提取：句子開頭到「目標」或「想」為止
                if '目標' in text or '想要' in text or '想' in text:
                    start_idx = text.find(kw)
                    title = text[max(0, start_idx-10):start_idx+20].strip()
                    return ('goal', goal_type, title, {})
    
    # 事件關鍵字
    event_keywords = {
        'habit': ['每天', '每週', '習慣', '打卡'],
        'todo': ['要', '需要', '記得', '待辦', '做', '完成'],
        'milestone': ['達成', '突破', '里程碑', '完成'],
    }
    
    for event_type, keywords in event_keywords.items():
        for kw in keywords:
            if kw in text:
                return ('event', event_type, text[:50], {})
    
    return None

