import * as THREE from 'three';
import type { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';

export type ViewMode = 'perspective' | 'orthographic';

export class CameraController {
  public viewMode: ViewMode = 'perspective';

  constructor(
    private perspectiveCamera: THREE.PerspectiveCamera,
    private orthographicCamera: THREE.OrthographicCamera,
    private controls: OrbitControls,
    private container: HTMLElement
  ) {}

  get currentCamera(): THREE.PerspectiveCamera | THREE.OrthographicCamera {
    return this.viewMode === 'perspective' ? this.perspectiveCamera : this.orthographicCamera;
  }

  public toggleViewMode(): ViewMode {
    const oldCamera = this.currentCamera;
    const oldPosition = oldCamera.position.clone();
    const oldTarget = this.controls.target.clone();
    
    this.viewMode = this.viewMode === 'perspective' ? 'orthographic' : 'perspective';
    const newCamera = this.currentCamera;
    
    // Transfer position and target to the new camera
    newCamera.position.copy(oldPosition);
    newCamera.lookAt(oldTarget);
    
    // Calculate equivalent zoom/distance for different camera types
    if (this.viewMode === 'orthographic') {
      // Switching TO orthographic - calculate appropriate zoom
      const distance = oldPosition.distanceTo(oldTarget);
      const fov = this.perspectiveCamera.fov * (Math.PI / 180);
      const height = 2 * Math.tan(fov / 2) * distance;
      
      // Set orthographic camera bounds based on calculated height
      const aspect = this.container.clientWidth / this.container.clientHeight;
      const orthoHeight = height;
      const orthoWidth = orthoHeight * aspect;
      
      this.orthographicCamera.left = -orthoWidth / 2;
      this.orthographicCamera.right = orthoWidth / 2;
      this.orthographicCamera.top = orthoHeight / 2;
      this.orthographicCamera.bottom = -orthoHeight / 2;
      this.orthographicCamera.zoom = 1;
      this.orthographicCamera.updateProjectionMatrix();
    } else {
      // Switching TO perspective - just transfer position (perspective is distance-based)
      // No special calculations needed as distance naturally controls the view size
    }
    
    this.controls.object = newCamera;
    this.controls.target.copy(oldTarget);
    this.controls.update();
    
    return this.viewMode;
  }

  public setViewMode(mode: ViewMode): void {
    if (this.viewMode === mode) return; // No change needed
    
    const oldCamera = this.currentCamera;
    const oldPosition = oldCamera.position.clone();
    const oldTarget = this.controls.target.clone();
    
    this.viewMode = mode;
    const newCamera = this.currentCamera;
    
    // Transfer position and target to the new camera
    newCamera.position.copy(oldPosition);
    newCamera.lookAt(oldTarget);
    
    // Calculate equivalent zoom/distance for different camera types
    if (this.viewMode === 'orthographic') {
      // Switching TO orthographic - calculate appropriate zoom
      const distance = oldPosition.distanceTo(oldTarget);
      const fov = this.perspectiveCamera.fov * (Math.PI / 180);
      const height = 2 * Math.tan(fov / 2) * distance;
      
      // Set orthographic camera bounds based on calculated height
      const aspect = this.container.clientWidth / this.container.clientHeight;
      const orthoHeight = height;
      const orthoWidth = orthoHeight * aspect;
      
      this.orthographicCamera.left = -orthoWidth / 2;
      this.orthographicCamera.right = orthoWidth / 2;
      this.orthographicCamera.top = orthoHeight / 2;
      this.orthographicCamera.bottom = -orthoHeight / 2;
      this.orthographicCamera.zoom = 1;
      this.orthographicCamera.updateProjectionMatrix();
    }
    
    this.controls.object = newCamera;
    this.controls.target.copy(oldTarget);
    this.controls.update();
  }

  public frameToObject(mesh: THREE.Object3D, resetPosition = true) {
    const boundingBox = new THREE.Box3().setFromObject(mesh);
    const center = boundingBox.getCenter(new THREE.Vector3());
    const size = boundingBox.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z);

    // Scale clip planes to the model so small parts don't clip when you
    // zoom in close (default near=0.1 clips ~mm-scale features).
    const near = Math.max(maxDim / 500, 0.01);
    const far = Math.max(maxDim * 50, 1000);
    this.perspectiveCamera.near = near;
    this.perspectiveCamera.far = far;
    this.perspectiveCamera.updateProjectionMatrix();
    this.orthographicCamera.near = near;
    this.orthographicCamera.far = far;
    this.orthographicCamera.updateProjectionMatrix();

    // Center the model
    mesh.position.sub(center);

    // Camera fit logic
    const camDir = new THREE.Vector3();
    this.currentCamera.getWorldDirection(camDir);
    this.controls.target.set(0, 0, 0);

    let fitDist: number;
    if (this.viewMode === 'perspective') {
      const fov = this.perspectiveCamera.fov * (Math.PI / 180);
      fitDist = (maxDim / 2) / Math.tan(fov / 2) * 1.5;
    } else {
      const aspect = this.container.clientWidth / this.container.clientHeight;
      const camHeight = maxDim * 1.2;
      const camWidth = camHeight * aspect;
      
      this.orthographicCamera.left = -camWidth / 2;
      this.orthographicCamera.right = camWidth / 2;
      this.orthographicCamera.top = camHeight / 2;
      this.orthographicCamera.bottom = -camHeight / 2;
      this.orthographicCamera.zoom = 1;
      this.orthographicCamera.updateProjectionMatrix();
      
      fitDist = maxDim * 1.5;
    }

    this.currentCamera.position.copy(camDir.multiplyScalar(-fitDist));
    this.currentCamera.lookAt(0, 0, 0);

    this.controls.object = this.currentCamera;
    this.controls.update();
  }
}
