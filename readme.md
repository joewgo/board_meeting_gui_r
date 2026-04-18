# 🎤 露娜的 AI 董事會控制台 (AI Board Meeting Console) v2.8.9

這是一個以 Python + Tkinter 製作的桌面 GUI 工具，整合 GitHub Copilot SDK 與 LM Studio 本地模型，讓多個 AI 模型依照指定流程共同分析同一個議題，最後由裁判長輸出 Markdown 決策報告。`board_meeting_gui_v2.8.9.py` 提供單模型、接力、討論共識三種模式，並支援 LM Studio 本地模型、圖片輔助推論、提示詞模板、即時串流日誌與設定持久化。

## ✨ 核心功能

* **三種會議模式**
  * **單模型（只用裁判長）**：直接由裁判長模型回答使用者需求。
  * **接力模式（A→B→裁判長）**：專家 A 與專家 B 先後作答，再交由裁判長統整。
  * **討論共識（三專家→裁判長）**：三位專家先獨立作答，再進行多輪共識確認，最後由裁判長仲裁。
* **LM Studio 本地模型支援**：
  * 優先透過 `lmstudio` 官方 Python SDK 連線（不需要在 LM Studio 開啟「本地伺服器」）。
  * SDK 不可用或連線失敗時，自動退回 HTTP REST API（`aiohttp`）備援，依序嘗試 OpenAI 相容格式 `/chat/completions` 與 LM Studio 原生 `/chat` 端點。
  * 自動掃描本機常見路徑下的 `.gguf` 模型檔並加入選單（自動排除 `mmproj` 多模態投影檔）。
  * 掃描路徑涵蓋 `ai_models.json` 設定的路徑、當前使用者 `~/.lmstudio/models`，以及 Windows 上多個 AppData / Program Files 安裝路徑。
  * GUI 提供 LM Studio URL 輸入欄、**🔄 重新整理本地模型** 與 **🔌 測試連線** 按鈕。
  * 特殊選項「**[本地] 目前 LM Studio 載入的模型**」：直接使用 LM Studio 當前已載入的模型，無需指定路徑。
  * **LM Studio URL 自動正規化**：輸入任意格式的 URL（含 `localhost`、`127.0.0.1`、含 port 或不含 port），程式會自動補全並同時嘗試多個候選位址，提升連線成功率。
* **Windows asyncio 相容性修正**：在 Windows 上自動切換為 `SelectorEventLoop`，避免 `aiohttp` 在 `ProactorEventLoop` 下連線 localhost 不穩定的問題。Python 3.14+ 的 DeprecationWarning 已靜音處理，Python 3.16+ 移除後自動跳過。
* **模型清單來自 `ai_models.json`**：GitHub Copilot 雲端模型清單與 LM Studio 路徑設定皆可於 `ai_models.json` 調整，無需修改程式碼。
* **多模型陣容切換**：可為專家 A、B、C 與裁判長分別指定雲端或本地模型。
* **圖片與純文字雙模式**：可選擇整個資料夾掃描圖片，或直接多選圖片檔；若不提供圖片，也能以純文字模式執行。
* **提示詞模板系統**：會自動掃描 `GEM_提示詞整合` 目錄下的 `.md` / `.txt` 檔，並可一鍵套用到各角色的 system prompt。
* **即時串流日誌**：AI 回應會持續寫入 GUI 日誌區，便於觀察每個角色的推論過程。
* **可中途停止會議**：執行中可按下「⛔ 停止」，系統會在當前步驟完成後安全停止。
* **設定自動保存**：目前選擇的模式、模型、LM Studio URL、圖片來源、模板、提示詞與主題，都會寫入 `board_meeting_config.json`。
* **時間戳記報告輸出**：每次會議完成後自動產生 `AI_Board_Report_YYYYMMDD_HHMMSS.md`，不覆蓋舊檔。

---

## 🧭 介面流程總覽

### 步驟 0：選擇會議模式

程式最上方可選擇三種模式：

| 模式 | 說明 | 可用角色 |
| --- | --- | --- |
| 單模型（只用裁判長） | 只使用裁判長模型直接回答 | 裁判長 |
| 接力模式（A→B→裁判長） | A 先答、B 接續、裁判長整合 | 專家 A、專家 B、裁判長 |
| 討論共識（三專家→裁判長） | A/B/C 先回答，再進行共識確認，最後裁判長仲裁 | 專家 A、B、C、裁判長 |

* 討論共識模式可設定 **討論輪數上限**，目前 GUI 允許 `1` 到 `6` 輪。
* 介面會依會議模式自動啟用或停用不需要的模型與提示詞欄位。

### 步驟 1：選擇參考圖片（可選）

