#!/usr/bin/env python3
"""無人值守的完整 Signal round runner。

M1/M2/M3/SC/AUTO 是同一個 TEST_ROUND 內部的自動狀態批次，不是獨立 TC
round。runner 依序建立互斥狀態並 capture，所有結果最後合併到同一份 round
report。單一批次失敗時只跳過該批次並繼續；可用 START_AT=M2 補跑。
"""

import os
import atexit
import re
import subprocess
import sys
import termios
import tty
from pathlib import Path

ROOT = Path(__file__).parent
UDID = os.environ.get("UDID", "").strip()
APP_PACKAGE = os.environ.get("APP_PACKAGE", "").strip()
ALLOW_MANUAL_FALLBACK = os.environ.get("ALLOW_MANUAL_FALLBACK", "0") == "1"
STATE_ACTIONS = []


class AutomationError(RuntimeError):
    """Required test state could not be established without human input."""


def select_menu(title, options):
    """↑/↓ 選擇、Enter 確認；非 TTY 時由呼叫端使用 env，不進選單。"""
    selected = 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    print(f"\n{title}")
    try:
        tty.setraw(fd)
        while True:
            sys.stdout.write("\r\x1b[J")
            for i, (label, _) in enumerate(options):
                marker = "❯" if i == selected else " "
                style = "\x1b[7m" if i == selected else ""
                sys.stdout.write(f"{marker} {style}{label}\x1b[0m\r\n")
            sys.stdout.flush()
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                return options[selected][1]
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                if seq == "[A": selected = (selected - 1) % len(options)
                elif seq == "[B": selected = (selected + 1) % len(options)
            elif ch.lower() == "k": selected = (selected - 1) % len(options)
            elif ch.lower() == "j": selected = (selected + 1) % len(options)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\r\n")


def adb(*args):
    cmd = ["adb"] + (["-s", UDID] if UDID else []) + list(args)
    p = subprocess.run(cmd, text=True, capture_output=True)
    return (p.stdout + p.stderr).strip()


def open_action(action, data=None):
    adb("shell", "am", "force-stop", "com.android.settings")
    cmd = ["shell", "am", "start", "-a", action]
    if data:
        cmd += ["-d", data]
    print(adb(*cmd))


def manual_or_fail(title, instruction, action=None, component=None):
    """In an explicit manual round, open the exact page and wait; otherwise fail fast."""
    if not ALLOW_MANUAL_FALLBACK:
        raise AutomationError(f"{title}：自動化未能完成必要狀態。{instruction}")
    print(f"\n[手動 fallback] {title}\n  {instruction}")
    if component:
        print(adb("shell", "am", "start", "-n", component))
    elif action:
        open_action(action)
    input("  完成後按 Enter，runner 將讀回驗證並自動進入下一步：")


def ensure_timezone(zone, label):
    out = adb("shell", "cmd", "alarm", "set-timezone", zone)
    actual = adb("shell", "getprop", "persist.sys.timezone").strip()
    if actual == zone:
        print(f"[自動] 時區 → {zone}")
        return
    print(f"[自動] 時區設定失敗：{out or actual}")
    manual_or_fail(label, f"請切換至 {zone}。", action="android.settings.DATE_SETTINGS")
    actual = adb("shell", "getprop", "persist.sys.timezone").strip()
    if actual != zone:
        raise AutomationError(f"{label}：人工 fallback 後讀回 {actual!r}，預期 {zone!r}")


def battery_state():
    dump = adb("shell", "dumpsys", "battery")
    level_m = re.search(r"level:\s*(\d+)", dump)
    powered = any(re.search(fr"{kind} powered:\s*true", dump, re.I)
                  for kind in ("AC", "USB", "Wireless"))
    return (int(level_m.group(1)) if level_m else None), powered


