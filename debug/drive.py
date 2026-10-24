"""Temporary: drive the installed Highlight Autoclip backend.exe like the app does, and export clips.

python debug/drive.py <install dir>
Prints every render's state and error. Synthetic test media only; no personal data.
"""

import ctypes
import glob
import json
import os
import py_compile
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from ctypes import wintypes
from pathlib import Path

INSTALL = Path(sys.argv[1])
APP = INSTALL / "highlight-autoclip-tool.exe"  # the Electron binary (AUTOCLIP_NODE)
VIDEOS = Path(os.environ["VIDEOS_DIR"])
TEMP = Path(os.environ["RUNNER_TEMP"])
FFMPEG = glob.glob(str(INSTALL / "resources" / "backend" / "**" / "ffmpeg*.exe"), recursive=True)[0]
ONLY = set(filter(None, os.environ.get("CASES", "").split(",")))
RESULTS = []
DATA = None


def log(*a):
    print(*a, flush=True)


class Backend:
    def __init__(self, exe, data):
        global DATA
        DATA = data
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
        env.update(AUTOCLIP_DATA_DIR=str(data), AUTOCLIP_VIDEOS_DIR=str(VIDEOS), AUTOCLIP_NODE=str(APP),
                   ELECTRON_RUN_AS_NODE="1")
        self.p = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.pending, self.n, self.lock = {}, 0, threading.Lock()
        threading.Thread(target=self._out, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _out(self):
        for raw in self.p.stdout:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                log("[backend stdout]", raw[:300])
                continue
            if "event" in msg:
                continue
            q = self.pending.pop(msg.get("id"), None)
            if q:
                q.put(msg)

    def _err(self):
        for raw in self.p.stderr:
            log("[backend stderr]", raw.decode("utf-8", "replace").rstrip())

    def call(self, method, timeout=180, **params):
        with self.lock:
            self.n += 1
            id = self.n
        q = queue.Queue()
        self.pending[id] = q
        # Electron's JSON.stringify: raw UTF-8, nothing escaped
        self.p.stdin.write((json.dumps({"id": id, "method": method, "params": params}, ensure_ascii=False) + "\n").encode())
        self.p.stdin.flush()
        msg = q.get(timeout=timeout)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg["result"]

    def stop(self):
        self.p.kill()
        self.p.wait()


def sql(query, *args):
    with sqlite3.connect(DATA / "library.sqlite", timeout=30) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(query, args)]


def ff(*args, timeout=600):
    r = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y", *args], capture_output=True,
                       timeout=timeout)
    if r.returncode:
        log("ffmpeg failed:", r.stderr.decode("utf-8", "replace")[-600:])
    return r.returncode == 0


def wait(fn, timeout, step=1.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def segment(person, source, name, intervals=((2.0, 8.0), (10.0, 14.0))):
    with sqlite3.connect(DATA / "library.sqlite", timeout=30) as c:
        return c.execute(
            "insert into segment(person_id, source_id, start_sid, end_sid, start, end, intervals, name, rationale,"
            " status) values(?, ?, 0, 9, ?, ?, ?, ?, ?, 'ok')",
            (person, source, intervals[0][0], intervals[-1][1], json.dumps(intervals), name,
             json.dumps({"fade_in": 0.15, "fade_out": 0.3}))).lastrowid


def export(b, seg, label, quiet=False):
    """render.segment as the Clip page calls it, then wait for the row to finish."""
    rid = b.call("render.segment", id=seg)["render_id"]
    r = wait(lambda: next((x for x in sql("select * from render where id = ?", rid)
                           if x["state"] in ("done", "failed")), None), 300, 0.5)
    if not r:
        r = (sql("select * from render where id = ?", rid) or [{}])[0]
    checks = json.loads(r.get("checks") or "null")
    path = r.get("path")
    res = {"case": label, "state": r.get("state"), "error": (checks or {}).get("error"),
           "path_len": len(path) if path else None, "path": path}
    if not quiet:
        RESULTS.append(res)
        log("RESULT", json.dumps(res, ensure_ascii=False))
    return res


# --- holders: another program with the earlier export open, by Windows share mode --------------------

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                            wintypes.DWORD, wintypes.HANDLE]
READ, WRITE, DELETE = 1, 2, 4


def hold(path, share):
    h = k32.CreateFileW(path, 0x80000000, share, None, 3, 0x80, None)  # GENERIC_READ, OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateFileW")
    return h


