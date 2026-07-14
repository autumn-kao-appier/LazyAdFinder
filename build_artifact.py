#!/usr/bin/env python3
"""
build_artifact.py — 把一個 round 的 evidence 資料夾渲染成自包含 HTML report。

一條 TC 一張 evidence card：TC 定義、可驗度、expected vs actual、判定、
bid 欄位實際值、該 capture 的手機截圖（點開放大），以及「如何把手機設成
這個狀態 + 截圖該證明什麼」的重現步驟。涵蓋 bid_inspector 的全部 80 條 checks。

用法:
    python build_artifact.py <round_dir> [--out report.html]

    <round_dir>  evidence/<round>/ ；掃描底下每個 capture 子資料夾（含 baseline）
                 的 results.json / bid_request.json / phone.png。

判定分級:
    PASS      已 capture 且值符合規格
    FAIL      已 capture 且值違反規格（真缺陷；SDK 未實作會標 RD gap）
    PENDING   狀態類 TC，尚未在該 TC 要求的裝置狀態下 capture（需補抓）
    BLOCKED   受限於硬體/環境無法執行（無 SIM、需非 root 機、latency endpoint）
"""

import argparse
import base64
import glob
import html
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image
except Exception:
    Image = None


def encode_shot(path, max_w=720, quality=78):
    """縮圖 + JPEG 編碼成 data URI，控制 artifact 體積（原尺寸 PNG 會爆 16MB 上限）。

    720px 寬足以肉眼讀設定頁的開關/文字；無 Pillow 時退回原檔 base64。
    """
    if Image is None:
        return "data:image/png;base64," + base64.b64encode(open(path, "rb").read()).decode()
    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

sys.path.insert(0, str(Path(__file__).parent))
from bid_inspector import VALIDATORS, run_inspection, _unwrap  # noqa: E402


# ── TC 分類 / 可驗度 / 狀態重現 metadata ────────────────────────────────────────

CATEGORIES = {
    "A": "Core Identifiers",
    "B": "Device State — Bool",
    "C": "Device State — Numeric",
    "D": "Device / App — Format",
    "E": "Device State — Arrays",
    "F": "Geolocation",
    "G": "In-Session",
    "H": "Memory / Disk",
    "I": "Screen / Display",
    "J": "Negative / Absent",
    "K": "Network Latency",
    "M": "SKAdNetwork",
}

# TC → 分類字母（依 sheet Cat A–M）
CAT_OF = {
    "AND-01": "A", "AND-02": "A", "AND-03": "A", "AND-28": "A", "AND-29": "A",
    "AND-75": "A", "AND-76": "A",
    "AND-04": "B", "AND-05": "B", "AND-06": "B", "AND-07": "B", "AND-08": "B",
    "AND-09": "B", "AND-10": "B", "AND-11": "B", "AND-12": "B", "AND-13": "B",
    "AND-14": "B", "AND-15": "B",
    "AND-16": "C", "AND-17": "C", "AND-19": "C", "AND-20": "C", "AND-21": "C",
    "AND-22": "C", "AND-23": "C", "AND-24": "C", "AND-25": "C", "AND-26": "C",
    "AND-27": "C",
    "AND-30": "D", "AND-31": "D", "AND-32": "D", "AND-33": "D", "AND-34": "D",
    "AND-35": "D", "AND-36": "D", "AND-37": "D", "AND-38": "D", "AND-39": "D",
    "AND-40": "D", "AND-41": "D", "AND-66": "D", "AND-67": "D", "AND-68": "D",
    "AND-69": "D", "AND-70": "D", "AND-71": "D", "AND-73": "D", "AND-74": "D",
    "AND-42": "E", "AND-43": "E", "AND-44": "E",
    "AND-45": "F", "AND-46": "F",
    "AND-47": "G", "AND-48": "G", "AND-49": "G", "AND-50": "G", "AND-51": "G",
    "AND-52": "G",
    "AND-53": "H", "AND-54": "H", "AND-55": "H", "AND-56": "H",
    "AND-57": "I", "AND-58": "I", "AND-59": "I", "AND-60": "I",
    "AND-61": "K",
    "AND-62": "J", "AND-63": "J", "AND-64": "J", "AND-65": "J", "AND-72": "J",
    "AND-81": "M",
}

