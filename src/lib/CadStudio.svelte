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
    open?: boolean;
    opened_at?: number;
    last_active_at?: number;
  }

  let host: HTMLElement;
  let mainEl: HTMLElement;   // the viewer pane (overlay is sized to this)
  let viewerEl: any;
  let overlay: HTMLCanvasElement;
  let ctx: CanvasRenderingContext2D | null = null;

  let mode: Mode = 'nav';
  let kind: Kind = 'annotation';
  let strokes: Stroke[] = [];
  let current: InkPoint[] = [];
  let inkColor = '#ff3b30';
  let note = '';
  let pickedNodes: string[] = [];   // all nodes circled this annotation (multi-region)
  let bg: HTMLImageElement | null = null;

  // ---- reference library (server-persisted, per part) ----
  interface RefInfo { id: number; label: string; note: string; bytes: number; created_at: number; url: string; }
  let refs: RefInfo[] = [];
  let lastRefBlob: Blob | null = null; // a freshly loaded ref not yet kept
  let keeping = false;
  // ---- reference compare dock (ref shown beside/under the live viewer) ----
  let compareRef: { src: string; label: string; id: number | null } | null = null;
  let refImg: HTMLImageElement | null = null; // decoded ref, for canvas compositing
  let viewerPaneEl: HTMLElement;
  let refDockEl: HTMLElement;

  let wsState: 'connecting' | 'live' | 'down' = 'connecting';
  let version = 0;
  let toast = '';
  let sending = false;

  // ---- multi-part state ----
  type SideView = 'parts' | 'params' | 'refs';
  let parts: PartInfo[] = [];
  let currentPartId: string | null = null;
  let building = false;
  let collapsed: Record<string, boolean> = {}; // per-project accordion state
  // ---- VSCode-like shell ----
  let sidebarOpen = true;
  let activeView: SideView = 'parts';

  // ---- studio-level persistence (last part, accordion, shell) ----
  const STUDIO_KEY = 'cad-studio-state';
  type StudioState = {
    lastPartId?: string;
    collapsed?: Record<string, boolean>;
    sidebarOpen?: boolean;
    activeView?: SideView;
  };
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
        JSON.stringify({ lastPartId: currentPartId, collapsed, sidebarOpen, activeView })
      );
    } catch { /* quota / private mode */ }
  }

  function toggleView(v: SideView) {
    // Click the active icon → collapse; click another → switch & open.
    if (sidebarOpen && activeView === v) sidebarOpen = false;
    else { activeView = v; sidebarOpen = true; }
    if (v === 'refs') refreshRefs();
    saveStudioState();
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
    // Observe the viewer pane: the overlay must follow it when the sidebar
    // opens/closes or the window resizes.
    ro = new ResizeObserver(sizeOverlay);
    ro.observe(mainEl);
    window.addEventListener('paste', onPaste);
    await untilViewerReady();
    const studio = loadStudioState();
    if (studio.collapsed) collapsed = { ...studio.collapsed };
    if (typeof studio.sidebarOpen === 'boolean') sidebarOpen = studio.sidebarOpen;
    if (studio.activeView) activeView = studio.activeView;
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

  // Open tabs (stable order by opened_at) + recents (by last_active_at).
  // Server-shared state: every tablet & Claude (via list_parts) see the same.
  $: openTabs = parts
    .filter((p) => p.open)
    .sort((a, b) => (a.opened_at || 0) - (b.opened_at || 0));
  $: recents = parts
    .filter((p) => (p.last_active_at || 0) > 0)
    .sort((a, b) => (b.last_active_at || 0) - (a.last_active_at || 0))
    .slice(0, 5);

  function markActive(id: string) {
    const fd = new FormData();
    fd.append('part', id);
    fetch(`${apiBase}/api/active`, { method: 'POST', body: fd })
      .then(() => refreshParts())
      .catch(() => { /* offline → poll will reconcile */ });
  }

  function selectPart(id: string) {
    if (id === currentPartId) return;
    currentPartId = id;
    collapsed = { ...collapsed, [projectOf(id)]: false };
    saveStudioState();
    // optimistic: show the tab + recency immediately (server reconciles)
    const now = Date.now() / 1000;
    parts = parts.map((p) =>
      p.part_id === id
        ? { ...p, open: true, last_active_at: now, opened_at: p.opened_at || now }
        : p
    );
    markActive(id);
    // per-part saved camera/view (CADPersistence is keyed by persistenceId)
    try { viewerEl?.setAttribute?.('persistenceId', id); } catch { /* */ }
    if (mode === 'draw') exitDraw();
    loadModel({ isSwitch: true });
    wsSubscribe();
    refreshRefs();
  }

  function closeTab(id: string) {
    const wasActive = id === currentPartId;
    const remaining = openTabs.filter((p) => p.part_id !== id);
    parts = parts.map((p) =>
      p.part_id === id ? { ...p, open: false, opened_at: 0 } : p
    );
    const fd = new FormData();
    fd.append('part', id);
    fetch(`${apiBase}/api/close`, { method: 'POST', body: fd })
      .then(() => refreshParts())
      .catch(() => { /* offline → poll will reconcile */ });
    if (wasActive && remaining.length) {
      selectPart(remaining[remaining.length - 1].part_id);
    }
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
    if (!mainEl || !overlay) return;
    const r = mainEl.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    overlay.width = Math.max(1, Math.round(r.width * dpr));
    overlay.height = Math.max(1, Math.round(r.height * dpr));
    overlay.style.width = r.width + 'px';
    overlay.style.height = r.height + 'px';
    redraw();
  }

  // Draw `img` contained (letterboxed) inside a rect — preserves aspect.
  function drawContain(img: HTMLImageElement, x: number, y: number, w: number, h: number) {
    const iw = img.naturalWidth || img.width, ih = img.naturalHeight || img.height;
    if (!iw || !ih) return;
    const s = Math.min(w / iw, h / ih);
    const dw = iw * s, dh = ih * s;
    ctx!.drawImage(img, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
  }

  function redraw() {
    if (!ctx) return;
    const { width, height } = overlay;
    ctx.clearRect(0, 0, width, height);
    // Frozen composite (only in draw mode, when bg = viewer snapshot is set).
    // In nav mode the live <cad-viewer> + the dock <img> show through.
    if (bg) {
      if (compareRef && refImg && viewerPaneEl && refDockEl && mainEl) {
        // split: snapshot in the viewer-pane rect, reference in the dock rect
        const m = mainEl.getBoundingClientRect();
        const sx = overlay.width / m.width, sy = overlay.height / m.height;
        const vp = viewerPaneEl.getBoundingClientRect();
        const dk = refDockEl.getBoundingClientRect();
        ctx.drawImage(bg, (vp.left - m.left) * sx, (vp.top - m.top) * sy,
                      vp.width * sx, vp.height * sy);
        drawContain(refImg, (dk.left - m.left) * sx, (dk.top - m.top) * sy,
                    dk.width * sx, dk.height * sy);
      } else {
        ctx.drawImage(bg, 0, 0, width, height);
      }
    }
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
    pickedNodes = [];
    mode = 'draw';
    viewerEl?.setNavigationEnabled?.(false);
  }

  function exitDraw() {
    mode = 'nav';
    bg = null;
    strokes = [];
    current = [];
    pickedNodes = [];
    kind = 'annotation';
    viewerEl?.setNavigationEnabled?.(true);
    redraw();  // keep compareRef dock so the user can keep iterating
  }

  function undo() { strokes = strokes.slice(0, -1); redraw(); }
  function clearInk() { strokes = []; current = []; redraw(); }

  // ---- pointer (stylus) ------------------------------------------------
  function onPointerDown(e: PointerEvent) {
    if (mode !== 'draw') return;
    e.preventDefault();
    overlay.setPointerCapture(e.pointerId);
    current = [{ x: e.offsetX, y: e.offsetY, pressure: pressureOf(e) }];
    // multi-region: pick the node under each stroke start, accumulate (dedup).
    if (kind === 'annotation') {
      try {
        const n = viewerEl?.pickNodeAt?.(e.clientX, e.clientY) ?? null;
        if (n && !pickedNodes.includes(n)) pickedNodes = [...pickedNodes, n];
      } catch { /* over the ref dock / empty space → no node */ }
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

  // Dock a reference beside/under the live viewer (compare mode). The model
  // stays interactive; "✏️ Annoter" then freezes and lets you draw across both.
  function openCompare(src: string, label: string, id: number | null, blob: Blob | null) {
    lastRefBlob = blob;
    compareRef = { src, label, id };
    const img = new Image();
    img.onload = () => { refImg = img; redraw(); };
    img.src = src;
  }

  function closeCompare() {
    compareRef = null;
    refImg = null;
    lastRefBlob = null;
    if (mode === 'draw') exitDraw(); else redraw();
  }

  function loadReference(file: Blob) {
    openCompare(URL.createObjectURL(file), '', null, file);
  }

  // ---- reference library ----------------------------------------------
  async function refreshRefs() {
    if (!currentPartId) { refs = []; return; }
    try {
      const r = await fetch(
        `${apiBase}/api/references?part=${encodeURIComponent(currentPartId)}`,
        { cache: 'no-store' }
      );
      const j = await r.json();
      refs = j.items || [];
    } catch { /* keep last */ }
  }

  function loadReferenceFromLibrary(rf: RefInfo) {
    openCompare(`${apiBase}${rf.url}`, rf.label, rf.id, null);
  }

  async function keepReference() {
    if (!lastRefBlob || !currentPartId || keeping) return;
    keeping = true;
    try {
      const fd = new FormData();
      fd.append('image', lastRefBlob, 'reference.png');
      fd.append('part', currentPartId);
      const r = await fetch(`${apiBase}/api/references`, { method: 'POST', body: fd });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const rec = await r.json();
      lastRefBlob = null;
      if (compareRef) compareRef = { ...compareRef, id: rec.id }; // now persisted
      await refreshRefs();
      showToast('📌 Référence gardée');
    } catch (e) { showToast('✗ ' + (e as Error).message); }
    finally { keeping = false; }
  }

  async function deleteRef(id: number) {
    if (!currentPartId) return;
    const fd = new FormData();
    fd.append('part', currentPartId);
    fd.append('id', String(id));
    try {
      await fetch(`${apiBase}/api/references`, { method: 'DELETE', body: fd });
      await refreshRefs();
    } catch { /* */ }
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
      fd.append('picked_node', pickedNodes[0] || '');
      fd.append('picked_nodes', JSON.stringify(pickedNodes));
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

<div class="shell" bind:this={host} class:sb-open={sidebarOpen}>
  <!-- title bar: app name + editor tabs + connection -->
  <header class="titlebar">
    <div class="brand">cad-studio</div>
    <div class="tabs">
      {#each openTabs as t (t.part_id)}
        <div
          class="tab"
          class:active={t.part_id === currentPartId}
          role="button"
          tabindex="0"
          on:click={() => selectPart(t.part_id)}
          on:keydown={(e) => { if (e.key === 'Enter' || e.key === ' ') selectPart(t.part_id); }}
        >
          <span class="dot2" class:on={t.loaded && t.build_ok} class:err={t.loaded && !t.build_ok}></span>
          <span class="tname">{shortName(t.part_id)}</span>
          {#if t.building}<span class="tbadge">⏳</span>
          {:else if t.pending_feedback}<span class="tbadge fb">{t.pending_feedback}</span>{/if}
          <button class="tclose" aria-label="fermer l'onglet" on:click|stopPropagation={() => closeTab(t.part_id)}>✕</button>
        </div>
      {/each}
    </div>
    <div class="title-right">
      <span class="dot {wsState}"></span>
      <span>{wsState === 'live' ? 'live' : wsState === 'connecting' ? '…' : 'hors-ligne'}</span>
    </div>
  </header>

  <!-- activity bar -->
  <nav class="activitybar">
    <button class:sel={activeView === 'parts' && sidebarOpen} title="Pièces" aria-label="Pièces" on:click={() => toggleView('parts')}>🗂</button>
    <button class:sel={activeView === 'params' && sidebarOpen} title="Paramètres" aria-label="Paramètres" on:click={() => toggleView('params')}>⚙️</button>
    <button class:sel={activeView === 'refs' && sidebarOpen} title="Références" aria-label="Références" on:click={() => toggleView('refs')}>🖼</button>
  </nav>

  <!-- sidebar -->
  <aside class="sidebar">
    {#if activeView === 'parts'}
      <div class="side-head">Explorer · {parts.length} pièces</div>
      <div class="side-scroll">
        {#if recents.length}
          <div class="group-label">Récentes</div>
          {#each recents as p}
            <button class="part-btn" class:active={p.part_id === currentPartId} on:click={() => selectPart(p.part_id)}>
              <span class="dot2" class:on={p.loaded && p.build_ok} class:err={p.loaded && !p.build_ok}></span>
              <span class="pname">{shortName(p.part_id)}</span>
              {#if p.open}<span class="badge open">●</span>
              {:else if p.pending_feedback}<span class="badge fb">{p.pending_feedback}</span>{/if}
            </button>
          {/each}
          <div class="group-sep"></div>
        {/if}
        {#each Object.entries(grouped) as [proj, list]}
          <button class="proj" on:click={() => toggleProj(proj)}>
            <span class="caret">{collapsed[proj] ? '▸' : '▾'}</span>
            <span class="pj">{proj}</span>
            <span class="count">{list.length}</span>
          </button>
          {#if !collapsed[proj]}
            {#each list as p}
              <button class="part-btn" class:active={p.part_id === currentPartId} on:click={() => selectPart(p.part_id)}>
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
    {:else if activeView === 'params'}
      <div class="side-head">Paramètres</div>
      <div class="side-empty">Bientôt — sliders &amp; toggles des modèles paramétriques (phase 3).</div>
    {:else}
      <div class="side-head">Références{#if currentPartId} · {refs.length}{/if}</div>
      <div class="side-scroll">
        {#if !currentPartId}
          <div class="side-empty">Sélectionne une pièce.</div>
        {:else}
          <label class="ref-add">
            ＋ Ajouter une image
            <input type="file" accept="image/*" on:change={onPickFile} />
          </label>
          {#if !refs.length}
            <div class="side-empty">Aucune référence. Colle une image (⌘V) ou ajoute-en une — elle est gardée sur le serveur et visible par Claude.</div>
          {/if}
          {#each refs as rf (rf.id)}
            <div class="ref-card">
              <button class="ref-thumb" on:click={() => loadReferenceFromLibrary(rf)} title="Comparer / annoter avec cette référence">
                <img src={`${apiBase}${rf.url}`} alt={rf.label || ('réf ' + rf.id)} />
              </button>
              <div class="ref-meta">
                <div class="ref-label">{rf.label || ('réf #' + rf.id)}</div>
                {#if rf.note}<div class="ref-note">{rf.note}</div>{/if}
              </div>
              <button class="ref-del" aria-label="supprimer" title="supprimer" on:click={() => deleteRef(rf.id)}>🗑</button>
            </div>
          {/each}
        {/if}
      </div>
    {/if}
  </aside>

  <!-- main: viewer pane (+ optional reference dock for compare/annotate) -->
  <main class="main" bind:this={mainEl} class:has-dock={!!compareRef}>
    <div class="viewer-pane" bind:this={viewerPaneEl}>
      <cad-viewer
        bind:this={viewerEl}
        viewerBackgroundColor={viewerBackgroundColor}
        persistenceId="cad-studio"
      ></cad-viewer>
    </div>

    {#if compareRef}
      <div class="ref-dock" bind:this={refDockEl}>
        <img class="ref-dock-img" src={compareRef.src} alt={compareRef.label || 'référence'} on:load={redraw} />
        <div class="ref-dock-bar">
          <span class="ref-dock-label">{compareRef.label || 'référence'}</span>
          {#if compareRef.id == null}
            <button class="dock-keep" on:click={keepReference} disabled={keeping}>{keeping ? '…' : '📌 Garder'}</button>
          {/if}
          <button class="dock-close" aria-label="fermer la référence" on:click={closeCompare}>✕</button>
        </div>
      </div>
    {/if}

    <canvas
      class="overlay"
      class:active={mode === 'draw'}
      bind:this={overlay}
      on:pointerdown={onPointerDown}
      on:pointermove={onPointerMove}
      on:pointerup={onPointerUp}
      on:pointercancel={onPointerUp}
    ></canvas>

    {#if building}
      <div class="spinner">⏳ Build de {currentPartId ? shortName(currentPartId) : ''}…</div>
    {/if}

    <div class="toolbar" class:drawing={mode === 'draw'}>
      {#if mode === 'nav'}
        <button class="primary" on:click={enterDraw}>✏️ Annoter</button>
        <label class="ref">
          🖼 Référence
          <input type="file" accept="image/*" on:change={onPickFile} />
        </label>
      {:else}
        <div class="colors">
          {#each COLORS as c}
            <button class="swatch" class:sel={inkColor === c} style="background:{c}" aria-label="couleur" on:click={() => (inkColor = c)}></button>
          {/each}
        </div>
        <button on:click={undo} disabled={strokes.length === 0}>↶ Annuler</button>
        <button on:click={clearInk} disabled={strokes.length === 0}>Effacer</button>
        <input class="note" placeholder="Note pour Claude (ex: +2mm ce trou)" bind:value={note} />
        <button class="primary" on:click={send} disabled={sending}>{sending ? '…' : '➤ Envoyer'}</button>
        <button on:click={exitDraw}>✕</button>
      {/if}
    </div>

    {#if toast}<div class="toast">{toast}</div>{/if}
  </main>

  <!-- status bar -->
  <footer class="statusbar">
    <span class="dot {wsState}"></span>
    <span>{wsState === 'live' ? `live · v${version}` : wsState === 'connecting' ? 'connexion…' : 'hors-ligne'}</span>
    {#if currentPartId}<span class="sb-sep">·</span><span>{shortName(currentPartId)}</span>{/if}
    {#if compareRef}<span class="sb-sep">·</span><span>réf</span>{/if}
    {#if pickedNodes.length === 1}<span class="sb-sep">·</span><span>⌖ {pickedNodes[0]}</span>
    {:else if pickedNodes.length > 1}<span class="sb-sep">·</span><span>⌖ {pickedNodes.length} zones</span>{/if}
    <span class="sb-spacer"></span>
    <button class="sb-toggle" title="Basculer la sidebar" on:click={() => { sidebarOpen = !sidebarOpen; saveStudioState(); }}>
      {sidebarOpen ? '◧' : '▢'} panneau
    </button>
  </footer>
</div>

<style>
  :host { display: block; width: 100%; height: 100%; }

  /* ── VSCode-like shell: title / [activity | sidebar | main] / status ── */
  .shell {
    position: relative; width: 100%; height: 100%; overflow: hidden;
    display: grid;
    grid-template-columns: 48px 260px 1fr;
    grid-template-rows: 40px 1fr 26px;
    grid-template-areas:
      "title  title  title"
      "act    side   main"
      "status status status";
    background: #1a1a1c; color: #e2e2e4;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 14px;
  }
  .shell:not(.sb-open) { grid-template-columns: 48px 0 1fr; }

  /* title bar */
  .titlebar {
    grid-area: title; display: flex; align-items: stretch; gap: 8px;
    background: #202022; border-bottom: 1px solid #2c2c2e; padding: 0 10px;
    min-width: 0;
  }
  .brand { display: flex; align-items: center; font-weight: 600; color: #cfcfd2;
    font-size: 13px; padding-right: 6px; flex: 0 0 auto; }
  .titlebar .tabs {
    display: flex; align-items: flex-end; gap: 4px; flex: 1 1 auto; min-width: 0;
    overflow-x: auto; scrollbar-width: none;
  }
  .titlebar .tabs::-webkit-scrollbar { display: none; }
  .tab {
    display: inline-flex; align-items: center; gap: 7px; flex: 0 0 auto;
    height: 30px; align-self: center; background: #2a2a2c; color: #b9b9bd;
    border: 1px solid #333; border-radius: 7px 7px 0 0; padding: 0 4px 0 10px;
    font-size: 13px; cursor: pointer; max-width: 200px;
  }
  .tab.active { background: #1a1a1c; color: #fff; border-color: #0a84ff;
    box-shadow: inset 0 2px 0 #0a84ff; }
  .tab .tname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    max-width: 120px; }
  .tab .tbadge { font-size: 11px; background: #333; border-radius: 5px; padding: 1px 6px; }
  .tab .tbadge.fb { background: #b3261e; color: #fff; }
  .tclose { background: none; border: none; color: inherit; opacity: 0.5;
    cursor: pointer; font-size: 13px; min-width: 24px; height: 24px;
    border-radius: 5px; padding: 0; line-height: 1; }
  .tclose:hover { opacity: 1; background: rgba(255,255,255,0.14); }
  .title-right { display: flex; align-items: center; gap: 6px; flex: 0 0 auto;
    color: #9a9a9e; font-size: 12px; }

  /* activity bar */
  .activitybar {
    grid-area: act; background: #18181a; border-right: 1px solid #2c2c2e;
    display: flex; flex-direction: column; align-items: center; gap: 4px;
    padding-top: 8px;
  }
  .activitybar button {
    width: 40px; height: 40px; background: none; border: none; cursor: pointer;
    font-size: 19px; border-radius: 9px; opacity: 0.55; color: #fff;
    border-left: 2px solid transparent;
  }
  .activitybar button:hover { opacity: 1; background: #242426; }
  .activitybar button.sel { opacity: 1; background: #242426; border-left-color: #0a84ff; }

  /* sidebar */
  .sidebar {
    grid-area: side; background: #1e1e20; border-right: 1px solid #2c2c2e;
    display: flex; flex-direction: column; min-width: 0; overflow: hidden;
  }
  .shell:not(.sb-open) .sidebar { display: none; }
  .side-head { padding: 10px 12px; font-size: 11px; text-transform: uppercase;
    letter-spacing: .6px; color: #8a8a8e; border-bottom: 1px solid #262628; flex: 0 0 auto; }
  .side-scroll { flex: 1 1 auto; overflow-y: auto; padding: 6px;
    -webkit-overflow-scrolling: touch; }
  .side-empty { padding: 16px 14px; color: #6f6f73; font-size: 13px; line-height: 1.5; }

  /* reference library cards */
  .ref-add {
    display: flex; align-items: center; justify-content: center; gap: 6px;
    margin: 6px; padding: 9px; border: 1px dashed #3a3a3c; border-radius: 8px;
    color: #b9b9bd; font-size: 13px; cursor: pointer; position: relative; overflow: hidden;
  }
  .ref-add:hover { background: #242426; }
  .ref-add input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
  .ref-card {
    display: flex; align-items: center; gap: 8px; padding: 6px; margin: 4px 2px;
    border-radius: 8px;
  }
  .ref-card:hover { background: #242426; }
  .ref-thumb {
    flex: 0 0 auto; width: 56px; height: 44px; padding: 0; border-radius: 6px;
    border: 1px solid #3a3a3c; background: #111; cursor: pointer; overflow: hidden;
  }
  .ref-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .ref-meta { flex: 1 1 auto; min-width: 0; }
  .ref-label { font-size: 13px; color: #e2e2e4; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .ref-note { font-size: 11px; color: #8a8a8e; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .ref-del { flex: 0 0 auto; background: none; border: none; cursor: pointer;
    font-size: 14px; opacity: 0.5; min-width: 30px; height: 30px; border-radius: 6px; }
  .ref-del:hover { opacity: 1; background: rgba(255,80,80,0.18); }
  .group-label { color: #8a8a8e; font-size: 11px; text-transform: uppercase;
    letter-spacing: .5px; padding: 8px 8px 4px; }
  .group-sep { height: 1px; background: #2c2c2e; margin: 8px 4px; }
  .proj {
    display: flex; align-items: center; gap: 9px; width: 100%;
    background: #242426; color: #cfcfd2; border: 1px solid #2e2e30;
    border-radius: 8px; margin: 5px 0 2px; padding: 0 10px; min-height: 40px;
    font-size: 13px; text-transform: uppercase; letter-spacing: .4px; cursor: pointer;
  }
  .proj .caret { width: 12px; color: #8a8a8e; }
  .proj .pj { flex: 1; text-align: left; }
  .proj .count { font-size: 11px; color: #888; background: #1c1c1e;
    border-radius: 5px; padding: 2px 7px; }
  .part-btn {
    display: flex; align-items: center; gap: 9px; width: 100%;
    background: none; border: none; color: #d8d8da; text-align: left;
    padding: 0 10px 0 22px; border-radius: 8px; cursor: pointer;
    font-size: 14px; min-height: 40px;
  }
  .part-btn:hover { background: #242426; }
  .part-btn.active { background: #0a4a8a; color: #fff; }
  .pname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dot2 { width: 9px; height: 9px; border-radius: 50%; background: #555; flex: 0 0 auto; }
  .dot2.on { background: #34c759; }
  .dot2.err { background: #ff3b30; }
  .badge { font-size: 11px; color: #999; background: #333; border-radius: 5px; padding: 2px 7px; }
  .badge.fb { background: #b3261e; color: #fff; }
  .badge.v { background: #2a2a2c; }
  .badge.open { background: #1f6f3a; color: #fff; }

  /* main viewer pane — cad-viewer + its own toolbar/gizmo live here */
  .main { grid-area: main; position: relative; min-width: 0; overflow: hidden; }
  .viewer-pane { position: absolute; inset: 0; }
  cad-viewer { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
  /* overlay spans the WHOLE main (viewer + dock) so a stroke crosses both */
  .overlay { position: absolute; inset: 0; touch-action: none; pointer-events: none; z-index: 5; }
  .overlay.active { pointer-events: auto; cursor: crosshair; }

  /* reference compare dock: side in landscape, bottom in portrait */
  .main.has-dock { display: grid; }
  @media (orientation: landscape) {
    .main.has-dock { grid-template-columns: 1fr minmax(220px, 38%); grid-template-rows: 100%; }
  }
  @media (orientation: portrait) {
    .main.has-dock { grid-template-rows: 1fr minmax(160px, 42%); grid-template-columns: 100%; }
  }
  .main.has-dock .viewer-pane { position: relative; inset: auto; }
  .ref-dock { position: relative; overflow: hidden; background: #0d0d0f;
    border-left: 1px solid #2c2c2e; }
  @media (orientation: portrait) {
    .ref-dock { border-left: none; border-top: 1px solid #2c2c2e; }
  }
  .ref-dock-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
  .ref-dock-bar { position: absolute; left: 0; right: 0; bottom: 0; z-index: 6;
    display: flex; align-items: center; gap: 8px; padding: 5px 8px;
    background: rgba(16,16,18,0.72); font-size: 12px; color: #cfcfd2; }
  .ref-dock-label { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dock-keep, .dock-close { background: rgba(255,255,255,0.16); color: #fff; border: none;
    border-radius: 6px; padding: 5px 10px; font-size: 12px; cursor: pointer; }
  .dock-keep:hover, .dock-close:hover { background: rgba(255,255,255,0.3); }

  .spinner {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
    z-index: 22; background: rgba(28,28,30,0.92); color: #fff;
    padding: 12px 20px; border-radius: 10px; font-size: 15px; border: 1px solid #3a3a3c;
  }

  /* bottom toolbar of the viewer pane (nav actions ↔ draw palette) */
  .toolbar {
    position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%);
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    max-width: calc(100% - 24px); padding: 7px 9px; border-radius: 12px;
    background: rgba(24,24,26,0.92); backdrop-filter: blur(8px);
    border: 1px solid #3a3a3c; z-index: 20;
  }
  .toolbar button, .toolbar .ref, .toolbar .note {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    background: #2c2c2e; color: #fff; border: 1px solid #4a4a4c;
    border-radius: 9px; padding: 0 14px; min-height: 44px; font-size: 14px;
    font-family: inherit; line-height: 1; cursor: pointer; box-sizing: border-box;
  }
  .toolbar button:disabled { opacity: 0.4; cursor: default; }
  .toolbar button.primary { background: #0a84ff; border-color: #0a84ff; font-weight: 600; }
  .toolbar .ref { position: relative; overflow: hidden; }
  .toolbar .ref input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
  .toolbar .note { background: #1c1c1e; cursor: text; width: min(40vw, 300px);
    justify-content: flex-start; padding: 0 14px; }
  .colors { display: flex; gap: 6px; }
  .swatch { width: 28px; height: 28px; min-height: 28px; border-radius: 50%;
    padding: 0; border: 2px solid #1c1c1e; cursor: pointer; }
  .swatch.sel { border-color: #fff; }

  /* status bar */
  .statusbar {
    grid-area: status; display: flex; align-items: center; gap: 7px;
    background: #0a84ff; color: #fff; padding: 0 12px; font-size: 12px;
    overflow: hidden; white-space: nowrap;
  }
  .statusbar .sb-sep { opacity: 0.7; }
  .sb-spacer { flex: 1 1 auto; }
  .sb-toggle { background: rgba(255,255,255,0.16); color: #fff; border: none;
    border-radius: 6px; padding: 3px 10px; font-size: 12px; cursor: pointer; }
  .sb-toggle:hover { background: rgba(255,255,255,0.28); }

  .dot { width: 8px; height: 8px; border-radius: 50%; background: #fff; flex: 0 0 auto; }
  .dot.live { background: #30d158; }
  .dot.connecting { background: #ffd60a; }
  .dot.down { background: #ff453a; }

  .toast {
    position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
    z-index: 30; background: rgba(28,28,30,0.95); color: #fff;
    padding: 9px 16px; border-radius: 9px; font-size: 14px; border: 1px solid #3a3a3c;
  }

  /* narrow screens (tablet portrait): sidebar overlays instead of pushing */
  @media (max-width: 720px) {
    .shell, .shell.sb-open { grid-template-columns: 48px 0 1fr; }
    .shell.sb-open .sidebar {
      display: flex; position: absolute; left: 48px; top: 40px; bottom: 26px;
      width: 260px; z-index: 40; box-shadow: 4px 0 16px rgba(0,0,0,0.5);
    }
  }
</style>
