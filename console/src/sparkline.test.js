import { describe, expect, it } from 'vitest';
import { computeSparklinePoints } from './sparkline.js';

describe('computeSparklinePoints', () => {
  it('returns null (honest "no data yet") for fewer than 2 numeric points', () => {
    expect(computeSparklinePoints([])).toBeNull();
    expect(computeSparklinePoints([5])).toBeNull();
    expect(computeSparklinePoints([null, undefined])).toBeNull();
    expect(computeSparklinePoints(undefined)).toBeNull();
  });

  it('drops null/undefined/NaN entries rather than plotting them as 0', () => {
    const points = computeSparklinePoints([1, null, 3, undefined, 5], 100, 20, 0);
    expect(points).toHaveLength(3);
  });

  it('places the lowest value at the bottom and highest at the top of the track', () => {
    const points = computeSparklinePoints([0, 10], 100, 20, 0);
    expect(points[0].y).toBe(20); // lowest value -> bottom (largest y)
    expect(points[1].y).toBe(0); // highest value -> top (y=0)
  });

  it('spaces points evenly across the available width', () => {
    const points = computeSparklinePoints([1, 2, 3], 100, 20, 0);
    expect(points[0].x).toBe(0);
    expect(points[1].x).toBe(50);
    expect(points[2].x).toBe(100);
  });

  it('renders a flat mid-track line for a degenerate (zero-variance) series, never a divide-by-zero', () => {
    const points = computeSparklinePoints([4, 4, 4], 100, 20, 0);
    expect(points.every((p) => p.y === 10)).toBe(true);
  });

  it('respects padding so the line never touches the viewBox edge', () => {
    const points = computeSparklinePoints([1, 2], 120, 32, 3);
    expect(points[0].x).toBe(3);
    expect(points[1].x).toBe(117);
  });
});
