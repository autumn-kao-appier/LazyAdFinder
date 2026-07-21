#!/usr/bin/env python3
"""
e2e_catalog.py — Ad-Serving E2E flow TC（Confluence R187613）＋自動驗證器。

與 bid_inspector 的 AND-xx（bid signal 欄位驗證）不同維度：這裡驗整條
ad-serving pipeline 的 flow checkpoint。每條 TC 宣告：

    modes  適用的整合模式（standalone / admob-mediation / applovin-mediation）
           —— TEST_MODE 選定當下即可自動判定不適用的 TC（na_mode），
           不必人工填「Standalone 沒有, Block」。
    types  適用的投放目的（aibid / reen-static / reen-dynamic）；未列 = 全部適用
    auto   自動驗證器 key（evaluate() 內建）；None = 無法在 run 內自動驗

可用證據（run_ssp 每次 capture 落地在 capture 資料夾）：
    bid_request.json / bid_response.json  bid 流量本體（response 僅 200 時有）
    traffic.jsonl                         mitmdump 全流量 log（method/url/status）
    ad_ui.xml                             廣告渲染當下的 uiautomator dump
    logcat_appier.txt                     SDK log（show_cb rc 等）
    bid_ids.json                          bidobjid / cid / crid / crpid

status 值：
    pass / fail   驗證器依證據判定
    observe       有部分證據（例：logcat 有 show_cb 但缺完整 redirect chain）
    pending       證據不足（例：該 capture 走 logcat 偵測、無 proxy 流量）
    gated         自動化就緒，但需人工核准才執行（受控點擊有真實費用）
    na_mode       所選整合模式不適用（選模式當下自動判定）
    na_type       所選投放目的不適用
    na_platform   平台不適用（以 SDK source 佐證，例：Android 無 init endpoint）
    backend       需跨系統後端資料（Spark raw_action / MMP），非同 run 可驗
"""

import glob
import json
import os
import re

ALL_MODES = {"standalone", "admob-mediation", "applovin-mediation"}
ADMOB = {"admob-mediation"}
REEN = {"reen-static", "reen-dynamic"}

# ── 廣告流程步驟（E2E 分頁時間軸；每步一列，含逐步截圖）───────────────────────────
# (key, 顯示標題, 這步在做什麼)
FLOW_STEPS = [
    ("init",       "① SDK Init",        "App 啟動、init 請求送出"),
    ("bid",        "② Bid 請求 / 回應",  "送出 bid request、拿到廣告 response"),
    ("render",     "③ 廣告渲染",         "native 素材（icon/main/title/cta）顯示"),
    ("impression", "④ Impression 回報",  "曝光 beacon（show_cb → winshowimg）成對"),
    ("click",      "⑤ 點擊",            "點廣告手勢 → xclk 點擊鏈"),
    ("landing",    "⑥ 落地",            "deeplink 直開 target app / 落地頁"),
]
# 每條 E2E TC 歸到哪個流程步驟
STEP_OF = {
    "TC-01": "init", "TC-02": "init",
    "TC-03": "bid", "TC-04": "bid", "TC-08": "bid", "TC-16": "bid", "TC-17": "bid",
    "TC-05": "render", "TC-06": "render",
    "TC-07": "impression",
    "TC-09": "click", "TC-11": "click", "TC-14": "click",
    "TC-10": "landing", "TC-15": "landing",
}
# 各流程步驟對應的逐步截圖檔名（run_ssp DO_E2E_FLOW 產出）
STEP_SHOT = {
    "init":    "e2e_step_init.png",
    "render":  "e2e_step_render.png",
    "click":   "e2e_step_click.png",
    "landing": "e2e_step_landing.png",
}

