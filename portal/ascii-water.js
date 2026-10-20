/* 首页与登录页的字符动画背景(参考 stackedlol/ascii-sharks 的想法:字符格子 + 画布;代码自己写,不依赖任何库)。
   两种画法:
   1. 有「帧图」时(私有配置仓 portal/media/hero_ascii.jpg + .json,由 scripts/ascii_sheet.py 从原来的首页视频转成:每帧缩成 112×49 个格子,所有帧拼成一张图,
      约 0.6 MB,原视频 10 MB):按格子取原片的颜色与亮度,亮的地方字符密、暗的地方留空,颜色沿用原片,10 帧/秒循环——就是原视频的字符版,铺满整块背景(居中裁边)。
   2. 没有帧图时(公开仓自己跑、测试里):画一张字符世界地图,海面波纹流动,陆地上冒一圈圈「信号」;陆地轮廓是 world.geo.json 按 1° 算好的位图(scripts/landmask.py),随脚本带着。
   用法:window.asciiWater(canvas, { src: "media/hero_ascii", fit: "contain", look: "ascii" }) → { stop }。look 可选 ascii / dense / blocks / dots;fit 为 cover(铺满裁边)或 contain(整幅)。系统设了「减少动态效果」只画一帧;标签页不可见、画布滚出视野时暂停。 */
