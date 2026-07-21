"""
mitmproxy addon: 偵測 Appier bid request/response，capture bid 資料。

Bid endpoints:
    POST https://ad3.apx.appier.net/v2/sdk/aos/ad      (production)
    POST https://adx-stg.apx.appier.net/v2/sdk/aos/ad  (staging)
    POST https://ad3.apx.appier.net/v2/sdk/ios/ad      (iOS production)
    request body 為未壓縮 UTF-8 JSON；response 200 = bid、204 = no-bid

只有 bid request 會寫 FLAG_FILE。其他 appier.net / appier.com 流量
（imp/click tracker GET、data-signal 的 signal.appier.com/v1/key）
不算 bid，只記進 NETWORK_FILE 與 terminal log，避免誤觸發 run script 停止。

用法（terminal 1）:
    mitmdump -s ~/LazyAdFinder/detector.py --listen-port 8081
"""

import gzip
import json as _json
import zlib
from urllib.parse import parse_qs, urlsplit
from mitmproxy import ctx, http

FLAG_FILE = "/tmp/appier_hit"
NETWORK_FILE = "/tmp/current_networks"
BID_FILE = "/tmp/appier_bid.json"
BID_STATUS_FILE = "/tmp/appier_bid_status"
BID_RESPONSE_FILE = "/tmp/appier_bid_response.json"
IMPRESSION_FILE = "/tmp/appier_impression.json"
# E2E 驗證器用的全流量 log（method/url/status，一行一筆 JSON）
TRAFFIC_FILE = "/tmp/appier_traffic.jsonl"

APPIER_DOMAINS = ("appier.net", "appier.com")
BID_HOST_SUFFIX = "apx.appier.net"
BID_PATHS = ("/v2/sdk/aos/ad", "/v2/sdk/ios/ad")
# 「已展示」callback（非 bid 端點，未被 cert pinning 排除，明碼 GET）：
# 2026-07-20 實機觀察到 iOS standalone/mediation 中獎後都會打這支，帶 cid/crid/
# crpid/bidobjid/idfa 等識別碼在 query string——bid 端點本身因 pinning 看不到內容時，
# 這是唯一能拿到「這輪確實中獎、中的是哪個 creative」證據的地方。
IMPRESSION_HOST_PATH = ("apn.c.appier.net", "/callback/show_cb")

NETWORK_MAP = {
    "appier.net":              "Appier",
    "appier.com":              "Appier",
    "smadex.com":              "Smadex",
    "googlesyndication.com":   "Google/AdMob",
    "doubleclick.net":         "Google/AdMob",
    "googleads.g.doubleclick": "Google/AdMob",
    "mintegral.com":           "Mintegral",
    "applovin.com":            "AppLovin",
    "unityads.unity3d.com":    "Unity Ads",
    "ironsrc.com":             "ironSource",
    "vungle.com":              "Liftoff/Vungle",
    "chartboost.com":          "Chartboost",
    "fbcdn.net":               "Meta AN",
    "facebook.com":            "Meta AN",
    "amazon-adsystem.com":     "Amazon",
    "inmobi.com":              "InMobi",
    "criteo.com":              "Criteo",
}


def _parse_body(content):
    """Try JSON parse with gzip/deflate fallback."""
    for attempt in (
        lambda b: _json.loads(b),
        lambda b: _json.loads(gzip.decompress(b)),
        lambda b: _json.loads(zlib.decompress(b)),
        lambda b: _json.loads(zlib.decompress(b, -15)),
    ):
        try:
            return attempt(content)
        except Exception:
            continue
    return None


def _is_bid(flow: http.HTTPFlow) -> bool:
    return (
        flow.request.host.endswith(BID_HOST_SUFFIX)
        and any(flow.request.path.startswith(path) for path in BID_PATHS)
        and flow.request.method == "POST"
    )


def _is_impression_win(flow: http.HTTPFlow) -> bool:
    host, path = IMPRESSION_HOST_PATH
    return flow.request.host.endswith(host) and flow.request.path.startswith(path)


def _save_json(path, content):
    parsed = _parse_body(content)
    if parsed is not None:
        with open(path, "w") as f:
            _json.dump(parsed, f, indent=2)
        return True
    with open(path, "wb") as f:
        f.write(content)
    return False