# 狀態類 TC：group（互斥組）+ 如何設定 + 截圖該證明什麼
STATE = {
    "AND-01": ("tracking", "維持廣告追蹤開啟（未 opt out）", "系統「廣告」設定頁顯示廣告 ID 存在（可與 bid 的 ia 對照）"),
    "AND-02": ("tracking", "在系統設定刪除廣告 ID（opt out）", "系統「廣告」設定頁顯示廣告 ID 已刪除"),
    "AND-75": ("tracking", "維持廣告追蹤開啟", "廣告設定頁顯示追蹤未受限（lat=0）"),
    "AND-76": ("tracking", "刪除廣告 ID（opt out）", "廣告設定頁顯示追蹤已受限（lat=1）"),
    "AND-04": ("darkmode", "開啟深色主題", "顯示設定頁的「深色主題」開關為開"),
    "AND-05": ("darkmode", "關閉深色主題", "顯示設定頁的「深色主題」開關為關"),
    "AND-06": ("charging", "接上電源充電", "電池頁顯示充電中"),
    "AND-07": ("charging", "拔除電源（未充電）", "電池頁顯示未充電"),
    "AND-08": ("batterysaver", "開啟省電模式", "省電模式開關為開"),
    "AND-09": ("batterysaver", "關閉省電模式", "省電模式開關為關"),
    "AND-10": ("jailbreak", "使用未 root 裝置", "裝置未 root"),
    "AND-11": ("jailbreak", "使用已 root 裝置", "裝置已 root（Magisk / root checker 佐證）"),
    "AND-12": ("emulator", "在模擬器（AVD）執行", "模擬器畫面"),
    "AND-13": ("emulator", "在實體機執行", "實體機畫面"),
    "AND-14": ("vpn", "連上 VPN", "狀態列顯示 VPN 圖示"),
    "AND-15": ("vpn", "不連 VPN", "狀態列無 VPN 圖示"),
    "AND-16": ("batterylevel", "電量充至滿（100%）", "電池頁電量百分比"),
    "AND-17": ("batterylevel", "電量降至極低（0%）", "電池頁電量百分比"),
    "AND-19": ("screenbright", "螢幕亮度調到最低", "亮度滑桿在最低"),
    "AND-20": ("screenbright", "螢幕亮度調到最高", "亮度滑桿在最高"),
    "AND-21": ("fontscale", "字體大小設為預設", "字體大小為標準"),
    "AND-22": ("fontscale", "字體大小設為最大", "文字明顯放大"),
    "AND-23": ("volume", "媒體音量調到靜音", "音量面板 media 為 0"),
    "AND-24": ("volume", "媒體音量調到最大", "音量面板 media 為滿"),
    "AND-25": ("tz", "時區設為台北（GMT+8）", "日期時間設定顯示 GMT+8"),
    "AND-26": ("tz", "時區設為紐約（EST/EDT）", "日期時間設定顯示美東時區"),
    "AND-27": ("tz", "時區設為 UTC / 倫敦", "日期時間設定顯示 GMT+0"),
    "AND-45": ("geo", "授予定位權限並開啟 GPS", "定位權限已允許"),
    "AND-46": ("geo", "拒絕定位權限", "定位權限已拒絕"),
    "AND-47": ("session", "App 前景停留超過 30 秒再觸發廣告", "App 已使用一段時間"),
    "AND-48": ("session", "冷啟動後立即觸發廣告", "App 剛啟動"),
    "AND-50": ("fgbg", "把 App 切到背景再回前景", "App 有背景→前景切換"),
    "AND-52": ("session", "App 前景累積使用超過 30 秒", "App 已使用一段時間"),
}

# 必須手動、adb 無法自動達成的狀態 → 後續測試者要驗這幾條時照這裡做
MANUAL = {
    "AND-20": "adb 設 brightness 上限只到 179（非 255），到不了最大值；請手動把亮度滑桿拉到最大後 `python run_ssp.py AND-20`",
    "AND-25": "非 root 無 adb 設時區指令；請手動將時區設為台北(GMT+8)後 `python run_ssp.py AND-25`",
    "AND-26": "請手動將時區設為紐約(America/New_York)後 `python run_ssp.py AND-26`",
    "AND-27": "請手動將時區設為 UTC/倫敦後 `python run_ssp.py AND-27`",
    "AND-02": "在系統「廣告」設定頁手動點『刪除廣告 ID』(opt out)後 `python run_ssp.py AND-02`",
    "AND-76": "同 AND-02，opt out 後 device.lat 應變為 1",
}

# 環境/硬體限制無法執行
BLOCKED = {
    "AND-11": "本輪為非 root 實機（Pixel 10a），jailbreak=true 需另備已 root 裝置",
    "AND-12": "emulator=true 需在 Android 模擬器（AVD）另跑一輪",
    "AND-39": "device.ext IPv6 需 4G/5G SIM 實機；辦公室 WiFi 無 IPv6 路由，團隊暫無 SIM 機",
    "AND-41": "cellular conntype 需 SIM 實機；團隊暫無 SIM 機",
    "AND-61": "Echo Server 僅回 IP，無法量測 latency；待 RD 提供其他方式",
    "AND-64": "無 SIM 機；SIM 模擬能力未確認（R5）",
}

# SDK 尚未實作 → 值恆為 null/[]，FAIL 屬 RD gap 而非執行問題
RD_GAP = {
    "AND-14": "SDK 目前 vpn 恆為 null",
    "AND-38": "SDK ip 恆為 null（由 server 端推導，改用 Echo Server 驗）",
    "AND-42": "SDK gyroscope 恆為 []（collector 未實作）",
    "AND-43": "SDK accelerometer 恆為 []（collector 未實作）",
    "AND-51": "SDK impression_history 恆為 []（未實作）",
    "AND-63": "SDK iaphistory 永遠帶 key",
}

# 可驗度分級
ABSENT_CHECKS = {"absent", "absent_or_empty", "falsy", "value_or_absent"}
PARTIAL_CHECKS = {"range", "positive_int", "positive_float", "nonempty",
                  "nonempty_notunknown", "array_nonempty", "array_number",
                  "array_regex", "array_impression", "leq_field",
                  "timestamp_recent", "truthy", "ipv4_nonzero", "regex"}


def tier_of(check, tc):
    if tc in BLOCKED:
        return "Blocked"
    if check in ABSENT_CHECKS:
        return "Absent"
    if check in PARTIAL_CHECKS:
        return "Partial"
    return "Verifiable"


