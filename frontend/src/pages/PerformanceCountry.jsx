/**
 * Country Reservations Trend — All countries over 7 weeks / 7 months, sortable.
 * + Compare to Last Year (via Cloudbeds Insights API).
 * + Branch Compare: side-by-side country data across multiple branches.
 */
import { useEffect, useState, useMemo, useRef } from "react";
import axios from "axios";
import {
  AreaChart, Area,
  LineChart, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";
import SyncBadge from "../components/SyncBadge";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";

const COLORS = [
  "#6366f1", "#10b981", "#f59e0b", "#ef4444", "#3b82f6",
  "#a855f7", "#06b6d4", "#ec4899", "#84cc16", "#f97316",
  "#8b5cf6", "#14b8a6", "#e11d48", "#0ea5e9", "#d946ef",
];

const BRANCH_COLORS = [
  { text: "text-gray-900", header: "text-gray-600", headerSub: "text-gray-400", cell: "text-gray-900", bg: "" },
  { text: "text-indigo-700", header: "text-indigo-600", headerSub: "text-indigo-400", cell: "text-indigo-700", bg: "bg-indigo-50/30 border-indigo-200" },
  { text: "text-emerald-700", header: "text-emerald-600", headerSub: "text-emerald-400", cell: "text-emerald-700", bg: "bg-emerald-50/30 border-emerald-200" },
  { text: "text-amber-700", header: "text-amber-600", headerSub: "text-amber-400", cell: "text-amber-700", bg: "bg-amber-50/30 border-amber-200" },
  { text: "text-rose-700", header: "text-rose-600", headerSub: "text-rose-400", cell: "text-rose-700", bg: "bg-rose-50/30 border-rose-200" },
];

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function fmtNum(val) {
  if (val == null || val === 0) return "0";
  return new Intl.NumberFormat("en").format(Math.round(val));
}

function fmtPct(val) {
  if (val == null) return "-";
  return `${val.toFixed(1)}%`;
}

export default function PerformanceCountry() {
  const { isAll, selected, branches } = useBranch();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState("monthly"); // weekly | monthly | share | compare | branch
  const [filterCountry, setFilterCountry] = useState("");
  const [dateType, setDateType] = useState("check_in"); // check_in | booked

  // Share % view state — distribution of a metric across countries per period.
  const [shareMetric, setShareMetric] = useState("reservations"); // reservations | revenue
  const [shareGranularity, setShareGranularity] = useState("monthly"); // monthly | weekly
  const [shareData, setShareData] = useState(null);
  const [shareLoading, setShareLoading] = useState(true);
  const [shareSelected, setShareSelected] = useState([]); // country names ticked for trend

  // Trend table sort state. sortKey is one of:
  //   "country" | "total_reservations" | "total_nights" | "total_revenue"
  //   or "period:<periodLabel>" for per-period columns
  const [sortKey, setSortKey] = useState("total_reservations");
  const [sortDir, setSortDir] = useState("desc");

  // Compare (YoY) view state
  const now = new Date();
  const [cmpYear, setCmpYear] = useState(now.getFullYear());
  const [cmpMonth, setCmpMonth] = useState(now.getMonth() + 1);
  const [cmpData, setCmpData] = useState(null);
  const [cmpLoading, setCmpLoading] = useState(false);

  // Branch compare state — multi-select
  const [selectedBranches, setSelectedBranches] = useState([]);
  const [branchYear, setBranchYear] = useState(now.getFullYear());
  const [branchMonth, setBranchMonth] = useState(now.getMonth() + 1);
  const [branchDataMap, setBranchDataMap] = useState({}); // { branchId: apiData }
  const [branchLoading, setBranchLoading] = useState(false);

  // When entering branch view, auto-select current branch if on a specific one
  useEffect(() => {
    if (view === "branch" && !isAll && selected && selectedBranches.length === 0) {
      setSelectedBranches([selected]);
    }
  }, [view, isAll, selected]);

  // Load trend data (weekly/monthly)
  const loadTrend = () => {
    setLoading(true);
    const params = { view, limit: 500, date_type: dateType };
    if (!isAll && selected) params.branch_id = selected;

    axios.get("/api/metrics/country-reservations", { params })
      .then((r) => setData(r.data.data))
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  };

  // Load Share % data — same endpoint as the trend view, but driven by the
  // tab's own granularity toggle (independent of the weekly/monthly buttons).
  const loadShare = () => {
    setShareLoading(true);
    const params = { view: shareGranularity, limit: 500, date_type: dateType };
    if (!isAll && selected) params.branch_id = selected;

    axios.get("/api/metrics/country-reservations", { params })
      .then((r) => setShareData(r.data.data))
      .catch(() => setShareData(null))
      .finally(() => setShareLoading(false));
  };

  // Load YoY compare data
  const loadCompare = () => {
    setCmpLoading(true);
    const params = { year: cmpYear, month: cmpMonth, date_type: dateType };
    if (!isAll && selected) params.branch_id = selected;

    axios.get("/api/metrics/country-yoy-insights", { params })
      .then((r) => setCmpData(r.data.data))
      .catch(() => setCmpData(null))
      .finally(() => setCmpLoading(false));
  };

  // Load branch compare data — fetch each selected branch in parallel
  const loadBranchCompare = () => {
    if (selectedBranches.length === 0) {
      setBranchDataMap({});
      return;
    }
    setBranchLoading(true);
    const requests = selectedBranches.map((bid) =>
      axios.get("/api/metrics/country-yoy-insights", {
        params: { year: branchYear, month: branchMonth, branch_id: bid, date_type: dateType },
      }).then((r) => ({ bid, data: r.data.data }))
        .catch(() => ({ bid, data: null }))
    );

    Promise.all(requests)
      .then((results) => {
        const map = {};
        for (const r of results) map[r.bid] = r.data;
        setBranchDataMap(map);
      })
      .finally(() => setBranchLoading(false));
  };

  useEffect(() => {
    if (view === "compare") {
      loadCompare();
    } else if (view === "branch") {
      loadBranchCompare();
    } else if (view === "share") {
      loadShare();
    } else {
      loadTrend();
    }
  }, [selected, isAll, view, cmpYear, cmpMonth, branchYear, branchMonth, selectedBranches, dateType, shareGranularity]);

  const periods = data?.periods || [];
  const allCountries = data?.countries || [];
  const trend = data?.trend || {};
  const trendCurrency = data?.currency || "VND";
  const trendCurrencyLabel = CURRENCY_SYMBOLS[trendCurrency] || trendCurrency;

  const currentBranchName = useMemo(() => {
    if (isAll || !selected) return "All Branches";
    const b = branches.find((br) => br.id === selected);
    return b?.name || "Current";
  }, [branches, selected, isAll]);

  // Branch compare: union of countries across all selected branches
  const branchCountryList = useMemo(() => {
    const names = new Set();
    for (const d of Object.values(branchDataMap)) {
      if (d?.countries) d.countries.forEach((c) => names.add(c.country));
    }
    return [...names].sort();
  }, [branchDataMap]);

  // Filter countries
  const filteredCountries = useMemo(() => {
    if (!filterCountry) return allCountries;
    return allCountries.filter((c) => c.country === filterCountry);
  }, [allCountries, filterCountry]);

  // Sort countries by the active sort key/direction
  const countries = useMemo(() => {
    const arr = [...filteredCountries];
    const dir = sortDir === "asc" ? 1 : -1;
    const periodKey = sortKey.startsWith("period:") ? sortKey.slice(7) : null;

    arr.sort((a, b) => {
      let av, bv;
      if (periodKey) {
        av = (trend[periodKey] || {})[a.country] || 0;
        bv = (trend[periodKey] || {})[b.country] || 0;
      } else if (sortKey === "country") {
        av = (a.country || "").toLowerCase();
        bv = (b.country || "").toLowerCase();
        return av < bv ? -1 * dir : av > bv ? 1 * dir : 0;
      } else {
        av = a[sortKey] || 0;
        bv = b[sortKey] || 0;
      }
      return (av - bv) * dir;
    });
    return arr;
  }, [filteredCountries, sortKey, sortDir, trend]);

  const countryNames = countries.map((c) => c.country);

  // Chart caps at top 15 countries by reservations to stay readable;
  // the table below still shows the full list.
  const chartCountryNames = useMemo(() => {
    return [...filteredCountries]
      .sort((a, b) => (b.total_reservations || 0) - (a.total_reservations || 0))
      .slice(0, 15)
      .map((c) => c.country);
  }, [filteredCountries]);

  const toggleSort = (key) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // numeric defaults desc; string defaults asc
      setSortDir(key === "country" ? "asc" : "desc");
    }
  };

  const sortArrow = (key) => {
    if (sortKey !== key) return <span className="text-gray-300 ml-1">↕</span>;
    return <span className="text-indigo-600 ml-1">{sortDir === "asc" ? "▲" : "▼"}</span>;
  };

  // Build chart data (top 15 only, to keep stacked chart readable)
  const chartData = useMemo(() => {
    return periods.map((p) => {
      const row = { period: p };
      const periodData = trend[p] || {};
      for (const name of chartCountryNames) {
        row[name] = periodData[name] || 0;
      }
      return row;
    });
  }, [periods, trend, chartCountryNames]);

  const latestPeriod = periods[periods.length - 1];
  const prevPeriod = periods[periods.length - 2];
  const latestData = trend[latestPeriod] || {};
  const prevData = trend[prevPeriod] || {};
  const latestTotal = Object.values(latestData).reduce((a, b) => a + b, 0);
  const prevTotal = Object.values(prevData).reduce((a, b) => a + b, 0);

  // Toggle a branch in the multi-select
  const toggleBranch = (bid) => {
    setSelectedBranches((prev) =>
      prev.includes(bid) ? prev.filter((b) => b !== bid) : [...prev, bid]
    );
  };

  // Subtitle
  const subtitle = view === "compare"
    ? `${MONTHS[cmpMonth - 1]} ${cmpYear} vs ${MONTHS[cmpMonth - 1]} ${cmpYear - 1}`
    : view === "branch"
    ? `${MONTHS[branchMonth - 1]} ${branchYear} — Branch Comparison`
    : view === "share"
    ? `% share of ${shareMetric === "revenue" ? "revenue" : "reservations"} \u2014 last 7 ${shareGranularity === "monthly" ? "months" : "weeks"}`
    : `All countries \u2014 last 7 ${view === "monthly" ? "months" : "weeks"}`;

  // Country filter options differ per view
  const countryFilterOptions = useMemo(() => {
    if (view === "branch") return branchCountryList;
    if (view === "compare") return (cmpData?.countries || []).map((c) => c.country);
    if (view === "share") return (shareData?.countries || []).map((c) => c.country);
    return allCountries.map((c) => c.country);
  }, [view, branchCountryList, cmpData, shareData, allCountries]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-lg font-bold text-gray-900">Country Reservations</h1>
          <p className="text-sm text-gray-500">
            {subtitle}
            <SyncBadge timestamp={data?.data_synced_at || cmpData?.data_synced_at} />
          </p>
        </div>
        <div className="flex items-center gap-3">
          <CountryCombobox
            value={filterCountry}
            onChange={setFilterCountry}
            options={countryFilterOptions}
          />
          {view === "compare" && (
            <div className="flex items-center gap-2">
              <select value={cmpMonth} onChange={(e) => setCmpMonth(Number(e.target.value))}
                className="border rounded px-2 py-1.5 text-sm">
                {MONTHS.map((m, i) => (
                  <option key={i + 1} value={i + 1}>{m}</option>
                ))}
              </select>
              <select value={cmpYear} onChange={(e) => setCmpYear(Number(e.target.value))}
                className="border rounded px-2 py-1.5 text-sm">
                {[now.getFullYear() - 1, now.getFullYear(), now.getFullYear() + 1].map((y) => (
                  <option key={y} value={y}>{y}</option>
                ))}
              </select>
            </div>
          )}
          {view === "branch" && (
            <div className="flex items-center gap-2">
              <select value={branchMonth} onChange={(e) => setBranchMonth(Number(e.target.value))}
                className="border rounded px-2 py-1.5 text-sm">
                {MONTHS.map((m, i) => (
                  <option key={i + 1} value={i + 1}>{m}</option>
                ))}
              </select>
              <select value={branchYear} onChange={(e) => setBranchYear(Number(e.target.value))}
                className="border rounded px-2 py-1.5 text-sm">
                {[now.getFullYear() - 1, now.getFullYear(), now.getFullYear() + 1].map((y) => (
                  <option key={y} value={y}>{y}</option>
                ))}
              </select>
            </div>
          )}
          <div className="flex rounded-lg border overflow-hidden">
            {["weekly", "monthly", "share", "compare", "branch"].map((v) => (
              <button key={v} onClick={() => setView(v)}
                className={`px-3 py-1.5 text-sm font-medium ${
                  view === v ? "bg-indigo-600 text-white" : "bg-white text-gray-600 hover:bg-gray-50"
                }`}>
                {v === "weekly" ? "Weekly" : v === "monthly" ? "Monthly" : v === "share" ? "Share %" : v === "compare" ? "Compare" : "Branches"}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Date type tabs (matches OTA Mix page) */}
      <div className="flex border-b border-gray-200">
        {[
          ["check_in", "By Check-in Date"],
          ["booked",   "By Date Booked"],
        ].map(([k, label]) => (
          <button key={k} onClick={() => setDateType(k)}
            className={`px-5 py-2.5 text-sm font-medium transition-colors border-b-2 -mb-px ${
              dateType === k
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
            }`}>
            {label}
          </button>
        ))}
      </div>

      {/* ── Share % View ── */}
      {view === "share" ? (
        shareLoading ? (
          <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading...</div>
        ) : !shareData || (shareData.countries || []).length === 0 ? (
          <div className="text-center text-gray-400 py-16 text-sm">No data available.</div>
        ) : (
          <ShareView
            data={shareData}
            metric={shareMetric}
            setMetric={setShareMetric}
            granularity={shareGranularity}
            setGranularity={setShareGranularity}
            filterCountry={filterCountry}
            selected={shareSelected}
            setSelected={setShareSelected}
          />
        )
      ) : view === "branch" ? (
        <>
          {/* Branch selector chips */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-gray-500 uppercase tracking-wider mr-1">Select branches:</span>
            {branches.map((b, i) => {
              const isChecked = selectedBranches.includes(b.id);
              const colorIdx = isChecked ? selectedBranches.indexOf(b.id) : -1;
              const color = colorIdx >= 0 ? BRANCH_COLORS[colorIdx % BRANCH_COLORS.length] : null;
              return (
                <button key={b.id} onClick={() => toggleBranch(b.id)}
                  className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${
                    isChecked
                      ? `${color.bg || "bg-gray-100"} ${color.text} border-current`
                      : "bg-white text-gray-500 border-gray-300 hover:border-gray-400"
                  }`}>
                  {isChecked && <span className="mr-1">&#10003;</span>}
                  {b.name}
                </button>
              );
            })}
          </div>
          {branchLoading ? (
            <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading...</div>
          ) : selectedBranches.length === 0 ? (
            <div className="text-center text-gray-400 py-16 text-sm">Select at least one branch to compare.</div>
          ) : (
            <BranchCompareView
              branches={branches}
              selectedBranches={selectedBranches}
              branchDataMap={branchDataMap}
              filterCountry={filterCountry}
              year={branchYear}
              month={branchMonth}
            />
          )}
        </>
      ) : view === "compare" ? (
        /* ── YoY Compare View ── */
        cmpLoading ? (
          <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading...</div>
        ) : !cmpData || cmpData.countries?.length === 0 ? (
          <div className="text-center text-gray-400 py-16 text-sm">No data available.</div>
        ) : (
          <CompareView data={cmpData} filterCountry={filterCountry} />
        )
      ) : (
        /* ── Trend View (Weekly / Monthly) ── */
        loading ? (
          <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading...</div>
        ) : !data || countries.length === 0 ? (
          <div className="text-center text-gray-400 py-16 text-sm">No data available.</div>
        ) : (
          <>
            {/* KPI Summary */}
            <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
              <div className="bg-white rounded-lg border p-4">
                <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">
                  {latestPeriod} Reservations
                </p>
                <p className="text-2xl font-bold text-gray-900">{fmtNum(latestTotal)}</p>
                {prevTotal > 0 && (
                  <PctChange current={latestTotal} previous={prevTotal} label={prevPeriod} />
                )}
              </div>
              <div className="bg-white rounded-lg border p-4">
                <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Top Country</p>
                <p className="text-2xl font-bold text-gray-900">{filteredCountries[0]?.country || "-"}</p>
                <p className="text-xs text-gray-400 mt-1">{fmtNum(filteredCountries[0]?.total_reservations)} total reservations</p>
              </div>
              <div className="bg-white rounded-lg border p-4">
                <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Countries Tracked</p>
                <p className="text-2xl font-bold text-gray-900">{countries.length}</p>
                <p className="text-xs text-gray-400 mt-1">{fmtNum(countries.reduce((a, c) => a + c.total_nights, 0))} total room nights</p>
              </div>
            </div>

            {/* Stacked Area Chart */}
            <div className="bg-white rounded-lg border p-4">
              <h2 className="text-sm font-semibold text-gray-700 mb-4">
                Reservation Trend by Country <span className="text-xs font-normal text-gray-400">(top 15)</span>
              </h2>
              <ResponsiveContainer width="100%" height={360}>
                <AreaChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis dataKey="period" tick={{ fontSize: 12 }} />
                  <YAxis tick={{ fontSize: 12 }} />
                  <Tooltip
                    contentStyle={{ fontSize: 12, borderRadius: 8 }}
                    formatter={(val, name) => [fmtNum(val), name]}
                  />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  {chartCountryNames.map((name, i) => (
                    <Area
                      key={name}
                      type="monotone"
                      dataKey={name}
                      stackId="1"
                      fill={COLORS[i % COLORS.length]}
                      stroke={COLORS[i % COLORS.length]}
                      fillOpacity={0.7}
                    />
                  ))}
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Summary Table */}
            <div className="bg-white rounded-lg border overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="text-center px-3 py-3 font-semibold text-gray-600 w-10">#</th>
                    <th
                      onClick={() => toggleSort("country")}
                      className="text-left px-4 py-3 font-semibold text-gray-600 cursor-pointer hover:bg-gray-100 select-none"
                    >
                      Country{sortArrow("country")}
                    </th>
                    <th
                      onClick={() => toggleSort("total_reservations")}
                      className="text-right px-4 py-3 font-semibold text-gray-600 cursor-pointer hover:bg-gray-100 select-none"
                    >
                      Total Reservations{sortArrow("total_reservations")}
                    </th>
                    <th
                      onClick={() => toggleSort("total_nights")}
                      className="text-right px-4 py-3 font-semibold text-gray-600 cursor-pointer hover:bg-gray-100 select-none"
                    >
                      Room Nights{sortArrow("total_nights")}
                    </th>
                    <th
                      onClick={() => toggleSort("total_revenue")}
                      className="text-right px-4 py-3 font-semibold text-gray-600 cursor-pointer hover:bg-gray-100 select-none"
                    >
                      Revenue ({trendCurrencyLabel}){sortArrow("total_revenue")}
                    </th>
                    {periods.map((p) => (
                      <th
                        key={p}
                        onClick={() => toggleSort(`period:${p}`)}
                        className="text-right px-3 py-3 font-semibold text-gray-500 text-xs cursor-pointer hover:bg-gray-100 select-none"
                      >
                        {p}{sortArrow(`period:${p}`)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {countries.map((c, i) => (
                    <tr key={`${c.country_code}__${c.country}`} className="hover:bg-gray-50">
                      <td className="px-3 py-2.5 text-center">
                        <span className="inline-block w-5 h-5 rounded-full text-xs font-bold text-white leading-5 text-center"
                          style={{ backgroundColor: COLORS[i % COLORS.length] }}>
                          {i + 1}
                        </span>
                      </td>
                      <td className="px-4 py-2.5 font-medium text-gray-900">{c.country}</td>
                      <td className="px-4 py-2.5 text-right font-semibold">{fmtNum(c.total_reservations)}</td>
                      <td className="px-4 py-2.5 text-right">{fmtNum(c.total_nights)}</td>
                      <td className="px-4 py-2.5 text-right">{fmtNum(c.total_revenue)}</td>
                      {periods.map((p, pIdx) => {
                        const val = (trend[p] || {})[c.country] || 0;
                        const prevP = pIdx > 0 ? periods[pIdx - 1] : null;
                        const prevVal = prevP ? ((trend[prevP] || {})[c.country] || 0) : null;
                        const pct = (prevVal != null && prevVal > 0)
                          ? ((val - prevVal) / prevVal) * 100
                          : null;
                        return (
                          <td key={p} className="px-3 py-2.5 text-right text-xs">
                            {val > 0 || (prevVal != null && prevVal > 0) ? (
                              <div className="flex flex-col items-end leading-tight">
                                <span className="font-medium">
                                  {val > 0 ? val : <span className="text-gray-300">-</span>}
                                </span>
                                {pct != null && (
                                  <span className={`text-[10px] mt-0.5 ${
                                    pct > 0 ? "text-emerald-600"
                                    : pct < 0 ? "text-red-500"
                                    : "text-gray-400"
                                  }`}>
                                    {pct > 0 ? "▲" : pct < 0 ? "▼" : ""}
                                    {Math.abs(pct).toFixed(1)}%
                                  </span>
                                )}
                              </div>
                            ) : (
                              <span className="text-gray-300">-</span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )
      )}
    </div>
  );
}


/* ── Share % View Component ────────────────────────────────────────────────
 * Distribution of a chosen metric (reservations or revenue) across countries,
 * per period. Each cell is that country's % of the period total; the small
 * number underneath is the percentage-POINT change vs the previous period
 * (i.e. share gained/lost, not a relative growth rate). Tick countries to plot
 * their share trend as lines.
 */
function ShareView({
  data, metric, setMetric, granularity, setGranularity,
  filterCountry, selected, setSelected,
}) {
  const periods = data.periods || [];
  const trendMap = metric === "revenue" ? (data.trend_revenue || {}) : (data.trend || {});

  // Total of the metric in each period (denominator for the share).
  const periodTotals = useMemo(() => {
    const t = {};
    for (const p of periods) {
      const row = trendMap[p] || {};
      t[p] = Object.values(row).reduce((a, b) => a + (b || 0), 0);
    }
    return t;
  }, [periods, trendMap]);

  // Every country that appears in the metric trend.
  const countryList = useMemo(() => {
    const set = new Set();
    for (const p of periods) {
      for (const c of Object.keys(trendMap[p] || {})) set.add(c);
    }
    return [...set];
  }, [periods, trendMap]);

  // Per-country total across all periods → used for ordering + overall share.
  const countryTotals = useMemo(() => {
    const t = {};
    for (const c of countryList) {
      let sum = 0;
      for (const p of periods) sum += (trendMap[p]?.[c] || 0);
      t[c] = sum;
    }
    return t;
  }, [countryList, periods, trendMap]);

  const grandTotal = periods.reduce((a, p) => a + (periodTotals[p] || 0), 0);

  // Rows: countries sorted by overall metric desc, optionally narrowed by the
  // header combobox filter.
  let rows = [...countryList].sort((a, b) => countryTotals[b] - countryTotals[a]);
  if (filterCountry) rows = rows.filter((c) => c === filterCountry);

  const shareOf = (country, period) => {
    const tot = periodTotals[period] || 0;
    if (!tot) return null;
    return ((trendMap[period]?.[country] || 0) / tot) * 100;
  };

  const toggle = (country) => {
    setSelected((prev) =>
      prev.includes(country) ? prev.filter((c) => c !== country) : [...prev, country]
    );
  };

  // Color for a ticked country = its position in the selection.
  const selColor = (country) => COLORS[selected.indexOf(country) % COLORS.length];

  // Chart: one point per period, a key per ticked country holding its share%.
  const chartData = useMemo(() => periods.map((p) => {
    const row = { period: p };
    for (const c of selected) row[c] = shareOf(c, p);
    return row;
  }), [periods, selected, trendMap, periodTotals]);

  const latestPeriod = periods[periods.length - 1];
  const topCountry = rows[0];
  const topShare = topCountry ? shareOf(topCountry, latestPeriod) : null;

  return (
    <>
      {/* Controls: metric + granularity toggles */}
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">Compare %</span>
          <div className="flex rounded-lg border overflow-hidden">
            {[["reservations", "Reservations"], ["revenue", "Revenue"]].map(([k, label]) => (
              <button key={k} onClick={() => setMetric(k)}
                className={`px-3 py-1.5 text-sm font-medium ${
                  metric === k ? "bg-indigo-600 text-white" : "bg-white text-gray-600 hover:bg-gray-50"
                }`}>
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-gray-500 uppercase tracking-wider">By</span>
          <div className="flex rounded-lg border overflow-hidden">
            {[["monthly", "Monthly"], ["weekly", "Weekly"]].map(([k, label]) => (
              <button key={k} onClick={() => setGranularity(k)}
                className={`px-3 py-1.5 text-sm font-medium ${
                  granularity === k ? "bg-indigo-600 text-white" : "bg-white text-gray-600 hover:bg-gray-50"
                }`}>
                {label}
              </button>
            ))}
          </div>
        </div>
        {selected.length > 0 && (
          <button onClick={() => setSelected([])}
            className="text-xs text-gray-500 hover:text-gray-700 underline ml-auto">
            Clear {selected.length} selected
          </button>
        )}
      </div>

      {/* KPI Summary */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <div className="bg-white rounded-lg border p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">
            Top Share — {latestPeriod}
          </p>
          <p className="text-2xl font-bold text-gray-900">{topCountry || "-"}</p>
          <p className="text-xs text-gray-400 mt-1">
            {topShare != null ? fmtPct(topShare) : "-"} of {metric === "revenue" ? "revenue" : "reservations"}
          </p>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Countries Tracked</p>
          <p className="text-2xl font-bold text-gray-900">{rows.length}</p>
          <p className="text-xs text-gray-400 mt-1">across {periods.length} periods</p>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Selected for Trend</p>
          <p className="text-2xl font-bold text-gray-900">{selected.length}</p>
          <p className="text-xs text-gray-400 mt-1">tick countries in the table</p>
        </div>
      </div>

      {/* Share trend line chart (only when countries are ticked) */}
      {selected.length > 0 && (
        <div className="bg-white rounded-lg border p-4">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">
            % Share Trend
            <span className="text-xs font-normal text-gray-400 ml-1">
              ({metric === "revenue" ? "revenue" : "reservations"})
            </span>
          </h2>
          <ResponsiveContainer width="100%" height={320}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis dataKey="period" tick={{ fontSize: 12 }} />
              <YAxis tick={{ fontSize: 12 }} unit="%" />
              <Tooltip
                contentStyle={{ fontSize: 12, borderRadius: 8 }}
                formatter={(val, name) => [val == null ? "-" : fmtPct(val), name]}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {selected.map((c) => (
                <Line key={c} type="monotone" dataKey={c}
                  stroke={selColor(c)} strokeWidth={2} dot={{ r: 3 }} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Share table */}
      <div className="bg-white rounded-lg border overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="text-center px-3 py-3 font-semibold text-gray-600 w-10">#</th>
              <th className="text-left px-4 py-3 font-semibold text-gray-600">Country</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Overall %</th>
              {periods.map((p) => (
                <th key={p} className="text-right px-3 py-3 font-semibold text-gray-500 text-xs">
                  {p}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            {rows.map((country, i) => {
              const isSel = selected.includes(country);
              const overall = grandTotal > 0 ? (countryTotals[country] / grandTotal) * 100 : null;
              return (
                <tr key={country}
                  onClick={() => toggle(country)}
                  className={`cursor-pointer ${isSel ? "bg-indigo-50/50" : "hover:bg-gray-50"}`}>
                  <td className="px-3 py-2.5 text-center">
                    <input type="checkbox" readOnly checked={isSel}
                      className="accent-indigo-600 pointer-events-none" />
                  </td>
                  <td className="px-4 py-2.5 font-medium text-gray-900">
                    {isSel && (
                      <span className="inline-block w-2 h-2 rounded-full mr-2 align-middle"
                        style={{ backgroundColor: selColor(country) }} />
                    )}
                    {country}
                  </td>
                  <td className="px-4 py-2.5 text-right font-semibold">{fmtPct(overall)}</td>
                  {periods.map((p, pIdx) => {
                    const share = shareOf(country, p);
                    const prevP = pIdx > 0 ? periods[pIdx - 1] : null;
                    const prevShare = prevP ? shareOf(country, prevP) : null;
                    const dPp = (share != null && prevShare != null) ? share - prevShare : null;
                    return (
                      <td key={p} className="px-3 py-2.5 text-right text-xs">
                        {share != null && (share > 0 || prevShare > 0) ? (
                          <div className="flex flex-col items-end leading-tight">
                            <span className="font-medium">
                              {share > 0 ? fmtPct(share) : <span className="text-gray-300">-</span>}
                            </span>
                            {dPp != null && Math.abs(dPp) >= 0.05 && (
                              <span className={`text-[10px] mt-0.5 ${
                                dPp > 0 ? "text-emerald-600" : "text-red-500"
                              }`}>
                                {dPp > 0 ? "▲" : "▼"}{Math.abs(dPp).toFixed(1)}pp
                              </span>
                            )}
                          </div>
                        ) : (
                          <span className="text-gray-300">-</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}


/* ── YoY Compare View Component ───────────────────────────────────────────── */

function CompareView({ data, filterCountry }) {
  const { year, month } = data;
  const countries = filterCountry
    ? data.countries.filter((c) => c.country === filterCountry)
    : data.countries;
  const monthName = MONTHS[month - 1];
  const currency = data.currency || "VND";
  const currencyLabel = CURRENCY_SYMBOLS[currency] || currency;

  const totalCurrentNights = countries.reduce((a, c) => a + c.current_nights, 0);
  const totalPrevNights = countries.reduce((a, c) => a + c.prev_nights, 0);
  const totalCurrentRevenue = countries.reduce((a, c) => a + c.current_revenue, 0);
  const totalPrevRevenue = countries.reduce((a, c) => a + c.prev_revenue, 0);
  const growingCount = countries.filter((c) => c.nights_change_pct != null && c.nights_change_pct > 0).length;
  const decliningCount = countries.filter((c) => c.nights_change_pct != null && c.nights_change_pct < 0).length;

  return (
    <>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-white rounded-lg border p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">
            {monthName} {year} Nights
          </p>
          <p className="text-2xl font-bold text-gray-900">{fmtNum(totalCurrentNights)}</p>
          {totalPrevNights > 0 && (
            <PctChange current={totalCurrentNights} previous={totalPrevNights}
              label={`${monthName} ${year - 1}`} />
          )}
        </div>
        <div className="bg-white rounded-lg border p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">
            {monthName} {year} Revenue
          </p>
          <p className="text-2xl font-bold text-gray-900">{fmtNum(totalCurrentRevenue)}</p>
          {totalPrevRevenue > 0 && (
            <PctChange current={totalCurrentRevenue} previous={totalPrevRevenue}
              label={`${monthName} ${year - 1}`} />
          )}
        </div>
        <div className="bg-white rounded-lg border p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Growing</p>
          <p className="text-2xl font-bold text-emerald-600">{growingCount}</p>
          <p className="text-xs text-gray-400 mt-1">countries with more room nights</p>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Declining</p>
          <p className="text-2xl font-bold text-red-500">{decliningCount}</p>
          <p className="text-xs text-gray-400 mt-1">countries with fewer room nights</p>
        </div>
      </div>

      <div className="bg-white rounded-lg border overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="text-center px-3 py-3 font-semibold text-gray-600 w-10">#</th>
              <th className="text-left px-4 py-3 font-semibold text-gray-600">Country</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Nights {year}</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Nights {year - 1}</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Change</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Revenue {year} ({currencyLabel})</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Revenue {year - 1} ({currencyLabel})</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Change</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Guests {year}</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Guests {year - 1}</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Change</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {countries.map((c, i) => (
              <tr key={c.country} className="hover:bg-gray-50">
                <td className="px-3 py-2.5 text-center text-gray-400 font-mono text-xs">{i + 1}</td>
                <td className="px-4 py-2.5 font-medium text-gray-900">{c.country}</td>
                <td className="px-4 py-2.5 text-right font-semibold">{fmtNum(c.current_nights)}</td>
                <td className="px-4 py-2.5 text-right text-gray-500">{fmtNum(c.prev_nights)}</td>
                <td className="px-4 py-2.5 text-right">
                  <ChangeBadge value={c.nights_change_pct} />
                </td>
                <td className="px-4 py-2.5 text-right font-semibold">{fmtNum(c.current_revenue)}</td>
                <td className="px-4 py-2.5 text-right text-gray-500">{fmtNum(c.prev_revenue)}</td>
                <td className="px-4 py-2.5 text-right">
                  <ChangeBadge value={c.revenue_change_pct} />
                </td>
                <td className="px-4 py-2.5 text-right font-semibold">{fmtNum(c.current_guests)}</td>
                <td className="px-4 py-2.5 text-right text-gray-500">{fmtNum(c.prev_guests)}</td>
                <td className="px-4 py-2.5 text-right">
                  <ChangeBadge value={c.guests_change_pct} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}


/* ── Branch Compare View Component ─────────────────────────────────────────── */

function BranchCompareView({ branches, selectedBranches, branchDataMap, filterCountry, year, month }) {
  const monthName = MONTHS[month - 1];

  // Build per-branch lookup: branchId -> { countryName -> row }
  const branchLookups = selectedBranches.map((bid) => {
    const d = branchDataMap[bid];
    const map = {};
    if (d?.countries) {
      for (const c of d.countries) map[c.country] = c;
    }
    return { bid, map };
  });

  // Branch names + currency labels in selection order
  const branchNames = selectedBranches.map((bid) => {
    const b = branches.find((br) => br.id === bid);
    return b?.name || bid;
  });
  const branchCurrencyLabels = selectedBranches.map((bid) => {
    const cur = branchDataMap[bid]?.currency
      || branches.find((br) => br.id === bid)?.currency
      || "VND";
    return CURRENCY_SYMBOLS[cur] || cur;
  });

  // Union of all countries
  let allCountryNames = [...new Set(
    branchLookups.flatMap((bl) => Object.keys(bl.map))
  )];
  if (filterCountry) {
    allCountryNames = allCountryNames.filter((c) => c === filterCountry);
  }

  // Build rows — sort by first branch nights desc
  const rows = allCountryNames.map((country) => {
    const perBranch = branchLookups.map((bl) =>
      bl.map[country] || { current_nights: 0, current_revenue: 0, current_guests: 0 }
    );
    return { country, perBranch };
  }).sort((a, b) => {
    const aNights = a.perBranch.reduce((s, p) => s + p.current_nights, 0);
    const bNights = b.perBranch.reduce((s, p) => s + p.current_nights, 0);
    return bNights - aNights;
  });

  // Per-branch totals
  const totals = branchLookups.map((_, idx) => {
    const tot = { nights: 0, revenue: 0, guests: 0 };
    for (const r of rows) {
      tot.nights += r.perBranch[idx].current_nights;
      tot.revenue += r.perBranch[idx].current_revenue;
      tot.guests += r.perBranch[idx].current_guests;
    }
    return tot;
  });

  return (
    <>
      {/* KPI Cards — one pair (nights + revenue) per branch */}
      <div className={`grid gap-4`} style={{ gridTemplateColumns: `repeat(${Math.min(selectedBranches.length * 2, 6)}, minmax(0, 1fr))` }}>
        {branchNames.map((name, idx) => {
          const color = BRANCH_COLORS[idx % BRANCH_COLORS.length];
          return [
            <div key={`n-${idx}`} className={`bg-white rounded-lg border p-4 ${color.bg}`}>
              <p className={`text-xs uppercase tracking-wider mb-1 ${color.header}`}>
                {name} Nights
              </p>
              <p className={`text-2xl font-bold ${color.text}`}>{fmtNum(totals[idx].nights)}</p>
              <p className="text-xs text-gray-400 mt-1">{monthName} {year}</p>
            </div>,
            <div key={`r-${idx}`} className={`bg-white rounded-lg border p-4 ${color.bg}`}>
              <p className={`text-xs uppercase tracking-wider mb-1 ${color.header}`}>
                {name} Revenue ({branchCurrencyLabels[idx]})
              </p>
              <p className={`text-2xl font-bold ${color.text}`}>{fmtNum(totals[idx].revenue)}</p>
              <p className="text-xs text-gray-400 mt-1">{monthName} {year}</p>
            </div>,
          ];
        })}
      </div>

      {/* Comparison Table */}
      <div className="bg-white rounded-lg border overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="text-center px-3 py-3 font-semibold text-gray-600 w-10">#</th>
              <th className="text-left px-4 py-3 font-semibold text-gray-600">Country</th>
              {branchNames.map((name, idx) => {
                const color = BRANCH_COLORS[idx % BRANCH_COLORS.length];
                return [
                  <th key={`n-${idx}`} className={`text-right px-4 py-3 font-semibold ${color.header}`}>
                    {name}<br /><span className={`text-xs font-normal ${color.headerSub}`}>Nights</span>
                  </th>,
                  <th key={`r-${idx}`} className={`text-right px-4 py-3 font-semibold ${color.header}`}>
                    {name}<br /><span className={`text-xs font-normal ${color.headerSub}`}>Revenue ({branchCurrencyLabels[idx]})</span>
                  </th>,
                  <th key={`g-${idx}`} className={`text-right px-4 py-3 font-semibold ${color.header}`}>
                    {name}<br /><span className={`text-xs font-normal ${color.headerSub}`}>Guests</span>
                  </th>,
                ];
              })}
            </tr>
          </thead>
          <tbody className="divide-y">
            {rows.map((r, i) => (
              <tr key={r.country} className="hover:bg-gray-50">
                <td className="px-3 py-2.5 text-center text-gray-400 font-mono text-xs">{i + 1}</td>
                <td className="px-4 py-2.5 font-medium text-gray-900">{r.country}</td>
                {r.perBranch.map((pb, idx) => {
                  const color = BRANCH_COLORS[idx % BRANCH_COLORS.length];
                  return [
                    <td key={`n-${idx}`} className={`px-4 py-2.5 text-right font-semibold ${color.cell}`}>{fmtNum(pb.current_nights)}</td>,
                    <td key={`r-${idx}`} className={`px-4 py-2.5 text-right font-semibold ${color.cell}`}>{fmtNum(pb.current_revenue)}</td>,
                    <td key={`g-${idx}`} className={`px-4 py-2.5 text-right font-semibold ${color.cell}`}>{fmtNum(pb.current_guests)}</td>,
                  ];
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}


/* ── Helpers ─────────────────────────────────────────────────────────────────── */

function ChangeBadge({ value }) {
  if (value == null) return <span className="text-gray-300">-</span>;
  const isUp = value > 0;
  const isZero = value === 0;
  const cls = isUp
    ? "bg-emerald-50 text-emerald-700"
    : isZero
    ? "bg-gray-50 text-gray-500"
    : "bg-red-50 text-red-700";
  return (
    <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}>
      {isUp ? "+" : ""}{value.toFixed(1)}%
    </span>
  );
}

function PctChange({ current, previous, label }) {
  const pct = ((current - previous) / previous) * 100;
  const isUp = pct > 0;
  const cls = isUp ? "text-green-600" : pct < 0 ? "text-red-600" : "text-gray-500";
  return (
    <div className="flex items-center gap-1.5 mt-1">
      <span className={"text-xs font-medium " + cls}>
        {isUp ? "\u25B2" : pct < 0 ? "\u25BC" : ""}{Math.abs(pct).toFixed(1)}%
      </span>
      <span className="text-xs text-gray-400">vs {label}</span>
    </div>
  );
}


/* \u2500\u2500 CountryCombobox \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
 * Searchable replacement for the old <select> filter. Needed because backfilling
 * NULL guest_country to "Unknown" surfaced 50+ countries that used to be hidden,
 * making a plain dropdown unwieldy.
 *
 * Behaviour:
 *   - Click input: opens panel showing full list (or filtered by current text)
 *   - Type: filters case-insensitively; Enter picks the first match
 *   - Esc closes; click outside closes
 *   - Selecting an item commits it to `value` and shows it as the input text
 *   - "All Countries" is the synthetic top item that maps to value = ""
 */
function CountryCombobox({ value, onChange, options }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const wrapRef = useRef(null);
  const inputRef = useRef(null);

  // Close on outside click
  useEffect(() => {
    function onDocClick(e) {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) {
        setOpen(false);
        setQuery("");
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  // Filter options against the live query (case-insensitive, substring match)
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) => o.toLowerCase().includes(q));
  }, [options, query]);

  const select = (val) => {
    onChange(val);
    setQuery("");
    setOpen(false);
    inputRef.current?.blur();
  };

  // Display: when closed, show selected value; when open, show what user types
  const displayValue = open ? query : (value || "");
  const placeholder = value ? "" : "All Countries";

  return (
    <div ref={wrapRef} className="relative w-48">
      <input
        ref={inputRef}
        type="text"
        value={displayValue}
        placeholder={placeholder}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Escape") { setOpen(false); setQuery(""); inputRef.current?.blur(); }
          else if (e.key === "Enter" && open) {
            // Enter on empty query with a current value clears it; otherwise pick first match
            if (!query && value) select("");
            else if (filtered.length > 0) select(filtered[0]);
          }
        }}
        className="border rounded px-2 py-1.5 text-sm w-full pr-7 bg-white"
      />
      {/* Clear / chevron */}
      {value && !open ? (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); select(""); }}
          className="absolute right-1.5 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-xs px-1"
          title="Clear"
        >
          \u00D7
        </button>
      ) : (
        <span className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none text-xs">
          \u25BE
        </span>
      )}

      {open && (
        <div className="absolute z-20 mt-1 w-full max-h-72 overflow-y-auto bg-white border border-gray-200 rounded-lg shadow-lg text-sm">
          {/* "All Countries" sentinel */}
          <button
            type="button"
            onClick={() => select("")}
            className={`w-full text-left px-3 py-1.5 hover:bg-indigo-50 ${
              value === "" ? "bg-indigo-100 text-indigo-700 font-medium" : "text-gray-700"
            }`}
          >
            All Countries
          </button>
          <div className="border-t border-gray-100" />
          {filtered.length === 0 ? (
            <div className="px-3 py-2 text-gray-400 italic">No matches</div>
          ) : (
            filtered.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => select(c)}
                className={`w-full text-left px-3 py-1.5 hover:bg-indigo-50 ${
                  value === c ? "bg-indigo-100 text-indigo-700 font-medium" : "text-gray-700"
                }`}
              >
                {c}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
