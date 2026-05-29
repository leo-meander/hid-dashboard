/**
 * Marketing Activity — Consolidated view of Paid Ads, KOL, and CRM performance.
 * Month-based filter with MoM comparison.
 */
import { useEffect, useState, useMemo } from "react";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";
import { getMarketingActivitySummary, getCRMBranchComparison } from "../api/marketingActivity";
import { getEmailSummary, getEmailByCampaign } from "../api/emailMarketing";

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
  if (val == null) return "\u2014";
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
      {isUp ? "\u25B2" : pct < 0 ? "\u25BC" : ""}{Math.abs(pct).toFixed(1)}%
    </span>
  );
}

function RoasBadge({ value }) {
  if (value == null || value === 0) return <span className="text-gray-400">{"\u2014"}</span>;
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

export default function MarketingActivity() {
  const { isAll, selected, currency: branchCurrency } = useBranch();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("overview");

  const today = new Date();
  const currentMonthStr = today.getFullYear() + "-" + String(today.getMonth() + 1).padStart(2, "0");
  const [month, setMonth] = useState(currentMonthStr);

  const load = () => {
    setLoading(true);
    const params = { month };
    if (!isAll && selected) params.branch_id = selected;

    getMarketingActivitySummary(params)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  };

  useEffect(load, [selected, isAll, month]);

  const cur = isAll ? "VND" : (data?.currency || branchCurrency || "VND");
  const overview = data?.overview;
  const prevOverview = data?.prev_overview;
  const prevMonth = data?.prev_month;
  const crmRatePlans = data?.crm_by_rate_plan || [];

  const TABS = [
    { key: "overview", label: "Overview" },
    { key: "crm-rate-plans", label: "CRM Reservations" },
    { key: "email-stat", label: "Email Stat" },
  ];

  // Format prev month label
  const prevLabel = prevMonth ? new Date(prevMonth + "-01").toLocaleDateString("en", { month: "short", year: "numeric" }) : "";

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-lg font-bold text-gray-900">Marketing Activity</h1>
        <input type="month" value={month} onChange={(e) => setMonth(e.target.value)}
          className="border rounded px-3 py-1.5 text-sm" />
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
        <EmailStatTab month={month} onViewRevenue={() => setTab("crm-rate-plans")} />
      ) : loading ? (
        <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading...</div>
      ) : !data ? (
        <div className="text-center text-gray-400 py-16 text-sm">No data available</div>
      ) : (
        <>
          {tab === "overview" && <OverviewTab overview={overview} prevOverview={prevOverview} prevLabel={prevLabel} cur={cur} />}
          {tab === "crm-rate-plans" && <CRMRatePlansTab rows={crmRatePlans} cur={cur} month={month} />}
        </>
      )}
    </div>
  );
}

