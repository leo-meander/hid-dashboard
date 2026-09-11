import { useState, useEffect, useCallback, useRef } from "react";
import axios from "axios";

const BRANCH_OPTS = ["all", "saigon", "taipei", "oani", "osaka", "1948"];
const SERVICES = ["ghl", "meta", "google_ads", "tiktok"];
const SERVICE_LABELS = { ghl: "GHL CRM", meta: "Meta CAPI", google_ads: "Google Ads", tiktok: "TikTok" };
const isoDateDaysAgo = (days) => new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);

function StatusBadge({ svc }) {
  if (!svc) return <span className="text-gray-300">—</span>;
  if (svc.success === null || svc.action === "skipped_no_config" || svc.action?.startsWith("skipped"))
    return <span className="px-2 py-0.5 rounded text-xs bg-gray-100 text-gray-500">skip</span>;
  if (svc.success)
    return <span className="px-2 py-0.5 rounded text-xs bg-green-100 text-green-700 font-medium">✓ {svc.action || "ok"}</span>;
  return (
    <span className="px-2 py-0.5 rounded text-xs bg-red-100 text-red-700 font-medium" title={svc.error || svc.partial_failure_error || ""}>
      ✗ {svc.error ? "error" : svc.case || "fail"}
    </span>
  );
}

// Cloudbeds created the reservation → we finished fanning it out. Blank for
// rows written before the backend started recording dateCreated, and for a
// reservation Cloudbeds sent without one.
function LagCell({ seconds }) {
  if (seconds === null || seconds === undefined)
    return <td className="py-2 px-3 text-gray-300">—</td>;

  const abs = Math.abs(seconds);
  const label =
    abs < 60 ? `${abs}s`
      : abs < 3600 ? `${Math.floor(abs / 60)}m`
        : abs < 86400 ? `${Math.floor(abs / 3600)}h ${Math.floor((abs % 3600) / 60)}m`
          : `${Math.floor(abs / 86400)}d`;

  // A negative lag means we recorded the fan-out before Cloudbeds says the
  // reservation existed — that is a wrong tz offset for the branch, not a fast
  // pipeline, so it gets its own colour rather than blending in with the good rows.
  const tone =
    seconds < 0 ? "text-purple-600"
      : seconds <= 15 * 60 ? "text-gray-500"
        : seconds <= 60 * 60 ? "text-amber-600"
          : "text-red-600 font-medium";

  return (
    <td className={`py-2 px-3 whitespace-nowrap text-xs ${tone}`} title={`${seconds}s from Cloudbeds dateCreated to fan-out`}>
      {seconds < 0 ? `-${label}` : label}
    </td>
  );
}