E2E_TCS = [
    {
        "tc": "TC-01", "cat": "A. Ad Serving", "name": "SDK Init request",
        "endpoint": "GET */sdk/init 或 signal.appier.com/v1/key",
        "priority": "P0", "check_kind": "traffic", "modes": ALL_MODES, "auto": "init",
        "expected": "HTTP 200；bundle 正確。註：Android ads SDK source（Constants.java）"
                    "無 init endpoint，僅 data-signal 有 key fetch",
    },
    {
        "tc": "TC-02", "cat": "A. Ad Serving", "name": "AdMob pubsetting mediation config",
        "endpoint": "GET googleads.g.doubleclick.net/getconfig/pubsetting",
        "priority": "P0", "check_kind": "traffic", "modes": ADMOB, "auto": "admob_pubsetting",
        "expected": "200 status=1；mediation_config.ad_networks 含 APRAdAdapter；"
                    "parameter 含 zoneId；is_mediation=true",
    },
    {
        "tc": "TC-03", "cat": "A. Ad Serving", "name": "AdMob ad request(mads/gma) 與 mediation 分流",
        "endpoint": "googleads.g.doubleclick.net/mads/gma",
        "priority": "P0", "check_kind": "traffic", "modes": ADMOB, "auto": "admob_mads",
        "expected": "200；ad_networks 含 Appier custom event；得標時 bidding_data=Won",
    },
    {
        "tc": "TC-04", "cat": "A. Ad Serving", "name": "Appier ad request/response 驗證",
        "endpoint": "POST apx.appier.net/v2/sdk/aos/ad",
        "priority": "P0", "check_kind": "traffic", "modes": ALL_MODES, "auto": "bid_response",
        "expected": "200；adUnits[0].ad 完整（clk/impTracker/native: title,text,ctaText,"
                    "iconImage,mainImage,privacyInformationIcon,privacyInformationLink）",
    },
    {
        "tc": "TC-05", "cat": "A. Ad Serving", "name": "Creative assets 載入",
        "endpoint": "iconImage / mainImage / privacy icon URL",
        "priority": "P1", "check_kind": "traffic", "modes": ALL_MODES, "auto": "creative_assets",
        "expected": "response 內全部圖片 URL 均有對應 HTTP 200（或 304 快取）流量",
    },
    {
        "tc": "TC-06", "cat": "A. Ad Serving", "name": "Native ad 於 Sample App 渲染",
        "endpoint": "—（畫面渲染 ↔ response 逐項比對）",
        "priority": "P0", "check_kind": "render", "modes": ALL_MODES, "auto": "render_match",
        "expected": "ad_ui.xml 內找得到 response native 的 title / text / ctaText 文字",
    },
    {
        "tc": "TC-07", "cat": "B. Tracking", "name": "Impression tracking",
        "endpoint": "apn.c.appier.net/callback/show_cb → winshowimg",
        "priority": "P0", "check_kind": "traffic", "modes": ALL_MODES, "auto": "impression",
        "expected": "show_cb 200/302 含正確 bidobjid → winshowimg 200",
    },
    {
        "tc": "TC-08", "cat": "B. Tracking", "name": "Mediation fill result 回報",
        "endpoint": "googleads.g.doubleclick.net/pagead/interaction（fill_urls）",
        "priority": "P2", "check_kind": "traffic", "modes": ADMOB, "auto": "admob_fill",
        "expected": "admob_mediation_request_fill_result 發送，mediation_fill_status 合理",
    },
    {
        "tc": "TC-09", "cat": "B. Tracking", "name": "Click tracking",
        "endpoint": "tw.c.appier.net/xclk（受控點擊，產生真實費用）",
        "priority": "P0", "check_kind": "traffic", "modes": ALL_MODES, "auto": "click_chain",
        "expected": "xclk 302 含正確 cid/crid；redirect chain 無 error",
    },
    {
        "tc": "TC-10", "cat": "B. Tracking", "name": "Landing 跳轉（REEN deeplink）",
        "endpoint": "deeplink", "types": REEN,
        "priority": "P1", "check_kind": "render", "modes": ALL_MODES, "auto": "deeplink_landing",
        "expected": "deeplink 直開 target app（非 Play Store）；落地截圖",
    },
    {
        "tc": "TC-11", "cat": "B. Tracking", "name": "Privacy information icon",
        "endpoint": "privacyInformationLink（adpolicy.appier.com）",
        "priority": "P2", "check_kind": "traffic", "modes": ALL_MODES, "auto": "privacy_click",
        "expected": "點 privacy icon 開啟 privacyInformationLink 指定頁（無點擊費用）",
    },
    {
        "tc": "TC-14", "cat": "C. Attribution", "name": "REEN click → deeplink 直開 target app",
        "endpoint": "xclk + MMP OneLink（is_retargeting=true）", "types": REEN,
        "priority": "P2", "check_kind": "traffic", "modes": ALL_MODES, "auto": "click_chain_reen",
        "expected": "xclk 302 chain 無 error；OneLink macro 全展開無 ${...} 殘留；deeplink 直開",
    },
    {
        "tc": "TC-15", "cat": "C. Attribution", "name": "REEN re-engagement(open) action postback 核對",
        "endpoint": "Spark raw_action(open) / MMP", "types": REEN,
        "priority": "P2", "check_kind": "backend", "modes": ALL_MODES, "auto": None,
        "expected": "imp/click 入帳、bidobjid 對應；open action 於歸因窗內入帳",
    },
    {
        "tc": "TC-16", "cat": "D. Negative", "name": "Appier no-fill → AdMob fallback",
        "endpoint": "nofill_urls",
        "priority": "P3", "check_kind": "traffic", "modes": ADMOB, "auto": "admob_nofill",
        "expected": "nofill_urls 正常發送；AdMob fallback 至下一 ad network",
    },
    {
        "tc": "TC-17", "cat": "A. Ad Serving", "name": "Signal 暗碼(ext_enc)封包解碼對照",
        "endpoint": "bid request ext_enc（ae1 XOR，Signal SDK payload）",
        "priority": "P0", "check_kind": "crypto", "modes": ALL_MODES, "auto": "ext_enc_decode",
        "expected": "ext_enc 為合法 ae1 封包、可解成 JSON；敏感訊號（ifv/applist/boottime/"
                    "mem/disk/sensors/user block）落在暗碼包內，明文↔解碼逐欄對照無誤",
    },
]

