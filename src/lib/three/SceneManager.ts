import * as THREE from 'three';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js';
import { ThreeMFLoader } from 'three/examples/jsm/loaders/3MFLoader.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { OrientationGizmo, type ViewDirection } from './OrientationGizmo.js';

export type PayloadType = 'stl' | '3mf' | 'glb';

export interface ModelInfo {
  triangleCount: number;
  dimensions: { x: string; y: string; z: string } | null;
}

export class SceneManager {
  public scene: THREE.Scene;
  public renderer!: THREE.WebGLRenderer;
  public controls!: OrbitControls;
  public perspectiveCamera: THREE.PerspectiveCamera;
  public orthographicCamera: THREE.OrthographicCamera;
  public currentCamera: THREE.PerspectiveCamera | THREE.OrthographicCamera;
  public currentMesh: THREE.Mesh | null = null;
  // For GLB the loaded result is a whole scene graph (named nodes); for
  // STL/3MF it's just the single mesh. Annotation picking walks this.
  public currentObject: THREE.Object3D | null = null;
  public gridHelper!: THREE.GridHelper;
  private gridSize = 100;
  private gridDivisions = 100;
  private orientationGizmo: OrientationGizmo | null = null;
  private gizmoScale: number = 1.0; // Store the gizmo scale multiplier
  private gridVisible: boolean = true; // toolbar toggle; survives grid recreation
  private headLight!: THREE.DirectionalLight;
  private headLightTarget!: THREE.Object3D;

  private stlLoader = new STLLoader();
  private threeMFLoader = new ThreeMFLoader();
  private gltfLoader = new GLTFLoader();
  private raycaster = new THREE.Raycaster();
  private animationFrameId: number | null = null;

  constructor(
    container: HTMLElement,
    backgroundColor: string,
    gridColor: string,
    gridCenterLineColor: string,
    gizmoScale: number = 1.0
  ) {
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(backgroundColor);
    this.gizmoScale = gizmoScale; // Store the gizmo scale

    // Initialize cameras
    const aspect = container.clientWidth / container.clientHeight;
    this.perspectiveCamera = new THREE.PerspectiveCamera(75, aspect, 0.1, 1000);
    this.orthographicCamera = new THREE.OrthographicCamera(-aspect, aspect, 1, -1, 0.1, 1000);
    this.currentCamera = this.perspectiveCamera;
    this.currentCamera.position.z = 10;

    // Setup scene elements
    this.setupGrid(gridColor, gridCenterLineColor);
    this.setupLighting();
    this.setupRenderer(container);
    this.setupControls();
    this.setupOrientationGizmo(container);
  }

  private setupGrid(gridColor: string, gridCenterLineColor: string) {
    this.gridHelper = new THREE.GridHelper(this.gridSize, this.gridDivisions, gridColor, gridCenterLineColor);
    this.gridHelper.visible = this.gridVisible;
    this.scene.add(this.gridHelper);
  }

  public setGridVisible(visible: boolean) {
    this.gridVisible = visible;
    if (this.gridHelper) this.gridHelper.visible = visible;
  }

  public isGridVisible(): boolean {
    return this.gridVisible;
  }

