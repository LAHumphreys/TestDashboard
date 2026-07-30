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

    // valueText lets a caller print the value in its own units — "4m 12s"
    // rather than "252" — without this helper knowing what it is showing.
    row.appendChild(el("span", "bar-value",
      item.valueText || item.value.toLocaleString()));

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

/* ---------------- treemap (where the time goes) ---------------- */

/*
 * A profiler-style box graph: one rectangle per item, area proportional
 * to value, filling a fixed viewBox. Click a box to drill in.
 *
 * SQUARIFIED, not sliced. Naive slicing gives correct areas and unusable
 * shapes - a slow test inside a fast script becomes a 2px-wide sliver
 * that cannot be clicked or labelled. The squarify pass (Bruls, Huizing
 * & van Wijk) packs items into rows chosen to keep each box as close to
 * square as it can, and that is what leaves room for text and a hit
 * target.
 *
 * A treemap's real weakness is that it CANNOT label its small cells, and
 * pretending otherwise is what makes one unreadable. So text is drawn
 * only where it fits, and everything else stays reachable through the
 * tooltip, the keyboard, and the full data table under the chart. That
 * table is the accessible twin rather than a nicety - it is the only
 * place a 0.1% box is legible.
 *
 * Layout is pure arithmetic inside a fixed viewBox, deliberately: it
 * measures no element, so it produces identical geometry with or without
 * a browser and can be verified without one.
 */

/** Total of a list of {value} items, ignoring negatives. */
function sumValues(items) {
  return items.reduce((total, item) => total + Math.max(0, item.value), 0);
}

/**
 * Worst (largest) aspect ratio in a row of `areas` laid across `side`.
 * `sum` is the row's total area. Lower is better; 1 is a perfect square.
 */
function worstRatio(areas, side, sum) {
  if (sum <= 0 || side <= 0) {
    return Infinity;
  }
  const thickness = sum / side;
  let worst = 1;
  for (const area of areas) {
    const length = area / thickness;
    if (length <= 0) {
      return Infinity;
    }
    const ratio = Math.max(thickness / length, length / thickness);
    if (ratio > worst) {
      worst = ratio;
    }
  }
  return worst;
}

/**
 * Squarified treemap layout over the rectangle (x, y, w, h).
 *
 * `items` are [{value, ...}] and need NOT be sorted - a copy is sorted
 * descending, which is what the algorithm assumes. Returns
 * [{item, x, y, w, h}] and never mutates the input.
 */
export function treemapLayout(items, x, y, w, h) {
  const boxes = [];
  const queue = items
    .filter((item) => item.value > 0)
    .slice()
    .sort((a, b) => b.value - a.value);
  if (queue.length === 0 || w <= 0 || h <= 0) {
    return boxes;
  }

  // Work in AREA units, so the packing arithmetic is scale-free and a
  // row's thickness falls straight out of its total.
  const total = sumValues(queue);
  const scale = (w * h) / total;
  let left = x;
  let top = y;
  let freeW = w;
  let freeH = h;
  let index = 0;

  while (index < queue.length) {
    const side = Math.min(freeW, freeH);
    const row = [];
    const areas = [];
    let rowSum = 0;

    // Grow the row while adding the next item makes it MORE square.
    while (index < queue.length) {
      const area = queue[index].value * scale;
      const current = worstRatio(areas, side, rowSum);
      const next = worstRatio(areas.concat([area]), side, rowSum + area);
      if (areas.length > 0 && next > current) {
        break;
      }
      row.push(queue[index]);
      areas.push(area);
      rowSum += area;
      index += 1;
    }

    // Lay the row along the shorter side, then shrink the free space.
    const thickness = side > 0 ? rowSum / side : 0;
    const horizontal = freeW >= freeH;
    let offset = 0;
    for (let i = 0; i < row.length; i++) {
      const length = thickness > 0 ? areas[i] / thickness : 0;
      if (horizontal) {
        boxes.push({
          item: row[i], x: left, y: top + offset, w: thickness, h: length,
        });
      } else {
        boxes.push({
          item: row[i], x: left + offset, y: top, w: length, h: thickness,
        });
      }
      offset += length;
    }
    if (horizontal) {
      left += thickness;
      freeW -= thickness;
    } else {
      top += thickness;
      freeH -= thickness;
    }
    // Floating-point residue can leave a sliver no item is left for.
    if (freeW <= 0.01 || freeH <= 0.01) {
      break;
    }
  }
  return boxes;
}

/**
 * Shade class for a box, relative to the LARGEST box beside it.
 *
 * Relative, not an absolute share of the total, because an absolute
 * scale collapses at both ends of the range this page actually shows:
 * with three environments every box is over 30% and they all come out
 * the same darkest shade, and with sixty tests every box is under 2% and
 * they all come out the same lightest one. Either way the shading stops
 * separating anything, which was its only job.
 *
 * So depth means "large for this view", and NOT a proportion that can be
 * read off — the area is what encodes proportion, and the tooltip gives
 * the percentage in figures.
 */