// Per-branch lag at a glance. The row-by-row column answers "was this booking
// late"; this answers the question that actually gets asked — "is that branch
// slower than the others" — which no amount of staring at timestamps does,
// because a branch with a quarter of the volume looks late either way.
function LagSummary({ events }) {
  const byBranch = {};
  for (const ev of events) {
    if (ev.lag_seconds === null || ev.lag_seconds === undefined) continue;
    (byBranch[ev.branch] ||= []).push(ev.lag_seconds);
  }
  const rows = Object.entries(byBranch)
    .map(([branch, lags]) => {
      const sorted = [...lags].sort((a, b) => a - b);
      const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
      return { branch, n: sorted.length, median: at(0.5), p90: at(0.9) };
    })
    .sort((a, b) => b.median - a.median);

  if (!rows.length) return null;
  const fmt = (s) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`);

  return (
    <div className="mb-4 flex flex-wrap gap-2">
      {rows.map(r => (
        <div key={r.branch} className="px-3 py-2 bg-white border rounded-lg text-xs">
          <div className="font-semibold text-gray-700 uppercase">{r.branch}</div>
          <div className="text-gray-500 mt-0.5">
            median <span className="font-medium text-gray-700">{fmt(r.median)}</span>
            {" · "}p90 <span className="font-medium text-gray-700">{fmt(r.p90)}</span>
            {" · "}<span className="text-gray-400">{r.n} rows</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function EventRow({ ev }) {
  const [open, setOpen] = useState(false);
  const ts = new Date(ev.timestamp).toLocaleString("en-GB", { hour12: false });
  const anyFail = SERVICES.some(s => ev[s]?.success === false);

  return (
    <>
      <tr
        onClick={() => setOpen(o => !o)}
        className={`cursor-pointer border-b hover:bg-gray-50 text-sm ${anyFail ? "bg-red-50/40" : ""}`}
      >
        <td className="py-2 px-3 text-gray-500 whitespace-nowrap">{ts}</td>
        <LagCell seconds={ev.lag_seconds} />
        <td className="py-2 px-3">
          <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-indigo-100 text-indigo-700 uppercase">
            {ev.branch}
          </span>
        </td>
        <td className="py-2 px-3 font-mono text-xs text-gray-700">{ev.reservation_id}</td>
        <td className="py-2 px-3 text-xs text-gray-500 max-w-[140px] truncate">{ev.guest_email}</td>
        <td className="py-2 px-3 text-xs text-gray-400">{ev.source}</td>
        {SERVICES.map(s => (
          <td key={s} className="py-2 px-3 text-center">
            <StatusBadge svc={ev[s]} />
          </td>
        ))}
      </tr>
      {open && (
        <tr className="bg-gray-50 border-b">
          <td colSpan={6 + SERVICES.length} className="px-4 py-3">
            <pre className="text-xs text-gray-600 overflow-auto whitespace-pre-wrap">
              {JSON.stringify({
                reservation_created_at: ev.reservation_created_at,
                lag_seconds: ev.lag_seconds,
                ghl: ev.ghl, meta: ev.meta, google_ads: ev.google_ads, tiktok: ev.tiktok,
              }, null, 2)}
            </pre>
          </td>
        </tr>
      )}
    </>
  );
}

export default function WebhookMonitor() {
  const [events, setEvents] = useState([]);
  const [branch, setBranch] = useState("all");
  const [loading, setLoading] = useState(false);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [failuresOnly, setFailuresOnly] = useState(false);
  const [polling, setPolling] = useState(false);
  const [pollStatus, setPollStatus] = useState("");
  const [diagnostic, setDiagnostic] = useState(null);
  const [diagnosing, setDiagnosing] = useState(false);
  const [backfillFrom, setBackfillFrom] = useState(() => isoDateDaysAgo(3));
  const [backfillTo, setBackfillTo] = useState(() => isoDateDaysAgo(1));
  const [backfilling, setBackfilling] = useState(false);
  const eventCountRef = useRef(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // failures_only is filtered server-side: failures older than the 200-row
      // page would otherwise be invisible, which is exactly when you need them.
      const params = { limit: 200 };
      if (branch !== "all") params.branch = branch;
      if (failuresOnly) params.failures_only = true;
      const res = await axios.get("/api/admin/webhook-events", { params });
      const data = res.data.data || [];
      setEvents(data);
      eventCountRef.current = data.length;
      setLastRefresh(new Date());
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [branch, failuresOnly]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [autoRefresh, load]);

  const failCount = events.filter(e => SERVICES.some(s => e[s]?.success === false)).length;

  return (
    <div className="max-w-7xl mx-auto">
      {pollStatus && (
        <div className="mb-3 px-3 py-2 bg-emerald-50 border border-emerald-200 rounded text-sm text-emerald-700">
          ⏳ {pollStatus}
        </div>
      )}
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-xl font-semibold text-gray-800">Webhook Monitor</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {failuresOnly
              ? `${events.length} failed`
              : `${events.length} events · last 7 days`}
            {!failuresOnly && failCount > 0 && (
              <button
                onClick={() => setFailuresOnly(true)}
                className="ml-2 text-red-600 font-medium hover:underline"
              >
                · {failCount} failed
              </button>
            )}
            {lastRefresh && <span className="ml-2 text-gray-400">· refreshed {lastRefresh.toLocaleTimeString("en-GB")}</span>}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={() => setFailuresOnly(f => !f)}
            className={`text-sm px-3 py-1.5 rounded border font-medium ${
              failuresOnly
                ? "bg-red-600 border-red-600 text-white"
                : "bg-white border-gray-300 text-gray-600 hover:bg-gray-50"
            }`}
          >
            {failuresOnly ? "✗ Failed only" : "Failed only"}
          </button>
          <label className="flex items-center gap-2 text-sm text-gray-600 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={e => setAutoRefresh(e.target.checked)}
              className="rounded"
            />
            Auto-refresh 15s
          </label>
          <select
            value={branch}
            onChange={e => setBranch(e.target.value)}
            className="text-sm border border-gray-300 rounded px-2 py-1.5"
          >
            {BRANCH_OPTS.map(b => <option key={b} value={b}>{b === "all" ? "All branches" : b}</option>)}
          </select>
          <button
            onClick={async () => {
              setPolling(true);
              setPollStatus("Fetching from Cloudbeds…");
              try {
                await axios.post("/api/admin/poll-now", null, { params: { minutes: 60 } });
                // Poll background job runs async — wait then retry until count changes
                const before = eventCountRef.current;
                for (let i = 0; i < 6; i++) {
                  await new Promise(r => setTimeout(r, 5000));
                  setPollStatus(`Waiting for results… (${(i + 1) * 5}s)`);
                  await load();
                  if (eventCountRef.current !== before) break;
                }
                setPollStatus("");
              } catch {
                setPollStatus("Error — please try again");
              } finally {
                setPolling(false);
              }
            }}
            disabled={polling}
            title="Pull last 60 min of reservations from Cloudbeds now"
            className="px-3 py-1.5 text-sm bg-emerald-600 text-white rounded hover:bg-emerald-700 disabled:opacity-50 min-w-[90px]"
          >
            {polling ? "Polling…" : "Poll Now"}
          </button>
          <input type="date" value={backfillFrom} onChange={e => setBackfillFrom(e.target.value)} aria-label="Backfill start date" className="text-sm border border-gray-300 rounded px-2 py-1.5" />
          <input type="date" value={backfillTo} onChange={e => setBackfillTo(e.target.value)} aria-label="Backfill end date" className="text-sm border border-gray-300 rounded px-2 py-1.5" />
          <button
            onClick={async () => {
              setBackfilling(true);
              setPollStatus(`Starting backfill for ${backfillFrom} to ${backfillTo}...`);
              try {
                const res = await axios.post("/api/admin/reservation-backfill", null, { params: { date_from: backfillFrom, date_to: backfillTo } });
                setPollStatus(res.data.message || "Backfill started. Refresh to see results.");
              } catch (e) {
                setPollStatus(`Backfill error: ${e.response?.data?.detail || e.message}`);
              } finally {
                setBackfilling(false);
              }
            }}
            disabled={backfilling || !backfillFrom || !backfillTo}
            title="Re-send every reservation created in this inclusive date range"
            className="px-3 py-1.5 text-sm bg-orange-600 text-white rounded hover:bg-orange-700 disabled:opacity-50"
          >
            {backfilling ? "Starting..." : "Backfill"}
          </button>
          <button
            onClick={async () => {
              setDiagnosing(true);
              try {
                const res = await axios.get("/api/admin/poll-diagnostic", { params: { minutes: 60 } });
                setDiagnostic(res.data.data);
              } catch (e) {
                setDiagnostic({ error: e.response?.data?.detail || e.message });
              } finally {
                setDiagnosing(false);
              }
            }}
            disabled={diagnosing}
            title="Ask Cloudbeds the same question the poller asks, and show the raw answer"
            className="px-3 py-1.5 text-sm bg-amber-600 text-white rounded hover:bg-amber-700 disabled:opacity-50"
          >
            {diagnosing ? "Checking…" : "Diagnose"}
          </button>
          <button
            onClick={load}
            disabled={loading}
            className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </div>

      {diagnostic && (
        <div className="mb-4 bg-amber-50 border border-amber-200 rounded-lg p-4">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-sm font-semibold text-amber-900">Poll diagnostic</h2>
            <button
              onClick={() => setDiagnostic(null)}
              className="text-xs text-amber-700 hover:underline"
            >
              close
            </button>
          </div>
          <pre className="text-xs text-amber-900 overflow-auto whitespace-pre-wrap max-h-[28rem]">
            {JSON.stringify(diagnostic, null, 2)}
          </pre>
        </div>
      )}

      <LagSummary events={events} />

      {events.length === 0 ? (
        <div className="text-center py-20 text-gray-400">
          {loading
            ? "Loading…"
            : failuresOnly
              ? "No failures in the last 7 days."
              : "No events yet — click Poll Now to fetch recent reservations."}
        </div>
      ) : (
        <div className="bg-white rounded-lg border overflow-x-auto">
          <table className="w-full text-left">
            <thead className="bg-gray-50 text-xs font-semibold text-gray-500 uppercase tracking-wide border-b">
              <tr>
                <th className="py-2 px-3">Time</th>
                <th className="py-2 px-3" title="Cloudbeds dateCreated → fan-out">Lag</th>
                <th className="py-2 px-3">Branch</th>
                <th className="py-2 px-3">Reservation</th>
                <th className="py-2 px-3">Email</th>
                <th className="py-2 px-3">Source</th>
                {SERVICES.map(s => <th key={s} className="py-2 px-3 text-center">{SERVICE_LABELS[s]}</th>)}
              </tr>
            </thead>
            <tbody>
              {events.map(ev => <EventRow key={ev.id} ev={ev} />)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
