"""把 portal/world.geo.json 栅格成 1° 的陆地位图(经度 -180…180、纬度 80…-60),按行打包成 base64,贴进 portal/ascii-water.js 的 MASK_B64。
用法:python3 scripts/landmask.py portal/world.geo.json > mask.b64(标准错误里顺带打一张字符预览)。"""
import base64, json, sys
from PIL import Image, ImageDraw
g = json.load(open(sys.argv[1]))
W, H = 360, 140  # 1 degree cells; lat from +80 (row 0) down to -60
im = Image.new("1", (W * 4, H * 4), 0)  # 4x supersample then threshold at >= 25% coverage
d = ImageDraw.Draw(im)
def px(lon, lat):
    return ((lon + 180) * 4, (80 - lat) * 4)
for f in g["features"]:
    geom = f["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        outer = [px(x, y) for x, y in poly[0]]
        if len(outer) >= 3:
            d.polygon(outer, fill=1)
        for hole in poly[1:]:
            pts = [px(x, y) for x, y in hole]
            if len(pts) >= 3:
                d.polygon(pts, fill=0)
small = im.resize((W, H), Image.BOX)  # mode 1 BOX resize -> treat as >=50%
bits = bytearray((W * H + 7) // 8)
land = 0
for y in range(H):
    for x in range(W):
        if small.getpixel((x, y)):
            i = y * W + x
            bits[i >> 3] |= 0x80 >> (i & 7)
            land += 1
print("land cells", land, "of", W * H, file=sys.stderr)
b64 = base64.b64encode(bytes(bits)).decode()
print(b64)
# ascii preview
for y in range(0, H, 4):
    print("".join("#" if small.getpixel((x, y)) else "." for x in range(0, W, 2)), file=sys.stderr)
