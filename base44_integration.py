"""
Base44 API 整合模組
負責呼叫 Base44 backend functions，同步用戶和儲存目標/事件
"""

import os
import requests
import re
import json
from typing import Optional, Dict

BASE44_API_URL = os.environ.get('BASE44_API_URL', 'https://app-ffa38ee7.base44.app')

def sync_user_to_base44(
    line_user_id: str,
    display_name: str = '',
    coach_tone: str = '',
    coach_style: str = '',
    quote_freq: str = '',
    total_messages: int = 0,
    reminder_enabled: bool = False,
    reminder_time: str = '08:00',
) -> bool:
    """
    同步用戶資料到 Base44
    在每次對話時呼叫，確保用戶資料最新
    """
    try:
        url = f'{BASE44_API_URL}/functions/syncUser'
        payload = {
            'line_user_id': line_user_id,
            'display_name': display_name,
            'coach_tone': coach_tone,
            'coach_style': coach_style,
            'quote_freq': quote_freq,
            'total_messages': total_messages,
            'reminder_enabled': reminder_enabled,
            'reminder_time': reminder_time,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[Base44] syncUser 成功 | user={line_user_id}")
            return True
        else:
            print(f"[Base44] syncUser 失敗: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"[Base44] syncUser 例外: {e}")
        return False


def save_goal_to_base44(
    line_user_id: str,
    display_name: str,
    title: str,
    description: str = '',
    goal_type: str = 'short',  # short / medium / long
    target_date: str = '',
) -> bool:
    """
    儲存目標到 Base44
    """
    try:
        url = f'{BASE44_API_URL}/functions/saveGoalOrEvent'
        payload = {
            'entity_type': 'goal',
            'line_user_id': line_user_id,
            'display_name': display_name,
            'title': title,
            'description': description,
            'type': goal_type,
            'target_date': target_date,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[Base44] 儲存目標: {title}")
            return True
        else:
            print(f"[Base44] 儲存目標失敗: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"[Base44] 儲存目標例外: {e}")
        return False


def save_event_to_base44(
    line_user_id: str,
    display_name: str,
    title: str,
    event_type: str = 'todo',  # habit / todo / milestone / reminder
    due_date: str = '',
    recurrence: str = 'none',
    note: str = '',
) -> bool:
    """
    儲存事件（待辦/習慣/里程碑）到 Base44
    """
    try:
        url = f'{BASE44_API_URL}/functions/saveGoalOrEvent'
        payload = {
            'entity_type': 'event',
            'line_user_id': line_user_id,
            'display_name': display_name,
            'title': title,
            'type': event_type,
            'due_date': due_date,
            'recurrence': recurrence,
            'note': note,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"[Base44] 儲存事件: {title}")
            return True
        else:
            print(f"[Base44] 儲存事件失敗: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"[Base44] 儲存事件例外: {e}")
        return False


# ============================
# 關鍵詞偵測 & 自動儲存
# ============================

GOAL_KEYWORDS = ['目標', '想要', '計畫', '想做', '我的目標', '設定', '達成', 'goal']
EVENT_KEYWORDS = ['習慣', '待辦', '提醒', '里程碑', '記得', 'todo', 'habit', 'milestone']

def detect_and_save_goal_or_event(
    line_user_id: str,
    display_name: str,
    user_text: str,
) -> None:
    """
    偵測用戶訊息中的目標/事件關鍵詞，自動儲存
    """
    text_lower = user_text.lower()
    
    # 偵測目標
    for keyword in GOAL_KEYWORDS:
        if keyword in text_lower:
            # 簡單抽取：用戶輸入「我想要減肥」→ title = 減肥
            # 更好的方式：用 AI 結構化，但這裡先簡化
            match = re.search(r'(?:想要|計畫|想做|設定|達成目標|目標)(.{2,20}?)(?:。|！|，|$)', user_text)
            if match:
                title = match.group(1).strip()
                save_goal_to_base44(
                    line_user_id=line_user_id,
                    display_name=display_name,
                    title=title,
                    goal_type='short',  # 預設短期
                )
                return

    # 偵測事件
    for keyword in EVENT_KEYWORDS:
        if keyword in text_lower:
            match = re.search(r'(?:習慣|待辦|提醒)(.{2,20}?)(?:。|！|，|$)', user_text)
            if match:
                title = match.group(1).strip()
                event_type = 'habit' if '習慣' in user_text else 'todo'
                save_event_to_base44(
                    line_user_id=line_user_id,
                    display_name=display_name,
                    title=title,
                    event_type=event_type,
                )
                return

