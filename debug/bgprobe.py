# Temporary: which ways of cover-scaling a camera-size image to 1920x1080 BT.709 yuv420p survive the Windows ffmpeg.
import subprocess, sys, tempfile, time
from pathlib import Path
import av, imageio_ffmpeg, numpy as np

FF = imageio_ffmpeg.get_ffmpeg_exe()
print(subprocess.run([FF, "-version"], capture_output=True, text=True).stdout.splitlines()[0], "| av", av.__version__)
D = Path(tempfile.mkdtemp())
W, H = 1920, 1080
COVER = f"scale={W}:{H}:force_original_aspect_ratio=increase"
TAIL = f"crop={W}:{H},format=yuv420p"
CHAINS = {
    "one-step": f"{COVER}:out_color_matrix=bt709:out_range=tv,{TAIL}",
    "two-step": f"{COVER},scale=out_color_matrix=bt709:out_range=tv,{TAIL}",
    "two-step rgb24 between": f"{COVER},format=rgb24,scale=out_color_matrix=bt709:out_range=tv,{TAIL}",
    "two-step gbrp between": f"{COVER},format=gbrp,scale=out_color_matrix=bt709:out_range=tv,{TAIL}",
    "two-step yuv444p between": f"{COVER},format=yuv444p,scale=out_color_matrix=bt709:out_range=tv,{TAIL}",
    "two-step setparams": f"{COVER},setparams=colorspace=unknown,scale=out_color_matrix=bt709:out_range=tv,{TAIL}",
    "no matrix": f"{COVER},{TAIL}",
}


def ff(*a):
    return subprocess.run([FF, "-nostdin", "-hide_banner", *a], capture_output=True, text=True, errors="replace")


def check(raw):
    a = np.fromfile(raw, np.uint8)
    if a.size != W * H * 3 // 2:
        return f"size {a.size}"
    n = W * H
    return f"yuv {int(a[n // 2])},{int(a[n + n // 8])},{int(a[n + n // 4 + n // 8])}"


for size in ("7952x5304", "5304x7952", "8192x5464"):
    for ext, src in (("jpeg", "color=c=0xe67814"), ("png", "color=c=0xe67814")):
        img = D / f"{size}.{ext}"
        r = ff("-y", "-f", "lavfi", "-i", f"{src}:s={size}", "-frames:v", "1", *(["-q:v", "3"] if ext == "jpeg" else []), str(img))
        if r.returncode:
            print("make", size, ext, r.stderr[-300:]); continue
        for name, vf in CHAINS.items():
            raw = D / "out.yuv"
            raw.unlink(missing_ok=True)
            t = time.time()
            r = ff("-v", "verbose", "-y", "-i", str(img), "-vf", vf, "-frames:v", "1", "-f", "rawvideo", str(raw))
            res = check(raw) if r.returncode == 0 else ""
            print(f"{size} {ext:4} {name:26} exit 0x{r.returncode & 0xFFFFFFFF:08X} {time.time() - t:.1f}s {res}")
            if size == "7952x5304" and ext == "jpeg" and name in ("two-step", "one-step"):
                print("   " + "\n   ".join(l for l in r.stderr.splitlines() if "scale" in l or "auto_" in l or "csp" in l)[:3000])
        # PyAV (its own ffmpeg libs): shrink keeping the format, then convert at 1920 wide
        for name in ("pyav two-step", "pyav one-step"):
            code = f"""
import av, numpy as np, sys
with av.open(r"{img}") as c:
    f = next(c.decode(video=0))
s = max({W} / f.width, {H} / f.height)
w, h = max({W}, round(f.width * s)), max({H}, round(f.height * s))
if "{name}" == "pyav two-step":
    f = f.reformat(w, h).reformat(format="yuv420p", dst_colorspace="ITU709", dst_color_range="MPEG")
else:
    f = f.reformat(w, h, "yuv420p", dst_colorspace="ITU709", dst_color_range="MPEG")
y = f.planes[0]
print(f.width, f.height, f.format.name, np.frombuffer(y, np.uint8)[0])
"""
            t = time.time()
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            print(f"{size} {ext:4} {name:26} exit 0x{r.returncode & 0xFFFFFFFF:08X} {time.time() - t:.1f}s {r.stdout.strip()} {r.stderr.strip()[-300:]}")
