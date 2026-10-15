# -*- coding: utf-8 -*-
"""
凭据的本地加密存储。

WebDAV 密码不能明文写进 config.json —— 那份文件可能被同步软件带走、
被备份到移动硬盘，也可能被同机其他程序直接读到。

Windows 上用系统自带的 **DPAPI**（CryptProtectData）：
    - 密文绑定「当前 Windows 用户 + 本机」，换用户/换机器解不开；
    - 不需要用户记第二个密码，也不依赖任何第三方包（ctypes 直调 crypt32）；
    - 这是 Chrome / Windows 凭据管理器同款机制，适合「防同机其他程序
      与备份泄漏」，不防能以该用户身份执行代码的攻击者。

落盘格式是带前缀的字符串：
    dpapi:<base64 密文>     Windows
    b64:<base64>           非 Windows 的退化方案（仅避免肉眼可见明文，
                           不提供安全保证，macOS/Linux 主要用本地目录后端）
"""
import base64
import ctypes
import sys
from ctypes import wintypes

PREFIX_DPAPI = "dpapi:"
PREFIX_B64 = "b64:"


class SecretError(Exception):
    pass


# ==================================================================
# Windows DPAPI
# ==================================================================
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    # buf 必须随 DATA_BLOB 一起返回给调用方持有，否则可能被提前回收。
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _dpapi_call(encrypt: bool, data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob, _in_buf = _blob_from_bytes(data)
    out_blob = _DATA_BLOB()
    # 两个 API 的签名一致：pDataIn, szDescr, pEntropy, reserved,
    # promptStruct, flags, pDataOut。
    func = (crypt32.CryptProtectData if encrypt
            else crypt32.CryptUnprotectData)
    ok = func(ctypes.byref(in_blob), None, None, None, None, 0,
              ctypes.byref(out_blob))
    if not ok:
        raise SecretError("DPAPI %s 失败 (GetLastError=%d)"
                          % ("加密" if encrypt else "解密",
                             kernel32.GetLastError()))
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


# ==================================================================
# 对外接口
# ==================================================================
def is_dpapi_available() -> bool:
    return sys.platform.startswith("win")


def encrypt(plaintext: str) -> str:
    """把明文密码加密成可写进 config.json 的字符串。空串原样返回。"""
    if plaintext is None or plaintext == "":
        return ""
    raw = plaintext.encode("utf-8")
    if is_dpapi_available():
        return PREFIX_DPAPI + base64.b64encode(_dpapi_call(True, raw)).decode("ascii")
    # 非 Windows：base64 只是避免明文被一眼看到，不声称安全。
    return PREFIX_B64 + base64.b64encode(raw).decode("ascii")


def decrypt(token: str) -> str:
    """解密 encrypt() 的产物。空串/None 返回空串；无法解密抛 SecretError。"""
    if not token:
        return ""
    if token.startswith(PREFIX_DPAPI):
        if not is_dpapi_available():
            raise SecretError("该凭据由 Windows DPAPI 加密，无法在当前系统解密")
        raw = _dpapi_call(False, base64.b64decode(token[len(PREFIX_DPAPI):]))
        return raw.decode("utf-8")
    if token.startswith(PREFIX_B64):
        return base64.b64decode(token[len(PREFIX_B64):]).decode("utf-8")
    # 兼容历史遗留的裸明文（旧版本 config.json 直接存明文密码）。
    return token
