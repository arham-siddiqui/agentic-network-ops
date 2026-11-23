// Small canvas line chart: hairline grid, 2px lines, direct end labels, threshold rules, a
// ground-truth band, and a crosshair tooltip. Null values are gaps, never zero.

const INK = { text: "#f2f2f0", text2: "#c4c5c0", muted: "#8d8f8a", grid: "#26272a", axis: "#3a3c40", surface: "#17181b" };

export const FORMAT = {
  rate: (v) => `${v.toFixed(1)}/s`,
  seconds: (v) => (v < 1 ? `${Math.round(v * 1000)} ms` : `${v.toFixed(2)} s`),
  fraction: (v) => `${Math.round(v * 100)}%`,
  cores: (v) => v.toFixed(2),
};

function niceMax(v) {
  if (!(v > 0)) return 1;
  const exp = 10 ** Math.floor(Math.log10(v));
  const n = v / exp;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * exp;
}

export class LineChart {
  constructor(container, spec) {
    this.spec = spec;
    this.data = null;
    const wrap = document.createElement("section");
    wrap.className = "panel chart";
    wrap.innerHTML = `<header><h3></h3><div class="legend"></div></header><canvas></canvas><div class="tooltip"></div>`;
    wrap.querySelector("h3").textContent = spec.title;
    const legend = wrap.querySelector(".legend");
    if (spec.series.length > 1) {
      for (const s of spec.series) {
        const item = document.createElement("span");
        item.innerHTML = `<i style="background:${s.color}"></i>`;
        item.append(s.label);
        legend.append(item);
      }
    } else if (spec.subtitle) {
      legend.textContent = spec.subtitle;
    }
    container.appendChild(wrap);
    this.canvas = wrap.querySelector("canvas");
    this.tooltip = wrap.querySelector(".tooltip");
    this.wrap = wrap;
    this.hoverX = null;
    this.canvas.addEventListener("mousemove", (e) => {
      const r = this.canvas.getBoundingClientRect();
      this.hoverX = e.clientX - r.left;
      this.draw();
    });
    this.canvas.addEventListener("mouseleave", () => {
      this.hoverX = null;
      this.tooltip.style.display = "none";
      this.draw();
    });
    new ResizeObserver(() => this.draw()).observe(this.canvas);
  }

  setData(data) {
    this.data = data;
    this.draw();
  }

  draw() {
    const d = this.data;
    const canvas = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
    if (canvas.height !== Math.round(h * dpr)) canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!d) return;

    const pad = { l: 52, r: 70, t: 16, b: 24 };
    const pw = w - pad.l - pad.r;
    const ph = h - pad.t - pad.b;
    const fmt = FORMAT[this.spec.unit];
    let maxV = this.spec.minMax ?? 0;
    for (const s of this.spec.series) for (const v of d.series[s.key] ?? []) if (v !== null && v > maxV) maxV = v;
    for (const th of this.spec.thresholds ?? []) maxV = Math.max(maxV, th.value);
    const yMax = this.spec.fixedMax ?? niceMax(maxV * 1.12);
    const x = (t) => pad.l + ((t - d.t0) / (d.t1 - d.t0)) * pw;
    const y = (v) => pad.t + ph - (Math.min(v, yMax) / yMax) * ph;

    // ground-truth band
    for (const band of d.bands ?? []) {
      const a = Math.max(d.t0, band.start);
      const b = Math.min(d.t1, band.end ?? d.t1);
      if (b <= a) continue;
      ctx.fillStyle = "rgba(250,178,25,0.10)";
      ctx.fillRect(x(a), pad.t, x(b) - x(a), ph);
      ctx.fillStyle = INK.muted;
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "left";
      if (x(b) - x(a) > 90) ctx.fillText("fault (ground truth)", x(a) + 6, pad.t + 12);
    }

