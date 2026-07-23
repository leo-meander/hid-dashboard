import { useState, useRef, useEffect } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { useBranch } from "../context/BranchContext";
import { getTeamKpiSummary, upsertTarget, upsertActual, getTaskOverview } from "../api/teamKpi";

const ROLES = [
  { key: "kol",      label: "KOL",       person: "Mel",   emoji: "🤝" },
  { key: "paid_ads", label: "Paid Ads",  person: "Mason", emoji: "📢" },
  { key: "designer", label: "Designer",  person: "Nora",  emoji: "🎨" },
  { key: "crm",      label: "CRM",       person: "Kin",   emoji: "📊" },
  { key: "pm",       label: "PM",        person: "Nuha",  emoji: "🗂️" },
];

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const CURRENT_YEAR = new Date().getFullYear();

// ── Color helpers ─────────────────────────────────────────────────────────────

function pctColor(pct, higherIsBetter = true) {
  if (pct === null || pct === undefined) return null;
  const v = higherIsBetter ? pct : (200 - pct);  // invert for lower-is-better (budget utilisation)
  if (v >= 100) return "green";
  if (v >= 80)  return "yellow";
  if (v >= 60)  return "orange";
  return "red";
}

const COLOR_CLASSES = {
  green:  { bg: "bg-green-50",  text: "text-green-700",  badge: "bg-green-100 text-green-700",  ring: "#22c55e" },
  yellow: { bg: "bg-yellow-50", text: "text-yellow-700", badge: "bg-yellow-100 text-yellow-700", ring: "#eab308" },
  orange: { bg: "bg-orange-50", text: "text-orange-700", badge: "bg-orange-100 text-orange-700", ring: "#f97316" },
  red:    { bg: "bg-red-50",    text: "text-red-700",    badge: "bg-red-100 text-red-700",    ring: "#ef4444" },
};

// ── Per-KPI formula tooltips ──────────────────────────────────────────────────

const KPI_TOOLTIPS = {
  delivery_rate:        "On-Time Delivery Rate — Nora's completed tasks with Deadline in that month where 'On-time vs Original' = On-time. Formula: on-time tasks ÷ total completed tasks × 100. Source: Lark Base.",
  task_completion_rate: "Team Task Completion Rate — all tasks across the team with a Deadline in that month, regardless of assignee. Formula: completed tasks ÷ total tasks due × 100. Source: Lark Base.",
  branch_kpi_rate:      "Branch KPI Achievement Rate — actual revenue (Cloudbeds or manual override, after deductions) ÷ target revenue × 100. Mirrors the Revenue KPI page formula.",
  budget_utilisation:   "Marketing Budget Utilisation — total actual spend (Paid Ads + KOL + CRM) ÷ total allocated budget × 100 for the branch and month.",
  roas:                 "Return on Ad Spend — Revenue from Paid Ads ÷ Ad Spend. In the All view, this is a weighted average: total revenue across all branches ÷ total spend across all branches.",
  crm_revenue:          "Revenue attributed to CRM campaigns in that month. Enter targets in mil VND (e.g. enter 56 for 56 million VND).",
};

// ── Tooltip ───────────────────────────────────────────────────────────────────

function Tooltip({ text, children }) {
  const [show, setShow] = useState(false);
  return (
    <span className="relative inline-flex items-center" onMouseEnter={() => setShow(true)} onMouseLeave={() => setShow(false)}>
      {children}
      {show && (
        <span className="absolute z-50 bottom-full left-1/2 -translate-x-1/2 mb-1.5 w-56 text-[11px] leading-relaxed bg-gray-900 text-white rounded-lg px-2.5 py-2 shadow-lg pointer-events-none whitespace-normal text-center">
          {text}
          <span className="absolute top-full left-1/2 -translate-x-1/2 border-4 border-transparent border-t-gray-900" />
        </span>
      )}
    </span>
  );
}

// ── Achievement ring (SVG) ────────────────────────────────────────────────────

function AchievementRing({ pct, label, sub }) {
  const color = pct !== null ? pctColor(pct) : null;
  const ringColor = color ? COLOR_CLASSES[color].ring : "#9ca3af";
  const radius = 52;
  const circ = 2 * Math.PI * radius;
  const filled = pct !== null ? Math.min(pct / 100, 1) * circ : 0;

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative w-36 h-36">
        <svg className="w-full h-full -rotate-90" viewBox="0 0 120 120">
          <circle cx="60" cy="60" r={radius} fill="none" stroke="#e5e7eb" strokeWidth="10" />
          {pct !== null && (
            <circle
              cx="60" cy="60" r={radius}
              fill="none"
              stroke={ringColor}
              strokeWidth="10"
              strokeDasharray={`${filled} ${circ}`}
              strokeLinecap="round"
              style={{ transition: "stroke-dasharray 0.5s ease" }}
            />
          )}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className={`text-2xl font-bold ${color ? COLOR_CLASSES[color].text : "text-gray-400"}`}>
            {pct !== null ? `${pct}%` : "—"}
          </span>
        </div>
      </div>
      <p className="text-sm font-semibold text-gray-700">{label}</p>
      {sub && <p className="text-xs text-gray-400">{sub}</p>}
    </div>
  );
}

// ── YTD horizontal bars ───────────────────────────────────────────────────────

