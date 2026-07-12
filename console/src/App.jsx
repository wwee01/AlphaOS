// ND-1: single-page shell -- the Tonight cockpit is the only view this
// phase ships (docs/roadmap/console-migration-nd.md ND-1 scope: "Tonight
// page only"). ND-2 adds the remaining views of the 7-view IA + real
// navigation.
import React from 'react';
import Tonight from './pages/Tonight.jsx';
import { ErrorBoundary } from './components/ErrorBoundary.jsx';

export default function App() {
  return (
    <>
      <header
        style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
          marginBottom: 20, paddingBottom: 12, borderBottom: '1px solid var(--border)',
        }}
      >
        <div className="num" style={{ fontSize: 18, fontWeight: 700, letterSpacing: '0.08em', color: 'var(--primary)' }}>
          ALPHAOS
        </div>
        <div className="label-caps">console · read-only · ND-1</div>
      </header>
      <ErrorBoundary>
        <Tonight />
      </ErrorBoundary>
    </>
  );
}
