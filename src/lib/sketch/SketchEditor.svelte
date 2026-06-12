<script lang="ts">
  /*
   * SketchEditor — a lightweight 2D technical sketcher for cad-studio.
   *
   * Geometry is stored in millimetres, world Y up (CAD convention). The SVG is
   * rendered from a single buildSvg() pass for BOTH the live view (current
   * pan/zoom) and the PNG export (fit-to-content) — pointer hit-testing is done
   * in JS against world-space entities, so we don't bind per-element handlers.
   *
   * No constraint solver (Phase 1): the geometry is what you draw and the typed
   * dimensions are the source of truth. Entity types line/circle/arc map 1:1 on
   * FreeCAD/planegcs primitives so a solver can be bolted on later.
   */
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { getStroke } from 'perfect-freehand';

  export let apiBase = '';
  export let partId: string;
  export let sketchId: number | null = null;   // null → new sketch
  export let label = '';

  const dispatch = createEventDispatcher();

  type Pt = [number, number];
  type Entity =
    | { id: string; type: 'line'; p1: Pt; p2: Pt }
    | { id: string; type: 'rect'; p0: Pt; p1: Pt }
    | { id: string; type: 'circle'; c: Pt; r: number }
    | { id: string; type: 'arc'; c: Pt; r: number; a0: number; a1: number }
    | { id: string; type: 'bezier'; pts: Pt[] }
    | { id: string; type: 'ink'; d: string; color: string };
  type Dimension = { id: string; kind: 'linear' | 'diameter'; refs: string[]; a?: Pt; b?: Pt; value: number; label?: string };
  type Label = { at: Pt; text: string };
  type Tool = 'select' | 'pan' | 'line' | 'rect' | 'circle' | 'arc' | 'bezier' | 'dim' | 'diam' | 'text' | 'ink';

  let entities: Entity[] = [];
  let dimensions: Dimension[] = [];
  let labels: Label[] = [];

  let tool: Tool = 'select';
  let inkColor = '#111';
  let snapGrid = true;
  let snapStepMm = 1;         // grid snap resolution (mm)
  const SNAP_STEPS = [0.5, 1, 2, 5, 10];
  // grid spacing adapts to zoom so it stays a readable reference (~45px lines)
  $: gridStepMm = niceStep(45 / scale);
  let title = label || '';
  let note = '';
  let dirty = false;
  let saving = false;

  // view transform: screen = (ox + wx*scale, oy - wy*scale)
  let scale = 3;              // px per mm
  let ox = 60, oy = 0;        // set in onMount once we know height
  let w = 800, h = 600;

  let svgEl: HTMLElement;
  let ro: ResizeObserver | null = null;

  // interaction
  let pending: Pt[] = [];     // points collected for a multi-click tool
  let cursor: Pt | null = null;
  let panning = false;
  let panStart = { x: 0, y: 0, ox: 0, oy: 0 };
  let inkPts: { x: number; y: number; pressure: number }[] = [];
  let selected = -1;          // entity index
  let dragStart: Pt | null = null;
  let dragOrig: Entity | null = null;

  // inline value prompt
  let ask: { x: number; y: number; placeholder: string; resolve: (v: string | null) => void } | null = null;
  let askValue = '';

  let uid = 1;
  const nid = (p: string) => `${p}${uid++}`;

  // ---- coordinate helpers ---------------------------------------------
  const sx = (wx: number) => ox + wx * scale;
  const sy = (wy: number) => oy - wy * scale;
  const wx = (px: number) => (px - ox) / scale;
  const wy = (py: number) => (oy - py) / scale;

  function clientToWorld(e: { clientX: number; clientY: number }): Pt {
    const r = svgEl.getBoundingClientRect();
    return [wx(e.clientX - r.left), wy(e.clientY - r.top)];
  }

  function snap(p: Pt): Pt {
    // snap to existing key points first (centres, endpoints)
    const thr = 8 / scale;
    let best: Pt | null = null, bd = thr;
    for (const kp of keyPoints()) {
      const d = Math.hypot(kp[0] - p[0], kp[1] - p[1]);
      if (d < bd) { bd = d; best = kp; }
    }
    if (best) return best;
    if (snapGrid) return [Math.round(p[0] / snapStepMm) * snapStepMm, Math.round(p[1] / snapStepMm) * snapStepMm];
    return p;
  }

  function keyPoints(): Pt[] {
    const out: Pt[] = [];
    for (const e of entities) {
      if (e.type === 'line') { out.push(e.p1, e.p2); }
      else if (e.type === 'rect') { out.push(e.p0, e.p1, [e.p0[0], e.p1[1]], [e.p1[0], e.p0[1]]); }
      else if (e.type === 'circle' || e.type === 'arc') { out.push(e.c); }
      else if (e.type === 'bezier') { out.push(...e.pts); }
    }
    return out;
  }

  // ---- tool actions ----------------------------------------------------
  const TOOL_CLICKS: Record<string, number> = { line: 2, rect: 2, circle: 2, arc: 3, bezier: 4, dim: 2, diam: 1, text: 1 };

  function onDown(e: PointerEvent) {
    if (ask) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    const wp = clientToWorld(e);

    if (tool === 'pan' || e.button === 1 || e.button === 2) {
      panning = true; panStart = { x: e.clientX, y: e.clientY, ox, oy }; return;
    }
    if (tool === 'ink') {
      inkPts = [{ x: wp[0], y: wp[1], pressure: e.pressure || 0.5 }]; return;
    }
    if (tool === 'select') {
      selected = hitTest(wp);
      if (selected >= 0) { dragStart = wp; dragOrig = JSON.parse(JSON.stringify(entities[selected])); }
      return;
    }
    // geometry / annotation tools collect snapped points
    const sp = snap(wp);
    pending = [...pending, sp];
    const need = TOOL_CLICKS[tool] ?? 0;
    if (pending.length >= need) commitTool();
  }

  function onMove(e: PointerEvent) {
    const wp = clientToWorld(e);
    cursor = snap(wp);
    if (panning) {
      ox = panStart.ox + (e.clientX - panStart.x);
      oy = panStart.oy + (e.clientY - panStart.y);
      bump(); return;
    }
    if (tool === 'ink' && inkPts.length) {
      inkPts = [...inkPts, { x: wp[0], y: wp[1], pressure: e.pressure || 0.5 }]; bump(); return;
    }
    if (tool === 'select' && selected >= 0 && dragStart && dragOrig) {
      const dx = wp[0] - dragStart[0], dy = wp[1] - dragStart[1];
      entities[selected] = translate(dragOrig, dx, dy);
      entities = entities; dirty = true; bump(); return;
    }
    bump();
  }

  function onUp(e: PointerEvent) {
    if (panning) { panning = false; return; }
    if (tool === 'ink' && inkPts.length > 1) {
      const d = strokePath(inkPts);
      entities = [...entities, { id: nid('e'), type: 'ink', d, color: inkColor }];
      inkPts = []; dirty = true;
    } else { inkPts = []; }
    if (tool === 'select') { dragStart = null; dragOrig = null; }
  }

  async function commitTool() {
    const p = pending; pending = [];
    if (tool === 'line') {
      entities = [...entities, { id: nid('e'), type: 'line', p1: p[0], p2: p[1] }];
    } else if (tool === 'rect') {
      entities = [...entities, { id: nid('e'), type: 'rect', p0: p[0], p1: p[1] }];
    } else if (tool === 'circle') {
      const r = Math.hypot(p[1][0] - p[0][0], p[1][1] - p[0][1]);
      entities = [...entities, { id: nid('e'), type: 'circle', c: p[0], r }];
    } else if (tool === 'arc') {
      const c = p[0];
      const r = Math.hypot(p[1][0] - c[0], p[1][1] - c[1]);
      const a0 = Math.atan2(p[1][1] - c[1], p[1][0] - c[0]) * 180 / Math.PI;
      const a1 = Math.atan2(p[2][1] - c[1], p[2][0] - c[0]) * 180 / Math.PI;
      entities = [...entities, { id: nid('e'), type: 'arc', c, r, a0, a1 }];
    } else if (tool === 'bezier') {
      entities = [...entities, { id: nid('e'), type: 'bezier', pts: p }];
    } else if (tool === 'text') {
      const txt = await prompt2(p[0], 'texte');
      if (txt) labels = [...labels, { at: p[0], text: txt }];
    } else if (tool === 'dim') {
      const len = Math.hypot(p[1][0] - p[0][0], p[1][1] - p[0][1]);
      const v = await prompt2(mid(p[0], p[1]), `longueur mm (${len.toFixed(1)})`);
      const val = v ? parseFloat(v) : len;
      dimensions = [...dimensions, { id: nid('d'), kind: 'linear', refs: [], a: p[0], b: p[1], value: round1(val) }];
    } else if (tool === 'diam') {
      const idx = hitTest(p[0]);
      const ent = idx >= 0 ? entities[idx] : null;
      if (ent && ent.type === 'circle') {
        const v = await prompt2(ent.c, `Ø mm (${(2 * ent.r).toFixed(1)})`);
        const val = v ? parseFloat(v) : 2 * ent.r;
        dimensions = [...dimensions, { id: nid('d'), kind: 'diameter', refs: [ent.id], value: round1(val) }];
      } else {
        flash('Clique sur un cercle pour le coter en Ø.');
      }
    }
    dirty = true;
  }

  function translate(e: Entity, dx: number, dy: number): Entity {
    const t = (p: Pt): Pt => [p[0] + dx, p[1] + dy];
    if (e.type === 'line') return { ...e, p1: t(e.p1), p2: t(e.p2) };
    if (e.type === 'rect') return { ...e, p0: t(e.p0), p1: t(e.p1) };
    if (e.type === 'circle' || e.type === 'arc') return { ...e, c: t(e.c) };
    if (e.type === 'bezier') return { ...e, pts: e.pts.map(t) };
    return e; // ink not movable
  }

  function hitTest(p: Pt): number {
    const thr = 6 / scale;
    for (let i = entities.length - 1; i >= 0; i--) {
      const e = entities[i];
      if (e.type === 'circle') { if (Math.abs(Math.hypot(p[0] - e.c[0], p[1] - e.c[1]) - e.r) < thr) return i; }
      else if (e.type === 'arc') { if (Math.abs(Math.hypot(p[0] - e.c[0], p[1] - e.c[1]) - e.r) < thr) return i; }
      else if (e.type === 'line') { if (distToSeg(p, e.p1, e.p2) < thr) return i; }
      else if (e.type === 'rect') {
        const c = [[e.p0, [e.p1[0], e.p0[1]]], [[e.p1[0], e.p0[1]], e.p1], [e.p1, [e.p0[0], e.p1[1]]], [[e.p0[0], e.p1[1]], e.p0]] as [Pt, Pt][];
        if (c.some(([a, b]) => distToSeg(p, a, b) < thr)) return i;
      } else if (e.type === 'bezier') { if (e.pts.some((q) => Math.hypot(p[0] - q[0], p[1] - q[1]) < thr * 2)) return i; }
    }
    return -1;
  }

  function distToSeg(p: Pt, a: Pt, b: Pt): number {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const l2 = dx * dx + dy * dy;
    if (l2 === 0) return Math.hypot(p[0] - a[0], p[1] - a[1]);
    let t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
  }

  const mid = (a: Pt, b: Pt): Pt => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const round1 = (n: number) => Math.round(n * 10) / 10;
  const f1 = (n: number) => (Math.round(n * 10) / 10).toString();

  // round a mm value to a 1/2/5·10^k "nice" step (grid + scale bar)
  function niceStep(mm: number): number {
    if (!isFinite(mm) || mm <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(mm)));
    const r = mm / p;
    return (r < 1.5 ? 1 : r < 3.5 ? 2 : r < 7.5 ? 5 : 10) * p;
  }

  // live measurement of the segment being drawn (cursor vs pending points)
  function liveMeasure(): string {
    if (!cursor) return '';
    if ((tool === 'line' || tool === 'dim') && pending.length === 1) {
      const dx = cursor[0] - pending[0][0], dy = cursor[1] - pending[0][1];
      return `${f1(Math.hypot(dx, dy))} mm`;
    }
    if (tool === 'rect' && pending.length === 1)
      return `${f1(Math.abs(cursor[0] - pending[0][0]))} × ${f1(Math.abs(cursor[1] - pending[0][1]))} mm`;
    if (tool === 'circle' && pending.length === 1) {
      const r = Math.hypot(cursor[0] - pending[0][0], cursor[1] - pending[0][1]);
      return `r ${f1(r)} · Ø${f1(2 * r)} mm`;
    }
    if (tool === 'arc' && pending.length >= 1)
      return `r ${f1(Math.hypot(cursor[0] - pending[0][0], cursor[1] - pending[0][1]))} mm`;
    return '';
  }

  function strokePath(pts: { x: number; y: number; pressure: number }[]): string {
    // store the ink outline in WORLD coords as an SVG path
    const out = getStroke(pts.map((p) => [p.x, p.y, p.pressure]), { size: 1.5, thinning: 0.6, smoothing: 0.5, streamline: 0.5, simulatePressure: true }) as number[][];
    if (!out.length) return '';
    return 'M' + out.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' L') + 'Z';
  }

  // ---- prompt helper ---------------------------------------------------
  function prompt2(at: Pt, placeholder: string): Promise<string | null> {
    askValue = '';
    return new Promise((resolve) => {
      ask = { x: sx(at[0]), y: sy(at[1]), placeholder, resolve };
    });
  }
  function askOk() { const a = ask; ask = null; a?.resolve(askValue || null); }
  function askCancel() { const a = ask; ask = null; a?.resolve(null); }

  // ---- misc ------------------------------------------------------------
  function undo() {
    if (entities.length) { entities = entities.slice(0, -1); dirty = true; }
  }
  function delSelected() {
    if (selected >= 0) { entities = entities.filter((_, i) => i !== selected); selected = -1; dirty = true; }
  }
  function onWheel(e: WheelEvent) {
    e.preventDefault();
    const r = svgEl.getBoundingClientRect();
    const mxw = wx(e.clientX - r.left), myw = wy(e.clientY - r.top);
    const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    scale = Math.max(0.3, Math.min(60, scale * f));
    // keep cursor anchored
    ox = (e.clientX - r.left) - mxw * scale;
    oy = (e.clientY - r.top) + myw * scale;
    bump();
  }
  function onKey(e: KeyboardEvent) {
    if (ask) { if (e.key === 'Enter') askOk(); if (e.key === 'Escape') askCancel(); return; }
    if (e.key === 'Escape') { pending = []; selected = -1; bump(); }
    else if (e.key === 'Delete' || e.key === 'Backspace') delSelected();
    else if ((e.key === 'z' && (e.ctrlKey || e.metaKey))) undo();
  }
  let flashMsg = '';
  let flashT: any;
  function flash(m: string) { flashMsg = m; clearTimeout(flashT); flashT = setTimeout(() => (flashMsg = ''), 2200); }

  // force re-render of the {@html} svg
  let tick = 0;
  function bump() { tick++; }
  $: svgInner = (tick, buildSvg({ scale, ox, oy }, w, h, true));

  function bbox(): [number, number, number, number] | null {
    let xs: number[] = [], ys: number[] = [];
    const add = (p: Pt) => { xs.push(p[0]); ys.push(p[1]); };
    for (const e of entities) {
      if (e.type === 'line') { add(e.p1); add(e.p2); }
      else if (e.type === 'rect') { add(e.p0); add(e.p1); }
      else if (e.type === 'circle' || e.type === 'arc') { add([e.c[0] - e.r, e.c[1] - e.r]); add([e.c[0] + e.r, e.c[1] + e.r]); }
      else if (e.type === 'bezier') e.pts.forEach(add);
    }
    if (!xs.length) return null;
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }

  // ---- SVG builder (live + export share this) --------------------------
  function buildSvg(tf: { scale: number; ox: number; oy: number }, W: number, H: number, live: boolean): string {
    const SX = (x: number) => tf.ox + x * tf.scale;
    const SY = (y: number) => tf.oy - y * tf.scale;
    let s = '';

    // grid + numbered axes (graph-paper scale)
    if (live) {
      const x0 = wx(0), x1 = wx(W), y0 = wy(H), y1 = wy(0);
      const gx0 = Math.floor(x0 / gridStepMm) * gridStepMm, gx1 = Math.ceil(x1 / gridStepMm) * gridStepMm;
      const gy0 = Math.floor(y0 / gridStepMm) * gridStepMm, gy1 = Math.ceil(y1 / gridStepMm) * gridStepMm;
      const axisY = Math.max(11, Math.min(H - 3, SY(0)));   // X-labels ride the x-axis (clamped on screen)
      const axisX = Math.max(2, Math.min(W - 22, SX(0)));   // Y-labels ride the y-axis
      const lab = (x: number, y: number, t: string) =>
        `<text x="${x}" y="${y}" font-family="ui-monospace,monospace" font-size="10" fill="#5b6b7a">${t}</text>`;
      if ((gx1 - gx0) / gridStepMm < 500) {
        for (let x = gx0; x <= gx1; x += gridStepMm) {
          const px = SX(x);
          s += `<line x1="${px}" y1="0" x2="${px}" y2="${H}" stroke="${x === 0 ? '#7c8a99' : '#dde4ec'}" stroke-width="${x === 0 ? 1.5 : 1}"/>`;
          if (x !== 0) s += lab(px + 2, axisY - 3, `${+x.toFixed(2)}`);
        }
        for (let y = gy0; y <= gy1; y += gridStepMm) {
          const py = SY(y);
          s += `<line x1="0" y1="${py}" x2="${W}" y2="${py}" stroke="${y === 0 ? '#7c8a99' : '#dde4ec'}" stroke-width="${y === 0 ? 1.5 : 1}"/>`;
          if (y !== 0) s += lab(axisX + 2, py - 2, `${+y.toFixed(2)}`);
        }
        s += lab(SX(0) + 2, SY(0) - 2, '0');
      }
    }

    // entities
    entities.forEach((e, i) => {
      const sel = live && i === selected ? ' stroke="#0a84ff" stroke-width="2.5"' : ' stroke="#16202c" stroke-width="1.6"';
      if (e.type === 'line') s += `<line x1="${SX(e.p1[0])}" y1="${SY(e.p1[1])}" x2="${SX(e.p2[0])}" y2="${SY(e.p2[1])}" fill="none"${sel}/>`;
      else if (e.type === 'rect') s += `<rect x="${Math.min(SX(e.p0[0]), SX(e.p1[0]))}" y="${Math.min(SY(e.p0[1]), SY(e.p1[1]))}" width="${Math.abs((e.p1[0] - e.p0[0]) * tf.scale)}" height="${Math.abs((e.p1[1] - e.p0[1]) * tf.scale)}" fill="none"${sel}/>`;
      else if (e.type === 'circle') s += `<circle cx="${SX(e.c[0])}" cy="${SY(e.c[1])}" r="${e.r * tf.scale}" fill="none"${sel}/>`;
      else if (e.type === 'arc') s += `<path d="${arcPath(e, SX, SY, tf.scale)}" fill="none"${sel}/>`;
      else if (e.type === 'bezier' && e.pts.length >= 2) {
        const P = e.pts.map((p) => `${SX(p[0])},${SY(p[1])}`);
        const d = e.pts.length >= 4 ? `M${P[0]} C${P[1]} ${P[2]} ${P[3]}` : `M${P[0]} L${P.slice(1).join(' L')}`;
        s += `<path d="${d}" fill="none"${sel}/>`;
      } else if (e.type === 'ink') {
        const d = worldPathToScreen(e.d, SX, SY);
        s += `<path d="${d}" fill="${e.color}" stroke="none"/>`;
      }
    });

    // dimensions
    dimensions.forEach((d) => {
      if (d.kind === 'linear' && d.a && d.b) {
        const m = mid(d.a, d.b);
        s += `<line x1="${SX(d.a[0])}" y1="${SY(d.a[1])}" x2="${SX(d.b[0])}" y2="${SY(d.b[1])}" stroke="#c0392b" stroke-width="1" stroke-dasharray="4 3"/>`;
        s += dimText(SX(m[0]), SY(m[1]), `${d.value}${d.label ? ' ' + d.label : ''}`);
      } else if (d.kind === 'diameter') {
        const c = entities.find((e) => e.id === d.refs[0]);
        if (c && c.type === 'circle') s += dimText(SX(c.c[0]), SY(c.c[1]), `Ø${d.value}`);
      }
    });

    // labels
    labels.forEach((l) => { s += dimText(SX(l.at[0]), SY(l.at[1]), l.text, '#2c3e50'); });

    // pending preview
    if (live && pending.length) {
      const all = cursor ? [...pending, cursor] : pending;
      for (let i = 1; i < all.length; i++) s += `<line x1="${SX(all[i - 1][0])}" y1="${SY(all[i - 1][1])}" x2="${SX(all[i][0])}" y2="${SY(all[i][1])}" stroke="#0a84ff" stroke-width="1" stroke-dasharray="3 3"/>`;
      all.forEach((p) => s += `<circle cx="${SX(p[0])}" cy="${SY(p[1])}" r="3" fill="#0a84ff"/>`);
    }
    // live ink preview
    if (live && inkPts.length > 1) {
      const d = worldPathToScreen(strokePath(inkPts), SX, SY);
      s += `<path d="${d}" fill="${inkColor}" stroke="none" opacity="0.8"/>`;
    }
    // cursor crosshair + live measurement of the segment being drawn
    if (live && cursor && tool !== 'pan' && tool !== 'select') {
      const cx = SX(cursor[0]), cy = SY(cursor[1]);
      s += `<line x1="${cx - 8}" y1="${cy}" x2="${cx + 8}" y2="${cy}" stroke="#0a84ff" stroke-width="1"/>`;
      s += `<line x1="${cx}" y1="${cy - 8}" x2="${cx}" y2="${cy + 8}" stroke="#0a84ff" stroke-width="1"/>`;
      const m = liveMeasure();
      if (m) s += dimText(cx + 4 + m.length * 3.5, cy - 16, m, '#0a84ff');
    }

    // scale bar (screen-anchored, bottom-right) — a round mm length at this zoom
    if (live) {
      const Lmm = niceStep(90 / tf.scale);
      const Lpx = Lmm * tf.scale;
      const bx = W - 24 - Lpx, by = H - 24;
      s += `<g stroke="#33404d" stroke-width="2">`;
      s += `<line x1="${bx}" y1="${by}" x2="${bx + Lpx}" y2="${by}"/>`;
      s += `<line x1="${bx}" y1="${by - 4}" x2="${bx}" y2="${by + 4}"/>`;
      s += `<line x1="${bx + Lpx}" y1="${by - 4}" x2="${bx + Lpx}" y2="${by + 4}"/></g>`;
      s += `<text x="${bx + Lpx / 2}" y="${by - 6}" font-family="ui-sans-serif,system-ui" font-size="11" fill="#33404d" text-anchor="middle">${Lmm} mm</text>`;
    }
    return s;
  }

  function arcPath(e: { c: Pt; r: number; a0: number; a1: number }, SX: any, SY: any, sc: number): string {
    const r0 = e.a0 * Math.PI / 180, r1 = e.a1 * Math.PI / 180;
    const x0 = e.c[0] + e.r * Math.cos(r0), y0 = e.c[1] + e.r * Math.sin(r0);
    const x1 = e.c[0] + e.r * Math.cos(r1), y1 = e.c[1] + e.r * Math.sin(r1);
    let da = e.a1 - e.a0; while (da < 0) da += 360; while (da > 360) da -= 360;
    const large = da > 180 ? 1 : 0;
    // world Y up → screen Y down flips sweep direction
    return `M${SX(x0)},${SY(y0)} A${e.r * sc},${e.r * sc} 0 ${large} 0 ${SX(x1)},${SY(y1)}`;
  }

  function dimText(x: number, y: number, t: string, color = '#c0392b'): string {
    const wpx = t.length * 7 + 8;
    return `<g><rect x="${x - wpx / 2}" y="${y - 9}" width="${wpx}" height="16" rx="3" fill="#ffffffcc"/><text x="${x}" y="${y + 3}" font-family="ui-monospace,monospace" font-size="12" fill="${color}" text-anchor="middle">${escapeXml(t)}</text></g>`;
  }
  function escapeXml(s: string) { return s.replace(/[<>&"]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]!)); }

  function worldPathToScreen(d: string, SX: any, SY: any): string {
    // path stored as "Mx,y Lx,y ...Z" in world coords → screen
    return d.replace(/([ML])([-\d.]+),([-\d.]+)/g, (_, cmd, x, y) => `${cmd}${SX(parseFloat(x)).toFixed(2)},${SY(parseFloat(y)).toFixed(2)}`);
  }

  // ---- load / save -----------------------------------------------------
  function toDoc() {
    return {
      version: 1, units: 'mm',
      view: { px_per_mm: scale },
      entities, dimensions, labels,
    };
  }

  async function exportPng(): Promise<Blob | null> {
    const W = 800, H = 600;
    const bb = bbox();
    let tf: { scale: number; ox: number; oy: number };
    if (bb) {
      const [minx, miny, maxx, maxy] = bb;
      const pad = 30;
      const sc = Math.min((W - 2 * pad) / Math.max(1, maxx - minx), (H - 2 * pad) / Math.max(1, maxy - miny), 8);
      tf = { scale: sc, ox: W / 2 - ((minx + maxx) / 2) * sc, oy: H / 2 + ((miny + maxy) / 2) * sc };
    } else { tf = { scale, ox: ox, oy: oy }; }
    const inner = buildSvg(tf, W, H, false);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}"><rect width="${W}" height="${H}" fill="#fff"/>${inner}</svg>`;
    return await new Promise((resolve) => {
      const img = new Image();
      const url = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svg)));
      img.onload = () => {
        const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
        const c = cv.getContext('2d'); if (!c) return resolve(null);
        c.drawImage(img, 0, 0); cv.toBlob((b) => resolve(b), 'image/png');
      };
      img.onerror = () => resolve(null);
      img.src = url;
    });
  }

  async function save() {
    if (saving) return;
    saving = true;
    try {
      const png = await exportPng();
      const fd = new FormData();
      fd.append('part', partId);
      fd.append('doc', JSON.stringify(toDoc()));
      fd.append('label', title || 'croquis');
      fd.append('note', note);
      if (sketchId != null) fd.append('id', String(sketchId));
      if (png) fd.append('image', png, 'sketch.png');
      const r = await fetch(`${apiBase}/api/sketches`, { method: 'POST', body: fd });
      const j = await r.json();
      if (j.id != null) sketchId = j.id;
      dirty = false;
      dispatch('saved', { id: sketchId });
      flash('💾 Croquis enregistré');
    } catch {
      flash('⚠ échec de l’enregistrement');
    } finally { saving = false; }
  }

  async function loadExisting() {
    if (sketchId == null) return;
    try {
      const r = await fetch(`${apiBase}/api/sketches/doc?part=${encodeURIComponent(partId)}&id=${sketchId}`, { cache: 'no-store' });
      const j = await r.json();
      const d = j.doc || {};
      entities = d.entities || []; dimensions = d.dimensions || []; labels = d.labels || [];
      // bump uid past loaded ids
      const ids = [...entities, ...dimensions].map((x: any) => parseInt(String(x.id).replace(/\D/g, '')) || 0);
      uid = Math.max(0, ...ids) + 1;
      fitView();
    } catch { /* */ }
  }

  function fitView() {
    const bb = bbox();
    if (!bb) { ox = 60; oy = h - 60; return; }
    const [minx, miny, maxx, maxy] = bb; const pad = 40;
    scale = Math.max(0.5, Math.min(20, Math.min((w - 2 * pad) / Math.max(1, maxx - minx), (h - 2 * pad) / Math.max(1, maxy - miny))));
    ox = w / 2 - ((minx + maxx) / 2) * scale;
    oy = h / 2 + ((miny + maxy) / 2) * scale;
    bump();
  }

  function close() {
    if (dirty && !confirm('Quitter sans enregistrer ?')) return;
    dispatch('close');
  }

  onMount(() => {
    const r = svgEl.getBoundingClientRect(); w = r.width; h = r.height; oy = h - 60;
    ro = new ResizeObserver(() => { const b = svgEl.getBoundingClientRect(); w = b.width; h = b.height; bump(); });
    ro.observe(svgEl);
    window.addEventListener('keydown', onKey);
    loadExisting();
  });
  onDestroy(() => { ro?.disconnect(); window.removeEventListener('keydown', onKey); });

  const TOOLS: { id: Tool; icon: string; title: string }[] = [
    { id: 'select', icon: '➚', title: 'Sélectionner / déplacer' },
    { id: 'pan', icon: '✋', title: 'Déplacer la vue' },
    { id: 'line', icon: '╱', title: 'Ligne' },
    { id: 'rect', icon: '▭', title: 'Rectangle' },
    { id: 'circle', icon: '◯', title: 'Cercle' },
    { id: 'arc', icon: '◜', title: 'Arc (centre, début, fin)' },
    { id: 'bezier', icon: '∿', title: 'Courbe Bézier (4 points)' },
    { id: 'dim', icon: '↔', title: 'Cote linéaire' },
    { id: 'diam', icon: 'Ø', title: 'Cote diamètre (clique un cercle)' },
    { id: 'text', icon: 'T', title: 'Étiquette texte' },
    { id: 'ink', icon: '✎', title: 'Main levée' },
  ];
