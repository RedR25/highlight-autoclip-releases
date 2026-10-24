"""Temporary: drive the installed Highlight Autoclip backend.exe like the app does, and export clips.

python debug/drive.py <install dir>
Prints every render's state and error. Synthetic test media only; no personal data.
"""

import glob
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

INSTALL = Path(sys.argv[1])
BACKEND = INSTALL / "resources" / "backend" / "backend.exe"
APP = INSTALL / "highlight-autoclip-tool.exe"  # the Electron binary (AUTOCLIP_NODE)
VIDEOS = Path(os.environ["VIDEOS_DIR"])
DATA = Path(os.environ["RUNNER_TEMP"]) / "autoclip-data"
FFMPEG = glob.glob(str(INSTALL / "resources" / "backend" / "**" / "ffmpeg*.exe"), recursive=True)[0]
ONLY = set(filter(None, os.environ.get("CASES", "").split(",")))
RESULTS = []


def log(*a):
    print(*a, flush=True)


class Backend:
    def __init__(self):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
        env.update(AUTOCLIP_DATA_DIR=str(DATA), AUTOCLIP_VIDEOS_DIR=str(VIDEOS), AUTOCLIP_NODE=str(APP),
                   ELECTRON_RUN_AS_NODE="1")
        self.p = subprocess.Popen([str(BACKEND)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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


def export(b, seg, label):
    """render.segment as the Clip page calls it, then wait for the row to finish."""
    rid = b.call("render.segment", id=seg)["render_id"]
    r = wait(lambda: next((x for x in sql("select * from render where id = ?", rid)
                           if x["state"] in ("done", "failed")), None), 300)
    if not r:
        r = (sql("select * from render where id = ?", rid) or [{}])[0]
    checks = json.loads(r.get("checks") or "null")
    path = r.get("path")
    res = {"case": label, "state": r.get("state"), "error": (checks or {}).get("error"),
           "qc_ok": (checks or {}).get("ok"), "problems": (checks or {}).get("problems"),
           "path_len": len(path) if path else None, "path": path,
           "exists": bool(path) and os.path.exists(path)}
    if path:
        try:
            res["media.allowed(render)"] = b.call("media.allowed", path=path)
        except Exception as e:
            res["media.allowed(render)"] = f"ERROR {e}"
    RESULTS.append(res)
    log("RESULT", json.dumps(res, ensure_ascii=False))
    return res


def make_sources(folder):
    folder.mkdir(parents=True, exist_ok=True)
    enc = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    dec = subprocess.run([FFMPEG, "-hide_banner", "-decoders"], capture_output=True, text=True).stdout
    log("ffmpeg:", subprocess.run([FFMPEG, "-version"], capture_output=True, text=True).stdout.splitlines()[0])
    log("av1 decoders:", [l.split()[1] for l in dec.splitlines() if "av1" in l.lower() and len(l.split()) > 1])
    av1 = next((e for e in ("libsvtav1", "libaom-av1", "librav1e") if f" {e} " in enc), None)
    log("av1 encoder:", av1, "| libvpx-vp9:", " libvpx-vp9 " in enc, "| libopus:", " libopus " in enc)
    v = ["-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-f", "lavfi", "-i",
         "sine=frequency=330:sample_rate=48000,volume=0.3", "-t", "20", "-ac", "2"]
    small = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i",
             "sine=frequency=330:sample_rate=48000,volume=0.3", "-t", "20", "-ac", "2"]
    vp9 = ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "800k", "-c:a", "libopus", "-b:a", "96k"]
    av1a = {"libsvtav1": ["-c:v", "libsvtav1", "-preset", "12"],
            "libaom-av1": ["-c:v", "libaom-av1", "-cpu-used", "8", "-row-mt", "1", "-b:v", "500k"],
            "librav1e": ["-c:v", "librav1e", "-speed", "10"]}.get(av1)
    made = {
        # yt-dlp windowsfilenames turns | " : ? into full-width look-alikes
        "Trấn Thành – Hài độc thoại ＂Bố già＂ ｜ Đặc biệt (Full Show) [Youtube-a1B2c3D4e5F].mp4":
            [*v, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
             "-ar", "44100", "-movflags", "+faststart"],
        "Cem Yılmaz – İstanbul Şov ÇĞİÖŞÜ çğıöşü [Youtube-x9Y8w7V6u5T].webm": [*small, *vp9],
        "Hài độc thoại VP9 Opus trong mp4 [Youtube-vp9mp4AAAAA].mp4": [*small, *vp9],
    }
    if av1a:
        made["Độc thoại AV1 Opus webm [Youtube-av1webmAAAA].webm"] = [*small, *av1a, "-c:a", "libopus", "-b:a", "96k"]
        made["Độc thoại AV1 Opus mp4 [Youtube-av1mp4AAAAA].mp4"] = [*small, *av1a, "-c:a", "libopus", "-b:a", "96k"]
    for name, args in made.items():
        out = folder / name
        if not out.exists():
            log("making", name, ff(*args, str(out)))


