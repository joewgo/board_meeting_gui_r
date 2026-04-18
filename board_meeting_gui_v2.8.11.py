"""
board_meeting_gui_v2.8.11.py
=============================
露娜的 AI 董事會控制台 — 多模型聯合仲裁 GUI 工具

v2.8.11 主要修正：
  1. 完全移除 create_session() 的 system_message 參數，
     改為將系統提示詞嵌入使用者 prompt 前方（以 [系統指令] / [使用者需求] 標籤區隔）。
     此方式徹底繞過 Copilot SDK / CLI binary 各版本對 system_message 型別格式不一致的問題。
  2. 移除專家模型的強制超時機制（原 120 / 240 秒），改為無限等待；
     適用於低效能裝置（如 Intel N100）使用本地模型時的長時間推論。
  3. _create_copilot_session 的例外捕捉從 TypeError 擴展至 Exception，
     避免 AttributeError 等非預期例外冒泡。
  4. 所有函式與方法皆補充完整的中文 docstring 與行內註解。
"""

import asyncio       # 非同步事件迴圈（async/await 核心）
import aiohttp       # 非同步 HTTP 用戶端（LM Studio HTTP 備援與圖片傳輸）
import sys           # 系統平台判斷（Windows asyncio 修正）

import base64        # Base64 編碼（圖片轉 data URI）
import os            # 檔案與路徑操作
import glob          # 檔案萬用字元搜尋
import json          # JSON 序列化 / 反序列化（設定檔讀寫）
import mimetypes     # 猜測圖片 MIME 類型
import threading     # 背景執行緒（避免阻塞 GUI）
import traceback     # 詳細例外堆疊追蹤（除錯用）
from datetime import datetime               # 時間戳記（報告檔名）
import tkinter as tk                         # 標準 GUI 框架
from tkinter import ttk, filedialog, scrolledtext, messagebox  # Tkinter 元件
from typing import Optional, Dict, Any, List, Tuple            # 型別提示
from urllib.parse import urlsplit, urlunsplit                   # URL 解析與重組

# ------------------------------------------------------------------
# [Copilot SDK 匯入]
# 移除所有容易因 SDK 版本差異而報錯的強型別與 Enum 匯入，
# 只保留最核心且確定存在的 CopilotClient。
# ------------------------------------------------------------------
from copilot import CopilotClient

# ------------------------------------------------------------------
# [LM Studio 官方 Python SDK — 選用]
# 可用時優先使用 SDK 連線（不需要在 LM Studio 開啟「Local Server」選項）。
# 未安裝時自動降級為 HTTP REST API 備援模式。
# ------------------------------------------------------------------
try:
    import lmstudio as lms
    HAS_LMS_SDK = True
except ImportError:
    HAS_LMS_SDK = False

# ------------------------------------------------------------------
# [Windows asyncio 相容性修正]
# Python 3.8+ 在 Windows 預設使用 ProactorEventLoop，
# 部分 aiohttp 版本在此迴圈下連線 localhost 可能不穩定。
# 強制改用 SelectorEventLoop 以提升相容性。
# Python 3.14+ 標記 WindowsSelectorEventLoopPolicy 為 deprecated（3.16 移除），
# 故 warnings 靜音必須包裹住 hasattr 與實例化兩個步驟，
# 避免 hasattr(asyncio, ...) 存取屬性時就觸發 DeprecationWarning。
# ------------------------------------------------------------------
import warnings as _warnings
with _warnings.catch_warnings():
    _warnings.simplefilter("ignore", DeprecationWarning)
    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==========================================
# [全域設定區塊]
# 定義所有程式層級的常數與預設值。
# 這些值會在找不到 board_meeting_config.json 時作為介面初始化的依據。
# ==========================================
CONFIG_FILE = "board_meeting_config.json"  # 設定檔路徑（啟動會議前自動寫入）

# 預設字串：當找不到 config.json 時，系統會使用這些預設值進行初始化
DEFAULT_PROMPT_A = """<system_configuration>\n**核心指令：啟動 [零信任事實檢查] 與 [動態人格生成] 協議**\n</system_configuration>\n請扮演 Axiom-01 (公理-01)，專注於『同行審查與邏輯校準』。"""
DEFAULT_PROMPT_B = """你是一位專注於『使用者體驗與生活品質』的電腦硬體專家。\n請基於使用者提供的圖文資訊，給出舒適且高品質的建議。"""
DEFAULT_PROMPT_C = """你是一位專注於『安全性、可讀性與開發者體驗（DX）』的全端程式語言專家。\n請基於使用者提供的圖文資訊與需求，提出務實、可落地的建議，並避免捏造資訊。"""
DEFAULT_PROMPT_JUDGE = """你是一位公正且具備全域視野的 AI 仲裁者。\n請綜合三位專家的意見，並參考使用者的圖文需求，給出最終的突破性決策，並將操作規劃整理為 Markdown 格式輸出。"""
DEFAULT_TOPIC = """這是公司大主管的電腦，華碩工作站...\n我想要知道，真的只能換CPU、 主機板、 作業系統然後重灌一路嗎？若是請給出建議的CPU、 主機板、 作業系統，規格及操作手法。\n記憶體是金士頓的DDR5 5600 32GB 1.25V 共4支，這個應該不用重買吧？"""

PROMPT_TEMPLATES_ROOT = "GEM_提示詞整合"

MEETING_MODE_MAPPING = {
    "單模型（只用裁判長）": "single",
    "接力模式（A→B→裁判長）": "relay",
    "討論共識（三專家→裁判長）": "debate"
}
DEFAULT_MEETING_MODE = "relay"
DEFAULT_MAX_ROUNDS = 2

# 模型對照表：將 GUI 顯示的友善名稱，映射為 SDK 實際認得的 API ID

MODEL_MAPPING = {}
LM_STUDIO_URL = "http://127.0.0.1:1234/v1"

def get_lm_studio_url_candidates(raw_url: str) -> List[str]:
    """根據使用者輸入的 LM Studio URL，產生多個候選 URL 供依序嘗試連線。

    參數:
        raw_url: 使用者在 GUI 或設定檔中提供的原始 URL 字串。
                 可為完整 URL、僅含 host:port、或甚至空字串。

    回傳:
        List[str]: 正規化後的候選 URL 清單。
        例如輸入 "localhost:1234" 可能產生:
          ["http://localhost:1234/v1", "http://localhost:1234/api/v1",
           "http://127.0.0.1:1234/v1", "http://127.0.0.1:1234/api/v1"]

    邏輯說明:
        1. 補上 http:// 前綴（若缺少 scheme）
        2. 清理尾端已知的 API 路徑（/models, /chat/completions, /chat）
        3. 補上 /v1 或 /api/v1 路徑變體
        4. 交叉 localhost ↔ 127.0.0.1 主機名稱變體
        5. 去重並維持順序
    """
    raw = str(raw_url or "").strip()
    if not raw:
        raw = LM_STUDIO_URL
    if "://" not in raw:
        raw = f"http://{raw}"

    parts = urlsplit(raw)
    scheme = parts.scheme or "http"
    netloc = parts.netloc or parts.path
    path = parts.path if parts.netloc else ""
    if not netloc:
        netloc = "127.0.0.1:1234"
    path = path.rstrip("/")
    if path.endswith("/models"):
        path = path[:-7]
    elif path.endswith("/chat/completions"):
        path = path[:-17]
    elif path.endswith("/chat"):
        path = path[:-5]
    if not path:
        path = "/v1"
    elif not path.endswith("/v1"):
        path = f"{path}/v1"
    path_variants = [path]
    if path == "/v1":
        path_variants.append("/api/v1")
    elif path == "/api/v1":
        path_variants.append("/v1")

    host, sep, port = netloc.partition(":")
    host_variants = [host]
    if host == "localhost":
        host_variants.append("127.0.0.1")
    elif host == "127.0.0.1":
        host_variants.append("localhost")

    candidates: List[str] = []
    for hv in host_variants:
        netloc_variant = f"{hv}{sep}{port}" if sep else hv
        for pv in path_variants:
            url = urlunsplit((scheme, netloc_variant, pv, "", "")).rstrip("/")
            if url not in candidates:
                candidates.append(url)
    return candidates

def normalize_lm_studio_url(raw_url: str) -> str:
    """將使用者輸入的 LM Studio URL 正規化為最優先的候選格式。

    參數:
        raw_url: 原始 URL 字串

    回傳:
        str: 正規化後的第一個候選 URL（最可能成功連線的格式）
    """
    return get_lm_studio_url_candidates(raw_url)[0]


def extract_model_ids(data: Dict[str, Any]) -> List[str]:
    """從 LM Studio API 的 JSON 回應中擷取所有已載入的模型 ID。

    參數:
        data: LM Studio /v1/models 回傳的 JSON dict。
              支援兩種格式：
              - OpenAI 相容格式：data["data"][i]["id"]
              - LM Studio 原生格式：data["models"][i]["loaded_instances"][j]["id"]

    回傳:
        List[str]: 模型 ID 字串清單（不含空值或非字串）
    """
    ids: List[str] = []
    for m in data.get("data", []):
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            ids.append(mid)
    for m in data.get("models", []):
        loaded = m.get("loaded_instances", []) if isinstance(m, dict) else []
        for inst in loaded:
            mid = inst.get("id") if isinstance(inst, dict) else None
            if isinstance(mid, str) and mid:
                ids.append(mid)
    return ids


def urlopen_without_proxy(req, timeout=3):
    """繞過系統 Proxy 發送 HTTP 請求（直連 localhost）。

    參數:
        req: urllib.request.Request 物件
        timeout: 連線逾時秒數（預設 3 秒）

    回傳:
        HTTPResponse 物件（需由呼叫者以 with 語句管理生命週期）

    說明:
        LM Studio 通常運行在 localhost，透過 Proxy 連線會失敗或延遲。
        此函式建立一個不使用任何 Proxy 的 opener 來直接連線。
    """
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)

