/**
 * Weekly Brief — KPI cards, revenue/OCC/ADR trends, per-branch table, OTA mix
 */
import { useEffect, useState, useMemo, useCallback } from "react";
import axios from "axios";
import {
  BarChart, Bar, LineChart, Line,
  PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";
import SyncBadge from "../components/SyncBadge";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";

const BRANCH_COLORS = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#3b82f6"];
const OTA_COLORS    = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#a855f7", "#06b6d4"];

/* ── Formatters ─────────────────────────────────────────────────────────── */

function currSym(currency) {
  return CURRENCY_SYMBOLS[currency] || currency + " ";
}

function fmtCompact(val, currency) {
  if (val == null || val === 0) return "0";
  const sym = currSym(currency);
  return `${sym}${new Intl.NumberFormat("en").format(Math.round(val))}`;
}

function fmtNumber(val) {
  if (val == null) return "--";
  return new Intl.NumberFormat("en").format(Math.round(val));
}

function fmtPct(val) {
  if (val == null) return "--";
  return `${(val * 100).toFixed(2)}%`;
}

function pctChange(curr, prev) {
  if (prev == null || prev === 0 || curr == null) return null;
  return ((curr - prev) / Math.abs(prev)) * 100;
}

function occColor(val) {
  const v = (val || 0) * 100;
  if (v >= 90) return "text-emerald-600";
  if (v >= 70) return "text-blue-600";
  if (v >= 50) return "text-yellow-600";
  return "text-red-600";
}

// Merge OTA categories case-insensitively
function mergeOtaMix(rows) {
  const map = {};
  for (const r of rows) {
    const key = r.category?.toLowerCase() || "other";
    if (!map[key]) map[key] = { category: key, count: 0, revenue_native: 0 };
    map[key].count += r.count;
    map[key].revenue_native += r.revenue_native;
  }
  const total = Object.values(map).reduce((s, r) => s + r.count, 0);
  return Object.values(map)
    .sort((a, b) => b.count - a.count)
    .map((r) => ({ ...r, pct: total > 0 ? (r.count / total) * 100 : 0 }));
}

/* ── KPI Card ───────────────────────────────────────────────────────────── */

function KpiCard({ label, value, change, suffix = "" }) {
  const changeColor = change == null ? "" : change >= 0 ? "text-emerald-600" : "text-red-500";
  const arrow = change == null ? "" : change >= 0 ? "\u2191" : "\u2193";
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4 flex flex-col justify-between min-h-[100px]">
      <p className="text-xs text-gray-500 uppercase tracking-wide">{label}</p>
      <p className="text-2xl font-bold text-gray-900 mt-1">{value}{suffix}</p>
      {change != null && (
        <p className={`text-xs font-medium mt-1 ${changeColor}`}>
          {arrow} {Math.abs(change).toFixed(2)}% WoW
        </p>
      )}
    </div>
  );
}

/* ── Sortable Table ─────────────────────────────────────────────────────── */