可使用兩種方式提供圖片：

1. **選資料夾...**：掃描資料夾中的 `.jpg`、`.jpeg`、`.png`、`.webp`
2. **選圖片檔...**：直接多選圖片檔案

補充說明：

* 程式最多會使用 **15 張圖片**。
* 若同時曾選過資料夾與圖片檔，執行時會以 **多選圖片檔** 為優先。
* 按下 **❌ 清除** 後，資料夾與圖片檔選擇都會被清空，下次將以純文字模式執行。
* 使用 LM Studio 本地模型且含圖片時，會自動改用 HTTP 方式傳送（SDK 不支援圖片 URL）。

### 步驟 2：選擇 AI 模型陣容

可分別指定：

* 🟢 專家 A
* 🔵 專家 B
* 🟠 專家 C
* 🟣 裁判長

模型清單讀取自 `ai_models.json`，目前包含（顯示名稱中的倍率為相對費用參考）：

**雲端模型（GitHub Copilot）**

| 顯示名稱 | API 模型 ID |
| --- | --- |
| Claude Sonnet 4.6 (default) \| 1x | claude-sonnet-4.6 |
| Claude Sonnet 4.5 \| 1x | claude-sonnet-4.5 |
| Claude Haiku 4.5 \| 0.33x | claude-haiku-4.5 |
| Claude Opus 4.6 \| 3x | claude-opus-4.6 |
| Claude Opus 4.5 \| 3x | claude-opus-4.5 |
| Claude Sonnet 4 \| 1x | claude-sonnet-4 |
| Gemini 3 Pro (Preview) \| 1x | gemini-3-pro-preview |
| Gemini 3.1 Pro (Preview) \| 1x | gemini-3.1-pro-preview |
| GPT-5.4 \| 1x | gpt-5.4 |
| GPT-5.3-Codex \| 1x | gpt-5.3-codex |
| GPT-5.2-Codex \| 1x | gpt-5.2-codex |
| GPT-5.2 \| 1x | gpt-5.2 |
| GPT-5.1-Codex-Max \| 1x | gpt-5.1-codex-max |
| GPT-5.1-Codex \| 1x | gpt-5.1-codex |
| GPT-5.1 \| 1x | gpt-5.1 |
| GPT-5.4 mini \| 0.33x | gpt-5.4-mini |
| GPT-5.1-Codex-Mini (Preview) \| 0.33x | gpt-5.1-codex-mini |
| GPT-5 mini \| 0x | gpt-5-mini |
| GPT-4.1 \| 0x | gpt-4.1 |

**本地模型（LM Studio）**

* **[本地] 目前 LM Studio 載入的模型**：動態取得當前 LM Studio 內已載入的模型
* **[本地] 自動掃描到的 .gguf 模型**：程式啟動時自動偵測本機路徑
* 按 **🔄 重新整理本地模型** 後，還會動態加入 **[本地-SDK]** 或 **[本地-API]** 項目（顯示實際已載入的模型 ID）

若要更改 LM Studio 伺服器網址，可直接在模型區塊的 **LM Studio URL** 欄位修改後，按 **🔄 重新整理本地模型** 套用。

### 步驟 3：設定提示詞與輸入主題

* 點擊 **⚙️ 展開系統提示詞設定** 可編輯四個角色的 system prompt。
* 每個角色都可從模板下拉選單選擇檔案，按 **套用模板** 後匯入內容。
* 若模板檔包含 `## 系統提示詞正文` 段落，程式只會擷取該段之後的內容。
* 在下方的 **User Topic** 欄位輸入本次想討論的需求、背景或問題。

### 步驟 4：啟動與停止

* 按下 **🚀 啟動會議** 後，程式會先自動儲存目前設定，再開始執行。
* 若未提供圖片，系統會先詢問是否以純文字模式繼續。
* 執行過程可在下方 **執行日誌 (Log)** 看到即時輸出。
* 若要中止，按下 **⛔ 停止** 即可。

---

## 🧠 三種模式的實際執行邏輯

### 1. 單模型模式

* 直接把使用者需求（與選用圖片）交給裁判長模型。
* 不會產生專家 A / B / C 的段落。

### 2. 接力模式（預設）

* 專家 A 先回答。
* 專家 B 依相同需求回答。
* 裁判長收到「使用者需求 + 專家 A 回答 + 專家 B 回答」後進行仲裁。

### 3. 討論共識模式

* 第 1 輪由專家 A / B / C 各自獨立回答。
* 第 2 輪起，三位專家會看到其他人的意見並產出修正版答案。
* 每位專家需在最後一行輸出：`共識：同意` 或 `共識：不同意`
* 若三位專家都表態同意，系統會提前結束共識輪次，交給裁判長生成最終文件。
* 最終報告會額外列出三位專家的共識狀態。

