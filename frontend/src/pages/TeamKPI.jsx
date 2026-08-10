import { useState, useRef, useEffect } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { useBranch } from "../context/BranchContext";
import { getTeamKpiSummary, upsertTarget, upsertActual, getTaskOverview, getTaskDetail } from "../api/teamKpi";

const ROLES = [
  { key: "kol",      label: "KOL",       person: "Mel",   emoji: "🤝" },
  { key: "paid_ads", label: "Paid Ads",  person: "Mason", emoji: "📢" },
  { key: "designer", label: "Designer",  person: "Nora",  emoji: "🎨" },
  { key: "crm",      label: "CRM",       person: "Kin",   emoji: "📊" },
  { key: "pm",       label: "PM",        person: "Nuha",  emoji: "🗂️" },
];

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const CURRENT_YEAR = new Date().getFullYear();
const CURRENT_MONTH = new Date().getMonth() + 1; // 1-indexed

// ── Color helpers ─────────────────────────────────────────────────────────────

// Every `pct` reaching this function is already direction-normalized by the
// backend (target/actual instead of actual/target for a lower-is-better
// KPI), so higher always means better here — no direction flag needed.
function pctColor(pct) {
  if (pct === null || pct === undefined) return null;
  if (pct >= 100) return "green";
  if (pct >= 80)  return "yellow";
  if (pct >= 60)  return "orange";
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
  delivery_rate:        "On-Time Delivery Rate — the same number as Nora's card in Task Overview. Formula: on-time ÷ scored × 100, where scored = completed tasks marked On-time or Late, grouped by Deadline month. Misses carrying a Late Reason are dropped from both sides, and tasks still open are not scored. Source: Lark Base, from Jul 2026.",
  task_completion_rate: "Team Task Completion Rate — all tasks across the team with a Deadline in that month, regardless of assignee. Formula: completed tasks ÷ total tasks due × 100. Source: Lark Base.",
  branch_kpi_rate:      "Branch KPI Achievement Rate — actual revenue (Cloudbeds or manual override, after deductions) ÷ target revenue × 100. Mirrors the Revenue KPI page formula.",
  budget_utilisation:   "Marketing Budget Utilisation — total actual spend (Paid Ads + KOL + CRM) ÷ total allocated budget × 100 for the branch and month.",
  roas:                 "Return on Ad Spend — Revenue from Paid Ads ÷ Ad Spend. In the All view, this is a weighted average: total revenue across all branches ÷ total spend across all branches.",
  crm_revenue:          "Revenue attributed to CRM campaigns in that month. Enter targets in mil VND (e.g. enter 56 for 56 million VND).",
  kol_posted:           "KOLs who published a post that month. Target is owned by KOL Engine → Set Targets → Posted: round(prev-month collaborated × Posted %). It moves as collaborations land, so it is read-only here.",
  kol_ads_collab:       "KOLs with ads permission who published that month (KOL Engine 'Ads-Allowed'). Target is owned by KOL Engine → Set Targets → Ads-Allowed: round(Posted target × Ads-Allowed %). Read-only here.",
  purchase_cvr:         "Website Purchase Conversion Rate — GA4's own 'User key event rate (purchase)' for the whole property, the number under Reports → Acquisition → User acquisition. Counts every purchase the property sees, not paid traffic only. Each month is its own GA4 query over that month's dates: unique users de-duplicate over time, so a month can never be built from daily data and YTD is a separate Jan-1 query, not a sum of the months. Purchases fire on the Cloudbeds booking-engine domain, so the rate can only be read property-wide.",
  ads_win_rate:         "% Ads Win — an ad wins a month when its ROAS that month beats the branch's blended CRTV ROAS for the same month, with enough data behind it (over 4,500 clicks or 5+ bookings; below that it stays TEST and counts on neither side). Each ad is judged once, ever. Formula: wins ÷ (wins + losses) among the ads decided that month. Meta only, and only ads named with CRTV. Source: Ads Platform.",
};

// "2026-08" → "Aug 2026". Months before a KPI's start date show blank, not 0.
function startLabel(starts) {
  if (!starts) return null;
  const [y, m] = String(starts).split("-").map(Number);
  return m >= 1 && m <= 12 ? `${MONTHS[m - 1]} ${y}` : starts;
}

// Hover text for a month that predates the KPI on this tab. When the start
// date is branch-specific there is a reason behind it worth carrying.
function startsTitle(kpi) {
  const base = `Tracked from ${startLabel(kpi.starts)}`;
  return kpi.starts_note ? `${base} — ${kpi.starts_note}` : base;
}

const PROVISIONAL_TITLE =
  "Provisional — the month is still syncing upstream. Losing verdicts only freeze once a month closes, so this rate is usually inflated.";

// Value as the KPI wants it read: a rate that means 1.63% shows both decimals
// and its own sign, while a count keeps the existing thousands formatting.
function fmtValue(kpi, value) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number") return String(value);
  const decimals = kpi.decimals ?? 0;
  const text = kpi.fixed_decimals
    ? value.toFixed(decimals)
    : value.toLocaleString(undefined, { maximumFractionDigits: decimals });
  // Any %-unit KPI gets its sign here even without an explicit value_suffix —
  // "100" next to a win-rate row reads as a count, not a rate.
  const suffix = kpi.value_suffix ?? (kpi.unit === "%" ? "%" : "");
  return suffix ? `${text}${suffix}` : text;
}

// ── Tooltip ───────────────────────────────────────────────────────────────────

const TOOLTIP_WIDTH = 224; // px, matches w-56