</script>

<div class="sk">
  <div class="sk-bar">
    <input class="sk-title" placeholder="titre du croquis" bind:value={title} on:input={() => (dirty = true)} />
    <div class="sk-tools">
      {#each TOOLS as t}
        <button class:sel={tool === t.id} title={t.title} on:click={() => { tool = t.id; pending = []; selected = -1; }}>{t.icon}</button>
      {/each}
    </div>
    <div class="sk-opts">
      <button class:sel={snapGrid} title="Aimanter à la grille" on:click={() => (snapGrid = !snapGrid)}>▦ snap</button>
      <button title="Pas d'aimantation (mm)" on:click={() => (snapStepMm = SNAP_STEPS[(SNAP_STEPS.indexOf(snapStepMm) + 1) % SNAP_STEPS.length])}>{snapStepMm} mm</button>
      <button title="Ajuster la vue" on:click={fitView}>⊡ fit</button>
      <button title="Annuler (⌘Z)" on:click={undo} disabled={!entities.length}>↶</button>
      <button title="Supprimer la sélection" on:click={delSelected} disabled={selected < 0}>🗑</button>
    </div>
    <span class="sk-spacer"></span>
    <button class="sk-save" on:click={save} disabled={saving}>{saving ? '…' : '💾 Enregistrer'}</button>
    <button class="sk-close" on:click={close} aria-label="fermer">✕</button>
  </div>

  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div
    class="sk-canvas"
    bind:this={svgEl}
    on:pointerdown={onDown}
    on:pointermove={onMove}
    on:pointerup={onUp}
    on:pointercancel={onUp}
    on:wheel={onWheel}
    on:contextmenu|preventDefault
    class:pan={tool === 'pan'}
  >
    <svg width={w} height={h}>{@html svgInner}</svg>

    {#if ask}
      <div class="sk-ask" style="left:{ask.x}px; top:{ask.y}px;">
        <!-- svelte-ignore a11y-autofocus -->
        <input autofocus placeholder={ask.placeholder} bind:value={askValue}
          on:keydown={(e) => { if (e.key === 'Enter') askOk(); if (e.key === 'Escape') askCancel(); }} />
        <button on:click={askOk}>✓</button>
        <button on:click={askCancel}>✕</button>
      </div>
    {/if}

    {#if flashMsg}<div class="sk-flash">{flashMsg}</div>{/if}
    {#if tool !== 'select' && tool !== 'pan' && pending.length}
      <div class="sk-hint">{pending.length}/{TOOL_CLICKS[tool]} points — Échap pour annuler</div>
    {/if}

    <div class="sk-readout">
      {#if cursor}<b>x</b> {f1(cursor[0])} · <b>y</b> {f1(cursor[1])} mm<span class="ro-sep">·</span>{/if}
      grille {gridStepMm} mm<span class="ro-sep">·</span>snap {snapGrid ? snapStepMm + ' mm' : 'off'}<span class="ro-sep">·</span>{f1(scale)} px/mm
    </div>
  </div>
</div>

<style>
  .sk { position: absolute; inset: 0; display: flex; flex-direction: column; background: #fafbfc; z-index: 50; }
  .sk-bar { display: flex; align-items: center; gap: 8px; padding: 6px 8px; background: #f1f3f5; border-bottom: 1px solid #dde1e6; flex-wrap: wrap; }
  .sk-title { font: 13px/1 ui-sans-serif, system-ui; padding: 5px 8px; border: 1px solid #cfd4da; border-radius: 6px; min-width: 140px; }
  .sk-tools, .sk-opts { display: flex; gap: 3px; }
  .sk-bar button { font-size: 14px; min-width: 30px; height: 30px; padding: 0 7px; border: 1px solid #cfd4da; background: #fff; border-radius: 6px; cursor: pointer; color: #1a2230; }
  .sk-bar button.sel { background: #0a84ff; border-color: #0a84ff; color: #fff; }
  .sk-bar button:disabled { opacity: 0.4; cursor: default; }
  .sk-spacer { flex: 1; }
  .sk-save { background: #16a34a !important; color: #fff !important; border-color: #16a34a !important; font-size: 13px !important; padding: 0 12px !important; }
  .sk-close { color: #64748b !important; }
  .sk-canvas { position: relative; flex: 1; overflow: hidden; touch-action: none; cursor: crosshair; background:
    radial-gradient(circle at 1px 1px, #e9edf2 1px, transparent 0); }
  .sk-canvas.pan { cursor: grab; }
  .sk-canvas svg { display: block; }
  .sk-ask { position: absolute; transform: translate(-50%, -130%); display: flex; gap: 3px; background: #fff; border: 1px solid #0a84ff; border-radius: 8px; padding: 4px; box-shadow: 0 4px 14px #0003; }
  .sk-ask input { width: 130px; font: 13px ui-monospace, monospace; border: 1px solid #cfd4da; border-radius: 5px; padding: 4px 6px; }
  .sk-ask button { width: 28px; height: 28px; border: 1px solid #cfd4da; background: #f6f8fa; border-radius: 5px; cursor: pointer; }
  .sk-flash { position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%); background: #1a2230ee; color: #fff; padding: 7px 14px; border-radius: 20px; font: 13px ui-sans-serif, system-ui; }
  .sk-hint { position: absolute; top: 10px; left: 50%; transform: translateX(-50%); background: #0a84ffee; color: #fff; padding: 5px 12px; border-radius: 16px; font: 12px ui-sans-serif, system-ui; }
  .sk-readout { position: absolute; bottom: 10px; left: 12px; background: #ffffffe6; border: 1px solid #dde1e6; color: #33404d; padding: 4px 10px; border-radius: 8px; font: 11px ui-monospace, monospace; pointer-events: none; box-shadow: 0 1px 4px #0001; }
  .sk-readout b { color: #0a84ff; font-weight: 600; }
  .ro-sep { color: #c0c8d0; margin: 0 6px; }
</style>
