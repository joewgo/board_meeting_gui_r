import asyncio
import aiohttp
import sys

import base64
import os
import glob
import json
import mimetypes
import threading
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlsplit, urlunsplit

# 移除所有容易因 SDK 版本差異而報錯的強型別與 Enum 匯入
# 只保留最核心且確定存在的 CopilotClient
from copilot import CopilotClient

# 嘗試載入 LM Studio 官方 Python SDK（pip install lmstudio）
# 可用時優先使用 SDK 連線（不需要在 LM Studio 開啟「Local Server」）
try:
    import lmstudio as lms
    HAS_LMS_SDK = True
except ImportError:
    HAS_LMS_SDK = False

# Windows asyncio 相容性修正：
# Python 3.8+ 在 Windows 預設使用 ProactorEventLoop，
# 部分 aiohttp 版本在此迴圈下連線 localhost 可能不穩定。
# 強制改用 SelectorEventLoop 以提升相容性。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==========================================
# [全域設定區塊] 
# ==========================================
CONFIG_FILE = "board_meeting_config.json"  

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
    return get_lm_studio_url_candidates(raw_url)[0]


def extract_model_ids(data: Dict[str, Any]) -> List[str]:
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
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)

def load_model_config():
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
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("image/"):
        return mime
    return "image/jpeg"


def encode_images_from_paths(paths: List[str], max_images: int = 15) -> Tuple[List[str], List[str]]:
    """將圖片路徑列表轉成 data URI 列表（供 Copilot SDK 多模態輸入）。"""
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
    """掃描指定資料夾並回傳 (image_paths, image_data_uris)。"""
    if not directory or not os.path.exists(directory):
        return [], []

    valid_extensions = ("*.jpg", "*.jpeg", "*.png", "*.webp")
    image_paths: List[str] = []

    for ext in valid_extensions:
        image_paths.extend(glob.glob(os.path.join(directory, ext)))

    image_paths = sorted(image_paths)
    return encode_images_from_paths(image_paths, max_images=max_images)


def build_payload(system_prompt: str, user_prompt: str, image_data_uris: List[str] = None) -> Dict[str, Any]:
    """組裝要發送給 copilot SDK 的請求資料結構 (Payload)。"""
    payload = {"prompt": user_prompt, "system_prompt": system_prompt}

    if image_data_uris:
        payload["images"] = image_data_uris

    return payload


def extract_system_prompt_from_template(text: str) -> str:
    """若模板包含「系統提示詞正文」段落，僅取該段之後內容作為 system prompt。"""
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
    """回傳 {display_name: absolute_path}，用於 GUI 下拉選擇。"""
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
    if not text:
        return None
    if "不同意" in text:
        return False
    if "同意" in text:
        return True
    return None



class LMStudioSession:
    def __init__(self, url, model=""):
        self.url = normalize_lm_studio_url(url)
        self.url_candidates = get_lm_studio_url_candidates(self.url)
        self.model = model  # 由 LMStudioClient.create_session 傳入（已去除 local: 前綴）
        self.callbacks = []

    def on(self, callback):
        self.callbacks.append(callback)

    async def _get_current_model(self) -> str:
        """查詢 LM Studio /v1/models 取得目前載入的模型 ID"""
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
        """使用 lmstudio 官方 Python SDK 連線（不需要開啟 LM Studio Local Server）"""
        loop = asyncio.get_event_loop()

        model_id = self.model if self.model and self.model != "current_model" else None
        system_prompt = payload.get("system_prompt", "")
        user_prompt = payload.get("prompt", "")

        def _run_sdk_sync():
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
        # 若 lmstudio 官方 SDK 已安裝且無圖片，優先使用 SDK 連線
        # SDK 不需要在 LM Studio 開啟「Local Server」選項，相容性更佳
        # 注意：SDK 不支援圖片 URL，含圖片時改用 HTTP 直連
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
        class Event:
            pass
        e = Event()
        e.data = Event()
        e.data.delta_content = content
        for cb in self.callbacks:
            cb(e)

    def _trigger_error(self, msg):
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
        class Event:
            pass
        e = Event()
        e.type = Event()
        e.type.value = "session.idle"
        for cb in self.callbacks:
            cb(e)

class LMStudioClient:
    def __init__(self, base_url="http://127.0.0.1:1234/v1"):
        self.base_url = base_url.rstrip('/')

    async def create_session(self, config):
        # 取出並清理 model ID（去除 local: 前綴後交給 session 保管）
        raw_model = config.get("model", "")
        if str(raw_model).startswith("local:"):
            raw_model = raw_model[len("local:"):]
        session = LMStudioSession(self.base_url, raw_model)
        return session


