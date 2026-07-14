"""
mitmproxy addon: 偵測 Appier bid request/response，capture bid 資料。

Bid endpoint（appier-ads-android Constants.java + AppierBaseAd.internalLoadAd）:
    POST https://ad3.apx.appier.net/v2/sdk/aos/ad      (production)
    POST https://adx-stg.apx.appier.net/v2/sdk/aos/ad  (staging)
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
from mitmproxy import ctx, http

FLAG_FILE = "/tmp/appier_hit"
NETWORK_FILE = "/tmp/current_networks"
BID_FILE = "/tmp/appier_bid.json"
BID_STATUS_FILE = "/tmp/appier_bid_status"
BID_RESPONSE_FILE = "/tmp/appier_bid_response.json"

APPIER_DOMAINS = ("appier.net", "appier.com")
BID_HOST_SUFFIX = "apx.appier.net"
BID_PATH = "/v2/sdk/aos/ad"

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
        and flow.request.path.startswith(BID_PATH)
        and flow.request.method == "POST"
    )


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
        ctx.options.ignore_hosts = [r".*\.apple\.com", r".*\.mzstatic\.com"]

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
