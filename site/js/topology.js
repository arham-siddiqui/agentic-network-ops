// SVG topology with animated request packets. Packet travel speed is slowed for visibility
// (illustrative); delays added by a fault are drawn at real scale on the simulation clock.

const NS = "http://www.w3.org/2000/svg";
const W = 170;
const H = 64;
const NODES = {
  load: { x: 95, y: 120, title: "Load generator", sub: "10 req/s · ≤ 4 in flight" },
  api: { x: 335, y: 120, title: "API", sub: "2 s worker-call timeout" },
  proxy: { x: 575, y: 120, title: "Impairment proxy", sub: "Toxiproxy · api_to_worker" },
  worker: { x: 815, y: 120, title: "Worker", sub: "0.5-core CPU quota" },
  investigator: { x: 160, y: 335, title: "Investigator (host)", sub: "read-only · allowlisted" },
  prometheus: { x: 440, y: 335, title: "Prometheus", sub: "scrape 2 s · rules 2 s" },
  grafana: { x: 640, y: 335, title: "Grafana", sub: "dashboard" },
};
const LANE_OUT = 111;
const LANE_BACK = 129;
const X = { loadR: 180, apiL: 250, apiR: 420, proxyL: 490, proxyR: 660, workerL: 730 };
const COLORS = { ok: "#3987e5", held: "#fab219", error: "#d03b3b", reply: "#7fb2ef" };

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
    this.svg = el("svg", { viewBox: "0 0 1000 470", role: "img", "aria-label": "Lab topology" });
    container.appendChild(this.svg);
    this.draw();
  }

  draw() {
    const s = this.svg;
    el("rect", { x: 10, y: 44, width: 980, height: 168, rx: 12, class: "zone" }, s);
    text(s, 24, 64, "lab network (internal)", "zone-label", "start");
    el("rect", { x: 10, y: 262, width: 735, height: 200, rx: 12, class: "zone" }, s);
    text(s, 24, 282, "monitoring + investigation", "zone-label", "start");
    el("rect", { x: 760, y: 262, width: 230, height: 200, rx: 12, class: "zone control-zone" }, s);
    text(s, 774, 282, "control network (internal)", "zone-label", "start");

    this.links = {
      loadApi: el("line", { x1: X.loadR, y1: 120, x2: X.apiL, y2: 120, class: "link" }, s),
      apiProxy: el("line", { x1: X.apiR, y1: 120, x2: X.proxyL, y2: 120, class: "link" }, s),
      proxyWorker: el("line", { x1: X.proxyR, y1: 120, x2: X.workerL, y2: 120, class: "link" }, s),
    };
    this.scrapes = {};
    for (const [job, node] of [["api", "api"], ["impairment-proxy", "proxy"], ["worker", "worker"]]) {
      const n = NODES[node];
      this.scrapes[job] = el(
        "path",
        { d: `M ${NODES.prometheus.x} ${NODES.prometheus.y - H / 2} C ${NODES.prometheus.x} 240, ${n.x} 230, ${n.x} ${n.y + H / 2}`, class: "scrape" },
        s,
      );
    }
    text(s, 452, 238, "scrape /metrics", "edge-label", "start");
    el("line", { x1: NODES.prometheus.x + W / 2, y1: 335, x2: NODES.grafana.x - W / 2, y2: 335, class: "link" }, s);
    el("line", { x1: NODES.investigator.x + W / 2, y1: 335, x2: NODES.prometheus.x - W / 2 - 6, y2: 335, class: "readonly" }, s);
    el("path", { d: `M ${NODES.prometheus.x - W / 2 - 6} 329 l 8 6 l -8 6 z`, fill: "#3987e5" }, s);
    text(s, (NODES.investigator.x + NODES.prometheus.x) / 2, 324, "21 fixed queries", "edge-label blue");

    // Control plane: proxy admin API and the network fault controller, unreachable from the lab network.
    el("rect", { x: 775, y: 296, width: 200, height: 36, rx: 8, class: "zone" }, s);
    text(s, 875, 319, "proxy admin API :8474", "edge-label");
    el("rect", { x: 775, y: 342, width: 200, height: 36, rx: 8, class: "zone" }, s);
    text(s, 875, 365, "network fault controller", "edge-label");
    text(s, 875, 400, "operator terminal → unix socket", "edge-label");
    text(s, 875, 418, "(no HTTP fault endpoint)", "edge-label");
    el("path", { d: `M ${NODES.proxy.x + 60} ${NODES.proxy.y + H / 2} C 640 250, 760 260, 775 314`, class: "scrape" }, s);
    text(s, 682, 252, "admin (control net only)", "edge-label");
    el("path", { d: `M ${NODES.investigator.x} ${NODES.investigator.y + H / 2} L ${NODES.investigator.x} 440 L 760 440`, class: "scrape down" }, s);
    const xMark = el("g", { transform: "translate(560 440)" }, s);
    el("line", { x1: -8, y1: -8, x2: 8, y2: 8, class: "no-path" }, xMark);
    el("line", { x1: -8, y1: 8, x2: 8, y2: -8, class: "no-path" }, xMark);
    text(s, 420, 432, "no route or tool from the investigator to fault controls", "edge-label red");
    text(s, NODES.worker.x, 194, "CPU fault: unix socket inside the container", "edge-label");

    this.packetLayer = el("g", {}, s);

    this.nodes = {};
    for (const [key, n] of Object.entries(NODES)) {
      const g = el("g", { class: "node", transform: `translate(${n.x - W / 2} ${n.y - H / 2})` }, s);
      el("rect", { width: W, height: H }, g);
      text(g, W / 2, 28, n.title);
      text(g, W / 2, 47, n.sub, "sub");
      this.nodes[key] = g;
    }
    this.badges = {};
    for (const key of ["proxy", "worker", "prometheus", "api"]) {
      const n = NODES[key];
      const g = el("g", { class: "badge", transform: `translate(${n.x} ${n.y - H / 2 - 4})`, visibility: "hidden" }, s);
      const r = el("rect", { x: -60, y: -12, width: 120, height: 22 }, g);
      const t = text(g, 0, 4, "");
      this.badges[key] = { g, r, t };
    }
  }

  badge(key, label, critical = false) {
    const b = this.badges[key];
    if (!label) {
      b.g.setAttribute("visibility", "hidden");
      return;
    }
    b.t.textContent = label;
    const width = Math.max(60, label.length * 7.2 + 18);
    b.r.setAttribute("x", -width / 2);
    b.r.setAttribute("width", width);
    b.g.setAttribute("class", critical ? "badge critical" : "badge");
    b.g.setAttribute("visibility", "visible");
  }

  // Reflect lab state on nodes, links, and scrape paths.
  update(state) {
    const kind = state.fault?.kind;
    const set = (node, cls) => this.nodes[node].setAttribute("class", `node ${cls}`.trim());
    set("worker", kind === "cpu" ? "hot" : kind === "worker_down" ? "down" : "");
    const network = kind === "latency" || kind === "timeout" || kind === "network";
    set("proxy", !state.up["impairment-proxy"] || kind === "proxy_down" ? "down" : network ? "impaired" : "");
    if (!state.up.worker) set("worker", "down");
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
        ? `toxic: +${p?.latencyMs ?? "?"} ms`
        : kind === "timeout"
          ? "toxic: dropping requests"
          : kind === "network"
            ? "network fault (ground truth)"
            : kind === "proxy_down"
              ? "DOWN"
              : null,
      kind === "proxy_down",
    );
    this.badge("worker", kind === "cpu" ? `CPU ${state.workerCpu?.toFixed(2) ?? "?"} / 0.50` : kind === "worker_down" ? "DOWN" : null, kind === "worker_down");
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
        const slot = X.proxyR + 10 + (request.id % 4) * 12;
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
    const node = el("circle", { r: 5, class: "packet", fill: COLORS.ok }, this.packetLayer);
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
