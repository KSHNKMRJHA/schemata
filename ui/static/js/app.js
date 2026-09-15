/* Part Intelligence — a little charting and BOM-drop behaviour. */
/**
 * @typedef {Object} HistoryPoint
 * @property {string} ts     ISO-ish date string for the x-axis
 * @property {number} total_stock  Total available stock across sources
 * @property {number} min_price    Lowest unit price observed
 */

/**
 * @typedef {Object} SettingsBody
 * @property {string} mouser_api_key
 * @property {string} digikey_client_id
 * @property {string} digikey_client_secret
 * @property {boolean} digikey_sandbox
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

  /**
   * Highlight the BOM drop zone on drag events and disable the submit
   * button while an upload is in flight.
   */
  Schemata.bomDrop = function () {
    var input = document.getElementById("bomFile");
    var form = document.getElementById("bomForm");
    if (!input || !form) return;
    var dz = document.querySelector(".dz-inner");
    if (dz) {
      var active = function (on) {
        return function (ev) {
          ev.preventDefault();
          dz.style.borderColor = on ? "#b17f22" : "";
          dz.style.background = on ? "#fff7e0" : "";
        };
      };
      form.addEventListener("dragenter", active(true));
      form.addEventListener("dragleave", active(false));
      form.addEventListener("dragover", function (ev) { ev.preventDefault(); });
      form.addEventListener("drop", active(false));
    }
    input.addEventListener("change", function () {
      if (input.files && input.files.length) {
        var label = dashes(input.files[0].name);
        if (dz) dz.querySelector("strong").textContent = "Ready: " + label;
      }
    });
    form.addEventListener("submit", function (e) {
      if (!input.files || !input.files.length) { e.preventDefault(); return; }
      var btn = document.getElementById("bomSubmit");
      if (btn) { btn.textContent = "Analyzing…"; btn.disabled = true; }
    });
  };

  /**
   * Wire up the source-settings form: persist credentials via the API and
   * probe the configured live adapters. No-op when the form is missing.
   */
  Schemata.settings = function () {
    var form = document.getElementById("settingsForm");
    var statusEl = document.getElementById("settingsStatus");
    var probeEl = document.getElementById("probeResults");
    var testBtn = document.getElementById("testSettings");
    if (!form) return;

    /** @returns {SettingsBody} The current form field values. */
    function readForm() {
      return {
        mouser_api_key: form.mouser_api_key ? form.mouser_api_key.value.trim() : "",
        digikey_client_id: form.digikey_client_id ? form.digikey_client_id.value.trim() : "",
        digikey_client_secret: form.digikey_client_secret ? form.digikey_client_secret.value.trim() : "",
        digikey_sandbox: form.digikey_sandbox ? form.digikey_sandbox.checked : false
      };
    }

    /**
     * @param {string} text Status message to show.
     * @param {string} kind  "ok", "err", or "" for neutral styling.
     */
    function setStatus(text, kind) {
      statusEl.textContent = text;
      statusEl.style.color = kind === "ok" ? "#1d7a44" : kind === "err" ? "#b33838" : "";
    }

    /**
     * POST the credential payload and react to the response.
     * @param {SettingsBody} body Credentials read from the form.
     */
    async function saveSettings(body) {
      try {
        const r = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const d = await r.json();
        if (r.status !== 200) throw new Error((d && d.detail) || "save failed");
        setStatus("Saved. Live: " + (d.live.join(", ") || "none") + ".", "ok");
        if (d.live.length) { window.location.reload(); }
        else if (probeEl) { probeEl.textContent = ""; }
      } catch (err) {
        setStatus("Save failed: " + err.message, "err");
      }
    }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var body = readForm();
      if (!body.mouser_api_key && !body.digikey_client_id && !body.digikey_client_secret && !body.digikey_sandbox) {
        setStatus("Nothing to save — enter at least one credential or toggle the sandbox option.", "err");
        return;
      }
      setStatus("Saving…", "");
      saveSettings(body);
    });

    if (testBtn) {
      testBtn.addEventListener("click", async function () {
        setStatus("Probing live sources…", "");
        if (probeEl) probeEl.innerHTML = "";
        try {
          const r = await fetch("/api/settings/test", { method: "POST", headers: { "Content-Type": "application/json" } });
          const d = await r.json();
          if (r.status !== 200) throw new Error((d && d.detail) || "probe failed");
          var rows = [];
          Object.keys(d.sources).forEach(function (name) {
            var s = d.sources[name];
            var cls = s.configured ? (s.ok ? "pill green" : "pill red") : "pill blue";
            var label = s.configured ? (s.ok ? "ok" : "error") : "demo";
            var detail = s.error ? " — " + s.error : (s.ok ? " · " + s.ms + " ms" : "");
            rows.push('<div class="probe-row"><span class="' + cls + '">' + label + '</span> <code class="mono">' + name + "</code>" + detail + "</div>");
          });
          if (probeEl) probeEl.innerHTML = rows.join("");
          setStatus('Probe of "' + d.probe + '" finished.', "ok");
        } catch (err) {
          setStatus("Probe failed: " + err.message, "err");
        }
      });
    }
  };

  /**
   * Insert spaces between camelCase words ("bomFile.csv" -> "bom File.csv").
   * @param {string} n A file name.
   * @returns {string} The name with spaces inserted before capital letters.
   */
  function dashes(n) { return n.replace(/([a-z\d])([A-Z])/g, "$1 $2"); }

  win.Schemata = Schemata;
})(window);