"""
Base44 同步模組
負責把 LINE bot 的用戶、目標、事件資料同步到 Base44 資料庫
"""
import os
import requests
import json
from datetime import datetime

BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_FUNCTION_URL = f'https://app-ffa38ee7.base44.app/functions'

def sync_user(line_user_id, display_name=None, coach_tone=None, coach_style=None, 
              quote_freq=None, total_messages=None, reminder_enabled=None, 
              reminder_time=None, plan=None):
    """同步用戶資料到 Base44"""
    try:
        payload = {
            'line_user_id': line_user_id,
        }
        if display_name is not None:
            payload['display_name'] = display_name
        if coach_tone is not None:
            payload['coach_tone'] = coach_tone
        if coach_style is not None:
            payload['coach_style'] = coach_style
        if quote_freq is not None:
            payload['quote_freq'] = quote_freq
        if total_messages is not None:
            payload['total_messages'] = total_messages
        if reminder_enabled is not None:
            payload['reminder_enabled'] = reminder_enabled
        if reminder_time is not None:
            payload['reminder_time'] = reminder_time
        if plan is not None:
            payload['plan'] = plan

        resp = requests.post(
            f'{BASE44_FUNCTION_URL}/syncUser',
            json=payload,
            timeout=10
        )
        result = resp.json()
        print(f"[Base44] syncUser {line_user_id}: {result.get('ok', False)}")
        return result
    except Exception as e:
        print(f"[Base44] syncUser 失敗: {e}")
        return {'ok': False, 'error': str(e)}

def save_goal(line_user_id, display_name, title, description='', goal_type='short', target_date=''):
    """儲存目標"""
    try:
        payload = {
            'entity_type': 'goal',
            'line_user_id': line_user_id,
            'display_name': display_name,
            'title': title,
            'description': description,
            'type': goal_type,  # short / medium / long
            'target_date': target_date,
        }
        resp = requests.post(
            f'{BASE44_FUNCTION_URL}/saveGoalOrEvent',
            json=payload,
            timeout=10
        )
        result = resp.json()
        print(f"[Base44] saveGoal {title}: {result.get('ok', False)}")
        return result
    except Exception as e:
        print(f"[Base44] saveGoal 失敗: {e}")
        return {'ok': False, 'error': str(e)}

def save_event(line_user_id, display_name, title, event_type='todo', due_date='', recurrence='none', note=''):
    """儲存事件（習慣、待辦、里程碑）"""
    try:
        payload = {
            'entity_type': 'event',
            'line_user_id': line_user_id,
            'display_name': display_name,
            'title': title,
            'type': event_type,  # habit / todo / milestone / reminder
            'due_date': due_date,
            'recurrence': recurrence,  # none / daily / weekly / monthly
            'note': note,
        }
        resp = requests.post(
            f'{BASE44_FUNCTION_URL}/saveGoalOrEvent',
            json=payload,
            timeout=10
        )
        result = resp.json()
        print(f"[Base44] saveEvent {title}: {result.get('ok', False)}")
        return result
    except Exception as e:
        print(f"[Base44] saveEvent 失敗: {e}")
        return {'ok': False, 'error': str(e)}

def update_goal_progress(line_user_id, title, progress_note='', status=None):
    """更新目標進度"""
    try:
        payload = {
            'entity_type': 'goal_progress',
            'line_user_id': line_user_id,
            'title': title,
            'progress_note': progress_note,
        }
        if status:
            payload['status'] = status  # completed / paused / abandoned
        
        resp = requests.post(
            f'{BASE44_FUNCTION_URL}/saveGoalOrEvent',
            json=payload,
            timeout=10
        )
        result = resp.json()
        print(f"[Base44] updateGoalProgress {title}: {result.get('ok', False)}")
        return result
    except Exception as e:
        print(f"[Base44] updateGoalProgress 失敗: {e}")
        return {'ok': False, 'error': str(e)}

def detect_and_save_goal_or_event(line_user_id, display_name, user_message):
    """
    簡單的關鍵詞偵測，判斷訊息是否涉及目標設定或事件
    目標關鍵詞：想要、目標、計畫、學習、完成、達成、挑戰
    事件關鍵詞：習慣、待辦、做完、打卡、記錄、提醒
    """
    text = user_message.lower()
    
    # 目標關鍵詞
    goal_keywords = ['想要', '目標', '計畫', '學習', '完成', '達成', '挑戰', '想要學']
    # 事件關鍵詞
    event_keywords = ['習慣', '待辦', '打卡', '記錄', '每天', '每週', '做完', '完成了']
    
    is_goal = any(kw in text for kw in goal_keywords)
    is_event = any(kw in text for kw in event_keywords)
    
    if is_goal:
        # 簡化邏輯：取前 50 個字作為目標標題
        title = user_message[:50]
        save_goal(line_user_id, display_name, title)
        return f"✨ 好的！我已經把「{title}」記下來了。\n\n我會陪你一起朝著這個目標前進 💪"
    
    elif is_event:
        title = user_message[:50]
        save_event(line_user_id, display_name, title, event_type='todo')
        return f"✅ 已記錄：{title}\n\n加油！完成後跟我說一聲 🎉"
    
    return None  # 沒有偵測到目標或事件