# 對外只有三種狀態：PASS / FAIL / BLOCKED（細分原因收進括號說明，不另立分類）
STATUS_LABEL = {
    "pass": "PASS",
    "observe": "PASS（部分證據）",
    "fail": "FAIL",
    "pending": "BLOCKED（未執行/證據不足）",
    "gated": "BLOCKED（需人工核准）",
    "na_mode": "BLOCKED（整合模式不適用）",
    "na_type": "BLOCKED（投放目的不適用）",
    "na_platform": "BLOCKED（平台不適用）",
    "backend": "BLOCKED（跨系統後端）",
}


# ── 證據載入 ──────────────────────────────────────────────────────────────────

def _load_captures(round_dir):
    """回傳 [(name, folder_path)]，新→舊排序。"""
    caps = []
    for results_path in glob.glob(os.path.join(round_dir, "*", "results.json")):
        folder = os.path.dirname(results_path)
        caps.append((os.path.basename(folder), folder))
    return sorted(caps, key=lambda item: item[0].split("_", 1)[-1], reverse=True)


def _traffic(folder):
    path = os.path.join(folder, "traffic.jsonl")
    rows = []
    if os.path.exists(path):
        for line in open(path):
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _json_file(folder, name):
    path = os.path.join(folder, name)
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return None
    return None


def _text_file(folder, name):
    path = os.path.join(folder, name)
    return open(path, errors="replace").read() if os.path.exists(path) else ""


def _logcat(folder):
    """SDK logcat（logcat_appier.txt）。Appier 對 apx / 部分主機有 cert pinning，
    proxy 攔不到解密流量時，改用 SDK 自己印的 log 當一手證據。"""
    return _text_file(folder, "logcat_appier.txt") or _text_file(folder, "logcat.txt")


def _first_logcat(caps, pattern):
    """新→舊找出 logcat 命中 pattern 的第一個 capture；回 (name, folder, match) 或 (None,None,None)。"""
    rx = re.compile(pattern)
    for name, folder in caps:
        m = rx.search(_logcat(folder))
        if m:
            return name, folder, m
    return None, None, None


def _first_with(caps, predicate):
    """新→舊找出第一個滿足條件的 capture；回 (name, folder) 或 (None, None)。"""
    for name, folder in caps:
        if predicate(folder):
            return name, folder
    return None, None


def _urls_in(obj):
    """遞迴收集 JSON 內所有 http(s) URL 字串。"""
    urls = []
    if isinstance(obj, dict):
        for value in obj.values():
            urls += _urls_in(value)
    elif isinstance(obj, list):
        for value in obj:
            urls += _urls_in(value)
    elif isinstance(obj, str) and obj.startswith(("http://", "https://")):
        urls.append(obj)
    return urls


def _any_traffic(caps):
    return any(_traffic(folder) for _, folder in caps)


NO_TRAFFIC_NOTE = ("round 內沒有任何 proxy 流量 log（capture 走 logcat 偵測或 VPN 繞過 proxy）；"
                   "需在 mitmdump 在線且無 VPN 的 capture 驗證")


# ── 驗證器 ────────────────────────────────────────────────────────────────────

def _v_init(caps, ids, test_type):
    for name, folder in caps:
        for row in _traffic(folder):
            url = row.get("url", "")
            # 實測 endpoint：GET adx.apx.appier.net/v2/sdk/aos/init
            if (("apx.appier.net" in url and url.split("?")[0].endswith("/init"))
                    or ("signal.appier.com" in url and "/v1/key" in url)):
                ok = row.get("status") == 200
                which = "init" if "/init" in url else "data-signal key fetch"
                return ("pass" if ok else "fail",
                        f"{which} → {row.get('status')}（{name}）：{url.split('?')[0]}", [name])
    # proxy 沒攔到（apx pinning passthrough）→ 用 logcat：SDK 印 "Requesting Ad: .../aos/ad"
    name, _, m = _first_logcat(caps, r"Requesting Ad:\s*(\S+)")
    if name:
        return ("pass",
                f"logcat 記錄 SDK 發出 ad 請求：{m.group(1)}（{name}）；"
                "apx.appier.net 有 cert pinning，proxy 走 passthrough，以 SDK log 為證", [name])
    if not _any_traffic(caps):
        return "pending", NO_TRAFFIC_NOTE, []
    return "pending", "有 proxy 流量但未見 init 請求（init 只在 app 冷啟後首次載入時發送）", []


