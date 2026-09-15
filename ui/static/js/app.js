/* Schemata — charting behaviour for the part detail page. */
/**
 * @typedef {Object} HistoryPoint
 * @property {string} ts     ISO-ish date string for the x-axis
 * @property {number} total_stock  Total available stock across sources
 * @property {number} min_price    Lowest unit price observed
 */

(function (win) {
  "use strict";
  var Schemata = win.Schemata || {};

  /**
   * Render the stock/price history chart on the part detail page.
   * No-op when the canvas or its data element is missing, the series is
   * too short to draw, or the JSON fails to parse.
   */
  Schemata.historyChart = function () {
    var el = document.getElementById("historyChart");
    var dataEl = document.getElementById("history-data");
    if (!el || !dataEl) return;
    var series;
    try { series = JSON.parse(dataEl.textContent); } catch (e) { return; }
    if (!series || series.length < 2) return;

    var reduceMotion = win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var rect = el.getBoundingClientRect();
    var dpr = win.devicePixelRatio || 1;
    var W = (el.width = Math.max(320, rect.width) * dpr);
    var H = (el.height = 220 * dpr);
    el.style.width = "100%";
    var ctx = el.getContext("2d");
    ctx.scale(dpr, dpr);
    var pad = { l: 52, r: 14, t: 18, b: 28 };
    var w = rect.width - pad.l - pad.r;
    var h = 220 - pad.t - pad.b;

    var maxStock = 1, maxPrice = 1;
    series.forEach(function (p) {
      if (p.total_stock > maxStock) maxStock = p.total_stock;
      if (p.min_price > maxPrice) maxPrice = p.min_price;
    });
    maxPrice = maxPrice * 1.15 || 1;

    var xs = series.map(function (_, i) { return pad.l + (i / (series.length - 1)) * w; });
    var yStock = function (v) { return pad.t + h - (v / maxStock) * h; };
    var yPrice = function (v) { return pad.t + h - (v / maxPrice) * h; };

    // gridlines + labels (price axis right, stock axis left)
    ctx.strokeStyle = "#e2e9f0";
    ctx.lineWidth = 1;
    ctx.beginPath();
    var steps = 4;
    for (var g = 0; g <= steps; g++) {
      var gy = pad.t + (g / steps) * h;
      ctx.moveTo(pad.l, gy); ctx.lineTo(pad.l + w, gy);
    }
    ctx.stroke();
    ctx.font = "10px Consolas, monospace";
    ctx.fillStyle = "#7a8a98";
    for (var s = 0; s <= steps; s++) {
      var sy = pad.t + h - (s / steps) * h;
      ctx.textAlign = "right";
      ctx.fillText(Math.round((maxStock * s) / steps), pad.l - 6, sy + 3);
      ctx.textAlign = "left";
      ctx.fillText((maxPrice * s / steps).toFixed(2), pad.l + w + 6, sy + 3);
    }
    ctx.textAlign = "left";
    ctx.fillText("stock", pad.l - 6, pad.t - 4);
    ctx.fillText("price", pad.l + w + 6, pad.t - 4);

    ctx.lineWidth = 2;
    // stock line
    ctx.strokeStyle = "#173b63";
    ctx.beginPath();
    xs.forEach(function (x, i) { var y = yStock(series[i].total_stock); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
    // price line
    ctx.strokeStyle = "#d9a441";
    ctx.beginPath();
    xs.forEach(function (x, i) { var y = yPrice(series[i].min_price); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();

    // x labels: first & last date
    ctx.fillStyle = "#7a8a98";
    ctx.textAlign = "left";
    ctx.font = "10px Consolas, monospace";
    ctx.fillText(String(series[0].ts).slice(0, 10), pad.l, pad.t + h + 16);
    ctx.textAlign = "right";
    ctx.fillText(String(series[series.length - 1].ts).slice(0, 10), pad.l + w, pad.t + h + 16);

    if (!reduceMotion) {
      // subtle reveal
      el.style.transition = "opacity .5s ease";
      el.style.opacity = 0;
      requestAnimationFrame(function () { el.style.opacity = 1; });
    }
  };

  win.Schemata = Schemata;
})(window);