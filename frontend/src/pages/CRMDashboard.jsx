/**
 * CRM Dashboard — Revenue & Booking analytics for CRM room types.
 * Queries reservations where room_type contains "CRM".
 */
import { useEffect, useState, useMemo } from "react";
import { useBranch } from "../context/BranchContext";
import {
  getCRMSummary,
  getCRMDaily,
  getCRMMonthly,
  getCRMByBranch,
  getCRMBySource,
  getCRMRoomTypes,
  getCRMReservations,
} from "../api/crm";
import TrendChart from "../components/TrendChart";
import SyncBadge from "../components/SyncBadge";

function fmt(val, style = "number") {
  if (val == null) return "—";
  const n = Math.round(val);
  return new Intl.NumberFormat("en").format(n);
}

function pct(val) {
  if (val == null) return "—";
  return `${(val * 100).toFixed(2)}%`;
}

function cancelBand(rate) {
  if (rate <= 0.05) return "text-green-700 bg-green-50";
  if (rate <= 0.10) return "text-yellow-700 bg-yellow-50";
  if (rate <= 0.20) return "text-orange-700 bg-orange-50";
  return "text-red-700 bg-red-50";
}

const TABS = ["overview", "monthly", "reservations"];
const TAB_LABELS = { overview: "Overview", monthly: "Monthly", reservations: "Reservations" };