def cases(b, person, slug):
    lib = Path(b.call("settings.get")["library_root"])
    log("library_root:", lib)

    # --- 1. a real download through the app's own downloader -----------------------------------
    if not ONLY or "download" in ONLY:
        for url in ("ytsearch1:hài độc thoại Việt Nam", "https://www.youtube.com/watch?v=jNQXAC9IVRw"):
            try:
                r = b.call("links.resolve", person_id=person, url=url, timeout=120)
            except Exception as e:
                log("links.resolve error", e)
                continue
            item = r if r["kind"] == "video" else (r.get("entries") or [None])[0] if r["kind"] == "playlist" else None
            log("links.resolve", url, "->", r["kind"], r.get("reason") or (item or {}).get("title"))
            if not item:
                continue
            b.call("downloads.add", person_id=person, items=[item])
            d = wait(lambda: next((x for x in sql("select * from download where site_key = ?", item["site_key"])
                                   if x["state"] in ("done", "failed")), None), 420, 3)
            log("download:", d and d["state"], d and d["error"])
            if d and d["state"] == "done":
                break

    # --- 2. sources in an attached folder, codecs YouTube serves, non-ASCII names ---------------
    folder = VIDEOS / "Nguồn video ğİş"
    make_sources(folder)
    log("folders.attach", b.call("folders.attach", person_id=person, paths=[str(folder)]))
    n = len([p for p in folder.iterdir() if p.is_file()])
    wait(lambda: len(sql("select 1 from source where folder_id is not null and status != 'scanning'")) >= n, 300, 2)
    time.sleep(3)
    sources = sql("select s.*, d.title as dtitle from source s left join download d on d.id = s.download_id")
    for s in sources:
        log("source", s["id"], s["status"], s["error"], s["vcodec"], s["acodec"], s["duration"], s["path"])
        try:
            log("  media.allowed(source):", b.call("media.allowed", path=s["path"]))
        except Exception as e:
            log("  media.allowed(source) ERROR", e)
    ready = [s for s in sources if s["status"] == "ready"]

    first = None
    if not ONLY or "codecs" in ONLY:
        for s in ready:
            dur = s["duration"] or 20
            iv = ((1.0, min(6.0, dur - 1)), (min(8.0, dur - 0.5) - 1.5, min(8.0, dur - 0.5)))
            seg = segment(person, s["id"], "Chuyện ông bố đi chợ Tết: “mua gì cũng được”?", iv)
            r = export(b, seg, f"codec {s['vcodec']}/{s['acodec']} {Path(s['path']).suffix}")
            if r["state"] == "done" and first is None:
                first = (seg, r["path"])

    base = next((s for s in ready if s["vcodec"] == "h264"), ready[0] if ready else None)
    if base is None:
        log("no ready source; stopping")
        return

    # --- 3. re-export while the previous export is open ---------------------------------------
    if (not ONLY or "reopen" in ONLY) and first:
        seg, path = first
        # (a) like Electron's media:// stream: Node fs (libuv opens with read|write|delete sharing)
        holder = subprocess.Popen([str(APP), "-e", "const fs=require('fs');const fd=fs.openSync(process.argv[1],'r');"
                                   "console.log('node holds',fd);setTimeout(()=>{},90000)", path],
                                  env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"}, stdout=subprocess.PIPE, text=True)
        log(holder.stdout.readline().strip())
        export(b, seg, "re-export while open by Node fs (media:// stream)")
        holder.kill()
        holder.wait()
        # (b) a reader without delete sharing (Python open(); a player or Explorer preview)
        path = sql("select path from render where kind='segment' and ref_id=? and state='done' order by id desc", seg)
        if path:
            with open(path[0]["path"], "rb"):
                export(b, seg, "re-export while open without FILE_SHARE_DELETE")

    # --- 4. library on a path with spaces and non-ASCII characters ----------------------------
    if not ONLY or "unicode-root" in ONLY:
        root = VIDEOS / "Thư viện Highlight Autoclip ğİş"
        b.call("settings.set", key="library_root", value=str(root))
        seg = segment(person, base["id"], "İstanbul trafiğinde şoförle kavga")
        export(b, seg, "library root with spaces/unicode")
        b.call("settings.set", key="library_root", value=str(lib))

    # --- 5. very long resulting path ------------------------------------------------------------
    if not ONLY or "long" in ONLY:
        long_name = ("Khi tôi kể chuyện bố mẹ tôi đi du lịch nước ngoài lần đầu tiên và lạc mất hộ chiếu ở sân bay "
                     "rồi phải gọi cho đại sứ quán lúc nửa đêm")
        seg = segment(person, base["id"], long_name)
        export(b, seg, f"long clip name ({len(long_name)} chars)")
        root = VIDEOS / ("Highlight Autoclip library for stand-up comedy clips and compilations " * 2).strip()
        b.call("settings.set", key="library_root", value=str(root))
        seg = segment(person, base["id"], "Long name ASCII " + "x" * 130)
        export(b, seg, "path > 260 chars (deep library root, long clip name)")
        b.call("settings.set", key="library_root", value=str(lib))


def main():
    log("python", sys.version)
    log("backend:", BACKEND, BACKEND.exists())
    log("videos:", VIDEOS)
    b = Backend()
    try:
        log("ping", b.call("ping", timeout=120))
        p = b.call("person.create", name="Trấn Thành")
        log("person", p["id"], p["slug"], p["storage"])
        cases(b, p["id"], p["slug"])
    finally:
        time.sleep(1)
        b.stop()
    log("\n===== SUMMARY =====")
    for r in RESULTS:
        log(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