/* ── Overview Tab ──────────────────────────────────────────────────────────── */
function OverviewTab({ overview, prevOverview, prevLabel, cur }) {
  if (!overview) return null;
  const { paid_ads, kol, crm, total } = overview;
  const prev = prevOverview?.total;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KPICard label="Total Bookings" value={fmtNum(total.bookings)} prev={prev?.bookings} prevLabel={prevLabel} />
        <KPICard label={`Total Revenue (${cur})`} value={fmtNum(total.revenue)} prev={prev?.revenue} prevLabel={prevLabel} />
        <KPICard label={`Total Cost (${cur})`} value={fmtNum(total.cost)} prev={prev?.cost} prevLabel={prevLabel} />
        <KPICard label="Blended ROAS" value={total.roas ? total.roas.toFixed(2) + "x" : "\u2014"} />
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
                <td className="px-4 py-3 text-right">{hasCost ? fmtNum(d.cost) : <span className="text-gray-400">{"\u2014"}</span>}</td>
                <td className="px-4 py-3 text-right">
                  {d.roas ? <RoasBadge value={d.roas} /> :
                    hasCost && d.cost > 0 ? <RoasBadge value={d.revenue / d.cost} /> :
                    <span className="text-gray-400">{"\u2014"}</span>}
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

/* ── CRM Reservations Tab — grouped by Rate Plan Name ────────────────────── */
function CRMRatePlansTab({ rows, cur, month }) {
  const [view, setView] = useState("rate-plan");

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
        CRM reservations (CRM / MEANDER&apos;S FRIEND / Travel Guide / Grand Open / Extension Promotion) broken down by Rate Plan Name,
        filtered by Date Booked (not Stay Date).
        Excludes cancelled bookings and non-paying sources (Blogger / House Use / Special Case).
      </p>
      {hasZeroRevenueRow && (
        <p className="text-xs text-gray-500 italic">
          <span className="font-semibold not-italic">Note:</span> rows marked with{" "}
          <span className="font-semibold text-amber-600">0*</span> in Revenue have bookings whose
          accommodation total = 0 in Cloudbeds (typically complimentary stays, voucher redemptions,
          or comp event guests where the room rate was waived).
        </p>
      )}
      <div className="bg-white rounded-lg border overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="text-left px-4 py-3 font-semibold text-gray-600">Rate Plan Name</th>
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
                  <td className="px-4 py-3 font-medium text-gray-900">{r.rate_plan_name}</td>
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
              <td className="px-4 py-3 text-right">{fmtNum(totals.bookings)}</td>
              <td className="px-4 py-3 text-right">{fmtNum(totals.nights)}</td>
              <td className="px-4 py-3 text-right">{fmtNum(totals.revenue)}</td>
              <td className="px-4 py-3 text-right">{fmtNum(totalAdr)}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ── CRM Branch Comparison — campaign × branch and month × branch ───────── */
function shortBranchName(name) {
  return (name || "").replace(/^meander\s+/i, "").trim() || name || "—";
}

const COMPARE_METRICS = [
  { key: "revenue", label: "Revenue" },
  { key: "bookings", label: "Bookings" },
  { key: "nights", label: "Nights" },
];

// Per-row heatmap: shade a cell by its share of that row's leading branch.
// Darker indigo = higher within the row, so the winner per campaign/month
// pops out and you can read "hơn thua" at a glance.
function heatStyle(value, rowMax) {
  if (!value || value <= 0 || rowMax <= 0) return { style: undefined, cls: "text-gray-300" };
  const intensity = value / rowMax;
  const alpha = (0.1 + 0.5 * intensity).toFixed(3);
  const dark = intensity >= 0.65;
  return {
    style: { backgroundColor: `rgba(79,70,229,${alpha})` },
    cls: dark ? "text-white font-semibold" : intensity >= 0.999 ? "text-indigo-900 font-semibold" : "text-gray-700",
  };
}

function ComparisonMatrix({ title, subtitle, branches, rows, rowLabel, metric }) {
  const cellVal = (cell) => (cell ? cell[metric] || 0 : 0);
  const colTotal = (bid) => rows.reduce((s, r) => s + cellVal(r.cells[bid]), 0);
  const grandTotal = rows.reduce((s, r) => s + (r.total?.[metric] || 0), 0);

  return (
    <div className="bg-white rounded-lg border overflow-x-auto">
      <div className="px-4 py-3 border-b bg-gray-50/50 flex items-start justify-between gap-3 flex-wrap">
        <div>
          <p className="text-sm font-semibold text-gray-700">{title}</p>
          {subtitle && <p className="text-xs text-gray-400 mt-0.5">{subtitle}</p>}
        </div>
        <div className="flex items-center gap-1.5 text-[11px] text-gray-400 whitespace-nowrap">
          <span>low</span>
          <span className="inline-block w-5 h-3 rounded-sm" style={{ background: "rgba(79,70,229,0.12)" }} />
          <span className="inline-block w-5 h-3 rounded-sm" style={{ background: "rgba(79,70,229,0.32)" }} />
          <span className="inline-block w-5 h-3 rounded-sm" style={{ background: "rgba(79,70,229,0.6)" }} />
          <span>high (per row)</span>
        </div>
      </div>
      <table className="w-full text-sm">
        <thead className="bg-gray-50">
          <tr>
            <th className="text-left px-4 py-2.5 font-semibold text-gray-600">{rowLabel}</th>
            {branches.map((b) => (
              <th key={b.branch_id} className="text-right px-4 py-2.5 font-semibold text-gray-600 whitespace-nowrap">
                {shortBranchName(b.name)}
              </th>
            ))}
            <th className="text-right px-4 py-2.5 font-semibold text-gray-600">Total</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((r, i) => {
            const rowMax = Math.max(0, ...branches.map((b) => cellVal(r.cells[b.branch_id])));
            return (
              <tr key={i}>
                <td className="px-4 py-2.5 font-medium text-gray-900 whitespace-nowrap">{r[rowLabel === "Month" ? "month" : "rate_plan_name"]}</td>
                {branches.map((b) => {
                  const v = cellVal(r.cells[b.branch_id]);
                  const { style, cls } = heatStyle(v, rowMax);
                  return (
                    <td key={b.branch_id} style={style} className={`px-4 py-2.5 text-right tabular-nums ${cls}`}>
                      {fmtNum(v)}
                    </td>
                  );
                })}
                <td className="px-4 py-2.5 text-right tabular-nums font-semibold text-gray-900">{fmtNum(r.total?.[metric] || 0)}</td>
              </tr>
            );
          })}
          <tr className="bg-gray-50 font-semibold">
            <td className="px-4 py-2.5">Total</td>
            {branches.map((b) => (
              <td key={b.branch_id} className="px-4 py-2.5 text-right tabular-nums">{fmtNum(colTotal(b.branch_id))}</td>
            ))}
            <td className="px-4 py-2.5 text-right tabular-nums">{fmtNum(grandTotal)}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function CRMBranchComparison({ month }) {
  const { branches: allowedBranches } = useBranch();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [metric, setMetric] = useState("revenue");

  useEffect(() => {
    setLoading(true);
    getCRMBranchComparison({ month, months_back: 6 })
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [month]);

  // Only show branches this user is allowed to see, in the backend's display order.
  const branches = useMemo(() => {
    if (!data?.branches) return [];
    const allowedIds = new Set(allowedBranches.map((b) => b.id));
    return data.branches.filter((b) => allowedIds.size === 0 || allowedIds.has(b.branch_id));
  }, [data, allowedBranches]);

  if (loading) {
    return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading...</div>;
  }
  if (!data || branches.length === 0) {
    return <p className="text-gray-400 text-sm text-center py-8">No CRM comparison data.</p>;
  }

  const hasCampaign = (data.by_campaign || []).length > 0;
  const hasMonth = (data.by_month || []).length > 0;

  return (
    <div className="space-y-4">
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

function EmailStatTab({ month, onViewRevenue }) {
  const { currentBranch, isAll } = useBranch();
  const [summary, setSummary] = useState(null);
  const [campaigns, setCampaigns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

  const ghlBranch = useMemo(
    () => isAll ? null : branchToGHL(currentBranch?.name),
    [currentBranch, isAll]
  );

  useEffect(() => {
    setLoading(true);
    const { date_from, date_to } = monthBounds(month);
    const params = { date_from, date_to };
    if (ghlBranch) params.branch_name = ghlBranch;

    Promise.all([
      getEmailSummary(params),
      getEmailByCampaign(params),
    ])
      .then(([s, c]) => {
        setSummary(s);
        setCampaigns(c || []);
      })
      .catch(() => {
        setSummary(null);
        setCampaigns([]);
      })
      .finally(() => setLoading(false));
  }, [month, ghlBranch]);

  if (loading) {
    return <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading...</div>;
  }
  if (!summary || summary.total_sent === 0) {
    return (
      <div className="text-center text-gray-400 py-16 text-sm">
        No email data for this month{ghlBranch ? ` (${ghlBranch})` : ""}.
      </div>
    );
  }

  const workflows = campaigns.filter(c => c.campaign_type === "workflow");
  const bulks = campaigns.filter(c => c.campaign_type === "bulk");
  const q = search.trim().toLowerCase();
  const filteredCampaigns = q
    ? campaigns.filter(c => (c.workflow_name || "").toLowerCase().includes(q))
    : campaigns;

  return (
    <div className="space-y-6">
      <p className="text-xs text-gray-400">
        Workflow rows show LIFETIME totals (GHL doesn&apos;t expose per-day deltas);
        bulk rows are filtered to the selected month by schedule date.
      </p>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <EmailKPI label="Total Sent" value={fmtNum(summary.total_sent)} />
        <EmailKPI label="Open Rate" value={pct(summary.open_rate)} color="text-green-700" />
        <EmailKPI label="Click Rate" value={pct(summary.click_rate)} color="text-purple-700" />
        <EmailKPI label="CRM Revenue (VND)" value={fmtNum(summary.attributed_revenue_vnd)} color="text-emerald-700" />
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