function WeeklyTable({ rows, branchMap }) {
  const [sortKey, setSortKey] = useState("week_start");
  const [sortAsc, setSortAsc] = useState(false);

  const handleSort = useCallback((key) => {
    if (sortKey === key) {
      setSortAsc((p) => !p);
    } else {
      setSortKey(key);
      setSortAsc(true);
    }
  }, [sortKey]);

  const sorted = useMemo(() => {
    return [...rows].sort((a, b) => {
      let va = a[sortKey], vb = b[sortKey];
      if (typeof va === "string") return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
      return sortAsc ? (va || 0) - (vb || 0) : (vb || 0) - (va || 0);
    });
  }, [rows, sortKey, sortAsc]);

  const cols = [
    { key: "week_start", label: "Week", align: "left" },
    { key: "branch_id", label: "Branch", align: "left" },
    { key: "revenue_native", label: "Revenue", align: "right" },
    { key: "avg_occ_pct", label: "OCC%", align: "right" },
    { key: "avg_adr_native", label: "ADR", align: "right" },
    { key: "avg_revpar_native", label: "RevPAR", align: "right" },
    { key: "total_sold", label: "Sold", align: "right" },
    { key: "cancellation_pct", label: "Cancel%", align: "right" },
  ];

  const arrow = (key) => sortKey === key ? (sortAsc ? " \u25B2" : " \u25BC") : "";

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
      <div className="px-4 pt-4 pb-2">
        <p className="text-sm font-semibold text-gray-700">Per-Branch Weekly Breakdown</p>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-100 bg-gray-50">
            {cols.map((c) => (
              <th
                key={c.key}
                className={`px-4 py-2 cursor-pointer select-none whitespace-nowrap ${c.align === "right" ? "text-right" : "text-left"}`}
                onClick={() => handleSort(c.key)}
              >
                {c.label}{arrow(c.key)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-50">
          {sorted.map((r, i) => {
            const branch = branchMap[r.branch_id];
            const cur = branch?.native_currency || branch?.currency || "VND";
            return (
              <tr key={i} className="hover:bg-gray-50">
                <td className="px-4 py-2 text-gray-600 font-medium whitespace-nowrap">{r.week_start}</td>
                <td className="px-4 py-2 text-gray-700 whitespace-nowrap">{branch?.name || r.branch_id}</td>
                <td className="px-4 py-2 text-right font-semibold text-gray-800 tabular-nums">{fmtCompact(r.revenue_native, cur)}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${occColor(r.avg_occ_pct)}`}>{fmtPct(r.avg_occ_pct)}</td>
                <td className="px-4 py-2 text-right text-gray-600 tabular-nums">{fmtCompact(r.avg_adr_native, cur)}</td>
                <td className="px-4 py-2 text-right text-gray-600 tabular-nums">{fmtCompact(r.avg_revpar_native, cur)}</td>
                <td className="px-4 py-2 text-right text-gray-500 tabular-nums">{fmtNumber(r.total_sold)}</td>
                <td className="px-4 py-2 text-right text-gray-500 tabular-nums">{fmtPct(r.cancellation_pct)}</td>
              </tr>
            );
          })}
          {sorted.length === 0 && (
            <tr><td colSpan={cols.length} className="px-4 py-8 text-center text-gray-400">No data</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/* ── Main Component ─────────────────────────────────────────────────────── */

export default function PerformanceWeekly() {
  const { branches, selected, isAll, currency } = useBranch();

  const branchMap = useMemo(() => {
    const m = {};
    for (const b of branches) {
      m[b.id] = { ...b, name: b.name, currency: b.native_currency || b.currency || "VND" };
    }
    return m;
  }, [branches]);

  const [weeklyByBranch, setWeeklyByBranch] = useState({});
  const [otaMixByBranch, setOtaMixByBranch] = useState({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!branches.length) return;
    setLoading(true);

    const targetBranches = !isAll && selected
      ? branches.filter((b) => b.id === selected)
      : branches;

    const weeklyFetches = targetBranches.map((b) =>
      axios.get(`/api/metrics/weekly?branch_id=${b.id}`).then((r) => [b.id, r.data.data || []])
    );
    const otaFetches = targetBranches.map((b) =>
      axios.get(`/api/metrics/ota-mix?branch_id=${b.id}`).then((r) => [b.id, r.data.data || []])
    );

    Promise.all([Promise.all(weeklyFetches), Promise.all(otaFetches)])
      .then(([weeklyResults, otaResults]) => {
        const wMap = {};
        for (const [id, rows] of weeklyResults) wMap[id] = rows;
        setWeeklyByBranch(wMap);

        const oMap = {};
        for (const [id, rows] of otaResults) oMap[id] = mergeOtaMix(rows);
        setOtaMixByBranch(oMap);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [branches, selected, isAll]);

  const branchIds = useMemo(() => Object.keys(weeklyByBranch), [weeklyByBranch]);

  /* ── KPI summary: this week vs last week ── */
  const kpis = useMemo(() => {
    // Combine all branch data into a flat array sorted by week
    const allRows = [];
    for (const [bid, rows] of Object.entries(weeklyByBranch)) {
      for (const r of rows) allRows.push({ ...r, branch_id: bid });
    }
    if (!allRows.length) return null;

    // Get all unique weeks sorted desc
    const weeks = [...new Set(allRows.map((r) => r.week_start))].sort().reverse();
    if (!weeks.length) return null;

    const thisWeek = weeks[0];
    const lastWeek = weeks[1] || null;

    // Aggregate for selected scope
    const agg = (week) => {
      const wRows = allRows.filter((r) => r.week_start === week);
      if (!wRows.length) return null;
      const totalRevenue = wRows.reduce((s, r) => s + (r.revenue_vnd || r.revenue_native || 0), 0);
      const avgOcc = wRows.reduce((s, r) => s + (r.avg_occ_pct || 0), 0) / wRows.length;
      const avgAdr = wRows.reduce((s, r) => s + (r.avg_adr_native || 0), 0) / wRows.length;
      const totalSold = wRows.reduce((s, r) => s + (r.total_sold || 0), 0);
      return { revenue: totalRevenue, occ: avgOcc, adr: avgAdr, sold: totalSold };
    };

    const curr = agg(thisWeek);
    const prev = lastWeek ? agg(lastWeek) : null;
    if (!curr) return null;

    const displayCur = isAll ? "VND" : (branchMap[selected]?.currency || currency || "VND");

    // If single branch, use native revenue
    let revenueCurr = curr.revenue;
    let revenuePrev = prev?.revenue ?? null;
    if (!isAll && selected) {
      const thisRows = allRows.filter((r) => r.week_start === thisWeek);
      const prevRows = lastWeek ? allRows.filter((r) => r.week_start === lastWeek) : [];
      revenueCurr = thisRows.reduce((s, r) => s + (r.revenue_native || 0), 0);
      revenuePrev = prevRows.length ? prevRows.reduce((s, r) => s + (r.revenue_native || 0), 0) : null;
    }

    return {
      thisWeek,
      revenue: fmtCompact(revenueCurr, displayCur),
      revenueChange: pctChange(revenueCurr, revenuePrev),
      occ: fmtPct(curr.occ),
      occChange: prev ? pctChange(curr.occ, prev.occ) : null,
      adr: fmtCompact(curr.adr, displayCur),
      adrChange: prev ? pctChange(curr.adr, prev.adr) : null,
      sold: fmtNumber(curr.sold),
    };
  }, [weeklyByBranch, isAll, selected, branchMap, currency]);

  /* ── Chart data: revenue (stacked bar) ── */
  const revenueChartData = useMemo(() => {
    const dateSet = new Set();
    for (const rows of Object.values(weeklyByBranch)) {
      rows.forEach((r) => dateSet.add(r.week_start));
    }
    return [...dateSet].sort().map((week) => {
      const row = { week };
      for (const [bid, rows] of Object.entries(weeklyByBranch)) {
        const found = rows.find((r) => r.week_start === week);
        row[bid] = found ? found.revenue_native : 0;
      }
      return row;
    });
  }, [weeklyByBranch]);

  /* ── Chart data: OCC% (multi-line) ── */
  const occChartData = useMemo(() => {
    const dateSet = new Set();
    for (const rows of Object.values(weeklyByBranch)) {
      rows.forEach((r) => dateSet.add(r.week_start));
    }
    return [...dateSet].sort().map((week) => {
      const row = { week };
      for (const [bid, rows] of Object.entries(weeklyByBranch)) {
        const found = rows.find((r) => r.week_start === week);
        row[bid] = found ? +((found.avg_occ_pct || 0) * 100).toFixed(2) : null;
      }
      return row;
    });
  }, [weeklyByBranch]);

  /* ── Chart data: ADR (multi-line) ── */
  const adrChartData = useMemo(() => {
    const dateSet = new Set();
    for (const rows of Object.values(weeklyByBranch)) {
      rows.forEach((r) => dateSet.add(r.week_start));
    }
    return [...dateSet].sort().map((week) => {
      const row = { week };
      for (const [bid, rows] of Object.entries(weeklyByBranch)) {
        const found = rows.find((r) => r.week_start === week);
        row[bid] = found ? +(found.avg_adr_native || 0).toFixed(0) : null;
      }
      return row;
    });
  }, [weeklyByBranch]);

  /* ── Flat table data ── */
  const tableRows = useMemo(() => {
    const flat = [];
    for (const [bid, rows] of Object.entries(weeklyByBranch)) {
      for (const r of rows) flat.push({ ...r, branch_id: bid });
    }
    return flat;
  }, [weeklyByBranch]);

  /* ── Date range label ── */
  const dateRange = useMemo(() => {
    const weeks = [...new Set(tableRows.map((r) => r.week_start))].sort();
    if (!weeks.length) return "";
    return `${weeks[0]} to ${weeks[weeks.length - 1]}`;
  }, [tableRows]);

  const lastSynced = useMemo(() => {
    const ts = tableRows.map((r) => r.data_synced_at).filter(Boolean).sort();
    return ts.length ? ts[ts.length - 1] : null;
  }, [tableRows]);

  /* ── Render ───────────────────────────────────────────────────────────── */

  if (loading) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Weekly Brief</h1>
        </div>
        <div className="text-gray-400 animate-pulse">Loading...</div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-xl font-bold text-gray-800">Weekly Brief</h1>
        <p className="text-sm text-gray-500">
          Last 13 weeks {dateRange && `\u00B7 ${dateRange}`}
          <SyncBadge timestamp={lastSynced} />
        </p>
      </div>

      {/* KPI Summary Cards */}
      {kpis && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <KpiCard label="This Week Revenue" value={kpis.revenue} change={kpis.revenueChange} />
          <KpiCard label="This Week OCC%" value={kpis.occ} change={kpis.occChange} />
          <KpiCard label="This Week ADR" value={kpis.adr} change={kpis.adrChange} />
          <KpiCard label="Rooms Sold" value={kpis.sold} />
        </div>
      )}

      {/* Revenue Trend — Stacked Bar Chart */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
        <p className="text-sm font-semibold text-gray-700 mb-3">Weekly Revenue by Branch</p>
        {revenueChartData.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-gray-400 text-sm">No data available</div>
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={revenueChartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis
                dataKey="week"
                tick={{ fontSize: 11, fill: "#9ca3af" }}
                tickFormatter={(v) => v?.slice(5)}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                tick={{ fontSize: 11, fill: "#9ca3af" }}
                tickFormatter={(v) => new Intl.NumberFormat("en").format(Math.round(v))}
                tickLine={false}
                axisLine={false}
                width={72}
              />
              <Tooltip
                formatter={(v, name) => {
                  const branch = branchMap[name];
                  return [fmtCompact(v, branch?.currency || "VND"), branch?.name || name];
                }}
                labelFormatter={(l) => `Week of ${l}`}
                contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e5e7eb" }}
              />
              <Legend formatter={(id) => branchMap[id]?.name || id} iconSize={10} wrapperStyle={{ fontSize: 12 }} />
              {branchIds.map((id, i) => (
                <Bar key={id} dataKey={id} stackId="rev" fill={BRANCH_COLORS[i % BRANCH_COLORS.length]} radius={[2, 2, 0, 0]} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* OCC% Trend — Multi-line */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
        <p className="text-sm font-semibold text-gray-700 mb-3">Weekly OCC% by Branch</p>
        {occChartData.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-gray-400 text-sm">No data available</div>
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={occChartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis
                dataKey="week"
                tick={{ fontSize: 11, fill: "#9ca3af" }}
                tickFormatter={(v) => v?.slice(5)}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                tick={{ fontSize: 11, fill: "#9ca3af" }}
                tickFormatter={(v) => `${v.toFixed(2)}%`}
                tickLine={false}
                axisLine={false}
                width={56}
                domain={[0, 100]}
              />
              <Tooltip
                formatter={(v, name) => [`${Number(v).toFixed(2)}%`, branchMap[name]?.name || name]}
                labelFormatter={(l) => `Week of ${l}`}
                contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e5e7eb" }}
              />
              <Legend formatter={(id) => branchMap[id]?.name || id} iconSize={10} wrapperStyle={{ fontSize: 12 }} />
              {branchIds.map((id, i) => (
                <Line
                  key={id}
                  type="monotone"
                  dataKey={id}
                  stroke={BRANCH_COLORS[i % BRANCH_COLORS.length]}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4 }}
                  connectNulls
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* ADR Trend — Multi-line */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
        <p className="text-sm font-semibold text-gray-700 mb-3">Weekly ADR by Branch</p>
        {adrChartData.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-gray-400 text-sm">No data available</div>
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={adrChartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis
                dataKey="week"
                tick={{ fontSize: 11, fill: "#9ca3af" }}
                tickFormatter={(v) => v?.slice(5)}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                tick={{ fontSize: 11, fill: "#9ca3af" }}
                tickFormatter={(v) => new Intl.NumberFormat("en").format(Math.round(v))}
                tickLine={false}
                axisLine={false}
                width={72}
              />
              <Tooltip
                formatter={(v, name) => {
                  const branch = branchMap[name];
                  return [fmtCompact(v, branch?.currency || "VND"), branch?.name || name];
                }}
                labelFormatter={(l) => `Week of ${l}`}
                contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e5e7eb" }}
              />
              <Legend formatter={(id) => branchMap[id]?.name || id} iconSize={10} wrapperStyle={{ fontSize: 12 }} />
              {branchIds.map((id, i) => (
                <Line
                  key={id}
                  type="monotone"
                  dataKey={id}
                  stroke={BRANCH_COLORS[i % BRANCH_COLORS.length]}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4 }}
                  connectNulls
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Per-Branch Weekly Table */}
      <WeeklyTable rows={tableRows} branchMap={branchMap} />

      {/* OTA Mix Pie Charts */}
      {branchIds.some((bid) => (otaMixByBranch[bid] || []).length > 0) && (
        <div>
          <p className="text-sm font-semibold text-gray-700 mb-3">OTA vs Direct Mix (Last 30 days)</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {branchIds.map((bid) => {
              const mix = otaMixByBranch[bid] || [];
              const branch = branchMap[bid];
              if (!mix.length) return null;
              return (
                <div key={bid} className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
                  <p className="text-xs font-semibold text-gray-600 mb-2">{branch?.name}</p>
                  <ResponsiveContainer width="100%" height={160}>
                    <PieChart>
                      <Pie
                        data={mix}
                        dataKey="count"
                        nameKey="category"
                        cx="50%"
                        cy="50%"
                        outerRadius={60}
                        label={({ pct }) => `${pct.toFixed(2)}%`}
                        labelLine={false}
                      >
                        {mix.map((_, i) => (
                          <Cell key={i} fill={OTA_COLORS[i % OTA_COLORS.length]} />
                        ))}
                      </Pie>
                      <Tooltip formatter={(v, name) => [`${v} bookings`, name]} />
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="flex flex-wrap gap-2 mt-1">
                    {mix.map((o, i) => (
                      <span key={o.category} className="flex items-center gap-1 text-xs text-gray-500">
                        <span className="w-2 h-2 rounded-full inline-block" style={{ background: OTA_COLORS[i % OTA_COLORS.length] }} />
                        {o.category} {o.pct.toFixed(2)}%
                      </span>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