export default function CRMDashboard() {
  const { branches, selected, isAll } = useBranch();

  const today = new Date().toISOString().slice(0, 10);
  const [dateFrom, setDateFrom] = useState(() => {
    const d = new Date(); d.setDate(d.getDate() - 29);
    return d.toISOString().slice(0, 10);
  });
  const [dateTo, setDateTo] = useState(today);
  const [tab, setTab] = useState("overview");

  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState(null);
  const [daily, setDaily] = useState([]);
  const [monthly, setMonthly] = useState([]);
  const [byBranch, setByBranch] = useState([]);
  const [bySource, setBySource] = useState([]);
  const [roomTypes, setRoomTypes] = useState([]);
  const [reservations, setReservations] = useState({ items: [], total: 0 });

  const branchMap = useMemo(() => {
    const m = {};
    for (const b of branches) {
      m[b.id] = { name: b.name, currency: b.native_currency || b.currency || "VND" };
    }
    return m;
  }, [branches]);

  const params = useMemo(() => {
    const p = { date_from: dateFrom, date_to: dateTo };
    if (!isAll && selected) p.branch_id = selected;
    return p;
  }, [dateFrom, dateTo, selected, isAll]);

  // Fetch data based on active tab
  useEffect(() => {
    setLoading(true);

    if (tab === "overview") {
      Promise.all([
        getCRMSummary(params),
        getCRMDaily(params),
        getCRMByBranch({ date_from: dateFrom, date_to: dateTo }),
        getCRMBySource(params),
        getCRMRoomTypes(params),
      ])
        .then(([s, d, b, src, rt]) => {
          setSummary(s);
          setDaily(d || []);
          setByBranch(b || []);
          setBySource(src || []);
          setRoomTypes(rt || []);
        })
        .catch(console.error)
        .finally(() => setLoading(false));
    } else if (tab === "monthly") {
      getCRMMonthly(params)
        .then(d => setMonthly(d || []))
        .catch(console.error)
        .finally(() => setLoading(false));
    } else if (tab === "reservations") {
      getCRMReservations({ ...params, limit: 100 })
        .then(d => setReservations(d || { items: [], total: 0 }))
        .catch(console.error)
        .finally(() => setLoading(false));
    }
  }, [tab, params, dateFrom, dateTo]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">CRM Dashboard</h1>
          <p className="text-sm text-gray-500">
            Revenue & Bookings from CRM room types
            <SyncBadge timestamp={summary?.data_synced_at} />
          </p>
        </div>
        <div className="flex gap-2 items-center text-sm">
          <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
            className="border border-gray-200 rounded-lg px-3 py-1.5" />
          <span className="text-gray-400">&rarr;</span>
          <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
            className="border border-gray-200 rounded-lg px-3 py-1.5" />
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-100 rounded-lg p-1 w-fit">
        {TABS.map(t => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
              tab === t ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
            }`}>
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="text-gray-400 animate-pulse">Loading...</div>
      ) : tab === "overview" ? (
        <OverviewTab
          summary={summary}
          daily={daily}
          byBranch={byBranch}
          bySource={bySource}
          roomTypes={roomTypes}
          branchMap={branchMap}
        />
      ) : tab === "monthly" ? (
        <MonthlyTab monthly={monthly} />
      ) : (
        <ReservationsTab reservations={reservations} branchMap={branchMap} />
      )}
    </div>
  );
}

/* ── Overview Tab ─────────────────────────────────────────────────────────── */
function OverviewTab({ summary, daily, byBranch, bySource, roomTypes, branchMap }) {
  if (!summary) {
    return <div className="bg-white rounded-xl border p-8 text-center text-gray-400">No CRM data for this range.</div>;
  }

  const kpis = [
    { label: "Total Bookings", value: summary.total_bookings, color: "text-indigo-700 bg-indigo-50" },
    { label: "Confirmed", value: summary.confirmed_bookings, color: "text-green-700 bg-green-50" },
    { label: "Revenue", value: fmt(summary.total_revenue_native, "compact"), color: "text-blue-700 bg-blue-50" },
    { label: "Avg Nights", value: summary.avg_nights, color: "text-purple-700 bg-purple-50" },
    { label: "Total Nights", value: summary.total_nights, color: "text-teal-700 bg-teal-50" },
    { label: "Cancel Rate", value: pct(summary.cancellation_rate), color: cancelBand(summary.cancellation_rate) },
  ];

  return (
    <div className="space-y-6">
      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        {kpis.map(k => (
          <div key={k.label} className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
            <p className="text-xs text-gray-500 mb-1">{k.label}</p>
            <p className={`text-lg font-bold ${k.color} px-2 py-0.5 rounded inline-block`}>{k.value}</p>
          </div>
        ))}
      </div>

      {/* Revenue + Bookings Trend */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TrendChart
          title="Daily Revenue"
          data={daily}
          xKey="date"
          bars={[{ key: "revenue_native", name: "Revenue", color: "#6366f1" }]}
        />
        <TrendChart
          title="Daily Bookings"
          data={daily}
          xKey="date"
          lines={[{ key: "bookings", name: "Bookings", color: "#10b981" }]}
          formatY={v => v}
        />
      </div>

      {/* By Branch */}
      {byBranch.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
          <p className="px-4 pt-4 text-sm font-semibold text-gray-700">By Branch</p>
          <table className="w-full text-sm mt-2">
            <thead>
              <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-100">
                <th className="px-4 py-2 text-left">Branch</th>
                <th className="px-4 py-2 text-right">Bookings</th>
                <th className="px-4 py-2 text-right">Confirmed</th>
                <th className="px-4 py-2 text-right">Revenue</th>
                <th className="px-4 py-2 text-right">Nights</th>
                <th className="px-4 py-2 text-right">ADR</th>
                <th className="px-4 py-2 text-right">Cancel%</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {byBranch.map(r => (
                <tr key={r.branch_id} className="hover:bg-gray-50">
                  <td className="px-4 py-2 text-gray-700 font-medium">
                    {branchMap[r.branch_id]?.name || `...${r.branch_id.slice(-6)}`}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">{r.bookings}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-green-700">{r.confirmed}</td>
                  <td className="px-4 py-2 text-right tabular-nums font-medium">{fmt(r.revenue_native)}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{r.nights}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{fmt(r.adr_native)}</td>
                  <td className="px-4 py-2 text-right">
                    <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${cancelBand(r.cancellation_rate)}`}>
                      {pct(r.cancellation_rate)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Room Types + Sources side by side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Room Types */}
        {roomTypes.length > 0 && (
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
            <p className="text-sm font-semibold text-gray-700 mb-3">CRM Room Types</p>
            <div className="space-y-2">
              {roomTypes.map(rt => {
                const totalRev = roomTypes.reduce((s, r) => s + r.revenue_native, 0);
                const pctShare = totalRev > 0 ? (rt.revenue_native / totalRev) * 100 : 0;
                return (
                  <div key={rt.room_type} className="flex items-center justify-between text-sm">
                    <div className="flex-1 min-w-0">
                      <p className="text-gray-700 truncate text-xs">{rt.room_type}</p>
                      <div className="w-full bg-gray-100 rounded-full h-1.5 mt-1">
                        <div className="bg-indigo-500 h-1.5 rounded-full" style={{ width: `${pctShare}%` }} />
                      </div>
                    </div>
                    <div className="ml-3 text-right shrink-0">
                      <span className="text-xs text-gray-600">{rt.bookings} bk</span>
                      <span className="text-xs text-gray-400 ml-2">{fmt(rt.revenue_native, "compact")}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* By Source */}
        {bySource.length > 0 && (
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
            <p className="text-sm font-semibold text-gray-700 mb-3">Booking Source</p>
            <div className="space-y-2">
              {bySource.map(s => {
                const totalBk = bySource.reduce((sum, x) => sum + x.bookings, 0);
                const pctShare = totalBk > 0 ? (s.bookings / totalBk) * 100 : 0;
                return (
                  <div key={s.source} className="flex items-center justify-between text-sm">
                    <div className="flex-1 min-w-0">
                      <p className="text-gray-700 truncate text-xs">{s.source}</p>
                      <div className="w-full bg-gray-100 rounded-full h-1.5 mt-1">
                        <div className={`h-1.5 rounded-full ${
                          s.source_category === "Direct" ? "bg-green-500" : "bg-blue-500"
                        }`} style={{ width: `${pctShare}%` }} />
                      </div>
                    </div>
                    <div className="ml-3 text-right shrink-0">
                      <span className={`text-xs px-1.5 py-0.5 rounded ${
                        s.source_category === "Direct" ? "bg-green-50 text-green-700" : "bg-blue-50 text-blue-700"
                      }`}>{s.source_category}</span>
                      <span className="text-xs text-gray-600 ml-2">{s.bookings}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Monthly Tab ──────────────────────────────────────────────────────────── */
function MonthlyTab({ monthly }) {
  if (monthly.length === 0) {
    return <div className="bg-white rounded-xl border p-8 text-center text-gray-400">No monthly CRM data.</div>;
  }

  const chartData = monthly.map(m => ({
    label: `${m.year}-${String(m.month).padStart(2, "0")}`,
    revenue_native: m.revenue_native,
    bookings: m.bookings,
  }));

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TrendChart
          title="Monthly Revenue"
          data={chartData}
          xKey="label"
          bars={[{ key: "revenue_native", name: "Revenue", color: "#6366f1" }]}
        />
        <TrendChart
          title="Monthly Bookings"
          data={chartData}
          xKey="label"
          bars={[{ key: "bookings", name: "Bookings", color: "#10b981" }]}
          formatY={v => v}
        />
      </div>

      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-100">
              <th className="px-4 py-3 text-left">Month</th>
              <th className="px-4 py-3 text-right">Bookings</th>
              <th className="px-4 py-3 text-right">Confirmed</th>
              <th className="px-4 py-3 text-right">Revenue</th>
              <th className="px-4 py-3 text-right">Nights</th>
              <th className="px-4 py-3 text-right">ADR</th>
              <th className="px-4 py-3 text-right">Cancel%</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {monthly.map(m => (
              <tr key={`${m.year}-${m.month}`} className="hover:bg-gray-50">
                <td className="px-4 py-2.5 text-gray-700 font-medium tabular-nums">
                  {m.year}-{String(m.month).padStart(2, "0")}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums">{m.bookings}</td>
                <td className="px-4 py-2.5 text-right tabular-nums text-green-700">{m.confirmed}</td>
                <td className="px-4 py-2.5 text-right tabular-nums font-medium">{fmt(m.revenue_native)}</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{m.nights}</td>
                <td className="px-4 py-2.5 text-right tabular-nums">{fmt(m.adr_native)}</td>
                <td className="px-4 py-2.5 text-right">
                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${cancelBand(m.cancellation_rate)}`}>
                    {pct(m.cancellation_rate)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ── Reservations Tab ─────────────────────────────────────────────────────── */
function ReservationsTab({ reservations, branchMap }) {
  const { items, total } = reservations;

  if (items.length === 0) {
    return <div className="bg-white rounded-xl border p-8 text-center text-gray-400">No CRM reservations found.</div>;
  }

  const statusColor = (s) => {
    if (s === "Confirmed") return "text-blue-700 bg-blue-50";
    if (s === "Checked Out") return "text-green-700 bg-green-50";
    if (s === "Cancelled") return "text-red-700 bg-red-50";
    return "text-gray-700 bg-gray-50";
  };

  return (
    <div className="space-y-3">
      <p className="text-sm text-gray-500">{total} total reservations</p>
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-100">
              <th className="px-4 py-3 text-left">Check-in</th>
              <th className="px-4 py-3 text-left">Branch</th>
              <th className="px-4 py-3 text-left">Room Type</th>
              <th className="px-4 py-3 text-left">Source</th>
              <th className="px-4 py-3 text-left">Country</th>
              <th className="px-4 py-3 text-right">Nights</th>
              <th className="px-4 py-3 text-right">Revenue</th>
              <th className="px-4 py-3 text-center">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {items.map(r => (
              <tr key={r.id} className="hover:bg-gray-50">
                <td className="px-4 py-2 text-gray-600 tabular-nums">{r.check_in_date}</td>
                <td className="px-4 py-2 text-gray-700 text-xs font-medium">
                  {branchMap[r.branch_id]?.name || "—"}
                </td>
                <td className="px-4 py-2 text-gray-600 text-xs truncate max-w-[200px]">{r.room_type}</td>
                <td className="px-4 py-2 text-gray-600 text-xs">{r.source || "—"}</td>
                <td className="px-4 py-2 text-gray-600 text-xs">{r.guest_country || "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">{r.nights}</td>
                <td className="px-4 py-2 text-right tabular-nums font-medium">{fmt(r.grand_total_native)}</td>
                <td className="px-4 py-2 text-center">
                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${statusColor(r.status)}`}>
                    {r.status}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
