/**
 * Marketing Activity — Consolidated view of Paid Ads, KOL, and CRM performance.
 * Month-based filter with MoM comparison.
 */
import { useState, useMemo, useRef } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";
import {
  getMarketingActivitySummary,
  getCRMBranchComparison,
  getCRMRatePlanDetail,
  getRatePlanCampaigns,
  saveRatePlanCampaign,
} from "../api/marketingActivity";
import { getEmailSummary, getEmailByCampaign } from "../api/emailMarketing";
import SeasonalCampaignTab from "../components/SeasonalCampaignTab";
import ComparisonMatrix from "../components/ComparisonMatrix";

// Map HiD branch name to GHL location name (5 branches × different naming)
function branchToGHL(branchName) {
  if (!branchName) return null;
  const lower = branchName.toLowerCase();
  if (lower.includes("saigon")) return "Saigon";
  if (lower.includes("1948")) return "1948";
  if (lower.includes("taipei")) return "Taipei";
  if (lower.includes("oani")) return "Oani";
  if (lower.includes("osaka")) return "Osaka";
  return null;
}

// First and last day of YYYY-MM month string, returned as YYYY-MM-DD
function monthBounds(monthStr) {
  const [y, m] = monthStr.split("-").map(Number);
  const start = `${y}-${String(m).padStart(2, "0")}-01`;
  const lastDay = new Date(y, m, 0).getDate();
  const end = `${y}-${String(m).padStart(2, "0")}-${String(lastDay).padStart(2, "0")}`;
  return { date_from: start, date_to: end };
}

function fmtNum(val) {
  if (val == null || val === 0) return "0";
  return new Intl.NumberFormat("en").format(Math.round(val));
}

function fmtMoney(val, cur) {
  if (val == null) return "—";
  const sym = CURRENCY_SYMBOLS[cur] || "";
  return sym + new Intl.NumberFormat("en").format(Math.round(val));
}

function pctChange(cur, prev) {
  if (!prev || prev === 0) return null;
  return ((cur - prev) / prev) * 100;
}

function ChangeBadge({ current, previous }) {
  const pct = pctChange(current, previous);
  if (pct == null) return null;
  const isUp = pct > 0;
  const cls = isUp ? "text-green-600" : pct < 0 ? "text-red-600" : "text-gray-500";
  return (
    <span className={"text-xs font-medium " + cls}>
      {isUp ? "▲" : pct < 0 ? "▼" : ""}{Math.abs(pct).toFixed(1)}%
    </span>
  );
}

function RoasBadge({ value }) {
  if (value == null || value === 0) return <span className="text-gray-400">{"—"}</span>;
  const cls =
    value >= 3 ? "text-green-700 bg-green-50"
    : value >= 1.5 ? "text-yellow-700 bg-yellow-50"
    : "text-red-600 bg-red-50";
  return <span className={"px-2 py-0.5 rounded text-xs font-semibold " + cls}>{value.toFixed(2)}x</span>;
}

function KPICard({ label, value, sub, prev, prevLabel }) {
  return (
    <div className="bg-white rounded-lg border p-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">{label}</p>
      <p className="text-xl font-bold text-gray-900">{value}</p>
      {prev != null && (
        <div className="flex items-center gap-1.5 mt-1">
          <ChangeBadge current={parseFloat(String(value).replace(/[^0-9.-]/g, "")) || 0} previous={prev} />
          <span className="text-xs text-gray-400">vs {prevLabel}</span>
        </div>
      )}
      {sub && <p className="text-xs text-gray-400 mt-1">{sub}</p>}
    </div>
  );
}

function ytdBounds(year) {
  const today = new Date();
  const isCurrentYear = year === today.getFullYear();
  const pad = (n) => String(n).padStart(2, "0");
  const date_from = `${year}-01-01`;
  const date_to = isCurrentYear
    ? `${year}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`
    : `${year}-12-31`;
  return { date_from, date_to };
}

function ytdPrevLabel(year) {
  return `${year - 1} YTD`;
}

