// Catches render errors in a section so one component throwing never blanks
// the whole console. Same PATTERN NightDesk uses (../nightdesk/src/
// components/ErrorBoundary.jsx), rewritten fresh here with AlphaOS's own
// copy/tokens -- per the ND-1 plan doc, this app's code is hand-authored
// fresh, not copied from NightDesk (only its conventions are adopted).
import React from 'react';

export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    try {
      console.error('AlphaOS console view error:', error, info?.componentStack);
    } catch {
      /* noop */
    }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="block" style={{ borderColor: 'var(--red)' }}>
          <div className="label-caps" style={{ color: 'var(--red)', marginBottom: 6 }}>
            view error
          </div>
          <div style={{ fontSize: 13, color: 'var(--text-dim)', lineHeight: 1.6, marginBottom: 10 }}>
            This section failed to render. The API, the journal, and the trading system are unaffected —
            this console is read-only.
          </div>
          <div className="num" style={{ fontSize: 11, color: 'var(--text-dim)', wordBreak: 'break-word' }}>
            {String(this.state.error?.message ?? this.state.error)}
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
