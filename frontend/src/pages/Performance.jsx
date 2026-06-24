/**
 * Performance Hub — links to sub-pages + KPI Target vs Actual grid (editable)
 */
import { useEffect, useState, useCallback } from "react";
import { Link } from "react-router-dom";
import axios from "axios";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";

// ── Period helpers ──────────────────────────────────────────────────────────

function toISO(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function getPeriodRange(key) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const to = new Date(today);
  let from;
  switch (key) {
    case "7d":
      from = new Date(today); from.setDate(from.getDate() - 6); break;
    case "30d":
      from = new Date(today); from.setDate(from.getDate() - 29); break;
    case "90d":
      from = new Date(today); from.setDate(from.getDate() - 89); break;
    case "this_month":
      from = new Date(today.getFullYear(), today.getMonth(), 1); break;
    case "last_month": {
      const lm = new Date(today.getFullYear(), today.getMonth() - 1, 1);
      from = lm;
      to.setTime(new Date(today.getFullYear(), today.getMonth(), 0).getTime());
      break;
    }
    case "ytd":
      from = new Date(today.getFullYear(), 0, 1); break;
    default:
      from = new Date(today); from.setDate(from.getDate() - 29);
  }
  return { from: toISO(from), to: toISO(to) };
}

const PERIOD_OPTIONS = [
  { key: "7d", label: "7 Days" },
  { key: "30d", label: "30 Days" },
  { key: "90d", label: "90 Days" },
  { key: "this_month", label: "This Month" },
  { key: "last_month", label: "Last Month" },
  { key: "ytd", label: "Year to Date" },
];

// ── KPI Achievement Component ───────────────────────────────────────────────