function shadeFor(value, largest) {
  const ratio = largest > 0 ? value / largest : 0;
  if (ratio >= 0.75) return "tm-shade-4";
  if (ratio >= 0.50) return "tm-shade-3";
  if (ratio >= 0.30) return "tm-shade-2";
  if (ratio >= 0.12) return "tm-shade-1";
  return "tm-shade-0";
}

/* Below these sizes (viewBox units) a box cannot carry readable text. */
const TM_LABEL_MIN_W = 54;
const TM_LABEL_MIN_H = 20;
const TM_VALUE_MIN_H = 34;

/**
 * Truncate `text` to roughly `maxWidth` viewBox units.
 *
 * SVG text neither wraps nor ellipsizes, and an over-long <text> does
 * not clip - it spills across its neighbours, making two boxes
 * unreadable instead of one. 5.6 units per character matches the 11px
 * label style; erring narrow costs a character, erring wide costs the
 * spill.
 */
function fitText(text, maxWidth) {
  const perChar = 5.6;
  const room = Math.floor(maxWidth / perChar);
  const value = String(text);
  if (room <= 1) {
    return "";
  }
  return value.length <= room ? value : value.slice(0, room - 1) + "…";
}

/*
 * Smallest box worth drawing on its own, in square viewBox units.
 *
 * Below roughly this a box is a hairline: it cannot be labelled, cannot
 * comfortably be clicked, and adds no readable area. Items under it are
 * combined into ONE box carrying their summed value, which keeps every
 * area on screen proportional (see foldTiny).
 */
const TM_MIN_BOX_AREA = 220;

/**
 * Combine items too small to draw into one aggregate item.
 *
 * THE INVARIANT THIS EXISTS TO PROTECT: a box's share of the drawn area
 * equals its share of the total value. That is the only claim a treemap
 * makes, and it is the whole reason to use one — so something that is 1%
 * of the runtime occupies 1% of the screen, not 25% of it.
 *
 * Which rules out the obvious tidy-up. Showing "the largest 24" and
 * letting them fill the rectangle was tried and is wrong: it rescales
 * the survivors against each other, so on the dev database's 251 scripts
 * — where the time is spread almost evenly, and the top 24 are 11% of it
 * — every box on screen was inflated about ninefold. The chart looked
 * better and meant something false.
 *
 * Folding instead keeps the arithmetic true. The aggregate is as big as
 * the things it stands for really are, which on that same data is most
 * of the rectangle: 227 scripts, 4h 44m, 89% of the time. That reads as
 * "no single script is the problem, it is spread across all of them",
 * which is exactly what the data says and what a top-24 chart hid.
 *
 * Returns {shown, hiddenCount, hiddenValue, total, unlabelled}.
 */
function foldTiny(items, width, height, unit, format) {
  const sorted = items
    .filter((item) => item.value > 0)
    .slice()
    .sort((a, b) => b.value - a.value);
  const total = sumValues(sorted);
  if (total <= 0) {
    return {
      shown: [], hiddenCount: 0, hiddenValue: 0, total: 0, unlabelled: 0,
    };
  }

  const area = width * height;
  const big = [];
  const small = [];
  for (const item of sorted) {
    if ((item.value / total) * area >= TM_MIN_BOX_AREA) {
      big.push(item);
    } else {
      small.push(item);
    }
  }

  // One tiny item folded into "1 smaller script" is worse than just
  // drawing it, so only fold when there is a real tail.
  if (small.length < 2) {
    return {
      shown: sorted, hiddenCount: 0, hiddenValue: 0, total: total,
      unlabelled: 0,
    };
  }
  const hiddenValue = sumValues(small);
  big.push({
    label: small.length.toLocaleString() + " smaller " + (unit || "items"),
    value: hiddenValue,
    valueText: format(hiddenValue),
    isAggregate: true,
    onClick: null,
    tooltipRows: [
      { label: "combined", value: format(hiddenValue) },
      { label: unit || "items", value: small.length.toLocaleString() },
      { label: "share of the total", value:
        Math.round((hiddenValue / total) * 100) + "%" },
      { label: "each too small to draw; all in the table", value: "" },
    ],
  });
  return {
    shown: big,
    hiddenCount: small.length,
    hiddenValue: hiddenValue,
    total: total,
    unlabelled: 0,
  };
}

/**
 * Render a treemap into `container` (cleared first).
 *
 * items: [{label, sublabel, value, valueText, tooltipRows, onClick}] -
 * the same item shape barRows() takes, so a caller can swap one form for
 * the other without rebuilding its data.
 *
 * Returns {shown, hiddenCount, hiddenValue, total, unlabelled, drawn} so
 * the caller can say what was folded together and how many boxes went
 * unlabelled. Ignoring that return is how a chart that is not showing
 * everything comes to read as one that is.
 *
 * Every drawn box's share of the area equals its share of the total
 * value, aggregate included — see foldTiny for why nothing here rescales.
 */