function YtdBars({ kpis }) {
  const withPct = kpis
    .map(k => {
      const actuals = k.monthly.filter(m => !m.is_future && m.actual !== null && m.has_target && m.target > 0);
      if (!actuals.length) return null;
      const avgPct = actuals.reduce((s, m) => s + (m.actual / m.target * 100), 0) / actuals.length;
      return { label: k.label, pct: Math.round(avgPct * 10) / 10, higherIsBetter: k.higher_is_better };
    })
    .filter(Boolean);

  if (!withPct.length) return <p className="text-xs text-gray-400 mt-2">No data yet</p>;

  return (
    <div className="space-y-3">
      {withPct.map(({ label, pct, higherIsBetter }) => {
        const color = pctColor(pct, higherIsBetter);
        const cls = color ? COLOR_CLASSES[color] : { bg: "bg-gray-100", text: "text-gray-500" };
        return (
          <div key={label}>
            <div className="flex justify-between text-xs mb-1">
              <span className="text-gray-600 truncate max-w-[140px]">{label}</span>
              <span className={`font-semibold ${cls.text}`}>{pct}%</span>
            </div>
            <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-500 ${cls.bg.replace("bg-", "bg-").replace("-50", "-400")}`}
                style={{ width: `${Math.min(pct, 100)}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Editable cell ─────────────────────────────────────────────────────────────

function EditableCell({ value, onSave, placeholder = "—", disabled }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef(null);

  const start = () => {
    if (disabled) return;
    setDraft(value !== null && value !== undefined ? String(value) : "");
    setEditing(true);
    setTimeout(() => inputRef.current?.focus(), 0);
  };

  const commit = () => {
    const num = draft.trim() === "" ? null : parseFloat(draft.replace(/,/g, ""));
    setEditing(false);
    if (!isNaN(num) || draft.trim() === "") onSave(num);
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={draft}
        onChange={e => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={e => { if (e.key === "Enter") commit(); if (e.key === "Escape") setEditing(false); }}
        className="w-full text-center text-xs border border-blue-400 rounded px-1 py-0.5 bg-white outline-none"
      />
    );
  }

  return (
    <button
      onClick={start}
      disabled={disabled}
      title={disabled ? undefined : "Click to edit"}
      className={`w-full text-center text-xs rounded px-1 py-0.5 transition-colors
        ${disabled ? "cursor-default text-gray-400" : "hover:bg-yellow-100 cursor-pointer text-gray-700"}
        ${value !== null && value !== undefined ? "font-medium" : "text-gray-300"}`}
    >
      {value !== null && value !== undefined
        ? typeof value === "number" ? value.toLocaleString() : value
        : placeholder}
    </button>
  );
}

// ── KPI Grid ──────────────────────────────────────────────────────────────────

function KpiGrid({ kpis, roleKey, branchId, year, autoActuals, onRefresh }) {
  const [saving, setSaving] = useState(null); // "{kpi_key}-{month}-target|actual"

  const saveTarget = async (kpiKey, month, value) => {
    const key = `${kpiKey}-${month}-target`;
    setSaving(key);
    try {
      await upsertTarget({ role_key: roleKey, branch_id: branchId || null, year, month, kpi_key: kpiKey, target_value: value });
      onRefresh();
    } catch (e) {
      console.error("save target failed", e);
    } finally {
      setSaving(null);
    }
  };

  const saveActual = async (kpiKey, month, value) => {
    const key = `${kpiKey}-${month}-actual`;
    setSaving(key);
    try {
      await upsertActual({ role_key: roleKey, branch_id: branchId || null, year, month, kpi_key: kpiKey, actual_value: value });
      onRefresh();
    } catch (e) {
      console.error("save actual failed", e);
    } finally {
      setSaving(null);
    }
  };

  if (!kpis?.length) return null;

  return (
    <div className="overflow-x-auto rounded-xl border border-gray-200 shadow-sm">
      <table className="min-w-full text-xs border-collapse">
        <thead>
          <tr className="bg-gray-50 border-b border-gray-200">
            <th className="sticky left-0 bg-gray-50 text-left px-4 py-2.5 font-semibold text-gray-700 w-44 z-10 border-r border-gray-200">
              KPI
            </th>
            <th className="px-2 py-2.5 font-semibold text-gray-500 text-center w-14">Unit</th>
            <th className="px-2 py-2.5 font-semibold text-gray-500 text-center w-10">Type</th>
            {MONTHS.map(m => (
              <th key={m} className="px-1 py-2.5 font-semibold text-gray-600 text-center min-w-[52px]">{m}</th>
            ))}
            <th className="px-2 py-2.5 font-semibold text-gray-700 text-center w-16" title="Year-to-date actual — only months with a target set are included">YTD ⓘ</th>
            <th className="px-2 py-2.5 font-semibold text-gray-700 text-center w-16" title="Average achievement % — only months with a target set are counted">Avg % ⓘ</th>
          </tr>
        </thead>
        <tbody>
          {kpis.map((kpi, ki) => {
            // YTD and Avg% only count months that have a target set
            const targetedMonths = kpi.monthly.filter(m => !m.is_future && m.has_target);
            const ytdActual = targetedMonths.filter(m => m.actual !== null).reduce((s, m) => s + m.actual, 0);
            const actPcts = targetedMonths.filter(m => m.pct !== null).map(m => m.pct);
            const avgPct = actPcts.length ? Math.round(actPcts.reduce((a, b) => a + b, 0) / actPcts.length * 10) / 10 : null;
            const avgColor = pctColor(avgPct, kpi.higher_is_better);

            const rowSpan = kpi.no_target ? 1 : 2;
            return (
              <>
                {/* TARGET row — hidden for no_target KPIs */}
                {!kpi.no_target && (
                <tr className={`border-t border-gray-100 ${ki > 0 ? "border-t-2 border-gray-200" : ""}`}>
                  <td className="sticky left-0 bg-white px-4 py-1.5 font-semibold text-gray-800 border-r border-gray-200 z-10" rowSpan={2}>
                    <div className="flex items-center gap-1">
                      {kpi.label}
                      {KPI_TOOLTIPS[kpi.key] && (
                        <Tooltip text={KPI_TOOLTIPS[kpi.key]}>
                          <span className="text-[10px] text-gray-400 cursor-default">ⓘ</span>
                        </Tooltip>
                      )}
                    </div>
                    {kpi.org_wide && (
                      <span className="text-[10px] text-blue-500 font-normal">org-wide</span>
                    )}
                    {kpi.computed_target && (
                      <span className="text-[10px] text-gray-400 font-normal">= spend × ROAS target</span>
                    )}
                  </td>
                  <td className="px-2 py-1.5 text-center text-gray-400" rowSpan={2}>{kpi.unit}</td>
                  <td className="px-2 py-1.5 text-center">
                    <span
                      className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-yellow-100 text-yellow-700"
                      title={kpi.computed_target ? "Computed: actual spend × ROAS target" : undefined}
                    >T</span>
                  </td>
                  {kpi.monthly.map(m => (
                    <td key={m.month} className="px-1 py-1.5 bg-yellow-50">
                      {kpi.computed_target ? (
                        <div className="text-center text-gray-600 font-medium text-xs">
                          {m.target !== null && m.target !== undefined
                            ? m.target.toLocaleString(undefined, { maximumFractionDigits: kpi.decimals ?? 1 })
                            : <span className="text-gray-300">—</span>}
                        </div>
                      ) : (
                        <EditableCell
                          value={m.target}
                          disabled={m.is_future}
                          onSave={val => saveTarget(kpi.key, m.month, val)}
                          placeholder="—"
                        />
                      )}
                    </td>
                  ))}
                  <td className="px-2 py-1.5 text-center text-gray-400 font-medium">
                    {kpi.monthly.filter(m => m.target).reduce((s, m) => s + (m.target || 0), 0) > 0
                      ? kpi.monthly.filter(m => m.target).reduce((s, m) => s + (m.target || 0), 0).toLocaleString(undefined, { maximumFractionDigits: 1 })
                      : "—"}
                  </td>
                  <td className="px-2 py-1.5 text-center text-gray-400">—</td>
                </tr>
                )}
                {/* ACTUAL row */}
                <tr className={`border-b border-gray-200 ${kpi.no_target && ki > 0 ? "border-t-2 border-gray-200" : ""}`}>
                  {kpi.no_target && (
                    <>
                      <td className="sticky left-0 bg-white px-4 py-1.5 font-semibold text-gray-800 border-r border-gray-200 z-10">
                        <div className="flex items-center gap-1">
                          {kpi.label}
                          {KPI_TOOLTIPS[kpi.key] && (
                            <Tooltip text={KPI_TOOLTIPS[kpi.key]}>
                              <span className="text-[10px] text-gray-400 cursor-default">ⓘ</span>
                            </Tooltip>
                          )}
                        </div>
                        {kpi.org_wide && <span className="text-[10px] text-blue-500 font-normal">org-wide</span>}
                      </td>
                      <td className="px-2 py-1.5 text-center text-gray-400">{kpi.unit}</td>
                    </>
                  )}
                  <td className="px-2 py-1.5 text-center">
                    {kpi.auto_actuals === false
                      ? <span title="Enter manually" className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-orange-100 text-orange-600 cursor-help">M</span>
                      : <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-green-100 text-green-700">A</span>
                    }
                  </td>
                  {kpi.monthly.map(m => {
                    const color = m.has_target && m.pct !== null ? pctColor(m.pct, kpi.higher_is_better) : null;
                    const cls = color ? COLOR_CLASSES[color] : null;
                    const isSaving = saving === `${kpi.key}-${m.month}-actual`;

                    if ((autoActuals || kpi.auto_actuals) && kpi.auto_actuals !== false) {
                      // Read-only actual from API
                      return (
                        <td key={m.month} title={!m.has_target && m.actual !== null ? "No target set — excluded from YTD & Avg%" : undefined}
                          className={`px-1 py-1.5 text-center ${m.is_future ? "opacity-30" : ""} ${cls ? cls.bg : ""}`}>
                          {m.actual !== null ? (
                            <div className="flex flex-col items-center gap-0.5">
                              <span className={`font-semibold ${cls ? cls.text : "text-gray-400"}`}>
                                {typeof m.actual === "number"
                                  ? m.actual.toLocaleString(undefined, { maximumFractionDigits: kpi.decimals ?? 0 })
                                  : m.actual}
                              </span>
                              {m.pct !== null && m.has_target && kpi.unit !== "%" && (
                                <span className={`text-[10px] px-1 rounded ${cls ? cls.badge : ""}`}>{m.pct}%</span>
                              )}
                            </div>
                          ) : (
                            <span className="text-gray-300">—</span>
                          )}
                        </td>
                      );
                    }

                    // Manual actual entry
                    return (
                      <td key={m.month} className={`px-1 py-1.5 ${m.is_future ? "opacity-30 bg-gray-50" : cls ? cls.bg : "bg-white"}`}>
                        {isSaving ? (
                          <div className="flex justify-center"><div className="w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" /></div>
                        ) : (
                          <div className="flex flex-col items-center gap-0.5">
                            <EditableCell
                              value={m.actual}
                              disabled={m.is_future}
                              onSave={val => saveActual(kpi.key, m.month, val)}
                              placeholder="—"
                            />
                            {m.pct !== null && m.actual !== null && kpi.unit !== "%" && (
                              <span className={`text-[10px] px-1 rounded ${cls ? cls.badge : ""}`}>{m.pct}%</span>
                            )}
                          </div>
                        )}
                      </td>
                    );
                  })}
                  <td className="px-2 py-1.5 text-center">
                    {ytdActual > 0 ? (
                      <span className="font-semibold text-gray-700">
                        {ytdActual.toLocaleString(undefined, { maximumFractionDigits: kpi.decimals ?? 0 })}
                      </span>
                    ) : "—"}
                  </td>
                  <td className={`px-2 py-1.5 text-center font-semibold ${avgColor ? COLOR_CLASSES[avgColor].text : "text-gray-400"}`}>
                    {avgPct !== null ? `${avgPct}%` : "—"}
                  </td>
                </tr>
              </>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

// ── Task Overview ─────────────────────────────────────────────────────────────

const MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

// Mini bar chart SVG
function MiniBar({ value, max, color = "#6366f1" }) {
  const pct = max > 0 ? Math.min(value / max, 1) : 0;
  return (
    <div className="flex items-center gap-1.5">
      <div className="w-16 h-2 bg-gray-100 rounded-full overflow-hidden flex-shrink-0">
        <div className="h-full rounded-full" style={{ width: `${pct * 100}%`, backgroundColor: color }} />
      </div>
      <span className="text-xs text-gray-700 tabular-nums">{value}</span>
    </div>
  );
}

function computeScore(row) {
  if (!row) return null;
  const on_time_rate = row.on_time_rate ?? 0;
  const cycle_ratio  = row.cycle_ratio;
  const overdue      = row.overdue_count ?? 0;

  const s_ontime  = on_time_rate >= 90 ? 10 : on_time_rate >= 80 ? 8 : on_time_rate >= 70 ? 6 : 4;
  const s_cycle   = cycle_ratio == null ? 10 : cycle_ratio <= 1 ? 10 : cycle_ratio <= 1.2 ? 8 : cycle_ratio <= 1.5 ? 6 : 4;
  const s_overdue = overdue === 0 ? 10 : overdue <= 1 ? 8 : overdue <= 3 ? 6 : 4;
  const s_reopen  = 10;

  const total = s_ontime * 0.35 + s_cycle * 0.25 + s_overdue * 0.25 + s_reopen * 0.15;
  return Math.round(total * 10) / 10;
}

function ratingLabel(score) {
  if (score === null) return "—";
  if (score >= 9) return "Excellent";
  if (score >= 8) return "Good";
  if (score >= 7) return "Fair";
  if (score >= 5.5) return "Pass";
  return "Fail";
}

function ratingClasses(score) {
  if (score === null) return "bg-gray-50 text-gray-400";
  if (score >= 9) return "bg-green-100 text-green-700 font-semibold";
  if (score >= 8) return "bg-emerald-100 text-emerald-700 font-semibold";
  if (score >= 7) return "bg-blue-100 text-blue-700 font-semibold";
  if (score >= 5.5) return "bg-yellow-100 text-yellow-700 font-semibold";
  return "bg-red-100 text-red-700 font-semibold";
}

function fmtNum(v, decimals = 1) {
  if (v === null || v === undefined) return "—";
  return typeof v === "number" ? v.toFixed(decimals) : v;
}

function aggregateRows(rows) {
  // rows: array of month dicts; produce annual aggregate
  if (!rows.length) return null;
  const totals = { total_tasks: 0, completed: 0, on_time_count: 0, late_count: 0, overdue_count: 0 };
  let ct_sum = 0, ct_n = 0, ed_sum = 0, ed_n = 0;
  for (const r of rows) {
    totals.total_tasks   += r.total_tasks   ?? 0;
    totals.completed     += r.completed     ?? 0;
    totals.on_time_count += r.on_time_count ?? 0;
    totals.late_count    += r.late_count    ?? 0;
    totals.overdue_count += r.overdue_count ?? 0;
    if (r.cycle_time_avg != null) { ct_sum += r.cycle_time_avg; ct_n++; }
    if (r.estimated_avg  != null) { ed_sum += r.estimated_avg;  ed_n++; }
  }
  const cycle_time_avg = ct_n ? Math.round(ct_sum / ct_n * 10) / 10 : null;
  const estimated_avg  = ed_n ? Math.round(ed_sum / ed_n * 10) / 10 : null;
  const cycle_ratio    = (cycle_time_avg && estimated_avg) ? Math.round(cycle_time_avg / estimated_avg * 1000) / 1000 : null;
  const completion_rate = totals.total_tasks > 0 ? Math.round(totals.completed / totals.total_tasks * 100 * 10) / 10 : null;
  const on_time_rate    = totals.completed > 0   ? Math.round(totals.on_time_count / totals.completed * 100 * 10) / 10 : null;
  return { ...totals, cycle_time_avg, estimated_avg, cycle_ratio, completion_rate, on_time_rate };
}

function TaskOverview({ year }) {
  const [monthFilter, setMonthFilter] = useState(0); // 0 = All, 1–12 = specific month
  const [picFilter, setPicFilter] = useState("all"); // "all" or pic_id

  const { data, isPending, error } = useQuery({
    queryKey: ["task-overview", year],
    queryFn: () => getTaskOverview(year),
  });

  const thCls = "px-3 py-2 text-left text-[10px] font-semibold text-gray-500 uppercase tracking-wide border-b border-gray-200 whitespace-nowrap";
  const tdCls = "px-3 py-2 text-xs text-gray-700 border-b border-gray-100";

  if (isPending) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-12 flex justify-center">
        <div className="w-6 h-6 border-2 border-gray-300 border-t-gray-800 rounded-full animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3">
        {error?.response?.data?.detail || error.message || "Failed to load task overview"}
      </div>
    );
  }

  if (!data?.length) {
    return <div className="text-center py-8 text-sm text-gray-400">No task data available for {year}.</div>;
  }

  // Only show mapped PICs (skip "User XXXX" unknowns)
  const allPeople = [...data]
    .filter(p => !p.name.startsWith("User "))
    .sort((a, b) => a.name.localeCompare(b.name));

  const filtered = picFilter === "all" ? allPeople : allPeople.filter(p => p.pic_id === picFilter);
  const displayMonths = monthFilter === 0 ? MONTH_NAMES.map((_, i) => i + 1) : [monthFilter];

  // ── Per-person monthly scores for heatmap + trend ────────────────────────
  const peopleMonthly = filtered.map(p => {
    const monthScores = MONTH_NAMES.map((_, i) => {
      const m = i + 1;
      const row = p.months[String(m)] ?? p.months[m] ?? null;
      return computeScore(row);
    });
    const rows = Object.entries(p.months).filter(([k]) => !isNaN(Number(k))).map(([, v]) => v);
    const agg = aggregateRows(rows);
    return { name: p.name, monthScores, agg, score: computeScore(agg), open: p.open_workload, noDeadline: p.no_deadline_count ?? 0 };
  });

  // Score → heatmap bg color
  function heatColor(score) {
    if (score === null) return { bg: "#f9fafb", text: "#d1d5db" };
    if (score >= 9)   return { bg: "#dcfce7", text: "#15803d" };
    if (score >= 8)   return { bg: "#d1fae5", text: "#065f46" };
    if (score >= 7)   return { bg: "#dbeafe", text: "#1d4ed8" };
    if (score >= 5.5) return { bg: "#fef9c3", text: "#854d0e" };
    return { bg: "#fee2e2", text: "#b91c1c" };
  }

  // SVG trend line for one person
  function TrendLine({ scores }) {
    const W = 280, H = 56, PAD = 6;
    const valid = scores.map((s, i) => s !== null ? { i, s } : null).filter(Boolean);
    if (valid.length < 2) return <span className="text-[10px] text-gray-300 italic">no data</span>;
    const minS = 0, maxS = 10;
    const xOf = (i) => PAD + (i / 11) * (W - PAD * 2);
    const yOf = (s) => H - PAD - ((s - minS) / (maxS - minS)) * (H - PAD * 2);
    const pts = valid.map(v => `${xOf(v.i)},${yOf(v.s)}`).join(" ");
    // threshold lines
    const y9 = yOf(9), y8 = yOf(8), y7 = yOf(7), y55 = yOf(5.5);
    return (
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 56 }}>
        {/* threshold bands */}
        <rect x={0} y={y9}  width={W} height={y8 - y9}   fill="#dcfce7" opacity="0.5" />
        <rect x={0} y={y8}  width={W} height={y7 - y8}   fill="#d1fae5" opacity="0.4" />
        <rect x={0} y={y7}  width={W} height={y55 - y7}  fill="#dbeafe" opacity="0.4" />
        <rect x={0} y={y55} width={W} height={H - y55}   fill="#fee2e2" opacity="0.3" />
        {/* line */}
        <polyline points={pts} fill="none" stroke="#6366f1" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
        {/* dots */}
        {valid.map(v => (
          <circle key={v.i} cx={xOf(v.i)} cy={yOf(v.s)} r="3"
            fill={heatColor(v.s).bg} stroke={heatColor(v.s).text} strokeWidth="1.5" />
        ))}
        {/* month labels */}
        {MONTH_NAMES.map((m, i) => (
          <text key={m} x={xOf(i)} y={H - 1} textAnchor="middle" fontSize="7" fill="#9ca3af">{m}</text>
        ))}
      </svg>
    );
  }

  const SCORE_TOOLTIP = "Score = On-time rate ×0.35 + Cycle efficiency ×0.25 + Overdue ×0.25 + Reopen ×0.15 (scale 0–10). Reopen not yet tracked → fixed at 10.";

  return (
    <div className="space-y-5">

      {/* ── Filters — top ── */}
      <div className="flex flex-wrap items-center gap-4 bg-white border border-gray-200 rounded-xl px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500 font-medium">Person:</span>
          <div className="flex flex-wrap gap-1.5">
            <button onClick={() => setPicFilter("all")}
              className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${picFilter === "all" ? "bg-gray-900 text-white border-gray-900" : "bg-white text-gray-500 border-gray-200 hover:border-gray-400"}`}>
              All
            </button>
            {allPeople.map(p => (
              <button key={p.pic_id} onClick={() => setPicFilter(p.pic_id)}
                className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${picFilter === p.pic_id ? "bg-indigo-600 text-white border-indigo-600" : "bg-white text-gray-500 border-gray-200 hover:border-gray-400"}`}>
                {p.name}
              </button>
            ))}
          </div>
        </div>
        <div className="w-px h-5 bg-gray-200" />
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500 font-medium">Month:</span>
          <div className="flex flex-wrap gap-1.5">
            <button onClick={() => setMonthFilter(0)}
              className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${monthFilter === 0 ? "bg-gray-900 text-white border-gray-900" : "bg-white text-gray-500 border-gray-200 hover:border-gray-400"}`}>
              All
            </button>
            {MONTH_NAMES.map((m, i) => (
              <button key={m} onClick={() => setMonthFilter(i + 1)}
                className={`px-2.5 py-1 rounded-full text-xs font-medium border transition-colors ${monthFilter === i + 1 ? "bg-gray-900 text-white border-gray-900" : "bg-white text-gray-500 border-gray-200 hover:border-gray-400"}`}>
                {m}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* ── Scoring legend ── */}
      <div className="rounded-xl border border-gray-200 bg-white overflow-hidden text-xs">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 border-b border-gray-100">
          {/* Weights */}
          <div className="flex items-center gap-3">
            <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide">Weights</span>
            {[["On-time","35%"],["Cycle","25%"],["Overdue","25%"],["Reopen","15%"]].map(([label, pct]) => (
              <span key={label} className="text-[11px] text-gray-600">{label} <span className="font-semibold text-gray-800">{pct}</span></span>
            ))}
          </div>
          <div className="w-px h-4 bg-gray-200 hidden sm:block" />
          {/* Rating scale */}
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide">Rating</span>
            {[["≥ 9","Excellent","#15803d","#dcfce7"],["8–8.9","Good","#065f46","#d1fae5"],["7–7.9","Fair","#1d4ed8","#dbeafe"],["5.5–6.9","Pass","#854d0e","#fef9c3"],["< 5.5","Fail","#b91c1c","#fee2e2"]].map(([range, label, color, bg]) => (
              <span key={label} className="flex items-center gap-1 text-[11px]">
                <span className="px-1.5 py-0.5 rounded font-semibold text-[10px]" style={{ backgroundColor: bg, color }}>{range}</span>
                <span className="text-gray-500">{label}</span>
              </span>
            ))}
          </div>
        </div>
        <div className="px-4 py-2 text-[10px] text-gray-400">
          Period grouped by task <strong className="text-gray-500">Deadline</strong> month · Workload tracked only — not scored · Reopen not yet available in Lark
        </div>
      </div>

      {/* ── Summary scorecards ── */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        {peopleMonthly.map(d => {
          const rCls = ratingClasses(d.score);
          const onTimeRate = d.agg?.on_time_rate ?? 0;
          const ringColor = onTimeRate >= 90 ? "#22c55e" : onTimeRate >= 70 ? "#eab308" : "#ef4444";
          const radius = 28, circ = 2 * Math.PI * radius;
          const filled = Math.min(onTimeRate / 100, 1) * circ;
          return (
            <div key={d.name} className="bg-white rounded-xl border border-gray-200 p-3 flex flex-col items-center gap-2">
              <p className="text-xs font-semibold text-gray-700">{d.name}</p>
              <div className="relative w-16 h-16">
                <svg className="w-full h-full -rotate-90" viewBox="0 0 72 72">
                  <circle cx="36" cy="36" r={radius} fill="none" stroke="#e5e7eb" strokeWidth="7" />
                  {onTimeRate > 0 && (
                    <circle cx="36" cy="36" r={radius} fill="none" stroke={ringColor} strokeWidth="7"
                      strokeDasharray={`${filled} ${circ}`} strokeLinecap="round" />
                  )}
                </svg>
                <div className="absolute inset-0 flex flex-col items-center justify-center">
                  <span className="text-[11px] font-bold text-gray-800">{onTimeRate > 0 ? `${onTimeRate.toFixed(0)}%` : "—"}</span>
                </div>
              </div>
              <div className="text-[10px] text-gray-400">On-time rate</div>
              {d.score !== null && (
                <span className={`px-2 py-0.5 rounded-full text-[10px] ${rCls}`}>
                  {d.score.toFixed(1)} · {ratingLabel(d.score)}
                </span>
              )}
              <div className="text-[10px] text-gray-400">{d.agg?.total_tasks ?? 0} tasks · {d.open} open</div>
              {d.noDeadline > 0 && (
                <div title={`${d.noDeadline} open task(s) have no deadline set — please add a deadline in Lark so they appear in the monthly breakdown.`}
                  className="flex items-center gap-1 px-2 py-0.5 bg-red-50 border border-red-200 rounded-full text-[10px] text-red-600 font-medium cursor-help">
                  ⚠ {d.noDeadline} no deadline
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* ── Score heatmap: person × month ── */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 overflow-x-auto">
        <p className="text-xs font-semibold text-gray-600 mb-3">Performance Score — Heatmap</p>
        <table className="text-[11px] border-collapse w-full min-w-[520px]">
          <thead>
            <tr>
              <th className="text-left text-gray-400 font-medium pb-2 pr-3 w-16">Person</th>
              {MONTH_NAMES.map(m => (
                <th key={m} className="text-center text-gray-400 font-medium pb-2 px-0.5 w-10">{m}</th>
              ))}
              <th className="text-center text-gray-400 font-medium pb-2 pl-2">Annual</th>
            </tr>
          </thead>
          <tbody>
            {peopleMonthly.map(d => (
              <tr key={d.name}>
                <td className="pr-3 py-1 font-medium text-gray-700 whitespace-nowrap">{d.name}</td>
                {d.monthScores.map((score, i) => {
                  const { bg, text } = heatColor(score);
                  return (
                    <td key={i} className="px-0.5 py-1 text-center">
                      <div className="rounded text-[10px] font-semibold py-1 px-0.5" style={{ backgroundColor: bg, color: text, minWidth: 32 }}>
                        {score !== null ? score.toFixed(1) : "·"}
                      </div>
                    </td>
                  );
                })}
                <td className="pl-2 py-1 text-center">
                  {(() => { const { bg, text } = heatColor(d.score); return (
                    <div className="rounded text-[10px] font-bold py-1 px-1" style={{ backgroundColor: bg, color: text }}>
                      {d.score !== null ? d.score.toFixed(1) : "—"}
                    </div>
                  );})()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {/* legend */}
        <div className="flex items-center gap-3 mt-3 flex-wrap">
          {[["≥9 Excellent","#dcfce7","#15803d"],["≥8 Good","#d1fae5","#065f46"],["≥7 Fair","#dbeafe","#1d4ed8"],["≥5.5 Pass","#fef9c3","#854d0e"],["<5.5 Fail","#fee2e2","#b91c1c"],["No data","#f9fafb","#d1d5db"]].map(([label, bg, color]) => (
            <span key={label} className="flex items-center gap-1 text-[10px]">
              <span className="w-4 h-3 rounded inline-block" style={{ backgroundColor: bg }} />
              <span style={{ color }}>{label}</span>
            </span>
          ))}
        </div>
      </div>

      {/* ── Monthly score trend lines ── */}
      <div className="bg-white rounded-xl border border-gray-200 p-4">
        <p className="text-xs font-semibold text-gray-600 mb-3">Score Trend by Month</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {peopleMonthly.map(d => (
            <div key={d.name} className="border border-gray-100 rounded-lg p-3">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-medium text-gray-700">{d.name}</span>
                {d.score !== null && (
                  <span className={`px-1.5 py-0.5 rounded text-[10px] ${ratingClasses(d.score)}`}>
                    Avg {d.score.toFixed(1)}
                  </span>
                )}
              </div>
              <TrendLine scores={d.monthScores} />
            </div>
          ))}
        </div>
      </div>

      {/* ── Detail table ── */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
        <table className="min-w-full text-xs border-collapse">
          <thead>
            <tr className="bg-gray-50">
              <th className={thCls}>Person</th>
              <th className={thCls}>Month</th>
              <th className={thCls + " text-right"}>Total</th>
              <th className={thCls + " text-right"}>Done</th>
              <th className={thCls + " text-right"}>On-time rate</th>
              <th className={thCls + " text-right"}>Avg Cycle (days)</th>
              <th className={thCls + " text-right"}>Avg Estimate</th>
              <th className={thCls + " text-right"}>Cycle/Est.</th>
              <th className={thCls + " text-right"}>Overdue</th>
              <th className={thCls + " text-right cursor-help"} title={SCORE_TOOLTIP}>Score ⓘ</th>
              <th className={thCls}>Rating</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((person) => {
              const monthRows = displayMonths.map(m => ({
                month: m,
                data: person.months[String(m)] ?? person.months[m] ?? null,
              }));
              const hasData = monthRows.some(r => r.data !== null);
              if (!hasData && monthFilter !== 0) return null;

              const annualRows = Object.entries(person.months)
                .filter(([k]) => !isNaN(Number(k))).map(([, v]) => v);
              const annual = aggregateRows(annualRows);

              const rows = monthFilter === 0
                ? [...monthRows, { month: "Annual", data: annual }]
                : monthRows;

              return rows.map(({ month, data: row }, idx) => {
                const score = computeScore(row);
                const rCls = ratingClasses(score);
                const showName = idx === 0;
                const isAnnual = month === "Annual";
                const monthLabel = isAnnual ? "Annual" : MONTH_NAMES[month - 1];

                return (
                  <tr key={`${person.pic_id}-${month}`}
                    className={isAnnual ? "bg-gray-50 font-semibold" : "hover:bg-gray-50/50"}>
                    <td className={tdCls + " font-medium text-gray-900 min-w-[90px]"}>
                      {showName ? (
                        <div>
                          <div>{person.name}</div>
                          {person.open_workload > 0 && (
                            <div className="text-[10px] text-orange-500 font-normal mt-0.5">{person.open_workload} open</div>
                          )}
                        </div>
                      ) : ""}
                    </td>
                    <td className={tdCls + (isAnnual ? " text-gray-500 italic" : "")}>{monthLabel}</td>
                    <td className={tdCls + " text-right"}>{row ? row.total_tasks : "—"}</td>
                    <td className={tdCls + " text-right"}>{row ? `${row.completed} (${fmtNum(row.completion_rate)}%)` : "—"}</td>
                    <td className={tdCls + " text-right"}>
                      {row?.on_time_rate != null ? (
                        <span className={row.on_time_rate >= 90 ? "text-green-600 font-medium" : row.on_time_rate >= 70 ? "text-yellow-600" : "text-red-500"}>
                          {fmtNum(row.on_time_rate)}%
                        </span>
                      ) : "—"}
                    </td>
                    <td className={tdCls + " text-right"}>{fmtNum(row?.cycle_time_avg)}</td>
                    <td className={tdCls + " text-right"}>{fmtNum(row?.estimated_avg)}</td>
                    <td className={tdCls + " text-right"}>
                      {row?.cycle_ratio != null ? (
                        <span className={row.cycle_ratio <= 1 ? "text-green-600" : row.cycle_ratio <= 1.3 ? "text-yellow-600" : "text-red-500"}>
                          {fmtNum(row.cycle_ratio, 2)}×
                        </span>
                      ) : "—"}
                    </td>
                    <td className={tdCls + " text-right"}>
                      {row?.overdue_count != null ? (
                        <span className={row.overdue_count === 0 ? "text-green-600" : row.overdue_count <= 2 ? "text-yellow-600" : "text-red-500"}>
                          {row.overdue_count}
                        </span>
                      ) : "—"}
                    </td>
                    <td className={tdCls + " text-right font-semibold cursor-help"}
                      title={row ? `On-time: ${fmtNum(row.on_time_rate)}% ×0.35 | Cycle: ${fmtNum(row.cycle_ratio,2)}× ×0.25 | Overdue: ${row.overdue_count ?? 0} ×0.25 | Reopen: 10 ×0.15` : SCORE_TOOLTIP}>
                      {score ?? "—"}
                    </td>
                    <td className={tdCls}>
                      {score !== null ? (
                        <span className={`px-1.5 py-0.5 rounded text-[10px] ${rCls}`}>{ratingLabel(score)}</span>
                      ) : "—"}
                    </td>
                  </tr>
                );
              });
            })}
          </tbody>
        </table>
      </div>
      <p className="text-[10px] text-gray-400">
        Score = On-time rate ×0.35 + Cycle/Estimate ×0.25 + Overdue ×0.25 + Reopen ×0.15 (Reopen not yet tracked in Lark)
      </p>
    </div>
  );
}

export default function TeamKPI() {
  const { branches, selected, selectBranch } = useBranch();
  const [role, setRole] = useState("kol");
  const [year, setYear] = useState(CURRENT_YEAR);
  const queryClient = useQueryClient();

  const branchId = selected !== "all" ? selected : null;


  const isTaskOverview = role === "task_overview";

  const { data, isPending, isPlaceholderData, error } = useQuery({
    queryKey: ["team-kpi", role, year, branchId],
    queryFn: () => getTeamKpiSummary(role, year, branchId),
    placeholderData: keepPreviousData,
    enabled: !isTaskOverview,
  });

  const roleMeta = ROLES.find(r => r.key === role);
  const cur = data?.current_month;
  const curMonthLabel = cur ? MONTHS[cur - 1] : null;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Team KPI</h1>
          <p className="text-sm text-gray-400 mt-0.5">Monthly targets & actuals per team member</p>
        </div>
        <div className="flex items-center gap-3">
          {/* Year selector */}
          <div className="flex items-center gap-1 bg-white border border-gray-200 rounded-lg px-2 py-1.5 shadow-sm">
            <button
              onClick={() => setYear(y => y - 1)}
              className="text-gray-400 hover:text-gray-600 px-1"
            >‹</button>
            <span className="text-sm font-semibold text-gray-700 w-12 text-center">{year}</span>
            <button
              onClick={() => setYear(y => y + 1)}
              className="text-gray-400 hover:text-gray-600 px-1"
            >›</button>
          </div>
          <button
            onClick={() => queryClient.invalidateQueries({ queryKey: ["team-kpi"] })}
            title="Refresh"
            className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
          >
            <svg className={`w-4 h-4 ${isPending ? "animate-spin" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>
      </div>

      {/* Role tabs */}
      <div className="flex flex-wrap gap-2">
        {ROLES.map(r => (
          <button
            key={r.key}
            onClick={() => setRole(r.key)}
            className={`flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium transition-all border ${
              role === r.key
                ? "bg-gray-900 text-white border-gray-900 shadow-sm"
                : "bg-white text-gray-600 border-gray-200 hover:border-gray-400 hover:text-gray-900"
            }`}
          >
            <span>{r.emoji}</span>
            <span>{r.person}</span>
            <span className={`text-xs ${role === r.key ? "text-gray-400" : "text-gray-400"}`}>· {r.label}</span>
          </button>
        ))}
        <button
          onClick={() => setRole("task_overview")}
          className={`flex items-center gap-2 px-4 py-2 rounded-full text-sm font-medium transition-all border ${
            role === "task_overview"
              ? "bg-gray-900 text-white border-gray-900 shadow-sm"
              : "bg-white text-gray-600 border-gray-200 hover:border-gray-400 hover:text-gray-900"
          }`}
        >
          <span>📋</span>
          <span>Task Overview</span>
        </button>
      </div>

      {/* Branch selector — hidden on Task Overview */}
      <div className={`flex flex-wrap gap-1.5 ${isTaskOverview ? "hidden" : ""}`}>
        {branches?.map(b => (
          <button
            key={b.id}
            onClick={() => selectBranch(b.id)}
            className={`px-3 py-1 rounded-full text-xs font-medium transition-colors border ${
              selected === b.id
                ? "bg-gray-800 text-white border-gray-800"
                : "bg-white text-gray-500 border-gray-200 hover:border-gray-400"
            }`}
          >
            {b.name?.replace("MEANDER ", "")}
          </button>
        ))}
        <button
          onClick={() => selectBranch("all")}
          className={`px-3 py-1 rounded-full text-xs font-medium transition-colors border ${
            selected === "all"
              ? "bg-gray-800 text-white border-gray-800"
              : "bg-white text-gray-400 border-gray-200 hover:border-gray-400"
          }`}
        >
          All
        </button>
      </div>

      {/* Task Overview tab */}
      {role === "task_overview" && <TaskOverview year={year} />}

      {role !== "task_overview" && error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3">
          {error?.response?.data?.detail || error.message || "Failed to load"}
        </div>
      )}

      {/* Summary cards row */}
      {role !== "task_overview" && data && (
        <div className={"grid grid-cols-1 sm:grid-cols-2 gap-4 transition-opacity duration-150 " + (isPlaceholderData ? "opacity-40 pointer-events-none" : "")}>
          {/* This month ring */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5 flex items-center gap-6">
            <div className="flex flex-col items-center gap-1">
              <div className="relative">
                <AchievementRing
                  pct={data.current_month_pct}
                  label={curMonthLabel ? `${curMonthLabel} ${year}` : `${year}`}
                  sub="This month"
                />
              </div>
              <Tooltip text={`This month: average achievement % across all KPIs for ${curMonthLabel || "this month"} — only KPIs with both a target and an actual are included. Formula: avg(actual ÷ target × 100).`}>
                <span className="text-[10px] text-gray-400 underline decoration-dotted cursor-default">How it's calculated ⓘ</span>
              </Tooltip>
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-1 mb-3">
                <p className="text-xs text-gray-500">YTD Average</p>
                <Tooltip text="Average achievement % across all KPIs for past months — only months with a target set are counted. Example: if targets start from June, only June onward is included in YTD.">
                  <span className="text-[10px] text-gray-400 cursor-default">ⓘ</span>
                </Tooltip>
              </div>
              <div className="flex items-end gap-2">
                <span className={`text-3xl font-bold ${
                  data.overall_avg_pct !== null
                    ? (pctColor(data.overall_avg_pct)
                        ? COLOR_CLASSES[pctColor(data.overall_avg_pct)].text
                        : "text-gray-400")
                    : "text-gray-400"
                }`}>
                  {data.overall_avg_pct !== null ? `${data.overall_avg_pct}%` : "—"}
                </span>
                <span className="text-sm text-gray-400 mb-1">avg achieved</span>
              </div>
              <p className="text-xs text-gray-400 mt-2">
                {roleMeta?.emoji} {roleMeta?.person} · {roleMeta?.label}
                {!data.auto_actuals && (
                  <span className="ml-2 px-1.5 py-0.5 bg-blue-50 text-blue-500 rounded text-[10px] font-medium">Manual entry</span>
                )}
              </p>
            </div>
          </div>

          {/* YTD bars */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
            <div className="flex items-center gap-1 mb-3">
              <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider">YTD by KPI</p>
              <Tooltip text="Each bar = average achievement % for that KPI across past months with a target. Formula: avg(actual ÷ target × 100) — only months with both a target > 0 and an actual are counted.">
                <span className="text-[10px] text-gray-400 cursor-default normal-case font-normal">ⓘ</span>
              </Tooltip>
            </div>
            <YtdBars kpis={data.kpis || []} />
          </div>
        </div>
      )}

      {/* KPI grid */}
      {role !== "task_overview" && isPending && !data && (
        <div className="bg-white rounded-xl border border-gray-200 p-12 flex justify-center">
          <div className="w-6 h-6 border-2 border-gray-300 border-t-gray-800 rounded-full animate-spin" />
        </div>
      )}

      {role !== "task_overview" && data && (() => {
        const visibleKpis = (data.kpis || []).filter(k => branchId ? !k.org_wide : true);
        return (
        <div className={"space-y-2 transition-opacity duration-150 " + (isPlaceholderData ? "opacity-40 pointer-events-none" : "")}>
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-700">Monthly KPI Grid</h2>
            <div className="flex items-center gap-3 text-xs text-gray-400">
              <span className="flex items-center gap-1">
                <span className="inline-block w-3 h-3 rounded bg-yellow-100 border border-yellow-300" />
                T = Target (click to edit)
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block w-3 h-3 rounded bg-green-100 border border-green-300" />
                A = Actual (auto)
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block w-3 h-3 rounded bg-orange-100 border border-orange-300" />
                M = Manual (click to enter)
              </span>
            </div>
          </div>
          {visibleKpis.length === 0 ? (
            <div className="text-center py-8 text-sm text-gray-400">
              {branchId
                ? "No branch-specific KPIs for this role — select All to view."
                : "Select a branch to view KPIs for this role."}
            </div>
          ) : (
          <KpiGrid
            kpis={visibleKpis}
            roleKey={role}
            branchId={branchId}
            year={year}
            autoActuals={data.auto_actuals}
            onRefresh={() => queryClient.invalidateQueries({ queryKey: ["team-kpi"] })}
          />
          )}

          {!data.auto_actuals && (
            <div className="bg-blue-50 border border-blue-100 rounded-xl px-4 py-3 text-xs text-blue-600">
              <strong>Phase 2 coming:</strong> actuals for {roleMeta?.label} will be auto-synced from the data source.
              For now, click any <span className="font-semibold bg-green-100 text-green-700 px-1 rounded">A</span> cell to enter the actual manually.
            </div>
          )}
        </div>
        );
      })()}
    </div>
  );
}
