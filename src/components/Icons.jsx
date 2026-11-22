import React from 'react'

const base = {
  width: 20,
  height: 20,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.9,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
}

export const Icon = {
  Home: (p) => <svg {...base} {...p}><path d="M3 10.5 12 3l9 7.5" /><path d="M5 9.5V21h14V9.5" /><path d="M9.5 21v-6h5v6" /></svg>,
  Bookmark: (p) => <svg {...base} {...p}><path d="M6 3.5h12v17l-6-4-6 4z" /></svg>,
  Camera: (p) => <svg {...base} {...p}><path d="M3 8h4l2-3h6l2 3h4v12H3z" /><circle cx="12" cy="14" r="3.6" /></svg>,
  User: (p) => <svg {...base} {...p}><circle cx="12" cy="8" r="4" /><path d="M4 21c1.5-4 4.3-6 8-6s6.5 2 8 6" /></svg>,
  Search: (p) => <svg {...base} {...p}><circle cx="11" cy="11" r="7" /><path d="m20 20-3.6-3.6" /></svg>,
  Refresh: (p) => <svg {...base} {...p}><path d="M20 11a8 8 0 1 0-2.3 6.4" /><path d="M20 4v7h-7" /></svg>,
  Arrow: (p) => <svg {...base} {...p}><path d="M5 12h14" /><path d="m13 6 6 6-6 6" /></svg>,
  Back: (p) => <svg {...base} {...p}><path d="M19 12H5" /><path d="m11 18-6-6 6-6" /></svg>,
  External: (p) => <svg {...base} {...p}><path d="M14 4h6v6" /><path d="M20 4 10 14" /><path d="M18 14v6H4V6h6" /></svg>,
  Grid: (p) => <svg {...base} {...p}><rect x="3" y="3" width="7.5" height="7.5" rx="1.5" /><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5" /><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5" /><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5" /></svg>,
  List: (p) => <svg {...base} {...p}><path d="M8 6h13M8 12h13M8 18h13" /><circle cx="3.5" cy="6" r="1" /><circle cx="3.5" cy="12" r="1" /><circle cx="3.5" cy="18" r="1" /></svg>,
  Eye: (p) => <svg {...base} {...p}><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12Z" /><circle cx="12" cy="12" r="2.6" /></svg>,
  EyeOff: (p) => <svg {...base} {...p}><path d="M4 4 20 20" /><path d="M9.5 6.1A10.6 10.6 0 0 1 12 5.5c6.4 0 10 6.5 10 6.5a17 17 0 0 1-3.2 3.9" /><path d="M6.2 8.3A17 17 0 0 0 2 12s3.6 6.5 10 6.5a10.8 10.8 0 0 0 4-.7" /></svg>,
  Close: (p) => <svg {...base} {...p}><path d="M6 6 18 18M18 6 6 18" /></svg>,
  Check: (p) => <svg {...base} {...p}><path d="m4 12 5 5L20 6" /></svg>,
  Clock: (p) => <svg {...base} {...p}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3.5 2" /></svg>,
  Spark: (p) => <svg {...base} {...p}><path d="M12 2.5 14 9l6.5 2-6.5 2-2 6.5L10 13l-6.5-2L10 9z" /></svg>,
  Logout: (p) => <svg {...base} {...p}><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></svg>,
  Font: (p) => <svg {...base} {...p}><path d="M4 19 9.5 5h2L17 19" /><path d="M5.5 14h10" /><path d="M19 12h3M20.5 10.5v3" /></svg>,
}

export function Brand({ compact = false }) {
  return (
    <span className={'brand' + (compact ? ' compact' : '')}>
      <span className="brand-mark">FL</span>
      {!compact && <span className="brand-text">Feed<span>line</span></span>}
    </span>
  )
}
