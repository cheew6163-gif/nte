"""仅在保存证据时遮挡已知个人信息区域；不参与识图或动作判定。"""
import re
import numpy as np


# 1499×843参考画幅：账号栏、匹配昵称、结算昵称、对战顶部昵称。
_ACCOUNT_BAR = (0, 810, 270, 843)
_NAMES = {
    "匹配页": ((230, 120, 510, 170),),
    "胜利结算": ((80, 380, 350, 437), (1135, 380, 1430, 437)),
    "失败结算": ((80, 380, 350, 437), (1135, 380, 1430, 437)),
    "战场": ((225, 45, 365, 95), (1110, 45, 1280, 95)),
    "离开确认框": ((225, 45, 365, 95), (1110, 45, 1280, 95)),
}


def redact_frame(frame, scene=None):
    """返回副本，保留原始截图供识别；场景未知时合并已知遮挡区。"""
    result = np.array(frame, copy=True)
    height, width = result.shape[:2]
    value = getattr(scene, "value", scene)
    safe_scenes = {"地图", "篝火菜单", "人物界面", "活动页"}
    names = (_NAMES.get(value, ()) if value in safe_scenes or value in _NAMES else
             tuple(box for boxes in _NAMES.values() for box in boxes))
    for x1, y1, x2, y2 in (_ACCOUNT_BAR, *names):
        left, top = int(x1*width/1499), int(y1*height/843)
        right, bottom = int(np.ceil(x2*width/1499)), int(np.ceil(y2*height/843))
        result[top:bottom, left:right] = 0
        if result.ndim == 3 and result.shape[2] == 4:
            result[top:bottom, left:right, 3] = 255
    return result


def redact_text(text):
    """隐去个人目录、邮箱及带标签的账号/昵称，保留错误码和相对路径。"""
    text = re.sub(r"(?i)([a-z]:[\\/]Users[\\/])[^\\/\r\n\"'<>]+", r"\1<user>", str(text))
    text = re.sub(r"(/(?:home|Users)/)[^/\r\n\"'<>]+", r"\1<user>", text)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", text)
    text = re.sub(r"(?i)\b(UID\s*[:：=]?\s*)\d{5,}\b", r"\1<account>", text)
    return re.sub(r"(?i)((?:nickname|nick_name|昵称)\s*[:：=]\s*)[^\r\n,，;；]+", r"\1<private>", text)


_PRIVATE_KEYS = {"uid", "user_id", "account_id", "nickname", "nick_name",
                 "username", "user_name", "account_name", "email"}


def redact_report(value):
    if isinstance(value, dict):
        return {key: "<private>" if str(key).lower() in _PRIVATE_KEYS else redact_report(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_report(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value
