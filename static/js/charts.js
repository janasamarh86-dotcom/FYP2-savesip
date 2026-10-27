/*
 * SaveSip - shared Chart.js helpers.
 *
 * Load after Chart.js. Each helper takes a canvas id and a series in the
 * shape produced by chart_series() in routes.py: { labels: [...], values: [...] }.
 * When a series has no data, the canvas is replaced with a short message.
 */
(function (global) {
  "use strict";

  // Palette for the 10 categories (and any other unnamed series), based on
  // the SaveSip violet / teal theme
  const PALETTE = [
    "#6c5ce7", "#14b8a6", "#f59e42", "#ec4899", "#4f46e5",
    "#10b981", "#fbbf24", "#a78bfa", "#64748b", "#0ea5e9",
  ];

  // Fixed colours so a label always looks the same on every chart
  const NAMED_COLORS = {
    "TikTok": "#2a2640",
    "Instagram": "#d62976",
    "Shopee Live": "#ee4d2d",
    "None": "#c4c1d4",
    "Cash": "#14b8a6",
    "Cashless": "#6c5ce7",
    "Essential": "#14b8a6",
    "Non-essential": "#f59e42",
  };

  // Brand colours for pages that pick a single colour (e.g. reports.html)
  const BRAND = { violet: "#6c5ce7", teal: "#14b8a6", red: "#e5484d" };

  const currency = new Intl.NumberFormat("en-MY", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  function formatRM(value) {
    const amount = Number(value) || 0;
    return (amount < 0 ? "-RM" : "RM") + currency.format(Math.abs(amount));
  }

  function axisRM(value) {
    return "RM" + Number(value).toLocaleString("en-MY");
  }

  function colorsFor(labels) {
    return labels.map(function (label, index) {
      return NAMED_COLORS[label] || PALETTE[index % PALETTE.length];
    });
  }

  // Helpers accept a canvas element or the id of one
  function resolveCanvas(target) {
    return typeof target === "string" ? document.getElementById(target) : target;
  }

  function hasData(values) {
    return values.some(function (value) { return value > 0; });
  }

  function showEmpty(canvas, message) {
    const note = document.createElement("div");
    note.className = "d-flex h-100 flex-column align-items-center justify-content-center text-muted small text-center";
    const icon = document.createElement("i");
    icon.className = "bi bi-bar-chart fs-2 mb-2";
    const text = document.createElement("span");
    text.textContent = message || "No data for this period yet.";
    note.append(icon, text);
    canvas.replaceWith(note);
  }

  function tooltipLabel(context) {
    const type = context.chart.config.type;
    if (type === "pie" || type === "doughnut") {
      const total = context.dataset.data.reduce(function (sum, value) { return sum + value; }, 0);
      const share = total ? Math.round((context.parsed / total) * 100) : 0;
      return context.label + ": " + formatRM(context.parsed) + " (" + share + "%)";
    }
    const horizontal = context.chart.options.indexAxis === "y";
    const value = horizontal ? context.parsed.x : context.parsed.y;
    return (context.dataset.label ? context.dataset.label + ": " : "") + formatRM(value);
  }

  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.tooltip.callbacks.label = tooltipLabel;

  /** Pie (or doughnut) chart. Zero-value slices are left out of the legend. */
  function pie(target, series, options) {
    options = options || {};
    const canvas = resolveCanvas(target);
    if (!canvas) return null;
    if (!hasData(series.values)) {
      showEmpty(canvas, options.emptyMessage);
      return null;
    }

    const labels = [];
    const values = [];
    series.labels.forEach(function (label, index) {
      if (series.values[index] > 0) {
        labels.push(label);
        values.push(series.values[index]);
      }
    });

    return new Chart(canvas, {
      type: options.doughnut ? "doughnut" : "pie",
      data: {
        labels: labels,
        datasets: [{
          data: values,
          backgroundColor: colorsFor(labels),
          borderColor: "#ffffff",
          borderWidth: 2,
        }],
      },
      options: {
        plugins: { legend: { position: "bottom" } },
      },
    });
  }

  /** Single-series bar chart. Set horizontal: true for long labels. */
  function bar(target, series, options) {
    options = options || {};
    const canvas = resolveCanvas(target);
    if (!canvas) return null;
    if (!hasData(series.values)) {
      showEmpty(canvas, options.emptyMessage);
      return null;
    }

    const valueAxis = options.horizontal ? "x" : "y";
    const scales = {};
    scales[valueAxis] = { beginAtZero: true, ticks: { callback: axisRM } };

    return new Chart(canvas, {
      type: "bar",
      data: {
        labels: series.labels,
        datasets: [{
          label: options.label || "Amount",
          data: series.values,
          backgroundColor: options.color || colorsFor(series.labels),
          borderRadius: 6,
          maxBarThickness: 56,
        }],
      },
      options: {
        indexAxis: options.horizontal ? "y" : "x",
        plugins: { legend: { display: false } },
        scales: scales,
      },
    });
  }

  /** Multi-series line chart. datasets: [{ label, data, color }] */
  function line(target, labels, datasets, options) {
    options = options || {};
    const canvas = resolveCanvas(target);
    if (!canvas) return null;
    if (!datasets.some(function (dataset) { return hasData(dataset.data); })) {
      showEmpty(canvas, options.emptyMessage);
      return null;
    }

    return new Chart(canvas, {
      type: "line",
      data: {
        labels: labels,
        datasets: datasets.map(function (dataset) {
          return {
            label: dataset.label,
            data: dataset.data,
            borderColor: dataset.color,
            backgroundColor: dataset.color,
            tension: 0.3,
            pointRadius: 4,
          };
        }),
      },
      options: {
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { position: "bottom" } },
        scales: { y: { beginAtZero: true, ticks: { callback: axisRM } } },
      },
    });
  }

  global.SaveSipCharts = { pie: pie, bar: bar, line: line, formatRM: formatRM, colors: BRAND };
})(window);
