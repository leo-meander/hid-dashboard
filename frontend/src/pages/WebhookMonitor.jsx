import { useState, useEffect, useCallback } from "react";
import axios from "axios";

const BRANCH_OPTS = ["all", "saigon", "taipei", "oani", "osaka", "1948"];
const SERVICES = ["ghl", "meta", "google_ads", "tiktok"];
const SERVICE_LABELS = { ghl: "GHL CRM", meta: "Meta CAPI", google_ads: "Google Ads", tiktok: "TikTok" };

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

function EventRow({ ev }) {
  const [open, setOpen] = useState(false);
  const ts = new Date(ev.timestamp).toLocaleString("vi-VN", { hour12: false });
  const anyFail = SERVICES.some(s => ev[s]?.success === false);

  return (
    <>
      <tr
        onClick={() => setOpen(o => !o)}
        className={`cursor-pointer border-b hover:bg-gray-50 text-sm ${anyFail ? "bg-red-50/40" : ""}`}
      >
        <td className="py-2 px-3 text-gray-500 whitespace-nowrap">{ts}</td>
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
          <td colSpan={5 + SERVICES.length} className="px-4 py-3">
            <pre className="text-xs text-gray-600 overflow-auto whitespace-pre-wrap">
              {JSON.stringify({ ghl: ev.ghl, meta: ev.meta, google_ads: ev.google_ads, tiktok: ev.tiktok }, null, 2)}
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

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = branch !== "all" ? { branch, limit: 200 } : { limit: 200 };
      const res = await axios.get("/api/admin/webhook-events", { params });
      setEvents(res.data.data || []);
      setLastRefresh(new Date());
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [branch]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [autoRefresh, load]);

  const failCount = events.filter(e => SERVICES.some(s => e[s]?.success === false)).length;

  return (
    <div className="max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-xl font-semibold text-gray-800">Webhook Monitor</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {events.length} events in memory
            {failCount > 0 && <span className="ml-2 text-red-600 font-medium">· {failCount} failed</span>}
            {lastRefresh && <span className="ml-2 text-gray-400">· refreshed {lastRefresh.toLocaleTimeString("vi-VN")}</span>}
          </p>
        </div>
        <div className="flex items-center gap-3">
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
            onClick={load}
            disabled={loading}
            className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50"
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </div>

      {events.length === 0 ? (
        <div className="text-center py-20 text-gray-400">
          {loading ? "Loading…" : "No events yet — waiting for Cloudbeds webhooks."}
        </div>
      ) : (
        <div className="bg-white rounded-lg border overflow-x-auto">
          <table className="w-full text-left">
            <thead className="bg-gray-50 text-xs font-semibold text-gray-500 uppercase tracking-wide border-b">
              <tr>
                <th className="py-2 px-3">Time</th>
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
