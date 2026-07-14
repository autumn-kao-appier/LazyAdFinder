#!/usr/bin/env python3
"""
e2e_catalog.py — Ad-Serving E2E flow 測試案例（Confluence R187613）。

與 bid_inspector 的 AND-xx（bid signal 欄位驗證）不同維度：這裡是整條 ad-serving
pipeline 的 flow checkpoint（init → mediation config → ad request → creative →
render → impression → click → landing → attribution → negative）。

本輪上線的是 **Standalone SDK**（非 AdMob mediation），所以 mediation-only 的 TC
（pubsetting / mads / fill result / no-fill fallback）標為 blocked_standalone：
此版不走 mediation，該路徑不適用。

status 值:
    pass               已驗證通過
    blocked_standalone 此版為 Standalone，mediation 路徑不適用
    pending            待補（缺 RD 確認 / 缺資料）
    env                測試環境造成的假象，非產品問題
    observe            有觀察到現象、待釐清是否符合預期

check_kind:
    traffic  Charles/mitmdump 可直接觀察的 endpoint
    render   畫面渲染，需人工/截圖
    backend  需查 Spark raw_action / MMP，非流量可見
"""

E2E_TCS = [
    {
        "tc": "TC-01", "cat": "A. Ad Serving", "name": "SDK Init request",
        "endpoint": "GET adx.apx.appier.net /v1/sdk/init（Android 對應 path 待與 RD 確認）",
        "priority": "P0", "check_kind": "traffic",
        "expected": "HTTP 200；bundle = Sample App bundle id；Response 正常回傳",
        "status": "pending",
        "note": "Android init path 尚待 RD 確認；已有 capture（reqid FKFR-xm0C6Of01w6alZUag）",
        "evidence": ["aos_reen_coupang02.chls", "aos_reen_coupang02.mov"],
    },
    {
        "tc": "TC-02", "cat": "A. Ad Serving", "name": "AdMob pubsetting mediation config",
        "endpoint": "GET googleads.g.doubleclick.net/getconfig/pubsetting",
        "priority": "P0", "check_kind": "traffic",
        "expected": "200 status=1；mediation_config.ad_networks 含 "
                    "AppierAdsAdMobMediation.APRAdAdapter；parameter 含 zoneId(7906)；is_mediation=true",
        "status": "blocked_standalone",
        "note": "此版上線 Standalone，不走 AdMob mediation",
        "evidence": [],
    },
    {
        "tc": "TC-03", "cat": "A. Ad Serving", "name": "AdMob ad request(mads/gma) 與 mediation 分流",
        "endpoint": "googleads.g.doubleclick.net/mads/gma",
        "priority": "P0", "check_kind": "traffic",
        "expected": "200；ad_networks 含 Appier custom event(APRAdAdapter, zoneId)；"
                    "得標時 bidding_data=Won，含 fill_urls / imp_urls",
        "status": "blocked_standalone",
        "note": "此版上線 Standalone，不走 AdMob mediation",
        "evidence": [],
    },
    {
        "tc": "TC-04", "cat": "A. Ad Serving", "name": "Appier ad request 驗證",
        "endpoint": "POST adx.apx.appier.net/v1/sdk/ad（實際觀察為 /v2/sdk/aos/ad）",
        "priority": "P0", "check_kind": "traffic",
        "expected": "200；zoneid 與 config 一致(7906)；adUnits[0].ad 完整"
                    "(clk/impTracker/native: title,text,ctaText,iconImage,mainImage,"
                    "privacyInformationIcon,privacyInformationLink)",
        "status": "pass",
        "note": "實際 endpoint 為 POST /v2/sdk/aos/ad HTTP/1.1（非 sheet 寫的 v1/sdk/ad）",
        "evidence": [],
    },
    {
        "tc": "TC-05", "cat": "A. Ad Serving", "name": "Creative assets 載入",
        "endpoint": "iconImage / mainImage / privacy icon URL",
        "priority": "P1", "check_kind": "traffic",
        "expected": "圖片 request 均 HTTP 200（或 304 快取）；圖片正常顯示無破圖",
        "status": "observe",
        "note": "沒有 privacy icon 的獨立 request（疑為快取 304 或內嵌）；icon/main image 正常",
        "evidence": [],
    },
    {
        "tc": "TC-06", "cat": "A. Ad Serving", "name": "Native ad 於 Sample App 渲染",
        "endpoint": "—（畫面渲染）",
        "priority": "P0", "check_kind": "render",
        "expected": "各元素對應正確、無異常截斷/破版；標示為廣告；與 response 一致",
        "status": "pass", "note": "", "evidence": [],
    },
    {
        "tc": "TC-07", "cat": "B. Tracking", "name": "Impression tracking",
        "endpoint": "apn.c.appier.net/callback/show_cb → iota188.rtb.appier.net/winshowimg；AdMob imp_urls",
        "priority": "P0", "check_kind": "traffic",
        "expected": "show_cb 302 → winshowimg 200；show_cb 參數含正確 "
                    "bidobjid/cid/crid/deal_id/price_encoded；AdMob imp_urls 正常",
        "status": "pass",
        "note": "Standalone：確認 Appier show_cb / winshowimg 存在即可（無 AdMob imp_urls）",
        "evidence": [],
    },
    {
        "tc": "TC-08", "cat": "B. Tracking", "name": "Mediation fill result 回報",
        "endpoint": "googleads.g.doubleclick.net/pagead/interaction（fill_urls）",
        "priority": "P2", "check_kind": "traffic",
        "expected": "admob_mediation_request_fill_result 發送，mediation_fill_status 合理",
        "status": "blocked_standalone",
        "note": "此版上線 Standalone", "evidence": [],
    },
    {
        "tc": "TC-09", "cat": "B. Tracking", "name": "Click tracking",
        "endpoint": "AdMob aclk；tw.c.appier.net/xclk（會產生真實點擊費用）",
        "priority": "P0", "check_kind": "traffic",
        "expected": "xclk 302 Found 含正確 cid/crid/deal_id；aclk 302；redirect chain 無 error",
        "status": "pass",
        "note": "Standalone 只剩 xclk（無 AdMob aclk）",
        "evidence": [],
    },
    {
        "tc": "TC-10", "cat": "B. Tracking", "name": "Landing 跳轉（REEN deeplink）",
        "endpoint": "deeplink",
        "priority": "P1", "check_kind": "render",
        "expected": "deeplink 直開 target app（非 Play Store）；無白頁/卡住/crash",
        "status": "pass",
        "note": "跳到 Coupang TW 的 product page",
        "evidence": [],
    },
    {
        "tc": "TC-11", "cat": "B. Tracking", "name": "Privacy information icon",
        "endpoint": "privacyInformationLink（https://adpolicy.appier.com/）",
        "priority": "P2", "check_kind": "traffic",
        "expected": "點 privacy icon 開啟 privacyInformationLink 指定頁",
        "status": "env",
        "note": "點擊跳 pop-up 顯示 net::ERR_HTTP2_PROTOCOL_ERROR。實測：直連該頁 HTTP/2 200 "
                "正常，走 Charles 則 timeout → 是 Charles/MITM 破壞 HTTP/2 的測試環境假象，非 SDK/頁面問題。"
                "在 Charles 把 adpolicy.appier.com 排除 SSL Proxying 後再點即正常開啟。",
        "evidence": [],
    },
    {
        "tc": "TC-14", "cat": "C. Attribution", "name": "REEN click → deeplink 直開 target app",
        "endpoint": "xclk / aclk + MMP OneLink（is_retargeting=true）",
        "priority": "P2", "check_kind": "traffic",
        "expected": "xclk 302 / aclk 302 chain 無 error；OneLink macro 全展開無 ${...} 殘留；"
                    "deeplink 直開 target app",
        "status": "pass",
        "note": "需與 CM 確認測試窗口、測試機須已安裝推廣 app；is_retargeting=true",
        "evidence": [],
    },
    {
        "tc": "TC-15", "cat": "C. Attribution", "name": "REEN re-engagement(open) action postback 核對",
        "endpoint": "Spark raw_action(open) / MMP",
        "priority": "P2", "check_kind": "backend",
        "expected": "imp/click 正常入帳、bidobjid 對應；re-engagement open action 於歸因窗內入帳，"
                    "歸因至正確 CID/CRID",
        "status": "pass",
        "note": "yu.liang attribution 窗口",
        "evidence": ["AOS_REEN_action.csv",
                     "https://scheduler.spark.arepa.appier.info/job-detail?project=@celia.ho&job=94359835-516c-4981-aab3-91afa981fa8e"],
    },
    {
        "tc": "TC-16", "cat": "D. Negative", "name": "Appier no-fill → AdMob fallback",
        "endpoint": "nofill_urls",
        "priority": "P3", "check_kind": "traffic",
        "expected": "nofill_urls 正常發送；AdMob fallback 至下一 ad network，版位仍正常",
        "status": "blocked_standalone",
        "note": "此版上線 Standalone", "evidence": [],
    },
]

STATUS_LABEL = {
    "pass": "PASS",
    "blocked_standalone": "BLOCKED（Standalone 不適用）",
    "pending": "PENDING（待 RD/資料）",
    "env": "測試環境假象（非 bug）",
    "observe": "待釐清",
}


def summarize():
    from collections import Counter
    c = Counter(t["status"] for t in E2E_TCS)
    W = 92
    print("=" * W)
    print(f"  Ad-Serving E2E Flow — {len(E2E_TCS)} TC（本輪：Standalone SDK）")
    print("=" * W)
    for t in E2E_TCS:
        print(f"  {t['tc']:<6} [{t['priority']}] {STATUS_LABEL[t['status']]:<26} {t['name']}")
        if t["note"]:
            print(f"         ↳ {t['note']}")
    print("-" * W)
    parts = [f"{c[k]} {STATUS_LABEL[k].split('（')[0]}" for k in
             ("pass", "observe", "pending", "env", "blocked_standalone") if c.get(k)]
    print("  " + " / ".join(parts))
    print("=" * W)


if __name__ == "__main__":
    summarize()