def load_model_config():
    """讀取 ai_models.json 並建立全域模型對照表 MODEL_MAPPING。

    功能：
        1. 從 ai_models.json 讀取雲端模型清單（GitHub Copilot）
        2. 讀取 LM Studio URL 設定
        3. 自動掃描本機 .gguf 模型檔（排除 mmproj 投影檔）
        4. 掃描路徑涵蓋：
           - ai_models.json 中設定的 lm_studio_paths
           - 當前使用者 ~/.lmstudio/models
           - Windows 上多個 AppData / Program Files 安裝路徑

    副作用：
        - 清空並重建 MODEL_MAPPING 全域字典
        - 更新 LM_STUDIO_URL 全域變數
    """
    global MODEL_MAPPING, LM_STUDIO_URL
    MODEL_MAPPING.clear()

    try:
        if os.path.exists("ai_models.json"):
            with open("ai_models.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                MODEL_MAPPING.update(data.get("models", {}))
                raw_url = data.get("lm_studio_url", LM_STUDIO_URL)
                LM_STUDIO_URL = normalize_lm_studio_url(raw_url)

                # 掃描本地 .gguf 模型（排除 mmproj 投影檔）
                # 自動加入當前使用者的預設 LM Studio 模型路徑（跨電腦相容）
                configured_paths = data.get("lm_studio_paths", [])
                default_lm_path = os.path.join(os.path.expanduser("~"), ".lmstudio", "models")
                paths_to_scan = list(configured_paths)
                if default_lm_path not in paths_to_scan:
                    paths_to_scan.append(default_lm_path)
                # 補充 Windows 單一使用者安裝（User Scope）的常見模型路徑
                # 公司電腦安裝於 AppData 而非 Program Files 時，模型可能存於此
                if sys.platform == "win32":
                    user_home = os.path.expanduser("~")
                    extra_win_paths = [
                        os.path.join(user_home, "AppData", "Roaming", "lm-studio", "models"),
                        os.path.join(user_home, "AppData", "Local", "lm-studio", "models"),
                        os.path.join(user_home, "AppData", "Roaming", "LM Studio", "models"),
                        os.path.join(user_home, "AppData", "Local", "LM Studio", "models"),
                        # 「安裝給所有使用者」路徑（For All Users install）
                        r"C:\LM Studio\models",
                        r"C:\Program Files\LM Studio\models",
                        r"C:\Program Files (x86)\LM Studio\models",
                        os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "LM Studio", "models"),
                        os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "LM Studio", "models"),
                    ]
                    for ep in extra_win_paths:
                        if ep not in paths_to_scan:
                            paths_to_scan.append(ep)
                for base_path in paths_to_scan:
                    abs_base = os.path.abspath(base_path)
                    if not os.path.exists(abs_base):
                        continue
                    for root_dir, dirs, files in os.walk(abs_base):
                        for file in files:
                            if not file.lower().endswith(".gguf"):
                                continue
                            if file.lower().startswith("mmproj"):
                                continue  # 跳過多模態投影檔
                            full_path = os.path.join(root_dir, file)
                            # LM Studio API 使用相對路徑作為 model ID
                            rel_path = os.path.relpath(full_path, abs_base).replace("\\", "/")
                            parent = os.path.basename(root_dir)
                            stem = os.path.splitext(file)[0]
                            display = f"[本地] {parent} / {stem}"
                            MODEL_MAPPING[display] = f"local:{rel_path}"
    except Exception as e:
        print(f"Error loading models: {e}")
        if not MODEL_MAPPING:
            MODEL_MAPPING["GPT-5 mini (Fallback)"] = "gpt-5-mini"

    # 通用選項：使用目前 LM Studio 內已載入的模型
    MODEL_MAPPING["[本地] 目前 LM Studio 載入的模型"] = "local:current_model"

# 程式啟動時立即載入
load_model_config()


# ==========================================
# [邏輯區塊] 核心工具函式
# ==========================================

def _guess_image_mime(path: str) -> str:
    """根據檔案路徑猜測圖片的 MIME 類型。

    參數:
        path: 圖片檔案的完整路徑

    回傳:
        str: MIME 類型字串（例如 "image/jpeg"、"image/png"）。
             若無法辨識則預設回傳 "image/jpeg"。
    """
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("image/"):
        return mime
    return "image/jpeg"


def encode_images_from_paths(paths: List[str], max_images: int = 15) -> Tuple[List[str], List[str]]:
    """將圖片路徑列表轉換為 Base64 data URI 列表（供 Copilot SDK 多模態輸入）。

    參數:
        paths: 圖片檔案路徑清單
        max_images: 最大處理張數（預設 15 張，避免 payload 過大）

    回傳:
        Tuple[List[str], List[str]]:
            - used_paths: 實際成功編碼的圖片絕對路徑清單
            - data_uris: 對應的 data URI 字串清單（格式：data:image/xxx;base64,...）

    說明:
        - 自動清理空字串、前後空白、引號
        - 去重（以絕對路徑小寫為 key，保持原始順序）
        - 無法讀取的圖片會被靜默跳過（不中斷流程）
    """
    if not paths:
        return [], []

    cleaned: List[str] = []
    for p in paths:
        if not p:
            continue
        p2 = str(p).strip().strip('"')
        if os.path.exists(p2):
            cleaned.append(p2)

    # 去重（保序），並限制最大張數
    seen = set()
    unique_paths: List[str] = []
    for p in cleaned:
        ap = os.path.abspath(p)
        key = ap.lower()
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(ap)

    unique_paths = unique_paths[:max_images]

    used_paths: List[str] = []
    data_uris: List[str] = []
    for path in unique_paths:
        try:
            with open(path, "rb") as image_file:
                encoded = base64.b64encode(image_file.read()).decode("utf-8")
            mime = _guess_image_mime(path)
            used_paths.append(path)
            data_uris.append(f"data:{mime};base64,{encoded}")
        except Exception:
            pass  # 忽略無法讀取的圖片檔案

    return used_paths, data_uris


def encode_images_from_directory(directory: str, max_images: int = 15) -> Tuple[List[str], List[str]]:
    """掃描指定資料夾中的圖片檔並回傳編碼結果。

    參數:
        directory: 要掃描的資料夾路徑
        max_images: 最大處理張數（預設 15 張）

    回傳:
        Tuple[List[str], List[str]]:
            - image_paths: 找到的圖片絕對路徑清單（已排序）
            - image_data_uris: 對應的 data URI 字串清單

    說明:
        支援的副檔名：.jpg、.jpeg、.png、.webp
        若資料夾不存在或為空字串，回傳兩個空清單。
    """
    if not directory or not os.path.exists(directory):
        return [], []

    valid_extensions = ("*.jpg", "*.jpeg", "*.png", "*.webp")
    image_paths: List[str] = []

    for ext in valid_extensions:
        image_paths.extend(glob.glob(os.path.join(directory, ext)))

    image_paths = sorted(image_paths)
    return encode_images_from_paths(image_paths, max_images=max_images)


def build_payload(system_prompt: str, user_prompt: str, image_data_uris: List[str] = None) -> Dict[str, Any]:
    """組裝要發送給 AI 模型的請求資料結構 (Payload)。

    參數:
        system_prompt: 該角色的系統提示詞（system prompt）
        user_prompt: 使用者的議題文字（user topic）
        image_data_uris: 圖片的 Base64 data URI 清單（可選）

    回傳:
        Dict[str, Any]: 包含 prompt、system_prompt、images（若有）的字典。

    v2.8.11 說明:
        system_prompt 僅作為資料載體儲存在 payload 中，
        實際傳送時由 stream_agent_response() 決定如何組裝：
        - CopilotClient：system_prompt 嵌入 prompt 前方（不經 create_session 的 system_message）
        - LMStudioClient：system_prompt 由 HTTP payload 或 SDK Chat 物件獨立處理
    """
    # 將 system_prompt 與 user_prompt 分開保存，由下游函式決定合併方式
    payload = {"prompt": user_prompt}

    if image_data_uris:
        payload["images"] = image_data_uris
    
    # 將 system_prompt 存在 payload 中，僅供 stream_agent_response 取用
    payload["system_prompt"] = system_prompt

    return payload


def extract_system_prompt_from_template(text: str) -> str:
    """從模板文字中擷取系統提示詞正文。

    參數:
        text: 完整的模板檔案文字內容

    回傳:
        str: 擷取後的系統提示詞文字。
             若模板包含「## 系統提示詞正文」標題，僅取該段之後的內容。
             若無此標題，則回傳完整文字（去除前後空白）。

    說明:
        模板檔可同時包含說明文字與正式 prompt，
        此函式確保只有「## 系統提示詞正文」之後的內容被套用到角色 system prompt。
    """
    lines = (text or "").splitlines()
    found = False
    started = False
    out: List[str] = []

    for line in lines:
        if not found and ("系統提示詞正文" in line) and line.strip().startswith("##"):
            found = True
            continue

        if found and not started:
            if not line.strip():
                continue
            started = True

        if started:
            out.append(line)

    extracted = "\n".join(out).strip() if found else (text or "").strip()
    return extracted


def list_prompt_templates(root_dir: str = PROMPT_TEMPLATES_ROOT) -> Dict[str, str]:
    """掃描提示詞模板目錄，回傳可供 GUI 下拉選擇的模板清單。

    參數:
        root_dir: 模板根目錄路徑（預設為 GEM_提示詞整合）

    回傳:
        Dict[str, str]: {顯示名稱: 檔案絕對路徑}。
        若根目錄不存在則回傳空字典。

    說明:
        - 支援 .md 與 .txt 副檔名
        - 遞迴搜尋所有子目錄
        - 顯示名稱為相對於根目錄的路徑，重名時自動加編號
    """
    abs_root = os.path.abspath(root_dir)
    if not os.path.exists(abs_root):
        return {}

    patterns = [
        os.path.join(abs_root, "**", "*.md"),
        os.path.join(abs_root, "**", "*.txt"),
    ]

    files: List[str] = []
    for p in patterns:
        files.extend(glob.glob(p, recursive=True))

    files = sorted(set(files))

    result: Dict[str, str] = {}
    for full in files:
        rel = os.path.relpath(full, abs_root)
        display = rel
        # 避免重名
        if display in result:
            base = os.path.basename(full)
            i = 2
            while f"{base} ({i})" in result:
                i += 1
            display = f"{base} ({i})"
        result[display] = full

    return result


def parse_agreement(text: str) -> Optional[bool]:
    """從專家回答文字中解析共識表態（同意 / 不同意）。

    參數:
        text: 專家的完整回答文字

    回傳:
        Optional[bool]:
            - True: 文字中包含「同意」且不包含「不同意」
            - False: 文字中包含「不同意」
            - None: 無法辨識（空文字或未包含相關關鍵字）

    說明:
        優先檢查「不同意」，因為「不同意」包含「同意」子字串。
    """
    if not text:
        return None
    if "不同意" in text:
        return False
    if "同意" in text:
        return True
    return None