export default function MarketingActivity() {
  const { isAll, selected, currency: branchCurrency } = useBranch();
  const [tab, setTab] = useState("overview");
  const [viewMode, setViewMode] = useState("monthly"); // "monthly" | "ytd"

  const today = new Date();
  const currentMonthStr = today.getFullYear() + "-" + String(today.getMonth() + 1).padStart(2, "0");
  const [month, setMonth] = useState(currentMonthStr);
  const [ytdYear, setYtdYear] = useState(today.getFullYear());

  // Built once and shared: the CRM drill-down must query the exact window and
  // branch the table row was aggregated over, or its numbers won't reconcile.
  const queryParams = useMemo(() => {
    const params = {};
    if (!isAll && selected) params.branch_id = selected;
    if (viewMode === "ytd") {
      const { date_from, date_to } = ytdBounds(ytdYear);
      params.date_from = date_from;
      params.date_to = date_to;
    } else {
      params.month = month;
    }
    return params;
  }, [isAll, selected, viewMode, ytdYear, month]);

  const { data, isPending, isPlaceholderData } = useQuery({
    queryKey: ["marketing-activity", selected, isAll, month, ytdYear, viewMode],
    queryFn: () => getMarketingActivitySummary(queryParams),
    placeholderData: keepPreviousData,
  });

  const cur = isAll ? "VND" : (data?.currency || branchCurrency || "VND");
  const overview = data?.overview;
  const prevOverview = data?.prev_overview;
  const prevMonth = data?.prev_month;
  const crmRatePlans = data?.crm_by_rate_plan || [];

  const TABS = [
    { key: "overview", label: "Overview" },
    { key: "crm-rate-plans", label: "CRM Reservations" },
    { key: "seasonal", label: "Seasonal Campaign" },
    { key: "email-stat", label: "Email Stat" },
  ];

  // Format comparison label
  const prevLabel = viewMode === "ytd"
    ? ytdPrevLabel(ytdYear)
    : (prevMonth ? new Date(prevMonth + "-01").toLocaleDateString("en", { month: "short", year: "numeric" }) : "");

  // The window the numbers cover, spelled out for tabs that describe their
  // own period rather than comparing against a previous one.
  const periodLabel = viewMode === "ytd"
    ? `${ytdYear} YTD`
    : new Date(month + "-01").toLocaleDateString("en", { month: "long", year: "numeric" });

  const minYear = 2024;
  const maxYear = today.getFullYear();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-lg font-bold text-gray-900">Marketing Activity</h1>
        <div className="flex items-center gap-2">
          {/* Monthly / YTD toggle */}
          <div className="flex gap-0.5 bg-gray-100 rounded-lg p-1">
            {[{ key: "monthly", label: "Monthly" }, { key: "ytd", label: "YTD" }].map((v) => (
              <button key={v.key} onClick={() => setViewMode(v.key)}
                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                  viewMode === v.key ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
                }`}>
                {v.label}
              </button>
            ))}
          </div>
          {viewMode === "monthly" ? (
            <input type="month" value={month} onChange={(e) => setMonth(e.target.value)}
              className="border rounded px-3 py-1.5 text-sm" />
          ) : (
            <select value={ytdYear} onChange={(e) => setYtdYear(Number(e.target.value))}
              className="border rounded px-3 py-1.5 text-sm">
              {Array.from({ length: maxYear - minYear + 1 }, (_, i) => maxYear - i).map((y) => (
                <option key={y} value={y}>{y}</option>
              ))}
            </select>
          )}
        </div>
      </div>

      <div className="flex gap-1 border-b">
        {TABS.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === t.key ? "border-indigo-600 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"
            }`}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "email-stat" ? (
        // Email Stat fetches its own data — independent of the activity API
        <EmailStatTab
          month={viewMode === "ytd" ? null : month}
          ytdBounds={viewMode === "ytd" ? ytdBounds(ytdYear) : null}
          onViewRevenue={() => setTab("crm-rate-plans")}
        />
      ) : tab === "seasonal" ? (
        // Also self-fetching: its numbers come from the ads + rate plan join,
        // not from the summary payload the other two tabs share.
        <SeasonalCampaignTab
          branchId={isAll ? null : selected}
          month={viewMode === "ytd" ? null : month}
          ytd={viewMode === "ytd" ? ytdBounds(ytdYear) : null}
          cur={cur}
          periodLabel={periodLabel}
        />
      ) : isPending && !data ? (
        <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading...</div>
      ) : !data ? (
        <div className="text-center text-gray-400 py-16 text-sm">No data available</div>
      ) : (
        <div className={"transition-opacity duration-150 " + (isPlaceholderData ? "opacity-40 pointer-events-none" : "")}>
          {tab === "overview" && <OverviewTab overview={overview} prevOverview={prevOverview} prevLabel={prevLabel} cur={cur} isYtd={viewMode === "ytd"} ytdYear={ytdYear} />}
          {tab === "crm-rate-plans" && <CRMRatePlansTab rows={crmRatePlans} cur={cur} month={viewMode === "ytd" ? currentMonthStr : month} queryParams={queryParams} />}
        </div>
      )}
    </div>
  );
}

