#!/usr/bin/env python3
"""build_artifact_ios.py — iOS round 的 QA 報告產生器。

與 AOS（build_artifact.py）**同一套翻卡 UI**：重用其 render_card / CSS / js_block，
每張 TC 卡片正面是結果、背面是證據（含該 capture 的 phone.png 截圖）。差別只在資料
來源是 ios_bid_inspector 的 IOS-xx（會自動解 ext_enc / req_enc）。

用法：
    python build_artifact_ios.py <round_dir> [--out /path/report.html]
輸出 content-only HTML（無 <html>/<body> 外殼），build_platform.py 對 IOS_ 開頭的
round 會自動改用本產生器。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ios_bid_inspector as ibi                                    # noqa: E402
from build_artifact import (                                       # noqa: E402
    encode_shot, esc, fmt_val, CATEGORIES, render_card, js_block, CSS,
    FIELD_SCHEMA, tier_of,
)

# iOS 專屬欄位（不在 Android FIELD_SCHEMA）的使用者導向 schema：
#   field -> (signal 名稱, 型別, 格式, 備註)
IOS_FIELD_SCHEMA = {
    "req.compliance.gdpr_applies":          ("GDPR 適用", "integer", "0 / 1", ""),
    "req.compliance.force_gdpr_applies":    ("強制 GDPR 覆寫", "integer", "0 / 1", ""),
    "req.compliance.current_consent_status": ("使用者同意狀態", "integer", "consent status", ""),
    "req.compliance.coppa_applies":         ("COPPA 適用", "integer", "0 / 1", ""),
    "req.app.ver":                          ("App 版本", "string", "app version", ""),
    "req.app.bundle":                       ("App Bundle ID", "string", "bundle identifier", ""),
    "req.app.sdk_version":                  ("SDK 版本", "string", "semver", ""),
    "req.app.displaymanager":               ("Display Manager", "string", "", ""),
    "req.app.displaymanagerver":            ("Display Manager 版本", "string", "", ""),
    "skadn.skadnetids":                     ("SKAdNetwork IDs", "array(string)", "iOS-only", ""),
    "skadn.sourceapp":                      ("SKAdNetwork Source App", "string", "iOS-only", ""),
    "zone_id":                              ("Zone ID", "string", "", ""),
    "req_ver":                              ("Request 版本", "integer", "", ""),
    "test_mode":                            ("測試模式旗標", "—", "應缺席", ""),
    "device.ext.att_status":                ("ATT 授權狀態", "string", "iOS-only", ""),
}
_SCHEMA = {**FIELD_SCHEMA, **IOS_FIELD_SCHEMA}

# IOS-xx → 分類字母（沿用 AND-xx 號碼對應）
from build_artifact import CAT_OF                                  # noqa: E402
CAT_OF_IOS = {"IOS-" + k.split("-", 1)[1]: v for k, v in CAT_OF.items()}
CAT_OF_IOS.setdefault("IOS-85", "A")

STATUS = {"PASS": ("pass", "PASS"), "FAIL": ("fail", "FAIL"), "CAL": ("blocked", "待校準")}


def _ts_ms(ts):
    try:
        return int(datetime.strptime(ts, "%Y%m%d_%H%M%S").timestamp() * 1000)
    except Exception:
        return None


def load_captures(round_dir):
    caps = {}
    for entry in sorted(os.scandir(round_dir), key=lambda e: e.name):
        if not entry.is_dir() or not os.path.exists(os.path.join(entry.path, "results.json")):
            continue
        try:
            meta = json.load(open(os.path.join(entry.path, "results.json")))
        except Exception:
            continue
        cap = {"meta": meta, "dir": entry.path, "bid": None, "shots": {}}
        bidp = os.path.join(entry.path, "bid_request.json")
        if os.path.exists(bidp):
            try:
                cap["bid"] = json.load(open(bidp))
            except Exception:
                pass
        # 收集所有截圖：phone.png（廣告當下）+ state_proof_<group>.png（設定當下）
        for fn in sorted(os.listdir(entry.path)):
            if fn == "phone.png":
                key = "phone"
            elif fn.startswith("state_proof_") and fn.endswith(".png"):
                key = "proof::" + fn[len("state_proof_"):-len(".png")]
            else:
                continue
            try:
                cap["shots"][key] = encode_shot(os.path.join(entry.path, fn))
            except Exception:
                pass
        caps[entry.name] = cap
    return caps


# ── validator 中繼資料查找（check/expected/note/cal…）─────────────────────────
_VMETA = {(v["tc"], v["field"]): v for v in ibi.IOS_VALIDATORS}


def _expected_disp(v):
    chk = v.get("check", "")
    if chk == "value_or_absent":
        return f"{fmt_val(v.get('expected'))} 或缺席"
    if "expected" in v:
        return fmt_val(v["expected"])
    return {
        "uuid_ci_nonzero": "非零 UUID（大寫，IDFA/IDFV）",
        "regex": "符合格式" + (f"（{v['pattern'].pattern}）" if v.get("pattern") else ""),
        "nonempty": "非空", "nonempty_notunknown": "非空、非 unknown",
        "present": "欄位存在", "array": "陣列", "array_nonempty": "非空陣列",
        "array_timestamp": "13-digit ms 時間戳陣列", "array_number": "數值陣列",
        "array_impression": "impression 結構陣列", "array_regex": "字串陣列（符合格式）",
        "int_range": f"整數 {v.get('min')}–{v.get('max')}", "range": f"{v.get('min')}–{v.get('max')}",
        "positive_int": "正整數", "ipv4_nonzero": "合法非零 IPv4",
        "int_zero_or_absent": "整數 0 或缺席", "absent": "缺席", "absent_or_empty": "缺席/空",
        "falsy": "缺席或空陣列", "leq_field": f"≤ {v.get('ref_field')}",
        "vpn_active": "非空 VPN 協定字串", "timestamp_recent": "近期 13-digit ms 時間戳",
    }.get(chk, chk)


def _provenance(field):
    if field.startswith(("device.", "user.")):
        return "ext_enc（data-signal，已解碼）"
    if field.startswith("req.") or field.startswith("skadn"):
        return "req_enc（已解碼）"
    return "明文 body"


def _clean(note):
    """去掉內部校準標記（[待校準]、RD gap…），留給使用者看的乾淨文字。"""
    s = (note or "").replace("[待校準] ", "")
    s = re.sub(r"[，,、]?\s*RD gap\s*", "", s)      # 去 "RD gap" 及前置分隔
    s = s.replace("（）", "").replace("()", "").replace("（，", "（").strip()
    return s


def _card_from_result(r, capture_name, cap_shots):
    """把一條 IOS-xx 驗證結果組成 render_card 需要的卡片 dict（使用者導向，無內部標籤）。
    截圖不內嵌到每張卡（會 52× 重複）；只放 shot key，圖片存 SHOTS map 由 JS 共享填入。
    狀態切換類 TC 優先用該狀態的設定截圖（state_proof_<group>），否則用廣告當下 phone.png。"""
    field = r["field"]
    tc = r["tc"]
    # 選截圖：狀態 TC 且該 capture 有對應 group 的設定截圖 → 用它（附「設定當下」說明）
    st = ibi.IOS_STATE.get(tc)
    shot_key, shot_cap = "", ""
    if st and ("proof::" + st[0]) in cap_shots:
        shot_key = f"{capture_name}::proof::{st[0]}"
        shot_cap = st[2]                       # 這狀態的截圖該證明什麼
    elif "phone" in cap_shots:
        shot_key = f"{capture_name}::phone"
        shot_cap = "bid 當下 app 畫面"
    v = _VMETA.get((r["tc"], field), {})
    if r["passed"]:
        status = "PASS"
    elif v.get("cal"):
        status = "CAL"
    else:
        status = "FAIL"
    status_cls, status_label = STATUS[status]
    # signal 名稱 / 型別 / 格式 / schema 備註：跟 AOS 同一份 FIELD_SCHEMA（+iOS 補充）
    signal, schema_type, schema_format, schema_note = _SCHEMA.get(
        field, (field, "—", "—", ""))
    expected = _expected_disp(v)
    actual_disp = fmt_val(r["actual"])
    if status == "PASS":
        explanation = f"bid request 的 {field} = {actual_disp}，符合預期「{expected}」，判定 Pass。"
    elif status == "FAIL":
        explanation = f"bid request 的 {field} = {actual_disp}，不符合預期「{expected}」，判定 Fail。"
    else:
        explanation = _clean(r.get("note", "")) or "本欄位本輪暫時無法驗證，條件補齊後可重測。"
    return {
        "tc": r["tc"], "field": field, "cat": CAT_OF_IOS.get(r["tc"], "D"),
        "tier": tier_of(v.get("check", ""), r["tc"]),
        "shot": shot_key,
        "shot_matched": True,
        "shot_caption": shot_cap,
        "shot_data": "",   # 不內嵌；由 SHOTS map + JS 依 data-shot 填入（去重）
        "set": "", "shows": "",
        "rd_note": "",
        "blocked_reason": (_clean(r.get("note", "")) if status == "CAL" else ""),
        "manual_hint": "",
        "action": "",
        "bid_ids": {},
        "capture": capture_name,
        "actual": actual_disp,
        "ground_truth": None,
        "attempts": [],
        "status_cls": status_cls, "status_label": status_label,
        "expected": expected,
        "signal": signal,
        "schema_type": schema_type,
        "schema_format": schema_format,
        "schema_note": schema_note,
        "absent_reason": None, "mock_cmd": None, "mock_reset": None,
        "provenance": _provenance(field),
        "evidence_explanation": explanation,
        "condition": schema_note or "以 bid request 實際值比對 Golden 期望。",
    }


def evaluate_round(caps):
    """每個 capture 依宣告範圍重算、latest-wins 合併，回傳 (cards, counts, not_run)。"""
    merged = {}   # (tc, field) -> card
    for name in sorted(caps, key=lambda n: caps[n]["meta"].get("captured_at", "")):
        cap = caps[name]
        if cap["bid"] is None:
            continue
        tc_id = cap["meta"].get("tc_id", "BASELINE")
        tc_filter = ibi.AUTO_TCS if tc_id == "BASELINE" else set(tc_id.split(","))
        ref_ms = _ts_ms(cap["meta"].get("captured_at", ""))
        for r in ibi.run_inspection(cap["bid"], tc_filter, reference_ms=ref_ms):
            merged[(r["tc"], r["field"])] = _card_from_result(r, name, cap["shots"])

    cards = []
    for v in ibi.IOS_VALIDATORS:
        if v["check"] == "session_case":
            continue
        c = merged.get((v["tc"], v["field"]))
        if c is not None and c not in cards:
            cards.append(c)
    counts = {"pass": 0, "fail": 0, "blocked": 0}
    for c in cards:
        counts[c["status_cls"]] = counts.get(c["status_cls"], 0) + 1
    covered = {c["tc"] for c in cards}
    not_run = sorted({v["tc"] for v in ibi.IOS_VALIDATORS
                      if v["check"] != "session_case" and v["tc"] not in covered})
    return cards, counts, not_run


def render_html(round_name, cards, counts, not_run, caps, environment, meta):
    title = "SDK_AUTOMATION iOS — " + " · ".join(
        x.upper() for x in (meta["test_mode"], meta["test_type"]) if x)
    total = len({c["tc"] for c in cards})
    # 所有截圖（phone + state_proof）只各存一份（SHOTS map），卡片依 key 共享
    shots = {}
    for name, cap in caps.items():
        for key, data in cap.get("shots", {}).items():
            shots[f"{name}::{key}"] = data

    tiles = [("Pass", counts.get("pass", 0), "pass"),
             ("Fail", counts.get("fail", 0), "fail"),
             ("待補 / 未送", counts.get("blocked", 0), "blocked")]
    tiles_html = "".join(
        f'<button class="tile" data-filter="{cls}"><span class="tile-n">{n}</span>'
        f'<span class="tile-l">{esc(lbl)}</span></button>'
        for lbl, n, cls in tiles)
    tiles_html += (f'<button class="tile tile-all is-on" data-filter="all">'
                   f'<span class="tile-n">{total}</span><span class="tile-l">All</span></button>')

    by_cat = {}
    for c in cards:
        by_cat.setdefault(c["cat"], []).append(c)
    sections = []
    for letter in [k for k in CATEGORIES if k in by_cat]:
        cat_cards = by_cat[letter]
        cards_html = "".join(render_card(c) for c in cat_cards)
        sections.append(
            f'<section class="cat" id="cat-{letter}" data-cat="{letter}">'
            f'<h2 class="cat-h"><span class="cat-k">Cat {letter}</span>'
            f'{esc(CATEGORIES[letter])}<span class="cat-n">{len(cat_cards)}</span></h2>'
            f'<div class="grid">{cards_html}</div></section>')
    sections_html = "\n".join(sections)

    env_rows = "".join(
        f'<div><span>{esc(label)}</span><strong>{esc(str(environment.get(key) or "—"))}</strong></div>'
        for label, key in (("App", "bundle_id"), ("Device", "device"),
                           ("裝置名稱", "device_name"), ("iOS", "os_version"),
                           ("Build", "build_fingerprint"), ("Timezone", "timezone"),
                           ("Locale", "locale"), ("環境來源", "env_source")))

    notrun_html = ""
    if not_run:
        notrun_html = (f'<details class="manlist" open><summary>未擷取 TC（{len(not_run)}）'
                       f'—— 狀態切換類，需以指定 TC 單獨 capture（run_ssp_ios.py IOS-xx）</summary>'
                       f'<p class="manlist-lead">{esc("、".join(not_run))}</p></details>')

    return f"""<title>{esc(title)}</title>
