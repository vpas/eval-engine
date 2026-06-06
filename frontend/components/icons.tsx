// Octicon-flavored line-icon set (ported from the prototype's primitives.jsx).
import * as React from "react";

export const ICONS: Record<string, string> = {
  dot: '<circle cx="8" cy="8" r="2.5"/>',
  grid: '<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/>',
  pulse: '<path d="M1 8h3l2-5 3 10 2-5h4"/>',
  play: '<path d="M4 3l9 5-9 5z" fill="currentColor" stroke="none"/>',
  rocket: '<path d="M5 11c-2 1-2 3-2 3s2 0 3-2"/><path d="M8.5 12.5l-3-3c1-4 4-7 8-7 0 4-3 7-7 8z"/><circle cx="10" cy="6" r="1"/>',
  compare: '<path d="M8 2v12"/><path d="M3 5l-2 2 2 2"/><path d="M13 5l2 2-2 2"/><path d="M1 7h4M11 7h4"/>',
  chart: '<path d="M2 13V3M2 13h12"/><rect x="4" y="8" width="2.4" height="3"/><rect x="8" y="5" width="2.4" height="6"/><path d="M12 11V7"/>',
  list: '<path d="M5 4h9M5 8h9M5 12h9"/><circle cx="2.2" cy="4" r="0.7" fill="currentColor"/><circle cx="2.2" cy="8" r="0.7" fill="currentColor"/><circle cx="2.2" cy="12" r="0.7" fill="currentColor"/>',
  search: '<circle cx="7" cy="7" r="4.5"/><path d="M11 11l3 3"/>',
  filter: '<path d="M2 4h12l-4.5 5v4l-3 1.5V9z"/>',
  plus: '<path d="M8 3v10M3 8h10"/>',
  check: '<path d="M3 8.5l3.2 3.2L13 4.5"/>',
  x: '<path d="M4 4l8 8M12 4l-8 8"/>',
  chevdown: '<path d="M4 6l4 4 4-4"/>',
  chevright: '<path d="M6 4l4 4-4 4"/>',
  chevleft: '<path d="M10 4l-4 4 4 4"/>',
  arrowleft: '<path d="M9 3L4 8l5 5M4 8h10"/>',
  arrowright: '<path d="M7 3l5 5-5 5M12 8H2"/>',
  arrowup: '<path d="M8 13V3M4 7l4-4 4 4"/>',
  arrowdown: '<path d="M8 3v10M4 9l4 4 4-4"/>',
  external: '<path d="M9 3h4v4M13 3L7 9M11 9v4H3V5h4"/>',
  clock: '<circle cx="8" cy="8" r="6"/><path d="M8 5v3.2l2 1.3"/>',
  cpu: '<rect x="4" y="4" width="8" height="8" rx="1"/><path d="M6.5 1.5v2M9.5 1.5v2M6.5 12.5v2M9.5 12.5v2M1.5 6.5h2M1.5 9.5h2M12.5 6.5h2M12.5 9.5h2"/>',
  layers: '<path d="M8 2l6 3-6 3-6-3z"/><path d="M2 8l6 3 6-3M2 11l6 3 6-3"/>',
  database: '<ellipse cx="8" cy="4" rx="5" ry="2"/><path d="M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4"/><path d="M3 8c0 1.1 2.2 2 5 2s5-.9 5-2"/>',
  box: '<path d="M8 2l6 3v6l-6 3-6-3V5z"/><path d="M2 5l6 3 6-3M8 8v6"/>',
  flask: '<path d="M6 2v4L2.5 12a1.5 1.5 0 001.3 2.2h8.4A1.5 1.5 0 0013.5 12L10 6V2"/><path d="M5 2h6M4.5 9h7"/>',
  target: '<circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="3"/><circle cx="8" cy="8" r="0.5" fill="currentColor"/>',
  gauge: '<path d="M2.5 12a6 6 0 1111 0"/><path d="M8 12l3-3.5"/><circle cx="8" cy="12" r="1" fill="currentColor"/>',
  dollar: '<path d="M8 2v12"/><path d="M11 5c0-1.4-1.3-2-3-2s-3 .7-3 2 1.5 1.8 3 2 3 .8 3 2-1.3 2-3 2-3-.6-3-2"/>',
  bolt: '<path d="M9 1L3 9h4l-1 6 6-8H8z" fill="currentColor" stroke="none"/>',
  warn: '<path d="M8 2l6 11H2z"/><path d="M8 7v3M8 11.5v.5"/>',
  refresh: '<path d="M13 7a5 5 0 10-1 4M13 3v3.5H9.5"/>',
  pause: '<path d="M6 4v8M10 4v8"/>',
  stop: '<rect x="4" y="4" width="8" height="8" rx="1.5"/>',
  copy: '<rect x="5" y="5" width="8" height="8" rx="1.5"/><path d="M3 10V3.5C3 3.2 3.2 3 3.5 3H10"/>',
  user: '<circle cx="8" cy="5.5" r="2.5"/><path d="M3.5 13a4.5 4.5 0 019 0"/>',
  team: '<circle cx="6" cy="6" r="2"/><circle cx="11.5" cy="6.5" r="1.6"/><path d="M2.5 13a3.5 3.5 0 017 0M10 13a3 3 0 014.5-2.6"/>',
  tag: '<path d="M2 2h5l7 7-5 5-7-7z"/><circle cx="5" cy="5" r="1" fill="currentColor"/>',
  doc: '<path d="M4 2h5l3 3v9H4z"/><path d="M9 2v3h3M6 8h4M6 11h4"/>',
  link: '<path d="M6.5 9.5l3-3M7 4l1-1a2.5 2.5 0 013.5 3.5l-1 1M9 12l-1 1a2.5 2.5 0 01-3.5-3.5l1-1"/>',
  star: '<path d="M8 2l1.8 3.7 4 .6-3 2.8.7 4L8 11.2 4.5 13l.7-4-3-2.8 4-.6z"/>',
  spark: '<path d="M8 2v3M8 11v3M2 8h3M11 8h3M4 4l2 2M10 10l2 2M12 4l-2 2M6 10l-2 2"/>',
  shield: '<path d="M8 2l5 2v4c0 3-2.2 5.2-5 6-2.8-.8-5-3-5-6V4z"/><path d="M5.8 8l1.5 1.5L10.2 6.5"/>',
  diff: '<path d="M4 2v8M4 14v-1.5a2 2 0 012-2h2"/><circle cx="4" cy="12" r="1.5"/><circle cx="4" cy="2.5" r="1.5"/><path d="M12 6v8M12 2v1.5a2 2 0 01-2 2H8"/><circle cx="12" cy="4" r="1.5"/>',
  trophy: '<path d="M5 2h6v3a3 3 0 01-6 0z"/><path d="M5 3H3v1a2 2 0 002 2M11 3h2v1a2 2 0 01-2 2M8 8v3M5.5 14h5M6.5 11h3v3h-3z"/>',
  slice: '<circle cx="8" cy="8" r="6"/><path d="M8 2v6l4.2 2.2"/>',
  settings: '<circle cx="8" cy="8" r="2"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.5 3.5l1.4 1.4M11.1 11.1l1.4 1.4M12.5 3.5l-1.4 1.4M4.9 11.1l-1.4 1.4"/>',
  download: '<path d="M8 2v8M4.5 6.5L8 10l3.5-3.5M3 13h10"/>',
  bell: '<path d="M8 2a4 4 0 014 4c0 4 1.5 5 1.5 5h-11S4 10 4 6a4 4 0 014-4z"/><path d="M6.5 13a1.6 1.6 0 003 0"/>',
};

export function Icon({ name, className = "ic", size, style }:
  { name: string; className?: string; size?: number; style?: React.CSSProperties }) {
  const p = ICONS[name] || ICONS.dot;
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={style}
      dangerouslySetInnerHTML={{ __html: p }}
      aria-hidden="true"
    />
  );
}