async def stream_agent_response(client: CopilotClient, model_id: str, payload: dict, role_name: str, log_callback) -> str:
    """
    負責處理單一 AI 模型的即時串流輸出函式。
    使用動態屬性檢查 (Duck Typing) 來處理事件，避開 SDK 版本差異導致的 ImportError。
    """
    log_callback(f"\n{'-'*40}\n[{role_name} ({model_id}) 正在思考與作答...]\n", newline=False)
    
    # 建立支援串流的會話（相容新舊版 Copilot SDK）
    session_config = {"model": model_id, "streaming": True}
    try:
        session = await client.create_session(session_config)
    except TypeError:
        # 舊版 SDK 的 create_session() 不接受參數，改用無參數呼叫
        session = await client.create_session()
    
    done = asyncio.Event() 
    response_accumulator = [] 
    
    def handle_event(event):
        """動態事件處理器：不依賴任何強型別 Enum"""
        # 1. 嘗試捕捉文字碎片
        if hasattr(event, 'data') and hasattr(event.data, 'delta_content') and event.data.delta_content:
            chunk = event.data.delta_content
            response_accumulator.append(chunk)
            log_callback(chunk, newline=False)
            
        # 2. 判斷會議是否結束或報錯
        try:
            event_type_val = event.type.value if hasattr(event.type, 'value') else str(event.type)
            
            if event_type_val == "session.idle":
                done.set()
            elif event_type_val == "session.error":
                err_msg = event.data.message if hasattr(event, 'data') and hasattr(event.data, 'message') else "未知錯誤"
                log_callback(f"\n[錯誤] {role_name} 發生中斷: {err_msg}")
                done.set()
        except Exception:
            pass 
            
    # 註冊事件監聽器並發送請求
    session.on(handle_event)
    await session.send(payload)
    
    # 阻塞等待直到 done.set() 被觸發
    await done.wait() 
    
    log_callback("\n") # 補上最後的換行
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
    """非同步核心：控制整場會議的流程與檔案 I/O"""

    def _cancelled():
        return cancel_event is not None and cancel_event.is_set()

    mode_label = next((k for k, v in MEETING_MODE_MAPPING.items() if v == meeting_mode), meeting_mode)
    log_callback("--- 露娜的 AI 董事會 (v2.8.6) 啟動 ---")
    log_callback(f"[系統] 會議模式：{mode_label}")


    client_copilot = CopilotClient()
    client_local = LMStudioClient(LM_STUDIO_URL)
    
    def get_client(model_id):
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
    finally:
        log_callback("\n--- 會議結束 ---")

# ==========================================
# [GUI 區塊] Tkinter 介面設計
# ==========================================

class BoardMeetingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("露娜的 AI 董事會控制台 (v2.8.6 LM Studio 支援版)")
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
        """控制系統提示詞面板的展開與隱藏"""
        if self.prompt_frame.winfo_ismapped():
            self.prompt_frame.pack_forget()
            self.btn_toggle.config(text="⚙️ 展開系統提示詞設定 (System Prompts)")
        else:
            self.prompt_frame.pack(fill="x", after=self.btn_toggle)
            self.btn_toggle.config(text="⚙️ 隱藏系統提示詞設定 (System Prompts)")

    def browse_folder(self):
        """呼叫作業系統的資料夾選擇對話框"""
        folder = filedialog.askdirectory()
        if folder:
            self.dir_path.set(folder)
            self.image_files = []
            self.update_image_status()

    def browse_images(self):
        """選擇圖片檔（可多選），比指定資料夾更不容易忘記放圖。"""
        files = filedialog.askopenfilenames(
            title="選擇圖片檔（可多選）",
            filetypes=[("Images", "*.jpg;*.jpeg;*.png;*.webp"), ("All files", "*.*")],
        )
        if files:
            self.image_files = list(files)
            self.dir_path.set("")
            self.update_image_status()

    def clear_folder(self):
        """清除圖片來源（資料夾與多選檔案都會清掉）。"""
        self.dir_path.set("")
        self.image_files = []
        self.update_image_status()
        self.log("[系統] 已清除圖片來源，下次執行將使用純文字推論模式。")

    def update_image_status(self):
        if self.image_files:
            self.image_status.set(f"已選 {len(self.image_files)} 張圖片")
        elif self.dir_path.get():
            self.image_status.set("使用資料夾掃描")
        else:
            self.image_status.set("(未選擇圖片)")

    def on_meeting_mode_change(self, event=None):
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
        self._cancel_event.set()
        self.log("[系統] 取消訊號已送出，等待當前步驟完成後停止...")
        self.btn_cancel.config(state="disabled")

    def log(self, message, newline=True):
        """執行緒安全的日誌輸出函式 (支援打字機效果的不換行拼接)"""
        self.root.after(0, self._append_log, message, newline)
            
    def _append_log(self, message, newline):
        text_to_insert = message + ("\n" if newline else "")
        self.text_log.insert(tk.END, text_to_insert)
        self.text_log.see(tk.END)

    def load_config(self):
        """讀取 config.json，若檔案存在則覆蓋介面，若不存在則填入預設值"""
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
        """重新掃描本地 .gguf 模型並更新下拉選單，同時查詢 LM Studio API 取得已載入模型"""
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
        """測試 LM Studio 伺服器連線"""
        global LM_STUDIO_URL
        import urllib.request
        # 從 GUI 欄位讀取最新的 URL
        LM_STUDIO_URL = normalize_lm_studio_url(self.var_lm_url.get().rstrip("/") or LM_STUDIO_URL)
        self.var_lm_url.set(LM_STUDIO_URL)
        self.lm_status_label.config(text="測試中...", fg="orange")
        self.root.update_idletasks()

        def _test():
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
        """將當前介面設定存入 config.json。包含清空後的路徑狀態也會被正確儲存。"""
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
        """觸發啟動會議並鎖定按鈕防連點"""
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
        """背景非同步迴圈，執行結束後解鎖按鈕"""
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