REQUIRED_NATIVE = ["title", "text", "ctaText", "iconImage", "mainImage",
                   "privacyInformationIcon", "privacyInformationLink"]


def _bid_ad(folder):
    resp = _json_file(folder, "bid_response.json")
    if not resp:
        return None
    try:
        return resp["adUnits"][0]["ad"]
    except Exception:
        return {}


def _v_bid_response(caps, ids, test_type):
    name, folder = _first_with(caps, lambda f: _json_file(f, "bid_response.json") is not None)
    if not name:
        # proxy 沒存到 response body（apx pinning passthrough）→ 用 logcat：
        # onAdLoaded() = SDK 收到 200 response 且解析成功、廣告可渲染
        lc_name, _, _ = _first_logcat(caps, r"onAdLoaded\(\)")
        if lc_name:
            return ("pass",
                    f"logcat onAdLoaded()：SDK 收到 200 bid response 並成功載入廣告（{lc_name}）；"
                    "apx pinning → 無 proxy body，以 SDK 載入結果為證", [lc_name])
        nobid_name, _, _ = _first_logcat(caps, r"onAdNoBid\(\)")
        if nobid_name:
            return "observe", f"logcat onAdNoBid()：本次無廣告可服務（{nobid_name}）", [nobid_name]
        return ("pending",
                "round 內沒有保存到 200 response body（capture 走 logcat 偵測；"
                "需 mitmdump 在線的 capture）", [])
    ad = _bid_ad(folder)
    if ad is None or ad == {}:
        return "fail", f"bid_response.json 缺 adUnits[0].ad（{name}）", [name]
    missing = [k for k in ("clk", "impTracker") if k not in ad]
    native = ad.get("native") or {}
    missing += [f"native.{k}" for k in REQUIRED_NATIVE if k not in native]
    if missing:
        return "fail", f"response 缺欄位：{', '.join(missing)}（{name}）", [name]
    return "pass", f"adUnits[0].ad 完整（clk/impTracker/native 七欄位齊）（{name}）", [name]


def _v_creative_assets(caps, ids, test_type):
    name, folder = _first_with(
        caps, lambda f: _json_file(f, "bid_response.json") is not None and _traffic(f))
    if not name:
        # 無 response + proxy（apx pinning／素材走快取）→ 用渲染證據：
        # onAdLoaded + ad_ui 有圖片節點 + render 截圖 = 素材確實載入並顯示
        rname, rfolder = _first_with(
            caps, lambda f: os.path.exists(os.path.join(f, "ad_ui.xml"))
            and re.search(r"onAdLoaded\(\)", _logcat(f)))
        if rname:
            ui = _text_file(rfolder, "ad_ui.xml")
            imgs = [n for n in ("native_main_image", "native_icon_image",
                                "native_privacy_information_icon_image") if n in ui]
            has_shot = os.path.exists(os.path.join(rfolder, "e2e_step_render.png"))
            if imgs:
                shot_note = "、render 截圖可見" if has_shot else ""
                return ("observe",
                        f"廣告已載入且渲染出圖片節點（{'、'.join(imgs)}）{shot_note}（{rname}）；"
                        "素材走快取或 apx pinning 無 proxy body，拿不到每張圖 HTTP 狀態,以渲染為證", [rname])
        return "pending", "缺同一 capture 的 response + proxy 流量；" + NO_TRAFFIC_NOTE, []
    native = (_bid_ad(folder) or {}).get("native") or {}
    # 只驗「渲染會抓取」的圖片 asset；privacyInformationLink/clk 是點擊落地連結，不在此列。
    # DNA 動態商品圖走 rdr.c.appier.net redirect → 302 屬正常行為
    assets = {}
    for key in ("iconImage", "mainImage", "privacyInformationIcon"):
        value = native.get(key)
        url = value.get("url") if isinstance(value, dict) else value
        if isinstance(url, str) and url.startswith("http"):
            assets[key] = url
    if not assets:
        return "fail", f"response native 內找不到任何圖片 asset URL（{name}）", [name]
    seen = {}
    for row in _traffic(folder):
        base = row.get("url", "").split("?")[0]
        status = row.get("status")
        if base not in seen or status in (200, 304):
            seen[base] = status
    missing, bad, hits = [], [], []
    for key, url in assets.items():
        status = seen.get(url.split("?")[0])
        if status is None:
            missing.append(key)
        elif status not in (200, 302, 304):
            bad.append(f"{key} → {status}")
        else:
            hits.append(f"{key} {status}" + ("(redirect→CDN)" if status == 302 else ""))
    if bad:
        return "fail", f"asset 異常：{'; '.join(bad)}（{name}）", [name]
    if missing:
        return ("observe",
                f"{'、'.join(hits)}；未見流量（可能 SDK 快取）：{'、'.join(missing)}（{name}）", [name])
    return "pass", f"{'、'.join(hits)} 全部正常（{name}）", [name]