export function treemapBoxes(container, items, options) {
  clearNode(container);
  const opts = options || {};
  const width = opts.width || 720;
  const height = opts.height || 380;
  // formatValue exists for the aggregate box: it is the only item this
  // function invents, so the only one with no valueText of its own, and
  // "1847" beside neighbours reading "30m 47s" is a bug on screen.
  const format = opts.formatValue || ((value) => String(value));
  const capped = foldTiny(items, width, height, opts.unitLabel, format);
  items = capped.shown;

  const svg = svgEl("svg", {
    viewBox: "0 0 " + width + " " + height,
    class: "treemap",
    role: "img",
  });
  const total = sumValues(items);
  const biggest = items.reduce(
    (best, item) => (best === null || item.value > best.value ? item : best),
    null);
  svg.setAttribute("aria-label",
    "Treemap of " + items.length + " "
    + (opts.unitLabel || "items") + " by share of "
    + (opts.measureLabel || "the total")
    + (biggest ? "; the largest is " + biggest.label : "")
    + (capped.hiddenCount
      ? "; the smallest " + capped.hiddenCount
        + " are combined into one box of their true combined size"
      : "")
    + ". Every row is in the data table below.");

  const boxes = treemapLayout(items, 0, 0, width, height);

  // The gap between boxes is painted INSIDE each box, so a fixed inset
  // takes a bigger fraction of a small box than a large one - which is a
  // distortion of exactly the thing the chart claims to encode. Scale it
  // to the smallest box so the distortion stays negligible everywhere.
  const smallestSide = boxes.reduce(
    (least, box) => Math.min(least, box.w, box.h), Infinity);
  const GAP = Math.max(0.5, Math.min(2, smallestSide / 10));

  let unlabelled = 0;
  for (const box of boxes) {
    const item = box.item;
    const bx = box.x + GAP / 2;
    const by = box.y + GAP / 2;
    const bw = Math.max(1, box.w - GAP);
    const bh = Math.max(1, box.h - GAP);

    const shade = shadeFor(item.value, biggest ? biggest.value : 0);
    // The two deepest shades need light text on them; the class says so
    // on the GROUP so the text rule is a plain descendant selector
    // rather than one that depends on sibling order inside it.
    const deep = shade === "tm-shade-3" || shade === "tm-shade-4";
    const group = svgEl("g", {
      class: "tm-box" + (item.onClick ? " is-clickable" : "")
        + (deep && !item.isAggregate ? " is-deep" : "")
        + (item.isAggregate ? " is-rest" : ""),
    });
    group.appendChild(svgEl("rect", {
      x: bx, y: by, width: bw, height: bh, rx: 2,
      // The aggregate stands for many things rather than being one of
      // them, so it is outside the shade scale: given a shade it reads as
      // the single biggest script on the page.
      class: item.isAggregate ? "tm-fill tm-rest-fill" : "tm-fill " + shade,
    }));

    if (bw >= TM_LABEL_MIN_W && bh >= TM_LABEL_MIN_H) {
      const name = svgEl("text", { x: bx + 6, y: by + 15, class: "tm-label" });
      name.textContent = fitText(item.label, bw - 12);
      group.appendChild(name);
      if (bh >= TM_VALUE_MIN_H) {
        const value = svgEl("text",
          { x: bx + 6, y: by + 29, class: "tm-value" });
        value.textContent = fitText(
          item.valueText || String(item.value), bw - 12);
        group.appendChild(value);
      }
    } else {
      // Counted, and reported by the caller. An unlabelled box is still
      // an honest one — it is the right size and it answers to hover and
      // to the keyboard — but a reader cannot tell WHICH thing it is, and
      // that has to be said rather than left to be discovered.
      unlabelled += 1;
    }

    // One focusable hit target per box, exactly the size of the box, so
    // the whole rectangle is the control rather than the text in it.
    const hit = svgEl("rect", {
      x: bx, y: by, width: bw, height: bh, rx: 2,
      class: "tm-hit", tabindex: "0", role: item.onClick ? "button" : "img",
    });
    const share = total > 0
      ? Math.round((item.value / total) * 100) + "%" : "0%";
    hit.setAttribute("aria-label",
      item.label + ": " + (item.valueText || item.value) + ", " + share
      + " of the total"
      + (item.sublabel ? ", " + item.sublabel : "")
      + (item.onClick ? ". Activate to break it down." : ""));
    if (item.onClick) {
      hit.addEventListener("click", item.onClick);
      hit.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          if (event.preventDefault) {
            event.preventDefault();
          }
          item.onClick();
        }
      });
    }
    if (item.tooltipRows) {
      const show = (event) => {
        group.classList.add("is-hover");
        const rect = hit.getBoundingClientRect();
        const px = event && event.clientX
          ? event.clientX : rect.left + rect.width / 2;
        const py = event && event.clientY ? event.clientY : rect.top;
        showTooltip(px, py, item.label, item.tooltipRows);
      };
      const hide = () => {
        group.classList.remove("is-hover");
        hideTooltip();
      };
      hit.addEventListener("pointermove", show);
      hit.addEventListener("pointerleave", hide);
      hit.addEventListener("focus", show);
      hit.addEventListener("blur", hide);
    }

    group.appendChild(hit);
    svg.appendChild(group);
  }

  container.appendChild(svg);
  capped.unlabelled = unlabelled;
  capped.drawn = boxes.length;
  return capped;
}