/* ── Overview Tab ──────────────────────────────────────────────────────────── */
function OverviewTab({ overview, prevOverview, prevLabel, cur, isYtd, ytdYear }) {
  if (!overview) return null;
  const { paid_ads, kol, crm, total } = overview;
  const prev = prevOverview?.total;

  return (
    <div className="space-y-6">
      {isYtd && (
        <p className="text-xs text-gray-500">
          Year-to-date performance for <span className="font-semibold">{ytdYear}</span> (Jan 1 – today). Compared against the same period in {ytdYear - 1}.
        </p>
      )}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KPICard label="Total Bookings" value={fmtNum(total.bookings)} prev={prev?.bookings} prevLabel={prevLabel} />
        <KPICard label={`Total Revenue (${cur})`} value={fmtNum(total.revenue)} prev={prev?.revenue} prevLabel={prevLabel} />
        <KPICard label={`Total Cost (${cur})`} value={fmtNum(total.cost)} prev={prev?.cost} prevLabel={prevLabel} />
        <KPICard label="Blended ROAS" value={total.roas ? total.roas.toFixed(2) + "x" : "—"} />
      </div>

      <div className="bg-white rounded-lg border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="text-left px-4 py-3 font-semibold text-gray-600">Source</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Bookings</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Revenue ({cur})</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Cost ({cur})</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">ROAS</th>
              {prevOverview && <th className="text-right px-4 py-3 font-semibold text-gray-600">vs {prevLabel}</th>}
            </tr>
          </thead>
          <tbody className="divide-y">
            {[
              { label: "Paid Ads", color: "bg-blue-500", data: paid_ads, prev: prevOverview?.paid_ads, hasCost: true },
              { label: "KOL", color: "bg-purple-500", data: kol, prev: prevOverview?.kol, hasCost: true },
              { label: "CRM", color: "bg-emerald-500", data: crm, prev: prevOverview?.crm, hasCost: true },
            ].map(({ label, color, data: d, prev: p, hasCost }) => (
              <tr key={label} className="hover:bg-gray-50">
                <td className="px-4 py-3 font-medium">
                  <span className={"inline-block w-2 h-2 rounded-full mr-2 " + color} />{label}
                </td>
                <td className="px-4 py-3 text-right">{fmtNum(d.bookings)}</td>
                <td className="px-4 py-3 text-right">{fmtNum(d.revenue)}</td>
                <td className="px-4 py-3 text-right">{hasCost ? fmtNum(d.cost) : <span className="text-gray-400">{"—"}</span>}</td>
                <td className="px-4 py-3 text-right">
                  {d.roas ? <RoasBadge value={d.roas} /> :
                    hasCost && d.cost > 0 ? <RoasBadge value={d.revenue / d.cost} /> :
                    <span className="text-gray-400">{"—"}</span>}
                </td>
                {prevOverview && (
                  <td className="px-4 py-3 text-right">
                    <ChangeBadge current={d.revenue} previous={p?.revenue} />
                  </td>
                )}
              </tr>
            ))}
            <tr className="bg-gray-50 font-semibold">
              <td className="px-4 py-3">Total</td>
              <td className="px-4 py-3 text-right">{fmtNum(total.bookings)}</td>
              <td className="px-4 py-3 text-right">{fmtNum(total.revenue)}</td>
              <td className="px-4 py-3 text-right">{fmtNum(total.cost)}</td>
              <td className="px-4 py-3 text-right"><RoasBadge value={total.roas} /></td>
              {prevOverview && (
                <td className="px-4 py-3 text-right">
                  <ChangeBadge current={total.revenue} previous={prev?.revenue} />
                </td>
              )}
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ── Campaign label — hand-typed, one per rate plan name ─────────────────── */
// Cloudbeds rate plan names don't say which campaign they belong to, so the
// team types it here once per rate plan and everyone reading the table sees it.
function CampaignCell({ value, onSave, saving }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef(null);

  const start = () => {
    setDraft(value || "");
    setEditing(true);
    setTimeout(() => inputRef.current?.focus(), 0);
  };

  const commit = () => {
    setEditing(false);
    const next = draft.trim();
    if (next !== (value || "")) onSave(next);
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") setEditing(false);
        }}
        maxLength={200}
        placeholder="e.g. Summer 2026 Retention"
        className="w-full text-sm border border-blue-400 rounded px-2 py-1 bg-white outline-none"
      />
    );
  }

  return (
    <button
      onClick={start}
      title="Click to edit — blank clears it"
      className={`w-full text-left text-sm rounded px-2 py-1 transition-colors hover:bg-yellow-100 ${
        value ? "text-gray-800" : "text-gray-300 italic"
      }`}
    >
      {saving ? <span className="text-gray-400">Saving…</span> : value || "+ add campaign"}
    </button>
  );
}

