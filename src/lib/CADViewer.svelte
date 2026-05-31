<svelte:options customElement="cad-viewer" />

<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import * as THREE from 'three';
  import { SceneManager, CameraController, ModelOperations, type ModelInfo, type ViewMode } from './three/index.js';
  import { CADPersistence, type CADViewerState } from './persistence/CADPersistence.js';
  import Toolbar from './components/Toolbar.svelte';
  import InfoPanel from './components/InfoPanel.svelte';

  // --- Props ---
  export let payload: string = '';
  export let payloadType: 'stl' | '3mf' | 'glb' = 'stl';
  export let color = '#fca503';
  // When set, the viewer fetches this URL as binary and loads it (GLB/STL/3MF).
  // Used by <cad-studio> for the live build123d model; the string `payload`
  // path is left exactly as-is for existing consumers (vancad, vscode-openscad).
  export let modelUrl: string = '';
  export let viewerBackgroundColor = '#1e1e1e';
  export let gridColor = '#888888';
  export let gridCenterLineColor = '#444444';
  export let gizmoScale = 1.0; // Multiplier for orientation gizmo size (1.0 = default size)
  export let persistenceId: string = ''; // Unique identifier for persistence storage

  // Ensure gizmoScale is always a number (custom elements pass attributes as strings)
  $: gizmoScaleNumber = typeof gizmoScale === 'string' ? parseFloat(gizmoScale) || 1.0 : gizmoScale;

  // Hide the built-in Toolbar / InfoPanel when the host provides its own chrome
  // (e.g. <cad-studio>'s unified top bar). Default true → embeddable consumers
  // (vancad, vscode-openscad) keep the original overlays.
  export let showToolbar = true;
  export let showInfoPanel = true;

  // --- Themeable properties ---
  export let toolbarBackgroundColor = 'rgba(42, 42, 42, 0.8)';
  export let toolbarButtonBackgroundColor = '#444';
  export let toolbarButtonHoverBackgroundColor = '#555';
  export let toolbarButtonForegroundColor = 'white';
  export let toolbarButtonBorderColor = '#666';
  export let infoPanelBackgroundColor = 'rgba(42, 42, 42, 0.8)';
  export let infoPanelForegroundColor = '#eee';
  export let infoPanelSpanBackgroundColor = '#444';

  // --- Component State ---
  let container: HTMLElement;
  let sceneManager: SceneManager;
  let cameraController: CameraController;
  let resizeObserver: ResizeObserver;
  let modelInfo: ModelInfo = { triangleCount: 0, dimensions: null };
  let viewMode: ViewMode = 'perspective';
  let isWireframeMode = false;
  let gridVisible = true;
  let currentModelData: { payload: string; payloadType: 'stl' | '3mf' | 'glb'; color: string } | null = null;
  let isInitialized = false;
  let currentModelUrl: string | null = null;
  let urlLoadSeq = 0;
  let pendingLoad: { url: string; resolve: () => void; reject: (e: unknown) => void } | null = null;

  onMount(() => {
    initializeViewer();
    setupResizeObserver();
    
    // Load saved state first
    loadSavedState();
    
    // If no saved model and payload is provided, load it
    if (!currentModelData && payload) {
      loadModel();
    }
    
    isInitialized = true;
  });

  onDestroy(() => {
    cleanup();
  });

  function initializeViewer() {
    sceneManager = new SceneManager(container, viewerBackgroundColor, gridColor, gridCenterLineColor, gizmoScaleNumber);
    cameraController = new CameraController(
      sceneManager.perspectiveCamera,
      sceneManager.orthographicCamera,
      sceneManager.controls,
      container
    );
    sceneManager.updateCurrentCamera(cameraController.currentCamera);
    sceneManager.startAnimation();
    setupCameraChangeListeners();
  }

  function setupResizeObserver() {
    const handleResize = () => {
      if (!container) return;
      const { clientWidth: width, clientHeight: height } = container;
      sceneManager.resize(width, height);
    };

    resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(container);
  }

  function loadModel() {
    currentModelData = { payload, payloadType, color };
    modelInfo = sceneManager.loadModel(payload, payloadType, color);
    if (sceneManager.currentMesh) {
      cameraController.frameToObject(sceneManager.currentMesh, false);
      // Update wireframe state from mesh
      isWireframeMode = ModelOperations.getWireframeState(sceneManager.currentMesh);
      
      // Ensure the camera target is at origin after framing
      sceneManager.controls.target.set(0, 0, 0);
      sceneManager.controls.update();
    }
    
    // Save state after loading model
    // Force save so that when payload changes we immediately update persisted model
    saveCurrentState(true);
  }

  function loadSavedState() {
    const savedState = CADPersistence.loadState(persistenceId || undefined);
    if (!savedState) return;

    console.log('Loading saved state for ID:', persistenceId || 'default', savedState);

    // Restore camera mode first
    if (savedState.camera.mode !== viewMode) {
      viewMode = savedState.camera.mode;
      cameraController.setViewMode(viewMode);
      sceneManager.updateCurrentCamera(cameraController.currentCamera);
      sceneManager.controls.object = cameraController.currentCamera;
    }

    // If a payload prop was provided externally and it differs from the saved model,
    // prefer the incoming payload (do NOT restore the saved model). This ensures
    // the component behaves predictably when a parent updates the payload.
    const hasIncomingPayload = !!payload;
    const savedModel = savedState.model;

    if (savedModel && (!hasIncomingPayload || (hasIncomingPayload && savedModel.payload === payload && savedModel.payloadType === payloadType))) {
      // Restore model from saved state
      currentModelData = savedModel;
      modelInfo = sceneManager.loadModel(
        savedModel.payload,
        savedModel.payloadType,
        savedModel.color
      );
      
      // Model is loaded and centered, now apply saved camera state
      if (sceneManager.currentMesh) {
        // Center the model manually (just to be sure)
        const boundingBox = new THREE.Box3().setFromObject(sceneManager.currentMesh);
        const center = boundingBox.getCenter(new THREE.Vector3());
        sceneManager.currentMesh.position.sub(center);
        
        console.log('Model centered at:', sceneManager.currentMesh.position);
        console.log('Restoring camera to:', savedState.camera);
        
        // Manually apply camera state with proper handling for orthographic cameras
        cameraController.currentCamera.position.set(
          savedState.camera.position.x,
          savedState.camera.position.y,
          savedState.camera.position.z
        );
        
        sceneManager.controls.target.set(0, 0, 0); // Always target origin for models
        
        // Apply orthographic zoom if available
        if (savedState.camera.orthographicZoom && viewMode === 'orthographic') {
          const orthoCamera = cameraController.currentCamera as THREE.OrthographicCamera;
          orthoCamera.zoom = savedState.camera.orthographicZoom;
          orthoCamera.updateProjectionMatrix();
        }
        
        cameraController.currentCamera.lookAt(0, 0, 0);
        sceneManager.controls.update();
      }
    } else {
      // Do not restore the saved model because an incoming payload was provided
      // and differs; still apply the saved camera (useful when no model is present)
      CADPersistence.applyState(savedState, cameraController.currentCamera, sceneManager.controls);
    }

    // Restore wireframe mode
    if (sceneManager.currentMesh) {
      ModelOperations.setWireframeState(sceneManager.currentMesh, savedState.wireframe);
      isWireframeMode = savedState.wireframe;
    }

    // Restore grid visibility (default: visible, for back-compat)
    const savedGrid = savedState.grid ?? true;
    sceneManager.setGridVisible(savedGrid);
    gridVisible = savedGrid;
  }

  function saveCurrentState(force: boolean = false) {
    if (!sceneManager || !cameraController || (!isInitialized && !force)) return;

    const state = CADPersistence.createState(
      cameraController.currentCamera,
      sceneManager.controls,
      viewMode,
      isWireframeMode,
      gridVisible,
      currentModelData || undefined
    );

    console.log("saving state: ", persistenceId);

    CADPersistence.saveState(state, persistenceId || undefined);
  }

  function setupCameraChangeListeners() {
    if (!sceneManager?.controls) return;
    
    // Save state when camera changes
    sceneManager.controls.addEventListener('end', () => {
      if (isInitialized) {
        saveCurrentState();
      }
    });
  }

  function cleanup() {
    resizeObserver?.disconnect();
    sceneManager?.dispose();
  }

  // --- Toolbar Handlers ---
  function handleToggleViewMode() {
    viewMode = cameraController.toggleViewMode();
    sceneManager.updateCurrentCamera(cameraController.currentCamera);
    sceneManager.controls.object = sceneManager.currentCamera;
    
    // The camera controller handles position transfer, we just need to update the controls
    sceneManager.controls.update();
    
    // Save state after view mode change
    saveCurrentState();
  }

  function handleToggleWireframe() {
    ModelOperations.toggleWireframe(sceneManager.currentMesh);
    isWireframeMode = ModelOperations.getWireframeState(sceneManager.currentMesh);
    
    // Save state after wireframe change
    saveCurrentState();
  }

  function handleExportPNG() {
    sceneManager.exportScreenshot();
  }

  function handleToggleGrid() {
    gridVisible = !gridVisible;
    sceneManager.setGridVisible(gridVisible);
    if (isInitialized) saveCurrentState();
  }

  // --- Public API for controlling persistence ---
  export function clearSavedState() {
    CADPersistence.clearState(persistenceId || undefined);
  }

  export function getSavedState() {
    return CADPersistence.loadState(persistenceId || undefined);
  }

  // --- Binary model loading (used by <cad-studio>) ---
  function inferType(url: string): 'stl' | '3mf' | 'glb' {
    const u = url.split('?')[0].toLowerCase();
    if (u.endsWith('.stl')) return 'stl';
    if (u.endsWith('.3mf')) return '3mf';
    return 'glb';
  }

  function applyPersistedView() {
    if (!sceneManager || !cameraController) return;
    const saved = CADPersistence.loadState(persistenceId || undefined);
    if (!saved) return;
    if (saved.camera.mode !== viewMode) {
      viewMode = saved.camera.mode;
      cameraController.setViewMode(viewMode);
      sceneManager.updateCurrentCamera(cameraController.currentCamera);
      sceneManager.controls.object = cameraController.currentCamera;
    }
    CADPersistence.applyState(saved, cameraController.currentCamera, sceneManager.controls);
    if (sceneManager.currentMesh) {
      ModelOperations.setWireframeState(sceneManager.currentMesh, saved.wireframe);
      isWireframeMode = saved.wireframe;
    }
    const g = saved.grid ?? true;
    sceneManager.setGridVisible(g);
    gridVisible = g;
  }

  function _settleLoad(url: string, ok: boolean, err?: unknown) {
    if (pendingLoad && pendingLoad.url === url) {
      if (ok) pendingLoad.resolve();
      else pendingLoad.reject(err);
      pendingLoad = null;
    }
  }

  async function loadFromUrl() {
    if (!sceneManager || !modelUrl) return;
    const seq = ++urlLoadSeq;
    const url = modelUrl;
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) {
        console.warn('cad-viewer: model fetch failed', res.status);
        _settleLoad(url, false, new Error('HTTP ' + res.status));
        return;
      }
      const fmt = (res.headers.get('X-Model-Format') as 'stl' | '3mf' | 'glb')
        || inferType(url);
      const buf = await res.arrayBuffer();
      if (seq !== urlLoadSeq) return; // a newer load superseded this one
      modelInfo = await sceneManager.loadModelFromBuffer(buf, fmt, color);
      const obj = sceneManager.currentObject ?? sceneManager.currentMesh;
      if (obj) {
        cameraController.frameToObject(obj, false);
        sceneManager.controls.target.set(0, 0, 0);
        sceneManager.controls.update();
        isWireframeMode = ModelOperations.getWireframeState(sceneManager.currentMesh);
        // Restore camera / wireframe / grid for THIS part's persistenceId.
        applyPersistedView();
      }
      currentModelUrl = url;
      _settleLoad(url, true);
    } catch (err) {
      console.error('cad-viewer: loadFromUrl error', err);
      _settleLoad(url, false, err);
    }
  }

  // --- Public API consumed by <cad-studio> on the custom element instance ---
  // Resolves when the GLB has actually loaded into the scene (or rejects on
  // fetch/parse failure). Lets the studio drive a loading overlay precisely.
  export function loadModelUrl(url: string): Promise<void> {
    return new Promise((resolve, reject) => {
      // Supersede any in-flight load so its caller resolves (with an error
      // it can ignore) instead of hanging on a never-settled promise.
      if (pendingLoad) pendingLoad.reject(new Error('superseded'));
      pendingLoad = { url, resolve, reject };
      modelUrl = url; // triggers the reactive → loadFromUrl()
    });
  }

  export function clearScene() {
    sceneManager?.clear();
  }

  // Overlay a previous build (translucent) for visual diff. Additive: the
  // current model is untouched. clearGhost() removes it.
  export async function loadGhostUrl(url: string): Promise<void> {
    if (!sceneManager) return;
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const fmt = (res.headers.get('X-Model-Format') as 'stl' | '3mf' | 'glb') || inferType(url);
    await sceneManager.loadGhostFromBuffer(await res.arrayBuffer(), fmt);
  }

  export function clearGhost() {
    sceneManager?.clearGhost();
  }

  // Dimension box: wireframe bounds + X/Y/Z size labels (mm). Additive.
  export function setDimensionsVisible(visible: boolean) {
    sceneManager?.setDimensionsVisible(visible);
  }
  export function isDimensionsVisible(): boolean {
    return sceneManager?.isDimensionsVisible() ?? false;
  }

  // View controls re-exposed so a host (cad-studio) can drive them from its
  // own top bar instead of the built-in Toolbar.
  export function toggleViewMode() { handleToggleViewMode(); return viewMode; }
  export function toggleWireframe() { handleToggleWireframe(); return isWireframeMode; }
  export function toggleGrid() { handleToggleGrid(); return gridVisible; }
  export function exportPNG() { handleExportPNG(); }
  export function getViewState() {
    return { viewMode, wireframe: isWireframeMode, grid: gridVisible };
  }

  export function captureViewPNG(): string {
    return sceneManager ? sceneManager.captureCanvasDataURL() : '';
  }

  export function pickNodeAt(clientX: number, clientY: number): string | null {
    return sceneManager ? sceneManager.pickNodeAt(clientX, clientY) : null;
  }

  export function setNavigationEnabled(enabled: boolean) {
    sceneManager?.setControlsEnabled(enabled);
  }

  export function getViewerCanvas(): HTMLCanvasElement | null {
    return sceneManager ? sceneManager.getCanvas() : null;
  }

  // --- Reactive Statements ---
  $: if (sceneManager && modelUrl && modelUrl !== currentModelUrl) {
    loadFromUrl();
  }
  $: if (sceneManager && payload) {
    // Check if payload has changed compared to current model data
    const hasChanged = !currentModelData || 
                      currentModelData.payload !== payload || 
                      currentModelData.payloadType !== payloadType;
    
    if (hasChanged) {
      loadModel();
    }
  }
  
  $: if (sceneManager && container) {
    const computedStyle = getComputedStyle(container);
    sceneManager.updateBackgroundColor(computedStyle.backgroundColor);
  }

  $: if (sceneManager) {
    sceneManager.updateGrid(gridColor, gridCenterLineColor);
  }

  $: if (sceneManager?.currentMesh && currentModelData) {
    ModelOperations.updateMeshColor(sceneManager.currentMesh, currentModelData.color);
    // Update current model data color and save state
    if (currentModelData.color !== color) {
      currentModelData = { ...currentModelData, color };
      if (isInitialized) {
        saveCurrentState();
      }
    }
  }

  $: if (sceneManager && gizmoScaleNumber) {
    sceneManager.updateGizmoScale(gizmoScaleNumber);
  }
</script>

<style>
  .stl-viewer-host {
    position: relative;
    height: 100%;
    width: 100%;
    overflow: hidden;
  }
  
  .viewer-container {
    width: 100%;
    height: 100%;
    background-color: var(--viewer-background-color);
  }
</style>

<div 
  class="stl-viewer-host"
  style="--viewer-background-color: {viewerBackgroundColor};"
>
  <div class="viewer-container" bind:this={container}></div>

  {#if showToolbar}
    <Toolbar
      {viewMode}
      onToggleViewMode={handleToggleViewMode}
      onToggleWireframe={handleToggleWireframe}
      onToggleGrid={handleToggleGrid}
      {gridVisible}
      onExportPNG={handleExportPNG}
      {toolbarBackgroundColor}
      {toolbarButtonBackgroundColor}
      {toolbarButtonHoverBackgroundColor}
      {toolbarButtonForegroundColor}
      {toolbarButtonBorderColor}
    />
  {/if}

  {#if showInfoPanel}
    <InfoPanel
      {modelInfo}
      {infoPanelBackgroundColor}
      {infoPanelForegroundColor}
      {infoPanelSpanBackgroundColor}
    />
  {/if}
</div>
