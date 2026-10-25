"""Temporary: render a highlight video (compilation) with the installed Highlight Autoclip backend.exe, started the way
the app starts it, from rows written straight into its library (no LLM).

python debug/render_check.py <install dir>
Media: a public-domain video (Wikimedia Commons, a US government work) and synthetic images. No personal data.
"""

import glob
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

INSTALL = Path(sys.argv[1])
APP = INSTALL / "highlight-autoclip-tool.exe"
BACKEND = INSTALL / "resources" / "backend" / "backend.exe"
FFMPEG = glob.glob(str(INSTALL / "resources" / "backend" / "**" / "ffmpeg*.exe"), recursive=True)[0]
TEMP = Path(os.environ["RUNNER_TEMP"])
VIDEOS = Path(os.environ["VIDEOS_DIR"])
WRAP = os.environ.get("FFWRAP")
CASES = [c for c in os.environ.get("CASES", "").split(",") if c] or ["bigimg", "en-raw", "en", "vi", "profile"]
PD = "https://upload.wikimedia.org/wikipedia/commons/f/f4/20161008_Weekly_Address_HD.webm"  # public domain
RESULTS = []

WORDS = {
    "en": "so my mother calls me last night and she says honey I joined the internet and I said mom you can't join "
          "the internet it isn't a club and she said well they gave me a password so it's a club".split(),
    "vi": "xin chào các bạn hôm nay tôi sẽ kể một câu chuyện rất buồn cười về gia đình của tôi mẹ tôi gọi điện "
          "bảo con ơi mẹ vừa vào mạng rồi đấy mẹ còn có cả mật khẩu nữa".split(),
}


def log(*a):
    print(*a, flush=True)


def ff(*args):
    r = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-y", *args], capture_output=True)
    if r.returncode:
        log("ffmpeg failed", hex(r.returncode & 0xFFFFFFFF), r.stderr.decode("utf-8", "replace")[-800:])
    return r.returncode == 0