class AppierDetector:
    def running(self):
        # SSL passthrough 白名單，對齊 Charles 的 sslExcludeLocations，避免攔截破壞：
        #   - apple / mzstatic / icloud：Apple 服務會 pin
        #   - *google.com / *googleapis.com：Google 服務 + Android App Links 驗證
        #     （digitalassetlinks 走 googleapis）；攔截會讓 deeplink 驗不過 → 退回瀏覽器、不開 app
        #   - approov：API 防護/pinning 服務，攔截必失敗
        #   - dcard：Charles 既有排除
        # TEST 2026-07-20：apx.appier.net 原本也在此清單（理由：「cert pinning，
        # mitmproxy 特有需求」）。查 Charles 設定檔（com.xk72.charles.config）
        # 發現 Charles 的 sslExcludeLocations 完全沒有排除這個 host（include list
        # 是萬用字元 *），代表當初排除的其實只有 mitmdump 自己的 ignore_hosts，
        # 不是 Charles/SDK 層級的必然限制。先拿掉這行實測：如果是真 pinning，
        # 這支手機下一次觸發廣告會直接連線失敗/TLS handshake error；如果不是，
        # 就能直接看到 bid_request.json 的真實內容。視結果決定要不要留著這行。
        ctx.options.ignore_hosts = [
            r".*\.apple\.com", r".*\.mzstatic\.com", r".*\.icloud\.com",
            r".*google\.com", r".*googleapis\.com",
            r".*approov.*", r".*dcard.*",
        ]

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.host
        entry = f"{flow.request.method} https://{host}{flow.request.path}"

        if _is_bid(flow):
            if flow.request.content:
                as_json = _save_json(BID_FILE, flow.request.content)
                print(f">>> BID SAVED{'' if as_json else ' (raw, not JSON)'} → {BID_FILE}")
            with open(FLAG_FILE, "w") as f:
                f.write(entry)
            print(f"\n>>> APPIER BID REQUEST: {entry}\n")
        elif _is_impression_win(flow):
            # bid 端點本身因 cert pinning 看不到內容（BID_FILE 不會產生）；
            # 用這支明碼 callback 的 query string 當作等效 win 信號 + 識別碼來源。
            qs = parse_qs(urlsplit(flow.request.path).query)
            ids = {k: v[0] for k, v in qs.items() if v and v[0]}
            with open(IMPRESSION_FILE, "w") as f:
                _json.dump(ids, f, indent=2)
            with open(FLAG_FILE, "w") as f:
                f.write(entry)
            with open(BID_STATUS_FILE, "w") as f:
                f.write("200")
            print(f">>> IMPRESSION WIN (from tracker callback, bid body unavailable) "
                  f"→ {IMPRESSION_FILE}  cid={ids.get('cid')} crid={ids.get('crid')}")
        elif any(d in host for d in APPIER_DOMAINS):
            # tracker / data-signal key / 其他 appier 流量：只記錄，不觸發 flag
            print(f"    (appier non-bid) {entry}")

        for domain, name in NETWORK_MAP.items():
            if domain in host:
                try:
                    with open(NETWORK_FILE, "a") as f:
                        f.write(name + "\n")
                except Exception:
                    pass
                break

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            # E2E flow 驗證（init / creative assets / show_cb / xclk / privacy …）
            # 需要完整的 url+status 對照；全部記進 TRAFFIC_FILE，由 run_ssp 歸檔
            try:
                with open(TRAFFIC_FILE, "a") as f:
                    f.write(_json.dumps({
                        "method": flow.request.method,
                        "url": f"https://{flow.request.host}{flow.request.path}",
                        "status": flow.response.status_code,
                    }) + "\n")
            except Exception:
                pass
        if not _is_bid(flow) or flow.response is None:
            return
        status = flow.response.status_code
        with open(BID_STATUS_FILE, "w") as f:
            f.write(str(status))
        if status == 200 and flow.response.content:
            _save_json(BID_RESPONSE_FILE, flow.response.content)
            print(f">>> BID RESPONSE 200 (ad won) → {BID_RESPONSE_FILE}")
        else:
            print(f">>> BID RESPONSE {status}" + (" (no-bid)" if status == 204 else ""))


addons = [AppierDetector()]