# 互斥組 → [(tc, expected), ...]，供 PENDING 判定
def build_groups():
    groups = {}
    exp = {}
    for v in VALIDATORS:
        exp.setdefault(v["tc"], v.get("expected"))
    for tc, (grp, _, _) in STATE.items():
        groups.setdefault(grp, []).append(tc)
    return groups, exp


# ── evidence 掃描 ───────────────────────────────────────────────────────────────

def load_captures(round_dir):
    """回傳 {capture_name: {"bid": obj, "shot_b64": str|None, "ts": str}}。"""
    caps = {}
    for results_path in glob.glob(os.path.join(round_dir, "*", "results.json")):
        folder = os.path.dirname(results_path)
        name = os.path.basename(folder)
        bid = None
        bid_path = os.path.join(folder, "bid_request.json")
        if os.path.exists(bid_path):
            try:
                bid = json.load(open(bid_path))
            except Exception:
                bid = None
        shot_path = os.path.join(folder, "phone.png")
        shot_path = shot_path if os.path.exists(shot_path) else None
        # state-proof：看得見該狀態的系統畫面（肉眼證據），優先於 app 截圖
        proof_path = os.path.join(folder, "state_proof.png")
        proof_path = proof_path if os.path.exists(proof_path) else None
        proof_cap = None
        cap_path = os.path.join(folder, "state_proof_caption.txt")
        if os.path.exists(cap_path):
            proof_cap = open(cap_path).read().strip()
        # 本次實際執行了什麼（實機設定 / adb 模擬 real→mock）
        action = None
        act_path = os.path.join(folder, "state_action.txt")
        if os.path.exists(act_path):
            action = open(act_path).read().strip()
        # 本次 bid 識別碼（比廣告截圖有意義）
        bid_ids = None
        ids_path = os.path.join(folder, "bid_ids.json")
        if os.path.exists(ids_path):
            try:
                bid_ids = json.load(open(ids_path))
            except Exception:
                bid_ids = None
        ts = ""
        stored = {}
        test_type = ""
        try:
            data = json.load(open(results_path))
            ts = data.get("captured_at", "")
            test_type = data.get("test_type", "")
            # capture 當下算的結果才是權威（時間敏感的 check 事後重算會失真）
            stored = {(r["tc"], r["field"]): r for r in data.get("results", [])}
        except Exception:
            pass
        caps[name] = {"bid": bid, "shot_path": shot_path, "ts": ts,
                      "folder": name, "stored": stored,
                      "proof_path": proof_path, "proof_cap": proof_cap,
                      "action": action, "bid_ids": bid_ids, "test_type": test_type}
    return caps


def pick_capture(tc, caps):
    """優先取該 TC 專屬 capture 的最新一次；否則 baseline；再否則任一。"""
    matches = sorted(n for n in caps
                     if n.startswith(tc + "_") or n.startswith(tc.replace("-", "") + "_"))
    if matches:
        return matches[-1]  # 名稱含時間戳，最後一個即最新
    for name in caps:
        if name.startswith("baseline"):
            return name
    return next(iter(caps), None)


# ── 判定 ─────────────────────────────────────────────────────────────────────────

def classify(tc, check, passed, actual, groups, exp, targeted):
    """回傳 (status, rd_gap_note)。

    FAIL 僅保留給真正的 SDK 缺陷（RD gap）。狀態類 TC 若未通過，代表「該裝置
    狀態未生效／未設到」而非缺陷（adb 在新系統常設不動，如 battery saver），
    一律歸 PENDING——卡片並排 expected/actual/狀態截圖，人可自行判讀是否為狀態沒設到。
    """
    if tc in BLOCKED:
        return "BLOCKED", None
    if passed:
        return "PASS", None
    if tc in RD_GAP:
        return "FAIL", RD_GAP[tc]
    if tc in MANUAL:
        return "MANUAL", None
    if tc in STATE:
        return "PENDING", None
    return "FAIL", None


# ── HTML ─────────────────────────────────────────────────────────────────────────

STATUS_META = {
    "PASS":    ("pass", "Pass"),
    "FAIL":    ("fail", "Fail"),
    "PENDING": ("pending", "Pending capture"),
    "MANUAL":  ("manual", "需手動驗證"),
    "BLOCKED": ("blocked", "Blocked"),
}


def esc(x):
    return html.escape(str(x), quote=True)


