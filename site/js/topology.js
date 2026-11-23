// SVG topology with animated request packets. Packet travel speed is slowed for visibility
// (illustrative); delays added by a fault are drawn at real scale on the simulation clock.

const NS = "http://www.w3.org/2000/svg";
const W = 170;
const H = 70;
const ROW = 118;
const NODES = {
  load: { x: 110, y: ROW, title: "Load", sub: "10 req/s" },
  api: { x: 370, y: ROW, title: "API", sub: "2 s timeout" },
  proxy: { x: 630, y: ROW, title: "Proxy", sub: "Toxiproxy" },
  worker: { x: 890, y: ROW, title: "Worker", sub: "0.5-core quota" },
  investigator: { x: 240, y: 318, title: "Investigator", sub: "read-only" },
  prometheus: { x: 520, y: 318, title: "Prometheus", sub: "scrapes every 2 s" },
};
const LANE_OUT = ROW - 9;
const LANE_BACK = ROW + 9;
const X = { loadR: 195, apiL: 285, apiR: 455, proxyL: 545, proxyR: 715, workerL: 805 };
const COLORS = { ok: "#5aa2f0", held: "#fab219", error: "#ef5a5a", reply: "#9cc7f5" };

function el(name, attrs = {}, parent) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function text(parent, x, y, content, cls, anchor = "middle") {
  const t = el("text", { x, y, class: cls, "text-anchor": anchor }, parent);
  t.textContent = content;
  return t;
}

export class Topology {
  constructor(container, config) {
    this.config = config;
    this.packets = [];
    this.svg = el("svg", { viewBox: "0 36 1000 364", role: "img", "aria-label": "Lab topology" });
    container.appendChild(this.svg);
    this.draw();
  }

  draw() {
    const s = this.svg;
    this.links = {
      loadApi: el("line", { x1: X.loadR, y1: ROW, x2: X.apiL, y2: ROW, class: "link" }, s),
      apiProxy: el("line", { x1: X.apiR, y1: ROW, x2: X.proxyL, y2: ROW, class: "link" }, s),
      proxyWorker: el("line", { x1: X.proxyR, y1: ROW, x2: X.workerL, y2: ROW, class: "link" }, s),
    };
    this.scrapes = {};
    const p = NODES.prometheus;
    for (const [job, node] of [["api", "api"], ["impairment-proxy", "proxy"], ["worker", "worker"]]) {
      const n = NODES[node];
      this.scrapes[job] = el(
        "path",
        { d: `M ${p.x} ${p.y - H / 2} C ${p.x} 230, ${n.x} 220, ${n.x} ${n.y + H / 2}`, class: "scrape" },
        s,
      );
    }
    const inv = NODES.investigator;
    el("line", { x1: inv.x + W / 2, y1: inv.y, x2: p.x - W / 2 - 8, y2: p.y, class: "readonly" }, s);
    el("path", { d: `M ${p.x - W / 2 - 8} ${p.y - 6} l 8 6 l -8 6 z`, class: "arrow" }, s);

    // Fault controls: operator-only, no path from the investigator.
    el("rect", { x: 720, y: 283, width: 250, height: 70, rx: 14, class: "control-zone" }, s);
    text(s, 845, 312, "Fault controls", "control-title");
    text(s, 845, 334, "operator terminal only", "control-sub");

    this.packetLayer = el("g", {}, s);

    this.nodes = {};
    for (const [key, n] of Object.entries(NODES)) {
      const g = el("g", { class: "node", transform: `translate(${n.x - W / 2} ${n.y - H / 2})` }, s);
      el("rect", { width: W, height: H, rx: 14 }, g);
      text(g, W / 2, 31, n.title);
      text(g, W / 2, 52, n.sub, "sub");
      this.nodes[key] = g;
    }
    this.badges = {};
    for (const key of ["proxy", "worker", "prometheus"]) {
      const n = NODES[key];
      const g = el("g", { class: "badge", transform: `translate(${n.x} ${n.y - H / 2 - 18})`, visibility: "hidden" }, s);
      const r = el("rect", { x: -60, y: -14, width: 120, height: 26, rx: 13 }, g);
      const t = text(g, 0, 4, "");
      this.badges[key] = { g, r, t };
    }

    // Legend
    const legend = el("g", { transform: "translate(40 384)", class: "legend" }, s);
    [["request", COLORS.ok], ["held at proxy", COLORS.held], ["failed", COLORS.error]].forEach(([label, color], i) => {
      el("circle", { cx: i * 150, cy: -4, r: 5, fill: color }, legend);
      text(legend, i * 150 + 12, 0, label, "legend-text", "start");
    });
  }

  badge(key, label, critical = false) {
    const b = this.badges[key];
    if (!label) {
      b.g.setAttribute("visibility", "hidden");
      return;
    }
    b.t.textContent = label;
    const width = Math.max(70, label.length * 8 + 24);
    b.r.setAttribute("x", -width / 2);
    b.r.setAttribute("width", width);
    b.g.setAttribute("class", critical ? "badge critical" : "badge");
    b.g.setAttribute("visibility", "visible");
  }

