"""
mitmproxy addon: 偵測 Appier 的 network request，寫 flag 通知 run.py 停止。

用法（terminal 1）:
    mitmdump -s ~/LazyAdFinder/detector.py --listen-port 8081
"""

from mitmproxy import ctx, http

FLAG_FILE = "/tmp/appier_hit"
NETWORK_FILE = "/tmp/current_networks"
APPIER_DOMAINS = ("appier.net", "appier.com")

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


class AppierDetector:
    def running(self):
        ctx.options.ignore_hosts = [r".*\.apple\.com", r".*\.mzstatic\.com"]

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.host

        if any(d in host for d in APPIER_DOMAINS):
            entry = f"{flow.request.method} https://{host}{flow.request.path}"
            with open(FLAG_FILE, "w") as f:
                f.write(entry)
            print(f"\n>>> APPIER DETECTED: {entry}\n")

        for domain, name in NETWORK_MAP.items():
            if domain in host:
                try:
                    with open(NETWORK_FILE, "a") as f:
                        f.write(name + "\n")
                except Exception:
                    pass
                break


addons = [AppierDetector()]