def fmt_val(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
        return s if len(s) <= 120 else s[:117] + "…"
    return str(v)


def build(round_dir, out_path):
    caps = load_captures(round_dir)
    if not caps:
        sys.exit(f"no capture (results.json) found under {round_dir}")
    groups, exp = build_groups()
    round_name = os.path.basename(round_dir.rstrip("/"))

    # 優先用 capture 當下存的 results.json；缺才即時重算
    cap_results = {}
    for name, c in caps.items():
        if c["stored"]:
            cap_results[name] = c["stored"]
        elif c["bid"] is not None:
            cap_results[name] = {(r["tc"], r["field"]): r
                                 for r in run_inspection(c["bid"])}

    cards = []
    counts = {"PASS": 0, "FAIL": 0, "PENDING": 0, "MANUAL": 0, "BLOCKED": 0}
    for v in VALIDATORS:
        tc, field, check = v["tc"], v["field"], v["check"]
        cat = CAT_OF.get(tc, "D")
        cap_name = pick_capture(tc, caps)
        targeted = bool(cap_name and (cap_name.startswith(tc + "_")
                                      or cap_name.startswith(tc.replace("-", "") + "_")))
        res = cap_results.get(cap_name, {}).get((tc, field))
        passed = res["passed"] if res else False
        actual = res["actual"] if res else None
        status, rd_note = classify(tc, check, passed, actual, groups, exp, targeted)
        counts[status] += 1

        expected = v.get("expected", None)
        if "pattern" in v:
            expected_disp = f"match {v['pattern'].pattern}"
        elif "min" in v:
            expected_disp = f"{v['min']} … {v['max']}"
        elif check in ABSENT_CHECKS:
            expected_disp = "absent / empty"
        elif expected is not None:
            expected_disp = fmt_val(expected)
        else:
            expected_disp = check.replace("_", " ")

        st = STATE.get(tc)
        cap = caps.get(cap_name, {})
        has_proof = bool(cap.get("proof_path"))
        if has_proof:
            # 只保留「看得見狀態」的設定頁截圖；廣告畫面 phone.png 不再當證據（改用 bidobjid）
            shot_key = cap_name + "::proof"
            shot_caption = cap.get("proof_cap") or "狀態證據截圖"
            shot_matched = True
        else:
            shot_key = None
            shot_caption = None
            shot_matched = False
        cards.append({
            "tc": tc, "field": field, "cat": cat,
            "tier": tier_of(check, tc),
            "status": status, "status_cls": STATUS_META[status][0],
            "status_label": STATUS_META[status][1],
            "condition": v.get("note", "") or f"{field} — {check}",
            "expected": expected_disp,
            "actual": fmt_val(actual) if res else "—",
            "rd_note": rd_note,
            "blocked_reason": BLOCKED.get(tc),
            "set": st[1] if st else None,
            "shows": st[2] if st else None,
            "action": cap.get("action"),
            "bid_ids": cap.get("bid_ids"),
            "manual_hint": MANUAL.get(tc),
            "shot": shot_key,
            "shot_caption": shot_caption,
            "shot_matched": shot_matched,
            "capture": cap_name,
        })

    total = sum(counts.values())
    verified = counts["PASS"] + counts["FAIL"]
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 跨 capture 一致性：同一顆裝置每次 capture 的 ia / ifv 應恆定
    consistency = []
    for label, field in (("GAID (device.ia)", "ia"), ("App Set ID (device.ifv)", "ifv")):
        vals = []
        for c in caps.values():
            if c["bid"]:
                v = _unwrap(c["bid"]).get("device", {}).get(field)
                if v:
                    vals.append(v)
        distinct = sorted(set(vals))
        ok = len(distinct) == 1 and len(vals) > 1
        consistency.append({
            "label": label, "n": len(vals), "distinct": len(distinct),
            "ok": ok, "value": distinct[0] if distinct else "—",
        })

    # 截圖 data URI（只嵌「狀態證據」設定頁截圖；廣告 phone.png 不再當證據、不嵌入以省體積）
    shots_js = {}
    for name, c in caps.items():
        if c["proof_path"]:
            shots_js[name + "::proof"] = encode_shot(c["proof_path"])

    # 分類分組
    by_cat = {}
    for c in cards:
        by_cat.setdefault(c["cat"], []).append(c)

    # 從任一 capture 的 bid 取裝置型號
    model = "Android"
    for c in caps.values():
        if c["bid"]:
            dev = _unwrap(c["bid"]).get("device", {})
            model = dev.get("model") or model
            break
    # 本輪測試類型（aibid / reen-static / reen-dynamic）
    test_type = next((c["test_type"] for c in caps.values()
                      if c.get("test_type") and c["test_type"] != "unspecified"), "")

    html_out = render_html(round_name, generated, counts, total, verified,
                            by_cat, shots_js, model, consistency, test_type)
    Path(out_path).write_text(html_out, encoding="utf-8")
    print(f"→ {out_path}")
    print(f"  {total} TC checks: {counts['PASS']} pass / {counts['FAIL']} fail "
          f"/ {counts['PENDING']} pending / {counts['MANUAL']} manual / {counts['BLOCKED']} blocked")
    return out_path


def render_html(round_name, generated, counts, total, verified, by_cat, shots_js, model, consistency, test_type=""):
    tiles = [
        ("Pass", counts["PASS"], "pass"),
        ("Fail", counts["FAIL"], "fail"),
        ("Pending capture", counts["PENDING"], "pending"),
        ("需手動驗證", counts["MANUAL"], "manual"),
        ("Blocked", counts["BLOCKED"], "blocked"),
    ]
    tiles_html = "".join(
        f'<button class="tile" data-filter="{cls}"><span class="tile-n">{n}</span>'
        f'<span class="tile-l">{esc(label)}</span></button>'
        for label, n, cls in tiles
    )

    sections = []
    for letter in [k for k in CATEGORIES if k in by_cat]:
        cat_cards = by_cat[letter]
        cards_html = "".join(render_card(c) for c in cat_cards)
        sections.append(
            f'<section class="cat" data-cat="{letter}">'
            f'<h2 class="cat-h"><span class="cat-k">Cat {letter}</span>'
            f'{esc(CATEGORIES[letter])}<span class="cat-n">{len(cat_cards)}</span></h2>'
            f'<div class="grid">{cards_html}</div></section>'
        )
    sections_html = "\n".join(sections)

    # 後續測試者手動清單：需手動 + 硬體受限
    man_rows = "".join(
        f'<tr><td class="mtc">{esc(tc)}</td><td class="mtag mtag-man">需手動</td>'
        f'<td>{esc(hint)}</td></tr>'
        for tc, hint in sorted(MANUAL.items()))
    blk_rows = "".join(
        f'<tr><td class="mtc">{esc(tc)}</td><td class="mtag mtag-blk">硬體受限</td>'
        f'<td>{esc(reason)}</td></tr>'
        for tc, reason in sorted(BLOCKED.items()))
    checklist = (
        '<details class="manlist" open><summary>後續測試者手動清單'
        f'（{len(MANUAL)} 需手動 · {len(BLOCKED)} 硬體受限）</summary>'
        '<p class="manlist-lead">以下 TC 自動化跑不到，若要驗證請照說明手動操作後重跑對應 '
        '<code>python run_ssp.py &lt;TC&gt;</code>：</p>'
        '<div class="mwrap"><table class="mtable"><tbody>'
        + man_rows + blk_rows +
        '</tbody></table></div></details>')

    # 跨 capture 一致性面板
    con_rows = "".join(
        f'<div class="con-row"><span class="con-ok con-{"y" if c["ok"] else "n"}">'
        f'{"✓" if c["ok"] else "✗"}</span>'
        f'<span class="con-lab">{esc(c["label"])}</span>'
        f'<span class="con-msg">{c["distinct"]} 種值 / {c["n"]} 次 capture'
        f'{" — 跨啟動恆定" if c["ok"] else " — 不一致，需查"}</span>'
        f'<code class="con-val">{esc(c["value"])}</code></div>'
        for c in consistency)
    con_panel = (f'<section class="con"><h2 class="con-h">跨 capture 一致性</h2>'
                 f'<p class="con-lead">同一裝置每次 bid 的識別碼應恆定；用本輪所有 capture 自動比對。</p>'
                 f'{con_rows}</section>')

    shots_json = json.dumps(shots_js)

    report_title = os.environ.get("REPORT_TITLE", "SDK_AUTOMATION - AIBID")
    return f"""<title>{esc(report_title)}</title>
<style>{CSS}</style>
<header class="top">
  <div class="top-in">
    <div class="brand">
      <div class="sig" aria-hidden="true"></div>
      <div>
        <div class="kicker">Appier SDK 開發案 · 自動化測試</div>
        <h1>{esc(report_title)}</h1>
      </div>
    </div>
    <dl class="meta">
      <div><dt>Round</dt><dd>{esc(round_name)}</dd></div>
      <div><dt>類型</dt><dd>{esc(test_type or '—')}</dd></div>
      <div><dt>Device</dt><dd>Android · {esc(model)}</dd></div>
      <div><dt>Checks</dt><dd>{total}</dd></div>
      <div><dt>Generated</dt><dd>{esc(generated)}</dd></div>
    </dl>
  </div>
  <div class="tiles">{tiles_html}
    <button class="tile tile-all is-on" data-filter="all"><span class="tile-n">{total}</span><span class="tile-l">All</span></button>
  </div>
</header>
<main>
  <p class="lead">每條 TC 一張 evidence card：規格期望對照 bid request 實際值，附該狀態的手機截圖與「本次執行」動作。
  <b>Pass/Fail</b> 為已擷取並比對（Fail = 真 SDK 缺陷）；<b>需手動驗證</b> 為 adb 自動化達不到、要人工設定的狀態；
  <b>Pending</b> 尚未在要求狀態下擷取；<b>Blocked</b> 受限於硬體。mock 的欄位會標明「真實值 → 模擬值」。</p>
  {con_panel}
  {checklist}
  {sections_html}
</main>
<div class="lightbox" id="lb" hidden><img alt="evidence screenshot" id="lb-img"><button class="lb-x" id="lb-x" aria-label="close">×</button></div>
<script>{js_block(shots_json, json.dumps(round_name))}</script>
"""


def render_card(c):
    badges = f'<span class="tier tier-{c["tier"].lower()}">{esc(c["tier"])}</span>'
    shot_html = ""
    if c["shot"]:
        matched = "" if c["shot_matched"] else ' data-unmatched="1"'
        cap_lbl = c["shot_caption"] or ""
        shot_html = (f'<button class="shot" data-shot="{esc(c["shot"])}"{matched} '
                     f'title="點擊放大">'
                     f'<img alt="{esc(c["tc"])} screenshot" data-src="{esc(c["shot"])}">'
                     f'<span class="shot-cap">{esc(cap_lbl)}</span></button>')
    repro = ""
    if c["set"]:
        repro = (f'<div class="repro"><div><span class="rl">設定狀態</span>{esc(c["set"])}</div>'
                 f'<div><span class="rl">截圖佐證</span>{esc(c["shows"])}</div></div>')
    note = ""
    if c["rd_note"]:
        note = f'<div class="note note-rd">⚑ RD gap — {esc(c["rd_note"])}</div>'
    elif c["blocked_reason"]:
        note = f'<div class="note note-bl">⛔ {esc(c["blocked_reason"])}</div>'
    elif c.get("manual_hint"):
        note = f'<div class="note note-man">🔧 需手動驗證 — {esc(c["manual_hint"])}</div>'
    action = ""
    if c.get("action"):
        action = f'<div class="action"><span class="rl">本次執行</span>{esc(c["action"])}</div>'
    ids_html = ""
    b = c.get("bid_ids") or {}
    id_parts = [f'{k} <code>{esc(b[k])}</code>' for k in ("bidobjid", "cid", "crid", "crpid") if b.get(k)]
    if id_parts:
        ids_html = ('<div class="ids"><span class="rl">本次 bid</span>'
                    + ' · '.join(id_parts) + '</div>')

    return f"""<article class="card" data-status="{c['status_cls']}" data-auto="{c['status_cls']}" data-key="{esc(c['tc'])}|{esc(c['field'])}">
  <div class="card-top">
    <span class="tc">{esc(c['tc'])}</span>
    {badges}
    <span class="pill pill-{c['status_cls']}">{esc(c['status_label'])}</span>
  </div>
  <div class="field">{esc(c['field'])}</div>
  <p class="cond">{esc(c['condition'])}</p>
  <div class="kv">
    <div class="k">Expected</div><div class="v v-exp">{esc(c['expected'])}</div>
    <div class="k">Actual</div><div class="v v-act">{esc(c['actual'])}</div>
  </div>
  {note}
  {action}
  {ids_html}
  {repro}
  {shot_html}
  <div class="edit">
    <span class="rl">手動狀態</span>
    <select class="ovr">
      <option value="">自動（{esc(c['status_label'])}）</option>
      <option value="pass">Pass</option>
      <option value="fail">Fail</option>
      <option value="pending">Pending</option>
      <option value="manual">需手動</option>
      <option value="blocked">Blocked</option>
    </select>
    <input class="ovr-note" placeholder="覆寫理由（選填）">
  </div>
</article>"""


def js_block(shots_json, round_json):
    return """
const SHOTS = %s;
const ROUND = %s;
// lazy-set thumbnails
document.querySelectorAll('.shot img[data-src]').forEach(img=>{
  const k=img.getAttribute('data-src'); if(SHOTS[k]) img.src=SHOTS[k];
});
// filter
const tiles=document.querySelectorAll('.tile');
tiles.forEach(t=>t.addEventListener('click',()=>{
  tiles.forEach(x=>x.classList.remove('is-on')); t.classList.add('is-on');
  const f=t.dataset.filter;
  document.querySelectorAll('.card').forEach(c=>{
    c.style.display=(f==='all'||c.dataset.status===f)?'':'none';
  });
  document.querySelectorAll('.cat').forEach(sec=>{
    const any=[...sec.querySelectorAll('.card')].some(c=>c.style.display!=='none');
    sec.style.display=any?'':'none';
  });
}));
// lightbox
const lb=document.getElementById('lb'), lbImg=document.getElementById('lb-img');
document.querySelectorAll('.shot').forEach(s=>s.addEventListener('click',()=>{
  const k=s.dataset.shot; if(!SHOTS[k])return;
  lbImg.src=SHOTS[k]; lb.hidden=false; lb.classList.add('open');
}));
function closeLb(){lb.classList.remove('open'); lb.hidden=true; lbImg.src='';}
document.getElementById('lb-x').addEventListener('click',closeLb);
lb.addEventListener('click',e=>{if(e.target===lb)closeLb();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeLb();});

// ── manual status override (localStorage，重整不掉；同一 artifact URL 持久) ──
const ST = {pass:'Pass', fail:'Fail', pending:'Pending', manual:'需手動', blocked:'Blocked'};
const OVR_KEY = 'appier-qa-ovr:'+ROUND;
let OVR = {};
try { OVR = JSON.parse(localStorage.getItem(OVR_KEY) || '{}'); } catch(e){ OVR = {}; }
function applyStatus(card, st){
  card.dataset.status = st;
  const pill = card.querySelector('.pill');
  if(pill){ pill.className = 'pill pill-'+st; pill.textContent = ST[st] || st; }
}
function saveOvr(k, st, n){
  if(!st && !n){ delete OVR[k]; } else { OVR[k] = {st:st, note:n}; }
  localStorage.setItem(OVR_KEY, JSON.stringify(OVR));
}
function recount(){
  const c = {pass:0,fail:0,pending:0,manual:0,blocked:0};
  const cards = document.querySelectorAll('.card');
  cards.forEach(x=>{ c[x.dataset.status] = (c[x.dataset.status]||0)+1; });
  document.querySelectorAll('.tile').forEach(t=>{
    const f=t.dataset.filter, n=t.querySelector('.tile-n');
    if(!n) return;
    if(f==='all') n.textContent = cards.length;
    else if(c[f]!==undefined) n.textContent = c[f];
  });
}
document.querySelectorAll('.card').forEach(card=>{
  const k=card.dataset.key, sel=card.querySelector('.ovr'), note=card.querySelector('.ovr-note');
  if(!sel) return;
  const o = OVR[k];
  if(o){ if(o.st){ sel.value=o.st; applyStatus(card, o.st); } if(o.note && note){ note.value=o.note; } }
  sel.addEventListener('change',()=>{
    applyStatus(card, sel.value || card.dataset.auto);
    saveOvr(k, sel.value, note ? note.value : '');
    recount();
  });
  if(note) note.addEventListener('input',()=> saveOvr(k, sel.value, note.value));
});
recount();
""" % (shots_json, round_json)


CSS = """
:root{
  --bg:#f4f6f8; --panel:#ffffff; --ink:#131a21; --ink-soft:#4a5761; --line:#dde3e9;
  --accent:#0e7c86; --accent-soft:#e3f0f1;
  --pass:#2f7d3a; --pass-bg:#e6f2e8; --fail:#c0392b; --fail-bg:#fbe9e7;
  --pend:#5b6b78; --pend-bg:#eceff2; --block:#b5761a; --block-bg:#fbf0dd;
  --man:#7a5cc4; --man-bg:#efe9fb;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans TC",sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0f1519; --panel:#161e24; --ink:#e7edf1; --ink-soft:#9fb0bc; --line:#26313a;
  --accent:#38bdc9; --accent-soft:#123037;
  --pass:#5cc46a; --pass-bg:#16281a; --fail:#f0766a; --fail-bg:#2c1613;
  --pend:#9fb0bc; --pend-bg:#1c252c; --block:#e0a94a; --block-bg:#2a2011;
  --man:#b49af0; --man-bg:#221a33;
}}
:root[data-theme="dark"]{
  --bg:#0f1519; --panel:#161e24; --ink:#e7edf1; --ink-soft:#9fb0bc; --line:#26313a;
  --accent:#38bdc9; --accent-soft:#123037;
  --pass:#5cc46a; --pass-bg:#16281a; --fail:#f0766a; --fail-bg:#2c1613;
  --pend:#9fb0bc; --pend-bg:#1c252c; --block:#e0a94a; --block-bg:#2a2011;
  --man:#b49af0; --man-bg:#221a33;
}
:root[data-theme="light"]{
  --bg:#f4f6f8; --panel:#ffffff; --ink:#131a21; --ink-soft:#4a5761; --line:#dde3e9;
  --accent:#0e7c86; --accent-soft:#e3f0f1;
  --pass:#2f7d3a; --pass-bg:#e6f2e8; --fail:#c0392b; --fail-bg:#fbe9e7;
  --pend:#5b6b78; --pend-bg:#eceff2; --block:#b5761a; --block-bg:#fbf0dd;
  --man:#7a5cc4; --man-bg:#efe9fb;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
  line-height:1.5;-webkit-font-smoothing:antialiased}
.top{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--panel) 92%,transparent);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.top-in{max-width:1180px;margin:0 auto;padding:18px 24px 12px;display:flex;
  justify-content:space-between;gap:24px;flex-wrap:wrap;align-items:flex-end}
.brand{display:flex;gap:14px;align-items:center}
.sig{width:40px;height:40px;border-radius:9px;flex:none;
  background:
    linear-gradient(var(--accent),var(--accent)) 0 50%/100% 2px no-repeat,
    radial-gradient(circle at 18% 50%,var(--accent) 3px,transparent 3.5px),
    radial-gradient(circle at 50% 22%,var(--accent) 3px,transparent 3.5px),
    radial-gradient(circle at 82% 68%,var(--accent) 3px,transparent 3.5px);
  border:1px solid var(--line);background-color:var(--accent-soft)}
.kicker{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:21px;margin:2px 0 0;letter-spacing:-.01em}
.meta{display:flex;gap:22px;margin:0;flex-wrap:wrap}
.meta div{display:flex;flex-direction:column}
.meta dt{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft)}
.meta dd{margin:0;font-family:var(--mono);font-size:13px;font-variant-numeric:tabular-nums}
.tiles{max-width:1180px;margin:0 auto;padding:6px 24px 14px;display:flex;gap:8px;flex-wrap:wrap}
.tile{cursor:pointer;border:1px solid var(--line);background:var(--panel);border-radius:9px;
  padding:8px 14px;display:flex;flex-direction:column;min-width:78px;font-family:var(--sans);
  color:var(--ink);transition:border-color .15s,transform .05s}
.tile:hover{border-color:var(--accent)}
.tile:active{transform:translateY(1px)}
.tile.is-on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.tile-n{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums;font-family:var(--mono)}
.tile-l{font-size:11px;color:var(--ink-soft);letter-spacing:.02em}
.tile[data-filter=pass] .tile-n{color:var(--pass)} .tile[data-filter=fail] .tile-n{color:var(--fail)}
.tile[data-filter=pending] .tile-n{color:var(--pend)} .tile[data-filter=blocked] .tile-n{color:var(--block)}
.tile[data-filter=manual] .tile-n{color:var(--man)}
main{max-width:1180px;margin:0 auto;padding:22px 24px 80px}
.lead{color:var(--ink-soft);font-size:14px;max-width:80ch;margin:0 0 26px;border-left:2px solid var(--accent);
  padding-left:14px}
.lead b{color:var(--ink)}
.cat{margin:0 0 34px}
.cat-h{display:flex;align-items:center;gap:12px;font-size:15px;margin:0 0 14px;
  padding-bottom:8px;border-bottom:1px solid var(--line);letter-spacing:.01em}
.cat-k{font-family:var(--mono);font-size:12px;color:var(--accent);background:var(--accent-soft);
  padding:3px 8px;border-radius:6px;letter-spacing:.04em}
.cat-n{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--ink-soft);
  font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 16px;
  display:flex;flex-direction:column;gap:10px;position:relative;overflow:hidden}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px}
.card[data-status=pass]::before{background:var(--pass)}
.card[data-status=fail]::before{background:var(--fail)}
.card[data-status=pending]::before{background:var(--pend)}
.card[data-status=manual]::before{background:var(--man)}
.card[data-status=blocked]::before{background:var(--block)}
.card-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tc{font-family:var(--mono);font-weight:700;font-size:14px;letter-spacing:.02em}
.tier{font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-soft);
  border:1px solid var(--line);border-radius:5px;padding:1px 6px}
.pill{margin-left:auto;font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;letter-spacing:.02em}
.pill-pass{color:var(--pass);background:var(--pass-bg)} .pill-fail{color:var(--fail);background:var(--fail-bg)}
.pill-pending{color:var(--pend);background:var(--pend-bg)} .pill-blocked{color:var(--block);background:var(--block-bg)}
.pill-manual{color:var(--man);background:var(--man-bg)}
.field{font-family:var(--mono);font-size:12.5px;color:var(--accent);word-break:break-all}
.cond{margin:0;font-size:12.5px;color:var(--ink-soft)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;align-items:baseline}
.kv .k{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-soft)}
.kv .v{font-family:var(--mono);font-size:12.5px;word-break:break-all;font-variant-numeric:tabular-nums}
.v-exp{color:var(--ink)} .v-act{color:var(--ink);font-weight:600}
.card[data-status=fail] .v-act{color:var(--fail)}
.card[data-status=pass] .v-act{color:var(--pass)}
.edit{display:flex;align-items:center;gap:8px;flex-wrap:wrap;border-top:1px dashed var(--line);padding-top:9px;margin-top:2px}
.edit .rl{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent)}
.edit .ovr{font-family:var(--sans);font-size:12px;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 6px}
.edit .ovr-note{flex:1;min-width:120px;font-family:var(--sans);font-size:12px;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:3px 8px}
.edit .ovr-note::placeholder{color:var(--ink-soft)}
.note{font-size:11.5px;border-radius:7px;padding:7px 10px;line-height:1.45}
.note-rd{background:var(--fail-bg);color:var(--fail)}
.note-bl{background:var(--block-bg);color:var(--block)}
.note-man{background:var(--man-bg);color:var(--man)}
.action{font-size:11.5px;color:var(--ink-soft);border-top:1px dashed var(--line);padding-top:9px}
.action .rl{display:inline-block;min-width:64px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-right:8px}
.ids{font-size:11.5px;color:var(--ink-soft);border-top:1px dashed var(--line);padding-top:9px;line-height:1.7}
.ids .rl{display:inline-block;min-width:64px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-right:8px}
.ids code{font-family:var(--mono);font-size:11px;background:var(--bg);padding:1px 5px;border-radius:4px;word-break:break-all}
.manlist{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:0 0 26px}
.manlist summary{cursor:pointer;font-weight:600;font-size:14px;color:var(--man)}
.manlist-lead{font-size:12.5px;color:var(--ink-soft);margin:10px 0}
.manlist code{font-family:var(--mono);font-size:11.5px;background:var(--bg);padding:1px 5px;border-radius:4px}
.mwrap{overflow-x:auto}
.mtable{border-collapse:collapse;width:100%;font-size:12.5px}
.mtable td{padding:6px 10px;border-top:1px solid var(--line);vertical-align:top}
.mtc{font-family:var(--mono);font-weight:600;white-space:nowrap}
.mtag{white-space:nowrap;font-size:10px;letter-spacing:.04em;border-radius:5px;padding:2px 7px}
.mtag-man{color:var(--man);background:var(--man-bg)} .mtag-blk{color:var(--block);background:var(--block-bg)}
.con{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:0 0 26px}
.con-h{font-size:14px;margin:0;color:var(--accent)}
.con-lead{font-size:12.5px;color:var(--ink-soft);margin:6px 0 12px}
.con-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-top:1px solid var(--line);flex-wrap:wrap}
.con-ok{font-weight:700;width:18px;text-align:center}
.con-y{color:var(--pass)} .con-n{color:var(--fail)}
.con-lab{font-weight:600;font-size:13px;min-width:180px}
.con-msg{font-size:12px;color:var(--ink-soft);flex:1}
.con-val{font-family:var(--mono);font-size:11.5px;background:var(--bg);padding:2px 7px;border-radius:5px;word-break:break-all}
.repro{display:flex;flex-direction:column;gap:5px;font-size:11.5px;color:var(--ink-soft);
  border-top:1px dashed var(--line);padding-top:9px}
.repro .rl{display:inline-block;min-width:64px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--accent);margin-right:8px}
.shot{margin-top:2px;padding:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;
  cursor:zoom-in;background:var(--bg);display:block;width:100%;text-align:left}
.shot img{display:block;width:100%;max-height:150px;object-fit:cover;object-position:top}
.shot[data-unmatched] img{filter:grayscale(.5) opacity(.7)}
.shot-cap{display:block;font-size:10px;color:var(--ink-soft);padding:4px 8px;background:var(--panel)}
.lightbox{position:fixed;inset:0;z-index:50;background:rgba(6,10,13,.85);display:none;
  align-items:center;justify-content:center;padding:30px}
.lightbox.open{display:flex}
.lightbox img{max-width:min(440px,90vw);max-height:90vh;border-radius:10px;
  box-shadow:0 20px 60px rgba(0,0,0,.5)}
.lb-x{position:absolute;top:18px;right:22px;width:40px;height:40px;border-radius:50%;border:none;
  background:rgba(255,255,255,.14);color:#fff;font-size:24px;cursor:pointer;line-height:1}
.lb-x:hover{background:rgba(255,255,255,.26)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:640px){.top-in{padding:14px 16px 10px}.tiles{padding:6px 16px 12px}main{padding:18px 16px 60px}
  .meta{gap:14px}}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("round_dir", help="evidence round 資料夾")
    ap.add_argument("--out", help="輸出 HTML 路徑（預設 <round_dir>/report.html）")
    args = ap.parse_args()
    out = args.out or os.path.join(args.round_dir, "report.html")
    build(args.round_dir, out)


if __name__ == "__main__":
    main()