class Backend:
    def __init__(self, data, videos, extra):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
        env.update(extra)
        env.update(AUTOCLIP_DATA_DIR=str(data), AUTOCLIP_VIDEOS_DIR=str(videos), AUTOCLIP_NODE=str(APP),
                   ELECTRON_RUN_AS_NODE="1")
        self.data = data
        self.p = subprocess.Popen([str(BACKEND)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.pending, self.n, self.lock, self.shown = {}, 0, threading.Lock(), -1
        threading.Thread(target=self._out, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _out(self):
        for raw in self.p.stdout:
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                log("[backend stdout]", raw[:300])
                continue
            if msg.get("event") == "render.progress":
                tenth = int(msg["data"]["progress"] * 10)
                if tenth != self.shown:
                    self.shown = tenth
                    log(f"  progress {msg['data']['progress']:.2f}")
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
        self.p.stdin.write((json.dumps({"id": id, "method": method, "params": params}, ensure_ascii=False) + "\n").encode())
        self.p.stdin.flush()
        msg = q.get(timeout=timeout)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg["result"]

    def sql(self, query, *args):
        with sqlite3.connect(self.data / "library.sqlite", timeout=30) as c:
            c.row_factory = sqlite3.Row
            cur = c.execute(query, args)
            return cur.lastrowid if query.lstrip().lower().startswith("insert") else [dict(r) for r in cur]

    def stop(self):
        self.p.kill()
        self.p.wait()


def wait(fn, timeout, step=1.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def source_file():
    """45 s of a public-domain address (a person on camera, speaking) as H.264/AAC 1080p30 MP4, like a download."""
    out = TEMP / "source.mp4"
    if out.exists():
        return out
    webm = TEMP / "pd.webm"
    try:
        req = urllib.request.Request(PD, headers={"User-Agent": "autoclip-render-check/1.0 (GitHub Actions; debugging)"})
        with urllib.request.urlopen(req, timeout=120) as r, open(webm, "wb") as f:
            shutil.copyfileobj(r, f)
        ok = ff("-ss", "20", "-t", "45", "-i", str(webm), "-vf", "scale=1920:1080,fps=30", "-c:v", "libx264", "-preset",
                "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
                "-movflags", "+faststart", str(out))
    except OSError as e:
        log("download failed:", e)
        ok = False
    if not ok:
        log("using a synthetic source instead")
        ff("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30", "-f", "lavfi", "-i", "anoisesrc=color=pink:r=48000:a=0.2",
           "-t", "45", "-ac", "2", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac",
           "-movflags", "+faststart", str(out))
    return out


SRCS = ["testsrc2=s=6000x4000", "smptehdbars=s=5472x3648", "rgbtestsrc=s=4000x6000", "testsrc=s=4608x3072",
        "gradients=s=6000x4000:seed=1", "gradients=s=5184x3456:seed=2", "cellauto=s=3000x2000:rule=110",
        "colorspectrum=s=6000x4000", "testsrc2=s=1920x1280", "smptebars=s=6000x4000"]


def backgrounds(folder):
    """10 large JPEGs where the app keeps Pexels downloads, as compilation.backgrounds lists them."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, src in enumerate(SRCS):
        p = folder / f"image-{1000 + i}.jpeg"
        if not ff("-f", "lavfi", "-i", src, "-frames:v", "1", "-q:v", "3", str(p)):
            ff("-f", "lavfi", "-i", "testsrc2=s=6000x4000", "-vf", f"hue=h={36 * i}", "-frames:v", "1", "-q:v", "3", str(p))
        out.append({"path": str(p), "kind": "image", "width": None, "height": None, "duration": None,
                    "credit": f"Photographer {i} on Pexels", "url": f"https://www.pexels.com/photo/{1000 + i}/"})
    return out


def sentences(lang, end):
    """Sentence rows of 8 words each, a word every 0.35 s, 0.7 s between sentences."""
    words, out, t, k = WORDS[lang], [], 0.5, 0
    while t < end - 1:
        ws = []
        for _ in range(8):
            ws.append([round(t, 3), round(t + 0.3, 3), words[k % len(words)], -0.1])
            t, k = t + 0.35, k + 1
        ws[-1][2] += "."
        out.append(ws)
        t += 0.7
    return out


def case(name):
    log(f"\n===== case {name} =====")
    raw, lang = name.endswith("-raw"), "vi" if name.startswith("vi") else "en"
    extra = {}
    if name == "profile":  # a Windows user whose name isn't ASCII: AppData, Videos and TEMP under it
        home = TEMP / "Users" / "Trần Văn Đức"
        data, videos, tmp = home / "AppData" / "Roaming" / "highlight-autoclip-tool", home / "Videos", home / "AppData" / "Local" / "Temp"
        tmp.mkdir(parents=True, exist_ok=True)
        videos.mkdir(parents=True, exist_ok=True)
        extra.update(TEMP=str(tmp), TMP=str(tmp))
    else:
        data, videos = TEMP / f"data-{name}", VIDEOS / name
        videos.mkdir(parents=True, exist_ok=True)
    fflog = TEMP / f"fflog-{name}"
    if WRAP and not raw:
        fflog.mkdir(exist_ok=True)
        extra.update(IMAGEIO_FFMPEG_EXE=WRAP, REAL_FFMPEG=FFMPEG, FFLOG=str(fflog))
    b = Backend(data, videos, extra)
    res = {"case": name}
    try:
        log("ping", b.call("ping", timeout=120))
        p = b.call("person.create", name="Trấn Thành" if lang == "vi" else "Test Comedian")
        folder = videos / ("Nguồn video" if lang == "vi" else "Sources")
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy(source_file(), folder / "set.mp4")
        b.call("folders.attach", person_id=p["id"], paths=[str(folder)])
        s = wait(lambda: next(iter(b.sql("select * from source where status != 'scanning'")), None), 300)
        log("source", {k: s[k] for k in ("id", "status", "width", "height", "fps", "duration", "vcodec", "acodec", "path")})
        lib = Path(b.call("settings.get")["library_root"])
        bgs = backgrounds(lib / ".cache" / "pexels")
        sid, dur = s["id"], s["duration"]
        for idx, (a, e) in enumerate([(0.0, 19.0), (19.0, dur)]):
            b.sql("insert into shot(source_id, idx, start, end, kind) values(?, ?, ?, ?, 'performer')", sid, idx, a, e)
        sents = sentences(lang, dur)
        for idx, ws in enumerate(sents):
            b.sql("insert into sentence(source_id, idx, start, end, text, words) values(?, ?, ?, ?, ?, ?)",
                  sid, idx, ws[0][0], ws[-1][1], " ".join(w[2] for w in ws), json.dumps(ws, ensure_ascii=False))
        ivs = [[3.0, 18.0], [20.0, 38.0]]
        seg = b.sql("insert into segment(person_id, source_id, start_sid, end_sid, start, end, intervals, name, summary, rationale,"
                    " status, updated_at) values(?, ?, 0, ?, ?, ?, ?, ?, 'test', ?, 'ok', ?)",
                    p["id"], sid, len(sents) - 1, ivs[0][0], ivs[-1][1], json.dumps(ivs),
                    "Mẹ vào mạng" if lang == "vi" else "Mom joins the internet", json.dumps({"fade_in": 0.3, "fade_out": 0.5}), time.time())
        params = {"output": {"resolution": "1080p", "fps": 30, "bitrate": "auto"}, "mode": "highlights", "topic_id": None,
                  "topic_ids": None, "format": "long", "transition": "dissolve", "target_minutes": 75,
                  "subtitle": {"font": "bangers", "style": "yellow_black", "position": "bottom"},
                  "background": {"source": "pexels", "kind": "images", "style": "topic", "count": 10}}
        now = time.time()
        comp = b.sql("insert into compilation(person_id, mode, aspect, format, params, title, query, backgrounds, state, progress,"
                     " planned, hook_id, repeats, created_at, updated_at) values(?, 'highlights', '16:9', 'long', ?, ?, 'calm city lights',"
                     " ?, 'rendering', 0.4, 1, ?, 0, ?, ?)",
                     p["id"], json.dumps(params), "Gia đình tôi" if lang == "vi" else "Family Matters", json.dumps(bgs), seg, now, now)
        secs = sum(e - a for a, e in ivs)
        b.sql("insert into compilation_item(compilation_id, pos, segment_id, intervals, out_start, out_end) values(?, 0, ?, ?, 0, ?)",
              comp, seg, json.dumps(ivs), secs)
        rid = b.sql("insert into render(kind, ref_id, aspect, state, item_pos, created_at) values('compilation', ?, '16:9', 'queued', null, ?)",
                    comp, now)
        log("queued render", rid, "compilation", comp)
        t0 = time.time()
        r = wait(lambda: next((x for x in b.sql("select * from render where id = ?", rid) if x["state"] in ("done", "failed")), None), 2400, 2)
        r = r or b.sql("select * from render where id = ?", rid)[0]
        checks = json.loads(r["checks"] or "null")
        res.update(state=r["state"], error=(checks or {}).get("error"), secs=round(time.time() - t0), path=r["path"],
                   checks=None if r["state"] != "done" else {k: checks.get(k) for k in ("ok", "problems", "video_duration", "fps", "width", "height", "loudness")})
        try:
            v = b.call("compilation.get", id=comp)
            res["ui"] = {"state": v["state"], "error": v["error"]}
        except Exception as e:
            res["ui"] = str(e)
    except Exception as e:
        res.update(state="driver error", error=repr(e))
    finally:
        b.stop()
    RESULTS.append(res)
    log("RESULT", json.dumps(res, ensure_ascii=False))
    if fflog.exists():
        calls = sorted(fflog.glob("*.json"))
        log(f"{len(calls)} ffmpeg calls logged")
        for f in calls:
            c = json.loads(f.read_text(encoding="utf-8"))
            if c["code"] != 0:
                log("FFMPEG FAILED", c["hex"], f"{c['secs']:.1f}s", "err:", c["err"])
                log("  args:", json.dumps(c["args"], ensure_ascii=False))
                log("  stderr:", c["stderr"][-3000:] or "(empty)")


BG = "scale=1920:1080:force_original_aspect_ratio=increase:out_color_matrix=bt709:out_range=tv,crop=1920:1080,format=yuv420p"


def bigimg():
    """stitch._bg's command, by hand, on large synthetic JPEGs (Pexels originals are often 6000-8000 px wide)."""
    log("\n===== case bigimg =====")
    d = TEMP / "bigimg"
    d.mkdir(exist_ok=True)
    for size in ("6000x4000", "6720x4480", "7008x4672", "7360x4912", "7680x4320", "7952x5304", "8192x5464", "5304x7952"):
        img = d / f"{size}.jpeg"
        ff("-f", "lavfi", "-i", f"smptebars=s={size}", "-frames:v", "1", "-q:v", "3", str(img))
        for label, vf in (("app", BG), ("no matrix", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuv420p"),
                          ("decode only", "null")):
            r = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", str(img), "-vf", vf, "-frames:v", "1",
                                "-f", "rawvideo", str(d / "out.yuv")], capture_output=True)
            log(f"{size} {label}: exit {hex(r.returncode & 0xFFFFFFFF)} stderr={r.stderr.decode('utf-8', 'replace')[-300:]!r}")


def facts():
    log("python", sys.version)
    log("ffmpeg", FFMPEG)
    r = subprocess.run([FFMPEG, "-hide_banner", "-version"], capture_output=True, text=True)
    log(r.stdout[:3000])
    r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True)
    log("\n".join(x for x in r.stdout.splitlines() if "264" in x))


def main():
    facts()
    for c in CASES:
        bigimg() if c == "bigimg" else case(c)
    log("\n===== SUMMARY =====")
    for r in RESULTS:
        log(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