---

## ⚙️ 系統需求與安裝

### 前置條件

* **Python 3.8 以上**
* **GitHub Copilot 可用環境**（使用雲端模型時）
  * 程式碼使用 `from copilot import CopilotClient`
  * 需先在本機完成 GitHub Copilot 驗證登入
* **LM Studio**（使用本地模型時，可選）
  * 純文字推論：不需要開啟 LM Studio 的「本地伺服器（Local Server）」，透過官方 SDK 直連即可。
  * 含圖片推論：需要開啟 LM Studio 的「本地伺服器」並確保模型已載入。

### 安裝套件

```bash
# 必要：GitHub Copilot Python SDK
pip install github-copilot-sdk

# 必要：非同步 HTTP 連線（LM Studio HTTP 備援與圖片傳輸）
pip install aiohttp

# 可選：LM Studio 官方 SDK（純文字推論，不需開啟 Local Server）
pip install lmstudio
```

其餘如 `tkinter`、`asyncio`、`json`、`threading`、`mimetypes`、`base64` 皆為 Python 標準函式庫。

---

## 🚀 啟動方式

請在專案目錄中執行：

```bash
python board_meeting_gui_v2.8.9.py
```

---

## 🧩 提示詞模板目錄

程式會自動掃描：

```text
GEM_提示詞整合\
```

支援副檔名：

* `.md`
* `.txt`

使用建議：

* 可依角色建立不同模板，例如程式審查、商業分析、投資判讀、文件編修等。
* 如果模板檔需要同時包含說明文字與正式 prompt，建議把真正要套用的內容放在 `## 系統提示詞正文` 標題之後。

---

## 📂 輸入與輸出檔案

### `ai_models.json`

模型清單與 LM Studio 設定檔，主要包含：

* `models`：GitHub Copilot 雲端模型的顯示名稱 → API model ID 對照表
* `lm_studio_url`：LM Studio 伺服器網址（預設 `http://localhost:1234/v1`）
* `lm_studio_paths`：本機 `.gguf` 模型自動掃描路徑清單

修改此檔可新增雲端模型或調整 LM Studio 掃描路徑，無需修改程式碼。

### `board_meeting_config.json`

啟動會議前會自動寫入，主要保存：

* 圖片資料夾路徑
* 多選圖片清單
* LM Studio URL
* 會議模式
* 討論輪數上限
* 四個角色的模型選擇
* 四個模板下拉選項
* 四個 system prompt
* 本次主題內容

若設定檔損壞或想重置介面狀態，可刪除此檔後重新啟動程式。

### `AI_Board_Report_YYYYMMDD_HHMMSS.md`

每次執行完成後會產生一份 Markdown 報告，內容可能包含：

* 會議模式
* 參考圖片（以本機 `file:///` 路徑嵌入）
* 使用者需求
* 專家 A / B / C 的完整回答（依模式決定是否出現）
* 共識狀態（僅討論共識模式）
* 裁判長的最終裁決

---

## ⚠️ 注意事項