<style>{CSS}</style>
<header class="top">
  <div class="top-in">
    <div class="brand">
      <div class="sig" aria-hidden="true"></div>
      <div>
        <div class="kicker">Appier SDK 開發案 · 自動化測試（iOS）</div>
        <h1>{esc(title)}</h1>
      </div>
    </div>
    <dl class="meta">
      <div><dt>Round</dt><dd>{esc(round_name)}</dd></div>
      <div><dt>類型</dt><dd>{esc(meta['test_type'] or '—')}</dd></div>
      <div><dt>整合模式</dt><dd>{esc(meta['test_mode'] or '—')}</dd></div>
      <div><dt>Test CID</dt><dd>{esc(meta['test_cid'] or '—')}</dd></div>
      <div><dt>執行人</dt><dd>{esc(meta['test_executor'] or '—')}</dd></div>
      <div><dt>Device</dt><dd>iOS · {esc(meta['model'])}</dd></div>
      <div><dt>Signal TC</dt><dd>{total}</dd></div>
      <div><dt>Generated</dt><dd>{esc(meta['generated'])}</dd></div>
    </dl>
  </div>
  <div class="tiles" data-tabtiles="signal">{tiles_html}</div>
</header>
<main>
  <section class="setup-cards">
    <article><h2>測試環境</h2><div class="setup-grid">{env_rows}</div></article>
  </section>
  <p class="lead">iOS Signal 驗證（<b>IOS-xx</b>，由 ios_bid_inspector 自動解 ext_enc / req_enc
  後比對）。每張卡片正面是判定、<b>翻到背面看證據</b>（含 bid 當下 app 截圖、來源 capture、
  BID request 實際值）。<b>待補 / 未送</b> = iOS SDK 目前未送出、或期望值待確認的欄位。</p>
  {notrun_html}
  {sections_html}