  private setupLighting() {
    // Strong uniform fill so NO face ever falls into dark shadow, regardless
    // of model orientation (build123d Z-up vs three.js Y-up doesn't matter).
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.9));

    // One subtle side light just for edge readability (definition without shadow).
    const side = new THREE.DirectionalLight(0xffffff, 0.25);
    side.position.set(5, 10, 7.5);
    this.scene.add(side);

    // "Headlight": a directional light parented to the active camera. It
    // shines along the camera's view direction → whatever you look at is
    // always lit, never a dark backside. Cameras must be in the scene graph
    // for child transforms to propagate.
    this.scene.add(this.perspectiveCamera);
    this.scene.add(this.orthographicCamera);
    this.headLightTarget = new THREE.Object3D();
    this.headLightTarget.position.set(0, 0, -1); // 1 unit in front of camera
    this.headLight = new THREE.DirectionalLight(0xffffff, 0.6);
    this.headLight.position.set(0, 0, 0);
    this.headLight.target = this.headLightTarget;
    this.attachHeadlightTo(this.currentCamera);
  }

  private attachHeadlightTo(cam: THREE.Camera) {
    this.headLight.parent?.remove(this.headLight);
    this.headLightTarget.parent?.remove(this.headLightTarget);
    cam.add(this.headLight);
    cam.add(this.headLightTarget);
  }

  private setupRenderer(container: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({
      antialias: true,
      preserveDrawingBuffer: true,
    });
    this.renderer.setSize(container.clientWidth, container.clientHeight);
    container.appendChild(this.renderer.domElement);
  }

  private setupControls() {
    this.controls = new OrbitControls(this.currentCamera, this.renderer.domElement);
    this.controls.enableDamping = true;
  }

  private setupOrientationGizmo(container: HTMLElement) {
    const baseSize = 240; // Base size in pixels
    const scaledSize = Math.round(baseSize * this.gizmoScale);
    
    this.orientationGizmo = new OrientationGizmo(container, this.currentCamera, {
      position: 'bottom-right',
      size: scaledSize, // Apply scale multiplier to the base size
      margin: 20
    });
    
    // Handle axis clicks: re-orient ONLY — keep the current orbit target and
    // the current camera distance (don't snap-zoom on every cube click).
    this.orientationGizmo.onAxisClick = (direction: THREE.Vector3) => {
      const center = this.controls.target.clone();
      const distance = this.currentCamera.position.distanceTo(center) || 1;

      this.currentCamera.position.copy(
        center.clone().add(direction.clone().normalize().multiplyScalar(distance))
      );
      this.currentCamera.lookAt(center);
      this.controls.update();
    };
  }

  private setViewDirection(direction: ViewDirection) {
    if (!this.currentMesh) return;
    
    const distance = 50; // Distance from object
    const position = new THREE.Vector3();
    
    switch (direction) {
      case 'front':
        position.set(0, 0, distance);
        break;
      case 'back':
        position.set(0, 0, -distance);
        break;
      case 'right':
        position.set(distance, 0, 0);
        break;
      case 'left':
        position.set(-distance, 0, 0);
        break;
      case 'top':
        position.set(0, distance, 0);
        break;
      case 'bottom':
        position.set(0, -distance, 0);
        break;
    }
    
    // Get the center of the current mesh
    const box = new THREE.Box3().setFromObject(this.currentMesh);
    const center = box.getCenter(new THREE.Vector3());
    
    // Set camera position relative to model center
    this.currentCamera.position.copy(center.clone().add(position));
    this.currentCamera.lookAt(center);
    this.controls.target.copy(center);
    this.controls.update();
  }

  public startAnimation() {
    const animate = () => {
      this.animationFrameId = requestAnimationFrame(animate);
      this.controls.update();
      this.renderer.render(this.scene, this.currentCamera);
      
      // Update orientation gizmo
      if (this.orientationGizmo) {
        this.orientationGizmo.update();
      }
    };
    animate();
  }

  public updateBackgroundColor(color: string) {
    this.scene.background = new THREE.Color(color);
  }

  public updateCurrentCamera(camera: THREE.PerspectiveCamera | THREE.OrthographicCamera) {
    this.currentCamera = camera;
    if (this.headLight) this.attachHeadlightTo(camera); // headlight follows the active cam

    // Update orientation gizmo to use new camera
    if (this.orientationGizmo) {
      this.orientationGizmo.updateMainCamera(camera);
    }
  }

  public updateGizmoScale(scale: number) {
    if (scale <= 0) return; // Prevent invalid scales
    
    this.gizmoScale = scale;
    
    // Recreate the gizmo with the new scale
    if (this.orientationGizmo) {
      this.orientationGizmo.dispose();
      this.orientationGizmo = null;
      
      // Find the container element (parent of the renderer canvas)
      const container = this.renderer.domElement.parentElement;
      if (container) {
        this.setupOrientationGizmo(container);
      }
    }
  }

  public updateGrid(gridColor: string, gridCenterLineColor: string) {
    this.gridHelper.material.dispose();
    this.scene.remove(this.gridHelper);
    this.gridHelper = new THREE.GridHelper(
      this.gridSize,
      this.gridDivisions,
      gridColor,
      gridCenterLineColor
    );
    this.gridHelper.visible = this.gridVisible;
    this.scene.add(this.gridHelper);
  }

  public updateGridSize(size: number) {
    this.gridSize = Math.max(size * 1.25, 20);
    this.scene.remove(this.gridHelper);
    this.gridHelper.dispose();

    this.gridHelper = new THREE.GridHelper(
      this.gridSize,
      this.gridDivisions,
      this.gridHelper.material.color,
      this.gridHelper.material.color
    );
    this.gridHelper.visible = this.gridVisible;
    this.scene.add(this.gridHelper);
  }

  public loadModel(payload: string, payloadType: 'stl' | '3mf', color: string): ModelInfo {
    this.clearCurrentModel();

    try {
      const meshes = this.parseModel(payload, payloadType, color);
      if (meshes.length > 0) {
        this.currentMesh = meshes[0];
        return this.calculateModelInfo(meshes);
      }
    } catch (error) {
      console.error(`Failed to parse ${payloadType.toUpperCase()}:`, error);
    }

    return { triangleCount: 0, dimensions: null };
  }

  private parseModel(payload: string, payloadType: string, color: string): THREE.Mesh[] {
    const meshes: THREE.Mesh[] = [];

    if (payloadType === 'stl') {
      const geometry = this.stlLoader.parse(payload);
      const material = new THREE.MeshStandardMaterial({
        color,
        metalness: 0.1,
        roughness: 0.75,
      });
      const mesh = new THREE.Mesh(geometry, material);
      this.scene.add(mesh);
      meshes.push(mesh);
    } else if (payloadType === '3mf') {
      const arrayBuffer = new TextEncoder().encode(payload).buffer;
      const object = this.threeMFLoader.parse(arrayBuffer);
      this.addMeshesRecursively(object, meshes);
    }

    return meshes;
  }

  private addMeshesRecursively(obj: any, meshes: THREE.Mesh[]) {
    if (obj.isMesh) {
      this.scene.add(obj);
      meshes.push(obj);
    }
    if (obj.children && obj.children.length) {
      obj.children.forEach((child: any) => this.addMeshesRecursively(child, meshes));
    }
  }

  private calculateModelInfo(meshes: THREE.Mesh[]): ModelInfo {
    if (!this.currentMesh) return { triangleCount: 0, dimensions: null };

    const boundingBox = new THREE.Box3().setFromObject(this.currentMesh);
    const size = boundingBox.getSize(new THREE.Vector3());
    const maxSize = Math.max(size.x, size.y, size.z);
    this.updateGridSize(maxSize);
    const triangleCount = meshes.reduce(
      (sum, m) => sum + (m.geometry.attributes.position.count / 3), 
      0
    );

    return {
      triangleCount,
      dimensions: {
        x: size.x.toFixed(2),
        y: size.y.toFixed(2),
        z: size.z.toFixed(2)
      }
    };
  }

  public clear() { this.clearCurrentModel(); }

  private clearCurrentModel() {
    if (this.currentMesh) {
      this.scene.remove(this.currentMesh);
      this.currentMesh.geometry.dispose();
      if (Array.isArray(this.currentMesh.material)) {
        this.currentMesh.material.forEach(m => m.dispose());
      } else {
        this.currentMesh.material.dispose();
      }
      this.currentMesh = null;
    }
    if (this.currentObject) {
      this.scene.remove(this.currentObject);
      this.currentObject.traverse((o) => {
        const m = o as THREE.Mesh;
        if (m.isMesh) {
          m.geometry?.dispose();
          const mat = m.material;
          if (Array.isArray(mat)) mat.forEach(x => x.dispose());
          else mat?.dispose();
        }
      });
      this.currentObject = null;
    }
  }

  public resize(width: number, height: number) {
    if (width === 0 || height === 0) return;

    this.renderer.setSize(width, height);
    const aspect = width / height;

    if ('isPerspectiveCamera' in this.currentCamera && this.currentCamera.isPerspectiveCamera) {
      (this.currentCamera as THREE.PerspectiveCamera).aspect = aspect;
    } else {
      const frustumHeight = (this.currentCamera as THREE.OrthographicCamera).top - 
                           (this.currentCamera as THREE.OrthographicCamera).bottom;
      const orthoCamera = this.currentCamera as THREE.OrthographicCamera;
      orthoCamera.left = -frustumHeight * aspect / 2;
      orthoCamera.right = frustumHeight * aspect / 2;
    }
    
    this.currentCamera.updateProjectionMatrix();
  }

  /**
   * Load a model from raw bytes. GLB keeps its scene graph + node names
   * (so annotation can say "you circled node X") and its authored colours;
   * STL/3MF binary reuse the existing single-mesh material. Additive — the
   * string `loadModel()` path used by <cad-viewer>'s `payload` is untouched.
   */
  public async loadModelFromBuffer(
    buffer: ArrayBuffer,
    payloadType: PayloadType,
    color: string
  ): Promise<ModelInfo> {
    this.clearCurrentModel();
    const meshes: THREE.Mesh[] = [];

    if (payloadType === 'glb') {
      const gltf = await new Promise<any>((resolve, reject) =>
        this.gltfLoader.parse(buffer, '', resolve, reject)
      );
      const root: THREE.Object3D = gltf.scene || gltf.scenes?.[0];
      root.traverse((o) => {
        const m = o as THREE.Mesh;
        if (m.isMesh) {
          m.castShadow = m.receiveShadow = false;
          meshes.push(m);
        }
      });
      this.scene.add(root);
      this.currentObject = root;
      this.currentMesh = meshes[0] ?? null;
    } else if (payloadType === 'stl') {
      const geometry = this.stlLoader.parse(buffer);
      const material = new THREE.MeshStandardMaterial({
        color,
        metalness: 0.1,
        roughness: 0.75,
      });
      const mesh = new THREE.Mesh(geometry, material);
      this.scene.add(mesh);
      meshes.push(mesh);
      this.currentMesh = mesh;
      this.currentObject = mesh;
    } else {
      const object = this.threeMFLoader.parse(buffer);
      this.addMeshesRecursively(object, meshes);
      this.scene.add(object);
      this.currentObject = object;
      this.currentMesh = meshes[0] ?? null;
    }

    if (meshes.length === 0) return { triangleCount: 0, dimensions: null };
    return this.calculateModelInfo(meshes);
  }

  /** Re-render this frame and return it as a PNG data URL (for compositing). */
  public captureCanvasDataURL(): string {
    this.renderer.render(this.scene, this.currentCamera);
    return this.renderer.domElement.toDataURL('image/png');
  }

  public getCanvas(): HTMLCanvasElement {
    return this.renderer.domElement;
  }

  /** Freeze/unfreeze 3D navigation (used while the user is drawing). */
  public setControlsEnabled(enabled: boolean) {
    if (this.controls) this.controls.enabled = enabled;
  }

  /**
   * Raycast a screen point against the model and return the name of the
   * nearest named node (GLB nodes carry their build123d label). null if the
   * point misses or the format has no node names (STL).
   */
  public pickNodeAt(clientX: number, clientY: number): string | null {
    const target = this.currentObject ?? this.currentMesh;
    if (!target) return null;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1
    );
    this.raycaster.setFromCamera(ndc, this.currentCamera);
    const hits = this.raycaster.intersectObject(target, true);
    if (hits.length === 0) return null;
    let o: THREE.Object3D | null = hits[0].object;
    while (o) {
      if (o.name && o !== this.currentObject) return o.name;
      o = o.parent;
    }
    return null;
  }

  public exportScreenshot() {
    const dataURL = this.captureCanvasDataURL();
    const link = document.createElement('a');
    link.href = dataURL;
    link.download = 'screenshot.png';
    link.click();
  }

  public dispose() {
    if (this.animationFrameId) {
      cancelAnimationFrame(this.animationFrameId);
    }
    this.clearCurrentModel();
    this.controls.dispose();
    this.renderer.dispose();
    
    // Dispose orientation gizmo
    if (this.orientationGizmo) {
      this.orientationGizmo.dispose();
      this.orientationGizmo = null;
    }
  }
}
