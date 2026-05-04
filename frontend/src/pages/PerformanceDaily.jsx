/**
 * Daily Brief — OCC%, Revenue, ADR, RevPAR per branch with color bands
 */
import { useEffect, useState, useMemo } from "react";
import axios from "axios";
import TrendChart from "../components/TrendChart";
import SyncBadge from "../components/SyncBadge";
import { useBranch } from "../context/BranchContext";

const OCC_COLORS = [
  "#6366f1","#10b981","#f59e0b","#ef4444","#3b82f6",
];

function occBand(pct) {
  if (pct >= 0.90) return "text-green-700 bg-green-50";
  if (pct >= 0.70) return "text-blue-700 bg-blue-50";
  if (pct >= 0.50) return "text-yellow-700 bg-yellow-50";
  return "text-red-700 bg-red-50";
}

function cancelBand(pct) {
  if (pct <= 0.05) return "text-green-700 bg-green-50";
  if (pct <= 0.10) return "text-yellow-700 bg-yellow-50";
  if (pct <= 0.20) return "text-orange-700 bg-orange-50";
  return "text-red-700 bg-red-50";
}

function fmt(val, currency) {
  if (val == null) return "—";
  return new Intl.NumberFormat("en").format(Math.round(val));
}

export default function PerformanceDaily() {
  const { branches, selected, isAll } = useBranch();

  const today = new Date().toISOString().slice(0, 10);
  const [dateFrom, setDateFrom] = useState(() => {
    const d = new Date(); d.setDate(d.getDate() - 13);
    return d.toISOString().slice(0, 10);
  });
  const [dateTo,   setDateTo]   = useState(today);
  const [metrics,  setMetrics]  = useState([]);
  const [events,   setEvents]   = useState([]);
  const [loading,  setLoading]  = useState(true);

  // branch lookup map: id → { name, currency }
  const branchMap = useMemo(() => {
    const m = {};
    for (const b of branches) {
      m[b.id] = { name: b.name, currency: b.native_currency || b.currency || "VND" };
    }
    return m;
  }, [branches]);

  useEffect(() => {
    setLoading(true);
    const bParam = !isAll && selected ? `&branch_id=${selected}` : "";
    Promise.all([
      axios.get(`/api/metrics/daily?date_from=${dateFrom}&date_to=${dateTo}${bParam}`),
      axios.get(`/api/events?date_from=${dateFrom}&date_to=${dateTo}${bParam}`),
    ])
      .then(([mRes, eRes]) => {
        setMetrics(mRes.data.data || []);
        setEvents(eRes.data.data || []);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [dateFrom, dateTo, selected, isAll]);

  const branchIds = useMemo(
    () => [...new Set(metrics.map((m) => m.branch_id))].sort(),
    [metrics]
  );

  const lastSynced = useMemo(() => {
    const ts = metrics.map((m) => m.computed_at).filter(Boolean).sort();
    return ts.length ? ts[ts.length - 1] : null;
  }, [metrics]);

  const chartData = useMemo(() => {
    return [...new Set(metrics.map((m) => m.date))].sort().map((date) => {
      const row = { date };
      metrics.filter((m) => m.date === date).forEach((m) => {
        const key = m.branch_id?.slice(-4);
        row[`occ_${key}`] = +(m.occ_pct * 100).toFixed(2);
      });
      return row;
    });
  }, [metrics]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Daily Brief</h1>
          <p className="text-sm text-gray-500">
            OCC%, Revenue, ADR, RevPAR
            <SyncBadge timestamp={lastSynced} />
          </p>
          <p className="text-xs text-gray-400 mt-0.5">OCC: all sources (excl. Cancelled · No-show) · Revenue/ADR/RevPAR: excl. Blogger · House Use · KOL · Special Case · Work Exchange</p>
        </div>
        <div className="flex gap-2 items-center text-sm">
          <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)}
            className="border border-gray-200 rounded-lg px-3 py-1.5" />
          <span className="text-gray-400">→</span>
          <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)}
            className="border border-gray-200 rounded-lg px-3 py-1.5" />
        </div>
      </div>

      {/* Event pins */}
      {events.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {events.map((e) => (
            <span key={e.id} className={`text-xs px-2 py-0.5 rounded-full border ${
              e.is_key_event
                ? "bg-amber-100 text-amber-700 border-amber-200"
                : "bg-gray-100 text-gray-600 border-gray-200"
            }`}>
              📍 {e.event_name} ({e.event_date_from})
            </span>
          ))}
        </div>
      )}

      {loading ? (
        <div className="text-gray-400 animate-pulse">Loading…</div>
      ) : metrics.length === 0 ? (
        <div className="bg-white rounded-xl border p-8 text-center text-gray-400">
          No data for this range.
        </div>
      ) : (
        <>
          {/* OCC Trend Chart */}
          <TrendChart
            title="Daily OCC % by Branch"
            data={chartData}
            xKey="date"
            lines={branchIds.map((id, i) => ({
              key: `occ_${id.slice(-4)}`,
              name: branchMap[id]?.name || `…${id.slice(-4)}`,
              color: OCC_COLORS[i % OCC_COLORS.length],
            }))}
            formatY={(v) => `${Number(v).toFixed(2)}%`}
            formatTooltip={(v, name) => [`${Number(v).toFixed(2)}%`, name]}
          />

          {/* Data table */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-100">
                  <th className="px-4 py-3 text-left">Date</th>
                  <th className="px-4 py-3 text-left">Branch</th>
                  <th className="px-4 py-3 text-right">OCC%</th>
                  <th className="px-4 py-3 text-right">Revenue</th>
                  <th className="px-4 py-3 text-right">ADR</th>
                  <th className="px-4 py-3 text-right">RevPAR</th>
                  <th className="px-4 py-3 text-right">Cancel%</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {metrics.map((m) => {
                  const branch = branchMap[m.branch_id];
                  const cur = branch?.currency || "VND";
                  return (
                    <tr key={m.id} className="hover:bg-gray-50">
                      <td className="px-4 py-2.5 text-gray-600 tabular-nums">{m.date}</td>
                      <td className="px-4 py-2.5 text-gray-700 text-xs font-medium">
                        {branch?.name || `…${m.branch_id?.slice(-8)}`}
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${occBand(m.occ_pct)}`}>
                          {(m.occ_pct * 100).toFixed(2)}%
                        </span>
                      </td>
                      <td className="px-4 py-2.5 text-right text-gray-700 font-medium tabular-nums">
                        {fmt(m.revenue_native, cur)}
                      </td>
                      <td className="px-4 py-2.5 text-right text-gray-600 tabular-nums">
                        {fmt(m.adr_native, cur)}
                      </td>
                      <td className="px-4 py-2.5 text-right text-gray-600 tabular-nums">
                        {fmt(m.revpar_native, cur)}
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${cancelBand(m.cancellation_pct || 0)}`}>
                          {((m.cancellation_pct || 0) * 100).toFixed(2)}%
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
