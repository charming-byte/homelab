// TRIAL: animated background in the style of soulextract.com, not part of the theme yet.
// Grid adapted from GridLines in @arwes/bgs (MIT, github.com/arwes/arwes); traces and pulses follow
// soulextract's circuit lines. Colors come from the theme tokens.
(() => {
  const root = getComputedStyle(document.documentElement);
  const token = (name) => root.getPropertyValue(name).trim();
  const COLORS = {
    grid: token("--sw-line"),
    pulse: token("--sw-accent"),
  };
  const GRID = { distance: 40, alpha: 0.15, dash: [2, 8] };
  // Traces route on a finer grid than the visible one, with 45° bends like a real board.
  const BOARD = { cell: 20, density: 1 / 12000, traceWidth: 1.5, traceAlpha: 0.16, padRadius: 3.5, viaRadius: 2.5, viaDensity: 1 / 40000 };
  const PULSE = { length: 36, speed: 50, width: 2, concurrent: [2, 5], pause: [400, 1600] };
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

  const layer = document.createElement("div");
  layer.setAttribute("aria-hidden", "true");
  // z-index -1 keeps the layer above the root background but below Homepage's unpositioned content.
  Object.assign(layer.style, {
    position: "fixed",
    inset: "0",
    zIndex: "-1",
    pointerEvents: "none",
    opacity: "0.8",
    filter: "brightness(0.7)",
    backgroundImage: `radial-gradient(color-mix(in srgb, ${COLORS.pulse} 5%, transparent) 25%, transparent)`,
  });
  const board = document.createElement("canvas");
  const pulses = document.createElement("canvas");
  // The board fades out toward the middle, where the cards sit, and stays at full strength along the edges.
  const fade = "radial-gradient(ellipse 60% 70% at center, transparent 20%, black)";
  for (const canvas of [board, pulses]) {
    Object.assign(canvas.style, { position: "absolute", inset: "0", width: "100%", height: "100%", maskImage: fade });
    layer.append(canvas);
  }

  // What still shows through the translucent cards gets blurred harder and darkened.
  const style = document.createElement("style");
  style.textContent = ":root .service-card { backdrop-filter: blur(20px) brightness(0.75); }";
  document.head.append(style);
  document.body.prepend(layer);
  // Clear the body background so it cannot cover the circuit layer's negative z-index.
  document.body.style.background = "transparent";

  let width = 0;
  let height = 0;
  let traces = [];

  function fit(canvas) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  const random = (min, max) => min + (max - min) * Math.random();
  const randomInt = (min, max) => Math.floor(random(min, max + 1));
  const pick = (list) => list[Math.floor(Math.random() * list.length)];

  // Eight directions, clockwise from east; a 45° turn is ±1.
  const DIRECTIONS = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]];

  function route(occupied, columns, rows) {
    const free = (x, y) => x >= 0 && y >= 0 && x < columns && y < rows && !occupied.has(`${x},${y}`);
    const startX = randomInt(0, columns - 1);
    const startY = randomInt(0, rows - 1);
    if (!free(startX, startY)) return null;

    // Mostly straight runs: start orthogonal, bend rarely.
    let direction = pick([0, 2, 4, 6]);
    let x = startX;
    let y = startY;
    const cells = [[x, y]];
    const steps = randomInt(6, 40);
    for (let i = 0; i < steps; i++) {
      if (cells.length > 2 && Math.random() < 0.12) direction = (direction + pick([1, 7])) % 8;
      const [dx, dy] = DIRECTIONS[direction];
      const nx = x + dx;
      const ny = y + dy;
      // A diagonal step must not cut through a trace running across the corner.
      const crosses = dx !== 0 && dy !== 0 && (!free(x + dx, y) && !free(x, y + dy));
      if (!free(nx, ny) || crosses) break;
      x = nx;
      y = ny;
      cells.push([x, y]);
    }
    if (cells.length < 5) return null;
    return cells;
  }

  // Only the corners matter for drawing; collinear cells collapse into one segment.
  function toPolyline(cells) {
    const { cell } = BOARD;
    const points = cells.map(([x, y]) => [x * cell + cell / 2, y * cell + cell / 2]);
    const corners = [points[0]];
    for (let i = 1; i < points.length - 1; i++) {
      const [ax, ay] = corners[corners.length - 1];
      const [bx, by] = points[i];
      const [cx, cy] = points[i + 1];
      if ((bx - ax) * (cy - by) !== (by - ay) * (cx - bx)) corners.push(points[i]);
    }
    corners.push(points[points.length - 1]);
    let length = 0;
    for (let i = 1; i < corners.length; i++) {
      length += Math.hypot(corners[i][0] - corners[i - 1][0], corners[i][1] - corners[i - 1][1]);
    }
    return { points: corners, length };
  }

  function layout() {
    const { cell, density } = BOARD;
    const columns = Math.ceil(width / cell);
    const rows = Math.ceil(height / cell);
    const occupied = new Set();
    const result = [];
    const wanted = Math.round(width * height * density);
    for (let attempt = 0; result.length < wanted && attempt < wanted * 20; attempt++) {
      const cells = route(occupied, columns, rows);
      if (!cells) continue;
      // Reserve the neighbors as well, so traces keep a cell of clearance like on a board.
      for (const [x, y] of cells) {
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) occupied.add(`${x + dx},${y + dy}`);
      }
      result.push(toPolyline(cells));
    }
    return result;
  }

  function tracePath(ctx, { points }) {
    ctx.beginPath();
    ctx.moveTo(points[0][0], points[0][1]);
    for (let i = 1; i < points.length; i++) ctx.lineTo(points[i][0], points[i][1]);
  }

  function pad(ctx, [x, y], radius) {
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.stroke();
  }

  function drawBoard() {
    const ctx = fit(board);

    ctx.strokeStyle = COLORS.grid;
    ctx.globalAlpha = GRID.alpha;
    ctx.lineWidth = 1;
    ctx.setLineDash(GRID.dash);
    for (let y = (height % GRID.distance) / 2; y <= height; y += GRID.distance) {
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }
    for (let x = (width % GRID.distance) / 2; x <= width; x += GRID.distance) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    const { traceWidth, traceAlpha, padRadius, viaRadius, viaDensity } = BOARD;
    ctx.globalAlpha = traceAlpha;
    ctx.strokeStyle = COLORS.pulse;
    ctx.lineWidth = traceWidth;
    ctx.lineJoin = "round";
    for (const trace of traces) {
      tracePath(ctx, trace);
      ctx.stroke();
      pad(ctx, trace.points[0], padRadius);
      pad(ctx, trace.points[trace.points.length - 1], padRadius);
    }

    // Loose vias between the traces.
    for (let i = 0; i < width * height * viaDensity; i++) {
      pad(ctx, [Math.round(random(0, width / BOARD.cell)) * BOARD.cell, Math.round(random(0, height / BOARD.cell)) * BOARD.cell], viaRadius);
    }


  }

  let pulseCtx;
  let active = [];

  function spawn(now) {
    const trace = pick(traces);
    if (!trace) return;
    // Circuit lines carry slow light pulses in both directions.
    active.push({ trace, start: now, duration: (trace.length / PULSE.speed) * 1000, reverse: Math.random() < 0.5 });
  }

  function drawPulses(now) {
    const ctx = pulseCtx;
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = COLORS.pulse;
    ctx.fillStyle = COLORS.pulse;
    ctx.lineWidth = PULSE.width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.shadowColor = COLORS.pulse;
    ctx.shadowBlur = 8;

    active = active.filter((pulse) => {
      const progress = (now - pulse.start) / pulse.duration;
      if (progress >= 1.25) return false;
      const { trace } = pulse;
      const head = Math.min(progress, 1) * (trace.length + PULSE.length);
      const offset = pulse.reverse ? trace.length - head : head - PULSE.length;
      // Dash trick: one visible dash of the pulse length, moved along the trace by the offset.
      ctx.setLineDash([PULSE.length, trace.length + PULSE.length]);
      ctx.lineDashOffset = -offset;
      ctx.globalAlpha = 1;
      tracePath(ctx, trace);
      ctx.stroke();

      // The pad at the far end lights up as the pulse arrives, then fades.
      if (progress > 0.9) {
        const end = pulse.reverse ? trace.points[0] : trace.points[trace.points.length - 1];
        ctx.setLineDash([]);
        ctx.globalAlpha = Math.max(0, 1 - (progress - 0.9) / 0.35);
        ctx.beginPath();
        ctx.arc(end[0], end[1], BOARD.padRadius, 0, Math.PI * 2);
        ctx.fill();
      }
      return true;
    });

    if (active.length < PULSE.concurrent[0] || (active.length < PULSE.concurrent[1] && now >= nextSpawn)) {
      spawn(now);
      nextSpawn = now + random(...PULSE.pause);
    }
  }

  let nextSpawn = 0;

  function resize() {
    width = innerWidth;
    height = innerHeight;
    traces = layout();
    drawBoard();
    pulseCtx = fit(pulses);
    active = [];
  }

  let last = 0;
  function frame(now) {
    if (reducedMotion.matches) return;
    // 30 fps is plenty for the pulses and halves the work of an always-open dashboard.
    if (now - last >= 33) {
      last = now;
      drawPulses(now);
    }
    requestAnimationFrame(frame);
  }

  resize();
  addEventListener("resize", resize);
  reducedMotion.addEventListener("change", () => {
    if (reducedMotion.matches) pulseCtx.clearRect(0, 0, width, height);
    else requestAnimationFrame(frame);
  });
  requestAnimationFrame(frame);
})();
