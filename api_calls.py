import os
import requests

BASE44_APP_ID = os.environ.get('BASE44_APP_ID', '69e35caa4e5d9a67dd7dd6e1')
BASE44_API_BASE = f'https://app-ffa38ee7.base44.app/functions'

def sync_user_to_base44(line_user_id, display_name, coach_tone, coach_style, quote_freq, total_messages=0, plan='free'):
    """同步用戶資料到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_API_BASE}/syncUser',
            json={
                'line_user_id': line_user_id,
                'display_name': display_name,
                'coach_tone': coach_tone,
                'coach_style': coach_style,
                'quote_freq': quote_freq,
                'total_messages': total_messages,
                'plan': plan,
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Base44] 用戶 {display_name} 已同步")
        else:
            print(f"[Base44] syncUser 失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44] syncUser exception: {e}")

def save_goal_or_event(entity_type, line_user_id, display_name, **fields):
    """儲存目標或事件到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_API_BASE}/saveGoalOrEvent',
            json={
                'entity_type': entity_type,
                'line_user_id': line_user_id,
                'display_name': display_name,
                **fields
            },
            timeout=5
        )
        if resp.status_code == 200:
            print(f"[Base44] {entity_type} 已儲存")
        else:
            print(f"[Base44] saveGoalOrEvent 失敗: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[Base44] saveGoalOrEvent exception: {e}")
