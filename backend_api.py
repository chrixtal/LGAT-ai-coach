"""
Base44 Backend API Client
用於 Python bot 調用 Base44 的 backend functions
"""
import os
import requests
import json
from datetime import datetime

BASE44_APP_URL = os.environ.get('BASE44_APP_URL', 'https://app-ffa38ee7.base44.app')
FUNCTION_TIMEOUT = 10

def sync_user(line_user_id, display_name='', coach_tone='', coach_style='', quote_freq='', total_messages=0):
    """同步用戶資料到 Base44"""
    try:
        resp = requests.post(
            f'{BASE44_APP_URL}/functions/syncUser',
            json={
                'line_user_id': line_user_id,
                'display_name': display_name,
                'coach_tone': coach_tone,
                'coach_style': coach_style,
                'quote_freq': quote_freq,
                'total_messages': int(total_messages),
            },
            timeout=FUNCTION_TIMEOUT
        )
        if resp.status_code == 200:
            print(f"[syncUser] ✅ {line_user_id} -> {display_name}")
            return resp.json()
        else:
            print(f"[syncUser] ❌ status={resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        print(f"[syncUser] ❌ {e}")
        return None

def save_goal_or_event(entity_type, line_user_id, display_name, **fields):
    """儲存目標或事件到 Base44
    
    entity_type: 'goal' | 'event' | 'goal_progress'
    fields: 
      - goal: title, description, type ('short'/'medium'/'long'), target_date
      - event: title, type ('habit'/'todo'/'milestone'/'reminder'), due_date, recurrence, note
      - goal_progress: progress_note, status ('completed'/'active')
    """
    try:
        resp = requests.post(
            f'{BASE44_APP_URL}/functions/saveGoalOrEvent',
            json={
                'entity_type': entity_type,
                'line_user_id': line_user_id,
                'display_name': display_name,
                **fields
            },
            timeout=FUNCTION_TIMEOUT
        )
        if resp.status_code == 200:
            print(f"[saveGoalOrEvent] ✅ {entity_type} for {line_user_id}")
            return resp.json()
        else:
            print(f"[saveGoalOrEvent] ❌ status={resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        print(f"[saveGoalOrEvent] ❌ {e}")
        return None

def detect_and_save_goal_or_event(line_user_id, display_name, text):
    """從文字中偵測關鍵詞，自動儲存目標或事件"""
    text_lower = text.lower()
    
    # 目標關鍵詞
    goal_keywords = {
        '目標': 'goal', '想': 'goal', '要': 'goal', '計畫': 'goal',
        '完成': 'goal', '達成': 'goal', '設定': 'goal',
    }
    
    # 事件關鍵詞
    event_keywords = {
        '待辦': 'todo', '做': 'todo', '記得': 'todo',
        '習慣': 'habit', '每天': 'habit', '每週': 'habit',
        '里程碑': 'milestone', '成就': 'milestone',
    }
    
    # 簡單啟發式：若包含特定詞彙，就儲存
    for keyword, gtype in goal_keywords.items():
        if keyword in text_lower:
            # 先偵測期限（短/中/長期）
            duration = 'short'
            if any(w in text for w in ['中期', '3-6個月', '季度']):
                duration = 'medium'
            elif any(w in text for w in ['長期', '年', '一年']):
                duration = 'long'
            
            save_goal_or_event('goal', line_user_id, display_name,
                              title=text[:50],  # 取前50字作為標題
                              description=text,
                              type=duration)
            break
    
    for keyword, etype in event_keywords.items():
        if keyword in text_lower:
            # 偵測週期
            recurrence = 'none'
            if '每天' in text or '每日' in text:
                recurrence = 'daily'
            elif '每週' in text or '周' in text:
                recurrence = 'weekly'
            elif '每月' in text or '月' in text:
                recurrence = 'monthly'
            
            save_goal_or_event('event', line_user_id, display_name,
                              title=text[:50],
                              type=etype,
                              recurrence=recurrence)
            break

