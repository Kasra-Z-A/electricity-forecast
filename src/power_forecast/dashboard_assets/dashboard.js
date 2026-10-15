"use strict";
const MODEL_NAMES = {
  "seasonal-daily-v1": "Prior-day baseline",
  "seasonal-weekly-v1": "Weekly baseline",
};
const $ = (id) => document.getElementById(id);
const fmt = (value) => (value == null ? "—" : value.toFixed(2));
const time = (value) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Vienna",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "shortOffset",
  }).format(new Date(value));
const stamp = (value) =>
  value
    ? new Intl.DateTimeFormat("en-GB", {
        timeZone: "Europe/Vienna",
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        timeZoneName: "short",
      }).format(new Date(value))
    : "No recorded attempt";
const dayLabel = (value) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(new Date(value + "T12:00:00Z"));
const node = (tag, text, className) => {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (className) el.className = className;
  return el;
};
const theme = () => {
  $("theme").textContent =
    document.documentElement.dataset.theme === "dark" ? "Light" : "Dark";
  $("theme").setAttribute(
    "aria-label",
    `Switch to ${$("theme").textContent.toLowerCase()} theme`,
  );
};
theme();
$("theme").addEventListener("click", () => {
  const next =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  window.storeTheme(next);
  theme();
});
let data,
  mode = "replay",
  current,
  curves = [],
  chartGeometry;