def holders(b, person, source, tag):
    """Re-export a clip while its earlier export is held open with each share mode."""
    for label, share in (("read", READ), ("read|write", READ | WRITE), ("read|write|delete (libuv = media://)",
                         READ | WRITE | DELETE), ("read|delete", READ | DELETE)):
        seg = segment(person, source, f"Giữ file {share}")
        first = export(b, seg, "first", quiet=True)
        if first["state"] != "done":
            log("first export failed", first)
            continue
        h = hold(first["path"], share)
        try:
            export(b, seg, f"{tag}: re-export while held open, share {label}")
        finally:
            k32.CloseHandle(h)
    vlc = Path(r"C:\Program Files\VideoLAN\VLC\vlc.exe")
    if vlc.exists():
        seg = segment(person, source, "Giữ file VLC")
        path = export(b, seg, "first", quiet=True)["path"]
        player = subprocess.Popen([str(vlc), "-I", "dummy", "--dummy-quiet", "--vout=dummy", "--aout=dummy", "--loop", path])
        time.sleep(5)
        export(b, seg, f"{tag}: re-export while playing in VLC")
        player.kill()
        player.wait()
    # Electron itself: Node fs.openSync, exactly what media:// createReadStream does
    seg = segment(person, source, "Giữ file Node")
    path = export(b, seg, "first", quiet=True)["path"]
    holder = subprocess.Popen([str(APP), "-e", "const fs=require('fs');const fd=fs.openSync(process.argv[1],'r');"
                               "console.log('node holds',fd);setTimeout(()=>{},90000)", path],
                              env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"}, stdout=subprocess.PIPE, text=True)
    log(holder.stdout.readline().strip())
    export(b, seg, f"{tag}: re-export while open by Electron-as-Node fs (media:// stream)")
    holder.kill()
    holder.wait()


def make_sources(folder):
    folder.mkdir(parents=True, exist_ok=True)
    v = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i",
         "anoisesrc=color=pink:sample_rate=48000:amplitude=0.2", "-t", "20", "-ac", "2"]
    made = {
        "Trấn Thành – Hài độc thoại ＂Bố già＂ ｜ Đặc biệt (Full Show) [Youtube-a1B2c3D4e5F].mp4":
            [*v, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-ar", "44100", "-movflags", "+faststart"],
    }
    for name, args in made.items():
        out = folder / name
        if not out.exists():
            log("making", name, ff(*args, str(out)))
    return len(made)


def setup(b):
    p = b.call("person.create", name="Trấn Thành")
    log("person", p["id"], p["slug"], p["storage"])
    folder = VIDEOS / "Nguồn video ğİş"
    n = make_sources(folder)
    log("folders.attach", b.call("folders.attach", person_id=p["id"], paths=[str(folder)]))
    wait(lambda: len(sql("select 1 from source where folder_id is not null and status != 'scanning'")) >= n, 300, 1)
    time.sleep(2)
    s = sql("select * from source where status = 'ready' and folder_id is not null")[0]
    log("source", s["id"], s["vcodec"], s["acodec"], s["duration"])
    return p, s


def download(b, person):
    """The app's own downloader on a real, publicly hosted file (YouTube bot-checks this runner)."""
    url = "https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4"
    r = b.call("links.resolve", person_id=person, url=url, timeout=120)
    log("links.resolve ->", r.get("kind"), r.get("reason") or r.get("title"))
    if r.get("kind") != "video":
        return None
    b.call("downloads.add", person_id=person, items=[r])
    d = wait(lambda: next((x for x in sql("select * from download where site_key = ?", r["site_key"])
                           if x["state"] in ("done", "failed")), None), 300, 2)
    log("download:", d and d["state"], d and d["error"])
    s = wait(lambda: next((x for x in sql("select * from source where download_id is not null")
                           if x["status"] != "scanning"), None), 120, 1)
    log("downloaded source:", s and (s["status"], s["vcodec"], s["acodec"], s["path"]))
    return s if s and s["status"] == "ready" else None


def shipped():
    b = Backend(INSTALL / "resources" / "backend" / "backend.exe", TEMP / "data-shipped")
    try:
        log("ping", b.call("ping", timeout=120))
        p, s = setup(b)
        if not ONLY or "download" in ONLY:
            d = download(b, p["id"])
            if d:
                export(b, segment(p["id"], d["id"], "Thỏ khổng lồ: “cảnh mở đầu”", ((30.0, 40.0), (45.0, 52.0))),
                       "real file through the app's downloader")
        if not ONLY or "fresh" in ONLY:
            # first exports and immediate re-exports with nothing holding them (Defender scans on close)
            fails = []
            for i in range(10):
                seg = segment(p["id"], s["id"], f"Lần xuất mới số {i}: “đi chợ Tết”?")
                for j in range(3):
                    r = export(b, seg, f"fresh {i}.{j}", quiet=True)
                    if r["state"] != "done":
                        fails.append(r)
            res = {"case": "30 exports (10 new files, each re-exported twice), no holder", "failed": len(fails),
                   "errors": sorted({f["error"] for f in fails})}
            RESULTS.append(res)
            log("RESULT", json.dumps(res, ensure_ascii=False))
        if not ONLY or "long" in ONLY:
            root = VIDEOS / ("Highlight Autoclip library for stand-up comedy clips and compilations " * 2).strip()
            lib = b.call("settings.get")["library_root"]
            b.call("settings.set", key="library_root", value=str(root))
            export(b, segment(p["id"], s["id"], "Long name ASCII " + "x" * 130), "path > 260 chars")
            b.call("settings.set", key="library_root", value=lib)
        if not ONLY or "holders" in ONLY:
            holders(b, p["id"], s["id"], "1.0.4")
    finally:
        b.stop()


# --- the proposed fix, applied to a copy of the installed app ------------------------------------------
# render.py's os.replace(tmp, out_path) is the only replace on a clip export's path. Its fix, a fallback that
# deletes the earlier file first, is patched into the frozen app's own os module (base_library.zip): the same
# CPython (3.12.10) compiles it, and only an "<x>.mp4.part" -> "<x>.mp4" replace takes the new branch.

PATCH = '''

import os as _os
import sys as _sys

_replace_unpatched = _os.replace


def _replace_fixed(src, dst):
    try:
        return _replace_unpatched(src, dst)
    except PermissionError:
        if not (str(src).endswith(".mp4.part") and str(dst).endswith(".mp4")):
            raise
        # Windows can't replace a file another program has open. Deleting it first works when that program
        # shares delete (Electron's media://, Chromium): the name is free at once (POSIX delete, Windows 10 1809+).
        try:
            _os.remove(dst)
        except PermissionError:
            raise ValueError("The clip's earlier file is open in another app. Close it, then try again.") from None
        return _replace_unpatched(src, dst)


if _os.replace is _replace_unpatched:
    _os.replace = _replace_fixed
    _sys.stderr.write("debug: os.replace patched\\n")
'''


def patched_copy():
    """os is frozen into python312.dll, so the patch rides in a stdlib module the app loads from base_library.zip."""
    dst = TEMP / "patched-app"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(INSTALL / "resources" / "backend", dst)
    base = dst / "_internal" / "base_library.zip"
    with zipfile.ZipFile(base) as z:
        names = z.namelist()
        blobs = {n: z.read(n) for n in names}
    log("base_library.zip:", len(names), "entries;", " ".join(sorted(n for n in names if "/" not in n))[:900])
    target = next(n for n in ("linecache.pyc", "traceback.pyc", "functools.pyc", "enum.pyc") if n in names)
    log("patching", target)
    work = TEMP / "patch"
    work.mkdir(exist_ok=True)
    src = Path(sys.base_prefix) / "Lib" / target.replace(".pyc", ".py")
    (work / "m.py").write_text(src.read_text(encoding="utf-8") + PATCH, encoding="utf-8")
    py_compile.compile(str(work / "m.py"), cfile=str(work / "m.pyc"), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    blobs[target] = (work / "m.pyc").read_bytes()
    base.unlink()
    with zipfile.ZipFile(base, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, blobs[n])
    return dst / "backend.exe"


def fixed():
    exe = patched_copy()
    b = Backend(exe, TEMP / "data-fixed")
    try:
        log("ping (patched)", b.call("ping", timeout=120))
        p, s = setup(b)
        holders(b, p["id"], s["id"], "with fix")
    finally:
        b.stop()


def main():
    log("python", sys.version)
    log("videos:", VIDEOS)
    shipped()
    if not ONLY or "fix" in ONLY:
        fixed()
    log("\n===== SUMMARY =====")
    for r in RESULTS:
        log(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