/* ── CRM Reservations Tab — grouped by Rate Plan Name ────────────────────── */
function CRMRatePlansTab({ rows, cur, month, queryParams }) {
  const [view, setView] = useState("rate-plan");
  const [openPlan, setOpenPlan] = useState(null);
  const queryClient = useQueryClient();

  // Campaign labels are global (a rate plan tag means the same campaign on
  // every branch), so this query carries no branch/month key.
  const { data: campaigns } = useQuery({
    queryKey: ["rate-plan-campaigns"],
    queryFn: getRatePlanCampaigns,
  });
  const [savingPlan, setSavingPlan] = useState(null);
  const [saveError, setSaveError] = useState(null);

  const saveCampaign = async (ratePlan, campaign) => {
    setSavingPlan(ratePlan);
    setSaveError(null);
    try {
      await saveRatePlanCampaign(ratePlan, campaign);
      await queryClient.invalidateQueries({ queryKey: ["rate-plan-campaigns"] });
    } catch (e) {
      // Leave the old label on screen — the cell must never show a value the
      // server didn't accept.
      const detail = e?.response?.data?.detail || e?.message || "unknown error";
      setSaveError(`Could not save the campaign for "${ratePlan}": ${detail}`);
    } finally {
      setSavingPlan(null);
    }
  };

  const subToggle = (
    <div className="flex gap-1 bg-gray-100 rounded-lg p-1 w-fit">
      {[
        { key: "rate-plan", label: "By Rate Plan" },
        { key: "compare", label: "Compare Branches" },
      ].map((v) => (
        <button key={v.key} onClick={() => setView(v.key)}
          className={`px-3 py-1 rounded-md text-sm font-medium transition-colors ${
            view === v.key ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
          }`}>
          {v.label}
        </button>
      ))}
    </div>
  );

  if (view === "compare") {
    return (
      <div className="space-y-4">
        {subToggle}
        <CRMBranchComparison month={month} />
      </div>
    );
  }

  if (!rows || rows.length === 0) {
    return (
      <div className="space-y-4">
        {subToggle}
        <p className="text-gray-400 text-sm text-center py-8">
          No CRM reservations found for this month.
        </p>
      </div>
    );
  }

  const totals = rows.reduce(
    (acc, r) => ({
      bookings: acc.bookings + (r.bookings || 0),
      nights: acc.nights + (r.nights || 0),
      revenue: acc.revenue + (r.revenue || 0),
    }),
    { bookings: 0, nights: 0, revenue: 0 }
  );
  const totalAdr = totals.nights > 0 ? totals.revenue / totals.nights : 0;

  const hasZeroRevenueRow = rows.some((r) => (r.bookings || 0) > 0 && (r.revenue || 0) === 0);
  const zeroRevTooltip =
    "Bookings exist but accommodation total = 0 in Cloudbeds — typically complimentary stays, voucher redemptions, or comp event guests where the room rate was waived.";

  return (
    <div className="space-y-4">
      {subToggle}
      <p className="text-sm text-gray-500">
        CRM reservations (CRM / MEANDER&apos;S FRIEND / Travel Guide / Grand Open / Extension Promotion / WELCOME) broken down by Rate Plan Name,
        filtered by Date Booked (not Stay Date).
        Excludes cancelled bookings and non-paying sources (Blogger / House Use / Special Case).
        <br />
        <span className="text-gray-400">
          Click a Rate Plan Name to see who booked it — status, countries, demographics.
          Campaign is filled in by hand — click a cell to name the campaign a rate plan belongs to.
          It applies to that rate plan on every branch.
        </span>
      </p>
      {hasZeroRevenueRow && (
        <p className="text-xs text-gray-500 italic">
          <span className="font-semibold not-italic">Note:</span> rows marked with{" "}
          <span className="font-semibold text-amber-600">0*</span> in Revenue have bookings whose
          accommodation total = 0 in Cloudbeds (typically complimentary stays, voucher redemptions,
          or comp event guests where the room rate was waived).
        </p>
      )}
      {saveError && (
        <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded px-3 py-2">
          {saveError}
        </p>
      )}
      <div className="bg-white rounded-lg border overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="text-left px-4 py-3 font-semibold text-gray-600">Rate Plan Name</th>
              <th className="text-left px-4 py-3 font-semibold text-gray-600">Campaign</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Bookings</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Nights</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">Revenue ({cur})</th>
              <th className="text-right px-4 py-3 font-semibold text-gray-600">ADR ({cur})</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {rows.map((r, i) => {
              const isZeroRev = (r.bookings || 0) > 0 && (r.revenue || 0) === 0;
              return (
                <tr key={i} className="hover:bg-gray-50">
                  <td className="px-4 py-3">
                    <button
                      onClick={() => setOpenPlan(r.rate_plan_name)}
                      title="See who booked this rate plan"
                      className="font-medium text-gray-900 text-left hover:text-blue-600 hover:underline"
                    >
                      {r.rate_plan_name}
                    </button>
                  </td>
                  <td className="px-2 py-2">
                    <CampaignCell
                      value={campaigns?.[r.rate_plan_name] || ""}
                      saving={savingPlan === r.rate_plan_name}
                      onSave={(val) => saveCampaign(r.rate_plan_name, val)}
                    />
                  </td>
                  <td className="px-4 py-3 text-right">{fmtNum(r.bookings)}</td>
                  <td className="px-4 py-3 text-right">{fmtNum(r.nights)}</td>
                  <td className="px-4 py-3 text-right">
                    {isZeroRev ? (
                      <span className="text-amber-600 font-semibold cursor-help" title={zeroRevTooltip}>
                        0*
                      </span>
                    ) : (
                      fmtNum(r.revenue)
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">{fmtNum(r.adr)}</td>
                </tr>
              );
            })}
            <tr className="bg-gray-50 font-semibold">
              <td className="px-4 py-3">Total</td>
              <td className="px-4 py-3"></td>
              <td className="px-4 py-3 text-right">{fmtNum(totals.bookings)}</td>
              <td className="px-4 py-3 text-right">{fmtNum(totals.nights)}</td>
              <td className="px-4 py-3 text-right">{fmtNum(totals.revenue)}</td>
              <td className="px-4 py-3 text-right">{fmtNum(totalAdr)}</td>
            </tr>
          </tbody>
        </table>
      </div>
      {openPlan && (
        <RatePlanDetailModal
          ratePlan={openPlan}
          cur={cur}
          queryParams={queryParams}
          onClose={() => setOpenPlan(null)}
        />
      )}
    </div>
  );
}

/* ── Rate Plan drill-down — who actually booked this plan ────────────────── */

function DistBar({ label, sublabel, value, max, total, right }) {
  const pct = max > 0 ? Math.max(1.5, (value / max) * 100) : 0;
  const share = total > 0 ? (value / total) * 100 : 0;
  return (
    <div className="py-1">
      <div className="flex items-baseline justify-between gap-2 mb-1">
        <span className="text-sm text-gray-700 truncate" title={label}>
          {label}
          {sublabel && <span className="text-gray-400 text-xs ml-1.5">{sublabel}</span>}
        </span>
        <span className="text-xs text-gray-500 whitespace-nowrap">
          {right ?? <>{fmtNum(value)} <span className="text-gray-400">· {share.toFixed(0)}%</span></>}
        </span>
      </div>
      <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
        <div className="h-full rounded-full bg-blue-500" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function MiniStat({ label, stats, suffix }) {
  return (
    <div className="border border-gray-200 rounded p-2 text-center">
      <p className="text-[11px] text-gray-500">{label}</p>
      <p className="text-lg font-semibold text-gray-900">
        {stats?.count > 0 ? fmtNum(stats.avg) : "—"}
        {stats?.count > 0 && suffix ? <span className="text-xs font-normal text-gray-400 ml-0.5">{suffix}</span> : null}
      </p>
      <p className="text-[10px] text-gray-400">
        {stats?.count > 0 ? `med ${fmtNum(stats.median)} · n=${stats.count}` : "no data"}
      </p>
    </div>
  );
}

function RatePlanDetailModal({ ratePlan, cur, queryParams, onClose }) {
  const { data, isPending, error } = useQuery({
    queryKey: ["crm-rate-plan-detail", ratePlan, queryParams],
    queryFn: () => getCRMRatePlanDetail({ ...queryParams, rate_plan: ratePlan }),
  });

  const h = data?.headline;
  const ex = data?.excluded;
  const cov = data?.coverage;
  const bookings = h?.bookings || 0;

  const sumOf = (rows) => (rows || []).reduce((a, r) => a + (r.bookings || 0), 0);
  const maxOf = (rows) => Math.max(1, ...(rows || []).map((r) => r.bookings || 0));

  return (
    <div
      className="fixed inset-0 bg-black/40 flex items-start justify-center z-50 p-4 overflow-y-auto"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-lg shadow-xl w-full max-w-3xl my-4 max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 bg-white border-b px-5 py-3 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h3 className="font-semibold text-gray-900 truncate" title={ratePlan}>{ratePlan}</h3>
            <p className="text-xs text-gray-400 mt-0.5">
              {data
                ? `Booked ${data.period.from} → ${data.period.to}`
                : "Loading…"}
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700 text-xl leading-none shrink-0">
            ×
          </button>
        </div>

        {isPending ? (
          <p className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading…</p>
        ) : error || !data ? (
          <p className="text-center text-red-600 py-16 text-sm">
            Could not load this rate plan: {error?.message || "unknown error"}
          </p>
        ) : (
          <div className="p-5 space-y-5">
            {/* Headline — same rows the table row counted */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div className="border border-gray-200 rounded-lg p-3">
                <p className="text-xs text-gray-500">Bookings</p>
                <p className="text-xl font-bold text-gray-900 mt-0.5">{fmtNum(bookings)}</p>
                <p className="text-[11px] text-gray-400 mt-0.5">{fmtNum(h.guests)} guests</p>
              </div>
              <div className="border border-gray-200 rounded-lg p-3">
                <p className="text-xs text-gray-500">Nights</p>
                <p className="text-xl font-bold text-gray-900 mt-0.5">{fmtNum(h.nights)}</p>
                <p className="text-[11px] text-gray-400 mt-0.5">
                  {bookings > 0 ? `${(h.nights / bookings).toFixed(1)} per booking` : "—"}
                </p>
              </div>
              <div className="border border-gray-200 rounded-lg p-3">
                <p className="text-xs text-gray-500">Revenue ({cur})</p>
                <p className="text-xl font-bold text-gray-900 mt-0.5">{fmtNum(h.revenue)}</p>
                <p className="text-[11px] text-gray-400 mt-0.5">ADR {fmtNum(h.adr)}</p>
              </div>
              <div className="border border-gray-200 rounded-lg p-3">
                <p className="text-xs text-gray-500">Cancelled</p>
                <p className="text-xl font-bold text-gray-900 mt-0.5">{ex.cancel_rate}%</p>
                <p className="text-[11px] text-gray-400 mt-0.5">
                  {fmtNum(ex.cancelled)} of {fmtNum(ex.total_rows)} ever booked
                </p>
              </div>
            </div>

            <p className="text-xs text-gray-400">
              The four numbers above count the same bookings as the table row
              {ex.cancelled > 0 || ex.non_paying_source > 0 ? (
                <> — {fmtNum(ex.cancelled)} cancelled
                  {ex.non_paying_source > 0 && <> and {fmtNum(ex.non_paying_source)} non-paying (Blogger / House Use / Special Case / Work Exchange)</>}
                  {" "}excluded. The status list below counts every booking, including those.</>
              ) : "."}
            </p>

            {/* Status */}
            <div>
              <p className="text-xs font-semibold text-gray-600 mb-2">Status</p>
              <div className="flex flex-wrap gap-1.5">
                {data.by_status.map((s) => (
                  <span
                    key={s.status}
                    className={`inline-block px-2 py-0.5 rounded text-xs ${
                      s.cancelled ? "bg-red-100 text-red-800" : "bg-emerald-100 text-emerald-800"
                    }`}
                  >
                    {s.status} · {fmtNum(s.bookings)}
                  </span>
                ))}
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Country */}
              <div>
                <p className="text-xs font-semibold text-gray-600 mb-2">Guest country</p>
                {data.by_country.length === 0 ? (
                  <p className="text-xs text-gray-400">No bookings to break down.</p>
                ) : (
                  data.by_country.map((c) => (
                    <DistBar
                      key={c.country}
                      label={c.country}
                      sublabel={c.country_code || undefined}
                      value={c.bookings}
                      max={maxOf(data.by_country)}
                      total={bookings}
                    />
                  ))
                )}
              </div>

              {/* Demographics */}
              <div className="space-y-4">
                <div>
                  <div className="flex items-baseline justify-between mb-2">
                    <p className="text-xs font-semibold text-gray-600">Gender</p>
                    <span className="text-[11px] text-gray-400">
                      {fmtNum(cov.gender_known)} of {fmtNum(bookings)} on file
                    </span>
                  </div>
                  {data.by_gender.map((g) => (
                    <DistBar key={g.gender} label={g.gender} value={g.bookings}
                      max={maxOf(data.by_gender)} total={sumOf(data.by_gender)} />
                  ))}
                </div>
                <div>
                  <div className="flex items-baseline justify-between mb-2">
                    <p className="text-xs font-semibold text-gray-600">Age at check-in</p>
                    <span className="text-[11px] text-gray-400">
                      {fmtNum(cov.age_known)} of {fmtNum(bookings)} on file
                    </span>
                  </div>
                  {data.by_age.map((a) => (
                    <DistBar key={a.bucket} label={a.bucket} value={a.bookings}
                      max={maxOf(data.by_age)} total={sumOf(data.by_age)} />
                  ))}
                </div>
              </div>
            </div>

            {(cov.gender_known === 0 || cov.age_known === 0) && bookings > 0 && (
              <p className="text-xs text-gray-500 italic">
                <span className="font-semibold not-italic">Note:</span> gender and birthdate are
                backfilled per guest from Cloudbeds and are missing on most reservations. Read the
                &quot;on file&quot; counts before treating either split as representative.
              </p>
            )}

            {/* Stay shape */}
            <div>
              <p className="text-xs font-semibold text-gray-600 mb-2">Stay shape</p>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                <MiniStat label="Nights" stats={data.nights_stats} />
                <MiniStat label="Adults" stats={data.adults_stats} />
                <MiniStat label="Lead time" stats={data.lead_time_stats} suffix="d" />
                <MiniStat label={`ADR (${cur})`} stats={data.adr_stats} />
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Source */}
              <div>
                <p className="text-xs font-semibold text-gray-600 mb-2">Booking source</p>
                {data.by_source.map((s) => (
                  <DistBar key={s.source} label={s.source} sublabel={s.category || undefined}
                    value={s.bookings} max={maxOf(data.by_source)} total={bookings} />
                ))}
              </div>
              {/* Rooms + branch */}
              <div className="space-y-4">
                <div>
                  <p className="text-xs font-semibold text-gray-600 mb-2">Room type</p>
                  {data.by_room_type.map((r) => (
                    <DistBar key={r.room_type} label={r.room_type} sublabel={r.category || undefined}
                      value={r.bookings} max={maxOf(data.by_room_type)} total={bookings} />
                  ))}
                </div>
                <div>
                  <p className="text-xs font-semibold text-gray-600 mb-2">Branch</p>
                  <div className="flex flex-wrap gap-1.5">
                    {data.by_branch.map((b) => (
                      <span key={b.branch} className="inline-block px-2 py-0.5 rounded text-xs bg-gray-100 text-gray-700">
                        {b.branch} · {fmtNum(b.bookings)}
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── CRM Branch Comparison — campaign × branch and month × branch ───────── */
const COMPARE_METRICS = [
  { key: "revenue", label: "Revenue" },
  { key: "bookings", label: "Bookings" },
  { key: "nights", label: "Nights" },
];

function CRMBranchComparison({ month }) {
  const { branches: allowedBranches } = useBranch();
  const [metric, setMetric] = useState("revenue");

  const { data, isPending, isPlaceholderData } = useQuery({
    queryKey: ["crm-branch-comparison", month],
    queryFn: () => getCRMBranchComparison({ month, months_back: 6 }),
    placeholderData: keepPreviousData,
  });

  // Only show branches this user is allowed to see, in the backend's display order.
  const branches = useMemo(() => {
    if (!data?.branches) return [];
    const allowedIds = new Set(allowedBranches.map((b) => b.id));
    return data.branches.filter((b) => allowedIds.size === 0 || allowedIds.has(b.branch_id));
  }, [data, allowedBranches]);

  if (isPending && !data) {
    return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading...</div>;
  }
  if (!data || branches.length === 0) {
    return <p className="text-gray-400 text-sm text-center py-8">No CRM comparison data.</p>;
  }

  const hasCampaign = (data.by_campaign || []).length > 0;
  const hasMonth = (data.by_month || []).length > 0;

  return (
    <div className={"space-y-4 transition-opacity duration-150 " + (isPlaceholderData ? "opacity-40 pointer-events-none" : "")}>
      <div className="flex items-center justify-between flex-wrap gap-3">
        <p className="text-sm text-gray-500">
          CRM performance compared across branches. Revenue in VND for cross-branch parity.
          Filtered by Date Booked, excluding cancelled bookings and non-paying sources.
        </p>
        <div className="flex gap-1 bg-gray-100 rounded-lg p-1 w-fit">
          {COMPARE_METRICS.map((m) => (
            <button key={m.key} onClick={() => setMetric(m.key)}
              className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                metric === m.key ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
              }`}>
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {hasCampaign ? (
        <ComparisonMatrix
          title={`By Campaign × Branch — ${metric === "revenue" ? "Revenue (VND)" : COMPARE_METRICS.find((m) => m.key === metric).label}`}
          subtitle="Selected month, grouped by Rate Plan Name"
          branches={branches}
          rows={data.by_campaign}
          rowLabel="Rate Plan"
          metric={metric}
          rowTitle={(r) => r.campaign_name || r.rate_plan_name}
          rowHint={(r) => r.rate_plan_name}
        />
      ) : (
        <p className="text-gray-400 text-sm text-center py-6">No campaign data this month.</p>
      )}

      {hasMonth && (
        <ComparisonMatrix
          title={`By Month × Branch — ${metric === "revenue" ? "Revenue (VND)" : COMPARE_METRICS.find((m) => m.key === metric).label}`}
          subtitle="Trailing 6 months by Date Booked"
          branches={branches}
          rows={data.by_month}
          rowLabel="Month"
          metric={metric}
          rowTitle={(r) => r.month}
        />
      )}
    </div>
  );
}

/* ── Email Stat Tab — GHL workflow + bulk email performance ─────────────── */
function pct(v) {
  if (v == null) return "—";
  return `${(v * 100).toFixed(2)}%`;
}

function EmailKPI({ label, value, color = "text-gray-900" }) {
  return (
    <div className="bg-white rounded-lg border p-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">{label}</p>
      <p className={`text-xl font-bold ${color}`}>{value}</p>
    </div>
  );
}

/* Roll campaign rows into the same shape /email/summary returns, so the KPI
   cards can follow the campaign-name search instead of always showing all-up
   totals. Rates mirror the backend: unique_opened / sent, unique_clicked / sent. */
function rollupCampaigns(rows) {
  const sum = (key) => rows.reduce((s, c) => s + (Number(c[key]) || 0), 0);
  const sent = sum("sent");
  const opened = sum("unique_opened");
  const clicked = sum("unique_clicked");
  return {
    total_sent: sent,
    open_rate: sent > 0 ? opened / sent : 0,
    click_rate: sent > 0 ? clicked / sent : 0,
    attributed_revenue_vnd: sum("attributed_revenue_vnd"),
  };
}

function EmailStatTab({ month, ytdBounds: ytdB, onViewRevenue }) {
  const { currentBranch, isAll } = useBranch();
  const [search, setSearch] = useState("");

  const ghlBranch = useMemo(
    () => isAll ? null : branchToGHL(currentBranch?.name),
    [currentBranch, isAll]
  );

  const { data: emailData, isPending, isPlaceholderData } = useQuery({
    queryKey: ["email-stat", month, ytdB, ghlBranch],
    queryFn: () => {
      const bounds = ytdB || monthBounds(month);
      const params = { date_from: bounds.date_from, date_to: bounds.date_to };
      if (ghlBranch) params.branch_name = ghlBranch;
      return Promise.all([
        getEmailSummary(params),
        getEmailByCampaign(params),
      ]).then(([summary, campaigns]) => ({ summary, campaigns: campaigns || [] }));
    },
    placeholderData: keepPreviousData,
  });

  const summary = emailData?.summary;
  const campaigns = emailData?.campaigns || [];

  if (isPending && !emailData) {
    return <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading...</div>;
  }
  if (!summary || summary.total_sent === 0) {
    const periodLabel = ytdB ? `${ytdB.date_from.slice(0, 4)} YTD` : "this month";
    return (
      <div className="text-center text-gray-400 py-16 text-sm">
        No email data for {periodLabel}{ghlBranch ? ` (${ghlBranch})` : ""}.
      </div>
    );
  }

  const q = search.trim().toLowerCase();
  const filteredCampaigns = q
    ? campaigns.filter(c => (c.workflow_name || "").toLowerCase().includes(q))
    : campaigns;
  const workflows = filteredCampaigns.filter(c => c.campaign_type === "workflow");
  const bulks = filteredCampaigns.filter(c => c.campaign_type === "bulk");
  // With a search active every card reflects the matched campaigns only.
  const kpi = q ? rollupCampaigns(filteredCampaigns) : summary;

  return (
    <div className={"space-y-6 transition-opacity duration-150 " + (isPlaceholderData ? "opacity-40 pointer-events-none" : "")}>
      <p className="text-xs text-gray-400">
        Workflow rows show LIFETIME totals (GHL doesn&apos;t expose per-day deltas);
        bulk rows are filtered to the selected month by schedule date.
      </p>

      {q && (
        <p className="text-xs font-medium text-indigo-600">
          Showing totals for {filteredCampaigns.length} campaign{filteredCampaigns.length === 1 ? "" : "s"} matching &quot;{search.trim()}&quot;.
        </p>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <EmailKPI label={q ? "Total Sent (filtered)" : "Total Sent"} value={fmtNum(kpi.total_sent)} />
        <EmailKPI label="Open Rate" value={pct(kpi.open_rate)} color="text-green-700" />
        <EmailKPI label="Click Rate" value={pct(kpi.click_rate)} color="text-purple-700" />
        <EmailKPI label="CRM Revenue (VND)" value={fmtNum(kpi.attributed_revenue_vnd)} color="text-emerald-700" />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-white rounded-lg border p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Workflow (lifetime)</p>
          <p className="text-lg font-bold text-indigo-700">
            {fmtNum(workflows.reduce((s, c) => s + c.sent, 0))}{" "}
            <span className="text-sm font-normal text-gray-500">emails · {workflows.length} active</span>
          </p>
        </div>
        <div className="bg-white rounded-lg border p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">Bulk (this month)</p>
          <p className="text-lg font-bold text-amber-700">
            {fmtNum(bulks.reduce((s, c) => s + c.sent, 0))}{" "}
            <span className="text-sm font-normal text-gray-500">emails · {bulks.length} sent</span>
          </p>
        </div>
      </div>

      {campaigns.length > 0 && (
        <div className="bg-white rounded-lg border overflow-x-auto">
          <div className="px-4 py-3 border-b bg-gray-50/50 flex items-center gap-3 flex-wrap">
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search campaign name..."
              className="w-full md:w-80 px-3 py-1.5 text-sm border rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500/30 focus:border-indigo-400"
            />
            <span className="text-xs text-gray-500 whitespace-nowrap">
              {filteredCampaigns.length} of {campaigns.length}
            </span>
            <button
              type="button"
              onClick={onViewRevenue}
              className="ml-auto text-xs font-medium text-indigo-600 hover:text-indigo-800 hover:underline whitespace-nowrap"
            >
              View revenue → CRM Reservations
            </button>
          </div>
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-4 py-3 font-semibold text-gray-600">Campaign</th>
                <th className="text-left px-4 py-3 font-semibold text-gray-600">Branch</th>
                <th className="text-left px-4 py-3 font-semibold text-gray-600">Type</th>
                <th className="text-right px-4 py-3 font-semibold text-gray-600">Sent</th>
                <th className="text-right px-4 py-3 font-semibold text-gray-600">Open%</th>
                <th className="text-right px-4 py-3 font-semibold text-gray-600">Click%</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {filteredCampaigns.map((c) => (
                <tr key={`${c.workflow_id}-${c.branch_name}`} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium text-gray-900 truncate max-w-[280px]">{c.workflow_name}</td>
                  <td className="px-4 py-3 text-gray-600">{c.branch_name || "—"}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                      c.campaign_type === "workflow"
                        ? "bg-indigo-50 text-indigo-700"
                        : "bg-amber-50 text-amber-700"
                    }`}>
                      {c.campaign_type}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">{fmtNum(c.sent)}</td>
                  <td className="px-4 py-3 text-right">{pct(c.open_rate)}</td>
                  <td className="px-4 py-3 text-right">{pct(c.click_rate)}</td>
                </tr>
              ))}
              {filteredCampaigns.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-gray-400 text-sm">
                    No campaigns match &quot;{search}&quot;.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