  // Reflect lab state on nodes, links, and scrape paths.
  update(state) {
    const kind = state.fault?.kind;
    const set = (node, cls) => this.nodes[node].setAttribute("class", `node ${cls}`.trim());
    const network = kind === "latency" || kind === "timeout" || kind === "network";
    set("worker", kind === "cpu" ? "hot" : kind === "worker_down" || !state.up.worker ? "down" : "");
    set("proxy", !state.up["impairment-proxy"] || kind === "proxy_down" ? "down" : network ? "impaired" : "");
    set("api", state.up.api ? "" : "down");
    this.links.apiProxy.setAttribute(
      "class",
      kind === "proxy_down" ? "link broken" : kind === "latency" || kind === "network" ? "link impaired" : "link",
    );
    this.links.proxyWorker.setAttribute(
      "class",
      kind === "timeout" || kind === "worker_down" ? "link broken" : kind === "latency" ? "link impaired" : "link",
    );
    for (const [job, path] of Object.entries(this.scrapes)) {
      path.setAttribute("class", state.up[job] ? "scrape" : "scrape down");
    }
    const p = state.fault?.params;
    this.badge(
      "proxy",
      kind === "latency"
        ? `+${p?.latencyMs ?? "?"} ms delay`
        : kind === "timeout"
          ? "dropping requests"
          : kind === "network"
            ? "network fault"
            : kind === "proxy_down"
              ? "down"
              : null,
      kind === "proxy_down" || kind === "timeout",
    );
    const cpu = state.workerCpu;
    this.badge(
      "worker",
      kind === "cpu" ? `CPU ${Number.isFinite(cpu) ? cpu.toFixed(2) : "?"} / 0.50` : kind === "worker_down" ? "down" : null,
      kind === "worker_down",
    );
    const firing = state.firing;
    this.badge("prometheus", firing ? `${firing} alert${firing > 1 ? "s" : ""} firing` : null, true);
  }

  // Build an illustrative packet timeline for one request.
  addRequest(request, now) {
    if (this.packets.length > this.config.ui.maxPackets) return;
    const hop = this.config.ui.hopAnimationMs / 1000;
    const span = (a, b, lanes) => ({ x1: a, x2: b, y: lanes });
    const legs = [];
    const plan = request.plan;
    const forward = (to, color = COLORS.ok) => legs.push({ ...span(X.loadR, to, LANE_OUT), dur: (hop * (to - X.loadR)) / 183, color });
    const back = (from, color, extraWait = 0) => {
      if (extraWait) legs.push({ ...span(from, from, LANE_BACK), dur: extraWait, color: COLORS.held });
      legs.push({ ...span(from, X.loadR, LANE_BACK), dur: (hop * (from - X.loadR)) / 183, color });
    };
    if (plan.stage === "ok") {
      forward(X.workerL);
      if (plan.heldMs > 0) {
        // Each held response gets its own slot beside the proxy; at most 4 are ever in flight.
        const slot = X.proxyR + 12 + (request.id % 4) * 14;
        legs.push({ ...span(X.workerL, slot, LANE_BACK), dur: hop * 0.4, color: COLORS.reply });
        back(slot, COLORS.reply, plan.heldMs / 1000);
      } else {
        back(X.workerL, COLORS.reply);
      }
    } else if (plan.stage === "dropped") {
      forward(X.proxyL);
      legs.push({ ...span(X.proxyL, X.proxyL, LANE_OUT), dur: 0.6, color: COLORS.error, fade: true });
      // The API gives up at its 2 s timeout and answers 504.
      const elapsed = legs.reduce((sum, l) => sum + l.dur, 0);
      legs.push({ ...span(X.apiL, X.apiL, LANE_BACK), dur: Math.max(0, 2 - elapsed), hidden: true });
      legs.push({ ...span(X.apiL, X.loadR, LANE_BACK), dur: hop * 0.4, color: COLORS.error });
    } else if (plan.stage === "upstream_refused") {
      forward(X.proxyR);
      back(X.proxyR, COLORS.error);
    } else {
      forward(X.apiR + 40);
      back(X.apiR + 40, COLORS.error);
    }
    const node = el("circle", { r: 6, class: "packet", fill: COLORS.ok }, this.packetLayer);
    this.packets.push({ start: now, legs, node, end: now + legs.reduce((sum, l) => sum + l.dur, 0) });
  }

  render(now) {
    this.packets = this.packets.filter((p) => {
      if (now >= p.end || now < p.start) {
        p.node.remove();
        return false;
      }
      let t = now - p.start;
      for (const leg of p.legs) {
        if (t <= leg.dur || leg === p.legs[p.legs.length - 1]) {
          const f = leg.dur > 0 ? Math.min(1, t / leg.dur) : 1;
          p.node.setAttribute("cx", leg.x1 + (leg.x2 - leg.x1) * f);
          p.node.setAttribute("cy", leg.y);
          p.node.setAttribute("fill", leg.color ?? COLORS.ok);
          p.node.setAttribute("opacity", leg.hidden ? 0 : leg.fade ? 1 - f : 1);
          break;
        }
        t -= leg.dur;
      }
      return true;
    });
  }

  clear() {
    for (const p of this.packets) p.node.remove();
    this.packets = [];
  }
}