    // grid + y labels
    ctx.font = "12px system-ui, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 3; i++) {
      const v = (yMax * i) / 3;
      const yy = Math.round(y(v)) + 0.5;
      ctx.strokeStyle = i === 0 ? INK.axis : INK.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.l, yy);
      ctx.lineTo(pad.l + pw, yy);
      ctx.stroke();
      ctx.fillStyle = INK.muted;
      ctx.fillText(fmt(v), pad.l - 6, yy);
    }
    // x labels (relative)
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const span = d.t1 - d.t0;
    for (let s = 0; s <= span; s += 60) {
      const t = d.t1 - s;
      // The end label is right-aligned so it never collides with the series end labels.
      ctx.textAlign = s === 0 ? "right" : "center";
      ctx.fillText(s === 0 ? (d.endLabel ?? "now") : `−${s / 60}m`, x(t), pad.t + ph + 6);
    }

    // thresholds
    for (const th of this.spec.thresholds ?? []) {
      const yy = Math.round(y(th.value)) + 0.5;
      ctx.strokeStyle = th.color ?? INK.muted;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(pad.l, yy);
      ctx.lineTo(pad.l + pw, yy);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = INK.muted;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(th.label, pad.l + pw + 6, yy);
    }

    // series lines
    const ends = [];
    for (const s of this.spec.series) {
      const values = d.series[s.key] ?? [];
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2.5;
      ctx.lineJoin = "round";
      ctx.beginPath();
      let pen = false;
      let last = null;
      values.forEach((v, i) => {
        if (v === null) {
          pen = false;
          return;
        }
        const px = x(d.xs[i]);
        const py = y(v);
        if (pen) ctx.lineTo(px, py);
        else ctx.moveTo(px, py);
        pen = true;
        last = [px, py, v];
      });
      ctx.stroke();
      if (last && this.spec.series.length <= 4) ends.push({ s, last });
    }
    // direct end labels, nudged apart
    ends.sort((a, b) => a.last[1] - b.last[1]);
    let prevY = -Infinity;
    for (const { s, last } of ends) {
      const ly = Math.max(last[1], prevY + 13);
      prevY = ly;
      ctx.fillStyle = s.color;
      ctx.fillRect(pad.l + pw + 6, ly - 1, 8, 3);
      ctx.fillStyle = INK.text2;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      if (!(this.spec.thresholds ?? []).some((th) => Math.abs(y(th.value) - ly) < 10)) {
        ctx.fillText(s.short ?? s.label, pad.l + pw + 17, ly);
      }
    }

    // crosshair + tooltip
    if (this.hoverX !== null && this.hoverX >= pad.l && this.hoverX <= pad.l + pw && d.xs.length) {
      const t = d.t0 + ((this.hoverX - pad.l) / pw) * (d.t1 - d.t0);
      let idx = 0;
      for (let i = 0; i < d.xs.length; i++) if (Math.abs(d.xs[i] - t) < Math.abs(d.xs[idx] - t)) idx = i;
      const px = x(d.xs[idx]);
      ctx.strokeStyle = INK.text2;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px + 0.5, pad.t);
      ctx.lineTo(px + 0.5, pad.t + ph);
      ctx.stroke();
      const rows = [];
      for (const s of this.spec.series) {
        const v = d.series[s.key]?.[idx] ?? null;
        if (v !== null) {
          ctx.fillStyle = s.color;
          ctx.strokeStyle = INK.surface;
          ctx.lineWidth = 2.5;
          ctx.beginPath();
          ctx.arc(px, y(v), 4.5, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }
        rows.push(`<div><span class="swatch" style="background:${s.color}"></span>${s.label}: ${v === null ? "no data" : fmt(v)}</div>`);
      }
      const ago = Math.round(d.t1 - d.xs[idx]);
      this.tooltip.innerHTML = `<div style="color:#898781">${ago === 0 ? "now" : `${ago} s ago`}</div>${rows.join("")}`;
      this.tooltip.style.display = "block";
      const left = Math.min(px + 12, w - this.tooltip.offsetWidth - 4);
      this.tooltip.style.left = `${Math.max(4, left)}px`;
      this.tooltip.style.top = "38px";
    }
  }
}
