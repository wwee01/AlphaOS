// ND-2 Autonomy & Risk page -- renders /api/v1/governance
// (build_governance_report() verbatim). Mirrors streamlit_app.
// tab_governance() layout (autonomy + hard limits side by side, kill
// switch + real-money lock side by side, trading calendar full-width).
// PURE READ, zero controls -- the only kill-switch CONTROL in this console
// lives in the masthead (components/Annunciator.jsx, rendered by
// components/Masthead.jsx on every page). This page only EXPLAINS the same
// state, never a second control surface (hard constraint #6).
//
// ND-6: recomposed as an authoritative "spec sheet" (design ruling §5) --
// same five panels, restyled with the instrument-block hierarchy and an
// autonomy-level StatTile hero. The real-money lock panel deliberately has
// NO interactive element anywhere in it (hard constraint #6: "no unlock
// affordance").
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { getGovernance } from '../api.js';
import { Badge, Block } from '../components/ui.jsx';
import { StatTile } from '../components/StatTile.jsx';
import { describeUnreachable, formatClockUTC } from '../format.js';

const POLL_MS = 15000;

function AutonomyPanel({ autonomy }) {
  return (
    <Block title="Autonomy">
      <StatTile label="level" value={autonomy.level_label} size="md" tone="primary" />
      <div className="prose" style={{ fontSize: 13, margin: '12px 0 4px' }}>{autonomy.may_alone}</div>
      <div className="prose" style={{ fontSize: 13, marginBottom: 8 }}>{autonomy.may_not_alone}</div>
      {autonomy.unattended_exception ? (
        <div className="stale-banner" style={{ borderColor: 'var(--border)', background: 'var(--surface-low)', color: 'var(--text)' }}>
          {autonomy.unattended_exception.text}
        </div>
      ) : (
        <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
          No unattended close-window exception armed (UNATTENDED_APPROVE_WINDOWS unset or its daily cap is 0).
        </div>
      )}
      <div className="block-footer" style={{ fontSize: 11, color: 'var(--text-dim)' }}>L2: {autonomy.l2_status}</div>
    </Block>
  );
}

function HardLimitsPanel({ hl }) {
  const row = (label, value) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, fontSize: 13, padding: '4px 0', borderBottom: '1px solid var(--border)' }}>
      <span style={{ color: 'var(--text-dim)' }}>{label}</span>
      <span className="num" style={{ textAlign: 'right' }}>{value}</span>
    </div>
  );
  return (
    <Block title="Hard limits (read-only)">
      {row('Risk/trade', `${(hl.risk_per_trade_pct * 100).toFixed(2)}% ($${hl.risk_per_trade_dollars.toLocaleString()})`)}
      {row('Max open positions', hl.max_open_positions)}
      {row('Daily-loss stop', `${(hl.daily_loss_stop_pct * 100).toFixed(2)}% ($${hl.daily_loss_stop_dollars.toLocaleString()})`)}
      {row('Auto-approvals', `${hl.auto_approvals_used_today}/${hl.auto_approvals_cap} today`)}
      {row('Unattended approvals', `${hl.unattended_approvals_used_today}/${hl.unattended_approvals_cap} today · window(s): ${hl.unattended_windows_label}`)}
      {row('Max spread', `${(hl.max_spread_pct * 100).toFixed(2)}%`)}
      {row('Min $ volume', `$${hl.min_dollar_volume.toLocaleString()}`)}
      {row('AI budget (30d, all real calls)', `${hl.ai_budget_used_30d}/${hl.ai_budget_cap_30d}`)}
      {row('Bear-debate calls', `${hl.debate_calls_used_today}/${hl.debate_calls_cap_today} today`)}
      {row('Hypothesis-gen calls', `${hl.hypothesis_gen_calls_used_today}/${hl.hypothesis_gen_calls_cap_today} today`)}
      {row('Max paper trades/day', `${hl.max_paper_trades_per_day_display} (used ${hl.paper_trades_used_today} today)`)}
      <div style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 10 }}>{hl.changes_note}</div>
    </Block>
  );
}

function KillSwitchPanel({ ks }) {
  return (
    <Block title="Kill switch (state only — control is in the masthead)">
      <Badge tone={ks.engaged ? 'danger' : 'ok'} style={{ fontSize: 13, padding: '6px 12px' }}>
        ● {ks.state_label}{ks.engaged ? ` — ${ks.reason ?? 'no reason recorded'}` : ''}
      </Badge>
      <div className="prose" style={{ fontSize: 13, margin: '10px 0' }}>{ks.explanation}</div>
      <div className="block-footer" style={{ fontSize: 11, color: 'var(--text-dim)' }}>{ks.control_note}</div>
    </Block>
  );
}

function RealMoneyLockPanel({ lock }) {
  return (
    <Block title="Real-money lock">
      <div style={{ fontSize: 13, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
        <Badge tone="danger" caps>locked</Badge> {lock.structural_statement}
      </div>
      <div className="num" style={{ fontSize: 12, marginBottom: 8, color: 'var(--text-dim)' }}>
        REAL_TRADING_ENABLED={lock.real_trading_enabled_raw} · ALLOW_REAL_ORDERS={lock.allow_real_orders_raw} · mode={lock.mode}
      </div>
      <div className="block-footer" style={{ fontSize: 11, color: 'var(--text-dim)' }}>{lock.no_unlock_note}</div>
    </Block>
  );
}

function TradingCalendarPanel({ cal }) {
  const dayState = cal.is_trading_day ? 'a trading day' : 'MARKET CLOSED';
  return (
    <Block title="Trading calendar">
      <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>
        {/* ND-7: "is a trading day" is a healthy/active state (ruling §3
            migration) -- green, not brand cyan; "market closed" stays
            plain ink (a fact, not a warning). */}
        Today ({cal.today_et} ET): <span style={{ color: cal.is_trading_day ? 'var(--good)' : 'var(--text)' }}>{dayState}</span> · scan windows: {cal.scan_windows_label} · {cal.note}
      </div>
    </Block>
  );
}

export default function Governance() {
  const [data, setData] = useState(null);
  const [unreachable, setUnreachable] = useState(false);
  const [lastGoodAsOf, setLastGoodAsOf] = useState(null);
  const mountedRef = useRef(true);

  const poll = useCallback(async () => {
    try {
      const r = await getGovernance();
      if (!mountedRef.current) return;
      setData(r);
      setUnreachable(false);
      setLastGoodAsOf(r.as_of ?? null);
    } catch {
      if (!mountedRef.current) return;
      setUnreachable(true);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [poll]);

  const unreachableMsg = describeUnreachable(unreachable, lastGoodAsOf);

  return (
    <div className={unreachable ? 'dim' : ''}>
      {unreachableMsg && <div className="stale-banner">{unreachableMsg}</div>}
      {!data ? (
        <div className="label-caps">loading governance…</div>
      ) : (
        <>
          <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 12 }}>as of {formatClockUTC(data.as_of)}</div>
          <div className="grid reveal-stagger">
            <div className="col-6"><AutonomyPanel autonomy={data.autonomy} /></div>
            <div className="col-6"><HardLimitsPanel hl={data.hard_limits} /></div>
            <div className="col-6"><KillSwitchPanel ks={data.kill_switch} /></div>
            <div className="col-6"><RealMoneyLockPanel lock={data.real_money_lock} /></div>
            <div className="col-12"><TradingCalendarPanel cal={data.trading_calendar} /></div>
          </div>
        </>
      )}
    </div>
  );
}
