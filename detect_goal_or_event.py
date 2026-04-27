# 自動偵測用戶訊息中的目標/事件關鍵詞
# 用簡單的 regex + keyword matching 來識別

import re
from typing import Optional, Dict, Any

# 目標相關關鍵詞（短中長期）
GOAL_SHORT_KEYWORDS = ['想', '要', '決定', '計畫', '這週', '這個月', '30天', '7天']
GOAL_MEDIUM_KEYWORDS = ['3個月', '半年', '一季', '這季', '下季']
GOAL_LONG_KEYWORDS = ['一年', '年度', '長期目標', '夢想', '終極']

# 事件相關關鍵詞
EVENT_TODO_KEYWORDS = ['要做', '待辦', '明天', '後天', '下午', '晚上', '今天', '周末']
EVENT_HABIT_KEYWORDS = ['每天', '每週', '每月', '習慣', '打卡', '記錄', '堅持']
EVENT_MILESTONE_KEYWORDS = ['達成', '完成', '跨越', '突破', '里程碑']

# 完成度相關
COMPLETION_KEYWORDS = ['完成', '做完', '搞定', '成功', '達成', '達到', '達成了', '完成了']
PROGRESS_KEYWORDS = ['進度', '進展', '差點', '快要', '有點', '覺得']

def detect_goal_or_event(user_text: str) -> Optional[Dict[str, Any]]:
    """
    偵測訊息中是否包含目標或事件，回傳結構化資料
    
    Returns:
        {
            'type': 'goal' | 'event' | 'goal_progress' | None,
            'title': str,
            'description': str,
            'goal_type': 'short' | 'medium' | 'long',
            'event_type': 'todo' | 'habit' | 'milestone',
            'target_date': str (YYYY-MM-DD) or None,
            'status': 'active' | 'completed',
        } or None
    """
    
    text = user_text.strip()
    if not text:
        return None
    
    # 1. 檢查是否是進度更新（完成度回報）
    if any(kw in text for kw in COMPLETION_KEYWORDS):
        # "我完成了跑步" / "讀書完成了" / "今天達成了運動目標"
        match = re.search(r'(完成|做完|搞定|成功|達成|達到)了?(.+?)($|，|。|!)', text)
        if match:
            thing = match.group(2).strip()
            return {
                'type': 'goal_progress',
                'title': thing,
                'status': 'completed',
                'progress_note': text,
            }
    
    # 2. 檢查是否是新目標（"我想..." / "我要..." / "決定...")
    if any(kw in text for kw in ['想', '要', '決定', '計畫', '目標是']):
        goal_match = re.search(r'(想|要|決定|計畫|目標是)(.+?)($|，|。|!)', text)
        if goal_match:
            goal_text = goal_match.group(2).strip()
            
            # 判斷目標期限
            goal_type = 'short'  # 預設短期
            if any(kw in text for kw in GOAL_LONG_KEYWORDS):
                goal_type = 'long'
            elif any(kw in text for kw in GOAL_MEDIUM_KEYWORDS):
                goal_type = 'medium'
            
            # 嘗試從訊息裡萃取日期（簡單版）
            target_date = extract_date_from_text(text)
            
            return {
                'type': 'goal',
                'title': goal_text,
                'description': text,
                'goal_type': goal_type,
                'target_date': target_date,
            }
    
    # 3. 檢查是否是待辦/習慣/里程碑事件
    if any(kw in text for kw in EVENT_TODO_KEYWORDS):
        event_match = re.search(r'(要做|待辦|明天|後天|今天|周末)(.+?)($|，|。|!)', text)
        if event_match:
            event_text = event_match.group(2).strip()
            return {
                'type': 'event',
                'title': event_text,
                'event_type': 'todo',
                'due_date': extract_date_from_text(text),
            }
    
    if any(kw in text for kw in EVENT_HABIT_KEYWORDS):
        habit_match = re.search(r'(每天|每週|每月|習慣)(.+?)($|，|。|!)', text)
        if habit_match:
            habit_text = habit_match.group(2).strip()
            recurrence = 'daily'
            if '每週' in text:
                recurrence = 'weekly'
            elif '每月' in text:
                recurrence = 'monthly'
            
            return {
                'type': 'event',
                'title': habit_text,
                'event_type': 'habit',
                'recurrence': recurrence,
            }
    
    if any(kw in text for kw in EVENT_MILESTONE_KEYWORDS):
        milestone_match = re.search(r'(達成|完成|跨越|突破)(.+?)($|，|。|!)', text)
        if milestone_match:
            milestone_text = milestone_match.group(2).strip()
            return {
                'type': 'event',
                'title': milestone_text,
                'event_type': 'milestone',
            }
    
    return None


def extract_date_from_text(text: str) -> Optional[str]:
    """
    簡單的日期萃取：支援
    - 明天 / 後天 / 今天
    - 下週一 / 下個月
    - 2026-05-01 / 2026/5/1
    """
    from datetime import datetime, timedelta
    
    today = datetime.now()
    
    # 相對日期
    if '明天' in text:
        return (today + timedelta(days=1)).strftime('%Y-%m-%d')
    if '後天' in text:
        return (today + timedelta(days=2)).strftime('%Y-%m-%d')
    if '下週' in text or '下周' in text:
        return (today + timedelta(days=7)).strftime('%Y-%m-%d')
    if '下個月' in text or '下月' in text:
        return (today + timedelta(days=30)).strftime('%Y-%m-%d')
    
    # 絕對日期（YYYY-MM-DD 或 YYYY/M/D）
    iso_match = re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', text)
    if iso_match:
        return f"{iso_match.group(1)}-{iso_match.group(2).zfill(2)}-{iso_match.group(3).zfill(2)}"
    
    slash_match = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})', text)
    if slash_match:
        return f"{slash_match.group(1)}-{slash_match.group(2).zfill(2)}-{slash_match.group(3).zfill(2)}"
    
    return None
