/* charts.js — hand-rolled chart primitives for the testboard home screen.
 *
 * No libraries, no build step. Three pieces:
 *   - a shared singleton tooltip (one per page, positioned near the cursor);
 *   - stackedColumnChart(): an SVG stacked-column chart (the nightly trend);
 *   - barRows(): HTML horizontal bars (environments, top failing scripts).
 *
 * Design rules applied throughout (see the project's chart conventions):
 * thin marks (columns <= 24px, bars 12px), 4px rounded data-ends with a
 * square baseline, 2px surface gaps between stacked segments, hairline
 * solid gridlines, a legend whenever there are >= 2 series, sparing direct
 * labels, and a hover/focus tooltip whose values are always also available
 * from a table twin — the tooltip enhances, it never gates.
 *
 * SECURITY: every dynamic string reaches the DOM via textContent only.
 */

"use strict";

import { clearNode, el } from "./api.js";

const SVG_NS = "http://www.w3.org/2000/svg";

/* ---------------- shared tooltip ---------------- */

let tooltipNode = null;

function tooltip() {
  if (!tooltipNode) {
    tooltipNode = el("div", "chart-tooltip");
    tooltipNode.setAttribute("role", "status");
    tooltipNode.hidden = true;
    document.body.appendChild(tooltipNode);
  }
  return tooltipNode;
}

/**
 * Show the tooltip near viewport coordinates (x, y).
 * `title` is a plain string; `rows` is [{swatchClass, label, value}] — the
 * value leads visually, the label follows; the swatch is a short line key.
 */
export function showTooltip(x, y, title, rows) {
  const node = tooltip();
  clearNode(node);
  if (title) {
    node.appendChild(el("div", "tt-title", title));
  }
  for (const row of rows) {
    const line = el("div", "tt-row");
    if (row.swatchClass) {
      line.appendChild(el("span", "tt-key " + row.swatchClass));
    }
    line.appendChild(el("span", "tt-value", row.value));
    line.appendChild(el("span", "tt-label", row.label));
    node.appendChild(line);
  }
  node.hidden = false;
  // Position after render so the size is known; clamp to the viewport.
  const rect = node.getBoundingClientRect();
  let left = x + 12;
  if (left + rect.width > window.innerWidth - 8) {
    left = x - rect.width - 12;
  }
  let top = y - rect.height - 10;
  if (top < 8) {
    top = y + 16;
  }
  node.style.left = Math.max(8, left) + "px";
  node.style.top = top + "px";
}

export function hideTooltip() {
  if (tooltipNode) {
    tooltipNode.hidden = true;
  }
}

/* ---------------- SVG helpers ---------------- */

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const key of Object.keys(attrs || {})) {
    node.setAttribute(key, String(attrs[key]));
  }
  return node;
}

/** Path for a rect whose TOP corners are rounded (data-end), base square. */
function roundedTopRectPath(x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h);
  return (
    "M" + x + "," + (y + h)
    + " L" + x + "," + (y + radius)
    + " Q" + x + "," + y + " " + (x + radius) + "," + y
    + " L" + (x + w - radius) + "," + y
    + " Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + radius)
    + " L" + (x + w) + "," + (y + h)
    + " Z"
  );
}

/** Round a raw maximum up to a clean axis maximum (1/2/2.5/5 × 10^k). */
function niceCeil(value) {
  if (value <= 0) {
    return 1;
  }
  const power = Math.pow(10, Math.floor(Math.log10(value)));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (value <= step * power) {
      return step * power;
    }
  }
  return 10 * power;
}

/** "2026-07-24" -> short display like "Jul 24" (display only). */
export function formatNight(isoDate) {
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  if (typeof isoDate !== "string" || isoDate.length < 10) {
    return String(isoDate);
  }
  const month = parseInt(isoDate.slice(5, 7), 10);
  const day = parseInt(isoDate.slice(8, 10), 10);
  if (!month || !day) {
    return isoDate;
  }
  return MONTHS[month - 1] + " " + day;
}

/* ---------------- stacked column chart (nightly trend) ---------------- */

/**
 * Render a stacked-column chart into `container` (cleared first).
 *
 * nights: [{date: "YYYY-MM-DD", ...perSeriesCounts, total}]
 * series: [{key, label, segClass, swatchClass}] — bottom of the stack first.
 * A legend row is rendered above the plot (>= 2 series), and each night
 * band is a keyboard-focusable hover target with a full tooltip.
 */