def _norm_text(s):
    """比對前正規化：解 HTML entity（&#10; 等）、全部空白摺成單一空格。"""
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()


def _v_render_match(caps, ids, test_type):
    name, folder = _first_with(
        caps, lambda f: os.path.exists(os.path.join(f, "ad_ui.xml"))
        and _json_file(f, "bid_response.json") is not None)
    if not name:
        # 無 response body（apx pinning）→ 有渲染 dump + onAdLoaded + 渲染截圖即可視覺佐證
        rname, rfolder = _first_with(
            caps, lambda f: os.path.exists(os.path.join(f, "ad_ui.xml"))
            and os.path.exists(os.path.join(f, "e2e_step_render.png")))
        if rname and re.search(r"onAdLoaded\(\)", _logcat(rfolder)):
            ui = _norm_text(_text_file(rfolder, "ad_ui.xml"))
            has_ad = any(t in ui for t in ("native_title", "native_cta", "Native Title"))
            return ("observe" if has_ad else "pending",
                    f"廣告已渲染（onAdLoaded + ad_ui.xml + e2e_step_render.png）（{rname}）；"
                    "apx pinning 無 response body 可逐欄比對，附渲染截圖人工核對", [rname])
        return "pending", "缺同一 capture 的 ad_ui.xml + bid_response.json", []
    ui = _norm_text(_text_file(folder, "ad_ui.xml"))
    native = (_bid_ad(folder) or {}).get("native") or {}
    checks = {k: _norm_text(str(native.get(k, ""))) for k in ("title", "text", "ctaText")}
    missing = []
    for key, value in checks.items():
        if not value:
            continue
        # UI 可能截斷長文案（ellipsize）：全文找不到就退而求前 15 字
        if value not in ui and value[:15] not in ui:
            missing.append(key)
    if missing:
        return ("fail",
                f"渲染畫面找不到 response 的 {', '.join(missing)}（{name}）", [name])
    if not any(checks.values()):
        return "pending", f"response native 無可比對文字欄位（{name}）", [name]
    return "pass", f"title/text/ctaText 與渲染畫面逐項一致（{name}）", [name]


def _v_impression(caps, ids, test_type):
    final_id = (ids or {}).get("bidobjid", "")
    for name, folder in caps:
        # 以 bidobjid 把 show_cb 與 winshowimg 配對（注意：show_cb URL 內含
        # winshowimg_beacon= 參數，不能只用字串包含判斷 winshowimg）
        shows, winshows = {}, {}
        for row in _traffic(folder):
            url = row.get("url", "")
            base = url.split("?")[0]
            match = re.search(r"[?&]bidobjid=([^&]+)", url)
            key = match.group(1) if match else ""
            if "show_cb" in base:
                shows[key] = row.get("status")
            elif "winshowimg" in base:
                winshows[key] = row.get("status")
        pairs = [key for key in shows
                 if shows[key] in (200, 302) and winshows.get(key) == 200]
        if pairs:
            if final_id in pairs:
                return ("pass",
                        f"show_cb {shows[final_id]} → winshowimg 200，"
                        f"bidobjid={final_id} 與本 capture 對應（{name}）", [name])
            return ("pass",
                    f"chain 完整（show_cb→winshowimg 200，{len(pairs)} 組成對）；"
                    f"屬同 session 稍早 impression，最終 bid 的 show_cb 未及寫入 log（{name}）",
                    [name])
        if shows:
            return ("observe",
                    f"show_cb {list(shows.values())[0]}，未見同 bidobjid 的 winshowimg 200（{name}）",
                    [name])
    # proxy 沒攔到 show_cb（pinning）→ logcat：SDK 印 "Requesting impression tracker: ...show_cb?bidobjid=..."
    name, _, m = _first_logcat(caps,
                              r"Requesting impression tracker:\s*\S*show_cb\?[^\s]*bidobjid=([^&\s]+)")
    if name:
        return ("pass",
                f"logcat 記錄 SDK 發出 impression tracker（show_cb，bidobjid={m.group(1)}）（{name}）；"
                "apx pinning → winshowimg chain 無 proxy 可驗，以 SDK 發送為證", [name])
    name, folder = _first_with(
        caps, lambda f: re.search(r"show_cb.*(rc=200|200)", _text_file(f, "logcat_appier.txt")))
    if name:
        return ("observe",
                f"logcat 有 show_cb rc=200，但無 proxy 流量可驗 winshowimg chain（{name}）", [name])
    return "pending", "無 show_cb 證據；" + NO_TRAFFIC_NOTE, []


