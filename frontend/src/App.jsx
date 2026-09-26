import { useCallback, useEffect, useRef, useState } from "react";

const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, "");
const REFRESH_MS = 4000;

function formatTime(value) {
  if (!value) return "—";
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function StatusBadge({ status }) {
  return <span className={`status-badge status-${(status || "unknown").toLowerCase()}`}>{status || "UNKNOWN"}</span>;
}

function MetricCard({ label, value, detail, tone, icon }) {
  return (
    <article className={`metric-card metric-${tone}`}>
      <div className="metric-heading">
        <span>{label}</span>
        <span className="metric-icon">{icon}</span>
      </div>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

function App() {
  const [monitoring, setMonitoring] = useState(null);
  const [replicas, setReplicas] = useState(null);
  const [error, setError] = useState("");
  const [generating, setGenerating] = useState(0);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [scaleEvent, setScaleEvent] = useState("");
  const previousReplicas = useRef(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${API_URL}/monitoring`);
      if (!response.ok) throw new Error(`Monitoring request failed (${response.status})`);
      const body = await response.json();
      setMonitoring(body);
      setLastUpdated(new Date());
      setError("");
    } catch (requestError) {
      setError(requestError.message || "Unable to reach the orders API");
    }
    try {
      const response = await fetch(`${API_URL}/monitoring/replicas`);
      if (!response.ok) throw new Error(`Replica request failed (${response.status})`);
      const body = await response.json();
      setReplicas(body);
      if (body.available && typeof body.count === "number") {
        const previous = previousReplicas.current;
        if (previous !== null && body.count !== previous) {
          setScaleEvent(body.count > previous ? `Scaled out · ${previous} → ${body.count}` : `Scaled in · ${previous} → ${body.count}`);
          window.setTimeout(() => setScaleEvent(""), 7000);
        }
        previousReplicas.current = body.count;
      } else {
        previousReplicas.current = null;
      }
    } catch (requestError) {
      setReplicas({ available: false, count: null, replicas: [], reason: requestError.message });
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function generateOrders(count) {
    setGenerating(count);
    try {
      const response = await fetch(`${API_URL}/orders/bulk?count=${count}`, { method: "POST" });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Order generation failed (${response.status})`);
      }
      await refresh();
    } catch (requestError) {
      setError(requestError.message || "Could not generate orders");
    } finally {
      setGenerating(0);
    }
  }

  const stats = monitoring?.orders || {};
  const queueDepth = monitoring?.queue_depth ?? "—";
  const activeCount = replicas?.available ? replicas.count : "—";
  const busy = generating > 0;

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="QueuePulse home">
          <span className="brand-mark"><span /></span>
          <span>queue<span className="brand-accent">pulse</span></span>
        </a>
        <div className="topbar-right">
          <span className={`live-indicator ${error ? "is-error" : ""}`} />
          <span>{error ? "API connection issue" : "Live system monitor"}</span>
          <span className="topbar-divider" />
          <span className="refresh-label">Refreshes every 4s</span>
        </div>
      </header>

      <section className="hero">
        <div className="hero-copy">
          <div className="eyebrow"><span className="eyebrow-line" /> EVENT-DRIVEN ORDER PROCESSING</div>
          <h1>Work arrives.<br /><span>Workers respond.</span></h1>
          <p>Watch Azure Queue trigger containers on demand — and scale back to zero when the queue is clear.</p>
          <div className="hero-tags">
            <span><i className="tag-dot dot-green" /> Azure Storage Queue</span>
            <span><i className="tag-dot dot-blue" /> KEDA autoscaling</span>
            <span><i className="tag-dot dot-purple" /> Container Apps</span>
          </div>
        </div>
        <div className="hero-visual" aria-label="Queue-triggered workers diagram">
          <div className="orbit orbit-one" />
          <div className="orbit orbit-two" />
          <div className="flow-node api-node"><span className="node-glyph">↗</span><small>ORDER API</small></div>
          <div className="flow-node queue-node"><span className="queue-glyph">▤</span><small>QUEUE</small><b>{queueDepth}</b></div>
          <div className={`flow-node worker-node ${Number(activeCount) > 0 ? "worker-active" : ""}`}><span className="worker-glyph">▦</span><small>WORKERS</small><b>{activeCount}</b></div>
          <div className="flow-line line-one"><span /></div>
          <div className="flow-line line-two"><span /></div>
          <div className="visual-caption">{replicas?.available ? "AZURE REPLICA DATA" : "LOCAL DEMO MODE"}</div>
        </div>
      </section>

      {error && <div className="error-banner" role="alert"><span>!</span>{error}</div>}

      <section className="metrics-grid" aria-label="Order processing metrics">
        <MetricCard label="Queue depth" value={queueDepth} detail="Approx. visible messages" tone="mint" icon="▤" />
        <MetricCard label="Active workers" value={activeCount} detail={replicas?.available ? "Azure-reported replicas" : "Replica data unavailable"} tone="blue" icon="▦" />
        <MetricCard label="Orders generated" value={stats.total ?? "—"} detail="All submitted orders" tone="violet" icon="↗" />
        <MetricCard label="Completed" value={stats.completed ?? "—"} detail="Successfully processed" tone="green" icon="✓" />
        <MetricCard label="Processing" value={stats.processing ?? "—"} detail="Currently claimed" tone="amber" icon="◷" />
        <MetricCard label="Failed" value={stats.failed ?? "—"} detail="Needs attention" tone="rose" icon="!" />
      </section>

      <section className="demo-panel">
        <div className="demo-copy">
          <div className="eyebrow"><span className="eyebrow-line" /> LOAD GENERATOR</div>
          <h2>Trigger a scaling event</h2>
          <p>Enqueue a batch and watch KEDA add worker replicas as the backlog grows.</p>
        </div>
        <div className="demo-actions">
          {[10, 50, 100].map((count) => (
            <button key={count} className={count === 100 ? "button-primary" : "button-secondary"} disabled={busy} onClick={() => generateOrders(count)}>
              {generating === count ? <span className="button-spinner" /> : <span className="button-plus">+</span>}
              {generating === count ? "Sending…" : `Generate ${count}`}
            </button>
          ))}
        </div>
        <div className="demo-footer">
          <span className="footer-pulse" /> Queue messages drive worker count
          {scaleEvent && <strong className="scale-event">{scaleEvent}</strong>}
          <span className="updated-at">{lastUpdated ? `Updated ${formatTime(lastUpdated)}` : "Connecting…"}</span>
        </div>
      </section>

      <section className="content-grid">
        <article className="panel recent-panel">
          <div className="panel-heading">
            <div><div className="eyebrow">ORDER STREAM</div><h2>Recent orders</h2></div>
            <span className="subtle-count">{monitoring?.recent_orders?.length || 0} latest</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Order</th><th>Product</th><th>Status</th><th>Worker / container</th><th>Created</th></tr></thead>
              <tbody>
                {(monitoring?.recent_orders || []).map((order) => (
                  <tr key={order.order_id}>
                    <td className="order-id">#{order.order_id.slice(0, 8)}</td>
                    <td>{order.product}</td>
                    <td><StatusBadge status={order.status} /></td>
                    <td className="worker-name">{order.worker_id || <span className="muted">Waiting for worker</span>}</td>
                    <td className="time-cell">{formatTime(order.created_at)}</td>
                  </tr>
                ))}
                {!monitoring?.recent_orders?.length && (
                  <tr><td className="empty-state" colSpan="5">Orders will appear here as soon as they enter the queue.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </article>

        <article className="panel workers-panel">
          <div className="panel-heading">
            <div><div className="eyebrow">CONTAINER APPS</div><h2>Worker fleet</h2></div>
            <span className={`fleet-pill ${replicas?.available && replicas.count > 0 ? "fleet-running" : ""}`}>
              <i /> {replicas?.available ? `${replicas.count} active` : "Not connected"}
            </span>
          </div>
          {replicas?.available ? (
            <>
              <div className="fleet-summary">
                <div className="fleet-count">{replicas.count}<span> / 10</span></div>
                <div className="fleet-label">replicas<br /><small>Azure-reported active revisions</small></div>
              </div>
              <div className="replica-list">
                {replicas.replicas.map((replica) => (
                  <div className="replica-row" key={`${replica.revision}-${replica.name}`}>
                    <span className="replica-icon">▦</span>
                    <span className="replica-details"><strong>{replica.name}</strong><small>{replica.revision}</small></span>
                    <span className="replica-state"><i />{replica.state}</span>
                  </div>
                ))}
                {replicas.count === 0 && <div className="no-replicas"><span>◌</span><strong>Scaled to zero</strong><small>No queue workload detected</small></div>}
              </div>
            </>
          ) : (
            <div className="monitoring-unavailable">
              <span className="unavailable-icon">⌁</span>
              <strong>Replica telemetry unavailable</strong>
              <p>{replicas?.reason || "Connect the API to Azure Container Apps management to view live replicas."}</p>
              <small>Order worker IDs are still shown in the order stream after processing.</small>
            </div>
          )}
          <div className="worker-history">
            <span>SEEN WORKER CONTAINERS</span>
            {(monitoring?.worker_containers || []).length ? (
              <div className="worker-chips">{monitoring.worker_containers.slice(0, 3).map((worker) => <code key={worker}>{worker}</code>)}</div>
            ) : <small>Waiting for the first processed order</small>}
          </div>
        </article>
      </section>

      <footer className="page-footer">
        <span>QUEUEPULSE <i /> HACKATHON DEMO</span>
        <span>API → QUEUE → KEDA → WORKERS → SCALE TO ZERO</span>
      </footer>
    </main>
  );
}

export default App;
