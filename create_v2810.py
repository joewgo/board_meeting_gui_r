import re, sys

with open('board_meeting_gui_v2.8.9.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1) Version strings
content = content.replace('v2.8.9', 'v2.8.10')

# 2) Add traceback import
content = content.replace(
    'import threading\n',
    'import threading\nimport traceback\n'
)

# 3) Fix _PermissionApprovedResult + handler → return plain dict
old_perm = '''class _PermissionApprovedResult:
    """PermissionRequestResult 相容物件：實作 approve_all 語意。
    新版 SDK (main) 呼叫 handler 後存取 .kind / .rules / .feedback / .message / .path，
    此物件直接提供必要屬性，無需依賴 copilot.session.PermissionRequestResult dataclass
    （避免在舊版 SDK 中 ImportError）。"""
    kind: str = "approved"
    rules = None
    feedback = None
    message = None
    path = None


def _default_permission_handler(request, invocation=None):
    """自動核准所有來自 Copilot 的工具權限請求。
    Board Meeting 場景不呼叫任何外部工具，handler 通常不會被觸發；
    但新版 SDK (main) 要求必須傳入合法 callable，此處回傳相容物件確保安全。"""
    return _PermissionApprovedResult()'''

new_perm = '''def _default_permission_handler(request, invocation=None):
    """自動核准所有來自 Copilot 的工具權限請求。
    回傳符合 PermissionRequestResult TypedDict 格式的純 dict（JSON 可序列化）。
    Board Meeting 場景通常不觸發工具呼叫，此 handler 作為安全保障。
    v2.8.10 修正：改用 dict 回傳，避免自訂物件導致 JSON 序列化失敗。"""
    return {"kind": "approved"}'''

if old_perm in content:
    content = content.replace(old_perm, new_perm)
    print("✅ Permission handler fixed")
else:
    print("❌ Permission handler block NOT found - manual check needed")

# 4) Simplify _create_copilot_session
old_sess = '''async def _create_copilot_session(client, copilot_config: Dict[str, Any]):
    """建立 CopilotClient session，相容新版 SDK (main) 與舊版 SDK (≤0.1.25)：

    新版 SDK (main branch)：
        create_session(*, on_permission_request, model=None, streaming=None, system_message=None, ...)
        → 所有參數皆為 keyword-only；on_permission_request 為必填；不接受位置引數

    舊版 SDK (≤0.1.25)：
        create_session(config: Optional[SessionConfig] = None)
        → 接受 config dict 作為位置引數；無 on_permission_request 參數

    策略：優先嘗試新版 keyword-only API，再降級至舊版 config dict，最後無參數。
    """
    # 取出 session 相關 kwargs（排除 on_permission_request，下面獨立傳入）
    session_kwargs = {k: v for k, v in copilot_config.items() if k != "on_permission_request"}

    # 策略 1：新版 SDK (main) — 全 keyword-only，on_permission_request 為必填 kwarg
    # 若 SDK 不認識 on_permission_request 或其他 kwargs，會拋出 TypeError，落入策略 2
    try:
        return await client.create_session(
            on_permission_request=_default_permission_handler,
            **session_kwargs,
        )
    except TypeError:
        pass

    # 策略 2：舊版 SDK (≤0.1.25) — 傳入 config dict（不含 on_permission_request）
    try:
        return await client.create_session(session_kwargs)
    except TypeError:
        pass

    # 策略 3：極舊版 SDK — 不帶任何參數
    return await client.create_session()'''

new_sess = '''async def _create_copilot_session(client, copilot_config: Dict[str, Any]):
    """建立 CopilotClient session。

    SDK v0.1.x (已安裝版本) 的 create_session 接受一個 config dict：
        create_session(config: Optional[SessionConfig] = None)
        config 可包含 model / streaming / system_message / on_permission_request 等欄位。
        SDK 內部從 config dict 讀取 on_permission_request 並自動設置 requestPermission 旗標。

    v2.8.10 修正：直接將 on_permission_request 包含在 config dict 中（符合 SDK 文件規範），
    廢除 Strategy 1~3 複雜回退邏輯，避免邊界條件導致 AttributeError。
    """
    config_with_handler = {**copilot_config, "on_permission_request": _default_permission_handler}
    return await client.create_session(config_with_handler)'''

if old_sess in content:
    content = content.replace(old_sess, new_sess)
    print("✅ _create_copilot_session simplified")
else:
    print("❌ _create_copilot_session block NOT found - manual check needed")

# 5) Improve exception logging with traceback
old_exc = '''    except asyncio.CancelledError:
        log_callback("\\n⛔ 會議已取消。")
    except Exception as e:
        log_callback(f"\\n❌ 會議發生錯誤：{e}")'''

new_exc = '''    except asyncio.CancelledError:
        log_callback("\\n⛔ 會議已取消。")
    except Exception as e:
        log_callback(f"\\n❌ 會議發生錯誤：{e}")
        log_callback(f"\\n[除錯] 詳細錯誤追蹤:\\n{traceback.format_exc()}")'''

if old_exc in content:
    content = content.replace(old_exc, new_exc)
    print("✅ Exception traceback logging added")
else:
    print("❌ Exception handler NOT found - manual check needed")

with open('board_meeting_gui_v2.8.10.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("✅ board_meeting_gui_v2.8.10.py written")

# Verify key strings
checks = [
    ('v2.8.10', True),
    ('v2.8.9', False),
    ('_PermissionApprovedResult', False),
    ('import traceback', True),
    ('config_with_handler', True),
    ('traceback.format_exc()', True),
]
for s, should_exist in checks:
    found = s in content
    ok = found == should_exist
    print(f"  {'✅' if ok else '❌'} '{s}': {'found' if found else 'not found'} (expected {'found' if should_exist else 'not found'})")
