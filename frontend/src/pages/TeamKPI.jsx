import { useState, useRef, useEffect } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { useBranch } from "../context/BranchContext";
import { getTeamKpiSummary, upsertTarget, upsertActual } from "../api/teamKpi";

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
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-green-100 text-green-700">A</span>
                  </td>
                  {kpi.monthly.map(m => {
                    const color = m.has_target && m.pct !== null ? pctColor(m.pct, kpi.higher_is_better) : null;
                    const cls = color ? COLOR_CLASSES[color] : null;
                    const isSaving = saving === `${kpi.key}-${m.month}-actual`;

                    if ((autoActuals || kpi.auto_actuals) && kpi.auto !== false) {
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
                              {m.pct !== null && m.has_target && (
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
                            {m.pct !== null && m.actual !== null && (
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

export default function TeamKPI() {
  const { branches, selected, selectBranch } = useBranch();
  const [role, setRole] = useState("kol");
  const [year, setYear] = useState(CURRENT_YEAR);
  const queryClient = useQueryClient();

  const branchId = selected !== "all" ? selected : null;


  const { data, isPending, isPlaceholderData, error } = useQuery({
    queryKey: ["team-kpi", role, year, branchId],
    queryFn: () => getTeamKpiSummary(role, year, branchId),
    placeholderData: keepPreviousData,
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
      </div>

      {/* Branch selector */}
      <div className="flex flex-wrap gap-1.5">
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

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3">
          {error?.response?.data?.detail || error.message || "Failed to load"}
        </div>
      )}

      {/* Summary cards row */}
      {data && (
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
      {isPending && !data && (
        <div className="bg-white rounded-xl border border-gray-200 p-12 flex justify-center">
          <div className="w-6 h-6 border-2 border-gray-300 border-t-gray-800 rounded-full animate-spin" />
        </div>
      )}

      {data && (() => {
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
                A = Actual {data.auto_actuals ? "(auto)" : "(click to enter)"}
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