/**
 * Stacked columns over an ordered sequence of buckets.
 *
 * `options` lets a caller change what a bucket IS. The home screen plots
 * one column per calendar day; the suite page plots one per execution,
 * because a suite can run more than once in a day and collapsing those
 * into one column hides exactly the thing being looked for.
 *
 *   options.labelFor(item, index) -> x-axis tick text
 *   options.titleFor(item)        -> tooltip / aria title
 *   options.unit                  -> what a column counts ("night")
 */
export function stackedColumnChart(container, nights, series, options) {
  const opts = options || {};
  const titleFor = opts.titleFor || ((item) => formatNight(item.date));
  const labelFor = opts.labelFor || ((item, index) => {
    const day = parseInt(item.date.slice(8, 10), 10);
    return index === 0 || day === 1 ? formatNight(item.date) : String(day);
  });
  const unit = opts.unit || "night";
  clearNode(container);

  const legend = el("div", "chart-legend");
  for (const s of series) {
    const item = el("span", "legend-item");
    item.appendChild(el("span", "legend-swatch " + s.swatchClass));
    item.appendChild(el("span", "legend-label", s.label));
    legend.appendChild(item);
  }
  container.appendChild(legend);

  const width = 560;
  const height = 190;
  const margin = { top: 8, right: 6, bottom: 22, left: 34 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;

  const svg = svgEl("svg", {
    viewBox: "0 0 " + width + " " + height,
    role: "img",
  });
  svg.setAttribute("aria-label",
    "Stacked columns: " + series.map((s) => s.label).join(" and ")
    + " per " + unit);

  const stackTotal = (night) =>
    series.reduce((sum, s) => sum + (night[s.key] || 0), 0);
  const rawMax = Math.max(0, ...nights.map(stackTotal));
  const yMax = niceCeil(rawMax);
  const yScale = (value) => plotH - (value / yMax) * plotH;

  // Gridlines + y tick labels: a few clean values, hairline, solid.
  // Counts are whole numbers, so never label a gridline "0.5 runs" —
  // with a small maximum, one gridline at the top is the honest axis.
  const tickCount = yMax >= 2 ? 2 : 1;
  for (let i = 0; i <= tickCount; i++) {
    const value = (yMax / tickCount) * i;
    const y = margin.top + yScale(value);
    svg.appendChild(svgEl("line", {
      x1: margin.left, y1: y, x2: width - margin.right, y2: y,
      class: i === 0 ? "chart-axisline" : "chart-gridline",
    }));
    if (i > 0) {
      const label = svgEl("text", {
        x: margin.left - 6, y: y + 3.5,
        class: "chart-tick", "text-anchor": "end",
      });
      label.textContent = value.toLocaleString();
      svg.appendChild(label);
    }
  }

  const band = plotW / Math.max(1, nights.length);
  const barW = Math.min(24, Math.max(6, band - 10));
  const SEG_GAP = 2;      // surface gap between stacked segments
  const CORNER = 3.5;     // rounded data-end radius

  nights.forEach((night, index) => {
    const bandX = margin.left + index * band;
    const barX = bandX + (band - barW) / 2;
    const group = svgEl("g", { class: "trend-band" });

    // Segments, bottom-up; only the topmost visible segment is rounded.
    let cumulative = 0;
    const total = stackTotal(night);
    let remainingAbove = total;
    for (const s of series) {
      const value = night[s.key] || 0;
      remainingAbove -= value;
      if (value <= 0) {
        continue;
      }
      const yTop = margin.top + yScale(cumulative + value);
      const yBottom = margin.top + yScale(cumulative);
      const h = Math.max(1, yBottom - yTop - (cumulative > 0 ? SEG_GAP : 0));
      const y = yBottom - h - (cumulative > 0 ? SEG_GAP : 0);
      const isTop = remainingAbove === 0;
      if (isTop) {
        group.appendChild(svgEl("path", {
          d: roundedTopRectPath(barX, y, barW, h, CORNER),
          class: "seg " + s.segClass,
        }));
      } else {
        group.appendChild(svgEl("rect", {
          x: barX, y: y, width: barW, height: h,
          class: "seg " + s.segClass,
        }));
      }
      cumulative += value;
    }

    // X label: sparse enough to stay legible however many columns there
    // are — a 45-day window in a half-width card cannot carry 22 labels.
    const stride = Math.max(1, Math.ceil(nights.length / 8));
    const last = nights.length - 1;
    const showLabel = index === 0 || index === last
      || (index % stride === 0 && last - index >= stride);
    if (showLabel) {
      // Anchor the outermost labels to the plot edges. Centred, a wide
      // label on the first or last band overflows the viewBox and gets
      // clipped — which is how a timestamp ends up reading "14:3(".
      const isFirst = index === 0;
      const isLast = index === nights.length - 1;
      const anchor = isFirst ? "start" : (isLast ? "end" : "middle");
      let x = bandX + band / 2;
      if (isFirst) {
        x = margin.left;
      } else if (isLast) {
        x = width - margin.right;
      }
      const label = svgEl("text", {
        x: x, y: height - 7,
        class: "chart-tick", "text-anchor": anchor,
      });
      label.textContent = labelFor(night, index);
      svg.appendChild(label);
    }

    // Hover/focus target: the whole band, full plot height (>= mark size).
    const hit = svgEl("rect", {
      x: bandX, y: margin.top, width: band, height: plotH,
      class: "band-hit", tabindex: "0", role: "img",
    });
    const parts = series
      .map((s) => (night[s.key] || 0).toLocaleString() + " " + s.label);
    hit.setAttribute("aria-label",
      titleFor(night) + ": " + parts.join(", ")
      + ", " + (night.total || 0).toLocaleString() + " runs");
    const show = (event) => {
      group.classList.add("is-hover");
      const rect = hit.getBoundingClientRect();
      const x = event && event.clientX ? event.clientX
        : rect.left + rect.width / 2;
      const y = rect.top;
      const rows = series.map((s) => ({
        swatchClass: s.swatchClass,
        label: s.label,
        value: (night[s.key] || 0).toLocaleString(),
      }));
      rows.push({
        swatchClass: "",
        label: "runs",
        value: (night.total || 0).toLocaleString(),
      });
      showTooltip(x, y, titleFor(night), rows);
    };
    const hide = () => {
      group.classList.remove("is-hover");
      hideTooltip();
    };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", show);
    hit.addEventListener("blur", hide);

    svg.appendChild(group);
    svg.appendChild(hit);
  });

  container.appendChild(svg);
}

/* ---------------- horizontal bar rows (HTML) ---------------- */

/**
 * Render label + bar + value rows into `container` (cleared first).
 *
 * items: [{label, sublabel, value, tooltipRows, onClick}] — bars scale to
 * the max value; every bar carries its value as a direct end label, so the
 * tooltip only adds context. Rows with onClick become real buttons.
 */
export function barRows(container, items, options) {
  clearNode(container);
  const opts = options || {};
  const max = Math.max(1, ...items.map((item) => item.value));

  for (const item of items) {
    const row = el(item.onClick ? "button" : "div", "bar-row");
    if (item.onClick) {
      row.type = "button";
      row.addEventListener("click", item.onClick);
    }

    const labelBox = el("div", "bar-labels");
    labelBox.appendChild(el("span", "bar-label", item.label));
    if (item.sublabel) {
      labelBox.appendChild(el("span", "bar-sublabel", item.sublabel));
    }
    row.appendChild(labelBox);

    const track = el("div", "bar-track");
    const fill = el("div", "bar-fill " + (opts.fillClass || "fill-fail"));
    const pct = (item.value / max) * 100;
    fill.style.width = (item.value > 0 ? Math.max(pct, 1.5) : 0) + "%";
    track.appendChild(fill);
    row.appendChild(track);

    row.appendChild(el("span", "bar-value", item.value.toLocaleString()));

    if (item.tooltipRows) {
      const show = (event) => {
        showTooltip(event.clientX, event.clientY, item.label,
          item.tooltipRows);
      };
      row.addEventListener("pointermove", show);
      row.addEventListener("pointerleave", hideTooltip);
      row.addEventListener("focus", (event) => {
        const rect = row.getBoundingClientRect();
        showTooltip(rect.left + rect.width / 2, rect.top, item.label,
          item.tooltipRows);
      });
      row.addEventListener("blur", hideTooltip);
    }

    container.appendChild(row);
  }
}