</main>
<div class="lightbox" id="lb" hidden><img alt="evidence screenshot" id="lb-img"><button class="lb-x" id="lb-x" aria-label="close">×</button></div>
<script>{js_block(json.dumps(shots), json.dumps(round_name), "{}")}
// 縮圖去重：每張卡的 .shot 依 data-shot 從 SHOTS 共享填入（圖片只存一份）
document.querySelectorAll('.shot').forEach(function(b){{
  var im=b.querySelector('img'), k=b.getAttribute('data-shot');
  if(im && !im.getAttribute('src') && SHOTS[k]) im.src=SHOTS[k];
}});
</script>
"""


def _meta_return(out_path, round_name, meta, counts, ncaps):
    return {
        "out": out_path, "round_name": round_name,
        "test_type": meta["test_type"], "test_mode": meta["test_mode"],
        "test_cid": meta["test_cid"], "test_executor": meta["test_executor"],
        "model": meta["model"], "elapsed": None,
        "signal_total": counts.get("pass", 0) + counts.get("fail", 0) + counts.get("blocked", 0),
        "signal_counts": {"PASS": counts.get("pass", 0), "FAIL": counts.get("fail", 0),
                          "PENDING": counts.get("blocked", 0), "MANUAL": 0, "BLOCKED": 0},
        "e2e_total": 0, "e2e_score": {"PASS": 0, "FAILED": 0, "BLOCKED": 0},
    }


def build(round_dir, out_path, e2e_round=None):
    caps = load_captures(round_dir)
    if not caps:
        sys.exit(f"no capture (results.json) found under {round_dir}")
    cards, counts, not_run = evaluate_round(caps)
    round_name = os.path.basename(round_dir.rstrip("/"))
    latest = caps[max(caps, key=lambda n: caps[n]["meta"].get("captured_at", ""))]
    environment = latest["meta"].get("environment", {})
    meta = {
        "test_type": latest["meta"].get("test_type", ""),
        "test_mode": latest["meta"].get("test_mode", ""),
        "test_cid": latest["meta"].get("test_cid", ""),
        "test_executor": latest["meta"].get("test_executor", ""),
        "model": environment.get("device") or "iPhone",
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    if not cards:
        # 沒有 bid body（極端：只有 impression 識別碼）→ 仍出一頁精簡說明
        html_out = (f"<title>{esc('SDK_AUTOMATION iOS — ' + round_name)}</title>"
                    f"<style>{CSS}</style><main style='padding:24px'>"
                    f"<h1>iOS · {esc(meta['test_type'])}</h1>"
                    f"<p class='lead'>本輪未取得可驗證的 bid body（僅有 impression 識別碼）。</p></main>")
        Path(out_path).write_text(html_out, encoding="utf-8")
        print(f"→ {out_path}\n  0 checks（無 bid body）")
        return _meta_return(out_path, round_name, meta, {}, len(caps))

    html_out = render_html(round_name, cards, counts, not_run, caps, environment, meta)
    Path(out_path).write_text(html_out, encoding="utf-8")
    print(f"→ {out_path}")
    print(f"  {len(cards)} checks: {counts.get('pass',0)} pass / {counts.get('fail',0)} fail "
          f"/ {counts.get('blocked',0)} 待校準 / {len(not_run)} TC 未擷取")
    return _meta_return(out_path, round_name, meta, counts, len(caps))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("round_dir", help="iOS evidence round 資料夾")
    ap.add_argument("--out", help="輸出 HTML 路徑（預設 <round_dir>/report.html）")
    args = ap.parse_args()
    build(args.round_dir, args.out or os.path.join(args.round_dir, "report.html"))


if __name__ == "__main__":
    main()