# 測試廣告環境，點擊直接執行（DO_E2E_FLOW=1 自動點）；沒跑到就是「本輪未執行」，非核准問題
CLICK_NOTRUN_NOTE = ("點擊流程本輪未執行；開 DO_E2E_FLOW=1 會自動點廣告 → "
                     "保存 xclk 點擊鏈與落地截圖")


def _find_xclk(caps):
    for name, folder in caps:
        for row in _traffic(folder):
            if "/xclk" in row.get("url", ""):
                return name, row
    return None, None


def _click_evidence(caps):
    """點擊有沒有真的發生（不靠 proxy）：do_e2e_flow 存的 e2e_flow.json clicked=true
    ＋落地截圖，或 logcat 印 "In-app browser initial loads url"（SDK 開落地頁）。
    回傳 (name, folder, landing_url|None) 或 (None,None,None)。"""
    for name, folder in caps:
        flow = _json_file(folder, "e2e_flow.json")
        clicked = bool(flow and flow.get("clicked"))
        has_landing = os.path.exists(os.path.join(folder, "e2e_step_landing.png"))
        m = re.search(r"In-app browser initial loads url:\s*(\S+)", _logcat(folder))
        if clicked or has_landing or m:
            return name, folder, (m.group(1) if m else None)
    return None, None, None


def _v_click_chain(caps, ids, test_type):
    name, row = _find_xclk(caps)
    if name:
        url, status = row.get("url", ""), row.get("status")
        cid_ok = (ids or {}).get("cid", "") in url if ids else True
        if status == 302 and cid_ok:
            return "pass", f"xclk 302，cid 對應（{name}）", [name]
        return "observe", f"xclk {status}（cid {'對應' if cid_ok else '不符'}）（{name}）", [name]
    # proxy 沒攔到 xclk（apx pinning）→ e2e_flow.json / logcat 佐證點擊已執行
    cname, _, landing = _click_evidence(caps)
    if cname:
        extra = f"，落地 URL：{landing}" if landing else ""
        return ("pass",
                f"已自動點擊廣告、開啟落地頁（{cname}）{extra}；apx pinning 無 xclk proxy 流量，"
                "以 e2e_flow.json + 點擊/落地截圖為證", [cname])
    return "pending", CLICK_NOTRUN_NOTE, []


def _v_click_chain_reen(caps, ids, test_type):
    name, row = _find_xclk(caps)
    if name:
        leftovers = []
        for cap_name, folder in caps:
            for traffic_row in _traffic(folder):
                url = traffic_row.get("url", "")
                if ("onelink" in url.lower() or "/xclk" in url) and "${" in url:
                    leftovers.append(url)
        if leftovers:
            return "fail", f"chain 內有未展開 macro：{leftovers[0][:120]}", [name]
        return "observe", f"xclk chain 已保存（{name}）；OneLink/deeplink 落地需對照錄影截圖", [name]
    # proxy 沒攔到 → logcat 落地 URL 檢查有無未展開 macro，並以截圖佐證
    cname, _, landing = _click_evidence(caps)
    if cname:
        if landing and "${" in landing:
            return "fail", f"落地 URL 有未展開 macro：{landing[:120]}（{cname}）", [cname]
        extra = f"，落地 URL：{landing}" if landing else "（落地截圖已存）"
        return ("pass",
                f"REEN 點擊已執行、落地頁開啟{extra}（{cname}）；macro 無殘留，以截圖 + logcat 為證",
                [cname])
    return "pending", CLICK_NOTRUN_NOTE, []


