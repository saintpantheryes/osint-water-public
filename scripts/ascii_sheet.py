"""把首页视频转成「字符动画」用的小帧图:视频缩成 cols×rows 个格子(一格一个字符,格子宽高比 0.6),每秒 fps 帧,
所有帧拼成一张 JPEG(每行 per_row 帧)再加一份 JSON 说明(列、行、帧率、帧数、每行帧数)。页面脚本 portal/ascii-water.js 按格子取颜色与亮度,
亮的地方用密的字符、暗的地方留空,颜色沿用原片。10 MB 的视频变成几百 KB 的一张图,打开就能画,也不用浏览器放视频。
用法:python3 scripts/ascii_sheet.py hero.mp4 out_dir [--cols 128] [--fps 12] [--quality 72]
产出 out_dir/hero_ascii.jpg 与 out_dir/hero_ascii.json。需要 ffmpeg 与 Pillow。"""
import argparse, json, pathlib, subprocess, sys
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument("video"); ap.add_argument("out")
ap.add_argument("--cols", type=int, default=128); ap.add_argument("--fps", type=int, default=12)
ap.add_argument("--quality", type=int, default=72); ap.add_argument("--per-row", type=int, default=16)
ap.add_argument("--cell-aspect", type=float, default=0.6, help="一个字符格子的宽÷高")
a = ap.parse_args()
probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", a.video], capture_output=True, text=True, check=True).stdout.strip()
vw, vh = map(int, probe.split(","))
rows = max(1, round(a.cols * a.cell_aspect * vh / vw))  # 格子比字符格子的形状:宽 0.6 高,所以行数比按像素算的少
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", a.video, "-vf", f"fps={a.fps},scale={a.cols}:{rows}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
n = len(raw) // (a.cols * rows * 3)
sheet_rows = (n + a.per_row - 1) // a.per_row
sheet = Image.new("RGB", (a.cols * a.per_row, rows * sheet_rows))
for i in range(n):
    fr = Image.frombytes("RGB", (a.cols, rows), raw[i * a.cols * rows * 3:(i + 1) * a.cols * rows * 3])
    sheet.paste(fr, ((i % a.per_row) * a.cols, (i // a.per_row) * rows))
out = pathlib.Path(a.out); out.mkdir(parents=True, exist_ok=True)
sheet.save(out / "hero_ascii.jpg", quality=a.quality, optimize=True)
(out / "hero_ascii.json").write_text(json.dumps({"cols": a.cols, "rows": rows, "fps": a.fps, "frames": n, "perRow": a.per_row, "source": pathlib.Path(a.video).name, "sourceSize": [vw, vh]}, ensure_ascii=False), encoding="utf-8")
print(f"{n} 帧 {a.cols}×{rows} 格 @{a.fps} fps → {out / 'hero_ascii.jpg'} {(out / 'hero_ascii.jpg').stat().st_size // 1024} KB", file=sys.stderr)
