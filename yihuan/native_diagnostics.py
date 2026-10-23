"""只读 Win32 输入环境诊断；不切换桌面、不解除 ClipCursor、不提权。"""
import ctypes as C
from ctypes import wintypes as W
import os
import time


def clear_error():
    if hasattr(C, 'set_last_error'):
        C.set_last_error(0)


def last_error():
    return C.get_last_error() if hasattr(C, 'get_last_error') else 0


def error_details(api, parameters, code, result=0):
    # 必须传入调用后立即保存的 code，不能在格式化/其他 API 之后再读。
    text = (C.FormatError(code).strip() if hasattr(C, 'FormatError') else f'Windows error {code}') if code else (
        'API 未提供扩展错误码（调用前已清零）；失败不能解释为成功' if not result else '')
    return {'api': api, 'parameters': parameters, 'return_value': result,
            'last_error': int(code), 'error_text': text, 'monotonic': time.monotonic()}


class WinApiError(RuntimeError):
    def __init__(self, message, details):
        self.details = details
        super().__init__(f"{message}；{details['api']} WinError={details['last_error']}：{details['error_text']}")


def bind(dll, name, result, args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


def _failed(name, code, parameters=None):
    return {'error': error_details(name, parameters or {}, code)}


def desktop_context(user):
    kernel = C.WinDLL('kernel32', use_last_error=True)
    tid = bind(kernel, 'GetCurrentThreadId', W.DWORD, [])()
    get_thread = bind(user, 'GetThreadDesktop', W.HANDLE, [W.DWORD])
    get_station = bind(user, 'GetProcessWindowStation', W.HANDLE, [])
    query = bind(user, 'GetUserObjectInformationW', W.BOOL,
                 [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)])
    open_input = bind(user, 'OpenInputDesktop', W.HANDLE, [W.DWORD, W.BOOL, W.DWORD])
    close = bind(user, 'CloseDesktop', W.BOOL, [W.HANDLE])

    def info(handle, desktop=False):
        result = {'handle': int(handle or 0)}
        name, required = C.create_unicode_buffer(512), W.DWORD()
        clear_error(); ok = query(handle, 2, name, C.sizeof(name), C.byref(required)); code = last_error()
        if ok:
            result['name'] = name.value
        else:
            result.update(_failed('GetUserObjectInformationW', code, {'index': 2}))
        if desktop:
            receives = W.BOOL()
            clear_error(); ok = query(handle, 6, C.byref(receives), C.sizeof(receives), C.byref(required)); code = last_error()
            if ok:
                result['receives_input'] = bool(receives.value)
            else:
                result['receives_input_error'] = _failed('GetUserObjectInformationW', code, {'index': 6})
        return result

    result = {'thread_id': int(tid)}
    clear_error(); thread = get_thread(tid); code = last_error()
    result['thread_desktop'] = info(thread, True) if thread else _failed('GetThreadDesktop', code)
    clear_error(); station = get_station(); code = last_error()
    result['window_station'] = info(station) if station else _failed('GetProcessWindowStation', code)
    clear_error(); handle = open_input(0, False, 1); code = last_error()
    if handle:
        try:
            result['input_desktop'] = info(handle, True)
        finally:
            close(handle)
    else:
        result['input_desktop'] = _failed('OpenInputDesktop', code, {'flags': 0, 'inherit': False, 'access': 1})
    a, b = result['thread_desktop'].get('name'), result['input_desktop'].get('name')
    result['same_input_desktop'] = (a == b) if a and b else None
    # 只查询访问权限，不切换桌面、窗口站，也不改变光标限制。
    clear_error(); write_desktop = open_input(0, False, 0x81); code = last_error()
    result['input_desktop_read_write_access'] = bool(write_desktop)
    if write_desktop:
        close(write_desktop)
    else:
        result['input_desktop_access_error'] = _failed('OpenInputDesktop', code, {'access': 0x81})
    station_name = result['window_station'].get('name')
    if station_name:
        open_station = bind(user, 'OpenWindowStationW', W.HANDLE, [W.LPCWSTR, W.BOOL, W.DWORD])
        close_station = bind(user, 'CloseWindowStation', W.BOOL, [W.HANDLE])
        clear_error(); writable = open_station(station_name, False, 0x10); code = last_error()
        result['window_station']['write_attributes_access'] = bool(writable)
        if writable:
            close_station(writable)
        else:
            result['window_station']['access_error'] = _failed('OpenWindowStationW', code, {'name': station_name, 'access': 0x10})
    return result