// Rendered into a portal at a fixed screen position rather than absolutely
// inside its trigger — the grid it usually sits in scrolls horizontally
// (overflow-x-auto), which clips an absolutely-positioned tooltip at the
// grid's edge no matter how high its z-index is.
function Tooltip({ text, children }) {
  const [pos, setPos] = useState(null); // null = hidden
  const anchorRef = useRef(null);

  const show = () => {
    const rect = anchorRef.current?.getBoundingClientRect();
    if (!rect) return;
    const left = Math.min(
      Math.max(rect.left + rect.width / 2, TOOLTIP_WIDTH / 2 + 8),
      window.innerWidth - TOOLTIP_WIDTH / 2 - 8
    );
    setPos({ top: rect.top, left, arrowLeft: rect.left + rect.width / 2 - left });
  };

  return (
    <span ref={anchorRef} className="relative inline-flex items-center" onMouseEnter={show} onMouseLeave={() => setPos(null)}>
      {children}
      {pos && createPortal(
        <span
          className="fixed z-50 text-[11px] leading-relaxed bg-gray-900 text-white rounded-lg px-2.5 py-2 shadow-lg pointer-events-none whitespace-normal text-center"
          style={{ top: pos.top, left: pos.left, width: TOOLTIP_WIDTH, transform: "translate(-50%, calc(-100% - 6px))" }}
        >
          {text}
          <span
            className="absolute top-full border-4 border-transparent border-t-gray-900"
            style={{ left: `calc(50% + ${pos.arrowLeft}px)`, transform: "translateX(-50%)" }}
          />
        </span>,
        document.body
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
      // m.pct is already direction-normalized by the backend (higher = better).
      const actuals = k.monthly.filter(m => !m.is_future && m.pct !== null);
      if (!actuals.length) return null;
      const avgAchievement = actuals.reduce((s, m) => s + m.pct, 0) / actuals.length;
      // For a KPI whose own unit is already "%" (e.g. a win rate), showing
      // "achievement % of a % target" compounds two percentages into a
      // number that no longer reads as either one (a 20% target hit at
      // 100% actual becomes "500%"). Show the metric's own average value
      // instead — same number the Actual cells above it already display —
      // but keep coloring it by achievement so it still signals over/under.
      const isPctUnit = k.unit === "%";
      const withActual = isPctUnit ? k.monthly.filter(m => !m.is_future && m.pct !== null && m.actual !== null) : actuals;
      const displayAvg = isPctUnit
        ? withActual.reduce((s, m) => s + m.actual, 0) / withActual.length
        : avgAchievement;
      return { label: k.label, pct: Math.round(displayAvg * 10) / 10, color: pctColor(avgAchievement) };
    })
    .filter(Boolean);

  if (!withPct.length) return <p className="text-xs text-gray-400 mt-2">No data yet</p>;

  return (
    <div className="space-y-3">
      {withPct.map(({ label, pct, color }) => {
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
      title={disabled ? "Locked (past month)" : "Click to edit"}
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
  const [saveError, setSaveError] = useState(null);

  const saveTarget = async (kpiKey, month, value) => {
    const key = `${kpiKey}-${month}-target`;
    setSaving(key);
    setSaveError(null);
    try {
      await upsertTarget({ role_key: roleKey, branch_id: branchId || null, year, month, kpi_key: kpiKey, target_value: value });
      await onRefresh();
    } catch (e) {
      console.error("save target failed", e);
      setSaveError(e?.response?.data?.detail || e?.response?.data?.error || e?.message || "Save failed");
    } finally {
      setSaving(null);
    }
  };

  const saveActual = async (kpiKey, month, value) => {
    const key = `${kpiKey}-${month}-actual`;
    setSaving(key);
    setSaveError(null);
    try {
      await upsertActual({ role_key: roleKey, branch_id: branchId || null, year, month, kpi_key: kpiKey, actual_value: value });
      await onRefresh();
    } catch (e) {
      console.error("save actual failed", e);
      setSaveError(e?.response?.data?.detail || e?.response?.data?.error || e?.message || "Save failed");
    } finally {
      setSaving(null);
    }
  };

  if (!kpis?.length) return null;

  return (
    <div>
    {saveError && (
      <div className="mb-2 px-3 py-2 text-xs bg-red-50 border border-red-200 text-red-700 rounded-lg">
        Save failed: {saveError}
      </div>
    )}
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
            <th className="px-2 py-2.5 font-semibold text-gray-700 text-center w-16" title="Average achievement % — only months with a target set are counted. For %-unit KPIs this shows the average actual value instead (achievement % of a % target reads as a meaningless number), colored by achievement.">Avg % ⓘ</th>
          </tr>
        </thead>
        <tbody>
          {kpis.map((kpi, ki) => {
            // YTD and Avg% only count months that have a target set
            const targetedMonths = kpi.monthly.filter(m => !m.is_future && m.has_target);
            // A rate whose months cannot be added together carries its own
            // year-to-date reading from the source; it stays blank when that
            // reading is missing rather than falling back to a meaningless sum.
            const ytdIsQuery = kpi.ytd_mode === "query";
            // ROAS is a ratio too — summing ×2.17 + ×1.05 + … across months
            // produces a meaningless number. Its YTD is server-computed as
            // Σrevenue ÷ Σspend instead (see team_kpi_service.py).
            const ytdIsRatio = kpi.ytd_mode === "ratio";
            const ytdSum = targetedMonths.filter(m => m.actual !== null).reduce((s, m) => s + m.actual, 0);
            const ytdActual = (ytdIsQuery || ytdIsRatio) ? kpi.ytd_actual : (ytdSum > 0 ? ytdSum : null);
            // m.pct is already direction-normalized by the backend (higher =
            // better for every KPI), so this is a plain average.
            const actPcts = targetedMonths.filter(m => m.pct !== null).map(m => m.pct);
            const avgAchievement = actPcts.length ? Math.round(actPcts.reduce((a, b) => a + b, 0) / actPcts.length * 10) / 10 : null;
            // For a %-unit KPI, "achievement % of a % target" compounds two
            // percentages (a 20% target hit at 100% actual reads as "500%").
            // Show the average of the actual values instead — same number
            // the monthly cells above already display — but keep the color
            // tied to achievement so it still signals over/under target.
            const isPctUnit = kpi.unit === "%";
            const avgActuals = targetedMonths.filter(m => m.actual !== null).map(m => m.actual);
            const avgActualPct = avgActuals.length ? Math.round(avgActuals.reduce((a, b) => a + b, 0) / avgActuals.length * 10) / 10 : null;
            const avgPct = isPctUnit ? avgActualPct : avgAchievement;
            const avgColor = pctColor(avgAchievement);

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
                      <span className="text-[10px] text-gray-400 font-normal">
                        {kpi.computed_target_note || "= spend × ROAS target"}
                      </span>
                    )}
                    {kpi.starts && (
                      <span className="block text-[10px] text-gray-400 font-normal">
                        {kpi.starts_note ? (
                          <Tooltip text={kpi.starts_note}>
                            <span className="cursor-help">from {startLabel(kpi.starts)} ⓘ</span>
                          </Tooltip>
                        ) : `from ${startLabel(kpi.starts)}`}
                      </span>
                    )}
                    {kpi.view_note && (
                      <Tooltip text={kpi.view_note.text}>
                        <span className="text-[10px] text-amber-600 font-normal cursor-help">
                          {kpi.view_note.label} ⓘ
                        </span>
                      </Tooltip>
                    )}
                  </td>
                  <td className="px-2 py-1.5 text-center text-gray-400" rowSpan={2}>{kpi.unit}</td>
                  <td className="px-2 py-1.5 text-center">
                    <span
                      className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-yellow-100 text-yellow-700"
                      title={kpi.computed_target
                        ? `Computed ${kpi.computed_target_note || "= spend × ROAS target"} — read-only`
                        : undefined}
                    >T</span>
                  </td>
                  {kpi.monthly.map(m => (
                    <td key={m.month} className="px-1 py-1.5 bg-yellow-50">
                      {m.not_started ? (
                        <div className="text-center text-gray-300" title={startsTitle(kpi)}>·</div>
                      ) : kpi.computed_target ? (
                        <div className="text-center text-gray-600 font-medium text-xs">
                          {m.target !== null && m.target !== undefined
                            ? m.target.toLocaleString(undefined, { maximumFractionDigits: kpi.decimals ?? 1 })
                            : <span className="text-gray-300">—</span>}
                        </div>
                      ) : (
                        <EditableCell
                          value={m.target}
                          disabled={year < CURRENT_YEAR || (year === CURRENT_YEAR && m.month < CURRENT_MONTH)}
                          onSave={val => saveTarget(kpi.key, m.month, val)}
                          placeholder="—"
                        />
                      )}
                    </td>
                  ))}
                  <td className="px-2 py-1.5 text-center text-gray-400 font-medium">
                    {/* Same rule as the actual row: monthly targets for a rate
                        are not addable. "ratio" KPIs (ROAS) get a real
                        server-computed YTD target instead of a blank cell;
                        "query" KPIs (GA4 rates) have none to show. */}
                    {ytdIsRatio
                      ? (kpi.ytd_target !== null && kpi.ytd_target !== undefined
                          ? kpi.ytd_target.toLocaleString(undefined, { maximumFractionDigits: kpi.decimals ?? 1 })
                          : "—")
                      : !ytdIsQuery && kpi.monthly.filter(m => m.target).reduce((s, m) => s + (m.target || 0), 0) > 0
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
                    // m.pct is already direction-normalized by the backend
                    // (higher = better), so no higher_is_better here.
                    const color = m.has_target && m.pct !== null ? pctColor(m.pct) : null;
                    const cls = color ? COLOR_CLASSES[color] : null;
                    const isSaving = saving === `${kpi.key}-${m.month}-actual`;

                    // Before the KPI existed — blank and locked, never a 0.
                    if (m.not_started) {
                      return (
                        <td key={m.month} className="px-1 py-1.5 text-center bg-gray-50"
                            title={startsTitle(kpi)}>
                          <span className="text-gray-300">·</span>
                        </td>
                      );
                    }

                    if ((autoActuals || kpi.auto_actuals) && kpi.auto_actuals !== false) {
                      // Read-only actual from API
                      return (
                        <td key={m.month} title={
                            kpi.view_note ? kpi.view_note.text
                            : !m.has_target && m.actual !== null ? "No target set — excluded from YTD & Avg%"
                            : undefined}
                          className={`px-1 py-1.5 text-center ${m.is_future ? "opacity-30" : ""} ${cls ? cls.bg : ""}`}>
                          {m.actual !== null ? (
                            <div className="flex flex-col items-center gap-0.5">
                              <span className={`font-semibold ${cls ? cls.text : "text-gray-400"}`}>
                                {fmtValue(kpi, m.actual)}
                                {m.provisional && (
                                  <span className="text-gray-400 font-normal cursor-help"
                                        title={kpi.provisional_note || PROVISIONAL_TITLE}>*</span>
                                )}
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
                              disabled={year < CURRENT_YEAR || (year === CURRENT_YEAR && m.month < CURRENT_MONTH)}
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
                  <td className="px-2 py-1.5 text-center"
                      title={
                        ytdIsQuery ? "Year-to-date is its own query over Jan 1 → today — these months cannot be added together"
                        : ytdIsRatio ? "Year-to-date ROAS = total revenue ÷ total spend (VND) across the included months — not a sum of monthly ratios"
                        : undefined
                      }>
                    {ytdActual !== null && ytdActual !== undefined ? (
                      <span className="font-semibold text-gray-700">{fmtValue(kpi, ytdActual)}</span>
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

// Missed deadlines, reported as one number. Late = finished after the
// deadline; Overdue = still unfinished with the deadline gone. Neither
// includes misses carrying a Late Reason — those sit in Excused.
const missedCount = (row) => (row?.late_count ?? 0) + (row?.overdue_count ?? 0);

// Nominal weight of each scoring component.
const SCORE_WEIGHTS = { ontime: 0.35, cycle: 0.25, overdue: 0.25, reopen: 0.15 };

// A component Lark has no data for is dropped and its weight spread over the
// components that do have data. Scoring a blank field as a perfect 10 handed
// everybody free points — Reopen Count is unfilled on nearly every row, so
// 15% of the score was a gift, and Cycle Time is missing often enough to
// matter too. Effective weights are returned so the tooltips can show what
// actually applied instead of the nominal 0.35 / 0.25 / 0.25 / 0.15.
function computeScoreDetail(row) {
  if (!row) return null;
  const on_time_rate = row.on_time_rate ?? null;  // null = nothing scored yet
  const cycle_ratio  = row.cycle_ratio ?? null;
  // Overdue and Late are one figure: every blown deadline without a Late
  // Reason, whether or not the task was eventually finished. Only meaningful
  // once the person has at least one task in the period.
  const overdue      = (row.total_tasks ?? 0) > 0 ? missedCount(row) : null;
  // reopen_filled counts records that actually carried a Reopen Count, so a 0
  // here means "field never filled", not "no reopens".
  const reopen       = (row.reopen_filled ?? 0) > 0 ? (row.reopen_count ?? 0) : null;

  const s_ontime  = on_time_rate == null ? null : on_time_rate >= 90 ? 10 : on_time_rate >= 80 ? 8 : on_time_rate >= 70 ? 6 : 4;
  const s_cycle   = cycle_ratio  == null ? null : cycle_ratio <= 1 ? 10 : cycle_ratio <= 1.2 ? 8 : cycle_ratio <= 1.5 ? 6 : 4;
  const s_overdue = overdue      == null ? null : overdue === 0 ? 10 : overdue <= 1 ? 8 : overdue <= 3 ? 6 : 4;
  const s_reopen  = reopen       == null ? null : reopen === 0 ? 10 : reopen === 1 ? 8 : reopen <= 3 ? 6 : 4;

  const scored = { ontime: s_ontime, cycle: s_cycle, overdue: s_overdue, reopen: s_reopen };
  const live = Object.keys(SCORE_WEIGHTS).filter(k => scored[k] !== null);
  const totalW = live.reduce((a, k) => a + SCORE_WEIGHTS[k], 0);

  const weights = { ontime: null, cycle: null, overdue: null, reopen: null };
  let score = null;
  if (totalW > 0) {
    let sum = 0;
    for (const k of live) {
      const w = SCORE_WEIGHTS[k] / totalW;
      weights[k] = w;
      sum += scored[k] * w;
    }
    score = Math.round(sum * 10) / 10;
  }

  return { score, on_time_rate, cycle_ratio, overdue, reopen, s_ontime, s_cycle, s_overdue, s_reopen, weights };
}

// Effective weight for a tooltip: "×0.41", or "—" when the component was
// dropped for lack of data.
const fmtWeight = (w) => (w == null ? "— (no data)" : `×${w.toFixed(2)}`);

function computeScore(row) {
  return computeScoreDetail(row)?.score ?? null;
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
  const totals = { total_tasks: 0, open_count: 0, completed: 0, on_time_count: 0, on_time_filled: 0,
                   late_count: 0, overdue_count: 0, late_excused_count: 0, overdue_excused_count: 0,
                   reopen_count: 0, reopen_filled: 0 };
  const reason_counts = {};
  let ct_sum = 0, ct_n = 0, ed_sum = 0, ed_n = 0;
  for (const r of rows) {
    totals.total_tasks    += r.total_tasks    ?? 0;
    totals.open_count     += r.open_count     ?? 0;
    totals.completed      += r.completed      ?? 0;
    totals.on_time_count  += r.on_time_count  ?? 0;
    totals.on_time_filled += r.on_time_filled ?? 0;
    totals.late_count     += r.late_count     ?? 0;
    totals.overdue_count  += r.overdue_count  ?? 0;
    totals.reopen_count   += r.reopen_count   ?? 0;
    totals.reopen_filled  += r.reopen_filled  ?? 0;
    totals.late_excused_count    += r.late_excused_count    ?? 0;
    totals.overdue_excused_count += r.overdue_excused_count ?? 0;
    for (const [k, v] of Object.entries(r.reason_counts ?? {})) {
      reason_counts[k] = (reason_counts[k] ?? 0) + v;
    }
    if (r.cycle_time_avg != null) { ct_sum += r.cycle_time_avg; ct_n++; }
    if (r.estimated_avg  != null) { ed_sum += r.estimated_avg;  ed_n++; }
  }
  const cycle_time_avg = ct_n ? Math.round(ct_sum / ct_n * 10) / 10 : null;
  const estimated_avg  = ed_n ? Math.round(ed_sum / ed_n * 10) / 10 : null;
  const cycle_ratio    = (cycle_time_avg && estimated_avg) ? Math.round(cycle_time_avg / estimated_avg * 1000) / 1000 : null;
  const completion_rate = totals.total_tasks > 0 ? Math.round(totals.completed / totals.total_tasks * 100 * 10) / 10 : null;
  const on_time_rate    = totals.on_time_filled > 0 ? Math.round(totals.on_time_count / totals.on_time_filled * 100 * 10) / 10 : null;
  return { ...totals, reason_counts, cycle_time_avg, estimated_avg, cycle_ratio, completion_rate, on_time_rate };
}

const CATEGORY_LABELS = {
  total: "All tasks",
  done: "Completed tasks",
  on_time: "On-time tasks",
  late: "Late tasks",
  overdue: "Overdue tasks",
  missed: "Missed deadlines (overdue + late)",
  excused: "Excused misses (not counted)",
  no_deadline: "Open tasks with no deadline",
  open: "Open tasks — not Completed yet",
};

// Reason keys arrive lower-cased and slash-collapsed from the backend. Any
// Late Reason excuses a miss, so options can be added in Lark without touching
// this — just title-case whatever comes back.
const prettyReason = (key) =>
  String(key).replace(/\//g, " / ").replace(/^./, (c) => c.toUpperCase());

// The four components, the metric behind each one, and the value that earns
// each band. Drives the formula modal so the bands cannot drift from
// computeScoreDetail without the explanation drifting with them.
const SCORE_COMPONENTS = [
  { key: "ontime",  name: "On-time rate",     metric: "on-time ÷ scored tasks",              bands: ["≥ 90%", "≥ 80%", "≥ 70%", "< 70%"] },
  { key: "cycle",   name: "Cycle efficiency", metric: "avg Cycle Time ÷ avg Estimated Days", bands: ["≤ 1.0", "≤ 1.2", "≤ 1.5", "> 1.5"] },
  { key: "overdue", name: "Overdue / Late",   metric: "blown deadlines, no Late Reason",     bands: ["0", "1", "≤ 3", "> 3"] },
  { key: "reopen",  name: "Reopen",           metric: "sum of Reopen Count",                 bands: ["0", "1", "≤ 3", "> 3"] },
];

// The full "how is this number made" panel. The header strip and the per-cell
// tooltips only have room for the weights; the part people actually get stuck
// on — which raw number earns which band, and what happens when Lark has no
// data for a component — needs the space of a modal.
function ScoreFormulaModal({ onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const Step = ({ n, title, children }) => (
    <div className="flex gap-3">
      <span className="flex-shrink-0 w-5 h-5 rounded-full bg-gray-900 text-white text-[10px] font-semibold flex items-center justify-center mt-0.5">{n}</span>
      <div className="min-w-0 flex-1">
        <p className="text-xs font-semibold text-gray-800 mb-1.5">{title}</p>
        {children}
      </div>
    </div>
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-xl w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between px-5 py-3.5 border-b border-gray-100">
          <div>
            <div className="text-sm font-semibold text-gray-900">How the Performance Score is calculated</div>
            <div className="text-[11px] text-gray-500 mt-0.5">Scored per month, on a 0–10 scale</div>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-lg leading-none">✕</button>
        </div>

        <div className="px-5 py-4 overflow-y-auto space-y-5">

          <Step n="1" title="Group the tasks by Deadline month">
            <p className="text-[11px] text-gray-600 leading-5">
              A task belongs to the month of its <b>Deadline</b>, not the month it was created or finished.
              Months before July are excluded (the base was standardized then), and tasks with status
              <b> Upcoming Tasks</b>, <b>Regular task</b> or <b>Blocked task</b> never enter the KPI at all.
            </p>
          </Step>

          <Step n="2" title="Turn four raw numbers into 0–10">
            <div className="overflow-x-auto">
              <table className="w-full text-[11px] border-collapse">
                <thead>
                  <tr className="text-gray-400 text-[10px] uppercase tracking-wide">
                    <th className="text-left font-medium pb-1.5 pr-3">Component</th>
                    <th className="text-left font-medium pb-1.5 pr-3">Raw metric</th>
                    <th className="text-center font-medium pb-1.5 px-1.5">10</th>
                    <th className="text-center font-medium pb-1.5 px-1.5">8</th>
                    <th className="text-center font-medium pb-1.5 px-1.5">6</th>
                    <th className="text-center font-medium pb-1.5 px-1.5">4</th>
                    <th className="text-right font-medium pb-1.5 pl-2">Weight</th>
                  </tr>
                </thead>
                <tbody>
                  {SCORE_COMPONENTS.map(c => (
                    <tr key={c.key} className="border-t border-gray-100">
                      <td className="py-1.5 pr-3 font-medium text-gray-800 whitespace-nowrap">{c.name}</td>
                      <td className="py-1.5 pr-3 text-gray-500">{c.metric}</td>
                      {c.bands.map((b, i) => (
                        <td key={i} className="py-1.5 px-1.5 text-center text-gray-700 whitespace-nowrap"
                            style={{ backgroundColor: ["#f0fdf4", "#f7fee7", "#fefce8", "#fef2f2"][i] }}>
                          {b}
                        </td>
                      ))}
                      <td className="py-1.5 pl-2 text-right font-semibold text-gray-800">
                        {Math.round(SCORE_WEIGHTS[c.key] * 100)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ul className="mt-2 space-y-1 text-[11px] text-gray-500 leading-5 list-disc pl-4">
              <li><b>Scored</b> = completed tasks marked On-time or Late. Tasks still open are not scored.</li>
              <li>A miss carrying a <b>Late Reason</b> is excused — dropped from the on-time rate (top and bottom) and from Overdue/Late.</li>
              <li><b>Overdue</b> = still open with the deadline past (checked by day). <b>Late</b> = finished after the deadline. One figure covers both.</li>
              <li>Cycle Time / Estimated Days outside 0–365 days are ignored as data-entry errors.</li>
            </ul>
          </Step>

          <Step n="3" title="Drop what Lark has no data for, share its weight out">
            <p className="text-[11px] text-gray-600 leading-5">
              A blank field used to score a perfect 10 — a free 15% for everyone, since Reopen Count is
              unfilled on nearly every row. A component with no data is now dropped and the remaining
              weights are rescaled: <code className="bg-gray-100 px-1 rounded">effective = weight ÷ sum of weights that have data</code>.
            </p>
            <div className="mt-2 bg-gray-50 rounded-lg px-3 py-2 text-[11px] text-gray-600 leading-5">
              No Reopen data → live weights are 0.35 + 0.25 + 0.25 = <b>0.85</b>, so On-time becomes
              0.35 ÷ 0.85 = <b>×0.41</b>, Cycle and Overdue/Late <b>×0.29</b> each.
              Hover a heatmap cell to see the weights that actually applied that month.
            </div>
          </Step>

          <Step n="4" title="Add it up — that is the month's score">
            <div className="bg-gray-900 rounded-lg px-3 py-2.5 font-mono text-[11px] text-gray-100 leading-6 overflow-x-auto">
              <div className="text-gray-400 text-[10px] mb-1">Example — a month with all four components filled</div>
              <div>On-time 100%<span className="text-gray-500"> → </span>10 × 0.35 <span className="text-gray-500">=</span> 3.50</div>
              <div>Cycle 0.71<span className="text-gray-500"> → </span>10 × 0.25 <span className="text-gray-500">=</span> 2.50</div>
              <div>Overdue/Late 0<span className="text-gray-500"> → </span>10 × 0.25 <span className="text-gray-500">=</span> 2.50</div>
              <div>Reopen 0<span className="text-gray-500"> → </span>10 × 0.15 <span className="text-gray-500">=</span> 1.50</div>
              <div className="border-t border-gray-700 mt-1 pt-1">Score<span className="text-gray-500"> = </span><b>10.0</b> · Excellent</div>
            </div>
          </Step>

          <Step n="5" title="The card averages the months in the period">
            <p className="text-[11px] text-gray-600 leading-5">
              With Month = All, the score on a card is the <b>average of that person's monthly scores</b>,
              not a recount of the whole period — which is why it need not line up with the on-time ring
              above it (the ring adds the counts up). The Annual column averages every month that has a score.
            </p>
          </Step>

          <div className="pt-1 border-t border-gray-100">
            <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide mb-1.5">Rating</p>
            <div className="flex items-center gap-2 flex-wrap">
              {[["≥ 9","Excellent","#15803d","#dcfce7"],["8–8.9","Good","#065f46","#d1fae5"],["7–7.9","Fair","#1d4ed8","#dbeafe"],["5.5–6.9","Pass","#854d0e","#fef9c3"],["< 5.5","Fail","#b91c1c","#fee2e2"]].map(([range, label, color, bg]) => (
                <span key={label} className="flex items-center gap-1 text-[11px]">
                  <span className="px-1.5 py-0.5 rounded font-semibold text-[10px]" style={{ backgroundColor: bg, color }}>{range}</span>
                  <span className="text-gray-500">{label}</span>
                </span>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// "7 tasks" / "2 open" on a scorecard, clickable straight through to the list
// behind the number. The drilldown carries the same period the card aggregated
// — one month when the Month filter picks one, the whole tracked window when it
// is All — so the list can be counted against the figure it came from.
function CountLink({ n, label, category, picName, year, monthFilter, periodLabel, onOpen }) {
  if (!n) return <>{n} {label}</>;
  return (
    <button
      type="button"
      onClick={() => onOpen({
        picName, year,
        month: monthFilter === 0 ? null : monthFilter,
        category,
      })}
      title={`Click to list the ${n} ${label} — ${periodLabel}`}
      className="text-gray-500 underline decoration-dotted hover:text-indigo-600 cursor-pointer"
    >
      {n} {label}
    </button>
  );
}

function TaskDetailModal({ drilldown, tasks, isLoading, onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // No month = the whole tracked window. Spelled out in the header so a list
  // spanning several months is never mistaken for one month's worth.
  const monthName =
    drilldown.month != null ? `${MONTH_NAMES[drilldown.month - 1]} ${drilldown.year}`
    : drilldown.category === "no_deadline" ? null
    : `${drilldown.year} (from Jul)`;
  const catLabel = CATEGORY_LABELS[drilldown.category] ?? drilldown.category;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-xl w-full max-w-sm mx-4 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
          <div>
            <div className="text-sm font-semibold text-gray-900">
              {drilldown.picName}{monthName ? ` — ${monthName}` : ""}
            </div>
            <div className="text-[11px] text-gray-500 mt-0.5">{catLabel}</div>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-lg leading-none">✕</button>
        </div>
        <div className="px-4 py-3 max-h-80 overflow-y-auto">
          {isLoading ? (
            <div className="flex justify-center py-6">
              <div className="w-5 h-5 border-2 border-gray-300 border-t-gray-700 rounded-full animate-spin" />
            </div>
          ) : !tasks?.length ? (
            <p className="text-sm text-gray-400 text-center py-4">No tasks found.</p>
          ) : (
            <ul className="space-y-2">
              {tasks.map((t, i) => (
                <li key={i} className="flex items-start gap-2 text-xs text-gray-700">
                  <span className={`mt-1 w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                    t.excused ? "bg-blue-400" :
                    t.on_time === "on_time" ? "bg-green-500" :
                    t.on_time === "late" ? "bg-red-400" :
                    t.status === "open" ? "bg-orange-400" : "bg-gray-400"
                  }`} />
                  <span className="min-w-0">
                    {t.lark_url ? (
                      <a href={t.lark_url} target="_blank" rel="noreferrer"
                         className="hover:underline hover:text-blue-600" title="Open in Lark">
                        {t.name} <span className="text-gray-300">↗</span>
                      </a>
                    ) : t.name}
                    {t.deadline ? (
                      <span className="text-gray-400 ml-1.5">· due {t.deadline}</span>
                    ) : t.lark_status ? (
                      <span className="text-gray-400 ml-1.5">· {t.lark_status}</span>
                    ) : null}
                    {t.late_reason && (
                      <span className={"block mt-0.5 text-[10px] " + (t.excused ? "text-blue-600" : "text-gray-500")}>
                        {t.late_reason}{t.excused ? " · not counted" : ""}
                      </span>
                    )}
                    {t.late_note && (
                      <span className="block text-[10px] text-gray-400 italic">{t.late_note}</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="px-4 py-2 bg-gray-50 text-[10px] text-gray-400 border-t border-gray-100">
          {!isLoading && tasks?.length ? `${tasks.length} task${tasks.length !== 1 ? "s" : ""}` : ""}
        </div>
      </div>
    </div>
  );
}

function TaskOverview({ year }) {
  const [monthFilter, setMonthFilter] = useState(0); // 0 = All, 1–12 = specific month
  const [picFilter, setPicFilter] = useState("all"); // "all" or pic_id
  const [drilldown, setDrilldown] = useState(null); // { picName, year, month, category }
  const [showFormula, setShowFormula] = useState(false);

  const { data, isPending, error } = useQuery({
    queryKey: ["task-overview", year],
    queryFn: () => getTaskOverview(year),
  });

  const { data: drillTasks, isPending: drillLoading } = useQuery({
    queryKey: ["task-detail", drilldown?.picName, drilldown?.year, drilldown?.month, drilldown?.category],
    queryFn: () => getTaskDetail(drilldown.picName, drilldown.year, drilldown.month, drilldown.category),
    enabled: !!drilldown,
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

  const avgScore = (scores) => {
    const valid = scores.filter(s => s !== null && s !== undefined);
    return valid.length ? Math.round(valid.reduce((a, b) => a + b, 0) / valid.length * 10) / 10 : null;
  };

  // ── Per-person monthly scores for heatmap + trend ────────────────────────
  // monthDetails / monthScores stay full-year: the heatmap and trend below are
  // month-by-month views and would be pointless filtered down to one column.
  // agg and score follow the Month filter, because the scorecards above were
  // silently showing the whole year no matter which month was picked.
  const peopleMonthly = filtered.map(p => {
    const monthRow = (m) => p.months[String(m)] ?? p.months[m] ?? null;
    const monthDetails = MONTH_NAMES.map((_, i) => {
      const m = i + 1;
      if (year === CURRENT_YEAR && m > CURRENT_MONTH) return null;
      const det = computeScoreDetail(monthRow(m));
      // A month with no scoreable component at all is "no data", not a zero.
      return det && det.score !== null ? det : null;
    });
    const monthScores = monthDetails.map(d => d?.score ?? null);
    const agg = aggregateRows(displayMonths.map(monthRow).filter(Boolean));
    return {
      name: p.name, monthScores, monthDetails, agg,
      score: avgScore(displayMonths.map(m => monthScores[m - 1])),
      annualScore: avgScore(monthScores),
      open: agg?.open_count ?? 0,
      noDeadline: p.no_deadline_count ?? 0,
      excluded: p.excluded_status_count ?? 0,
    };
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

  const SCORE_TOOLTIP = "Score = On-time rate ×0.35 + Cycle efficiency ×0.25 + Overdue/Late ×0.25 + Reopen ×0.15 (scale 0–10). Overdue/Late counts every blown deadline, finished or not. Reopen: 0→10, 1→8, ≤3→6, else→4.\n\nA component Lark has no data for is dropped and its weight shared out over the rest — a blank field no longer scores a free 10. Hover a month cell to see the weights that actually applied.";

  const periodLabel = monthFilter === 0 ? `${year} (from Jul)` : `${MONTH_NAMES[monthFilter - 1]} ${year}`;

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
            {[["On-time","35%"],["Cycle","25%"],["Overdue/Late","25%"],["Reopen","15%"]].map(([label, pct]) => (
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
        <div className="px-4 py-2 text-[10px] text-gray-400 flex flex-wrap items-center gap-x-1 gap-y-1">
          <span>
            Period grouped by task <strong className="text-gray-500">Deadline</strong> month · Cards show <strong className="text-gray-500">{periodLabel}</strong> · Data standardized from July, earlier months excluded · A component with no data in Lark is dropped and its weight shared out (Reopen is unfilled on most rows)
          </span>
          <button
            type="button"
            onClick={() => setShowFormula(true)}
            className="px-2 py-0.5 rounded-full border border-gray-200 text-[10px] text-gray-600 font-medium hover:border-gray-400 hover:text-gray-900">
            How the score works ⓘ
          </button>
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

          const onTimeN = d.agg?.on_time_count ?? 0;
          const scoredN = d.agg?.on_time_filled ?? 0;
          const excusedN = (d.agg?.late_excused_count ?? 0) + (d.agg?.overdue_excused_count ?? 0);

          const lateN = d.agg?.late_count ?? 0;
          const rateTip = [
            `On-time rate — ${periodLabel}`,
            `= on-time ÷ scored`,
            scoredN
              ? `${onTimeN} ÷ ${scoredN} = ${onTimeRate.toFixed(1)}%`
              : `No scored tasks yet — nothing completed with an on-time result.`,
            ``,
            `Scored = completed tasks marked On-time or Late.`,
            `Not scored: tasks still open${excusedN ? `, and ${excusedN} miss(es) carrying a Late Reason` : ""}.`,
            ``,
            `${lateN} task(s) finished late still count here — they are also in`,
            `the Overdue/Late column, which totals every blown deadline whether`,
            `the task got finished afterwards or not.`,
          ].join("\n");

          const monthList = displayMonths
            .map((m) => { const det = d.monthDetails[m - 1]; return det ? `${MONTH_NAMES[m - 1]} ${det.score.toFixed(1)}` : null; })
            .filter(Boolean);
          const scoreTip = [
            `Score ${d.score?.toFixed(1)} — ${periodLabel}`,
            monthList.length > 1
              ? `= average of ${monthList.length} monthly scores: ${monthList.join("  ·  ")}`
              : monthList.join(""),
            ``,
            `Each month = On-time ×0.35 + Cycle/Est. ×0.25 + Overdue/Late ×0.25 + Reopen ×0.15`,
            `A component Lark has no data for is dropped and its weight shared`,
            `out over the rest — Reopen is unfilled on most rows, so in practice`,
            `On-time and Overdue/Late carry most of the score.`,
            monthList.length > 1 ? `Averaged per month, so it won't match the on-time rate above.` : null,
          ].filter(v => v !== null).join("\n");

          const tasksTip = [
            `${d.agg?.total_tasks ?? 0} tasks — deadline falls in ${periodLabel}.`,
            `${d.open} open — not Completed yet, deadline in the same period.`,
            d.excluded
              ? `${d.excluded} task(s) hidden: status Upcoming Tasks / Regular task / Blocked task, outside the KPI.`
              : null,
            d.noDeadline
              ? `${d.noDeadline} open task(s) created since July have no deadline, so they fall in no month — see the red badge.`
              : null,
          ].filter(Boolean).join("\n");

          return (
            <div key={d.name} className="bg-white rounded-xl border border-gray-200 p-3 flex flex-col items-center gap-2">
              <p className="text-xs font-semibold text-gray-700">{d.name}</p>
              <div className="relative w-16 h-16 cursor-help" title={rateTip}>
                <svg className="w-full h-full -rotate-90" viewBox="0 0 72 72">
                  <circle cx="36" cy="36" r={radius} fill="none" stroke="#e5e7eb" strokeWidth="7" />
                  {onTimeRate > 0 && (
                    <circle cx="36" cy="36" r={radius} fill="none" stroke={ringColor} strokeWidth="7"
                      strokeDasharray={`${filled} ${circ}`} strokeLinecap="round" />
                  )}
                </svg>
                <div className="absolute inset-0 flex flex-col items-center justify-center">
                  <span className="text-[11px] font-bold text-gray-800">{onTimeRate > 0 ? `${onTimeRate.toFixed(0)}%` : "—"}</span>
                  <span className="text-[9px] text-gray-400 leading-none">{d.agg?.on_time_count ?? 0}/{d.agg?.on_time_filled ?? 0}</span>
                </div>
              </div>
              <div className="text-[10px] text-gray-400 cursor-help" title={rateTip}>On-time rate ⓘ</div>
              {d.score !== null && (
                <button
                  type="button"
                  onClick={() => setShowFormula(true)}
                  title={`${scoreTip}\n\nClick for the full formula.`}
                  className={`px-2 py-0.5 rounded-full text-[10px] cursor-pointer hover:ring-1 hover:ring-gray-300 ${rCls}`}>
                  {d.score.toFixed(1)} · {ratingLabel(d.score)}
                </button>
              )}
              <div className="text-[10px] text-gray-400 cursor-help" title={tasksTip}>
                <CountLink
                  n={d.agg?.total_tasks ?? 0} label="tasks" category="total"
                  picName={d.name} year={year} monthFilter={monthFilter}
                  periodLabel={periodLabel} onOpen={setDrilldown}
                />
                {" · "}
                <CountLink
                  n={d.open} label="open" category="open"
                  picName={d.name} year={year} monthFilter={monthFilter}
                  periodLabel={periodLabel} onOpen={setDrilldown}
                />
              </div>
              {d.noDeadline > 0 && (
                <button
                  type="button"
                  onClick={() => setDrilldown({ picName: d.name, year, month: null, category: "no_deadline" })}
                  title={`${d.noDeadline} open task(s) have no deadline set — click to list them. Only counts tasks created since July, when the base was standardized. Upcoming Tasks, Regular task and Blocked task are excluded.`}
                  className="flex items-center gap-1 px-2 py-0.5 bg-red-50 border border-red-200 rounded-full text-[10px] text-red-600 font-medium cursor-pointer hover:bg-red-100 hover:underline">
                  ⚠ {d.noDeadline} no deadline
                </button>
              )}
            </div>
          );
        })}
      </div>

      {/* ── Score heatmap: person × month ── */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 overflow-x-auto">
        <div className="flex items-center gap-2 mb-3">
          <p className="text-xs font-semibold text-gray-600">Performance Score — Heatmap</p>
          <button
            type="button"
            onClick={() => setShowFormula(true)}
            className="text-[10px] text-gray-400 underline decoration-dotted hover:text-indigo-600 cursor-pointer">
            formula ℹ
          </button>
        </div>
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
                  const det = d.monthDetails[i];
                  return (
                    <td key={i} className="px-0.5 py-1 text-center relative group/cell">
                      <div className="rounded text-[10px] font-semibold py-1 px-0.5 cursor-default" style={{ backgroundColor: bg, color: text, minWidth: 32 }}>
                        {score !== null ? score.toFixed(1) : "·"}
                      </div>
                      {det && (
                        <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 hidden group-hover/cell:block bg-gray-800 text-white text-[9px] rounded-lg p-2 z-20 whitespace-nowrap shadow-lg leading-[1.6]">
                          <div className="font-semibold text-[10px] mb-0.5">{MONTH_NAMES[i]} score: {det.score.toFixed(1)}</div>
                          <div>On-time: <b>{det.on_time_rate != null ? `${det.on_time_rate.toFixed(0)}%` : "—"}</b> → {det.s_ontime ?? "—"} {fmtWeight(det.weights.ontime)}</div>
                          <div>Cycle: <b>{det.cycle_ratio != null ? det.cycle_ratio.toFixed(2) : "—"}</b> → {det.s_cycle ?? "—"} {fmtWeight(det.weights.cycle)}</div>
                          <div>Overdue/Late: <b>{det.overdue ?? "—"}</b> → {det.s_overdue ?? "—"} {fmtWeight(det.weights.overdue)}</div>
                          <div>Reopen: <b>{det.reopen != null ? det.reopen : "—"}</b> → {det.s_reopen ?? "—"} {fmtWeight(det.weights.reopen)}</div>
                        </div>
                      )}
                    </td>
                  );
                })}
                <td className="pl-2 py-1 text-center">
                  {/* Always the full year — this column is labelled Annual and
                      must not follow the Month filter the cards above use. */}
                  {(() => { const { bg, text } = heatColor(d.annualScore); return (
                    <div className="rounded text-[10px] font-bold py-1 px-1" style={{ backgroundColor: bg, color: text }}>
                      {d.annualScore !== null ? d.annualScore.toFixed(1) : "—"}
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
                {d.annualScore !== null && (
                  <span className={`px-1.5 py-0.5 rounded text-[10px] ${ratingClasses(d.annualScore)}`}>
                    Avg {d.annualScore.toFixed(1)}
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
              <th className={thCls + " text-right cursor-help"}
                  title="Every blown deadline, counted as one figure. Late = finished after the deadline. Overdue = still unfinished with the deadline gone. Misses carrying a Late Reason are not here — they sit in Excused.">
                Overdue / Late ⓘ
              </th>
              <th className={thCls + " text-right cursor-help"}
                  title="Missed deadlines that carry a Late Reason. Any reason excuses the miss; leaving it blank does not. Excluded from on-time rate and Overdue/Late — shown so process bottlenecks stay visible. Hover a number for the per-reason split.">
                Excused ⓘ
              </th>
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
                    <td
                      className={tdCls + " text-right" + (row && !isAnnual ? " cursor-pointer hover:text-blue-600" : "")}
                      onClick={row && !isAnnual ? () => setDrilldown({ picName: person.name, year, month, category: "total" }) : undefined}
                    >{row ? row.total_tasks : "—"}</td>
                    <td
                      className={tdCls + " text-right" + (row && !isAnnual ? " cursor-pointer hover:text-blue-600" : "")}
                      onClick={row && !isAnnual ? () => setDrilldown({ picName: person.name, year, month, category: "done" }) : undefined}
                    >{row ? `${row.completed} (${fmtNum(row.completion_rate)}%)` : "—"}</td>
                    <td
                      className={tdCls + " text-right" + (row?.on_time_rate != null && !isAnnual ? " cursor-pointer" : "")}
                      onClick={row?.on_time_rate != null && !isAnnual ? () => setDrilldown({ picName: person.name, year, month, category: "on_time" }) : undefined}
                    >
                      {row?.on_time_rate != null ? (
                        <span className={(row.on_time_rate >= 90 ? "text-green-600 font-medium" : row.on_time_rate >= 70 ? "text-yellow-600" : "text-red-500") + " hover:underline"}>
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
                    {(() => {
                      const missed = row ? missedCount(row) : null;
                      const tip = row
                        ? `${row.late_count ?? 0} late (finished after the deadline) + ${row.overdue_count ?? 0} overdue (still unfinished)`
                        : undefined;
                      return (
                        <td
                          className={tdCls + " text-right" + (missed != null && !isAnnual ? " cursor-pointer" : "")}
                          title={tip}
                          onClick={missed != null && !isAnnual ? () => setDrilldown({ picName: person.name, year, month, category: "missed" }) : undefined}
                        >
                          {missed != null ? (
                            <span className={(missed === 0 ? "text-green-600" : missed <= 2 ? "text-yellow-600" : "text-red-500") + (missed > 0 ? " hover:underline" : "")}>
                              {missed}
                            </span>
                          ) : "—"}
                        </td>
                      );
                    })()}
                    {(() => {
                      const excused = row ? (row.late_excused_count ?? 0) + (row.overdue_excused_count ?? 0) : null;
                      const breakdown = Object.entries(row?.reason_counts ?? {})
                        .map(([k, v]) => `${prettyReason(k)}: ${v}`).join("\n");
                      return (
                        <td
                          className={tdCls + " text-right" + (excused > 0 && !isAnnual ? " cursor-pointer" : "")}
                          title={breakdown || undefined}
                          onClick={excused > 0 && !isAnnual ? () => setDrilldown({ picName: person.name, year, month, category: "excused" }) : undefined}
                        >
                          {excused != null ? (
                            <span className={excused > 0 ? "text-blue-600 hover:underline" : "text-gray-300"}>
                              {excused}
                            </span>
                          ) : "—"}
                        </td>
                      );
                    })()}
                    {(() => {
                      const det = computeScoreDetail(row);
                      const tip = det
                        ? [
                            `On-time: ${fmtNum(det.on_time_rate)}% ${fmtWeight(det.weights.ontime)}`,
                            `Cycle: ${fmtNum(det.cycle_ratio, 2)}× ${fmtWeight(det.weights.cycle)}`,
                            `Overdue/Late: ${det.overdue ?? "—"} ${fmtWeight(det.weights.overdue)}`,
                            `Reopen: ${det.reopen ?? "—"} ${fmtWeight(det.weights.reopen)}`,
                          ].join("\n")
                        : SCORE_TOOLTIP;
                      return (
                        <td className={tdCls + " text-right font-semibold cursor-help"} title={tip}>
                          {score ?? "—"}
                        </td>
                      );
                    })()}
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
        Score = On-time rate ×0.35 + Cycle/Estimate ×0.25 + Overdue/Late ×0.25 + Reopen ×0.15 — a component with no data in Lark is dropped and its weight shared out over the rest, so an unfilled field no longer scores a free 10.
      </p>
      {drilldown && (
        <TaskDetailModal
          drilldown={drilldown}
          tasks={drillTasks}
          isLoading={drillLoading}
          onClose={() => setDrilldown(null)}
        />
      )}

      {showFormula && <ScoreFormulaModal onClose={() => setShowFormula(false)} />}
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
            onRefresh={() => queryClient.refetchQueries({ queryKey: ["team-kpi", role, year, branchId] })}
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