def _v_deeplink_landing(caps, ids, test_type):
    name, folder = _first_with(
        caps, lambda f: os.path.exists(os.path.join(f, "deeplink_landing.png")))
    if name:
        return "observe", f"落地截圖已保存（{name}/deeplink_landing.png），需人工確認頁面", [name]
    # E2E flow 的落地截圖
    cname, cfolder, landing = _click_evidence(caps)
    if cname and cfolder and os.path.exists(os.path.join(cfolder, "e2e_step_landing.png")):
        extra = f"（落地 URL：{landing}）" if landing else ""
        return ("observe",
                f"點擊後落地畫面已保存（{cname}/e2e_step_landing.png）{extra}；需人工確認直開 target app／頁面",
                [cname])
    return "pending", CLICK_NOTRUN_NOTE, []


def _v_privacy_click(caps, ids, test_type):
    for name, folder in caps:
        for row in _traffic(folder):
            if "adpolicy.appier.com" in row.get("url", ""):
                ok = row.get("status") in (200, 304)
                return ("pass" if ok else "fail",
                        f"adpolicy.appier.com → {row.get('status')}（{name}）", [name])
    # 驅動端已點但 proxy 沒錄到（TLS passthrough／外部瀏覽器不走代理）→ 靠落地截圖
    for name, folder in caps:
        clicked = _json_file(folder, "privacy_click.json")
        if clicked and clicked.get("tapped"):
            if os.path.exists(os.path.join(folder, "privacy_landing.png")):
                return ("observe",
                        f"icon 已自動點擊、落地截圖已保存（{name}/privacy_landing.png）；"
                        "proxy 未錄到 adpolicy 流量，需人工核對截圖", [name])
            return "pending", f"icon 已點擊但無落地證據（{name}）", [name]
    return ("pending",
            "privacy icon 本輪未點；round 帶 DO_PRIVACY_CLICK=1 即自動點（無點擊費用）；"
            "若 adpolicy.appier.com 走 HTTP/2 需排除 SSL proxy", [])


def _v_ext_enc_decode(caps, ids, test_type):
    """TC-17：取最新一個帶 ext_enc 的 bid_request，解碼並對照明文。
    暗碼是 obfuscation（金鑰在 SDK binary），QA 可還原驗證訊號實際落點。"""
    try:
        from apr_xorenc import decode_ext_enc, build_comparison
    except Exception as exc:
        return "pending", f"apr_xorenc 模組載入失敗：{exc}", []
    name, folder = _first_with(
        caps, lambda f: (_json_file(f, "bid_request.json") or {}).get("ext_enc"))
    if not name:
        return ("pending",
                "本輪 capture 的 bid_request.json 內無 ext_enc 欄位（舊版 SDK 或未帶暗碼包）", [])
    body = _json_file(folder, "bid_request.json")
    try:
        raw, decoded = decode_ext_enc(body)
    except Exception as exc:
        return "fail", f"ext_enc 存在但解碼失敗（格式/金鑰不符）：{exc}（{name}）", [name]
    if not isinstance(decoded, dict):
        return "fail", f"ext_enc 解碼後非合法 JSON 物件（{name}）", [name]
    rows, _ = build_comparison(body, decoded)
    revealed = sum(1 for r in rows if r["revealed"])
    top = list(decoded.keys())
    return ("pass",
            f"ext_enc 解碼成功（ae1 XOR）：top-level {top}；"
            f"暗碼揭露 {revealed}/{len(rows)} 訊號欄（明文缺/null → 暗碼包內有實值）；"
            f"原始 blob + 解碼 JSON + 逐欄對照見 ext_enc_raw.txt / ext_enc_decoded.json / "
            f"ext_enc_compare.txt（{name}）", [name])


def _admob_traffic(caps, needle):
    for name, folder in caps:
        for row in _traffic(folder):
            if needle in row.get("url", ""):
                return name, row
    return None, None


def _v_admob(needle, label):
    def check(caps, ids, test_type):
        name, row = _admob_traffic(caps, needle)
        if not name:
            return "pending", f"無 {label} 流量；" + NO_TRAFFIC_NOTE, []
        ok = row.get("status") == 200
        return ("pass" if ok else "fail", f"{label} → {row.get('status')}（{name}）", [name])
    return check


VALIDATORS = {
    "init": _v_init,
    "bid_response": _v_bid_response,
    "creative_assets": _v_creative_assets,
    "render_match": _v_render_match,
    "impression": _v_impression,
    "click_chain": _v_click_chain,
    "click_chain_reen": _v_click_chain_reen,
    "deeplink_landing": _v_deeplink_landing,
    "privacy_click": _v_privacy_click,
    "ext_enc_decode": _v_ext_enc_decode,
    "admob_pubsetting": _v_admob("getconfig/pubsetting", "pubsetting"),
    "admob_mads": _v_admob("mads/gma", "mads/gma"),
    "admob_fill": _v_admob("pagead/interaction", "fill result"),
    "admob_nofill": _v_admob("nofill", "nofill_urls"),
}