def ensure_battery(title, level=None, charging=None):
    actual_level, actual_charging = battery_state()
    STATE_ACTIONS.append(
        f"battery before: level={actual_level}, charging={actual_charging}")
    # A USB-only ADB device cannot be physically unplugged without killing the
    # test session. Use Android's battery simulation for charging-state cases.
    if charging is False and actual_charging:
        STATE_ACTIONS.append("adb shell dumpsys battery unplug")
        adb("shell", "dumpsys", "battery", "unplug")
        actual_level, actual_charging = battery_state()
        if not actual_charging:
            print("[自動] Battery mock → unplugged（USB ADB 保持連線）")
    elif charging is True and not actual_charging:
        STATE_ACTIONS.append("adb shell dumpsys battery reset")
        adb("shell", "dumpsys", "battery", "reset")
        actual_level, actual_charging = battery_state()
        if not actual_charging:
            STATE_ACTIONS.append("adb shell dumpsys battery set ac 1")
            adb("shell", "dumpsys", "battery", "set", "ac", "1")
        actual_level, actual_charging = battery_state()
        if actual_charging:
            print("[自動] Battery → charging")
    if level is not None and actual_level != level:
        STATE_ACTIONS.append(f"adb shell dumpsys battery set level {level}")
        adb("shell", "dumpsys", "battery", "set", "level", str(level))
        actual_level, actual_charging = battery_state()
    level_ok = level is None or actual_level == level
    charging_ok = charging is None or actual_charging == charging
    if level_ok and charging_ok:
        STATE_ACTIONS.append(
            f"battery after: level={actual_level}, charging={actual_charging}, target_level={level}, target_charging={charging}")
        print(f"[自動確認] 電量={actual_level}% charging={actual_charging}：符合")
        return
    need = []
    if level is not None: need.append(f"電量 {level}%")
    if charging is not None: need.append("接上電源" if charging else "拔除電源")
    manual_or_fail(title, f"目前 {actual_level}% / charging={actual_charging}；請調整為 {'、'.join(need)}。",
                   action="android.intent.action.POWER_USAGE_SUMMARY")
    actual_level, actual_charging = battery_state()
    if ((level is not None and actual_level != level) or
            (charging is not None and actual_charging != charging)):
        raise AutomationError(f"{title}：人工 fallback 後仍為 {actual_level}% / charging={actual_charging}")


def vpn_active():
    dump = adb("shell", "dumpsys", "connectivity")
    links = adb("shell", "ip", "link")
    return bool(re.search(r"TRANSPORT_VPN|type:\s*VPN", dump, re.I) or
                re.search(r"\b(tun\d+|ppp\d+|wg\d+|tailscale\d*)\b", links, re.I))