(function () {
  "use strict";
  var MASK_W = 360, MASK_H = 140, LAT_TOP = 80;  // 位图:经度 -180…180、纬度 80…-60,每格 1°
  var MASK_B64 = "AAAAAAAAAAAAOA///4A///////8AAAAB/nwAAAAAAAAAAD8gAAAAAAAAAAAAAAAAAAAAAAAeH3n//5////////4AAAAATsAAAAAAAAAAAAD4AAAAAAAAAAAAAAAAAAAAAAeMMA5P/AH///////+AAAAAPGAAAAAAAAAAAAA8AAAAAAAAAAAAAAAAAAAAAH7HG8//+AP///////wAAAAAAAAAAAAARwAAAAH/+AAAAAAAAAAAAAAAAAAAAAP/0c//+AAAf/////+AAAAAAAAAAAAHwAAAB////AAAB/sAAAAAAAAAAAAAAfAwAAAQAAAAH/////+AAAAAAAAAAAAeAAAAP///2AAAAAAAAAAAAAAAAAAAAf8AY8+d/gAAD/////YAAAAAAAAAAAA4ACAP///+/8HwAOAAAAAAAAAAAAAAAf/+4/Y/74AAD/////QAAAAAAAAAAADgAPln///////4Af8AAAAAwAAAAAAAAOf/8E4///gADf////wAAAAAAAAAAADwAfZ////////4///gAAABAAA//wAAAAAP/B8f//8AB////+gAAAAAAB/gAAACAfv/////////////8AAAAAB///+P//H//HeDxv+AA3///+AAAAAAB//8AAwA+Pv///////////////H+wAf////////B4HPzwT+AAf///gAAAAAAH///4MHf//+/////////////////+AD/////////////4Z/wA///4AAAAAAAP///+I////P//////////////////4M////////////7gD/+A///gAQQAAAAf//98v///+//////////////////bw/////////////rAn+YAf/wAAP8AAAA/8f8H///////////////////////AwAf///////////3wz/gAH/gAAP4AAAD/4//f//////////////////////4AOA///////////+CBA7wAH/gAACAAAAP/j/////////////////////////+AAP///////////4Ai4HAAH/AAAAAAAA/+H///////////////////////v/4AAP///////////wAA/AAAB+AAAAAAAB/+H//////////////////////jP+AAAX//z////////gAA/0QAAGAAAAAAAB//T7////////////////////+A+YAAAA/zAB///////gAA/4wAAAAAAAAAAB//A////////////////////+eDgAAAAAD0AAX//////4AA/94AAAAAAAABYA5+D///////////////////4AAHAAAAAAHcAAP//////8AAf/8AAAAAAAAB8AE+m///////////////////wAAfgAAAAAcAAAC///////wAf/8AAAAAAAAB4AOcH///////////////////AAA/gAAAABwAAAB///////+A///AAAAAAAACcAPQH//////////////////8AAB/AAAAAEAAAAAf///////z///4AAAAAAAP/AOz///////////////////+iAB8AAAABgAAAABf///////x///8AAAAAAAPfB//////////////////////6AA8AAAAAAAAAAAj///////x///8AAAAAAAfPz//////////////////////+AA4AAAAAAAAAAAD///////////2AAAAAAAAPn//////////////////////6AAgAAAAAAAAAAAD///////////MAAAAAAAAQf//////////////////////7ABAAAAAAAAAAAAB3////////+MPAAAAAAAAD///////////////////////zAAAAAAAAAAAAAAAP/////////wfgAAAAAAAf///////////////////////yAAAAAAAAAAAAAAAf/////////gDgAAAAAAAH///////////////////////iAAAAAAAAAAAAAAAP/////////tAAAAAAAAAD/////uP/h//////////////BAAAAAAAAAAAAAAAP/////////+AAAAAAAAAB//f//P/+H/////////////+ECAAAAAAAAAAAAAAP////////8wAAAAAAAAAB//H/+EP+P/////////////8GAAAAAAAAAAAAAAAf////////gAAAAAAAAAH/7jx/8AD+H/////////////4HgAAAAAAAAAAAAAAf////////gAAAAAAAAAH/4Nw/8AB/D////////////+ACAAAAAAAAAAAAAAAP////////gAAAAAAAAAH/wA8f+fh/g////////////8AAAAAAAAAAAAAAAAAf///////8AAAAAAAAAAH/AMHfP///z///////////v4AMAAAAAAAAAAAAAAAP///////4AAAAAAAAAAP/IMCOP///h//////////+RgAMAAAAAAAAAAAAAAAH///////4AAAAAAAAAAH/AAGPP///g//////////8BwAYAAAAAAAAAAAAAAAH///////wAAAAAAAAAAH8AEYGH///w//////////+44A4AAAAAAAAAAAAAAAD///////wAAAAAAAAAAAg/+AABM//////////////g4D4AAAAAAAAAAAAAAAB///////wAAAAAAAAAAAj/+ABwE/////////////+A4/wAAAAAAAAAAAAAAAB///////AAAAAAAAAAAB//8AAAA//////////////AhuAAAAAAAAAAAAAAAAAP/////+AAAAAAAAAAAD//+AAAB//////////////gngAAAAAAAAAAAAAAAAAH/////8AAAAAAAAAAAP///4OAB//////////////gDAAAAAAAAAAAAAAAAAAG/////4AAAAAAAAAAAP///8P4z//////////////wCAAAAAAAAAAAAAAAAAACf////4AAAAAAAAAAAP/////////////////////gCAAAAAAAAAAAAAAAAAAB///hgYAAAAAAAAAAAP/////////P///////////wAAAAAAAAAAAAAAAAAAAAv//AAYAAAAAAAAAAQ///////9//H///////////wAAAAAAAAAAAAAAAAAAAB3/+AAcAAAAAAAAAAB///////9//j///////////gAAAAAAAAAAAAAAAAAAAAT/+AAMAAAAAAAAAAH///////8//wH//////////AAAAAAAAAAAAAAAAAAAAAJ/+AAEAAAAAAAAAAH///////+f/wR/////////+QAAAAAAAAAAAAAAAAAAAAI/8AAAAAAAAAAAAAH///////+f/84Af///////84AAAAAAAAAAAAAAAAAAAACf8AAAAAAAAAAAAAP///////+P//+AP///////4oAAAAAAAAAAAAAAAAAAAAAP8ABuAAAAAAAAAAf////////H///AH///v///AgAAAAAAAAAAAAAAAAAAAAAH+A4BwAAAAAAAAAf////////n//+AD//4P//YAAAAAAAAAAAAAAAAAAAAAAAP+B4AcAAAAAAAAAP////////n//+AAf/4P/+IAAAAAAAAAAAAAAAgAAAAAAAH/B4IBwAAAAAAAAf////////h//8AAf/gH/84AAAAAAAACAAAAAAAAAAAAAAD/vwBn/AAAAAAAAP////////x//4AAf/AD/8wAwAAAAAAAAAAAAAAAAAAAAAAf/wAAAAAAAAAAAP////////4//gAAf+AD/+AAwAAAAAAAAAAAAAAAAAAAAAAH/gAAACAAAAAAAf////////4f+AAAf8ADf/AAwAAAAAAAAAAAAAAAAAAAAAAAH/gAACAAAAAAAf////////8f8AAAPwAAP/gAgAAAAAAAAAAAAAAAAAAAAAAAD/gAAAAAAAAAAf////////+fgAAAPwAAP/gAYAAAAAAAAAAAAAAAAAAAAAAAAfAAAAAAAAAAAf/////////eAAAAHwAAN/gAsAAAgAAAAAAAAAAAAAAAAAAAAHAAgAAAAAAAAf/////////gBAAAHwAIM/gAGAAAAAAAAAAAAAAAAAAAAAAAADADIAAAAAAAAP/////////g4AAAHwAAMfgBLAAAAAAAAAAAAAAAAAAAAAAAADgP/aAAAAAAAH//////////4AAADwAAIeABHAAAAAAAAAAAAAAAAAAAAAAAABzPf+AAAAAAAD//////////4AAADoAAMEACJAAAAAAAAAAAAAAAAAAAAAAAAA9///AAAAAAAB//////////wAAABIAAMAAAHAAAAAAAAAAAAAAAAAAAAAAAAAE///wAAAAAAB//////////wAAAAMAAEAAADgIAAAAAAAAAAAAAAAAAAAAAAAA///wAAAAAAAf/////////gAAAAMAADAAMDAAAAAAAAAAAAAAAAAAAAAAAAAA////gAAAAAAP/B///////gAAAAAABDgAeAAAAAAAAAAAAAAAAAAAAAAAAAAA////wAAAAAACAAv//////AAAAAAAAzgA+AgAAAAAAAAAAAAAAAAAAAAAAAAA////4AAAAAAAAAL/////+AAAAAAAAZgB4AAAAAAAAAAAAAAAAAAAAAAAAAAB////4AAAAAAAAAD/////8AAAAAAAAcwH8AIAAAAAAAAAAAAAAAAAAAAAAAAB////8AAAAAAAAAH/////wAAAAAAAAHwf8CYAAAAAAAAAAAAAAAAAAAAAAAAD////4AAAAAAAAAn/////gAAAAAAAAHwf8MYAAAAAAAAAAAAAAAAAAAAAABAH/////gAAAAAAAAH/////AAAAAAAAADof9AQkAAAAAAAAAAAAAAAAAAAAAAAH/////4AAAAAAAAH/////AAAAAAAAAD0P5yQ1AAAAAAAAAAAAAAAAAAAAAAAH/////+AAAAAAAAD////+AAAAAAAAAB/P5wA74AAAAAAAAAAAAAAAAAAAAAAH//////8AAAAAAAB////8AAAAAAAAAA8AxQif/AAAAAAAAAAAAAAAAAAAAAAP//////+AAAAAAAA////4AAQAAAAAAAcABYAD/wYAAAAAAAAAAAAAAAAAAAAH///////gAAAAAAA////4AAAAAAAAAAMABIAI/zwAAAAAAAAAAAAAAAAAAAAH///////gAAAAAAA////4AAAAAAAAAAHoAAAA/8BAAAAAAAAAAAAAAAAAAAAD///////gAAAAAAAf///4AAAAAAAAAAB/AAhA/4AoAAAAAAAAAAAAAAAAAAAB///////gAAAAAAAf///4AAAAAAAAAAABe5gAOMAQAAAAAAAAAAAAAAAAAAAB///////AAAAAAAAf///8AAAAAAAAAAAABGAAAHgGAAAAAAAAAAAAAAAAAAAA///////AAAAAAAAP///+AAAAAAAAAAAAAAAAAAgBAAAAAAAAAAAAAAAAAAAAf/////+AAAAAAAAP///8AAAAAAAAAAAAAACgCAECAAAAAAAAAAAAAAAAAAAAf/////8AAAAAAAAf///8AQAAAAAAAAAAAAD+CAAAAAAAQAAAAAAAAAAAAAAAP/////4AAAAAAAA////+AwAAAAAAAAAAAAD+HAAAAAAAAAAAAAAAAAAAAAAAP/////4AAAAAAAA////+AwAAAAAAAAAAAB34HgAAAAAAAAAAAAAAAAAAAAAAH/////4AAAAAAAA////+DwAAAAAAAAAAAD/8HgAAAAAAAAAAAAAAAAAAAAAAB/////4AAAAAAAA////8PwAAAAAAAAAAAH//HwAAAABAAAAAgAAAAAAAAAAAA/////4AAAAAAAB////gPwAAAAAAAAAAAP///wAAAgGAAAAAAAAAAAAAAAAAAf////wAAAAAAAA////APgAAAAAAAAAAAP///wAAAAAAAAAAAAAAAAAAAAAAAP////wAAAAAAAAf//+APgAAAAAAAAAAAf///8AAAAAAAAAAAAAAAAAAAAAAAP////wAAAAAAAAf//+APgEAAAAAAAAAP////+AAIAAAAAAAAAAAAAAAAAAAAP////gAAAAAAAAP//+AfAAAAAAAAAAAf////+AAEAAAAAAAAAAAAAAAAAAAAf////AAAAAAAAAP//+AfAAAAAAAAAAB//////gAAAAAAAAAAAAAAAAAAAAAAf///4AAAAAAAAAP//+AfAAAAAAAAAAB//////gAAAAAAAAAAAAAAAAAAAAAAf///gAAAAAAAAAP//+AOAAAAAAAAAAB//////wAAAAAAAAAAAAAAAAAAAAAAf///AAAAAAAAAAH//4AAAAAAAAAAAAB//////4AAAAAAAAAAAAAAAAAAAAAAf//+AAAAAAAAAAH//4AAAAAAAAAAAAB//////4AAAAAAAAAAAAAAAAAAAAAAf//+AAAAAAAAAAH//4AAAAAAAAAAAAA//////4AAAAAAAAAAAAAAAAAAAAAA///+AAAAAAAAAAD/9wAAAAAAAAAAAAA//////8AAAAAAAAAAAAAAAAAAAAAA///8AAAAAAAAAAB/5gAAAAAAAAAAAAAf/////4AAAAAAAAAAAAAAAAAAAAAA///8AAAAAAAAAAB/7gAAAAAAAAAAAAAf/////4AAAAAAAAAAAAAAAAAAAAAA///4AAAAAAAAAAA/+AAAAAAAAAAAAAAf/////4AAAAAAAAAAAAAAAAAAAAAA///wAAAAAAAAAAA/+AAAAAAAAAAAAAAf/AP//wAAAAAAAAAAAAAAAAAAAAAA///gAAAAAAAAAAA/4AAAAAAAAAAAAAAf8AG//gAAAAAAAAAAAAAAAAAAAAAA//3AAAAAAAAAAAAQAAAAAAAAAAAAAAAOAAF//gAAAAAAAAAAAAAAAAAAAAAB//4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB//AAABgAAAAAAAAAAAAAAAAAB//4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/AAAAwAAAAAAAAAAAAAAAAAD//4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/AAAAWAAAAAAAAAAAAAAAAAD//gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABYAAAA8AAAAAAAAAAAAAAAAAB/8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAAAAAAAAAAAAAAAAD/8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAACQAAAAAAAAAAAAAAAAAD/gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcAAADAAAAAAAAAAAAAAAAAAB/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcAAAGAAAAAAAAAAAAAAAAAAB/gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAeAAAAAAAAAAAAAAAAAAH/gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB4AAAAAAAAAAAAAAAAAAH+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD4AAAAAAAAAAAAAAAAAAP+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAAAAAAAAAAAAAAAAH/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAF8AAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD4BwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/gAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
  var mask = null;
  function unpack() {
    var bin = atob(MASK_B64), bits = new Uint8Array(MASK_W * MASK_H);
    for (var i = 0; i < bits.length; i++) bits[i] = (bin.charCodeAt(i >> 3) >> (7 - (i & 7))) & 1;
    return bits;
  }
  function landAt(lon, lat) {
    var x = Math.floor(lon + 180), y = Math.floor(LAT_TOP - lat);
    if (x < 0 || x >= MASK_W || y < 0 || y >= MASK_H) return 0;
    return mask[y * MASK_W + x];
  }
  var OCEAN = [" ", " ", ".", ".", ":", "-", "~", "~", "="];  // 海面:按波高挑字符,低处留白
  var LAND_IN = ["%", "&", "8", "#"], LAND_EDGE = ["#", "@", "#", "W"];  // 陆地内部 / 海岸线
  function hash(x, y) { var h = (x * 374761393 + y * 668265263) | 0; h = (h ^ (h >> 13)) * 1274126177; return ((h ^ (h >> 16)) >>> 0) / 4294967296; }
  function miller(lat) { var r = lat * Math.PI / 180; return 1.25 * Math.log(Math.tan(Math.PI / 4 + 0.4 * r)); }
  function unmiller(y) { return (Math.atan(Math.exp(y / 1.25)) - Math.PI / 4) / 0.4 * 180 / Math.PI; }

  // 几种「画法」(参考 ascii-magic.com 的 Look):按亮度从暗到亮挑字符
  var LOOKS = {
    ascii: [" ", " ", ".", ":", "-", "=", "+", "*", "#", "%", "@"],          // 最暗的两档留空
    dense: [".", ":", "-", "=", "+", "*", "#", "%", "@", "@"],              // 密:每格都有字
    blocks: [" ", "\u2591", "\u2591", "\u2592", "\u2592", "\u2593", "\u2593", "\u2588", "\u2588"],  // 方块 ░▒▓█
    dots: [" ", "\u00b7", "\u00b7", "\u2022", "\u2022", "\u25cf", "\u25cf", "\u25cf"]   // 点阵 ·•●
  };

  /* 取帧图:说明 JSON + 整张图读进内存(像素数组);任一步失败就回退到世界地图 */
  function loadSheet(src) {
    if (!src || !window.fetch) return Promise.resolve(null);
    return fetch(src + ".json", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (meta) {
      if (!meta || !meta.cols || !meta.rows || !meta.frames) return null;
      return new Promise(function (resolve) {
        var img = new Image();
        img.onload = function () {
          var off = document.createElement("canvas"); off.width = img.naturalWidth; off.height = img.naturalHeight;
          var c = off.getContext("2d"); c.drawImage(img, 0, 0);
          try { meta.px = c.getImageData(0, 0, off.width, off.height).data; meta.sheetW = off.width; resolve(meta); } catch (e) { resolve(null); }
        };
        img.onerror = function () { resolve(null); };
        img.src = src + ".jpg";
      });
    }).catch(function () { return null; });
  }

  window.asciiWater = function (canvas, opts) {
    opts = opts || {};
    if (!mask) mask = unpack();
    var ctx = canvas.getContext("2d");
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var fps = opts.fps || 24, cols = 0, rows = 0, cw = 0, ch = 0, dpr = 1;
    var kind = null, edge = null, pings = [], last = 0, t0 = performance.now(), raf = 0, visible = true, onscreen = true, stopped = false;
    var sheet = null, sc = null;  // 帧图及其在画布上的摆法(格子大小、裁掉多少)
    var RAMP = LOOKS[opts.look] || LOOKS.ascii, cover = opts.fit !== "contain", sat = opts.saturate || 1.35, lift = opts.lift || 0.12;  // 画法、铺法(cover 裁边铺满 / contain 整幅放进去)、颜色加浓、整体提亮

    function build() {
      var W = canvas.clientWidth, H = canvas.clientHeight;
      if (!W || !H) return;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      cols = Math.max(48, Math.min(128, Math.round(W / (opts.cell || 9.5))));  // 电脑约 110 多列,手机约 50 列
      cw = W / cols; ch = cw / 0.6; rows = Math.max(1, Math.round(H / ch)); ch = H / rows;
      kind = new Uint8Array(cols * rows); edge = new Uint8Array(cols * rows);
      // 地图整幅放进画布:经度铺满宽度,纬度按 Miller 投影落在中间,上下留海
      var yTop = miller(LAT_TOP), yBot = miller(LAT_TOP - MASK_H), span = (2 * Math.PI) / cols / 0.6;  // 一行对应的投影高度
      var mapRows = (yTop - yBot) / span, off = (rows - mapRows) / 2;
      for (var r = 0; r < rows; r++) {
        var lat = unmiller(yTop - (r - off + 0.5) * span);
        for (var c = 0; c < cols; c++) kind[r * cols + c] = landAt((c + 0.5) / cols * 360 - 180, lat);
      }
      for (r = 0; r < rows; r++) for (var c2 = 0; c2 < cols; c2++) {
        var i = r * cols + c2;
        if (!kind[i]) continue;
        var e = 0;
        if (r > 0 && !kind[i - cols]) e = 1; else if (r < rows - 1 && !kind[i + cols]) e = 1;
        else if (c2 > 0 && !kind[i - 1]) e = 1; else if (c2 < cols - 1 && !kind[i + 1]) e = 1;
        edge[i] = e;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.font = "600 " + (ch * 0.92).toFixed(1) + "px ui-monospace, Menlo, Consolas, \"Courier New\", monospace";
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      if (sheet) {
        /* 帧图摆进画布:cover = 格子放大到宽、高都盖住,多出来的居中裁掉;contain = 整幅放进去,四边留底色 */
        var cw2 = (cover ? Math.max : Math.min)(W / sheet.cols, H * 0.6 / sheet.rows), ch2 = cw2 / 0.6;
        sc = { cw: cw2, ch: ch2, c0: Math.max(0, Math.floor((sheet.cols - W / cw2) / 2)), r0: Math.max(0, Math.floor((sheet.rows - H / ch2) / 2)),
               nc: Math.min(sheet.cols, Math.ceil(W / cw2) + 1), nr: Math.min(sheet.rows, Math.ceil(H / ch2) + 1),
               ox: (W - sheet.cols * cw2) / 2, oy: (H - sheet.rows * ch2) / 2 };
        sc.font = "600 " + (ch2 * 0.95).toFixed(1) + "px ui-monospace, Menlo, Consolas, \"Courier New\", monospace";
      }
    }

    /* 帧图模式:取当前帧,每个格子按亮度挑字符、颜色沿用原片(稍微提亮,深蓝底上看得清) */
    function frameSheet(t) {
      var W = canvas.clientWidth, H = canvas.clientHeight, s = (t - t0) / 1000;
      var f = still ? 0 : Math.floor(s * sheet.fps) % sheet.frames;
      var fx = (f % sheet.perRow) * sheet.cols, fy = Math.floor(f / sheet.perRow) * sheet.rows, px = sheet.px, sw = sheet.sheetW;
      ctx.fillStyle = opts.bg || "#0b1b3a"; ctx.fillRect(0, 0, W, H);
      ctx.font = sc.font;
      var buckets = {};
      for (var r = sc.r0; r < sc.r0 + sc.nr && r < sheet.rows; r++) {
        var y = sc.oy + (r + 0.5) * sc.ch, row = (fy + r) * sw;
        for (var c = sc.c0; c < sc.c0 + sc.nc && c < sheet.cols; c++) {
          var i = (row + fx + c) * 4, R = px[i], G = px[i + 1], B = px[i + 2];
          var lum = (0.299 * R + 0.587 * G + 0.114 * B) / 255;
          var chr = RAMP[Math.min(RAMP.length - 1, Math.floor(Math.min(0.999, lum + lift * 0.5) * RAMP.length))];
          if (chr === " ") continue;
          var g = lum * 255, R2 = g + (R - g) * sat + lift * 255, G2 = g + (G - g) * sat + lift * 255, B2 = g + (B - g) * sat + lift * 255 + 20;  // 颜色加浓、提亮,深蓝底上看得清
          var k = (Math.max(0, R2) >> 4) + "," + (Math.max(0, G2) >> 4) + "," + (Math.max(0, B2) >> 4);  // 颜色量化成 16 级,同色一批写,少换画笔
          (buckets[k] || (buckets[k] = [])).push(sc.ox + (c + 0.5) * sc.cw, y, chr);
        }
      }
      for (var key in buckets) {
        var q = key.split(","); ctx.fillStyle = "rgb(" + Math.min(255, q[0] * 16 + 8) + "," + Math.min(255, q[1] * 16 + 8) + "," + Math.min(255, q[2] * 16 + 8) + ")";
        var b = buckets[key];
        for (var j = 0; j < b.length; j += 3) ctx.fillText(b[j + 2], b[j], b[j + 1]);
      }
    }

    function addPing(t) {
      for (var k = 0; k < 40; k++) {
        var c = Math.floor(Math.random() * cols), r = Math.floor(Math.random() * rows);
        if (kind[r * cols + c]) { pings.push({ c: c, r: r, t: t }); return; }
      }
    }

    function frame(t) {
      var W = canvas.clientWidth, H = canvas.clientHeight;
      if (!cols || Math.round(W * dpr) !== canvas.width || Math.round(H * dpr) !== canvas.height) build();
      if (!cols) return;
      if (sheet) return frameSheet(t);
      var s = (t - t0) / 1000;
      ctx.fillStyle = opts.bg || "#0b1b3a";
      ctx.fillRect(0, 0, W, H);
      if (!still) {
        if (!pings.length || t - pings[pings.length - 1].t > 1400 + Math.random() * 1600) { if (pings.length < 3) addPing(t); }
        pings = pings.filter(function (p) { return t - p.t < 1800; });
      }
      var buckets = {};
      function put(style, x, y, chr) { (buckets[style] || (buckets[style] = [])).push(x, y, chr); }
      var scan = ((s * 0.09) % 1.2) * cols;  // 慢慢扫过去的一道亮线
      for (var r = 0; r < rows; r++) {
        var y = (r + 0.5) * ch;
        for (var c = 0; c < cols; c++) {
          var i = r * cols + c, x = (c + 0.5) * cw, chr, a, rgb, g;
          var glow = 0;
          for (var p = 0; p < pings.length; p++) {
            var pg = pings[p], age = (t - pg.t) / 1000, rad = age * 3.2, d = Math.sqrt((c - pg.c) * (c - pg.c) * 0.36 + (r - pg.r) * (r - pg.r));
            var ring = Math.max(0, 1 - Math.abs(d - rad) / 0.8) * Math.max(0, 1 - age / 1.8);  // 一圈圈往外扩、越扩越淡的「信号」
            if (d < 0.8) ring = Math.max(ring, Math.max(0, 1 - age / 1.0));
            if (ring > glow) glow = ring;
          }
          var sc = Math.max(0, 1 - Math.abs(c - scan) / 2.5) * 0.35;
          if (kind[i]) {
            g = hash(c, r);
            var breathe = 0.5 + 0.5 * Math.sin(s * 0.6 + c * 0.05 + r * 0.09);
            chr = (edge[i] ? LAND_EDGE : LAND_IN)[Math.floor(g * 4)];
            a = (edge[i] ? 0.62 : 0.42) + 0.12 * breathe + sc;
            rgb = "150,175,215";
            if (glow > 0.25) { chr = glow > 0.6 ? "+" : "o"; a = Math.min(1, a + glow); rgb = "140,230,255"; }
          } else {
            var w = 0.5 + 0.5 * Math.sin(c * 0.32 + s * 1.1 + 2.2 * Math.sin(r * 0.21 - s * 0.45)) ;
            w = 0.55 * w + 0.45 * (0.5 + 0.5 * Math.sin(r * 0.6 - s * 0.8 + c * 0.11));
            var lvl = Math.floor(w * OCEAN.length);
            chr = OCEAN[Math.min(OCEAN.length - 1, lvl)];
            if (chr === " ") continue;
            a = 0.18 + 0.5 * w + sc;
            rgb = "90,170,255";
            if (glow > 0.25) { chr = "o"; a = Math.min(1, a + glow * 0.8); rgb = "140,230,255"; }
          }
          put("rgba(" + rgb + "," + (Math.round(Math.min(1, a) * 10) / 10) + ")", x, y, chr);
        }
      }
      for (var st in buckets) {
        ctx.fillStyle = st;
        var b = buckets[st];
        for (var k = 0; k < b.length; k += 3) ctx.fillText(b[k + 2], b[k], b[k + 1]);
      }
    }

    function loop(t) {
      if (stopped) return;
      raf = 0;
      if (!visible || !onscreen) return;
      if (t - last >= 1000 / (sheet ? sheet.fps : fps) - 2) { last = t; frame(t); }
      if (!still) raf = requestAnimationFrame(loop);
    }
    function wake() { if (!raf && !stopped && visible && onscreen) raf = requestAnimationFrame(loop); }

    var io = null;
    if (window.IntersectionObserver) { io = new IntersectionObserver(function (es) { onscreen = es[0].isIntersecting; wake(); }); io.observe(canvas); }
    var ro = null;
    if (window.ResizeObserver) { ro = new ResizeObserver(function () { if (still) { build(); frame(performance.now()); } else wake(); }); ro.observe(canvas); }
    function onVis() { visible = document.visibilityState !== "hidden"; wake(); }
    document.addEventListener("visibilitychange", onVis);
    build();
    wake();
    loadSheet(opts.src).then(function (m) { if (m && !stopped) { sheet = m; build(); if (still) frame(performance.now()); else wake(); } });  // 帧图到了就换成原片的字符版
    return {
      stop: function () { stopped = true; if (raf) cancelAnimationFrame(raf); if (io) io.disconnect(); if (ro) ro.disconnect(); document.removeEventListener("visibilitychange", onVis); },
      frame: function () { build(); frame(performance.now()); }
    };
  };
})();