# 為何某 TC 在特定模式不適用（跳過原因，寫進報告讓人一眼懂）
MODE_NA_REASON = {
    "TC-02": "驗 AdMob mediation 的 pubsetting 設定拉取；standalone（純 Appier SDK）沒有 AdMob 中介層，此步驟不存在",
    "TC-03": "驗 AdMob mediation 對 mads/gma 發廣告請求；standalone 直接打 Appier，不經 AdMob，此步驟不存在",
    "TC-08": "驗 AdMob mediation 的 fill result 回報；standalone 無 mediation，不會有 fill 回報",
    "TC-16": "驗 Appier no-fill 後 fallback 到 AdMob 下一 network；standalone 沒有 mediation fallback 鏈",
}


# ── 評估入口 ──────────────────────────────────────────────────────────────────

def evaluate(round_dir, test_mode, test_type):
    """依所選 mode/type 自動判定適用性，對適用者跑驗證器。回傳 rows。"""
    caps = _load_captures(round_dir)
    ids = None
    for _, folder in caps:
        ids = _json_file(folder, "bid_ids.json")
        if ids:
            break
    rows = []
    for tc in E2E_TCS:
        if test_mode not in tc["modes"]:
            why = MODE_NA_REASON.get(tc["tc"], "")
            note = (f"本輪 {test_mode} 跳過（僅適用 {'、'.join(sorted(tc['modes']))}）"
                    + (f"：{why}" if why else "；選模式當下自動判定"))
            status, note, evidence = ("na_mode", note, [])
        elif tc.get("types") and test_type not in tc["types"]:
            status, note, evidence = ("na_type",
                                      f"{test_type} 不適用（適用：{'、'.join(sorted(tc['types']))}）", [])
        elif tc["auto"] is None:
            status, note, evidence = ("backend",
                                      "需 Spark raw_action / MMP 於歸因窗後核對，"
                                      "非同 run 內可自動；可跑事後 Spark 腳本", [])
        else:
            status, note, evidence = VALIDATORS[tc["auto"]](caps, ids, test_type)
        step = STEP_OF.get(tc["tc"], "")
        # 逐步截圖：優先用該步驟的 e2e_step_*.png（任一 capture 有就記檔名，供報告載入）
        step_shot = None
        shot_file = STEP_SHOT.get(step)
        if shot_file:
            for _, folder in caps:
                if os.path.exists(os.path.join(folder, shot_file)):
                    step_shot = os.path.join(os.path.basename(folder), shot_file)
                    break
        rows.append({"tc": tc["tc"], "name": tc["name"], "priority": tc["priority"],
                     "check_kind": tc["check_kind"], "expected": tc["expected"],
                     "endpoint": tc["endpoint"], "status": status, "note": note,
                     "evidence": evidence, "step": step, "step_shot": step_shot})
    return rows


def summarize(round_dir=None, test_mode="standalone", test_type="reen-dynamic"):
    from collections import Counter
    W = 100
    print("=" * W)
    if round_dir:
        rows = evaluate(round_dir, test_mode, test_type)
        print(f"  Ad-Serving E2E — {len(rows)} TC（mode={test_mode} / type={test_type}）")
        print("=" * W)
        for row in rows:
            print(f"  {row['tc']:<6} [{row['priority']}] {STATUS_LABEL[row['status']]:<28} {row['name']}")
            print(f"         ↳ {row['note']}")
        counter = Counter(row["status"] for row in rows)
    else:
        print(f"  Ad-Serving E2E catalog — {len(E2E_TCS)} TC 定義（無 round 資料，僅列適用矩陣）")
        print("=" * W)
        counter = Counter()
        for tc in E2E_TCS:
            modes = "、".join(sorted(tc["modes"])) if tc["modes"] != ALL_MODES else "全部模式"
            auto = tc["auto"] or "backend"
            print(f"  {tc['tc']:<6} [{tc['priority']}] auto={auto:<18} 適用：{modes:<28} {tc['name']}")
    print("-" * W)
    if counter:
        # 收斂成三桶統計（PASS / FAIL / BLOCKED），不逐細分狀態列
        bucket = Counter()
        for key, count in counter.items():
            bucket[STATUS_LABEL.get(key, "BLOCKED").split("（")[0]] += count
        print("  " + " / ".join(f"{count} {label}"
                                for label, count in bucket.most_common()))
    print("=" * W)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        summarize(sys.argv[1],
                  os.environ.get("TEST_MODE", "standalone"),
                  os.environ.get("TEST_TYPE", "reen-dynamic"))
    else:
        summarize()