class LMStudioSession:
    """LM Studio 的會話物件，模擬 CopilotSession 的介面（Duck Typing）。

    提供與 CopilotSession 相同的 on() / send() / destroy() 方法，
    讓 stream_agent_response() 可以用統一的邏輯處理雲端與本地模型。

    連線優先順序：
        1. lmstudio 官方 Python SDK（不需開啟 Local Server）
        2. HTTP REST API — OpenAI 相容格式 /chat/completions
        3. HTTP REST API — LM Studio 原生格式 /chat
    """

    def __init__(self, url, model=""):
        """初始化 LM Studio 會話。

        參數:
            url: LM Studio 伺服器的基礎 URL
            model: 指定的模型 ID（已去除 local: 前綴）。
                   若為空字串或 "current_model"，將動態查詢目前載入的模型。
        """
        self.url = normalize_lm_studio_url(url)
        self.url_candidates = get_lm_studio_url_candidates(self.url)
        self.model = model  # 由 LMStudioClient.create_session 傳入（已去除 local: 前綴）
        self.callbacks = []

    def on(self, callback):
        """註冊事件回呼函式（模擬 CopilotSession.on()）。

        參數:
            callback: 事件處理函式，接收 event 物件

        回傳:
            callable: 取消訂閱的函式（呼叫後移除此 callback）
        """
        self.callbacks.append(callback)
        # 回傳 unsubscribe callable，與 CopilotSession.on() 行為一致
        def _unsubscribe():
            """取消此 callback 的訂閱（從回呼清單中移除）"""
            try:
                self.callbacks.remove(callback)
            except ValueError:
                pass
        return _unsubscribe

    async def destroy(self):
        """釋放會話資源（LMStudioSession 無需額外清理，僅為介面一致性）。

        說明:
            CopilotSession.destroy() 會釋放伺服器端資源，
            LMStudioSession 使用無狀態 HTTP，故僅清空 callback 清單。
        """
        self.callbacks.clear()

    async def _get_current_model(self) -> str:
        """查詢 LM Studio /v1/models API 取得目前已載入的模型 ID。

        回傳:
            str: 第一個找到的模型 ID，若全部失敗則回傳 "local-model"。

        說明:
            依序嘗試所有候選 URL，每個限時 3 秒。
            成功連線時會更新 self.url 為該候選 URL。
        """
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession() as s:
            for base_url in self.url_candidates:
                try:
                    async with s.get(f"{base_url}/models", timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            model_ids = extract_model_ids(data)
                            if model_ids:
                                self.url = base_url
                                return model_ids[0]
                except Exception:
                    continue
        return "local-model"

    async def _send_via_sdk(self, payload):
        """使用 lmstudio 官方 Python SDK 進行推論（不需要開啟 LM Studio Local Server）。

        參數:
            payload: 請求資料字典，包含 prompt、system_prompt 等欄位

        例外:
            RuntimeError: SDK 連線失敗時拋出，由 send() 決定是否退回 HTTP 模式

        說明:
            SDK 連線不需要 LM Studio 開啟「本地伺服器」選項，
            透過 lmstudio Python SDK 直接與 LM Studio 應用程式通訊。
            SDK 不支援圖片 URL，含圖片時應改用 HTTP 模式。
        """
        loop = asyncio.get_event_loop()

        model_id = self.model if self.model and self.model != "current_model" else None
        system_prompt = payload.get("system_prompt", "")
        user_prompt = payload.get("prompt", "")

        def _run_sdk_sync():
            """在同步上下文中執行 lmstudio SDK 推論（由 run_in_executor 呼叫）"""
            try:
                with lms.Client() as client:
                    # model_id 為 None 時使用目前 LM Studio 已載入的模型
                    model_handle = client.llm.model(model_id)
                    # 使用官方 lms.Chat 管理對話上下文
                    chat = lms.Chat(system_prompt) if system_prompt else lms.Chat()
                    chat.add_user_message(user_prompt)
                    stream = model_handle.respond_stream(chat)
                    for fragment in stream:
                        # fragment.content 是 SDK 1.x 的正確屬性；str() 作為舊版備用
                        text = fragment.content if hasattr(fragment, "content") else str(fragment)
                        if text:
                            self._trigger_data(text)
                self._trigger_done()
            except Exception as e:
                # 不在此直接觸發錯誤，改以 RuntimeError 回拋讓 send() 決策
                raise RuntimeError(
                    f"lmstudio SDK 連線失敗：{e}\n"
                    "請確認 LM Studio 已開啟且模型已載入。"
                ) from e

        try:
            await loop.run_in_executor(None, _run_sdk_sync)
        except Exception as sdk_err:
            # 記錄 SDK 失敗原因，但不中斷流程（由 send() 決定是否回退 HTTP）
            raise sdk_err

    async def send(self, payload):
        """發送推論請求至 LM Studio（核心入口）。

        參數:
            payload: 請求資料字典，包含：
                - prompt (str): 使用者輸入文字
                - system_prompt (str, 可選): 系統提示詞
                - images (List[str], 可選): 圖片 data URI 清單

        連線策略（依序嘗試）：
            1. lmstudio SDK（若已安裝且無圖片）— 不需開啟 Local Server
            2. HTTP REST API — 依序嘗試所有候選 URL：
               a. /api/v1/chat（LM Studio 原生格式，非串流）
               b. /v1/chat/completions（OpenAI 相容格式，串流）
            3. 全部失敗時觸發 session.error 事件

        說明:
            含圖片時強制使用 HTTP 模式，因 SDK 不支援圖片 URL。
        """
        if HAS_LMS_SDK and not payload.get("images"):
            try:
                await self._send_via_sdk(payload)
                return  # SDK 成功，不需要 HTTP 備援
            except Exception as sdk_err:
                # SDK 連線失敗（LM Studio 未開啟、版本不相容等），自動退回 HTTP 模式
                print(f"[LM Studio] SDK 連線失敗，改用 HTTP 模式：{sdk_err}")

        # 以下為原始 HTTP 直連方式（備用）
        # 將 Copilot payload 格式轉換為 OpenAI Chat Completions 格式
        messages = []
        if payload.get("system_prompt"):
            messages.append({"role": "system", "content": payload["system_prompt"]})

        has_images = bool(payload.get("images"))

        if has_images:
            # 多模態：使用陣列格式（支援圖片）
            user_content = []
            if payload.get("prompt"):
                user_content.append({"type": "text", "text": payload["prompt"]})
            for img_uri in payload["images"]:
                user_content.append({"type": "image_url", "image_url": {"url": img_uri}})
            messages.append({"role": "user", "content": user_content})
        else:
            # 純文字：使用純字串格式，相容所有 LM Studio 版本
            messages.append({"role": "user", "content": payload.get("prompt", "")})

        # 若為 current_model 特殊值，動態查詢目前載入的模型
        model_id = self.model
        if not model_id or model_id == "current_model":
            model_id = await self._get_current_model()
            if model_id == "local-model":
                self._trigger_error(
                    "LM Studio 已連線但未載入任何模型。\n"
                    "請先在 LM Studio 介面中載入一個模型後再試。"
                )
                return

        openai_data = {
            "model": model_id,
            "messages": messages,
            "stream": True,
            "temperature": 0.7
        }
        if has_images:
            native_input = []
            if payload.get("prompt"):
                native_input.append({"type": "message", "content": payload["prompt"]})
            for img_uri in payload["images"]:
                native_input.append({"type": "image", "data_url": img_uri})
        else:
            native_input = payload.get("prompt", "")
        native_data = {
            "model": model_id,
            "input": native_input,
            "system_prompt": payload.get("system_prompt", ""),
            "stream": False,
            "temperature": 0.7
        }

        timeout = aiohttp.ClientTimeout(connect=10, sock_connect=10, sock_read=300)
        last_error = None
        async with aiohttp.ClientSession() as session:
            for base_url in self.url_candidates:
                try:
                    is_native_api = base_url.endswith("/api/v1")
                    endpoint = "/chat" if is_native_api else "/chat/completions"
                    req_data = native_data if is_native_api else openai_data
                    async with session.post(
                        f"{base_url}{endpoint}", json=req_data, timeout=timeout
                    ) as response:
                        if response.status != 200:
                            err_text = await response.text()
                            last_error = f"HTTP {response.status} ({base_url}): {err_text}"
                            continue

                        self.url = base_url
                        if is_native_api:
                            data = await response.json()
                            texts = [
                                item.get("content", "")
                                for item in data.get("output", [])
                                if isinstance(item, dict) and item.get("type") == "message"
                            ]
                            content = "\n".join(t for t in texts if t)
                            if content:
                                self._trigger_data(content)
                            self._trigger_done()
                            return
                        async for line in response.content:
                            line = line.decode('utf-8').strip()
                            if not line or line == 'data: [DONE]':
                                continue
                            if line.startswith('data: '):
                                json_str = line[6:]
                                try:
                                    chunk = json.loads(json_str)
                                    if chunk['choices'][0]['delta'].get('content'):
                                        content = chunk['choices'][0]['delta']['content']
                                        self._trigger_data(content)
                                except Exception:
                                    pass
                        self._trigger_done()
                        return
                except aiohttp.ClientConnectorError as e:
                    last_error = (
                        f"無法連線至 LM Studio ({base_url})：{e}\n"
                        "請確認 LM Studio 已啟動且「本地伺服器 (Local Server)」已開啟。"
                    )
                    continue
                except Exception as e:
                    last_error = f"連線失敗 ({base_url}): {str(e)}"
                    continue

        self._trigger_error(last_error or "連線失敗 (請確認 LM Studio Server 已啟動)")

    def _trigger_data(self, content):
        """觸發資料事件，將文字碎片推送給所有已註冊的 callback。

        參數:
            content: 模型產生的文字碎片（delta_content）

        說明:
            建立一個模擬 CopilotSession 事件的輕量物件，
            設定 event.data.delta_content 屬性後分發給所有 callback。
        """
        class Event:
            pass
        e = Event()
        e.data = Event()
        e.data.delta_content = content
        for cb in self.callbacks:
            cb(e)

    def _trigger_error(self, msg):
        """觸發錯誤事件（session.error），通知 handle_event 中斷回合。

        參數:
            msg: 錯誤訊息字串
        """
        class Event:
            pass
        e = Event()
        e.type = Event()
        e.type.value = "session.error"
        e.data = Event()
        e.data.message = msg
        for cb in self.callbacks:
            cb(e)

    def _trigger_done(self):
        """觸發完成事件（session.idle），通知 handle_event 結束等待。"""
        class Event:
            pass
        e = Event()
        e.type = Event()
        e.type.value = "session.idle"
        for cb in self.callbacks:
            cb(e)

class LMStudioClient:
    """LM Studio 用戶端，模擬 CopilotClient 介面。

    提供 create_session() 方法，回傳 LMStudioSession 物件，
    讓 stream_agent_response() 可以用統一邏輯處理雲端與本地模型。
    """

    def __init__(self, base_url="http://127.0.0.1:1234/v1"):
        """初始化 LM Studio 用戶端。

        參數:
            base_url: LM Studio 伺服器的基礎 URL（預設 http://127.0.0.1:1234/v1）
        """
        self.base_url = base_url.rstrip('/')

    async def create_session(self, config):
        """建立 LMStudioSession（模擬 CopilotClient.create_session）。

        參數:
            config: 設定字典，包含 model 欄位（可帶有 "local:" 前綴）

        回傳:
            LMStudioSession: 已初始化的會話物件
        """
        # 取出並清理 model ID（去除 local: 前綴後交給 session 保管）
        raw_model = config.get("model", "")
        if str(raw_model).startswith("local:"):
            raw_model = raw_model[len("local:"):]
        session = LMStudioSession(self.base_url, raw_model)
        return session


def _default_permission_handler(request, invocation=None):
    """自動核准所有來自 Copilot 的工具權限請求。

    參數:
        request: SDK 傳入的權限請求物件
        invocation: SDK 傳入的呼叫資訊（可選，部分版本不傳入）

    回傳:
        dict: 符合 PermissionRequestResult TypedDict 格式的純 dict，
              包含 kind="approved"。使用純 dict 而非自訂物件，
              確保 JSON 序列化不會失敗（v2.8.10 修正）。

    說明:
        Board Meeting 場景不呼叫任何外部工具，此 handler 通常不會被觸發；
        但 SDK 要求必須傳入合法 callable，此處作為安全保障。
    """
    return {"kind": "approved"}


async def _create_copilot_session(client, copilot_config: Dict[str, Any]):
    """建立 CopilotClient session，自動適配不同版本的 SDK API。

    參數:
        client: CopilotClient 實例
        copilot_config: 會話設定字典，可包含 model、streaming 等欄位。
                        v2.8.11 起不再包含 system_message（已移至 prompt 內嵌）。

    回傳:
        CopilotSession: 已建立的會話物件

    相容策略（依序嘗試，每一步失敗則降級至下一步）：
        策略 1：直接傳入 config dict（含 on_permission_request）— SDK v0.1.x
        策略 2：keyword-only API — SDK main branch
        策略 3：只傳 config dict（不含 on_permission_request）— 舊版 SDK
        策略 4：不帶任何參數 — 極舊版 SDK

    v2.8.11 修正：
        - 例外捕捉從 TypeError 擴展至 Exception，避免 AttributeError 等非預期例外冒泡。
        - 移除 system_message，徹底繞過不同 SDK 版本對其格式不一致的問題。
    """
    # 策略 1：直接傳入 config dict（含 on_permission_request）
    # 符合 SDK v0.1.x 文件規範：create_session(config: Optional[SessionConfig] = None)
    try:
        config_with_handler = {**copilot_config, "on_permission_request": _default_permission_handler}
        return await client.create_session(config_with_handler)
    except Exception:
        pass

    # 策略 2：新版 SDK (main) — 全 keyword-only，on_permission_request 為必填 kwarg
    try:
        session_kwargs = {k: v for k, v in copilot_config.items() if k != "on_permission_request"}
        return await client.create_session(
            on_permission_request=_default_permission_handler,
            **session_kwargs,
        )
    except Exception:
        pass

    # 策略 3：舊版 SDK (≤0.1.25) — 傳入 config dict（不含 on_permission_request）
    try:
        return await client.create_session(copilot_config)
    except Exception:
        pass

    # 策略 4：極舊版 SDK — 不帶任何參數
    return await client.create_session()


async def stream_agent_response(client: CopilotClient, model_id: str, payload: dict, role_name: str, log_callback) -> str:
    """負責處理單一 AI 模型的即時串流輸出（核心推論函式）。

    參數:
        client: CopilotClient 或 LMStudioClient 實例
        model_id: 模型 ID（雲端模型直接使用 API ID，本地模型帶 "local:" 前綴）
        payload: build_payload() 產生的資料字典
        role_name: 角色顯示名稱（如 "🟢 專家 A"），用於日誌輸出
        log_callback: GUI 日誌輸出函式，簽名為 log_callback(text, newline=True)

    回傳:
        str: 該模型的完整回應文字（所有串流碎片拼接而成）

    v2.8.11 關鍵修正：
        1. CopilotClient 不再透過 create_session 的 system_message 傳遞系統提示詞，
           改為將 system_prompt 嵌入 user prompt 前方（以 [系統指令] / [使用者需求] 標籤區隔）。
           此方式徹底繞過 SDK / CLI binary 各版本對 system_message 型別格式不一致的問題。
        2. 移除 asyncio.wait_for 的超時限制，改用 bare await done.wait()，
           允許低效能裝置（如 Intel N100）上的模型充分推論，不會被強制中斷。
        3. LMStudioSession 的 payload 不受影響，仍由 HTTP / SDK 獨立處理 system_prompt。

    使用動態屬性檢查 (Duck Typing) 來處理事件，避開 SDK 版本差異導致的 ImportError。
    """
    log_callback(f"\n{'-'*40}\n[{role_name} ({model_id}) 正在思考與作答...]\n", newline=False)
    
    # ------------------------------------------------------------------
    # 建立支援串流的會話
    # ------------------------------------------------------------------
    if isinstance(client, LMStudioClient):
        # LMStudioClient：傳入 model config 以指定本地模型
        session = await client.create_session({"model": model_id})
    else:
        # CopilotClient：v2.8.11 修正 — 不傳 system_message，避免 SDK 版本差異
        # system_prompt 改為嵌入 prompt 前方
        copilot_config: Dict[str, Any] = {"model": model_id, "streaming": True}
        # 注意：此處刻意不設定 copilot_config["system_message"]
        session = await _create_copilot_session(client, copilot_config)
    
    done = asyncio.Event()        # 用於等待模型回應完成的同步旗標
    response_accumulator = []     # 收集所有串流文字碎片
    
    def handle_event(event):
        """動態事件處理器：不依賴任何強型別 Enum，純粹以屬性檢查判斷事件類型。

        處理兩類事件：
            1. 文字碎片（delta_content）— 附加到累加器並即時顯示在日誌
            2. 會話狀態（session.idle / session.error）— 設定 done 旗標結束等待
        """
        # 1. 嘗試捕捉文字碎片（模型產生的回應片段）
        if hasattr(event, 'data') and hasattr(event.data, 'delta_content') and event.data.delta_content:
            chunk = event.data.delta_content
            response_accumulator.append(chunk)
            log_callback(chunk, newline=False)
            
        # 2. 判斷會話是否結束或發生錯誤
        try:
            event_type_val = event.type.value if hasattr(event.type, 'value') else str(event.type)
            
            if event_type_val == "session.idle":
                # 模型已完成回應
                done.set()
            elif event_type_val == "session.error":
                # 模型回應過程中發生錯誤
                err_msg = event.data.message if hasattr(event, 'data') and hasattr(event.data, 'message') else "未知錯誤"
                log_callback(f"\n[錯誤] {role_name} 發生中斷: {err_msg}")
                done.set()
        except Exception:
            pass  # 忽略無法解析的事件（相容不同 SDK 版本）
            
    # 註冊事件監聽器
    unsubscribe = session.on(handle_event)
    
    # ------------------------------------------------------------------
    # 組裝 send_payload 並發送
    # ------------------------------------------------------------------
    if isinstance(client, LMStudioClient):
        # LMStudioSession 需要完整 payload（含 system_prompt 與 images），
        # 由 LMStudioSession.send() 內部決定如何組裝 HTTP 請求或 SDK Chat 物件
        send_payload = payload
    else:
        # CopilotSession：v2.8.11 修正 — 將 system_prompt 嵌入 prompt 前方
        # 使用明確的標籤區隔，讓模型能清楚分辨系統指令與使用者需求
        system_prompt = payload.get("system_prompt", "")
        user_prompt = payload.get("prompt", "")
        if system_prompt:
            combined_prompt = (
                f"[系統指令]\n{system_prompt}\n\n"
                f"[使用者需求]\n{user_prompt}"
            )
        else:
            combined_prompt = user_prompt
        send_payload = {"prompt": combined_prompt}
    
    try:
        await session.send(send_payload)
        
        # v2.8.11：移除超時限制，允許模型充分推論（尤其是低效能裝置上的本地模型）
        # 使用者可透過 GUI 的「⛔ 停止」按鈕手動中止
        await done.wait()
    finally:
        # 確保每次都清理事件監聽器與 session，避免資源洩漏或多 session 衝突
        if callable(unsubscribe):
            unsubscribe()
        try:
            await session.destroy()
        except Exception:
            pass
    
    log_callback("\n")  # 補上最後的換行
    return "".join(response_accumulator)

async def run_board_meeting(
    meeting_mode: str,
    max_rounds: int,
    model_a: str,
    model_b: str,
    model_c: str,
    judge: str,
    image_dir: str,
    image_files: List[str],
    topic: str,
    sys_a: str,
    sys_b: str,
    sys_c: str,
    sys_judge: str,
    log_callback,
    cancel_event=None,
):
    """非同步核心：控制整場會議的流程、專家推論、共識確認與報告產生。

    參數:
        meeting_mode: 會議模式 ID（"single" / "relay" / "debate"）
        max_rounds: 討論共識模式的最大輪數
        model_a ~ model_c: 三位專家的模型 ID
        judge: 裁判長的模型 ID
        image_dir: 圖片資料夾路徑（與 image_files 二選一）
        image_files: 多選圖片檔案路徑清單
        topic: 使用者的議題文字
        sys_a ~ sys_judge: 四個角色的系統提示詞
        log_callback: GUI 日誌輸出函式
        cancel_event: threading.Event，使用者按下「⛔ 停止」時會被 set()

    流程：
        1. 初始化 CopilotClient 與 LMStudioClient
        2. 編碼圖片（若有）
        3. 依據會議模式執行對應的推論流程
        4. 產生 Markdown 報告並寫入檔案
        5. 處理取消、例外與清理

    說明:
        此函式在背景執行緒中的 asyncio 事件迴圈內執行，
        不會阻塞 GUI 主迴圈。
    """

    def _cancelled():
        """檢查使用者是否已發出取消訊號"""
        return cancel_event is not None and cancel_event.is_set()

    mode_label = next((k for k, v in MEETING_MODE_MAPPING.items() if v == meeting_mode), meeting_mode)
    log_callback("--- 露娜的 AI 董事會 (v2.8.11) 啟動 ---")
    log_callback(f"[系統] 會議模式：{mode_label}")


    client_copilot = CopilotClient()
    client_local = LMStudioClient(LM_STUDIO_URL)
    
    def get_client(model_id):
        """根據模型 ID 前綴判斷應使用雲端還是本地用戶端。

        參數:
            model_id: 模型 ID 字串。以 "local:" 開頭表示本地模型。

        回傳:
            CopilotClient 或 LMStudioClient 實例
        """
        if str(model_id).startswith("local:"):
            return client_local
        return client_copilot


    # 圖片來源：優先使用 image_files（多選檔案），否則使用資料夾掃描
    if image_files:
        image_paths, image_data_uris = encode_images_from_paths(image_files)
    else:
        image_paths, image_data_uris = encode_images_from_directory(image_dir)

    if image_paths:
        log_callback(f"[系統] 偵測到 {len(image_paths)} 張圖片，完成編碼並加入推論。")
    else:
        log_callback("[系統] 未偵測到圖片，將以純文字模式進行推論。")

    ans_a = ""
    ans_b = ""
    ans_c = ""
    ans_judge = ""
    consensus = {"A": None, "B": None, "C": None}

    def _fmt_agree(v: Optional[bool]) -> str:
        """將共識布林值格式化為中文顯示字串。

        參數:
            v: True=同意, False=不同意, None=未表態

        回傳:
            str: "同意" / "不同意" / "未表態"
        """
        return "同意" if v is True else ("不同意" if v is False else "未表態")

    try:
        if meeting_mode == "single":
            payload_judge = build_payload(sys_judge, topic, image_data_uris)
            ans_judge = await stream_agent_response(get_client(judge), judge, payload_judge, "🟣 裁判長", log_callback)

        elif meeting_mode == "debate":
            max_rounds = int(max_rounds) if max_rounds else DEFAULT_MAX_ROUNDS
            if max_rounds < 1:
                max_rounds = 1

            # Round 1：三專家同題作答
            payload_a = build_payload(sys_a, topic, image_data_uris)
            ans_a = await stream_agent_response(get_client(model_a), model_a, payload_a, "🟢 專家 A", log_callback)
            if _cancelled(): raise asyncio.CancelledError()

            payload_b = build_payload(sys_b, topic, image_data_uris)
            ans_b = await stream_agent_response(get_client(model_b), model_b, payload_b, "🔵 專家 B", log_callback)
            if _cancelled(): raise asyncio.CancelledError()

            payload_c = build_payload(sys_c, topic, image_data_uris)
            ans_c = await stream_agent_response(get_client(model_c), model_c, payload_c, "🟠 專家 C", log_callback)
            if _cancelled(): raise asyncio.CancelledError()

            # Round 2..N：共識確認（必要時可修正）
            for round_no in range(2, max_rounds + 1):
                log_callback(f"\n[系統] 進入第 {round_no} 輪共識確認（若需可修正內容）。")

                prompt_a = (
                    f"【使用者需求】\n{topic}\n\n"
                    f"【其他專家意見】\n"
                    f"- 專家 B：\n{ans_b}\n\n"
                    f"- 專家 C：\n{ans_c}\n\n"
                    "請輸出你的『修正版最終答案』（如不需修正可簡短重申），並在最後一行輸出：共識：同意 或 共識：不同意。"
                )
                ans_a = await stream_agent_response(get_client(model_a), model_a, build_payload(sys_a, prompt_a, image_data_uris), "🟢 專家 A", log_callback)
                if _cancelled(): raise asyncio.CancelledError()

                prompt_b = (
                    f"【使用者需求】\n{topic}\n\n"
                    f"【其他專家意見】\n"
                    f"- 專家 A：\n{ans_a}\n\n"
                    f"- 專家 C：\n{ans_c}\n\n"
                    "請輸出你的『修正版最終答案』（如不需修正可簡短重申），並在最後一行輸出：共識：同意 或 共識：不同意。"
                )
                ans_b = await stream_agent_response(get_client(model_b), model_b, build_payload(sys_b, prompt_b, image_data_uris), "🔵 專家 B", log_callback)
                if _cancelled(): raise asyncio.CancelledError()

                prompt_c = (
                    f"【使用者需求】\n{topic}\n\n"
                    f"【其他專家意見】\n"
                    f"- 專家 A：\n{ans_a}\n\n"
                    f"- 專家 B：\n{ans_b}\n\n"
                    "請輸出你的『修正版最終答案』（如不需修正可簡短重申），並在最後一行輸出：共識：同意 或 共識：不同意。"
                )
                ans_c = await stream_agent_response(get_client(model_c), model_c, build_payload(sys_c, prompt_c, image_data_uris), "🟠 專家 C", log_callback)

                consensus = {"A": parse_agreement(ans_a), "B": parse_agreement(ans_b), "C": parse_agreement(ans_c)}
                if all(v is True for v in consensus.values()):
                    log_callback("\n[系統] 三位專家已達成共識，交由裁判長生成最終文件。")
                    break

            consensus = {"A": parse_agreement(ans_a), "B": parse_agreement(ans_b), "C": parse_agreement(ans_c)}
            consensus_line = f"A={_fmt_agree(consensus['A'])}, B={_fmt_agree(consensus['B'])}, C={_fmt_agree(consensus['C'])}"

            judge_combined_topic = (
                f"【使用者需求】：\n{topic}\n\n"
                f"【專家 A】：\n{ans_a}\n\n"
                f"【專家 B】：\n{ans_b}\n\n"
                f"【專家 C】：\n{ans_c}\n\n"
                f"【共識狀態】：{consensus_line}\n\n"
                "請進行仲裁並產出最終文件。"
            )
            payload_judge = build_payload(sys_judge, judge_combined_topic, image_data_uris)
            ans_judge = await stream_agent_response(get_client(judge), judge, payload_judge, "🟣 裁判長", log_callback)

        else:
            # 預設：接力模式（維持現況）
            payload_a = build_payload(sys_a, topic, image_data_uris)
            ans_a = await stream_agent_response(get_client(model_a), model_a, payload_a, "🟢 專家 A", log_callback)
            if _cancelled(): raise asyncio.CancelledError()

            payload_b = build_payload(sys_b, topic, image_data_uris)
            ans_b = await stream_agent_response(get_client(model_b), model_b, payload_b, "🔵 專家 B", log_callback)
            if _cancelled(): raise asyncio.CancelledError()

            judge_combined_topic = (
                f"【使用者需求】：\n{topic}\n\n"
                f"【專家 A】：\n{ans_a}\n\n"
                f"【專家 B】：\n{ans_b}\n\n"
                "請進行仲裁。"
            )
            payload_judge = build_payload(sys_judge, judge_combined_topic, image_data_uris)
            ans_judge = await stream_agent_response(get_client(judge), judge, payload_judge, "🟣 裁判長", log_callback)

        # I/O 寫入：產生帶有時間戳記的不重複檔名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"AI_Board_Report_{timestamp}.md"

        with open(output_file, "w", encoding="utf-8") as md_file:
            md_file.write(f"# 🎤 露娜的 AI 董事會：多模型聯合仲裁報告 ({timestamp})\n\n")
            md_file.write(f"- 會議模式：{mode_label}\n\n")

            if image_paths:
                md_file.write("## 🖼️ 【參考圖片】\n\n")
                for path in image_paths:
                    uri_path = os.path.abspath(path).replace("\\", "/")
                    md_file.write(f"![圖片](file:///{uri_path})\n\n")
                md_file.write("---\n\n")

            md_file.write("## 📝 【使用者需求】\n\n")
            md_file.write(f"> {topic.strip()}\n\n---\n\n")

            if meeting_mode in ("relay", "debate"):
                md_file.write(f"## 🟢 【專家 A ({model_a})】論點\n\n{ans_a}\n\n---\n\n")
                md_file.write(f"## 🔵 【專家 B ({model_b})】論點\n\n{ans_b}\n\n---\n\n")

            if meeting_mode == "debate":
                md_file.write(f"## 🟠 【專家 C ({model_c})】論點\n\n{ans_c}\n\n---\n\n")
                md_file.write("## ✅【共識狀態】\n\n")
                md_file.write(f"- 專家 A：{_fmt_agree(consensus['A'])}\n")
                md_file.write(f"- 專家 B：{_fmt_agree(consensus['B'])}\n")
                md_file.write(f"- 專家 C：{_fmt_agree(consensus['C'])}\n\n---\n\n")

            md_file.write(f"## 🟣 【最終裁決 ({judge})】\n\n{ans_judge}\n")

        log_callback(f"\n✅ 完整報告已成功寫入，並保留舊紀錄：{output_file}")

    except asyncio.CancelledError:
        log_callback("\n⛔ 會議已取消。")
    except Exception as e:
        log_callback(f"\n❌ 會議發生錯誤：{e}")
        log_callback(f"\n[除錯] 詳細錯誤追蹤:\n{traceback.format_exc()}")
    finally:
        log_callback("\n--- 會議結束 ---")

# ==========================================
# [GUI 區塊] Tkinter 介面設計
# ==========================================

class BoardMeetingApp:
    """露娜的 AI 董事會 — Tkinter GUI 主應用程式。

    負責：
        - 建立並佈局所有 GUI 元件（模式選擇、圖片、模型、提示詞、日誌）
        - 讀取 / 寫入設定檔（board_meeting_config.json）
        - 啟動背景執行緒執行非同步會議流程
        - 提供中途取消機制

    架構：
        上方容器（top_container）：會議模式、圖片選擇、模型陣容
        中間容器（mid_container）：系統提示詞折疊區、使用者需求輸入
        下方容器（bot_container）：啟動 / 停止按鈕、即時日誌
    """

    def __init__(self, root):
        """初始化 GUI 介面並載入上一次的設定。

        參數:
            root: Tkinter 根視窗（tk.Tk 實例）
        """
        self.root = root
        self.root.title("露娜的 AI 董事會控制台 (v2.8.11 LM Studio 支援版)")
        self.root.geometry("900x950") 
        
        # 容器規劃：上 (設定)、中 (提示詞與輸入)、下 (執行與日誌)
        self.top_container = tk.Frame(root)
        self.top_container.pack(fill="x", padx=10, pady=5)
        self.mid_container = tk.Frame(root)
        self.mid_container.pack(fill="both", expand=True, padx=10, pady=5)
        self.bot_container = tk.Frame(root)
        self.bot_container.pack(fill="both", expand=True, padx=10, pady=5)

        # 啟動時掃描提示詞模板（供下拉選擇）
        self.prompt_templates = list_prompt_templates()
        self.prompt_template_options = ["(不使用模板)"] + list(self.prompt_templates.keys())

        # === 頂部容器 (會議模式 / 圖片 / 模型) ===
        mode_frame = tk.LabelFrame(self.top_container, text="步驟 0：選擇會議模式", padx=10, pady=5)
        mode_frame.pack(fill="x", pady=2)

        tk.Label(mode_frame, text="會議模式：").pack(side="left")
        self.cb_meeting_mode = ttk.Combobox(mode_frame, values=list(MEETING_MODE_MAPPING.keys()), width=30, state="readonly")
        self.cb_meeting_mode.pack(side="left", padx=5)
        self.cb_meeting_mode.bind("<<ComboboxSelected>>", self.on_meeting_mode_change)

        tk.Label(mode_frame, text="討論輪數上限：").pack(side="left")
        self.var_max_rounds = tk.IntVar(value=DEFAULT_MAX_ROUNDS)
        self.spin_max_rounds = tk.Spinbox(mode_frame, from_=1, to=6, width=5, textvariable=self.var_max_rounds)
        self.spin_max_rounds.pack(side="left", padx=5)

        folder_frame = tk.LabelFrame(self.top_container, text="步驟 1：選擇參考圖片（資料夾/檔案）", padx=10, pady=5)
        folder_frame.pack(fill="x", pady=2)

        self.dir_path = tk.StringVar()
        self.image_files: List[str] = []
        self.image_status = tk.StringVar(value="(未選擇圖片)")

        tk.Entry(folder_frame, textvariable=self.dir_path, width=45, state="readonly").pack(side="left", padx=5)
        tk.Button(folder_frame, text="選資料夾...", command=self.browse_folder).pack(side="left", padx=2)
        tk.Button(folder_frame, text="選圖片檔...", command=self.browse_images).pack(side="left", padx=2)
        tk.Button(folder_frame, text="❌ 清除", command=self.clear_folder, fg="red").pack(side="left", padx=2)
        tk.Label(folder_frame, textvariable=self.image_status, fg="#555").pack(side="left", padx=6)

        model_frame = tk.LabelFrame(self.top_container, text="步驟 2：選擇 AI 模型陣容", padx=10, pady=5)
        model_frame.pack(fill="x", pady=2)
        model_options = list(MODEL_MAPPING.keys())
        
        tk.Label(model_frame, text="🟢 專家 A 模型：").grid(row=0, column=0, sticky="e", pady=2)
        self.cb_expert_a = ttk.Combobox(model_frame, values=model_options, width=45, state="readonly")
        self.cb_expert_a.grid(row=0, column=1)

        tk.Label(model_frame, text="🔵 專家 B 模型：").grid(row=1, column=0, sticky="e", pady=2)
        self.cb_expert_b = ttk.Combobox(model_frame, values=model_options, width=45, state="readonly")
        self.cb_expert_b.grid(row=1, column=1)

        tk.Label(model_frame, text="🟠 專家 C 模型：").grid(row=2, column=0, sticky="e", pady=2)
        self.cb_expert_c = ttk.Combobox(model_frame, values=model_options, width=45, state="readonly")
        self.cb_expert_c.grid(row=2, column=1)

        tk.Label(model_frame, text="🟣 裁判長 模型：").grid(row=3, column=0, sticky="e", pady=2)
        self.cb_judge = ttk.Combobox(model_frame, values=model_options, width=45, state="readonly")
        self.cb_judge.grid(row=3, column=1)

        # LM Studio 控制列
        lm_btn_frame = tk.Frame(model_frame)
        lm_btn_frame.grid(row=4, column=0, columnspan=2, sticky="w", pady=3)
        tk.Label(lm_btn_frame, text="LM Studio URL:").pack(side="left")
        self.var_lm_url = tk.StringVar(value=LM_STUDIO_URL)
        tk.Entry(lm_btn_frame, textvariable=self.var_lm_url, width=30).pack(side="left", padx=3)
        tk.Button(lm_btn_frame, text="🔄 重新整理本地模型", command=self.refresh_local_models).pack(side="left", padx=3)
        tk.Button(lm_btn_frame, text="🔌 測試 LM Studio 連線", command=self.test_lm_studio).pack(side="left", padx=3)
        self.lm_status_label = tk.Label(lm_btn_frame, text="", fg="#555")
        self.lm_status_label.pack(side="left", padx=5)

        # === 中部容器 (系統提示詞折疊區 & 使用者需求) ===
        self.btn_toggle = tk.Button(self.mid_container, text="⚙️ 展開系統提示詞設定 (System Prompts)", command=self.toggle_prompts, bg="#f0f0f0")
        self.btn_toggle.pack(fill="x", pady=5)
        
        self.prompt_frame = tk.Frame(self.mid_container)
        self.prompt_widgets = {}

        a_header = tk.Frame(self.prompt_frame)
        tk.Label(a_header, text="專家 A 提示詞:").pack(side="left")
        self.cb_tpl_a = ttk.Combobox(a_header, values=self.prompt_template_options, width=55, state="readonly")
        self.cb_tpl_a.set("(不使用模板)")
        self.cb_tpl_a.pack(side="left", padx=5)
        tk.Button(a_header, text="套用模板", command=lambda: self.apply_prompt_template("a")).pack(side="left")
        a_header.pack(fill="x")
        self.text_sys_a = scrolledtext.ScrolledText(self.prompt_frame, height=4, width=80)
        self.text_sys_a.pack(fill="x", pady=2)
        self.prompt_widgets["a"] = (self.cb_tpl_a, self.text_sys_a)

        b_header = tk.Frame(self.prompt_frame)
        tk.Label(b_header, text="專家 B 提示詞:").pack(side="left")
        self.cb_tpl_b = ttk.Combobox(b_header, values=self.prompt_template_options, width=55, state="readonly")
        self.cb_tpl_b.set("(不使用模板)")
        self.cb_tpl_b.pack(side="left", padx=5)
        tk.Button(b_header, text="套用模板", command=lambda: self.apply_prompt_template("b")).pack(side="left")
        b_header.pack(fill="x")
        self.text_sys_b = scrolledtext.ScrolledText(self.prompt_frame, height=4, width=80)
        self.text_sys_b.pack(fill="x", pady=2)
        self.prompt_widgets["b"] = (self.cb_tpl_b, self.text_sys_b)

        c_header = tk.Frame(self.prompt_frame)
        tk.Label(c_header, text="專家 C 提示詞:").pack(side="left")
        self.cb_tpl_c = ttk.Combobox(c_header, values=self.prompt_template_options, width=55, state="readonly")
        self.cb_tpl_c.set("(不使用模板)")
        self.cb_tpl_c.pack(side="left", padx=5)
        tk.Button(c_header, text="套用模板", command=lambda: self.apply_prompt_template("c")).pack(side="left")
        c_header.pack(fill="x")
        self.text_sys_c = scrolledtext.ScrolledText(self.prompt_frame, height=4, width=80)
        self.text_sys_c.pack(fill="x", pady=2)
        self.prompt_widgets["c"] = (self.cb_tpl_c, self.text_sys_c)

        j_header = tk.Frame(self.prompt_frame)
        tk.Label(j_header, text="裁判長 提示詞:").pack(side="left")
        self.cb_tpl_judge = ttk.Combobox(j_header, values=self.prompt_template_options, width=55, state="readonly")
        self.cb_tpl_judge.set("(不使用模板)")
        self.cb_tpl_judge.pack(side="left", padx=5)
        tk.Button(j_header, text="套用模板", command=lambda: self.apply_prompt_template("judge")).pack(side="left")
        j_header.pack(fill="x")
        self.text_sys_judge = scrolledtext.ScrolledText(self.prompt_frame, height=4, width=80)
        self.text_sys_judge.pack(fill="x", pady=2)
        self.prompt_widgets["judge"] = (self.cb_tpl_judge, self.text_sys_judge)

        topic_frame = tk.LabelFrame(self.mid_container, text="步驟 3：輸入本次會議需求 (User Topic)", padx=10, pady=5)
        topic_frame.pack(fill="both", expand=True, pady=5)
        self.text_topic = scrolledtext.ScrolledText(topic_frame, height=6, width=80)
        self.text_topic.pack(fill="both", expand=True)

        # === 底部容器 (按鈕與日誌) ===
        self.btn_start = tk.Button(self.bot_container, text="🚀 啟動會議", command=self.start_meeting, bg="#4CAF50", fg="white", font=("Arial", 12, "bold"))
        self.btn_start.pack(side="left", padx=5, pady=5)
        self.btn_cancel = tk.Button(self.bot_container, text="⛔ 停止", command=self.cancel_meeting, bg="#f44336", fg="white", font=("Arial", 12, "bold"), state="disabled")
        self.btn_cancel.pack(side="left", padx=5, pady=5)
        self._cancel_event = threading.Event()

        log_frame = tk.LabelFrame(self.bot_container, text="執行日誌 (Log)", padx=10, pady=5)
        log_frame.pack(fill="both", expand=True)
        self.text_log = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=10)
        self.text_log.pack(fill="both", expand=True)

        # 介面初始化完成後，讀取上一次的設定檔
        self.load_config()
        self.on_meeting_mode_change()
        self.update_image_status()

    def toggle_prompts(self):
        """切換系統提示詞面板的展開與隱藏狀態。

        功能：
            若面板已展開 → 隱藏面板，按鈕文字改為「展開」
            若面板已隱藏 → 展開面板，按鈕文字改為「隱藏」
        """
        if self.prompt_frame.winfo_ismapped():
            self.prompt_frame.pack_forget()
            self.btn_toggle.config(text="⚙️ 展開系統提示詞設定 (System Prompts)")
        else:
            self.prompt_frame.pack(fill="x", after=self.btn_toggle)
            self.btn_toggle.config(text="⚙️ 隱藏系統提示詞設定 (System Prompts)")

    def browse_folder(self):
        """呼叫作業系統的資料夾選擇對話框，設定圖片掃描目錄。

        說明:
            選擇資料夾後會清空多選圖片檔案清單，
            因為資料夾掃描與多選檔案為互斥的圖片來源。
        """
        folder = filedialog.askdirectory()
        if folder:
            self.dir_path.set(folder)
            self.image_files = []
            self.update_image_status()

    def browse_images(self):
        """開啟多選圖片檔案對話框（可一次選取多張圖片）。

        支援的檔案類型：.jpg、.jpeg、.png、.webp
        選取後會清空資料夾路徑，因為多選圖片優先於資料夾掃描。
        """
        files = filedialog.askopenfilenames(
            title="選擇圖片檔（可多選）",
            filetypes=[("Images", "*.jpg;*.jpeg;*.png;*.webp"), ("All files", "*.*")],
        )
        if files:
            self.image_files = list(files)
            self.dir_path.set("")
            self.update_image_status()

    def clear_folder(self):
        """清除所有圖片來源（資料夾與多選檔案都會清空）。

        清除後下次執行會議將以純文字模式進行推論。
        """
        self.dir_path.set("")
        self.image_files = []
        self.update_image_status()
        self.log("[系統] 已清除圖片來源，下次執行將使用純文字推論模式。")

    def update_image_status(self):
        """更新圖片來源狀態標籤，反映目前的選取狀態。

        顯示格式：
            - 已選 N 張圖片（多選模式）
            - 使用資料夾掃描（資料夾模式）
            - (未選擇圖片)（無任何來源）
        """
        if self.image_files:
            self.image_status.set(f"已選 {len(self.image_files)} 張圖片")
        elif self.dir_path.get():
            self.image_status.set("使用資料夾掃描")
        else:
            self.image_status.set("(未選擇圖片)")

    def on_meeting_mode_change(self, event=None):
        """當使用者切換會議模式時，自動啟用或停用相關 GUI 元件。

        參數:
            event: Tkinter ComboboxSelected 事件（可選，直接呼叫時為 None）

        行為：
            - 單模型模式：停用所有專家欄位與提示詞
            - 接力模式：啟用 A/B，停用 C
            - 討論共識模式：啟用 A/B/C 與輪數設定
        """
        mode_id = MEETING_MODE_MAPPING.get(self.cb_meeting_mode.get(), DEFAULT_MEETING_MODE)
        if mode_id == "debate":
            for w in (self.cb_expert_a, self.cb_expert_b, self.cb_expert_c):
                w.config(state="readonly")
            for w in (self.text_sys_a, self.text_sys_b, self.text_sys_c):
                w.config(state="normal")
            self.spin_max_rounds.config(state="normal")
        elif mode_id == "single":
            for w in (self.cb_expert_a, self.cb_expert_b, self.cb_expert_c):
                w.config(state="disabled")
            for w in (self.text_sys_a, self.text_sys_b, self.text_sys_c):
                w.config(state="disabled")
            self.spin_max_rounds.config(state="disabled")
        else:  # relay
            self.cb_expert_a.config(state="readonly")
            self.cb_expert_b.config(state="readonly")
            self.cb_expert_c.config(state="disabled")
            self.text_sys_a.config(state="normal")
            self.text_sys_b.config(state="normal")
            self.text_sys_c.config(state="disabled")
            self.spin_max_rounds.config(state="disabled")

    def _set_text(self, widget, text: str):
        """安全地設定 ScrolledText 元件的文字內容。

        參數:
            widget: 目標 ScrolledText 元件
            text: 要設定的文字內容

        說明:
            會暫時將元件狀態設為 normal 以允許寫入，
            寫入完成後恢復原始狀態（例如 disabled）。
        """
        prev = widget.cget("state") if hasattr(widget, "cget") else "normal"
        try:
            widget.config(state="normal")
        except Exception:
            pass
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text or "")
        try:
            widget.config(state=prev)
        except Exception:
            pass

    def apply_prompt_template(self, role: str):
        """將選定的模板檔案內容套用到指定角色的系統提示詞欄位。

        參數:
            role: 角色識別字串（"a" / "b" / "c" / "judge"）

        流程:
            1. 從對應的下拉選單取得選定的模板名稱
            2. 讀取模板檔案並擷取系統提示詞正文
            3. 彈出確認對話框
            4. 覆蓋目標文字欄位的內容
        """
        pair = self.prompt_widgets.get(role)
        if not pair:
            return

        cb, text_widget = pair
        selected = cb.get()
        if not selected or selected == "(不使用模板)":
            return

        path = self.prompt_templates.get(selected)
        if not path or not os.path.exists(path):
            messagebox.showwarning("找不到檔案", f"找不到模板檔案：{selected}")
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            content = extract_system_prompt_from_template(raw)
        except Exception as e:
            messagebox.showerror("讀取失敗", f"讀取模板失敗：{e}")
            return

        if not messagebox.askyesno("確認套用", f"套用模板會覆蓋目前的提示詞內容：\n\n{selected}\n\n是否繼續？"):
            return

        self._set_text(text_widget, content)
        self.log(f"[系統] 已套用提示詞模板：{selected}")

    def cancel_meeting(self):
        """發送取消訊號，要求在當前步驟完成後停止會議。

        說明:
            透過 threading.Event 通知背景執行緒中的 _cancelled() 檢查點。
            不會立即中斷正在進行的模型推論，而是在下一個 _cancelled() 檢查點安全停止。
        """
        self._cancel_event.set()
        self.log("[系統] 取消訊號已送出，等待當前步驟完成後停止...")
        self.btn_cancel.config(state="disabled")

    def log(self, message, newline=True):
        """執行緒安全的日誌輸出函式（支援打字機效果的不換行拼接）。

        參數:
            message: 要輸出的文字
            newline: 是否在文字後加換行符（預設 True）

        說明:
            使用 root.after(0, ...) 將 GUI 更新操作排程到主執行緒，
            確保從背景執行緒呼叫時不會觸發 Tkinter 的執行緒安全問題。
        """
        self.root.after(0, self._append_log, message, newline)
            
    def _append_log(self, message, newline):
        """實際執行日誌文字插入的內部方法（必須在 GUI 主執行緒中執行）。

        參數:
            message: 要插入的文字
            newline: 是否附加換行符
        """
        text_to_insert = message + ("\n" if newline else "")
        self.text_log.insert(tk.END, text_to_insert)
        self.text_log.see(tk.END)

    def load_config(self):
        """讀取 board_meeting_config.json 並還原 GUI 介面狀態。

        行為：
            - 若設定檔存在：讀取並還原所有欄位（模式、模型、URL、提示詞、主題等）
            - 若設定檔不存在：使用程式內建的預設值初始化介面
            - 若設定檔損壞：靜默忽略錯誤，由預設值填充

        副作用：
            - 可能更新 LM_STUDIO_URL 全域變數
        """
        global LM_STUDIO_URL
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
                cfg_lm_url = str(config.get("lm_studio_url", LM_STUDIO_URL)).strip().rstrip("/")
                LM_STUDIO_URL = normalize_lm_studio_url(cfg_lm_url) or LM_STUDIO_URL
                self.var_lm_url.set(LM_STUDIO_URL)

                self.dir_path.set(config.get("dir_path", ""))
                image_files = config.get("image_files", [])
                self.image_files = image_files if isinstance(image_files, list) else []

                self.cb_meeting_mode.set(config.get("meeting_mode", "接力模式（A→B→裁判長）"))
                try:
                    self.var_max_rounds.set(int(config.get("max_rounds", DEFAULT_MAX_ROUNDS)))
                except Exception:
                    self.var_max_rounds.set(DEFAULT_MAX_ROUNDS)

                model_options = list(MODEL_MAPPING.keys())
                first = model_options[0] if model_options else ""
                self.cb_expert_a.set(config.get("model_a", first))
                self.cb_expert_b.set(config.get("model_b", first))
                self.cb_expert_c.set(config.get("model_c", first))
                self.cb_judge.set(config.get("model_judge", first))

                self._set_text(self.text_sys_a, config.get("sys_a", DEFAULT_PROMPT_A))
                self._set_text(self.text_sys_b, config.get("sys_b", DEFAULT_PROMPT_B))
                self._set_text(self.text_sys_c, config.get("sys_c", DEFAULT_PROMPT_C))
                self._set_text(self.text_sys_judge, config.get("sys_judge", DEFAULT_PROMPT_JUDGE))

                self._set_text(self.text_topic, config.get("topic", DEFAULT_TOPIC))

                # 套用記錄的模板下拉（不會自動覆蓋文字，僅恢復選項狀態）
                for role, key, cb in [
                    ("a", "tpl_a", self.cb_tpl_a),
                    ("b", "tpl_b", self.cb_tpl_b),
                    ("c", "tpl_c", self.cb_tpl_c),
                    ("judge", "tpl_judge", self.cb_tpl_judge),
                ]:
                    val = config.get(key, "(不使用模板)")
                    if val in self.prompt_template_options:
                        cb.set(val)
                    else:
                        cb.set("(不使用模板)")

            else:
                self.dir_path.set("")
                self.image_files = []
                self.cb_meeting_mode.set("接力模式（A→B→裁判長）")
                self.var_max_rounds.set(DEFAULT_MAX_ROUNDS)

                model_options = list(MODEL_MAPPING.keys())
                first = model_options[0] if model_options else ""
                self.cb_expert_a.set(first)
                self.cb_expert_b.set(first)
                self.cb_expert_c.set(first)
                self.cb_judge.set(first)

                self._set_text(self.text_sys_a, DEFAULT_PROMPT_A)
                self._set_text(self.text_sys_b, DEFAULT_PROMPT_B)
                self._set_text(self.text_sys_c, DEFAULT_PROMPT_C)
                self._set_text(self.text_sys_judge, DEFAULT_PROMPT_JUDGE)
                self._set_text(self.text_topic, DEFAULT_TOPIC)

                self.cb_tpl_a.set("(不使用模板)")
                self.cb_tpl_b.set("(不使用模板)")
                self.cb_tpl_c.set("(不使用模板)")
                self.cb_tpl_judge.set("(不使用模板)")

        except Exception as e:
            print(f"讀取設定檔失敗: {e}")

    def refresh_local_models(self):
        """重新掃描本地 .gguf 模型檔並更新所有模型下拉選單。

        流程：
            1. 同步更新 LM_STUDIO_URL（從 GUI 欄位讀取最新值）
            2. 重新呼叫 load_model_config() 掃描所有路徑
            3. 更新四個角色的模型下拉選單選項
            4. 啟動背景執行緒查詢 LM Studio SDK / API，新增已載入的模型

        說明：
            背景執行緒優先嘗試 lmstudio SDK，失敗時退回 HTTP API。
            新增的模型會以 [本地-SDK] 或 [本地-API] 前綴顯示。
        """
        global LM_STUDIO_URL
        # 從 GUI 取得最新 URL（允許使用者在介面上修改）
        LM_STUDIO_URL = normalize_lm_studio_url(self.var_lm_url.get().rstrip("/") or LM_STUDIO_URL)
        self.var_lm_url.set(LM_STUDIO_URL)

        load_model_config()
        model_options = list(MODEL_MAPPING.keys())
        for cb in (self.cb_expert_a, self.cb_expert_b, self.cb_expert_c, self.cb_judge):
            current = cb.get()
            cb['values'] = model_options
            if current in model_options:
                cb.set(current)
            elif model_options:
                cb.set(model_options[0])

        local_count = sum(1 for v in MODEL_MAPPING.values() if str(v).startswith("local:"))
        self.lm_status_label.config(text=f"找到 {local_count} 個本地模型", fg="green" if local_count else "#555")
        self.log(f"[系統] 模型清單已重新整理（{len(MODEL_MAPPING)} 個模型，其中 {local_count} 個本地模型）")

        # 同步查詢 API，新增目前已載入的模型
        def _fetch_api_models():
            """背景執行緒：查詢 LM Studio SDK / HTTP API 並新增已載入的模型到下拉選單"""
            global LM_STUDIO_URL
            import urllib.request
            # 嘗試透過 SDK 取得已載入模型（不需要開啟 Local Server）
            if HAS_LMS_SDK:
                try:
                    with lms.Client() as client:
                        loaded = client.llm.list_loaded()
                        if loaded:
                            added = []
                            for m in loaded:
                                mid = getattr(m, "identifier", None) or getattr(m, "path", None) or str(m)
                                display = f"[本地-SDK] {mid}"
                                if display not in MODEL_MAPPING:
                                    MODEL_MAPPING[display] = f"local:{mid}"
                                    added.append(display)
                            if added:
                                new_opts = list(MODEL_MAPPING.keys())
                                def _update_sdk():
                                    """主執行緒回呼：更新下拉選單（SDK 模型）"""
                                    for cb in (self.cb_expert_a, self.cb_expert_b, self.cb_expert_c, self.cb_judge):
                                        cur = cb.get()
                                        cb['values'] = new_opts
                                        if cur in new_opts:
                                            cb.set(cur)
                                    self.log(f"[系統] 從 LM Studio SDK 新增 {len(added)} 個已載入模型：{', '.join(added)}")
                                self.root.after(0, _update_sdk)
                            return
                except Exception:
                    pass  # SDK 失敗時回退到 HTTP
            # HTTP 備用方式（try/except 在 for 內部，確保所有候選 URL 都會被嘗試）
            for base_url in get_lm_studio_url_candidates(LM_STUDIO_URL):
                try:
                    url = base_url.rstrip("/") + "/models"
                    req = urllib.request.Request(url, method='GET')
                    with urlopen_without_proxy(req, timeout=3) as resp:
                        LM_STUDIO_URL = base_url
                        data = json.loads(resp.read().decode('utf-8'))
                        model_ids = extract_model_ids(data)
                        if model_ids:
                            added = []
                            for mid in model_ids:
                                display = f"[本地-API] {mid}"
                                if display not in MODEL_MAPPING:
                                    MODEL_MAPPING[display] = f"local:{mid}"
                                    added.append(display)
                            if added:
                                new_opts = list(MODEL_MAPPING.keys())
                                def _update():
                                    """主執行緒回呼：更新下拉選單（HTTP API 模型）"""
                                    self.var_lm_url.set(LM_STUDIO_URL)
                                    for cb in (self.cb_expert_a, self.cb_expert_b, self.cb_expert_c, self.cb_judge):
                                        cur = cb.get()
                                        cb['values'] = new_opts
                                        if cur in new_opts:
                                            cb.set(cur)
                                    self.log(f"[系統] 從 LM Studio API 新增 {len(added)} 個已載入模型：{', '.join(added)}")
                                self.root.after(0, _update)
                        return  # 成功連線則結束
                except Exception:
                    continue  # 此候選 URL 失敗，嘗試下一個
        threading.Thread(target=_fetch_api_models, daemon=True).start()

    def test_lm_studio(self):
        """測試 LM Studio 伺服器連線（SDK 與 HTTP 雙重測試）。

        流程：
            1. 從 GUI 欄位讀取並正規化 LM Studio URL
            2. 優先嘗試 lmstudio SDK 連線
            3. SDK 失敗時退回 HTTP 測試（依序嘗試所有候選 URL）
            4. 在狀態標籤顯示結果（✅ 成功 / ⚠️ 已連線但無模型 / ❌ 失敗）

        說明：
            測試在背景執行緒中執行，避免阻塞 GUI。
        """
        global LM_STUDIO_URL
        import urllib.request
        # 從 GUI 欄位讀取最新的 URL
        LM_STUDIO_URL = normalize_lm_studio_url(self.var_lm_url.get().rstrip("/") or LM_STUDIO_URL)
        self.var_lm_url.set(LM_STUDIO_URL)
        self.lm_status_label.config(text="測試中...", fg="orange")
        self.root.update_idletasks()

        def _test():
            """背景執行緒：依序嘗試 SDK 與 HTTP 連線測試並更新狀態標籤"""
            # 優先嘗試 SDK 連線測試（不需要開啟 Local Server）
            if HAS_LMS_SDK:
                try:
                    with lms.Client() as client:
                        loaded = client.llm.list_loaded()
                        if loaded:
                            names = ", ".join(
                                getattr(m, "identifier", None) or str(m) for m in loaded[:3]
                            )
                            msg = f"✅ [SDK] 已連線｜載入模型: {names}"
                        else:
                            msg = "⚠️ [SDK] 已連線但無載入模型"
                        self.root.after(0, lambda m=msg: self.lm_status_label.config(
                            text=m, fg="green" if "✅" in m else "orange"))
                        return
                except Exception:
                    pass  # SDK 失敗時回退到 HTTP 測試
            # HTTP 備用測試（try/except 在 for 內部，確保所有候選 URL 都會被嘗試）
            last_err = None
            for base_url in get_lm_studio_url_candidates(LM_STUDIO_URL):
                try:
                    req = urllib.request.Request(f"{base_url}/models", method='GET')
                    with urlopen_without_proxy(req, timeout=3) as resp:
                        data = json.loads(resp.read().decode('utf-8'))
                        model_ids = extract_model_ids(data)
                        if model_ids:
                            names = ", ".join(model_ids[:3])
                            msg = f"✅ [HTTP] 已連線｜載入模型: {names}"
                            self.root.after(0, lambda m=msg, u=base_url: (self.var_lm_url.set(u), self.lm_status_label.config(text=m, fg="green")))
                        else:
                            self.root.after(0, lambda u=base_url: (self.var_lm_url.set(u), self.lm_status_label.config(text="⚠️ [HTTP] 已連線但無載入模型", fg="orange")))
                        return
                except Exception as e:
                    last_err = str(e)
                    continue  # 此候選 URL 失敗，嘗試下一個
            # 所有候選 URL 皆失敗
            err = last_err or "所有連線方式均失敗"
            self.root.after(0, lambda m=err: self.lm_status_label.config(text=f"❌ 連線失敗: {m}", fg="red"))

        threading.Thread(target=_test, daemon=True).start()

    def save_config(self):
        """將當前 GUI 介面的所有設定寫入 board_meeting_config.json。

        寫入的欄位包含：
            - 圖片來源（資料夾路徑、多選檔案清單）
            - LM Studio URL
            - 會議模式與討論輪數
            - 四個角色的模型選擇與模板選項
            - 四個角色的系統提示詞
            - 使用者主題

        說明:
            此方法在每次啟動會議前自動呼叫，確保設定不會遺失。
            包含清空後的路徑狀態也會被正確儲存。
        """
        config = {
            "dir_path": self.dir_path.get(),
            "image_files": self.image_files,
            "lm_studio_url": normalize_lm_studio_url(self.var_lm_url.get().strip().rstrip("/") or LM_STUDIO_URL),
            "meeting_mode": self.cb_meeting_mode.get(),
            "max_rounds": int(self.var_max_rounds.get()),
            "model_a": self.cb_expert_a.get(),
            "model_b": self.cb_expert_b.get(),
            "model_c": self.cb_expert_c.get(),
            "model_judge": self.cb_judge.get(),
            "tpl_a": self.cb_tpl_a.get(),
            "tpl_b": self.cb_tpl_b.get(),
            "tpl_c": self.cb_tpl_c.get(),
            "tpl_judge": self.cb_tpl_judge.get(),
            "sys_a": self.text_sys_a.get("1.0", tk.END).strip(),
            "sys_b": self.text_sys_b.get("1.0", tk.END).strip(),
            "sys_c": self.text_sys_c.get("1.0", tk.END).strip(),
            "sys_judge": self.text_sys_judge.get("1.0", tk.END).strip(),
            "topic": self.text_topic.get("1.0", tk.END).strip(),
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
        except Exception as e:
            self.log(f"⚠️ 儲存設定檔失敗: {e}")

    def start_meeting(self):
        """驗證輸入並啟動會議（觸發背景執行緒）。

        流程：
            1. 同步 GUI 的 LM Studio URL 到全域變數
            2. 驗證使用者需求是否為空
            3. 若無圖片，詢問是否以純文字模式繼續
            4. 自動儲存目前設定
            5. 鎖定「啟動」按鈕、啟用「停止」按鈕
            6. 開啟背景 daemon 執行緒執行 run_board_meeting

        說明:
            使用 daemon 執行緒確保 GUI 關閉時執行緒也會被終止。
        """
        global LM_STUDIO_URL
        # 啟動前同步 GUI URL 到全域變數
        LM_STUDIO_URL = normalize_lm_studio_url(self.var_lm_url.get().rstrip("/") or LM_STUDIO_URL)
        self.var_lm_url.set(LM_STUDIO_URL)

        topic = self.text_topic.get("1.0", tk.END).strip()
        if not topic:
            messagebox.showwarning("警告", "請輸入本次會議需求 (User Topic)！")
            return

        if not self.image_files and not self.dir_path.get():
            if not messagebox.askyesno("確認", "目前未選擇任何參考圖片，將以純文字模式推論。\n\n仍要啟動會議嗎？"):
                return

        meeting_mode = MEETING_MODE_MAPPING.get(self.cb_meeting_mode.get(), DEFAULT_MEETING_MODE)
        max_rounds = int(self.var_max_rounds.get()) if self.var_max_rounds.get() else DEFAULT_MAX_ROUNDS

        # 啟動前自動儲存設定
        self.save_config()
        self._cancel_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.text_log.delete(1.0, tk.END)

        model_a = MODEL_MAPPING.get(self.cb_expert_a.get(), "gpt-5-mini")
        model_b = MODEL_MAPPING.get(self.cb_expert_b.get(), "gpt-5-mini")
        model_c = MODEL_MAPPING.get(self.cb_expert_c.get(), "gpt-5-mini")
        judge = MODEL_MAPPING.get(self.cb_judge.get(), "gpt-5.2")

        image_dir = self.dir_path.get()
        image_files = list(self.image_files)  # copy

        sys_a = self.text_sys_a.get("1.0", tk.END).strip()
        sys_b = self.text_sys_b.get("1.0", tk.END).strip()
        sys_c = self.text_sys_c.get("1.0", tk.END).strip()
        sys_judge = self.text_sys_judge.get("1.0", tk.END).strip()

        # 開啟背景執行緒，避免阻塞 GUI
        threading.Thread(
            target=self._run_async_in_thread,
            args=(
                meeting_mode,
                max_rounds,
                model_a,
                model_b,
                model_c,
                judge,
                image_dir,
                image_files,
                topic,
                sys_a,
                sys_b,
                sys_c,
                sys_judge,
            ),
            daemon=True,
        ).start()

    def _run_async_in_thread(self, *args):
        """在背景執行緒中建立新的 asyncio 事件迴圈並執行會議。

        說明:
            每次會議都建立獨立的事件迴圈，避免與 GUI 主迴圈衝突。
            執行結束後（無論成功或失敗），都會解鎖「啟動」按鈕並停用「停止」按鈕。
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_board_meeting(*args, self.log, cancel_event=self._cancel_event))
        finally:
            loop.close()
            self.root.after(0, lambda: self.btn_start.config(state="normal"))
            self.root.after(0, lambda: self.btn_cancel.config(state="disabled"))

if __name__ == "__main__":
    root = tk.Tk()
    app = BoardMeetingApp(root)
    root.mainloop()
