# Highlight Autoclip

Turns full-length stand-up specials into highlight clips, topic compilations and vertical shorts.

## Install

Download from the [latest release](https://github.com/RedR25/highlight-autoclip-releases/releases/latest).

**macOS (Apple Silicon):** download the `.dmg` and drag **Highlight Autoclip** into **Applications**. It isn't notarized, so the first launch is blocked: open **System Settings → Privacy & Security** and click **Open Anyway**.

**Windows (64-bit):** download the `-setup.exe` and run it. It isn't code-signed, so SmartScreen may say "Windows protected your PC": click **More info → Run anyway**.

After that it updates itself: **Settings → Updates** (Stable or Beta channel). Every update is signed; the app refuses one that isn't.

## What's here

Only installers and update files. Each release holds:

- `…-macos-arm64.dmg`: to install on a Mac
- `…-macos-arm64.zip`: what the Mac app downloads to update itself
- `…-windows-x64-setup.exe`: to install on Windows, and what the Windows app updates with
- `latest.json` + `latest.json.sig`: the signed update manifest

Speech, matting and tracking models download on first use from their publishers.
