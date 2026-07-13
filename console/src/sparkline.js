// ND-6 pure display-math for the Sparkline component (design ruling §3.4:
// "used ONLY where real series data exists... never fabricate one" and hard
// constraint #4, "unknown-never-zero... in numbers AND charts"). Converts an
// ordered array of numbers into normalized SVG polyline points within a
// viewBox of the given width/height, padded so the line never touches the
// edges. No DOM, no React.

// Null/undefined/NaN entries are dropped (a missing sample is simply not
// plotted, never plotted as 0). Returns null -- an honest "no series data
// yet" signal -- when fewer than 2 numeric points remain, since a single
// point cannot draw a trend line; the caller (components/Sparkline.jsx)
// renders a plain-text fallback in that case rather than a fabricated flat
// line.
export function computeSparklinePoints(values, width = 120, height = 32, pad = 3) {
  const nums = (values ?? []).filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  if (nums.length < 2) return null;

  const lo = Math.min(...nums);
  const hi = Math.max(...nums);
  const span = hi - lo;
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;
  const stepX = innerW / (nums.length - 1);

  return nums.map((v, i) => {
    const x = pad + i * stepX;
    // A degenerate (zero-span, every value identical) series still renders
    // -- as a flat line at the track's vertical midpoint, which is an
    // honest depiction of "no variation", not the "no data" case above.
    const y = span <= 1e-9 ? pad + innerH / 2 : pad + innerH - ((v - lo) / span) * innerH;
    return { x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100 };
  });
}
