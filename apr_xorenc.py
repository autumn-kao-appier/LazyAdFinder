#!/usr/bin/env python3
"""
apr_xorenc.py — 解碼 SDK bid request 內 `ext_enc` 暗碼欄位（Signal SDK payload）。

`ext_enc` 是 data-signal SDK（Argus）收集的敏感裝置訊號，經 AprXorEnc 混淆後
以 `ae1:<saltLen>:<base64url(xor data)><salt>` 格式塞進 bid request。明文 body 的
`req.device` 只帶基本欄位，敏感訊號（ifv / applist / boottime / mem / disk /
iaphistory / sensors / jailbreak / geo、以及整個 user block）都在這個暗碼包裡。

這是 obfuscation 不是加密：secretKey 直接寫死在 SDK binary，salt 隨封包明文送出，
任何人反編譯 app 都能還原（真正的機密性靠 TLS）。此處只做「解碼側」，供 QA 對照。

演算法與金鑰對過兩邊官方原始碼，逐字一致：
  AOS: SDKs/Android/appier-ads-data-signal-android/.../crypto/SignalEncryptor.kt
  iOS: SDKs/IOS/appier-ads-ios/AppierAdsSdk/Security/AprXorEnc.swift
"""

import base64
import json

VERSION = "ae1"
# 與 SignalEncryptor.kt SECRET_KEY / AprXorEnc.swift secretKey 逐字一致（長度 64）
SECRET_KEY = "6cxqx3vRwA41I8FvZFTjS55xWj5mjvVX2CfV0UP5ywgv0nZ6PoDUeH_it986sZWz"


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def decrypt(blob: str) -> bytes:
    """把 `ae1:<saltLen>:<base64url(ciphertext)><salt>` 還原成原始 bytes。"""
    ver, salt_len_s, rest = blob.split(":", 2)
    if ver != VERSION:
        raise ValueError(f"unexpected ext_enc version: {ver!r}（僅支援 {VERSION}）")
    salt_len = int(salt_len_s)
    salt = rest[-salt_len:]                     # salt 明文接在尾巴
    ciphertext = _b64url_decode(rest[:-salt_len])
    key = (salt + SECRET_KEY).encode("utf-8")   # key = salt + secretKey，逐 byte XOR
    return bytes(ciphertext[i] ^ key[i % len(key)] for i in range(len(ciphertext)))


def decode_ext_enc(bid_body: dict):
    """從 bid request dict 取出 ext_enc → 解碼成 JSON。回 (raw_blob, decoded_obj)。
    沒有 ext_enc 欄位回 (None, None)。"""
    blob = bid_body.get("ext_enc")
    if not blob:
        return None, None
    return blob, json.loads(decrypt(blob))


# ── 訊號欄位對照表（明文 body vs 暗碼解碼後）─────────────────────────────────────
# 每列：(AND TC, 標籤, 明文路徑相對 req, 解碼路徑相對 decoded root)
# 明文路徑 None = 明文 body 結構上根本沒有此節點（訊號只走暗碼包）
SIGNAL_FIELDS = [
    ("AND-03", "device.ifv (App Set ID)",   "device.ifv",              "device.ifv"),
    ("AND-30", "device.lang",               "device.lang",             "device.lang"),
    ("AND-32", "device.input_lang",         "device.input_lang",       "device.input_lang"),
    ("AND-68", "device.type",               "device.type",             "device.type"),
    ("AND-42", "device.ext.gyroscope",      "device.ext.gyroscope",    "device.ext.gyroscope"),
    ("AND-43", "device.ext.accelerometer",  "device.ext.accelerometer","device.ext.accelerometer"),
    ("AND-44", "device.ext.boottime",       "device.ext.boottime",     "device.ext.boottime"),
    ("AND-53", "device.ext.mem_total",      "device.ext.mem_total",    "device.ext.mem_total"),
    ("AND-54", "device.ext.mem_available",  "device.ext.mem_available","device.ext.mem_available"),
    ("AND-55", "device.ext.disk_total",     "device.ext.disk_total",   "device.ext.disk_total"),
    ("AND-56", "device.ext.disk_free",      "device.ext.disk_free",    "device.ext.disk_free"),
    ("AND-61", "device.ext.latency",        "device.ext.latency",      "device.ext.latency"),
    ("AND-62", "device.ext.applist",        "device.ext.applist",      "device.ext.applist"),
    ("AND-63", "device.ext.iaphistory",     "device.ext.iaphistory",   "device.ext.iaphistory"),
    ("AND-11", "device.ext.jailbreak",      "device.ext.jailbreak",    "device.ext.jailbreak"),
    ("AND-49", "user.app_init_time",        None,                      "user.app_init_time"),
    ("AND-51", "user.impression_history",   None,                      "user.impression_history"),
]

