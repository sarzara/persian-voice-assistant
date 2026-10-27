/* Builds the overview charts from the JSON embedded by the server. No inline script (CSP). */
(function () {
  "use strict";
  // Bars: widths come from data-pct (inline style attributes are blocked by the CSP).
  document.querySelectorAll(".bar [data-pct]").forEach(function (el) {
    var v = Math.max(0, Math.min(100, parseFloat(el.dataset.pct) || 0));
    if (el.classList.contains("tick")) el.style.insetInlineStart = v + "%"; else el.style.width = v + "%";
  });

  var node = document.getElementById("chart-data");
  if (!node || typeof Chart === "undefined") return;
  var data = JSON.parse(node.textContent);
  var nf = new Intl.NumberFormat("fa-IR");
  var nf2 = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 4 });
  var pf = new Intl.NumberFormat("fa-IR", { style: "percent", maximumFractionDigits: 0 });
  var dayLabel = function (iso) {
    try { return new Intl.DateTimeFormat("fa-IR-u-ca-persian", { month: "2-digit", day: "2-digit" }).format(new Date(iso + "T12:00:00Z")); }
    catch (e) { return iso.slice(5); }
  };
  var labels = data.days.map(dayLabel);
  var palette = ["#2f6fde", "#1f9d6b", "#d98a0b", "#8a5cd1", "#d14343", "#0e9aa7", "#6b7385"];

  Chart.defaults.font.family = "Vazirmatn, Tahoma, sans-serif";
  Chart.defaults.color = "#6b7385";
  Chart.defaults.borderColor = "#e5e8ee";
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.plugins.legend.rtl = true;
  Chart.defaults.plugins.tooltip.rtl = true;
  Chart.defaults.plugins.legend.position = "bottom";

  function pivot(rows, key, value) {
    var groups = {};
    rows.forEach(function (r) {
      var g = r[key];
      if (!groups[g]) groups[g] = {};
      groups[g][r.day] = (groups[g][r.day] || 0) + (r[value] || 0);
    });
    return Object.keys(groups).sort().map(function (g, i) {
      return { label: g, data: data.days.map(function (d) { return groups[g][d] || 0; }),
               backgroundColor: palette[i % palette.length], borderColor: palette[i % palette.length] };
    });
  }
  function axis(fmt) { return { ticks: { callback: function (v) { return fmt.format(v); } } }; }
  function tip(fmt) { return { callbacks: { label: function (c) { return c.dataset.label + ": " + fmt.format(c.parsed.y); } } }; }

  function stacked(id, datasets, fmt) {
    var el = document.getElementById(id);
    if (!el) return;
    new Chart(el, { type: "bar", data: { labels: labels, datasets: datasets },
      options: { scales: { x: { stacked: true, reverse: true }, y: Object.assign({ stacked: true, beginAtZero: true }, axis(fmt)) },
                 plugins: { tooltip: tip(fmt) } } });
  }
  function lines(id, datasets, fmt, extra) {
    var el = document.getElementById(id);
    if (!el) return;
    datasets.forEach(function (d) { d.tension = 0.3; d.cubicInterpolationMode = "monotone"; d.pointRadius = 2; d.spanGaps = true; d.fill = false; });
    new Chart(el, { type: "line", data: { labels: labels, datasets: datasets },
      options: { scales: { x: { reverse: true }, y: Object.assign({ beginAtZero: true }, axis(fmt), extra || {}) },
                 plugins: { tooltip: tip(fmt) } } });
  }
  function series(rows, field) {
    var byDay = {};
    rows.forEach(function (r) { byDay[r.day] = r[field]; });
    return data.days.map(function (d) { return d in byDay ? byDay[d] : null; });
  }

  stacked("tokensByModel", pivot(data.usage, "model", "tokens"), nf);
  stacked("tokensByPurpose", pivot(data.usage, "purpose", "tokens"), nf);
  lines("costByModel", pivot(data.usage, "model", "cost"), nf2);
  lines("latency", [
    { label: "P50 (ms)", data: series(data.latency, "p50"), borderColor: palette[0], backgroundColor: palette[0] },
    { label: "P95 (ms)", data: series(data.latency, "p95"), borderColor: palette[4], backgroundColor: palette[4] }
  ], nf);
  lines("quality", [
    { label: "نرخ موفقیت · Success", data: series(data.quality, "success_rate"), borderColor: palette[1], backgroundColor: palette[1] },
    { label: "نرخ ویرایش · Edit", data: series(data.quality, "edit_rate"), borderColor: palette[2], backgroundColor: palette[2] }
  ], pf, { max: 1 });
})();
