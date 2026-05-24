import { getStroke } from 'perfect-freehand';

export interface InkPoint {
  x: number;
  y: number;
  pressure: number;
}

export interface Stroke {
  points: InkPoint[];
  color: string;
}

const STROKE_OPTIONS = {
  size: 7,
  thinning: 0.6,
  smoothing: 0.5,
  streamline: 0.5,
  simulatePressure: true,
};

/**
 * Turn a captured pointer stroke into a filled Path2D outline.
 * perfect-freehand gives a smoothed, pressure-aware polygon; we close it.
 */
export function strokeToPath2D(points: InkPoint[]): Path2D {
  const outline = getStroke(
    points.map((p) => [p.x, p.y, p.pressure]),
    STROKE_OPTIONS
  ) as number[][];

  const path = new Path2D();
  if (outline.length === 0) return path;
  path.moveTo(outline[0][0], outline[0][1]);
  for (let i = 1; i < outline.length; i++) {
    path.lineTo(outline[i][0], outline[i][1]);
  }
  path.closePath();
  return path;
}

/** Pressure from a PointerEvent: real stylus reports it; touch/mouse → 0.5. */
export function pressureOf(e: PointerEvent): number {
  if (e.pointerType === 'pen' && e.pressure > 0) return e.pressure;
  return 0.5;
}
