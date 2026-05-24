<svelte:options customElement="cad-studio" />

<!--
  <cad-studio> — the tablet page for the build123d ⇄ Claude loop.

  One backend instance, many parts: a sidebar lists every registered part
  (/api/parts); tap one to switch instantly (per-part camera kept via
  <cad-viewer persistenceId>). Live model + WS auto-reload per part,
  Freeze & Draw at the stylus, paste a reference image, Send → /api/feedback.

  Pure UI/network shell — all 3D stays in <cad-viewer>, so the embeddable
  renderer used by vancad/vscode-openscad is unaffected.
-->
<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import './CADViewer.svelte'; // ensure <cad-viewer> is registered
  import { strokeToPath2D, pressureOf, type Stroke, type InkPoint } from './annotate/freehand.js';

  export let viewerBackgroundColor = '#1e1e1e';
  export let apiBase = ''; // same origin when served by the Python backend

  type Mode = 'nav' | 'draw';
  type Kind = 'annotation' | 'reference';
  interface PartInfo {
    part_id: string;
    loaded: boolean;
    building: boolean;
    build_ok: boolean;
    version: number;
    pending_feedback: number;
  }

  let host: HTMLElement;
  let viewerEl: any;
  let overlay: HTMLCanvasElement;
  let ctx: CanvasRenderingContext2D | null = null;

  let mode: Mode = 'nav';
  let kind: Kind = 'annotation';
  let strokes: Stroke[] = [];
  let current: InkPoint[] = [];
  let inkColor = '#ff3b30';
  let note = '';
  let pickedNode: string | null = null;
  let bg: HTMLImageElement | null = null;

  let wsState: 'connecting' | 'live' | 'down' = 'connecting';
  let version = 0;
  let toast = '';
  let sending = false;

  // ---- multi-part state ----
  let parts: PartInfo[] = [];
  let currentPartId: string | null = null;
  let drawerOpen = false;
  let building = false;
  let collapsed: Record<string, boolean> = {}; // per-project accordion state

  // ---- studio-level persistence (last part, accordion) ----
  const STUDIO_KEY = 'cad-studio-state';
  type StudioState = { lastPartId?: string; collapsed?: Record<string, boolean> };
  function loadStudioState(): StudioState {
    try {
      const s = localStorage.getItem(STUDIO_KEY);
      return s ? JSON.parse(s) : {};
    } catch { return {}; }
  }
  function saveStudioState() {
    try {
      localStorage.setItem(
        STUDIO_KEY,
        JSON.stringify({ lastPartId: currentPartId, collapsed })
      );
    } catch { /* quota / private mode */ }
  }

  const COLORS = ['#ff3b30', '#34c759', '#0a84ff', '#ffd60a', '#ffffff'];

  let ws: WebSocket | null = null;
  let wsRetry: any = null;
  let ro: ResizeObserver | null = null;
  let partsPoll: any = null;
  let destroyed = false;

  onMount(async () => {
    ctx = overlay.getContext('2d');
    sizeOverlay();
    ro = new ResizeObserver(sizeOverlay);
    ro.observe(host);
    window.addEventListener('paste', onPaste);
    await untilViewerReady();
    const studio = loadStudioState();
    if (studio.collapsed) collapsed = { ...studio.collapsed };
    await refreshParts();
    if (parts.length && !currentPartId) {
      const last = studio.lastPartId;
      const restore = last && parts.some((p) => p.part_id === last) ? last : parts[0].part_id;
      selectPart(restore);
    }
    connectWs();
    partsPoll = setInterval(refreshParts, 4000);
  });

  onDestroy(() => {
    destroyed = true;
    ro?.disconnect();
    window.removeEventListener('paste', onPaste);
    ws?.close();
    clearTimeout(wsRetry);
    clearInterval(partsPoll);
  });

  function untilViewerReady(): Promise<void> {
    return new Promise((resolve) => {
      const tick = () => {
        if (destroyed) return resolve();
        if (viewerEl && typeof viewerEl.loadModelUrl === 'function') resolve();
        else setTimeout(tick, 50);
      };
      tick();
    });
  }

  // ---- parts registry --------------------------------------------------
  async function refreshParts() {
    try {
      const r = await fetch(`${apiBase}/api/parts`, { cache: 'no-store' });
      const j = await r.json();
      parts = j.parts || [];
      ensureCollapsed();
    } catch { /* keep last list */ }
  }

  function ensureCollapsed() {
    const projs = new Set(parts.map((p) => projectOf(p.part_id)));
    const curProj = currentPartId ? projectOf(currentPartId) : null;
    let changed = false;
    for (const p of projs) {
      if (!(p in collapsed)) {
        // expand the project containing the current part, collapse the rest
        collapsed[p] = curProj ? p !== curProj : false;
        changed = true;
      }
    }
    if (changed) collapsed = collapsed;
  }

  function toggleProj(p: string) {
    collapsed = { ...collapsed, [p]: !collapsed[p] };
    saveStudioState();
  }

  function projectOf(id: string): string {
    // Group by the FS segment right after "parts/" (boat, rover, …) when
    // present; fall back to the first segment (the TOML project name).
    const segs = id.split('/');
    const i = segs.indexOf('parts');
    if (i >= 0 && i + 1 < segs.length) return segs[i + 1];
    return segs[0] ?? id;
  }
  function shortName(id: string): string {
    const seg = id.split('/');
    return seg[seg.length - 1];
  }
  $: grouped = parts.reduce((acc: Record<string, PartInfo[]>, p) => {
    (acc[projectOf(p.part_id)] ??= []).push(p);
    return acc;
  }, {});

  function selectPart(id: string) {
    if (id === currentPartId) { drawerOpen = false; return; }
    currentPartId = id;
    collapsed = { ...collapsed, [projectOf(id)]: false };
    drawerOpen = false;
    saveStudioState();
    // per-part saved camera/view (CADPersistence is keyed by persistenceId)
    try { viewerEl?.setAttribute?.('persistenceId', id); } catch { /* */ }
    if (mode === 'draw') exitDraw();
    loadModel({ isSwitch: true });
    wsSubscribe();
  }

  // ---- networking ------------------------------------------------------
  function wsSubscribe() {
    if (ws && ws.readyState === WebSocket.OPEN && currentPartId) {
      ws.send(JSON.stringify({ type: 'subscribe', part: currentPartId }));
    }
  }

  function connectWs() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}${apiBase}/ws`;
    wsState = 'connecting';
    try {
      ws = new WebSocket(url);
    } catch {
      scheduleReconnect();
      return;
    }
    ws.onopen = () => { wsState = 'live'; wsSubscribe(); };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'reload') {
          version = msg.version ?? version;
          // Watcher reload on the SAME part → silent refresh (no clear, no
          // spinner). loadModel() manages `building` if it had been set by
          // a switch in flight.
          loadModel({ version });
          refreshParts();
        } else if (msg.type === 'build_error') {
          building = false;
          showToast('⚠ build123d a échoué — voir le terminal');
          refreshParts();
        }
      } catch { /* ignore */ }
    };
    ws.onclose = () => { wsState = 'down'; scheduleReconnect(); };
    ws.onerror = () => ws?.close();
  }

  function scheduleReconnect() {
    if (destroyed) return;
    clearTimeout(wsRetry);
    wsRetry = setTimeout(connectWs, 1500);
  }

  async function loadModel(opts?: { isSwitch?: boolean; version?: number }) {
    if (!viewerEl?.loadModelUrl || !currentPartId) return;
    const tag = opts?.version ?? Date.now();
    const isSwitch = !!opts?.isSwitch;
    if (isSwitch) {
      building = true;       // show the loading overlay
      viewerEl.clearScene?.(); // hide previous model immediately
    }
    try {
      await viewerEl.loadModelUrl(
        `${apiBase}/api/model?part=${encodeURIComponent(currentPartId)}&v=${tag}`
      );
    } catch (err) {
      if ((err as Error).message !== 'superseded') {
        showToast('Échec chargement : ' + (err as Error).message);
      }
    } finally {
      building = false;
    }
  }

  // ---- overlay sizing / drawing ---------------------------------------
  function sizeOverlay() {
    if (!host || !overlay) return;
    const r = host.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    overlay.width = Math.max(1, Math.round(r.width * dpr));
    overlay.height = Math.max(1, Math.round(r.height * dpr));
    overlay.style.width = r.width + 'px';
    overlay.style.height = r.height + 'px';
    redraw();
  }

  function redraw() {
    if (!ctx) return;
    const { width, height } = overlay;
    ctx.clearRect(0, 0, width, height);
    if (bg) ctx.drawImage(bg, 0, 0, width, height);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    ctx.save();
    ctx.scale(dpr, dpr);
    for (const s of strokes) {
      ctx.fillStyle = s.color;
      ctx.fill(strokeToPath2D(s.points));
    }
    if (current.length) {
      ctx.fillStyle = inkColor;
      ctx.fill(strokeToPath2D(current));
    }
    ctx.restore();
  }

  // ---- modes -----------------------------------------------------------
  function enterDraw() {
    const dataUrl = viewerEl?.captureViewPNG?.() || '';
    kind = 'annotation';
    if (dataUrl) {
      const img = new Image();
      img.onload = () => { bg = img; redraw(); };
      img.src = dataUrl;
    } else {
      bg = null;
    }
    strokes = [];
    current = [];
    pickedNode = null;
    mode = 'draw';
    viewerEl?.setNavigationEnabled?.(false);
  }

  function exitDraw() {
    mode = 'nav';
    bg = null;
    strokes = [];
    current = [];
    kind = 'annotation';
    viewerEl?.setNavigationEnabled?.(true);
    redraw();
  }

  function undo() { strokes = strokes.slice(0, -1); redraw(); }
  function clearInk() { strokes = []; current = []; redraw(); }

  // ---- pointer (stylus) ------------------------------------------------
  function onPointerDown(e: PointerEvent) {
    if (mode !== 'draw') return;
    e.preventDefault();
    overlay.setPointerCapture(e.pointerId);
    current = [{ x: e.offsetX, y: e.offsetY, pressure: pressureOf(e) }];
    if (pickedNode == null && kind === 'annotation') {
      try { pickedNode = viewerEl?.pickNodeAt?.(e.clientX, e.clientY) ?? null; }
      catch { pickedNode = null; }
    }
    redraw();
  }

  function onPointerMove(e: PointerEvent) {
    if (mode !== 'draw' || e.buttons === 0 || current.length === 0) return;
    e.preventDefault();
    current = [...current, { x: e.offsetX, y: e.offsetY, pressure: pressureOf(e) }];
    redraw();
  }

  function onPointerUp(e: PointerEvent) {
    if (mode !== 'draw' || current.length === 0) return;
    try { overlay.releasePointerCapture(e.pointerId); } catch { /* */ }
    strokes = [...strokes, { points: current, color: inkColor }];
    current = [];
    redraw();
  }

  // ---- reference image -------------------------------------------------
  function onPaste(e: ClipboardEvent) {
    const item = Array.from(e.clipboardData?.items || []).find((i) =>
      i.type.startsWith('image/')
    );
    if (!item) return;
    const file = item.getAsFile();
    if (file) loadReference(file);
  }

  function onPickFile(e: Event) {
    const f = (e.target as HTMLInputElement).files?.[0];
    if (f) loadReference(f);
  }

  function loadReference(file: Blob) {
    const img = new Image();
    img.onload = () => {
      bg = img;
      strokes = [];
      current = [];
      pickedNode = null;
      kind = 'reference';
      mode = 'draw';
      viewerEl?.setNavigationEnabled?.(false);
      redraw();
    };
    img.src = URL.createObjectURL(file);
  }

  // ---- send to Claude --------------------------------------------------
  async function send() {
    if (sending || !currentPartId) return;
    sending = true;
    try {
      redraw();
      const blob: Blob = await new Promise((res, rej) =>
        overlay.toBlob((b) => (b ? res(b) : rej(new Error('toBlob failed'))), 'image/png')
      );
      const fd = new FormData();
      fd.append('image', blob, 'feedback.png');
      fd.append('part', currentPartId);
      fd.append('note', note);
      fd.append('picked_node', pickedNode || '');
      fd.append('kind', kind);
      const r = await fetch(`${apiBase}/api/feedback`, { method: 'POST', body: fd });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      showToast(`✓ Envoyé à Claude (#${j.id})`);
      note = '';
      exitDraw();
      refreshParts();
    } catch (err) {
      showToast('✗ Échec envoi : ' + (err as Error).message);
    } finally {
      sending = false;
    }
  }

  let toastTimer: any;
  function showToast(t: string) {
    toast = t;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast = ''), 3200);
  }
