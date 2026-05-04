/**
 * Monthly Brief — KPI cards, revenue/OCC/ADR+RevPAR trends, per-branch table,
 * YoY comparison, country breakdown. Multi-year support.
 */
import { useEffect, useState, useMemo, useCallback } from "react";
import axios from "axios";
import {
  BarChart, Bar, LineChart, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";
import SyncBadge from "../components/SyncBadge";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";

const BRANCH_COLORS = ["#6366f1", "#10b981", "#f59e0b", "#ef4444", "#3b82f6"];
const MONTH_LABELS  = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/* ── Formatters ─────────────────────────────────────────────────────────── */

function currSym(currency) {
  return CURRENCY_SYMBOLS[currency] || currency + " ";
}

function fmt(val, currency) {
  if (val == null || val === 0) return "--";
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
          {arrow} {Math.abs(change).toFixed(2)}% MoM
        </p>
      )}
    </div>
  );
}

/* ── Sortable Monthly Table with YoY column ─────────────────────────────── */

function MonthlyTable({ rows, branchMap, allMonthly }) {
  const [sortKey, setSortKey] = useState("sort_key");
  const [sortAsc, setSortAsc] = useState(false);

  const handleSort = useCallback((key) => {
    if (sortKey === key) setSortAsc((p) => !p);
    else { setSortKey(key); setSortAsc(true); }
  }, [sortKey]);

  // Build YoY lookup: for each row, find same branch+month from prior year
  const enriched = useMemo(() => {
    return rows.map((r) => {
      const priorYear = r.year - 1;
      const prev = allMonthly.find(
        (m) => m.branch_id === r.branch_id && m.year === priorYear && m.month === r.month
      );
      const yoyRevChange = prev ? pctChange(r.revenue_native, prev.revenue_native) : null;
      return {
        ...r,
        sort_key: `${r.year}-${String(r.month).padStart(2, "0")}`,
        month_label: `${MONTH_LABELS[r.month]} ${r.year}`,
        yoy_rev_change: yoyRevChange,
      };
    });
  }, [rows, allMonthly]);

  const sorted = useMemo(() => {
    return [...enriched].sort((a, b) => {
      let va = a[sortKey], vb = b[sortKey];
      if (typeof va === "string") return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
      return sortAsc ? ((va ?? -Infinity) - (vb ?? -Infinity)) : ((vb ?? -Infinity) - (va ?? -Infinity));
    });
  }, [enriched, sortKey, sortAsc]);

  const cols = [
    { key: "sort_key", label: "Month", align: "left" },
    { key: "branch_id", label: "Branch", align: "left" },
    { key: "revenue_native", label: "Revenue", align: "right" },
    { key: "avg_occ_pct", label: "OCC%", align: "right" },
    { key: "avg_adr_native", label: "ADR", align: "right" },
    { key: "avg_revpar_native", label: "RevPAR", align: "right" },
    { key: "total_sold", label: "Sold", align: "right" },
    { key: "yoy_rev_change", label: "YoY Rev", align: "right" },
  ];

  const arrow = (key) => sortKey === key ? (sortAsc ? " \u25B2" : " \u25BC") : "";

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
      <div className="px-4 pt-4 pb-2">
        <p className="text-sm font-semibold text-gray-700">Per-Branch Monthly Breakdown</p>
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
            const yoyColor = r.yoy_rev_change == null ? "text-gray-400"
              : r.yoy_rev_change >= 0 ? "text-emerald-600" : "text-red-500";
            const yoyArrow = r.yoy_rev_change == null ? "" : r.yoy_rev_change >= 0 ? "\u2191" : "\u2193";
            return (
              <tr key={i} className="hover:bg-gray-50">
                <td className="px-4 py-2 text-gray-600 font-medium whitespace-nowrap">{r.month_label}</td>
                <td className="px-4 py-2 text-gray-700 whitespace-nowrap">{branch?.name || r.branch_id}</td>
                <td className="px-4 py-2 text-right font-semibold text-gray-800 tabular-nums">{fmt(r.revenue_native, cur)}</td>
                <td className={`px-4 py-2 text-right tabular-nums ${occColor(r.avg_occ_pct)}`}>{fmtPct(r.avg_occ_pct)}</td>
                <td className="px-4 py-2 text-right text-gray-600 tabular-nums">{fmt(r.avg_adr_native, cur)}</td>
                <td className="px-4 py-2 text-right text-gray-600 tabular-nums">{fmt(r.avg_revpar_native, cur)}</td>
                <td className="px-4 py-2 text-right text-gray-500 tabular-nums">{fmtNumber(r.total_sold)}</td>
                <td className={`px-4 py-2 text-right text-xs font-medium tabular-nums ${yoyColor}`}>
                  {r.yoy_rev_change != null ? `${yoyArrow} ${Math.abs(r.yoy_rev_change).toFixed(2)}%` : "--"}
                </td>
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

export default function PerformanceMonthly() {
  const { branches, selected, isAll, currency } = useBranch();
  const now = new Date();
  const [yearFrom, setYearFrom] = useState(now.getFullYear() - 1);
  const [yearTo,   setYearTo]   = useState(now.getFullYear());
  const [monthly,  setMonthly]  = useState([]);
  const [loading,  setLoading]  = useState(true);

  const branchMap = useMemo(() => {
    const m = {};
    for (const b of branches) m[b.id] = b;
    return m;
  }, [branches]);

  useEffect(() => {
    setLoading(true);
    const bParam = !isAll && selected ? `&branch_id=${selected}` : "";
    axios.get(`/api/metrics/monthly?year_from=${yearFrom}&year_to=${yearTo}${bParam}`)
      .then((r) => setMonthly(r.data.data || []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [yearFrom, yearTo, selected, isAll]);

  /* ── KPI: current month vs prior month ── */
  const kpis = useMemo(() => {
    if (!monthly.length) return null;

    // Sort desc to find latest month
    const sorted = [...monthly].sort((a, b) => b.year - a.year || b.month - a.month);
    const latestYear = sorted[0].year;
    const latestMonth = sorted[0].month;

    // Prior month
    let prevYear = latestYear;
    let prevMonth = latestMonth - 1;
    if (prevMonth < 1) { prevMonth = 12; prevYear--; }

    const currRows = monthly.filter((m) => m.year === latestYear && m.month === latestMonth);
    const prevRows = monthly.filter((m) => m.year === prevYear && m.month === prevMonth);

    if (!currRows.length) return null;

    const agg = (rows) => {
      if (!rows.length) return null;
      const totalRev = rows.reduce((s, r) => s + (r.revenue_vnd || r.revenue_native || 0), 0);
      const avgOcc = rows.reduce((s, r) => s + (r.avg_occ_pct || 0), 0) / rows.length;
      const avgAdr = rows.reduce((s, r) => s + (r.avg_adr_native || 0), 0) / rows.length;
      const totalSold = rows.reduce((s, r) => s + (r.total_sold || 0), 0);
      return { revenue: totalRev, occ: avgOcc, adr: avgAdr, sold: totalSold };
    };

    const curr = agg(currRows);
    const prev = agg(prevRows);
    if (!curr) return null;

    const displayCur = isAll ? "VND" : (branchMap[selected]?.native_currency || branchMap[selected]?.currency || currency || "VND");

    // Use native for single branch
    let revCurr = curr.revenue;
    let revPrev = prev?.revenue ?? null;
    if (!isAll && selected) {
      revCurr = currRows.reduce((s, r) => s + (r.revenue_native || 0), 0);
      revPrev = prevRows.length ? prevRows.reduce((s, r) => s + (r.revenue_native || 0), 0) : null;
    }

    return {
      label: `${MONTH_LABELS[latestMonth]} ${latestYear}`,
      revenue: fmt(revCurr, displayCur),
      revenueChange: pctChange(revCurr, revPrev),
      occ: fmtPct(curr.occ),
      occChange: prev ? pctChange(curr.occ, prev.occ) : null,
      adr: fmt(curr.adr, displayCur),
      adrChange: prev ? pctChange(curr.adr, prev.adr) : null,
      sold: fmtNumber(curr.sold),
    };
  }, [monthly, isAll, selected, branchMap, currency]);

  /* ── Revenue bar chart (by month, multi-year) ── */
  const revenueChartData = useMemo(() => {
    const map = {};
    for (const m of monthly) {
      const key = `${MONTH_LABELS[m.month]} ${m.year}`;
      const sortKey = `${m.year}-${String(m.month).padStart(2, "0")}`;
      if (!map[sortKey]) map[sortKey] = { label: key, sortKey };
      // If multiple branches, stack by branch; if single, just one bar
      if (isAll) {
        map[sortKey][m.branch_id] = (map[sortKey][m.branch_id] || 0) + (m.revenue_native || 0);
      } else {
        map[sortKey].revenue = (map[sortKey].revenue || 0) + (m.revenue_native || 0);
      }
    }
    return Object.values(map).sort((a, b) => a.sortKey.localeCompare(b.sortKey));
  }, [monthly, isAll]);

  const revBarKeys = useMemo(() => {
    if (!isAll) return ["revenue"];
    const ids = new Set();
    for (const m of monthly) ids.add(m.branch_id);
    return [...ids];
  }, [monthly, isAll]);

  /* ── OCC% line chart (by month) ── */
  const occChartData = useMemo(() => {
    const map = {};
    for (const m of monthly) {
      const key = `${MONTH_LABELS[m.month]} ${m.year}`;
      const sortKey = `${m.year}-${String(m.month).padStart(2, "0")}`;
      if (!map[sortKey]) map[sortKey] = { label: key, sortKey };
      if (isAll) {
        map[sortKey][m.branch_id] = +((m.avg_occ_pct || 0) * 100).toFixed(2);
      } else {
        map[sortKey].occ_pct = +((m.avg_occ_pct || 0) * 100).toFixed(2);
      }
    }
    return Object.values(map).sort((a, b) => a.sortKey.localeCompare(b.sortKey));
  }, [monthly, isAll]);

  const occLineKeys = useMemo(() => {
    if (!isAll) return ["occ_pct"];
    const ids = new Set();
    for (const m of monthly) ids.add(m.branch_id);
    return [...ids];
  }, [monthly, isAll]);

  /* ── ADR & RevPAR dual line chart ── */
  const adrRevparData = useMemo(() => {
    // For single branch: simple dual line. For all branches: show ADR per branch.
    const map = {};
    for (const m of monthly) {
      const sortKey = `${m.year}-${String(m.month).padStart(2, "0")}`;
      const label = `${MONTH_LABELS[m.month]} ${m.year}`;
      if (!map[sortKey]) map[sortKey] = { label, sortKey };
      if (!isAll) {
        map[sortKey].adr = +(m.avg_adr_native || 0).toFixed(0);
        map[sortKey].revpar = +(m.avg_revpar_native || 0).toFixed(0);
      } else {
        map[sortKey][`adr_${m.branch_id}`] = +(m.avg_adr_native || 0).toFixed(0);
        map[sortKey][`revpar_${m.branch_id}`] = +(m.avg_revpar_native || 0).toFixed(0);
      }
    }
    return Object.values(map).sort((a, b) => a.sortKey.localeCompare(b.sortKey));
  }, [monthly, isAll]);

  /* ── Table data: flat rows with branch_id ── */
  const tableRows = useMemo(() => {
    return monthly.map((m) => ({ ...m }));
  }, [monthly]);

  /* ── Country breakdown: last 3 months (single branch) ── */
  const recentMonths = useMemo(() => {
    if (isAll) return [];
    return [...monthly]
      .sort((a, b) => b.year - a.year || b.month - a.month)
      .slice(0, 3);
  }, [monthly, isAll]);

  /* ── All-branches unique branch IDs for charts ── */
  const allBranchIds = useMemo(() => {
    const ids = new Set();
    for (const m of monthly) ids.add(m.branch_id);
    return [...ids];
  }, [monthly]);

  /* ── Year picker ── */
  const yearOptions = [];
  for (let y = 2022; y <= now.getFullYear() + 1; y++) yearOptions.push(y);

  const yearPicker = (
    <div className="flex gap-2 text-sm">
      <select
        className="border border-gray-200 rounded-lg px-3 py-1.5 bg-white"
        value={yearFrom}
        onChange={(e) => setYearFrom(+e.target.value)}
      >
        {yearOptions.map((y) => <option key={y} value={y}>{y}</option>)}
      </select>
      <span className="text-gray-400 self-center">--</span>
      <select
        className="border border-gray-200 rounded-lg px-3 py-1.5 bg-white"
        value={yearTo}
        onChange={(e) => setYearTo(+e.target.value)}
      >
        {yearOptions.map((y) => <option key={y} value={y}>{y}</option>)}
      </select>
    </div>
  );

  /* ── Render ───────────────────────────────────────────────────────────── */

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Monthly Brief</h1>
          <p className="text-sm text-gray-500">
            Multi-year performance + country breakdown
            <SyncBadge timestamp={monthly.find((m) => m.data_synced_at)?.data_synced_at} />
          </p>
        </div>
        {yearPicker}
      </div>

      {loading ? (
        <div className="text-gray-400 animate-pulse">Loading...</div>
      ) : (
        <>
          {/* KPI Summary Cards */}
          {kpis && (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <KpiCard label={`MTD Revenue (${kpis.label})`} value={kpis.revenue} change={kpis.revenueChange} />
              <KpiCard label="Avg OCC%" value={kpis.occ} change={kpis.occChange} />
              <KpiCard label="Avg ADR" value={kpis.adr} change={kpis.adrChange} />
              <KpiCard label="Total Rooms Sold" value={kpis.sold} />
            </div>
          )}

          {/* Revenue Trend — Bar Chart */}
          <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
            <p className="text-sm font-semibold text-gray-700 mb-3">Monthly Revenue</p>
            {revenueChartData.length === 0 ? (
              <div className="flex items-center justify-center h-32 text-gray-400 text-sm">No data available</div>
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={revenueChartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis
                    dataKey="label"
                    tick={{ fontSize: 11, fill: "#9ca3af" }}
                    tickLine={false}
                    axisLine={false}
                    interval={revenueChartData.length > 12 ? 1 : 0}
                    angle={revenueChartData.length > 12 ? -45 : 0}
                    textAnchor={revenueChartData.length > 12 ? "end" : "middle"}
                    height={revenueChartData.length > 12 ? 50 : 30}
                  />
                  <YAxis
                    tick={{ fontSize: 11, fill: "#9ca3af" }}
                    tickFormatter={(v) => new Intl.NumberFormat("en").format(Math.round(v))}
                    tickLine={false}
                    axisLine={false}
                    width={80}
                  />
                  <Tooltip
                    formatter={(v, name) => {
                      if (isAll) {
                        const branch = branchMap[name];
                        return [fmt(v, branch?.native_currency || branch?.currency || "VND"), branch?.name || name];
                      }
                      const cur = branchMap[selected]?.native_currency || branchMap[selected]?.currency || "VND";
                      return [fmt(v, cur), "Revenue"];
                    }}
                    contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e5e7eb" }}
                  />
                  {isAll && <Legend formatter={(id) => branchMap[id]?.name || id} iconSize={10} wrapperStyle={{ fontSize: 12 }} />}
                  {revBarKeys.map((key, i) => (
                    <Bar
                      key={key}
                      dataKey={key}
                      stackId={isAll ? "rev" : undefined}
                      fill={isAll ? BRANCH_COLORS[i % BRANCH_COLORS.length] : "#6366f1"}
                      radius={[3, 3, 0, 0]}
                      maxBarSize={40}
                      name={isAll ? key : "Revenue"}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            )}
          </div>

          {/* OCC% Trend — Line Chart */}
          <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
            <p className="text-sm font-semibold text-gray-700 mb-3">Monthly OCC%</p>
            {occChartData.length === 0 ? (
              <div className="flex items-center justify-center h-32 text-gray-400 text-sm">No data available</div>
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={occChartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis
                    dataKey="label"
                    tick={{ fontSize: 11, fill: "#9ca3af" }}
                    tickLine={false}
                    axisLine={false}
                    interval={occChartData.length > 12 ? 1 : 0}
                    angle={occChartData.length > 12 ? -45 : 0}
                    textAnchor={occChartData.length > 12 ? "end" : "middle"}
                    height={occChartData.length > 12 ? 50 : 30}
                  />
                  <YAxis
                    tick={{ fontSize: 11, fill: "#9ca3af" }}
                    tickFormatter={(v) => `${Number(v).toFixed(2)}%`}
                    tickLine={false}
                    axisLine={false}
                    width={56}
                    domain={[0, 100]}
                  />
                  <Tooltip
                    formatter={(v, name) => {
                      if (isAll) return [`${Number(v).toFixed(2)}%`, branchMap[name]?.name || name];
                      return [`${Number(v).toFixed(2)}%`, "OCC%"];
                    }}
                    contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e5e7eb" }}
                  />
                  {isAll && <Legend formatter={(id) => branchMap[id]?.name || id} iconSize={10} wrapperStyle={{ fontSize: 12 }} />}
                  {occLineKeys.map((key, i) => (
                    <Line
                      key={key}
                      type="monotone"
                      dataKey={key}
                      stroke={isAll ? BRANCH_COLORS[i % BRANCH_COLORS.length] : "#10b981"}
                      strokeWidth={2}
                      dot={false}
                      activeDot={{ r: 4 }}
                      connectNulls
                      name={isAll ? key : "OCC%"}
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>

          {/* ADR & RevPAR Trend — Dual Line */}
          <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
            <p className="text-sm font-semibold text-gray-700 mb-3">
              {isAll ? "ADR by Branch" : "ADR & RevPAR"}
            </p>
            {adrRevparData.length === 0 ? (
              <div className="flex items-center justify-center h-32 text-gray-400 text-sm">No data available</div>
            ) : (
              <ResponsiveContainer width="100%" height={220}>
                <LineChart data={adrRevparData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis
                    dataKey="label"
                    tick={{ fontSize: 11, fill: "#9ca3af" }}
                    tickLine={false}
                    axisLine={false}
                    interval={adrRevparData.length > 12 ? 1 : 0}
                    angle={adrRevparData.length > 12 ? -45 : 0}
                    textAnchor={adrRevparData.length > 12 ? "end" : "middle"}
                    height={adrRevparData.length > 12 ? 50 : 30}
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
                      if (!isAll) {
                        const cur = branchMap[selected]?.native_currency || branchMap[selected]?.currency || "VND";
                        return [fmt(v, cur), name === "adr" ? "ADR" : "RevPAR"];
                      }
                      // Parse branch id from key like adr_<branchId>
                      const bid = name.replace(/^(adr|revpar)_/, "");
                      const branch = branchMap[bid];
                      const metricType = name.startsWith("adr") ? "ADR" : "RevPAR";
                      return [fmt(v, branch?.native_currency || branch?.currency || "VND"), `${branch?.name || bid} ${metricType}`];
                    }}
                    contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e5e7eb" }}
                  />
                  <Legend
                    formatter={(name) => {
                      if (!isAll) return name === "adr" ? "ADR" : "RevPAR";
                      const bid = name.replace(/^(adr|revpar)_/, "");
                      const branch = branchMap[bid];
                      const metricType = name.startsWith("adr") ? "ADR" : "RevPAR";
                      return `${branch?.name || bid} ${metricType}`;
                    }}
                    iconSize={10}
                    wrapperStyle={{ fontSize: 12 }}
                  />
                  {!isAll ? (
                    <>
                      <Line type="monotone" dataKey="adr" stroke="#f59e0b" strokeWidth={2} dot={false} activeDot={{ r: 4 }} name="adr" connectNulls />
                      <Line type="monotone" dataKey="revpar" stroke="#ef4444" strokeWidth={2} dot={false} activeDot={{ r: 4 }} name="revpar" connectNulls />
                    </>
                  ) : (
                    allBranchIds.map((bid, i) => (
                      <Line
                        key={`adr_${bid}`}
                        type="monotone"
                        dataKey={`adr_${bid}`}
                        stroke={BRANCH_COLORS[i % BRANCH_COLORS.length]}
                        strokeWidth={2}
                        dot={false}
                        activeDot={{ r: 4 }}
                        name={`adr_${bid}`}
                        connectNulls
                      />
                    ))
                  )}
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>

          {/* Per-Branch Monthly Table with YoY */}
          <MonthlyTable rows={tableRows} branchMap={branchMap} allMonthly={monthly} />

          {/* Country Breakdown (single branch, last 3 months) */}
          {!isAll && recentMonths.length > 0 && (
            <div>
              <p className="text-sm font-semibold text-gray-700 mb-3">Country Breakdown</p>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {recentMonths.map((m) => {
                  const countries = (m.country_breakdown || []).slice(0, 8);
                  if (!countries.length) return null;
                  return (
                    <div key={`${m.year}-${m.month}`} className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
                      <p className="text-sm font-semibold text-gray-700 mb-3">
                        Top Countries -- {MONTH_LABELS[m.month]} {m.year}
                      </p>
                      <div className="grid grid-cols-2 gap-2">
                        {countries.map((c) => (
                          <div key={c.country_code} className="bg-gray-50 rounded-lg px-3 py-2">
                            <p className="text-xs font-semibold text-gray-700">{c.country || c.country_code}</p>
                            <p className="text-xs text-gray-400">{c.count} bookings</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* All-branches: per-branch country info from latest month */}
          {isAll && (() => {
            const sorted = [...monthly].sort((a, b) => b.year - a.year || b.month - a.month);
            const latestYear = sorted[0]?.year;
            const latestMonth = sorted[0]?.month;
            const latestRows = monthly.filter(
              (m) => m.year === latestYear && m.month === latestMonth && (m.country_breakdown || []).length > 0
            );
            if (!latestRows.length) return null;
            return (
              <div>
                <p className="text-sm font-semibold text-gray-700 mb-3">
                  Country Breakdown -- {MONTH_LABELS[latestMonth]} {latestYear}
                </p>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                  {latestRows.map((m) => {
                    const branch = branchMap[m.branch_id];
                    const countries = (m.country_breakdown || []).slice(0, 6);
                    return (
                      <div key={m.branch_id} className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
                        <p className="text-xs font-semibold text-gray-600 mb-2">{branch?.name || m.branch_id}</p>
                        <div className="grid grid-cols-2 gap-2">
                          {countries.map((c) => (
                            <div key={c.country_code} className="bg-gray-50 rounded-lg px-3 py-2">
                              <p className="text-xs font-semibold text-gray-700">{c.country || c.country_code}</p>
                              <p className="text-xs text-gray-400">{c.count} bookings</p>
                            </div>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}
        </>
      )}
    </div>
  );
}
