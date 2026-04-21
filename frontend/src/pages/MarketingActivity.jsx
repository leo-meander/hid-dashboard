/**
 * Marketing Activity — Consolidated view of Paid Ads, KOL, and CRM performance.
 * Month-based filter with MoM comparison.
 */
import { useEffect, useState, useMemo } from "react";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";
import { getMarketingActivitySummary } from "../api/marketingActivity";

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

function ActivityBadges({ activities }) {
  const colors = {
    "Paid Ads": "bg-blue-100 text-blue-700",
    KOL: "bg-purple-100 text-purple-700",
    CRM: "bg-emerald-100 text-emerald-700",
  };
  return (
    <div className="flex gap-1 flex-wrap">
      {activities.map((a) => (
        <span key={a} className={"px-2 py-0.5 rounded text-xs font-medium " + (colors[a] || "bg-gray-100 text-gray-600")}>{a}</span>
      ))}
    </div>
  );
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
  const [filterCountry, setFilterCountry] = useState("");

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
  const monthly = data?.monthly_by_country || [];
  const suggestions = data?.kol_suggestions || [];
  const crmRatePlans = data?.crm_by_rate_plan || [];

  const countries = useMemo(() => {
    const set = new Set(monthly.map((r) => r.country));
    return [...set].sort();
  }, [monthly]);

  const filteredMonthly = useMemo(() => {
    if (!filterCountry) return monthly;
    return monthly.filter((r) => r.country === filterCountry);
  }, [monthly, filterCountry]);

  // Group KOL suggestions by country
  const suggestionsByCountry = useMemo(() => {
    const groups = {};
    for (const s of suggestions) {
      const c = s.country || "Unknown";
      if (!groups[c]) groups[c] = [];
      groups[c].push(s);
    }
    return Object.entries(groups).sort((a, b) => a[0].localeCompare(b[0]));
  }, [suggestions]);

  const TABS = [
    { key: "overview", label: "Overview" },
    { key: "monthly", label: "By Country" },
    { key: "crm-rate-plans", label: "CRM Reservations" },
    { key: "kol-suggest", label: "KOL Suggestions" },
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

      {loading ? (
        <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading...</div>
      ) : !data ? (
        <div className="text-center text-gray-400 py-16 text-sm">No data available</div>
      ) : (
        <>
          {tab === "overview" && <OverviewTab overview={overview} prevOverview={prevOverview} prevLabel={prevLabel} cur={cur} />}
          {tab === "monthly" && (
            <MonthlyTab rows={filteredMonthly} countries={countries}
              filterCountry={filterCountry} setFilterCountry={setFilterCountry} cur={cur} />
          )}
          {tab === "crm-rate-plans" && <CRMRatePlansTab rows={crmRatePlans} cur={cur} />}
          {tab === "kol-suggest" && <KOLSuggestTab groups={suggestionsByCountry} />}
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
              { label: "CRM", color: "bg-emerald-500", data: crm, prev: prevOverview?.crm, hasCost: false },
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

/* ── By Country Tab ────────────────────────────────────────────────────────── */
function MonthlyTab({ rows, countries, filterCountry, setFilterCountry, cur }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <label className="text-sm text-gray-600">Country:</label>
        <select value={filterCountry} onChange={(e) => setFilterCountry(e.target.value)}
          className="border rounded px-2 py-1 text-sm">
          <option value="">All</option>
          {countries.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>

      {rows.length === 0 ? (
        <p className="text-gray-400 text-sm text-center py-8">No marketing activity found for this month.</p>
      ) : (
        <div className="bg-white rounded-lg border overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="text-left px-4 py-3 font-semibold text-gray-600">Country</th>
                <th className="text-left px-4 py-3 font-semibold text-gray-600">Activities</th>
                <th className="text-right px-4 py-3 font-semibold text-gray-600">Bookings</th>
                <th className="text-right px-4 py-3 font-semibold text-gray-600">Revenue ({cur})</th>
                <th className="text-right px-4 py-3 font-semibold text-gray-600">Cost ({cur})</th>
                <th className="text-right px-4 py-3 font-semibold text-gray-600">ROAS</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((r, i) => (
                <tr key={i} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium">{r.country}</td>
                  <td className="px-4 py-3"><ActivityBadges activities={r.activities} /></td>
                  <td className="px-4 py-3 text-right">{fmtNum(r.total_bookings)}</td>
                  <td className="px-4 py-3 text-right">{fmtNum(r.total_revenue)}</td>
                  <td className="px-4 py-3 text-right">{r.total_cost > 0 ? fmtNum(r.total_cost) : "\u2014"}</td>
                  <td className="px-4 py-3 text-right"><RoasBadge value={r.roas} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ── CRM Reservations Tab — grouped by Rate Plan Name ────────────────────── */
function CRMRatePlansTab({ rows, cur }) {
  if (!rows || rows.length === 0) {
    return (
      <p className="text-gray-400 text-sm text-center py-8">
        No CRM reservations found for this month.
      </p>
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

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-500">
        CRM reservations (CRM / MEANDER&apos;S FRIEND / Travel Guide / Grand Open) broken down by Rate Plan Name.
        Excludes cancelled bookings and non-paying sources (Blogger / House Use / Special Case).
      </p>
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
            {rows.map((r, i) => (
              <tr key={i} className="hover:bg-gray-50">
                <td className="px-4 py-3 font-medium text-gray-900">{r.rate_plan_name}</td>
                <td className="px-4 py-3 text-right">{fmtNum(r.bookings)}</td>
                <td className="px-4 py-3 text-right">{fmtNum(r.nights)}</td>
                <td className="px-4 py-3 text-right">{fmtNum(r.revenue)}</td>
                <td className="px-4 py-3 text-right">{fmtNum(r.adr)}</td>
              </tr>
            ))}
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

/* ── KOL Suggestions Tab — grouped by Country ────────────────────────────── */
function KOLSuggestTab({ groups }) {
  if (groups.length === 0) {
    return (
      <p className="text-gray-400 text-sm text-center py-8">
        No KOL suggestions available. KOLs already used in paid ads are excluded.
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <p className="text-sm text-gray-500">
        KOLs with organic bookings not yet used in Paid Ads — grouped by country, sorted by revenue.
      </p>
      {groups.map(([country, rows]) => (
        <div key={country}>
          <h3 className="text-sm font-semibold text-gray-700 mb-2 flex items-center gap-2">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-indigo-500" />
            {country}
            <span className="text-xs text-gray-400 font-normal">({rows.length} KOL{rows.length > 1 ? "s" : ""})</span>
          </h3>
          <div className="bg-white rounded-lg border overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50">
                <tr>
                  <th className="text-left px-4 py-2.5 font-semibold text-gray-600">KOL</th>
                  <th className="text-left px-4 py-2.5 font-semibold text-gray-600">Branch</th>
                  <th className="text-right px-4 py-2.5 font-semibold text-gray-600">Organic Bookings</th>
                  <th className="text-right px-4 py-2.5 font-semibold text-gray-600">Organic Revenue (VND)</th>
                  <th className="text-left px-4 py-2.5 font-semibold text-gray-600">Nationality</th>
                  <th className="text-left px-4 py-2.5 font-semibold text-gray-600">Usage Rights Until</th>
                  <th className="text-center px-4 py-2.5 font-semibold text-gray-600">Ads Eligible</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {rows.map((r, i) => (
                  <tr key={i} className="hover:bg-gray-50">
                    <td className="px-4 py-2.5 font-medium text-gray-900">{r.kol_name.replace("KOL_", "")}</td>
                    <td className="px-4 py-2.5 text-gray-600">{r.branch}</td>
                    <td className="px-4 py-2.5 text-right font-medium">{fmtNum(r.organic_bookings)}</td>
                    <td className="px-4 py-2.5 text-right">{fmtNum(r.organic_revenue_vnd)}</td>
                    <td className="px-4 py-2.5 text-gray-600">{r.kol_nationality || "\u2014"}</td>
                    <td className="px-4 py-2.5">
                      {r.usage_rights_until ? (
                        <span className={new Date(r.usage_rights_until) < new Date() ? "text-red-600" : "text-gray-700"}>
                          {r.usage_rights_until}
                        </span>
                      ) : <span className="text-gray-400">{"\u2014"}</span>}
                    </td>
                    <td className="px-4 py-2.5 text-center">
                      {r.paid_ads_eligible
                        ? <span className="text-green-600 font-medium">Yes</span>
                        : <span className="text-gray-400">No</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}