_MISSING = object()


def _get_path(obj, dotted):
    if dotted is None:
        return _MISSING
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _fmt(value):
    if value is _MISSING:
        return "(欄位不存在)"
    if value is None:
        return "null"
    if isinstance(value, list):
        if not value:
            return "[]（空陣列）"
        return f"[{len(value)} 筆] {json.dumps(value[:3], ensure_ascii=False)}" + (" …" if len(value) > 3 else "")
    return json.dumps(value, ensure_ascii=False)


def build_comparison(bid_body: dict, decoded: dict):
    """產出「明文 body vs 暗碼解碼」逐欄對照。回 (rows, text)。
    rows: [{tc, label, plaintext, decoded, revealed}]；
    revealed=True 代表明文缺/None 但暗碼包內有實值（暗碼揭露的訊號）。"""
    req = bid_body.get("req", {})
    rows = []
    for tc, label, pt_path, dec_path in SIGNAL_FIELDS:
        pt_val = _get_path(req, pt_path)
        dec_val = _get_path(decoded, dec_path)
        pt_absent = pt_val is _MISSING or pt_val is None
        dec_present = dec_val is not _MISSING and dec_val is not None and dec_val != []
        rows.append({
            "tc": tc, "label": label,
            "plaintext": _fmt(pt_val), "decoded": _fmt(dec_val),
            "revealed": pt_absent and dec_present,
        })

    lines = []
    lines.append("Signal 暗碼(ext_enc)解碼對照 — 明文 body vs 解碼後 Signal payload")
    lines.append("=" * 92)
    lines.append(f"{'TC':<8} {'欄位':<28} {'明文 body':<22} 暗碼解碼後")
    lines.append("-" * 92)
    revealed_n = 0
    for r in rows:
        mark = " ★" if r["revealed"] else ""
        if r["revealed"]:
            revealed_n += 1
        lines.append(f"{r['tc']:<8} {r['label']:<28} {r['plaintext']:<22} {r['decoded']}{mark}")
    lines.append("-" * 92)
    lines.append(f"★ = 明文缺/null，但暗碼包內有實值（暗碼揭露的訊號）：{revealed_n}/{len(rows)} 欄")
    lines.append("註：ip/ipv6 於明文與暗碼皆 null（server-side 補值 / 本版 Blocked），非暗碼可救。")
    return rows, "\n".join(lines)


def write_evidence(bid_body: dict, folder):
    """capture 時呼叫：把原始 blob、解碼 JSON、對照表落地到 capture 資料夾。
    回 (decoded_obj, comparison_rows) 或 (None, None)。"""
    import os
    raw, decoded = decode_ext_enc(bid_body)
    if decoded is None:
        return None, None
    with open(os.path.join(folder, "ext_enc_raw.txt"), "w") as f:
        f.write(raw + "\n")
    with open(os.path.join(folder, "ext_enc_decoded.json"), "w") as f:
        json.dump(decoded, f, ensure_ascii=False, indent=2)
    rows, text = build_comparison(bid_body, decoded)
    with open(os.path.join(folder, "ext_enc_compare.txt"), "w") as f:
        f.write(text + "\n")
    return decoded, rows


if __name__ == "__main__":
    import sys
    body = json.load(open(sys.argv[1]))
    raw, decoded = decode_ext_enc(body)
    if decoded is None:
        sys.exit("no ext_enc in this bid request")
    _, text = build_comparison(body, decoded)
    print(text)
