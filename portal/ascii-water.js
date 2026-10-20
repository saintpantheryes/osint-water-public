/* 首页与登录页的「ASCII 水面世界」动画:用字符在画布上画一张世界地图,海面是流动的波纹,陆地上不时冒出一圈圈「信号」。
   参考 stackedlol/ascii-sharks 的想法(字符格子 + 画布),代码是自己写的,不依赖任何库、不取任何文件:
   陆地轮廓是 portal/world.geo.json 按 1° 格子算好的位图(scripts/landmask.py 生成),随脚本一起带着,登录前也能画。
   用法:window.asciiWater(canvas) → { stop }。系统设了「减少动态效果」只画一帧;标签页不可见、画布滚出视野时暂停。 */
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

  window.asciiWater = function (canvas, opts) {
    opts = opts || {};
    if (!mask) mask = unpack();
    var ctx = canvas.getContext("2d");
    var still = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var fps = opts.fps || 24, cols = 0, rows = 0, cw = 0, ch = 0, dpr = 1;
    var kind = null, edge = null, pings = [], last = 0, t0 = performance.now(), raf = 0, visible = true, onscreen = true, stopped = false;

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
      if (t - last >= 1000 / fps - 2) { last = t; frame(t); }
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
    return {
      stop: function () { stopped = true; if (raf) cancelAnimationFrame(raf); if (io) io.disconnect(); if (ro) ro.disconnect(); document.removeEventListener("visibilitychange", onVis); },
      frame: function () { build(); frame(performance.now()); }
    };
  };
})();