def token_status(pid):
    kernel = C.WinDLL('kernel32', use_last_error=True)
    advapi = C.WinDLL('advapi32', use_last_error=True)
    open_process = bind(kernel, 'OpenProcess', W.HANDLE, [W.DWORD, W.BOOL, W.DWORD])
    close = bind(kernel, 'CloseHandle', W.BOOL, [W.HANDLE])
    open_token = bind(advapi, 'OpenProcessToken', W.BOOL, [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)])
    query = bind(advapi, 'GetTokenInformation', W.BOOL,
                 [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)])
    get_count = bind(advapi, 'GetSidSubAuthorityCount', C.POINTER(C.c_ubyte), [C.c_void_p])
    get_rid = bind(advapi, 'GetSidSubAuthority', C.POINTER(W.DWORD), [C.c_void_p, W.DWORD])
    result = {'pid': int(pid)}
    clear_error(); process = open_process(0x1000, False, pid); code = last_error()
    if not process:
        return dict(result, **_failed('OpenProcess', code, {'pid': pid, 'access': 0x1000}))
    token = W.HANDLE()
    try:
        image_query = bind(kernel, 'QueryFullProcessImageNameW', W.BOOL,
                           [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)])
        image, capacity = C.create_unicode_buffer(32768), W.DWORD(32768)
        clear_error(); ok = image_query(process, 0, image, C.byref(capacity)); code = last_error()
        result['image'] = image.value if ok else _failed('QueryFullProcessImageNameW', code)
        clear_error(); ok = open_token(process, 0x0008, C.byref(token)); code = last_error()
        if not ok:
            return dict(result, **_failed('OpenProcessToken', code, {'pid': pid, 'access': 8}))
        for key, index in [('elevated', 20), ('ui_access', 26), ('session_id', 12)]:
            value, size = W.DWORD(), W.DWORD()
            clear_error(); ok = query(token, index, C.byref(value), C.sizeof(value), C.byref(size)); code = last_error()
            result[key] = (int(value.value) if key == 'session_id' else bool(value.value)) if ok else _failed('GetTokenInformation', code, {'class': index})
        required = W.DWORD()
        clear_error(); query(token, 25, None, 0, C.byref(required)); code = last_error()
        if not required.value:
            result['integrity'] = _failed('GetTokenInformation', code, {'class': 25})
        else:
            buffer = C.create_string_buffer(required.value)
            clear_error(); ok = query(token, 25, buffer, len(buffer), C.byref(required)); code = last_error()
            if not ok:
                result['integrity'] = _failed('GetTokenInformation', code, {'class': 25})
            else:
                sid = C.cast(buffer, C.POINTER(C.c_void_p))[0]
                count = get_count(sid)[0]
                rid = int(get_rid(sid, count - 1)[0])
                labels = {0: 'untrusted', 4096: 'low', 8192: 'medium', 8448: 'medium_plus',
                          12288: 'high', 16384: 'system', 20480: 'protected'}
                result['integrity_rid'] = rid
                result['integrity'] = labels.get(rid, str(rid))
        return result
    finally:
        if token: close(token)
        close(process)


def input_diagnostics(api, window=None, target=None):
    result = {'pid': os.getpid(), 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'issues': []}

    def safe(key, fn):
        try:
            result[key] = fn()
        except Exception as exc:
            result[key] = {'error': str(exc), 'details': getattr(exc, 'details', None)}
        return result[key]

    desktop = safe('desktop', lambda: desktop_context(api.user))
    if desktop.get('same_input_desktop') is False or desktop.get('thread_desktop', {}).get('receives_input') is False:
        result['issues'].append('当前线程不在接收输入的桌面；不会切换桌面或继续点击')
    safe('thread_dpi', lambda: api.current_dpi())
    virtual = safe('virtual_desktop', api.virtual_desktop)
    safe('physical_cursor', lambda: list(api.cursor_position()))
    safe('logical_cursor', lambda: list(api.logical_cursor_position()))
    clip = safe('clip_cursor', api.clip_rectangle)
    if target is not None:
        result['target'] = list(target)
        if 'left' in clip:
            inside = clip['left'] <= target[0] < clip['right'] and clip['top'] <= target[1] < clip['bottom']
            result['target_inside_clip'] = inside
            if not inside:
                result['issues'].append('目标在 ClipCursor 限制范围外；不会自动解除游戏鼠标限制')
    def foreground_info():
        foreground = api.user.GetForegroundWindow()
        info = {'hwnd': int(foreground or 0)}
        if foreground:
            for key, fn in [('title', api.title), ('pid', api.process_id), ('window_class', api.class_name)]:
                try:
                    info[key] = fn(foreground)
                except Exception as exc:
                    info[key] = {'error': str(exc), 'details': getattr(exc, 'details', None)}
        return info
    safe('foreground', foreground_info)
    foreground = result['foreground'].get('hwnd', 0)
    safe('assistant_security', lambda: token_status(os.getpid()))
    if window is not None:
        result['target_hwnd'] = int(window.hwnd)
        result['target_is_foreground'] = foreground == window.hwnd
        safe('target_security', lambda: token_status(api.process_id(window.hwnd)))
        a, b = result['assistant_security'].get('integrity_rid'), result['target_security'].get('integrity_rid')
        result['uipi_risk'] = (a < b) if a is not None and b is not None else None
        if result['uipi_risk']:
            result['issues'].append('助手完整性级别低于目标进程，有 UIPI 风险（尚非拒绝原因的证明）')
    if 'left' in virtual and 'left' in clip:
        result['cursor_clip_restricted'] = any(clip[k] != virtual[k] for k in ('left', 'top', 'right', 'bottom'))
    result['recent_api_calls'] = list(getattr(api, 'api_calls', []))[-20:]
    return result