def set_tailscale(expected):
    """Toggle the installed Tailscale client and verify via the VPN interface."""
    packages = adb("shell", "pm", "list", "packages", "com.tailscale.ipn")
    if "com.tailscale.ipn" not in packages:
        return False
    adb("shell", "monkey", "-p", "com.tailscale.ipn", "-c",
        "android.intent.category.LAUNCHER", "1")
    subprocess.run(["sleep", "1"])
    def read_ui():
        adb("shell", "uiautomator", "dump", "/sdcard/tailscale-window.xml")
        return adb("shell", "cat", "/sdcard/tailscale-window.xml")

    def tap_text(ui, label):
        match = re.search(
            rf'text="{re.escape(label)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            ui)
        if not match:
            return False
        x = (int(match.group(1)) + int(match.group(3))) // 2
        y = (int(match.group(2)) + int(match.group(4))) // 2
        adb("shell", "input", "tap", str(x), str(y))
        return True

    ui = read_ui()
    switch = re.search(
        r'checkable="true" checked="(true|false)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        ui)
    if not switch:
        return False
    checked = switch.group(1) == "true"
    if checked != expected:
        x = (int(switch.group(2)) + int(switch.group(4))) // 2
        y = (int(switch.group(3)) + int(switch.group(5))) // 2
        adb("shell", "input", "tap", str(x), str(y))
        subprocess.run(["sleep", "2"])
    if expected:
        # Reengage CIDs require Taiwan Office egress. Always select and verify
        # the configured Taipei exit node instead of accepting any VPN route.
        ui = read_ui()
        search_pos = ui.find('content-desc="Search"')
        selected_pos = ui.find('text="tpe-exit-3"')
        tpe_selected = 0 <= selected_pos < search_pos
        if not tpe_selected:
            if not tap_text(ui, "EXIT NODE"):
                return False
            subprocess.run(["sleep", "1"])
            ui = read_ui()
            if not tap_text(ui, "tpe-exit-3"):
                return False
            subprocess.run(["sleep", "3"])
            ui = read_ui()
            search_pos = ui.find('content-desc="Search"')
            selected_pos = ui.find('text="tpe-exit-3"')
            tpe_selected = 0 <= selected_pos < search_pos
        return tpe_selected and 'text="Connected"' in ui
    ui = read_ui()
    switch = re.search(r'checkable="true" checked="(true|false)"', ui)
    return bool(switch and switch.group(1) == "false")


def restore_standard_state():
    """Return the device to the team's readable, non-test baseline."""
    print("\n── 收尾：還原標準裝置狀態 ──")
    adb("shell", "dumpsys", "battery", "reset")
    adb("shell", "cmd", "uimode", "night", "no")
    adb("shell", "cmd", "power", "set-mode", "0")
    adb("shell", "settings", "put", "system", "screen_brightness_mode", "0")
    adb("shell", "settings", "put", "system", "screen_brightness", "102")
    adb("shell", "settings", "put", "system", "font_scale", "1.0")
    adb("shell", "cmd", "media_session", "volume", "--stream", "3", "--set", "12")
    adb("shell", "appops", "set", APP_PACKAGE, "ACCESS_FINE_LOCATION", "allow")
    adb("shell", "appops", "set", APP_PACKAGE, "ACCESS_COARSE_LOCATION", "allow")
    adb("shell", "cmd", "alarm", "set-timezone", "Asia/Taipei")
    vpn_off = set_tailscale(False) if vpn_active() else True
    try:
        ensure_tracking(True)
        tracking_restored = True
    except AutomationError:
        tracking_restored = False
    print("[還原] light mode / battery saver off / brightness 40% / font 1.0 / volume 12")
    print("[還原] location allowed / timezone Asia/Taipei / battery reset")
    print(f"[還原] VPN off：{'OK' if vpn_off else 'FAILED'}")
    print(f"[還原] GAID opt-in：{'OK' if tracking_restored else 'FAILED'}")


def build_and_open_report(env):
    """Rebuild the latest round report and open it for the tester."""
    safe_cid = re.sub(r"[^A-Za-z0-9_-]+", "-", env["TEST_CID"]).strip("-")
    prefix = (f"{env['TEST_MODE'].upper().replace('-', '_')}_"
              f"{env['TEST_TYPE'].upper().replace('-', '_')}_CID_"
              f"{safe_cid}_{env['TEST_ROUND']}")
    evidence_root = ROOT / "evidence"
    rounds = sorted(path for path in evidence_root.glob(f"{prefix}_*") if path.is_dir())
    if not rounds:
        print("[報告] 找不到本輪 evidence，略過開啟。")
        return False
    report = ROOT / "artifact-preview.html"
    result = subprocess.run(
        [sys.executable, str(ROOT / "build_artifact.py"), str(rounds[-1]),
         "--out", str(report)])
    if result.returncode:
        print(f"[報告] 建置失敗（exit {result.returncode}）")
        return False
    subprocess.run(["open", str(report)], check=False)
    print(f"[報告] 已自動開啟 {report}")
    return True


def latest_round_dir(env):
    safe_cid = re.sub(r"[^A-Za-z0-9_-]+", "-", env["TEST_CID"]).strip("-")
    prefix = (f"{env['TEST_MODE'].upper().replace('-', '_')}_"
              f"{env['TEST_TYPE'].upper().replace('-', '_')}_CID_"
              f"{safe_cid}_{env['TEST_ROUND']}")
    rounds = sorted(path for path in (ROOT / "evidence").glob(f"{prefix}_*") if path.is_dir())
    return rounds[-1] if rounds else None


def failed_signal_tcs(env):
    """Recompute the latest result for every executed Signal TC and return failures."""
    from build_artifact import load_captures, pick_capture
    from bid_inspector import VALIDATORS, run_inspection

    round_dir = latest_round_dir(env)
    if not round_dir:
        return set()
    caps = load_captures(str(round_dir))
    normal = {
        name: {(r["tc"], r["field"]): r for r in run_inspection(
            cap["bid"], reference_ms=cap.get("captured_at_ms"))}
        for name, cap in caps.items() if cap.get("bid") is not None
    }
    first = {
        name: {(r["tc"], r["field"]): r for r in run_inspection(
            cap["first_bid"], reference_ms=cap.get("captured_at_ms"))}
        for name, cap in caps.items() if cap.get("first_bid") is not None
    }
    failed = set()
    for validator in VALIDATORS:
        tc, field = validator["tc"], validator["field"]
        capture = pick_capture(tc, caps)
        source = first if tc in {"AND-48", "AND-49"} and capture in first else normal
        result = source.get(capture, {}).get((tc, field))
        if result is not None and not result["passed"]:
            failed.add(tc)
    return failed


def retry_failed_rounds(env):
    """Retry every failed Signal TC in a matching state capture."""
    from build_artifact import AUTO_TCS, M1_TCS, M2_TCS, M3_TCS

    max_retries = int(os.environ.get("MAX_FAILED_RETRIES", "1"))
    for attempt in range(1, max_retries + 1):
        failed = failed_signal_tcs(env)
        if not failed:
            print("[Retry] 沒有失敗的 Signal TC。")
            return
        print(f"\n===== 自動 Retry {attempt}/{max_retries}：{','.join(sorted(failed))} =====")
        phases = [
            ("AUTO", sorted(failed & AUTO_TCS)),
            ("M1", sorted(failed & M1_TCS)),
            ("M2", sorted(failed & M2_TCS)),
            ("M3", sorted(failed & M3_TCS)),
        ]
        for phase, tcs in phases:
            if not tcs:
                continue
            if phase == "AUTO":
                # AUTO_TCS 應在標準狀態下抓；否則（如 STOP_AFTER=M2 後）會沾 M2 殘留。
                restore_standard_state()
            elif phase == "M1":
                ensure_tracking(True)
                ensure_app_locale("en-US")
                ensure_battery("M1 retry battery", level=100, charging=False)
                ensure_vpn(False)
                ensure_timezone("Asia/Taipei", "M1 retry timezone")
                auto_common(False)
            elif phase == "M2":
                ensure_battery("M2 retry battery", level=0, charging=False)
                auto_common(True)
                if env["TEST_TYPE"].startswith("reen"):
                    ensure_tracking(True)  # REEN 不驗 opt-out（互斥），見 main M2 註解
                else:
                    ensure_tracking(False)
                ensure_vpn(True)
                ensure_timezone("America/New_York", "M2 retry timezone")
            elif phase == "M3":
                set_and_verify("Battery Saver", ("shell", "cmd", "power", "set-mode", "0"),
                               ("shell", "settings", "get", "global", "low_power"), "0")
                ensure_battery("M3 retry charging", charging=True)
                ensure_timezone("UTC", "M3 retry timezone")
            run_capture(f"{phase}_RETRY{attempt}", tcs, env,
                        dwell=35 if phase == "M2" else 0,
                        fgbg=phase == "M2",
                        action=f"Auto {phase} retry {attempt}：{','.join(tcs)}")


def ensure_vpn(expected):
    actual = vpn_active()
    if actual == expected:
        print(f"[自動確認] VPN={'on' if actual else 'off'}：符合")
        return
    if set_tailscale(expected):
        # Tailscale UI 若留在前景會蓋住 sample app，導致 capture 的刷廣告 loop 找不到版位
        adb("shell", "input", "keyevent", "KEYCODE_HOME")
        # set_tailscale 的 off 判斷抓 UI 第一個 checkable，可能不是 VPN 開關 → 用
        # vpn_active() ground truth 復驗，回報成功但實際不符就落到人工 fallback。
        if vpn_active() == expected:
            print(f"[自動] Tailscale VPN → {'on' if expected else 'off'}")
            return
        print("[警告] Tailscale 回報成功但 vpn_active 讀回不符，改人工確認")
    manual_or_fail("VPN " + ("on" if expected else "off"),
                   "請在已開啟頁面建立目標狀態。",
                   action="android.settings.VPN_SETTINGS")
    if vpn_active() != expected:
        raise AutomationError("VPN：人工 fallback 後讀回狀態仍不符合")


def dump_ui():
    # `uiautomator dump /dev/tty` 在部分裝置吐不出內容；dump 到檔案再 cat 才穩
    adb("shell", "uiautomator", "dump", "/sdcard/wizard-ui.xml")
    return adb("shell", "cat", "/sdcard/wizard-ui.xml")


def tracking_opted_in():
    # state-proof 截圖可能留下通知欄／鎖屏遮罩；遮罩存在時 uiautomator 只會
    # dump SystemUI，導致找不到 Delete/Get advertising ID。
    adb("shell", "cmd", "statusbar", "collapse")
    adb("shell", "wm", "dismiss-keyguard")
    adb("shell", "am", "start", "-n",
        "com.google.android.gms/.adsidentity.settings.AdsIdentitySettingsActivity")
    subprocess.run(["sleep", "2"])
    ui = dump_ui()
    if re.search(r"Get new advertising ID|重新取得廣告 ID|取得新的廣告 ID", ui, re.I):
        return False
    if re.search(r"Delete advertising ID|刪除廣告 ID", ui, re.I):
        return True
    return None


def ensure_tracking(expected):
    actual = tracking_opted_in()
    if actual == expected:
        print(f"[自動確認] GAID opt-{'in' if actual else 'out'}：符合")
        return
    labels = (["Get new advertising ID", "重新取得廣告 ID", "取得新的廣告 ID"]
              if expected else
              ["Delete advertising ID", "刪除廣告 ID"])
    for _ in range(2):
        ui = dump_ui()
        tapped = False
        for label in labels + ["Confirm", "確認", "OK"]:
            match = re.search(
                rf'text="{re.escape(label)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                ui, re.I)
            if not match:
                continue
            x = (int(match.group(1)) + int(match.group(3))) // 2
            y = (int(match.group(2)) + int(match.group(4))) // 2
            adb("shell", "input", "tap", str(x), str(y))
            subprocess.run(["sleep", "1"])
            tapped = True
            break
        if not tapped:
            break
    actual = tracking_opted_in()
    if actual != expected:
        manual_or_fail(
            "GAID opt-" + ("in" if expected else "out"),
            "UI automation 未成功，請完成廣告 ID 狀態切換。",
            component="com.google.android.gms/.adsidentity.settings.AdsIdentitySettingsActivity")
        actual = tracking_opted_in()
        if actual != expected:
            raise AutomationError(
                f"GAID：人工 fallback 後讀回 {actual!r}，預期 {expected!r}")
    print(f"[自動] GAID opt-{'in' if expected else 'out'}")


def ensure_app_locale(language_tag):
    cmd = ("shell", "cmd", "locale", "set-app-locales", APP_PACKAGE,
           "--user", "0", "--locales", language_tag)
    STATE_ACTIONS.append("adb " + " ".join(cmd))
    adb(*cmd)
    get_cmd = ("shell", "cmd", "locale", "get-app-locales", APP_PACKAGE, "--user", "0")
    actual = adb(*get_cmd)
    if language_tag.lower() not in actual.lower():
        manual_or_fail("App locale", f"請將 Sample App 語言設為 {language_tag}。",
                       action="android.settings.APP_LOCALE_SETTINGS")
        actual = adb(*get_cmd)
        if language_tag.lower() not in actual.lower():
            raise AutomationError(f"App locale 讀回 {actual!r}，預期 {language_tag}")
    STATE_ACTIONS.append(f"app locale after: {actual.strip()}")


def set_and_verify(label, set_args, get_args, expected, fallback=None):
    adb(*set_args)
    actual = adb(*get_args).strip()
    ok = actual == str(expected)
    print(f"[自動] {label}: {'OK' if ok else 'FAILED'}（讀回 {actual!r}，預期 {expected!r}）")
    if not ok:
        manual_or_fail(label, f"請調整為 {expected!r}。", action=fallback)
        actual = adb(*get_args).strip()
        if actual != str(expected):
            raise AutomationError(f"{label}：人工 fallback 後讀回 {actual!r}，預期 {expected!r}")
    return ok


def set_volume(value):
    out = adb("shell", "cmd", "media_session", "volume", "--stream", "3", "--get")
    m = re.search(r"range \[(\d+)\.\.(\d+)\]", out)
    if not m:
        manual_or_fail("媒體音量", "請調整至目標端點。")
        return
    target = int(m.group(1)) if value == "min" else int(m.group(2))
    STATE_ACTIONS.append(f"adb shell cmd audio set-volume 3 {target}")
    # cmd audio set-volume 在 Pixel 10a/Android 16 實測可靠；
    # media_session --set 會靜默失敗，保留當備援
    adb("shell", "cmd", "audio", "set-volume", "3", str(target))
    adb("shell", "cmd", "media_session", "volume", "--stream", "3", "--set", str(target))
    after = adb("shell", "cmd", "media_session", "volume", "--stream", "3", "--get")
    actual = re.search(r"volume is\s+(\d+)\s+in range\s+\[(\d+)\.\.(\d+)\]", after, re.I)
    if not actual or int(actual.group(1)) != target:
        # cmd media_session --set 在部分機型（Pixel 10a/Android 16）靜默無效；退回音量鍵逐步推到端點
        key = "KEYCODE_VOLUME_DOWN" if value == "min" else "KEYCODE_VOLUME_UP"
        span = int(m.group(2)) - int(m.group(1))
        STATE_ACTIONS.append(f"adb shell input keyevent {key} x{span}")
        for _ in range(span):
            adb("shell", "input", "keyevent", key)
        subprocess.run(["sleep", "2"])  # keyevent 進位有延遲，等落定再讀
        after = adb("shell", "cmd", "media_session", "volume", "--stream", "3", "--get")
        actual = re.search(r"volume is\s+(\d+)\s+in range\s+\[(\d+)\.\.(\d+)\]", after, re.I)
        if actual and int(actual.group(1)) != target:
            for _ in range(abs(target - int(actual.group(1)))):
                adb("shell", "input", "keyevent", key)
            subprocess.run(["sleep", "2"])
            after = adb("shell", "cmd", "media_session", "volume", "--stream", "3", "--get")
            actual = re.search(r"volume is\s+(\d+)\s+in range\s+\[(\d+)\.\.(\d+)\]", after, re.I)
    if not actual or int(actual.group(1)) != target:
        manual_or_fail("媒體音量", f"設定 {value} 後讀回失敗：{after}")
    STATE_ACTIONS.append(
        f"volume after: current={actual.group(1)}, min={actual.group(2)}, max={actual.group(3)}, target={value}")
    print(f"[自動] 媒體音量 → {target}（{value}）；讀回 {actual.group(1)}/{actual.group(3)}")


def _location_granted():
    """讀回 runtime 權限 ground truth：任一 FINE/COARSE granted 即視為「app 有定位」。
    回 True=有定位 / False=完全沒有 / None=讀不到。"""
    dump = adb("shell", "dumpsys", "package", APP_PACKAGE)
    vals = []
    for perm in ("ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION"):
        m = re.search(perm + r":\s*granted=(true|false)", dump)
        if m:
            vals.append(m.group(1) == "true")
    if not vals:
        return None
    return any(vals)


def set_location(grant):
    # revoke 只動 FINE 會留下 COARSE（restore 同時給兩者）→ 「拒絕」情境其實仍有粗定位、
    # bid 仍帶 geo，卻被當拒絕驗（假 PASS/FAIL）。FINE+COARSE 兩個機制（pm + appops）都要對齊。
    verb = "grant" if grant else "revoke"
    op = "allow" if grant else "ignore"
    out = ""
    for perm in ("android.permission.ACCESS_FINE_LOCATION",
                 "android.permission.ACCESS_COARSE_LOCATION"):
        out += adb("shell", "pm", verb, APP_PACKAGE, perm) + "\n"
    for opname in ("ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION"):
        adb("shell", "appops", "set", APP_PACKAGE, opname, op)
    actual = _location_granted()   # ground-truth 讀回
    cmd_err = "exception" in out.lower() or "not requested" in out.lower()
    if cmd_err or (actual is not None and actual != grant):
        manual_or_fail("Location permission",
                       f"請將 App 的 Location 權限（含粗略定位）切為 {'allowed' if grant else 'denied'}。",
                       action="android.settings.APPLICATION_DETAILS_SETTINGS")
        actual = _location_granted()
        if actual is not None and actual != grant:
            raise AutomationError(
                f"Location：讀回 granted={actual}，預期 {grant}（FINE+COARSE 未對齊）")
    print(f"[自動] Location permission → {'allowed' if grant else 'denied'}"
          f"（FINE+COARSE；讀回 granted={actual}）")


def auto_common(high):
    # high=False: M1 default/low；high=True: M2 opposite/high。
    print("\n── 自動設定裝置狀態 ──")
    adb("shell", "cmd", "uimode", "night", "yes" if high else "no")
    print(f"[自動] Dark mode → {'on' if high else 'off'}")
    set_and_verify("Battery Saver", ("shell", "cmd", "power", "set-mode", "1" if high else "0"),
                   ("shell", "settings", "get", "global", "low_power"), "1" if high else "0",
                   "android.settings.BATTERY_SAVER_SETTINGS")
    adb("shell", "settings", "put", "system", "screen_brightness_mode", "0")
    set_and_verify("Brightness", ("shell", "settings", "put", "system", "screen_brightness", "255" if high else "0"),
                   ("shell", "settings", "get", "system", "screen_brightness"), "255" if high else "0",
                   "android.settings.DISPLAY_SETTINGS")
    set_and_verify("Font scale", ("shell", "settings", "put", "system", "font_scale", "1.5" if high else "1.0"),
                   ("shell", "settings", "get", "system", "font_scale"), "1.5" if high else "1.0",
                   "android.settings.TEXT_READING_SETTINGS")
    set_volume("max" if high else "min")
    set_location(not high)


def run_capture(label, tcs, env, dwell=0, fgbg=False, action=""):
    global STATE_ACTIONS
    action_trace = "; ".join([action] + STATE_ACTIONS) if STATE_ACTIONS else action
    run_env = {**os.environ, **env, "CAPTURE_LABEL": label,
               "DWELL_SEC": str(dwell), "DO_FGBG": "1" if fgbg else "0",
               "STATE_ACTION": action_trace}
    print(f"\n{'='*18} {label}: Capture {'='*18}")
    cmd = [sys.executable, str(ROOT / "run_ssp.py")]
    if tcs:
        cmd.append(",".join(tcs))
    result = subprocess.run(cmd, env=run_env)
    STATE_ACTIONS = []
    if result.returncode == 2:
        print("\n[本 round 未完成] Sample App 沒有觸發 Appier bid request。")
    elif result.returncode == 3:
        print("\n[本 round 未完成] 回 204 No Fill，未取得指定廣告；不建立正式 Capture。")
    elif result.returncode == 4:
        print("\n[本 round 未完成] 未能驗證指定 CID 的 loaded ad；不建立正式 Capture。")
    elif result.returncode:
        print(f"\n[本 round 未完成] Capture 執行失敗（exit {result.returncode}）。")
    return result.returncode


def choose_env():
    test_type = os.environ.get("TEST_TYPE", "").strip()
    if not test_type:
        test_type = select_menu("投放目的", [
            ("AIBID", "aibid"), ("REEN Static", "reen-static"),
            ("REEN Dynamic", "reen-dynamic"),
        ])
    test_mode = os.environ.get("TEST_MODE", "").strip()
    if not test_mode:
        test_mode = select_menu("SDK 整合模式", [
            ("Standalone", "standalone"), ("AdMob Mediation", "admob-mediation"),
            ("AppLovin Mediation", "applovin-mediation"),
        ])
    cid = os.environ.get("TEST_CID", "").strip()
    while not cid:
        cid = input("Test CID：").strip()
    return {
        "TEST_TYPE": test_type, "TEST_MODE": test_mode, "TEST_CID": cid,
        "TEST_ROUND": os.environ.get("TEST_ROUND", "Run_" + __import__("datetime").datetime.now().strftime("%Y%m%d")),
    }


def _phase_m1(env):
    print("\n===== M1：Default / Low / Allowed =====")
    ensure_tracking(True)
    ensure_app_locale("en-US")
    ensure_battery("電池／充電", level=100, charging=False)
    ensure_vpn(False)
    ensure_timezone("Asia/Taipei", "台北時區")
    auto_common(False)
    return run_capture("M1", ["AND-01", "AND-75", "AND-05", "AND-07", "AND-09", "AND-10",
                              "AND-13", "AND-15", "AND-16", "AND-19", "AND-21", "AND-23",
                              "AND-25", "AND-31", "AND-45", "AND-48"], env,
                       action="Auto M1：default/low/allowed")


def _phase_m2(env):
    print("\n===== M2：Opposite / High / Denied =====")
    # REEN 靠 GAID 對受眾：opt-out 後 campaign 一律 204 no-bid，與 CID 鎖定
    # capture 互斥 → REEN 輪不驗 AND-02/AND-76（於 AIBID 輪驗證）。
    # 實證 2026-07-15：opt-out 下 40/40 attempts 全 204；opt-in 下首發即 200。
    reen = env["TEST_TYPE"].startswith("reen")
    # Battery Saver（AND-08）在系統認為充電中時無法開啟 → M2 必須 unplug mock。
    # （2026-07-15 曾疑 unplug mock 造成 adb 掉線，後查為 Appium server 崩潰所致）
    ensure_battery("M2 low battery / unplugged", level=0, charging=False)
    auto_common(True)
    if reen:
        print("[略過] AND-02/AND-76（GAID opt-out）與 REEN 投遞互斥，本輪不驗")
        ensure_tracking(True)
    else:
        ensure_tracking(False)
    ensure_vpn(True)
    ensure_timezone("America/New_York", "紐約時區")
    m2_tcs = ([] if reen else ["AND-02", "AND-76"]) + [
        "AND-04", "AND-08", "AND-14", "AND-17", "AND-20"]
    return run_capture("M2", m2_tcs + [
                           "AND-22", "AND-24", "AND-26", "AND-46", "AND-50", "AND-52"],
                       env, dwell=35, fgbg=True,
                       action="Auto M2：opposite/high/denied")


def _phase_m3(env):
    print("\n===== M3：Charging / UTC =====")
    set_and_verify("Battery Saver", ("shell", "cmd", "power", "set-mode", "0"),
                   ("shell", "settings", "get", "global", "low_power"), "0")
    ensure_battery("充電", charging=True)
    ensure_timezone("UTC", "UTC 時區")
    return run_capture("M3", ["AND-06", "AND-27"], env,
                       action="Auto M3：charging + UTC")


def _phase_session(env):
    # user.session_duration＝App 前景累積時間（iOS 實作同語意）。
    # 三情境各跑一個 capture：bid A → 情境動作 → bid B 對照（run_ssp SESSION_CASE）。
    print("\n===== SC：user.session_duration 三情境（App 前景時間）=====")
    # session baseline 必須從乾淨標準狀態量；SC 排在 M2/M3 之後，不先還原會沾到
    # opt-out/VPN-on/UTC/暗色/滿亮度殘留，且 opt-out 可能填不到廣告。
    restore_standard_state()
    # SC capture 給有界重試（預設 40），避免殘留/低填充狀態下 run_ssp 無限 retry 卡死。
    sc_attempts = os.environ.get("MAX_AD_ATTEMPTS", "40")
    rc = 0
    for case, desc in (("1", "只關廣告頁→累進"), ("2", "殺整個 App→重置"),
                       ("3", "背景切回→累進")):
        tc = f"AND-47-{case}"
        code = run_capture(tc, [tc],
                           {**env, "SESSION_CASE": case, "MAX_AD_ATTEMPTS": sc_attempts},
                           dwell=10, action=f"Auto SC{case}：{desc}")
        rc = rc or code
    return rc


def _phase_auto(env):
    # baseline 放最後：先還原標準狀態，避免沾到前面互斥狀態批次的殘留。
    print("\n===== AUTO：Baseline（還原標準狀態後執行）=====")
    restore_standard_state()
    return run_capture("AUTO", [], env, action="自動 baseline（其他狀態 capture 之後執行）")


# 同一個 TEST_ROUND 內的自動狀態批次；SC 次之、AUTO baseline 最後。
PHASE_ORDER = ["M1", "M2", "M3", "SC", "AUTO"]
PHASES = {"M1": _phase_m1, "M2": _phase_m2, "M3": _phase_m3,
          "SC": _phase_session, "AUTO": _phase_auto}


def main():
    if not APP_PACKAGE or not os.environ.get("APP_ACTIVITY"):
        sys.exit("請先設定 APP_PACKAGE 與 APP_ACTIVITY")
    env = choose_env()
    atexit.register(restore_standard_state)
    start_at = os.environ.get("START_AT", "M1").upper()
    if start_at not in PHASE_ORDER:
        sys.exit("START_AT 必須是 M1、M2、M3、SC 或 AUTO")

    # 單一狀態批次失敗（狀態建不起來 / capture 沒命中）不擋同 round 其他批次；
    # 缺的批次可用 START_AT 單獨補、STOP_AFTER 提前收尾。
    stop_after = os.environ.get("STOP_AFTER", "").upper()
    phase_names = PHASE_ORDER[PHASE_ORDER.index(start_at):]
    if stop_after in PHASE_ORDER:
        phase_names = [p for p in phase_names
                       if PHASE_ORDER.index(p) <= PHASE_ORDER.index(stop_after)]
    incomplete = []
    for name in phase_names:
        try:
            rc = PHASES[name](env)
        except AutomationError as exc:
            print(f"\n[{name} 跳過] {exc}")
            incomplete.append(name)
            continue
        if rc:
            incomplete.append(name)
    try:
        retry_failed_rounds(env)
    except AutomationError as exc:
        print(f"\n[Retry 中斷] {exc}")
    if incomplete:
        print(f"\n完整 TC round 部分完成：{'、'.join(incomplete)} 狀態批次未完成，其餘已合併。"
              "root/emulator/SIM 另列硬體輪次。")
    else:
        print("\n完整 TC round 已完成並合併；root/emulator/SIM 另列硬體輪次。")
    build_and_open_report(env)


if __name__ == "__main__":
    main()
