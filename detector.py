"""
mitmproxy addon: 偵測 Appier 的 network request，寫 flag 通知 run.py 停止。

用法（terminal 1）:
    mitmweb -s detector.py --listen-port 8080
"""

from mitmproxy import http

FLAG_FILE = "/tmp/appier_hit"
APPIER_DOMAINS = ("appier.net", "appier.com")


class AppierDetector:
    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.host
        if any(d in host for d in APPIER_DOMAINS):
            entry = f"{flow.request.method} https://{host}{flow.request.path}"
            with open(FLAG_FILE, "w") as f:
                f.write(entry)
            print(f"\n>>> APPIER DETECTED: {entry}\n")


addons = [AppierDetector()]
