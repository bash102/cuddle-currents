// Canvas glyph shapes — the second identity channel alongside color.
// Keep this list in sync with _SHAPES in src/cuddle/hub/enrollment.py.

function polygon(ctx, x, y, r, n, rot) {
  for (let i = 0; i < n; i++) {
    const a = rot + (i * 2 * Math.PI) / n;
    const px = x + r * Math.cos(a);
    const py = y + r * Math.sin(a);
    i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
  }
  ctx.closePath();
}

function starPath(ctx, x, y, r, pts) {
  const inner = r * 0.45;
  for (let i = 0; i < pts * 2; i++) {
    const rr = i % 2 ? inner : r;
    const a = -Math.PI / 2 + (i * Math.PI) / pts;
    const px = x + rr * Math.cos(a);
    const py = y + rr * Math.sin(a);
    i ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
  }
  ctx.closePath();
}

function plusPath(ctx, x, y, r) {
  const t = r * 0.4; // arm half-thickness
  const pts = [
    [-t, -r], [t, -r], [t, -t], [r, -t], [r, t], [t, t],
    [t, r], [-t, r], [-t, t], [-r, t], [-r, -t], [-t, -t],
  ];
  pts.forEach(([dx, dy], i) =>
    i ? ctx.lineTo(x + dx, y + dy) : ctx.moveTo(x + dx, y + dy));
  ctx.closePath();
}

export function tracePath(ctx, shape, x, y, r) {
  ctx.beginPath();
  switch (shape) {
    case "triangle": polygon(ctx, x, y, r, 3, -Math.PI / 2); break;
    case "square": polygon(ctx, x, y, r * 0.92, 4, Math.PI / 4); break;
    case "diamond": polygon(ctx, x, y, r, 4, -Math.PI / 2); break;
    case "hexagon": polygon(ctx, x, y, r, 6, -Math.PI / 2); break;
    case "star": starPath(ctx, x, y, r, 5); break;
    case "plus": plusPath(ctx, x, y, r * 0.95); break;
    case "ring": // hollow — stroked by the caller
    case "disc":
    default: ctx.arc(x, y, r, 0, 2 * Math.PI);
  }
}

const STROKE_SHAPES = new Set(["ring"]);

export function drawGlyph(ctx, shape, x, y, r, color, { glow = 0, alpha = 1 } = {}) {
  ctx.save();
  ctx.globalAlpha = alpha;
  if (glow) {
    ctx.shadowColor = color;
    ctx.shadowBlur = glow;
  }
  tracePath(ctx, shape, x, y, r);
  if (STROKE_SHAPES.has(shape)) {
    ctx.lineWidth = Math.max(2, r * 0.3);
    ctx.strokeStyle = color;
    ctx.stroke();
  } else {
    ctx.fillStyle = color;
    ctx.fill();
  }
  ctx.restore();
}

export function initials(name) {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}