* 這是一個本機 GUI 工具，執行時需要可用的桌面環境。
* 圖片讀取支援 `.jpg`、`.jpeg`、`.png`、`.webp`；無法讀取的圖片會被略過。
* **LM Studio 連線相容性**：部分電腦環境（特別是 Windows）可能因系統設定、網路 Proxy 或 LM Studio 版本差異，導致 HTTP 模式無法連線本地伺服器。遇此情況請優先安裝 `lmstudio` 官方 SDK（`pip install lmstudio`），SDK 模式不依賴 Local Server，相容性更佳。若 SDK 與 HTTP 均無法連線，雲端 GitHub Copilot 模型不受影響，仍可正常使用。
* 程式在 Windows 上執行時會自動切換為 `WindowsSelectorEventLoopPolicy`，以避免 `aiohttp` 在預設 `ProactorEventLoop` 下連線 localhost 不穩定的問題。Python 3.14+ 的 DeprecationWarning 已靜音，Python 3.16+ 移除該 API 後會自動跳過。
* 連線 LM Studio 時，程式會繞過系統 Proxy（直連 localhost），若有特殊網路環境請注意。
* 程式目前沒有額外的自動測試或 requirements 檔；若你要在新環境部署，建議先確認 Copilot SDK、aiohttp 與 Python 版本相容。
* **v2.8.9 修復（2026-04-18）**：
  * **修正 `TypeError: t.asString is not a function` 錯誤（第二次修正，根因正確定位）**：
    * **根因分析**：前版本（v2.8.8）在 `create_session` 時以 Python SDK 的 `SystemMessageReplaceConfig` 物件格式傳遞系統提示詞：`{"mode": "replace", "content": "..."}`. CLI binary v0.0.411 的 TypeScript 執行引擎內部對 `systemMessage` 欄位呼叫 `.asString()` 方法；普通 JavaScript object 沒有此方法，因此拋出 `TypeError: t.asString is not a function`。
    * **修復策略**：改用純字串（plain string）格式傳遞 `system_message`，取代舊有的 `{"mode": "replace", "content": "..."}` 物件格式。純字串可被 CLI binary 正確識別與處理。
    * **附加強化**：
      1. `session.on()` 現在正確儲存並於 `finally` 呼叫 `unsubscribe()`，避免 handler 殘留。
      2. `done.wait()` 改為 `asyncio.wait_for(done.wait(), timeout=120.0)`，防止無回應時程式無限等待。
      3. `session.destroy()` 在 `finally` 區塊中執行，確保每次使用後正確釋放 session 資源，避免多 session 衝突。
      4. 修正 GUI 視窗標題仍顯示 `v2.8.8` 的問題，統一為 `v2.8.9`。
  * **修正 LM Studio 本地模型相容性問題（2026-04-18 第三次修正）**：
    * `LMStudioSession.on()` 原本無回傳值（`None`），導致 `finally` 區塊呼叫 `unsubscribe()` 時拋出 `TypeError`。現在正確回傳 `_unsubscribe` callable，行為與 `CopilotSession.on()` 一致。
    * 新增 `LMStudioSession.destroy()` 非同步方法，避免 `await session.destroy()` 因 `AttributeError` 而失敗。
    * 修正 `stream_agent_response()` 中的 `send_payload` 組裝邏輯：CopilotSession 僅送 `prompt`（system_prompt 已在 `create_session` 時設定），LMStudioSession 需要完整 payload（含 `system_prompt`、`images`），以維持本地模型的系統提示詞與圖片推論功能。
    * `unsubscribe()` 呼叫前增加 `if callable(unsubscribe)` 安全檢查，防止任何意外情境下的空值呼叫。
* **v2.8.8 修復**：
  * **根本修正** `CopilotClient.create_session()` 跨版本相容問題：
    * 新版 SDK (main branch) 簽名為 `create_session(*, on_permission_request, model=None, streaming=None, system_message=None, ...)`，所有參數皆為 keyword-only，`on_permission_request` 為必填，完全不接受位置引數。
    * 修正舊版 v2.8.8（前次提交）策略順序錯誤：前次實作先嘗試舊版 config-dict 方式，再以位置引數 + keyword 混傳（仍被新 SDK 拒絕），最後 `create_session()` 無引數仍缺少 `on_permission_request`，三策略全部失敗。
    * **正確策略**：先嘗試新版 keyword-only API（策略 1），再降級為舊版 config dict（策略 2），最後無參數備援（策略 3）。
  * 新增 `_PermissionApprovedResult` 相容物件，取代回傳 `dict` 的舊實作。新版 SDK 呼叫 permission handler 後會存取 `.kind`、`.rules`、`.feedback`、`.message`、`.path` 屬性；舊版 dict 在此會拋出 `AttributeError`；相容物件可正確提供所有屬性，同時不依賴 `copilot.session.PermissionRequestResult` dataclass（避免舊版 SDK 的 `ImportError`）。
  * 修正 Windows asyncio `DeprecationWarning` 仍然輸出至 stderr 的問題：將 `warnings.catch_warnings()` 靜音區塊提前到 `hasattr` 存取之前，避免屬性存取本身就觸發警告。
* **v2.8.7 修復**：
  * 修正 `CopilotClient.create_session()` 呼叫方式：正確傳入 model / streaming / system_message config dict，並以 `try/except TypeError` 向下相容不接受參數的舊版 SDK。
  * 修正 system prompt 傳遞路徑：CopilotClient 的 system prompt 現在透過 `create_session` 的 `system_message` 欄位正確套用，不再依賴 `session.send()` 忽略的額外 key。
  * 修正 Windows asyncio DeprecationWarning：使用 `warnings.catch_warnings()` 靜音 Python 3.14+ 的棄用警告，並加上 `hasattr` 守衛以相容 Python 3.16+。
  * 區分 `CopilotClient` 與 `LMStudioClient` 的 `create_session` 呼叫路徑，避免 Duck Typing 混淆。

---

## 📌 目前對應的主程式

* 主程式：`board_meeting_gui_v2.8.9.py`
* 模型設定：`ai_models.json`
* 設定檔：`board_meeting_config.json`
* 模板目錄：`GEM_提示詞整合\`
