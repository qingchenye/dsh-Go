#!/usr/bin/env python3
"""Give the Android-only dsh-go Flutter app a stock iOS host project.

Upstream (`app/`) has no `ios/` directory: every native feature is Kotlin. This
script generates an *unmodified stock* Flutter iOS host with `flutter create`,
then patches the host so the Dart app can be compiled and shipped as an
unsigned `.ipa` for sideloading:

  * iOS deployment target raised (the plugins need 16.0: mobile_scanner,
    flutter_webrtc, file_picker ...),
  * bundle id / display name / URL scheme set,
  * Info.plist gains the permission strings, ATS relaxation (the app talks plain
    http/ws to the local engine, SSH/WebRTC to a PC) and file-sharing keys,
  * AppIcon is regenerated from the Android launcher icon when `sips` exists.

It is idempotent and never touches `app/lib` or `app/android`. It is NOT a port:
the Android-only native modules (local engine, root/Shizuku shell, APK
installer ...) have no iOS implementation, so those features fail at runtime.
The client-side features (remote control, chat, forum, webview) are pure Dart.

Environment overrides:
  REPO_ROOT, APP_DIR, FLUTTER, IOS_DEPLOYMENT_TARGET, IOS_BUNDLE_ID,
  IOS_DISPLAY_NAME, IOS_ORG, IOS_URL_SCHEME
"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = Path(os.environ.get("REPO_ROOT") or HERE.parents[2]).resolve()
APP = (ROOT / (os.environ.get("APP_DIR") or "app")).resolve()
FLUTTER = os.environ.get("FLUTTER") or "flutter"

DEPLOYMENT_TARGET = os.environ.get("IOS_DEPLOYMENT_TARGET") or "16.0"
DISPLAY_NAME = os.environ.get("IOS_DISPLAY_NAME") or "dsh-go"
URL_SCHEME = os.environ.get("IOS_URL_SCHEME") or "dshgo"
ORG = os.environ.get("IOS_ORG") or "io.github.ccch1mneyyy"

PERMISSIONS = {
    "NSCameraUsageDescription": "扫描二维码、拍照发给 DSH 需要相机权限。",
    "NSMicrophoneUsageDescription": "语音输入与语音通话需要麦克风权限。",
    "NSPhotoLibraryUsageDescription": "从相册选择图片发送给 DSH。",
    "NSPhotoLibraryAddUsageDescription": "把 DSH 生成的图片保存到相册。",
    "NSSpeechRecognitionUsageDescription": "把语音转成文字输入。",
    "NSLocalNetworkUsageDescription": "连接同一局域网内的电脑（DSH Remote / SSH）。",
}


def log(msg: str) -> None:
    print(f"ios_prepare: {msg}", flush=True)


def die(msg: str) -> None:
    print(f"ios_prepare: error: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd, cwd=None):
    log("+ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run([str(c) for c in cmd], cwd=cwd, text=True, capture_output=True)
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        if proc.stderr.strip():
            print(proc.stderr.strip(), file=sys.stderr)
        die(f"command failed (exit {proc.returncode}): {' '.join(str(c) for c in cmd)}")


def pubspec_value(key: str) -> str:
    text = (APP / "pubspec.yaml").read_text(encoding="utf-8")
    m = re.search(rf"^{key}:\s*(\S+)", text, re.M)
    if not m:
        die(f"app/pubspec.yaml has no `{key}:`")
    return m.group(1)


PROJECT_NAME = pubspec_value("name")
BUNDLE_ID = os.environ.get("IOS_BUNDLE_ID") or f"{ORG}.{PROJECT_NAME.replace('_', '')}"
IOS = APP / "ios"


# --------------------------------------------------------------------------- #
# 1. Stock iOS host project
# --------------------------------------------------------------------------- #
def scaffold_ios() -> None:
    if (IOS / "Runner.xcodeproj" / "project.pbxproj").exists():
        log("app/ios already exists, patching it in place")
        return
    tmp = Path(tempfile.mkdtemp(prefix="dsh-ios-scaffold-"))
    try:
        run(
            [
                FLUTTER, "create", "--no-pub", "--platforms=ios",
                "--org", ORG, "--project-name", PROJECT_NAME,
                "--template=app", tmp / "scaffold",
            ],
            cwd=ROOT,
        )
        src = tmp / "scaffold" / "ios"
        if not (src / "Runner.xcodeproj" / "project.pbxproj").exists():
            die(f"flutter create produced no ios project at {src}")
        shutil.copytree(src, IOS)
        log(f"created stock iOS host from flutter create ({PROJECT_NAME}, {ORG})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 2. Xcode project
# --------------------------------------------------------------------------- #
def patch_pbxproj() -> None:
    path = IOS / "Runner.xcodeproj" / "project.pbxproj"
    text = path.read_text(encoding="utf-8")
    before = text

    text, n_target = re.subn(
        r"IPHONEOS_DEPLOYMENT_TARGET = [0-9.]+;",
        f"IPHONEOS_DEPLOYMENT_TARGET = {DEPLOYMENT_TARGET};",
        text,
    )
    if n_target == 0:
        die("no IPHONEOS_DEPLOYMENT_TARGET in project.pbxproj")

    def replace_bundle_id(m: re.Match) -> str:
        quote, value = m.group(1) or "", m.group(2)
        suffix = ".RunnerTests" if value.endswith(".RunnerTests") else ""
        return f'PRODUCT_BUNDLE_IDENTIFIER = {quote}{BUNDLE_ID}{suffix}{quote};'

    text, n_bundle = re.subn(
        r'PRODUCT_BUNDLE_IDENTIFIER = ("?)([^";]+)\1;', replace_bundle_id, text
    )
    if n_bundle == 0:
        die("no PRODUCT_BUNDLE_IDENTIFIER in project.pbxproj")

    # Flutter's own script phases must be allowed to read/write the build dir.
    text = text.replace("ENABLE_USER_SCRIPT_SANDBOXING = YES;", "ENABLE_USER_SCRIPT_SANDBOXING = NO;")

    if text == before:
        log("project.pbxproj already patched")
        return
    path.write_text(text, encoding="utf-8")
    log(
        f"project.pbxproj: {n_target}x deployment target {DEPLOYMENT_TARGET}, "
        f"{n_bundle}x bundle id {BUNDLE_ID}"
    )


def patch_app_framework_info() -> None:
    path = IOS / "Flutter" / "AppFrameworkInfo.plist"
    if not path.exists():
        return
    data = plistlib.loads(path.read_bytes())
    data["MinimumOSVersion"] = DEPLOYMENT_TARGET
    path.write_bytes(plistlib.dumps(data, sort_keys=False))
    log(f"AppFrameworkInfo.plist: MinimumOSVersion = {DEPLOYMENT_TARGET}")


# --------------------------------------------------------------------------- #
# 3. CocoaPods
# --------------------------------------------------------------------------- #
def patch_podfile() -> None:
    path = IOS / "Podfile"
    if not path.exists():
        die("flutter create produced no ios/Podfile")
    text = path.read_text(encoding="utf-8")

    # Flutter's template ships a commented "global platform" line in some
    # versions and an active one in others; handle both.
    platform_re = re.compile(r"^[#\s]*platform :ios, '[^']*'.*$", re.M)
    if platform_re.search(text):
        text = platform_re.sub(f"platform :ios, '{DEPLOYMENT_TARGET}'", text, count=1)
    else:
        text = text.replace("target 'Runner' do", f"platform :ios, '{DEPLOYMENT_TARGET}'\n\ntarget 'Runner' do", 1)

    target_line = f"IPHONEOS_DEPLOYMENT_TARGET'] = '{DEPLOYMENT_TARGET}'"
    if target_line not in text:
        call = "flutter_additional_ios_build_settings(target)"
        if call in text:
            text = text.replace(
                call,
                call
                + "\n      target.build_configurations.each do |config|"
                + f"\n        config.build_settings['{target_line}"
                + "\n      end",
                1,
            )
        else:
            text = text.rstrip() + (
                "\n\npost_install do |installer|\n"
                "  installer.pods_project.targets.each do |target|\n"
                "    flutter_additional_ios_build_settings(target)\n"
                "    target.build_configurations.each do |config|\n"
                f"      config.build_settings['{target_line}\n"
                "    end\n"
                "  end\n"
                "end\n"
            )
    path.write_text(text, encoding="utf-8")
    log(f"Podfile: platform :ios, '{DEPLOYMENT_TARGET}' + per-pod deployment target")


# --------------------------------------------------------------------------- #
# 4. Info.plist
# --------------------------------------------------------------------------- #
def patch_info_plist() -> None:
    path = IOS / "Runner" / "Info.plist"
    if not path.exists():
        die("flutter create produced no ios/Runner/Info.plist")
    info = plistlib.loads(path.read_bytes())

    info["CFBundleDisplayName"] = DISPLAY_NAME
    info["CFBundleName"] = PROJECT_NAME           # <= 15 chars, pubspec name
    info["ITSAppUsesNonExemptEncryption"] = False  # no export paperwork, no App Store
    info["UIRequiredDeviceCapabilities"] = ["arm64"]

    # The app talks plain http/ws to the local engine and to PCs on the LAN.
    info["NSAppTransportSecurity"] = {
        "NSAllowsArbitraryLoads": True,
        "NSAllowsLocalNetworking": True,
    }
    info.update(PERMISSIONS)

    # In-app files browser: needed to drop in skill/plugin archives from Files.
    info["UIFileSharingEnabled"] = True
    info["LSSupportsOpeningDocumentsInPlace"] = True

    # lib/links: dshgo://open/... deep links.
    schemes = [URL_SCHEME]
    url_types = info.get("CFBundleURLTypes") or []
    existing = {
        s for entry in url_types for s in (entry.get("CFBundleURLSchemes") or [])
    }
    if URL_SCHEME not in existing:
        url_types.append(
            {
                "CFBundleTypeRole": "Editor",
                "CFBundleURLName": BUNDLE_ID,
                "CFBundleURLSchemes": schemes,
            }
        )
        info["CFBundleURLTypes"] = url_types

    path.write_bytes(plistlib.dumps(info, sort_keys=False))
    log(f"Info.plist: display name, {len(PERMISSIONS)} permission strings, ATS, URL scheme {URL_SCHEME}://")


# --------------------------------------------------------------------------- #
# 5. App icon (best effort: macOS `sips` only)
# --------------------------------------------------------------------------- #
# (size, scale, idiom, pixels) — the classic multi-size layout every Xcode from
# 12 to 26 accepts, so no actool surprises on the runner.
ICON_SLOTS = [
    ("20x20", "2x", "iphone", 40), ("20x20", "3x", "iphone", 60),
    ("29x29", "2x", "iphone", 58), ("29x29", "3x", "iphone", 87),
    ("40x40", "2x", "iphone", 80), ("40x40", "3x", "iphone", 120),
    ("60x60", "2x", "iphone", 120), ("60x60", "3x", "iphone", 180),
    ("20x20", "1x", "ipad", 20), ("20x20", "2x", "ipad", 40),
    ("29x29", "1x", "ipad", 29), ("29x29", "2x", "ipad", 58),
    ("40x40", "1x", "ipad", 40), ("40x40", "2x", "ipad", 80),
    ("76x76", "1x", "ipad", 76), ("76x76", "2x", "ipad", 152),
    ("83.5x83.5", "2x", "ipad", 167),
    ("1024x1024", "1x", "ios-marketing", 1024),
]


def patch_app_icon() -> None:
    sips = shutil.which("sips")
    if not sips:
        log("sips unavailable (not macOS): keeping the stock Flutter icon")
        return
    # Android's 192 px legacy launcher icon is the only square artwork in the
    # repo; it is upscaled. Everything better lives in Kotlin/painters.
    candidates = [
        APP / "android" / "app" / "src" / "main" / "res" / "mipmap-xxxhdpi" / "ic_launcher.png",
        APP / "android" / "app" / "src" / "main" / "res" / "mipmap-xxhdpi" / "ic_launcher.png",
    ]
    source = next((p for p in candidates if p.exists() and p.stat().st_size > 1000), None)
    if source is None:
        log("no icon source found: keeping the stock Flutter icon")
        return
    icon_dir = IOS / "Runner" / "Assets.xcassets" / "AppIcon.appiconset"
    icon_dir.mkdir(parents=True, exist_ok=True)
    images = []
    for slot, scale, idiom, px in ICON_SLOTS:
        name = f"Icon-App-{slot}@{scale}-{idiom}.png"
        subprocess.run(
            [sips, "-s", "format", "png", "-z", str(px), str(px), str(source), "--out", str(icon_dir / name)],
            check=True, capture_output=True,
        )
        images.append({"size": slot, "idiom": idiom, "filename": name, "scale": scale})
    contents = {"images": images, "info": {"author": "xcode", "version": 1}}
    (icon_dir / "Contents.json").write_text(json.dumps(contents, indent=2) + "\n", encoding="utf-8")
    log(f"AppIcon: regenerated {len(ICON_SLOTS)} sizes from {source.relative_to(APP)}")


# --------------------------------------------------------------------------- #
# 6. Verify
# --------------------------------------------------------------------------- #
def verify() -> None:
    pbx = (IOS / "Runner.xcodeproj" / "project.pbxproj").read_text(encoding="utf-8")
    assert f"IPHONEOS_DEPLOYMENT_TARGET = {DEPLOYMENT_TARGET};" in pbx
    assert BUNDLE_ID in pbx
    pod = (IOS / "Podfile").read_text(encoding="utf-8")
    assert f"platform :ios, '{DEPLOYMENT_TARGET}'" in pod
    info = plistlib.loads((IOS / "Runner" / "Info.plist").read_bytes())
    assert info["CFBundleDisplayName"] == DISPLAY_NAME
    assert all(k in info for k in PERMISSIONS)
    assert info["NSAppTransportSecurity"]["NSAllowsArbitraryLoads"] is True
    log("verification passed")


def main() -> int:
    log(f"root={ROOT}")
    log(f"app={APP}")
    log(f"target={DEPLOYMENT_TARGET} bundle={BUNDLE_ID} name={DISPLAY_NAME} project={PROJECT_NAME}")
    if not (APP / "pubspec.yaml").exists():
        die(f"{APP}/pubspec.yaml not found")
    scaffold_ios()
    patch_pbxproj()
    patch_app_framework_info()
    patch_podfile()
    patch_info_plist()
    patch_app_icon()
    verify()
    log("iOS host project ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