function selectMode(next) {
  mode = next;
  curves = data.curves.filter((c) => c.mode === mode);
  $("live").setAttribute("aria-pressed", String(mode === "live"));
  $("replay").setAttribute("aria-pressed", String(mode === "replay"));
  $("day").replaceChildren(
    ...curves.map((c) => {
      const option = node("option", dayLabel(c.date));
      option.value = c.date;
      return option;
    }),
  );
  $("day").disabled = !curves.length;
  // Prefer the newest day that already has actual prices. Tomorrow is published
  // but cannot be scored until its auction clears, so opening on it shows a
  // forecast with no actual line and nothing to compare it against.
  const scored = curves.find((c) => c.points.some((p) => p.actual != null));
  if (scored) $("day").value = scored.date;
  renderCurve();
  renderMetrics();
}
function renderCurve() {
  current = curves.find((c) => c.date === $("day").value);
  const empty = !current;
  $("empty").hidden = !empty;
  $("chart").hidden = empty;
  $("slot").hidden = empty;
  document.querySelector(".slider-label").hidden = empty;
  $("table-details").hidden = empty;
  $("empty").textContent =
    mode === "live"
      ? "No official forecasts have been published yet. Explore the historical replay while the daily service is being prepared."
      : "No historical replays are available yet.";
  $("curve-caption").textContent = current
    ? `${dayLabel(current.date)} · ${mode === "replay" ? "Reconstructed replay" : current.status === "late" ? "Official · published late" : "Official forecast"}`
    : "Awaiting the first forecast";
  $("day-facts").replaceChildren();
  $("intervals").replaceChildren();
  $("hover").textContent = "";
  if (!current) return;
  const facts = [
    MODEL_NAMES[current.model] || current.model,
    `${current.points.length} delivery intervals`,
    `${current.points.filter((p) => p.fallback).length} fallback slots`,
    `Cutoff ${stamp(current.cutoff_at)}`,
    current.published_at
      ? `Published ${stamp(current.published_at)}`
      : `Replay created ${stamp(current.issued_at)}`,
  ];
  $("day-facts").replaceChildren(...facts.map((x) => node("span", x)));
  for (const p of current.points) {
    const tr = node("tr");
    tr.append(
      ...[
        time(p.start),
        fmt(p.forecast),
        fmt(p.actual),
        p.fallback ? "Yes" : "No",
      ].map((x) => node("td", x)),
    );
    $("intervals").append(tr);
  }
  $("slot").max = current.points.length - 1;
  $("slot").value = 0;
  drawChart();
  inspect(0);
}
const NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, text) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}
function drawChart() {
  const chart = $("chart");
  chart.replaceChildren();
  const points = current.points;
  const values = points
    .flatMap((p) => [p.forecast, p.actual])
    .filter((v) => v != null);
  const low = Math.min(0, ...values),
    high = Math.max(...values),
    pad = Math.max((high - low) * 0.12, 5),
    min = low - pad,
    max = high + pad;
  const width = Math.max(300, Math.min(1000, chart.clientWidth));
  chart.setAttribute("viewBox", `0 0 ${width} 330`);
  const plotWidth = width - 80;
  const x = (i) => 60 + (i / Math.max(points.length - 1, 1)) * plotWidth,
    y = (v) => 285 - ((v - min) / (max - min)) * 255;
  chartGeometry = { x, y, plotWidth };
  chart.append(
    svg(
      "title",
      {},
      `${dayLabel(current.date)} electricity prices in euros per megawatt hour`,
    ),
  );
  for (let i = 0; i <= 4; i++) {
    const v = min + ((max - min) * i) / 4;
    chart.append(
      svg("line", {
        x1: 60,
        x2: width - 20,
        y1: y(v),
        y2: y(v),
        class: "grid-line",
      }),
      svg("text", { x: 48, y: y(v) + 4, "text-anchor": "end" }, Math.round(v)),
    );
  }
  for (
    let i = 0;
    i < points.length;
    i += Math.ceil(points.length / (width < 500 ? 4 : 6))
  ) {
    chart.append(
      svg(
        "text",
        { x: x(i), y: 311, "text-anchor": i === 0 ? "start" : "middle" },
        time(points[i].start).split(" ")[0],
      ),
    );
  }
  for (const [key, cls] of [
    ["forecast", "forecast-line"],
    ["actual", "actual-line"],
  ]) {
    let path = "",
      open = false;
    points.forEach((p, i) => {
      if (p[key] == null) {
        open = false;
        return;
      }
      path += `${open ? "L" : "M"}${x(i)},${y(p[key])} `;
      open = true;
    });
    chart.append(svg("path", { d: path, class: cls }));
  }
  chart.append(
    svg("line", {
      id: "crosshair",
      x1: 60,
      x2: 60,
      y1: 20,
      y2: 285,
      class: "crosshair",
    }),
    svg("circle", {
      id: "marker",
      cx: 60,
      cy: y(points[0].forecast),
      r: 4,
      fill: "var(--rust)",
    }),
  );
}
function inspect(index) {
  if (!current) return;
  index = Math.max(0, Math.min(current.points.length - 1, index));
  const p = current.points[index];
  $("slot").value = index;
  const x = chartGeometry.x(index);
  $("crosshair").setAttribute("x1", x);
  $("crosshair").setAttribute("x2", x);
  $("marker").setAttribute("cx", x);
  $("marker").setAttribute("cy", chartGeometry.y(p.forecast));
  $("hover").textContent =
    `${time(p.start)} · Forecast ${fmt(p.forecast)} · Actual ${fmt(p.actual)} €/MWh`;
  $("slot").setAttribute("aria-valuetext", $("hover").textContent);
}
$("slot").addEventListener("input", (e) => inspect(Number(e.target.value)));
$("chart").addEventListener("pointermove", (e) => {
  if (!current) return;
  const point = $("chart").createSVGPoint();
  point.x = e.clientX;
  point.y = e.clientY;
  const local = point.matrixTransform($("chart").getScreenCTM().inverse());
  inspect(
    Math.round(
      ((local.x - 60) / chartGeometry.plotWidth) * (current.points.length - 1),
    ),
  );
});
function renderMetrics() {
  // Errors summarise one model: the one behind the newest day in this mode.
  const model = curves[0]?.model;
  const scored = curves.filter((c) => c.mae != null && c.model === model),
    excluded = curves.filter((c) => c.mae != null && c.model !== model).length,
    count = scored.reduce((s, c) => s + c.points.length, 0);
  const mae = count
    ? scored.reduce((s, c) => s + c.mae * c.points.length, 0) / count
    : null;
  const rmse = count
    ? Math.sqrt(
        scored.reduce((s, c) => s + c.rmse * c.rmse * c.points.length, 0) /
          count,
      )
    : null;
  const metrics = [
    [fmt(mae), "Mean absolute error", "€/MWh"],
    [fmt(rmse), "Root mean square error", "€/MWh"],
    [String(scored.length), "Complete days", ""],
    [count.toLocaleString("en-GB"), "Intervals scored", ""],
  ];
  $("metrics").replaceChildren(
    ...metrics.map(([value, label, unit]) => {
      const block = node("div");
      const dd = node("p", value, "metric-value");
      dd.append(node("small", unit ? " " + unit : ""));
      block.append(dd, node("p", label, "label"));
      return block;
    }),
  );
  $("evaluation-note").textContent =
    mode === "replay"
      ? "Reconstructed history. These results test the pipeline; they are not a held-out model comparison."
      : "Official forecasts only. Errors are computed against the latest complete archived actual prices.";
  $("metric-window").textContent = scored.length
    ? `${MODEL_NAMES[model] || model} · ${dayLabel(scored.at(-1).date)} – ${dayLabel(scored[0].date)} · Interval-weighted errors · Latest archived actuals; revisions may change these figures.` +
      (excluded
        ? ` ${excluded} earlier ${excluded === 1 ? "day" : "days"} from another model ${excluded === 1 ? "is" : "are"} not included.`
        : "")
    : "Metrics appear when complete actual prices are available.";
  const recent = scored.slice(0, 14).reverse(),
    max = Math.max(1, ...recent.map((c) => c.mae));
  $("error-bars").replaceChildren(
    ...recent.map((c) => {
      const b = node("button", null, "error-day");
      b.setAttribute(
        "aria-label",
        `${dayLabel(c.date)}: mean absolute error ${fmt(c.mae)} euros per megawatt hour. Show this day.`,
      );
      b.append(node("span", c.mae.toFixed(1), "value"));
      const bar = node("span", null, "bar");
      bar.style.height = `${Math.max(2, (c.mae / max) * 75)}px`;
      b.append(bar, node("span", c.date.slice(5).replace("-", "/"), "day"));
      b.addEventListener("click", () => {
        $("day").value = c.date;
        renderCurve();
        $("curve-title").scrollIntoView({ block: "start" });
      });
      return b;
    }),
  );
}
function renderStatus() {
  const today = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Vienna",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  const age = Date.now() - new Date(data.generated_at).getTime();
  const stale = age > 20 * 60 * 1000;
  $("availability").textContent = stale
    ? `Archive snapshot · Updated ${stamp(data.generated_at)}. This snapshot is older than 20 minutes; check freshness before relying on publication status.`
    : data.tomorrow_available
      ? `Official forecast available for ${dayLabel(data.tomorrow)}. Select Official forecasts to explore it.`
      : `No official forecast for ${dayLabel(data.tomorrow)} yet. Historical replays are available below.`;
  // A stale export must never announce an old curve as tomorrow's forecast.
  if (data.tomorrow <= today && !stale)
    $("availability").textContent =
      "The dashboard snapshot needs refreshing. Check the delivery dates shown below.";
  if (data.deployment_mode === "snapshot")
    $("availability").textContent =
      `Historical dashboard · Exported ${stamp(data.generated_at)}. This version shows archived results. Daily forecasting is not running yet.`;
  else if (!stale && data.scheduler_active && !data.tomorrow_available)
    $("availability").textContent =
      `Daily jobs are active. Next forecast attempt: ${stamp(data.next_forecast_at)}, for delivery on ${dayLabel(data.next_delivery_date)}. Historical results are available below.`;
  const names = {
    collect: "Price collection",
    publish: "Forecast publication",
    settle: "Actuals & scoring",
    watchdog: "Pipeline check",
  };
  $("jobs").replaceChildren(
    ...data.jobs.map((j) => {
      const row = node("div", null, "job");
      const label = node("span", names[j.job]);
      label.append(node("small", stamp(j.finished_at)));
      const old =
        j.finished_at &&
        Date.now() - new Date(j.finished_at).getTime() > 36 * 3600000;
      row.append(
        label,
        node(
          "span",
          `${j.status.replaceAll("_", " ")}${old ? " · stale" : ""}`,
          "job-status",
        ),
      );
      return row;
    }),
  );
  $("updated").textContent =
    `Dashboard refreshed ${stamp(data.generated_at)}. Times shown in Europe/Vienna.`;
}
$("day").addEventListener("change", renderCurve);
$("live").addEventListener("click", () => selectMode("live"));
$("replay").addEventListener("click", () => selectMode("replay"));
async function load() {
  try {
    const response = await fetch("dashboard.json", { cache: "no-store" });
    if (!response.ok) throw Error("unavailable");
    data = await response.json();
    selectMode(
      data.tomorrow_available
        ? "live"
        : data.curves.some((c) => c.mode === "replay")
          ? "replay"
          : "live",
    );
    renderStatus();
  } catch {
    $("availability").textContent =
      "The dashboard data is unavailable. Please try again shortly.";
    $("curve-caption").textContent = "Data unavailable";
    $("day").disabled = true;
    $("live").disabled = true;
    $("replay").disabled = true;
    $("slot").hidden = true;
  }
}
new ResizeObserver(() => {
  if (current && !$("chart").hidden) {
    const index = Number($("slot").value);
    drawChart();
    inspect(index);
  }
}).observe($("chart"));
load();
setInterval(() => {
  if (data) renderStatus();
}, 60000);
