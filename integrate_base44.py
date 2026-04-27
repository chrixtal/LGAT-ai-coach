import os
import requests
import json

BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_SERVICE_TOKEN = os.environ.get('BASE44_SERVICE_TOKEN', '')

# Backend functions URL
def build_function_url(func_name):
    return f"https://app-ffa38ee7.base44.app/functions/{func_name}"

def sync_user(line_user_id, display_name, coach_tone='', coach_style='', quote_freq='', total_messages=0):
    """同步 LINE 用戶資料到 Base44"""
    try:
        url = build_function_url('syncUser')
        payload = {
            'line_user_id': line_user_id,
            'display_name': display_name,
            'coach_tone': coach_tone,
            'coach_style': coach_style,
            'quote_freq': quote_freq,
            'total_messages': total_messages,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[syncUser] ✅ {line_user_id} synced")
            return resp.json()
        else:
            print(f"[syncUser] ❌ {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        print(f"[syncUser] 錯誤: {e}")
        return None

def save_goal_or_event(entity_type, line_user_id, display_name, **fields):
    """儲存目標或事件到 Base44
    
    entity_type: 'goal' | 'event' | 'goal_progress'
    fields: 
      goal: title, description, type ('short'/'medium'/'long'), target_date
      event: title, type ('habit'/'todo'/'milestone'/'reminder'), due_date, recurrence ('none'/'daily'/'weekly'/'monthly'), note
      goal_progress: progress_note, status ('completed'/'paused')
    """
    try:
        url = build_function_url('saveGoalOrEvent')
        payload = {
            'entity_type': entity_type,
            'line_user_id': line_user_id,
            'display_name': display_name,
            **fields
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[saveGoalOrEvent] ✅ {entity_type} saved for {line_user_id}")
            return resp.json()
        else:
            print(f"[saveGoalOrEvent] ❌ {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        print(f"[saveGoalOrEvent] 錯誤: {e}")
        return None

def send_reminders():
    """呼叫 sendReminders function（通常由排程自動呼叫）"""
    try:
        url = build_function_url('sendReminders')
        resp = requests.post(url, json={}, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            print(f"[sendReminders] ✅ sent={result.get('sent_count')}, time={result.get('time_checked')}")
            return result
        else:
            print(f"[sendReminders] ❌ {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        print(f"[sendReminders] 錯誤: {e}")
        return None
