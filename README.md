# LGAT AI 教練 — LINE Bot

> 一個基於 LINE + Dify 的 AI 深度覺知教練，整合薩提爾冰山探索、目標追蹤與主動提醒功能。

---

## 📌 專案目的

幫助用戶透過 LINE 對話進行自我覺察與成長，核心功能：

- **深度覺知教練**：根據用戶教練風格偏好（語氣、溝通方式、引用頻率）個人化回應
- **薩提爾冰山探索**：引導用戶從「行為→內心話→感受→想法→期望」逐層深入思考
- **目標與事件追蹤**：自動偵測對話中提到的目標、習慣、待辦、里程碑，記錄到後台
- **主動提醒**：在用戶設定的時間主動推送晨間問候、目標回顧、習慣提醒等

---

## 🏗️ 系統架構

```
LINE 用戶
   │
   ▼
LINE Messaging API（Webhook）
   │
   ▼
FastAPI (main.py) — Zeabur 部署
   ├── SQLite（/data/lgat.db）— 用戶檔案、對話 ID、薩提爾狀態
   ├── Dify 主教練 Chatflow — 一般教練對話
   ├── Dify 薩提爾 Chatflow — 冰山探索對話
   └── Base44 後台 API
         ├── syncUser        — 用戶資料同步
         ├── saveGoalOrEvent — 目標/事件儲存
         └── sendReminders   — 主動提醒推送
```

---

## 🚀 部署說明

### 1. 環境需求
- Python 3.10+
- Zeabur（或任何支援 FastAPI 的 PaaS）
- 持久化儲存（掛載 `/data` volume）

### 2. 必要環境變數（Zeabur 服務設定）

| 變數名稱 | 說明 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Bot Channel Access Token |
| `LINE_CHANNEL_SECRET` | LINE Bot Channel Secret |
| `DIFY_API_KEY` | 主教練 Dify Chatflow API Key |
| `DIFY_SATIR_API_KEY` | 薩提爾 Dify Chatflow API Key |
| `DIFY_API_URL` | Dify API 位址（預設 `https://api.dify.ai/v1`） |
| `DB_PATH` | SQLite 路徑（預設 `/data/lgat.db`） |

### 3. 選填環境變數

| 變數名稱 | 說明 |
|---|---|
| `DIFY_API_KEY_FALLBACK` | 備援 Dify API Key |
| `MAINTENANCE_MODE` | 維護模式（`true`/`false`） |
| `BASE44_SYNC_USER_URL` | syncUser function URL |
| `BASE44_SAVE_GOAL_URL` | saveGoalOrEvent function URL |
| `BASE44_SECRET_KEY` | Base44 API 驗證密鑰（`x-secret-key` header） |
| `BASE44_SAVE_GOAL_URL` | saveGoalOrEvent function URL |
| `COACH_TONE_OPTIONS` | 教練語氣選項（自訂格式，有預設值） |
| `COACH_STYLE_OPTIONS` | 溝通方式選項（自訂格式，有預設值） |
| `COACH_QUOTE_OPTIONS` | 引用頻率選項（自訂格式，有預設值） |

### 4. 安裝與啟動

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

### 5. LINE Webhook 設定
將 Webhook URL 設定為：`https://<你的域名>/callback`

---

## 📱 用戶指令

| 指令 | 說明 |
|---|---|
| `/help` | 顯示所有指令說明 |
| `/reset` | 清除對話記憶，重新開始 |
| `/setting` | 重新設定教練風格（語氣、溝通方式、引用頻率） |
| `/profile` | 查看目前的教練設定 |
| `/toggle` | **切換薩提爾 ↔ 一般教練模式**（推薦使用） |
| `/satir` | 直接進入薩提爾冰山探索模式 |
| `/exit` | 離開薩提爾，回到一般教練模式 |

---

## 🌊 薩提爾冰山探索模式

輸入 `/toggle` 或 `/satir` 進入，系統會引導你逐層探索：

```
第 1 層：行為   — 發生了什麼事？
第 2 層：內心話 — 你沒說出口的是什麼？
第 3 層：感受   — 你有什麼感覺？
第 4 層：想法   — 這個感受背後你是怎麼想的？
第 5 層：期望   — 你真正渴望的是什麼？
```

探索完成後，AI 會提供具體可操作的行動建議。

再次輸入 `/toggle` 或 `/exit` 切回一般教練模式。

---

## 🎯 自動目標偵測

系統會自動偵測對話中的關鍵詞並分類儲存到後台：

| 類型 | 關鍵詞範例 |
|---|---|
| 短期目標 | 希望、想要、打算、這週、本月 |
| 中期目標 | 三個月、半年、中期、季度 |
| 長期目標 | 一年、明年、長期、五年 |
| 習慣 | 習慣、每天、每週、打卡、堅持 |
| 待辦 | 要做、需要、今天、明天、任務 |
| 里程碑 | 達成、完成、通過、拿到、升職 |

---

## 🔔 主動提醒

透過 Base44 後台（`https://app-ffa38ee7.base44.app`）設定：

| 提醒類型 | 說明 |
|---|---|
| 晨間問候 | 每天早上問候，開啟當天計畫 |
| 目標回顧 | 提醒用戶追蹤目標進度 |
| 習慣打卡 | 固定時間提醒習慣執行 |
| 週報 | 每週一回顧，整理成果 |
| 自訂提醒 | 任意訊息、任意時間 |

---

## 🧩 Onboarding 流程

新用戶第一次使用時，會依序詢問：

1. **教練語氣**：嚴格督促 / 溫柔支持 / 平衡理性
2. **溝通方式**：直接說重點 / 循循善誘
3. **引用頻率**：多一點 / 偶爾 / 不需要

設定完成後進入正式教練對話。可隨時用 `/setting` 重新設定。

---

## 📦 版本歷史

| 版本 | 日期 | 說明 |
|---|---|---|
| v1.0 | 2026-04 | 初始版本：LINE Bot + Dify 串接，基本對話 |
| v1.1 | 2026-04 | SQLite 用戶檔案、Onboarding 問卷、教練風格設定 |
| v1.2 | 2026-04 | Base44 後台串接：syncUser / saveGoalOrEvent / sendReminders |
| v1.3 | 2026-04 | 薩提爾冰山探索模式：/toggle、/satir、/exit 指令 |
| v1.4 | 2026-04 | 維護模式、loading animation 優化、防重複回應 |

---

## 📁 檔案結構

```
lgat/
├── main.py                      # 主程式（FastAPI + LINE Bot）
├── README.md                    # 本說明文件
├── requirements.txt             # Python 依賴
├── dify_system_prompt.md        # 主教練 Dify System Prompt
├── satir_chatflow_system_prompt.md  # 薩提爾 Dify System Prompt
├── satir_dify_setup_guide.md    # 薩提爾 Dify 設定指南
├── rich_menu_guide.md           # LINE Rich Menu 設定指南
└── satir_coaching_guide.md      # 薩提爾教練方法說明
```

---

## 🔗 相關連結

- **管理後台**：https://app-ffa38ee7.base44.app
- **LINE Bot**：透過 LINE 加好友（QR Code 在 LINE Developers Console）
- **Dify**：https://dify.ai

---

## 👨‍💻 開發者

Chris — LGAT 深度覺知教練系統
