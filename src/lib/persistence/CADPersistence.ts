import * as THREE from 'three';
import type { ViewMode } from '../three/CameraController.js';

export interface CADViewerState {
  model?: {
    payload: string;
    payloadType: 'stl' | '3mf';
    color: string;
  };
  camera: {
    position: { x: number; y: number; z: number };
    target: { x: number; y: number; z: number };
    mode: ViewMode;
    orthographicZoom?: number; // Store zoom level for orthographic camera
  };
  wireframe: boolean;
  grid?: boolean; // optional for back-compat with older saved entries
}

export class CADPersistence {
  private static readonly DEFAULT_STORAGE_KEY = 'cad-viewer-state';

  /**
   * Generate storage key based on identifier
   */
  private static getStorageKey(identifier?: string): string {
    return identifier ? `cad-viewer-state-${identifier}` : this.DEFAULT_STORAGE_KEY;
  }

  /**
   * Save the current state to localStorage
   */
  public static saveState(state: CADViewerState, identifier?: string): void {
    try {
      const key = this.getStorageKey(identifier);
      localStorage.setItem(key, JSON.stringify(state));
    } catch (error) {
      console.warn('Failed to save CAD viewer state:', error);
    }
  }

  /**
   * Load the state from localStorage
   */
  public static loadState(identifier?: string): CADViewerState | null {
    try {
      const key = this.getStorageKey(identifier);
      const stored = localStorage.getItem(key);
      if (stored) {
        return JSON.parse(stored);
      }
    } catch (error) {
      console.warn('Failed to load CAD viewer state:', error);
    }
    return null;
  }

  /**
   * Clear the saved state
   */
  public static clearState(identifier?: string): void {
    try {
      const key = this.getStorageKey(identifier);
      localStorage.removeItem(key);
    } catch (error) {
      console.warn('Failed to clear CAD viewer state:', error);
    }
  }

  /**
   * Create a state object from current scene state
   */
  public static createState(
    camera: THREE.Camera,
    controls: any,
    viewMode: ViewMode,
    wireframe: boolean,
    grid: boolean,
    model?: { payload: string; payloadType: 'stl' | '3mf'; color: string }
  ): CADViewerState {
    // If we have a model, the target should always be (0,0,0) since models are centered
    const target = model ? { x: 0, y: 0, z: 0 } : {
      x: controls.target.x,
      y: controls.target.y,
      z: controls.target.z
    };

    // Capture orthographic zoom if in orthographic mode
    const orthographicZoom = viewMode === 'orthographic' && 'zoom' in camera 
      ? (camera as THREE.OrthographicCamera).zoom 
      : undefined;

    return {
      model,
      camera: {
        position: {
          x: camera.position.x,
          y: camera.position.y,
          z: camera.position.z
        },
        target,
        mode: viewMode,
        orthographicZoom
      },
      wireframe,
      grid,
    };
  }

  /**
   * Apply a saved state to the scene
   */
  public static applyState(
    state: CADViewerState,
    camera: THREE.Camera,
    controls: any
  ): void {
    // Apply camera position and target
    camera.position.set(
      state.camera.position.x,
      state.camera.position.y,
      state.camera.position.z
    );
    
    controls.target.set(
      state.camera.target.x,
      state.camera.target.y,
      state.camera.target.z
    );
    
    // Apply orthographic zoom if available and camera is orthographic
    if (state.camera.orthographicZoom && 'zoom' in camera) {
      const orthoCamera = camera as THREE.OrthographicCamera;
      orthoCamera.zoom = state.camera.orthographicZoom;
      orthoCamera.updateProjectionMatrix();
    }
    
    camera.lookAt(controls.target);
    controls.update();
  }
}
