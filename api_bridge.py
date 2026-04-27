"""
Bridge 用來和 Base44 backend functions 通訊
"""
import os
import json
import requests
from datetime import datetime

BASE44_APP_URL = os.environ.get('BASE44_APP_URL', 'https://app-ffa38ee7.base44.app')
BASE44_API_TOKEN = os.environ.get('BASE44_API_TOKEN', '')

def sync_user(line_user_id, display_name, coach_tone='', coach_style='', quote_freq='', total_messages=0, reminder_enabled=False, reminder_time='08:00', plan='free'):
    """同步用戶資料到 Base44"""
    try:
        url = f'{BASE44_APP_URL}/functions/syncUser'
        headers = {
            'Content-Type': 'application/json',
        }
        if BASE44_API_TOKEN:
            headers['Authorization'] = f'Bearer {BASE44_API_TOKEN}'
        
        data = {
            'line_user_id': line_user_id,
            'display_name': display_name,
            'coach_tone': coach_tone,
            'coach_style': coach_style,
            'quote_freq': quote_freq,
            'total_messages': total_messages,
            'reminder_enabled': reminder_enabled,
            'reminder_time': reminder_time,
            'plan': plan,
        }
        resp = requests.post(url, json=data, headers=headers, timeout=5)
        if resp.status_code == 200:
            result = resp.json()
            print(f"[syncUser] 成功 | user={line_user_id}")
            return result
        else:
            print(f"[syncUser] 失敗 status={resp.status_code} | {resp.text}")
            return None
    except Exception as e:
        print(f"[syncUser] 例外: {e}")
        return None

def save_goal_or_event(entity_type, line_user_id, display_name='', **fields):
    """儲存目標或事件到 Base44"""
    try:
        url = f'{BASE44_APP_URL}/functions/saveGoalOrEvent'
        headers = {
            'Content-Type': 'application/json',
        }
        if BASE44_API_TOKEN:
            headers['Authorization'] = f'Bearer {BASE44_API_TOKEN}'
        
        data = {
            'entity_type': entity_type,  # 'goal' / 'event' / 'goal_progress'
            'line_user_id': line_user_id,
            'display_name': display_name,
            **fields
        }
        resp = requests.post(url, json=data, headers=headers, timeout=5)
        if resp.status_code == 200:
            result = resp.json()
            print(f"[saveGoalOrEvent] 成功 | entity_type={entity_type} user={line_user_id}")
            return result
        else:
            print(f"[saveGoalOrEvent] 失敗 status={resp.status_code} | {resp.text}")
            return None
    except Exception as e:
        print(f"[saveGoalOrEvent] 例外: {e}")
        return None

def detect_goal_or_event(text, profile):
    """
    簡單的關鍵詞偵測，判斷使用者是在設定目標還是事件
    回傳 (entity_type, fields) 或 None
    """
    text_lower = text.lower()
    
    # 目標關鍵詞
    goal_keywords = ['目標', '想要', '計畫', '想達成', '目標是', '我要', '我想', '希望', '夢想']
    event_keywords = ['完成', '做完', '打卡', '習慣', '每天', '待辦', '清單', '任務', '要做']
    
    has_goal = any(kw in text_lower for kw in goal_keywords)
    has_event = any(kw in text_lower for kw in event_keywords)
    
    if has_goal:
        return ('goal', {'title': text, 'type': 'short'})
    elif has_event:
        return ('event', {'title': text, 'type': 'todo'})
    
    return None