</script>

<div class="studio" bind:this={host}>
  <cad-viewer
    bind:this={viewerEl}
    viewerBackgroundColor={viewerBackgroundColor}
    persistenceId="cad-studio"
  ></cad-viewer>

  <canvas
    class="overlay"
    class:active={mode === 'draw'}
    bind:this={overlay}
    on:pointerdown={onPointerDown}
    on:pointermove={onPointerMove}
    on:pointerup={onPointerUp}
    on:pointercancel={onPointerUp}
  ></canvas>

  <!-- parts switcher -->
  <button class="parts-toggle" on:click={() => (drawerOpen = !drawerOpen)}>
    ☰ {currentPartId ? shortName(currentPartId) : 'Pièces'}
  </button>

  {#if drawerOpen}
    <button class="scrim" aria-label="fermer" on:click={() => (drawerOpen = false)}></button>
    <div class="drawer">
      <div class="drawer-head">
        <span>Pièces ({parts.length})</span>
        <button class="x" on:click={() => (drawerOpen = false)}>✕</button>
      </div>
      {#each Object.entries(grouped) as [proj, list]}
        <button class="proj" on:click={() => toggleProj(proj)}>
          <span class="caret">{collapsed[proj] ? '▸' : '▾'}</span>
          <span class="pj">{proj}</span>
          <span class="count">{list.length}</span>
        </button>
        {#if !collapsed[proj]}
          {#each list as p}
            <button
              class="part-btn"
              class:active={p.part_id === currentPartId}
              on:click={() => selectPart(p.part_id)}
            >
              <span class="dot2" class:on={p.loaded && p.build_ok} class:err={p.loaded && !p.build_ok}></span>
              <span class="pname">{shortName(p.part_id)}</span>
              {#if p.building}<span class="badge">build…</span>
              {:else if p.pending_feedback}<span class="badge fb">{p.pending_feedback}</span>
              {:else if p.loaded}<span class="badge v">v{p.version}</span>{/if}
            </button>
          {/each}
        {/if}
      {/each}
    </div>
  {/if}

  {#if building}
    <div class="spinner">⏳ Build de {currentPartId ? shortName(currentPartId) : ''}…</div>
  {/if}

  <div class="bar">
    {#if mode === 'nav'}
      <button class="primary" on:click={enterDraw}>✏️ Annoter</button>
      <label class="ref">
        🖼 Référence
        <input type="file" accept="image/*" on:change={onPickFile} />
      </label>
    {:else}
      <div class="colors">
        {#each COLORS as c}
          <button
            class="swatch"
            class:sel={inkColor === c}
            style="background:{c}"
            aria-label="couleur"
            on:click={() => (inkColor = c)}
          ></button>
        {/each}
      </div>
      <button on:click={undo} disabled={strokes.length === 0}>↶ Annuler</button>
      <button on:click={clearInk} disabled={strokes.length === 0}>Effacer</button>
      <input class="note" placeholder="Note pour Claude (ex: +2mm ce trou)" bind:value={note} />
      <button class="primary" on:click={send} disabled={sending}>
        {sending ? '…' : '➤ Envoyer'}
      </button>
      <button on:click={exitDraw}>✕</button>
    {/if}
  </div>

  <div class="status">
    <span class="dot {wsState}"></span>
    {wsState === 'live' ? `live · v${version}` : wsState === 'connecting' ? 'connexion…' : 'hors-ligne'}
    {#if currentPartId} · {shortName(currentPartId)}{/if}
    {#if kind === 'reference'} · réf{/if}
    {#if pickedNode} · ⌖ {pickedNode}{/if}
  </div>

  {#if toast}<div class="toast">{toast}</div>{/if}
</div>

<style>
  .studio { position: relative; width: 100%; height: 100%; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
  cad-viewer { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }

  .overlay {
    position: absolute; inset: 0; touch-action: none;
    pointer-events: none;
  }
  .overlay.active { pointer-events: auto; cursor: crosshair; }

  .parts-toggle {
    position: absolute; top: 12px; right: 12px; z-index: 26;
    background: rgba(28,28,30,0.9); color: #fff; border: 1px solid #4a4a4c;
    border-radius: 10px; padding: 0 16px; min-height: 48px; font-size: 16px;
    cursor: pointer; max-width: 56vw; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap;
  }
  .scrim {
    position: absolute; inset: 0; z-index: 24; background: rgba(0,0,0,0.35);
    border: none; padding: 0; cursor: pointer;
  }
  .drawer {
    position: absolute; top: 0; right: 0; bottom: 0; z-index: 25;
    width: min(360px, 84vw); overflow-y: auto;
    background: rgba(20,20,22,0.98); backdrop-filter: blur(10px);
    border-left: 1px solid #3a3a3c; padding: 10px;
    -webkit-overflow-scrolling: touch;
  }
  .drawer-head {
    display: flex; align-items: center; justify-content: space-between;
    color: #ddd; font-size: 15px; font-weight: 600; padding: 6px 6px 10px;
  }
  .drawer-head .x {
    background: #3a3a3c; color: #fff; border: 1px solid #555;
    border-radius: 8px; min-width: 40px; min-height: 40px; font-size: 16px;
    cursor: pointer;
  }
  .proj {
    display: flex; align-items: center; gap: 10px; width: 100%;
    background: #242426; color: #cfcfd2; border: 1px solid #333;
    border-radius: 9px; margin: 6px 0 2px; padding: 0 12px; min-height: 50px;
    font-size: 14px; text-transform: uppercase; letter-spacing: .5px;
    cursor: pointer;
  }
  .proj .caret { width: 14px; color: #8a8a8e; }
  .proj .pj { flex: 1; text-align: left; }
  .proj .count { font-size: 12px; color: #888; background: #1c1c1e;
    border-radius: 6px; padding: 3px 8px; }
  .part-btn {
    display: flex; align-items: center; gap: 10px; width: 100%;
    background: none; border: none; color: #e2e2e4; text-align: left;
    padding: 0 12px 0 26px; border-radius: 9px; cursor: pointer;
    font-size: 16px; min-height: 54px;
  }
  .part-btn:active { background: #2c2c2e; }
  .part-btn.active { background: #0a4a8a; color: #fff; }
  .pname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dot2 { width: 10px; height: 10px; border-radius: 50%; background: #555; flex: 0 0 auto; }
  .dot2.on { background: #34c759; }
  .dot2.err { background: #ff3b30; }
  .badge { font-size: 12px; color: #999; background: #333; border-radius: 6px;
    padding: 3px 8px; }
  .badge.fb { background: #b3261e; color: #fff; }
  .badge.v { background: #2a2a2c; }

  .spinner {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
    z-index: 22; background: rgba(28,28,30,0.92); color: #fff;
    padding: 12px 20px; border-radius: 10px; font-size: 15px;
    border: 1px solid #3a3a3c;
  }

  .bar {
    position: absolute; left: 50%; bottom: 16px; transform: translateX(-50%);
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    max-width: 96vw; padding: 8px 10px; border-radius: 12px;
    background: rgba(28,28,30,0.86); backdrop-filter: blur(8px);
    border: 1px solid #3a3a3c; z-index: 20;
  }
  /* Tous les contrôles du bas partagent la même base (taille, radius, font,
     bordure) — seule la couleur de fond change selon l'emphase. */
  .bar button, .bar .ref, .bar .note {
    display: inline-flex; align-items: center; justify-content: center;
    gap: 8px;
    background: #2c2c2e; color: #fff; border: 1px solid #4a4a4c;
    border-radius: 10px; padding: 0 16px; min-height: 48px; font-size: 15px;
    font-family: inherit; line-height: 1; cursor: pointer;
    box-sizing: border-box;
  }
  .bar button:disabled { opacity: 0.4; cursor: default; }
  .bar button.primary {
    background: #0a84ff; border-color: #0a84ff; font-weight: 600;
  }
  .bar .ref { position: relative; overflow: hidden; }
  .bar .ref input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
  .bar .note {
    background: #1c1c1e; cursor: text;
    width: min(46vw, 320px); justify-content: flex-start;
    padding: 0 14px;
  }
  .colors { display: flex; gap: 6px; }
  .swatch { width: 30px; height: 30px; min-height: 30px; border-radius: 50%;
    padding: 0; border: 2px solid #1c1c1e; }
  .swatch.sel { border-color: #fff; }

  .status {
    position: absolute; top: 12px; left: 12px; z-index: 20;
    font-size: 12px; color: #ddd; background: rgba(28,28,30,0.7);
    padding: 5px 9px; border-radius: 7px; display: flex; align-items: center; gap: 6px;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #777; }
  .dot.live { background: #34c759; }
  .dot.connecting { background: #ffd60a; }
  .dot.down { background: #ff3b30; }

  .toast {
    position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
    z-index: 30; background: rgba(28,28,30,0.92); color: #fff;
    padding: 9px 16px; border-radius: 9px; font-size: 14px;
    border: 1px solid #3a3a3c;
  }
</style>