function KPIAchievement({ branchId }) {
  const [period, setPeriod] = useState("90d");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const { from, to } = getPeriodRange(period);
    setLoading(true);
    const params = `date_from=${from}&date_to=${to}` + (branchId ? `&branch_id=${branchId}` : "");
    axios.get("/api/kpi/period-achievement?" + params)
      .then(r => {
        const rows = r.data.data || [];
        if (branchId) {
          setData(rows[0] || null);
        } else {
          if (!rows.length) { setData(null); return; }
          // Branches use different currencies — roll up the group total in VND,
          // the common denominator stored alongside every native amount.
          const agg = {
            actual_revenue_vnd: 0, target_revenue_vnd: 0,
            total_days: rows[0].total_days, date_from: rows[0].date_from, date_to: rows[0].date_to,
            branches: rows,
          };
          for (const r of rows) {
            agg.actual_revenue_vnd += r.actual_revenue_vnd || 0;
            agg.target_revenue_vnd += r.target_revenue_vnd || 0;
          }
          agg.achievement_pct = agg.target_revenue_vnd > 0
            ? agg.actual_revenue_vnd / agg.target_revenue_vnd
            : null;
          if (agg.total_days > 0) {
            agg.daily_goal_vnd = agg.target_revenue_vnd / agg.total_days;
            agg.daily_actual_vnd = agg.actual_revenue_vnd / agg.total_days;
          }
          setData(agg);
        }
      })
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [period, branchId]);

  const fmtDate = (s) => {
    const d = new Date(s + "T00:00:00");
    return d.toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit", year: "numeric" });
  };

  const pct = data?.achievement_pct;
  const pctDisplay = pct != null ? (pct * 100).toFixed(1) + "%" : "\u2014";
  const pctColor =
    pct == null ? "text-gray-400" :
    pct >= 1.0  ? "text-green-600" :
    pct >= 0.8  ? "text-yellow-600" :
    pct >= 0.6  ? "text-orange-600" :
                  "text-red-600";
  const barColor =
    pct == null ? "bg-gray-300" :
    pct >= 1.0  ? "bg-green-500" :
    pct >= 0.8  ? "bg-yellow-400" :
    pct >= 0.6  ? "bg-orange-400" :
                  "bg-red-500";
  const barWidth = pct != null ? Math.min(pct * 100, 100).toFixed(1) + "%" : "0%";

  const cur = data?.currency || (data?.branches?.[0]?.currency) || "";
  const fmtVal = (v) => {
    if (v == null) return "\u2014";
    const sym = CURRENCY_SYMBOLS[cur] || cur || "";
    return sym + new Intl.NumberFormat("en").format(Math.round(v));
  };

  const fmtBranch = (v, c) => {
    if (v == null) return "\u2014";
    const sym = CURRENCY_SYMBOLS[c] || c || "";
    return sym + new Intl.NumberFormat("en").format(Math.round(v));
  };

  const fmtVnd = (v) => (v == null ? "\u2014" : "\u20ab" + new Intl.NumberFormat("en").format(Math.round(v)));

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm">
      <div className="px-5 py-4 border-b border-gray-100 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <div className="w-2.5 h-2.5 rounded-full bg-indigo-500" />
          <h2 className="font-semibold text-gray-800">Goal Achievement</h2>
          {data && (
            <span className="text-xs text-gray-400 ml-1">
              {fmtDate(data.date_from)} – {fmtDate(data.date_to)}
            </span>
          )}
        </div>
        <div className="flex gap-1">
          {PERIOD_OPTIONS.map(opt => (
            <button
              key={opt.key}
              onClick={() => setPeriod(opt.key)}
              className={
                "px-3 py-1.5 text-xs font-medium rounded-lg transition-colors " +
                (period === opt.key
                  ? "bg-indigo-600 text-white shadow-sm"
                  : "bg-gray-50 text-gray-600 hover:bg-gray-100 border border-gray-200")
              }
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      <div className="px-5 pt-3 text-[11px] leading-relaxed text-gray-400">
        Achievement % = Adjusted Actual ÷ Adjusted Target. Both sides adjusted per branch:
        <span className="text-gray-500"> Revenue × (1 − Deduction %) + Other Revenue</span>.
        Other Revenue is a monthly figure prorated by day, so it scales with the selected range.
      </div>

      <div className="px-5 py-5">
        {loading ? (
          <div className="text-center py-4 text-gray-400 animate-pulse">Loading...</div>
        ) : !data ? (
          <div className="text-center py-4 text-gray-400">No data available</div>
        ) : data.branches ? (
          <div className="space-y-4">
            {/* Group total — all branches summed in VND (common currency) */}
            <div className="pb-4 border-b border-gray-200">
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-semibold text-gray-800">
                  All Branches <span className="font-normal text-gray-400">(VND)</span>
                </span>
                <span className={"text-lg font-bold " + pctColor}>{pctDisplay}</span>
              </div>
              <div className="flex items-baseline gap-2 mb-2">
                <span className="text-xl font-bold text-gray-800">
                  {fmtVnd(data.actual_revenue_vnd)}
                </span>
                <span className="text-sm text-gray-400">
                  / {fmtVnd(data.target_revenue_vnd)}
                </span>
              </div>
              <div className="h-2.5 bg-gray-100 rounded-full overflow-hidden mb-1.5">
                <div className={"h-full rounded-full transition-all duration-500 " + barColor} style={{ width: barWidth }} />
              </div>
              <p className="text-xs text-gray-400">
                Daily Goal {fmtVnd(data.daily_goal_vnd)}
                {" · "}
                Daily Actual {fmtVnd(data.daily_actual_vnd)}
                {data.daily_goal_vnd > 0 && (() => {
                  const diff = ((data.daily_actual_vnd - data.daily_goal_vnd) / data.daily_goal_vnd * 100).toFixed(1);
                  return (
                    <span className={Number(diff) >= 0 ? "text-green-600 ml-1" : "text-red-500 ml-1"}>
                      ({Number(diff) >= 0 ? "+" : ""}{diff}%)
                    </span>
                  );
                })()}
              </p>
            </div>
            {data.branches.map(row => {
              const rPct = row.achievement_pct;
              const rPctDisplay = rPct != null ? (rPct * 100).toFixed(1) + "%" : "\u2014";
              const rPctColor =
                rPct == null ? "text-gray-400" :
                rPct >= 1.0  ? "text-green-600" :
                rPct >= 0.8  ? "text-yellow-600" :
                rPct >= 0.6  ? "text-orange-600" :
                               "text-red-600";
              const rBarColor =
                rPct == null ? "bg-gray-300" :
                rPct >= 1.0  ? "bg-green-500" :
                rPct >= 0.8  ? "bg-yellow-400" :
                rPct >= 0.6  ? "bg-orange-400" :
                               "bg-red-500";
              const rBarW = rPct != null ? Math.min(rPct * 100, 100).toFixed(1) + "%" : "0%";
              const dailyDiff = row.daily_goal > 0
                ? ((row.daily_actual - row.daily_goal) / row.daily_goal * 100).toFixed(1)
                : null;

              return (
                <div key={row.branch_id}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-sm font-medium text-gray-700">{row.branch_name}</span>
                    <span className={"text-lg font-bold " + rPctColor}>{rPctDisplay}</span>
                  </div>
                  <div className="flex items-baseline gap-2 mb-2">
                    <span className="text-xl font-bold text-gray-800">
                      {fmtBranch(row.actual_revenue, row.currency)}
                    </span>
                    <span className="text-sm text-gray-400">
                      / {fmtBranch(row.target_revenue, row.currency)}
                    </span>
                  </div>
                  <div className="h-2 bg-gray-100 rounded-full overflow-hidden mb-1.5">
                    <div className={"h-full rounded-full transition-all duration-500 " + rBarColor} style={{ width: rBarW }} />
                  </div>
                  <p className="text-xs text-gray-400">
                    Daily Goal {fmtBranch(row.daily_goal, row.currency)}
                    {" \u00b7 "}
                    Daily Actual {fmtBranch(row.daily_actual, row.currency)}
                    {dailyDiff != null && (
                      <span className={Number(dailyDiff) >= 0 ? "text-green-600 ml-1" : "text-red-500 ml-1"}>
                        ({Number(dailyDiff) >= 0 ? "+" : ""}{dailyDiff}%)
                      </span>
                    )}
                  </p>
                </div>
              );
            })}
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between mb-2">
              <div>
                <span className="text-2xl font-bold text-gray-800">
                  {fmtVal(data.actual_revenue)}
                </span>
                <span className="text-sm text-gray-400 ml-2">
                  / {fmtVal(data.target_revenue)}
                </span>
              </div>
              <span className={"text-2xl font-bold " + pctColor}>{pctDisplay}</span>
            </div>
            <div className="h-2.5 bg-gray-100 rounded-full overflow-hidden mb-3">
              <div className={"h-full rounded-full transition-all duration-500 " + barColor} style={{ width: barWidth }} />
            </div>
            <p className="text-xs text-gray-400">
              Daily Goal {fmtVal(data.daily_goal)}
              {" \u00b7 "}
              Daily Actual {fmtVal(data.daily_actual)}
              {data.daily_goal > 0 && (() => {
                const diff = ((data.daily_actual - data.daily_goal) / data.daily_goal * 100).toFixed(1);
                return (
                  <span className={Number(diff) >= 0 ? "text-green-600 ml-1" : "text-red-500 ml-1"}>
                    ({Number(diff) >= 0 ? "+" : ""}{diff}%)
                  </span>
                );
              })()}
            </p>
          </>
        )}
      </div>
    </div>
  );
}

const CARDS = [
  { to: "/performance/daily", title: "Daily Brief", desc: "OCC%, Revenue, ADR, RevPAR per branch", color: "bg-indigo-50 border-indigo-200", icon: "\uD83D\uDCC5" },
  { to: "/performance/weekly", title: "Weekly Brief", desc: "Revenue trend, cancellation %, OTA mix", color: "bg-emerald-50 border-emerald-200", icon: "\uD83D\uDCCA" },
  { to: "/performance/monthly", title: "Monthly Brief", desc: "OCC/Revenue/ADR/RevPAR + country breakdown", color: "bg-amber-50 border-amber-200", icon: "\uD83D\uDDD3\uFE0F" },
  { to: "/performance/ota", title: "OTA Channel Mix", desc: "OTA vs Direct split by bookings and revenue", color: "bg-rose-50 border-rose-200", icon: "\uD83D\uDD00" },
];

const MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function fmtNum(val) {
  if (val == null || val === 0) return "\u2014";
  return new Intl.NumberFormat("en").format(Math.round(val));
}

function hitColor(pct) {
  if (pct == null) return "";
  if (pct >= 100) return "bg-green-100 text-green-800";
  if (pct >= 80) return "bg-yellow-50 text-yellow-700";
  return "bg-red-50 text-red-700";
}

function hitBg(pct) {
  if (pct == null) return "";
  if (pct >= 100) return "bg-green-50";
  if (pct >= 80) return "bg-yellow-50/50";
  if (pct > 0) return "bg-red-50/50";
  return "";
}

export default function Performance() {
  const { isAll, selected, currentBranch } = useBranch();
  const currentYear = new Date().getFullYear();
  const [year, setYear] = useState(currentYear);
  const [grid, setGrid] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    const params = { year };
    if (!isAll && selected) params.branch_id = selected;
    axios.get("/api/kpi/yearly-grid", { params })
      .then((r) => setGrid(r.data.data))
      .catch(() => setGrid(null))
      .finally(() => setLoading(false));
  }, [year, selected, isAll]);

  useEffect(load, [load]);

  const branches = grid?.branches || [];
  const months = grid?.months || [];
  const totals = grid?.totals || [];

  const handleSaveActual = (branchId, month, value) => {
    axios.put("/api/kpi/actual-override", {
      branch_id: branchId,
      year,
      month,
      actual_revenue: value,
    }).then(() => load());
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-800">Performance Hub</h1>
        <p className="text-sm text-gray-500">Daily, weekly, and monthly briefs</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {CARDS.map(({ to, title, desc, color, icon }) => (
          <Link key={to} to={to}
            className={`rounded-xl border p-6 ${color} hover:shadow-md transition-shadow`}>
            <div className="text-2xl mb-2">{icon}</div>
            <h2 className="text-base font-semibold text-gray-800">{title}</h2>
            <p className="text-sm text-gray-500 mt-1">{desc}</p>
          </Link>
        ))}
      </div>

      {/* Goal Achievement — period-based KPI tracker */}
      <KPIAchievement branchId={isAll ? null : selected} />

      {/* KPI Target vs Actual Grid */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-bold text-gray-800">Revenue KPI</h2>
            <p className="text-xs text-gray-400">Click on Actual cells to edit (accounting override)</p>
          </div>
          <select value={year} onChange={(e) => setYear(Number(e.target.value))}
            className="border rounded px-3 py-1.5 text-sm font-medium">
            {[currentYear, currentYear - 1, currentYear - 2].map((y) => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
        </div>

        {loading ? (
          <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading...</div>
        ) : !grid || branches.length === 0 ? (
          <div className="text-center text-gray-400 py-12 text-sm">No KPI data available for {year}.</div>
        ) : (
          <div className="bg-white rounded-lg border overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="bg-gray-900 text-white">
                  <th className="px-4 py-2.5 text-left font-semibold sticky left-0 bg-gray-900 z-10">{year}</th>
                  {branches.map((b) => (
                    <th key={b.id} colSpan={3} className="px-2 py-2.5 text-center font-semibold border-l border-gray-700">
                      {b.name.replace("HiD ", "")}
                    </th>
                  ))}
                </tr>
                <tr className="bg-gray-100">
                  <th className="px-4 py-2 text-left text-xs text-gray-500 font-medium sticky left-0 bg-gray-100 z-10">Month</th>
                  {branches.map((b) => [
                    <th key={b.id + "-t"} className="px-2 py-2 text-right text-xs text-gray-500 font-medium border-l border-gray-200">Target</th>,
                    <th key={b.id + "-a"} className="px-2 py-2 text-right text-xs text-gray-500 font-medium">Actual</th>,
                    <th key={b.id + "-h"} className="px-2 py-2 text-center text-xs text-gray-500 font-medium">Hit %</th>,
                  ])}
                </tr>
              </thead>
              <tbody>
                {months.map((row) => (
                  <tr key={row.month} className="border-t border-gray-100 hover:bg-gray-50/50">
                    <td className="px-4 py-2 font-medium text-gray-700 sticky left-0 bg-white z-10">
                      {MONTHS[row.month]}
                    </td>
                    {row.branches.map((bd, bi) => (
                      <BranchCells
                        key={bd.branch_id}
                        data={bd}
                        currency={branches[bi]?.currency}
                        month={row.month}
                        onSave={handleSaveActual}
                      />
                    ))}
                  </tr>
                ))}
                <tr className="border-t-2 border-gray-300 bg-gray-50 font-semibold">
                  <td className="px-4 py-2.5 font-bold text-gray-900 sticky left-0 bg-gray-50 z-10">Total</td>
                  {totals.map((td, bi) => (
                    <BranchCells key={td.branch_id} data={td} currency={branches[bi]?.currency} isTotal />
                  ))}
                </tr>
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function BranchCells({ data, currency, isTotal, month, onSave }) {
  const sym = CURRENCY_SYMBOLS[currency] || "";
  const hc = hitColor(data.hit_pct);
  const bg = isTotal ? "" : hitBg(data.hit_pct);
  const [editing, setEditing] = useState(false);
  const [editVal, setEditVal] = useState("");

  const startEdit = () => {
    if (isTotal || !onSave) return;
    setEditVal(data.actual > 0 ? String(Math.round(data.actual)) : "");
    setEditing(true);
  };

  const save = () => {
    setEditing(false);
    const num = parseFloat(editVal.replace(/,/g, ""));
    if (isNaN(num) && editVal !== "") return;
    // Empty = clear override (revert to Cloudbeds)
    onSave(data.branch_id, month, editVal === "" ? null : num);
  };

  const handleKey = (e) => {
    if (e.key === "Enter") save();
    if (e.key === "Escape") setEditing(false);
  };

  return (
    <>
      <td className={`px-2 py-2 text-right tabular-nums border-l border-gray-100 ${bg}`}>
        {data.target > 0 ? sym + fmtNum(data.target) : <span className="text-gray-300">{"\u2014"}</span>}
      </td>
      <td
        className={`px-2 py-2 text-right tabular-nums ${bg} ${!isTotal ? "cursor-pointer hover:bg-indigo-50" : ""}`}
        onClick={startEdit}
        title={!isTotal ? (data.is_override ? "Manually overridden (click to edit)" : "Click to override Cloudbeds value") : undefined}
      >
        {editing ? (
          <input
            type="text"
            value={editVal}
            onChange={(e) => setEditVal(e.target.value)}
            onBlur={save}
            onKeyDown={handleKey}
            autoFocus
            className="w-24 px-1 py-0.5 text-right text-sm border border-indigo-400 rounded outline-none"
          />
        ) : (
          <span className={data.is_override ? "border-b border-dashed border-indigo-400" : ""}>
            {data.actual > 0 ? sym + fmtNum(data.actual) : <span className="text-gray-300">{"\u2014"}</span>}
          </span>
        )}
      </td>
      <td className={`px-2 py-2 text-center tabular-nums ${bg}`}>
        {data.hit_pct != null ? (
          <span className={`inline-block px-2 py-0.5 rounded text-xs font-semibold ${hc}`}>
            {data.hit_pct.toFixed(1)}%
          </span>
        ) : (
          <span className="text-gray-300">{"\u2014"}</span>
        )}
      </td>
    </>
  );
}
