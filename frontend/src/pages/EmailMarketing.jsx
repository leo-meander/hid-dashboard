/**
 * Email Marketing Dashboard — GHL email performance analytics.
 * Shows both Workflow campaigns (automated) and Bulk campaigns (one-time blasts).
 * Uses BranchSelector (top bar) for branch filtering.
 */
import { useEffect, useState, useMemo } from "react";
import { useBranch } from "../context/BranchContext";
import {
  getEmailSummary,
  getEmailDaily,
  getEmailByCampaign,
} from "../api/emailMarketing";
import TrendChart from "../components/TrendChart";
import SyncBadge from "../components/SyncBadge";

function pct(val) {
  if (val == null) return "—";
  return `${(val * 100).toFixed(2)}%`;
}

function vnd(val) {
  if (val == null || val === 0) return "—";
  return `${Math.round(val).toLocaleString()} ₫`;
}

function rateBand(rate, thresholds = [0.3, 0.2, 0.1]) {
  if (rate >= thresholds[0]) return "text-green-700 bg-green-50";
  if (rate >= thresholds[1]) return "text-yellow-700 bg-yellow-50";
  if (rate >= thresholds[2]) return "text-orange-700 bg-orange-50";
  return "text-red-700 bg-red-50";
}

function badRate(rate, thresholds = [0.02, 0.05, 0.10]) {
  if (rate <= thresholds[0]) return "text-green-700 bg-green-50";
  if (rate <= thresholds[1]) return "text-yellow-700 bg-yellow-50";
  if (rate <= thresholds[2]) return "text-orange-700 bg-orange-50";
  return "text-red-700 bg-red-50";
}

const TYPE_BADGE = {
  workflow: "bg-indigo-50 text-indigo-700 border-indigo-200",
  bulk: "bg-amber-50 text-amber-700 border-amber-200",
};

const BRANCH_BADGE = {
  Saigon: "bg-emerald-50 text-emerald-700 border-emerald-200",
  "1948": "bg-sky-50 text-sky-700 border-sky-200",
  Taipei: "bg-violet-50 text-violet-700 border-violet-200",
  Oani: "bg-rose-50 text-rose-700 border-rose-200",
  Osaka: "bg-orange-50 text-orange-700 border-orange-200",
};

// Map HiD branch names to GHL location names
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

const TABS = ["overview", "campaigns"];
const TAB_LABELS = { overview: "Overview", campaigns: "Campaigns" };
const TYPE_FILTERS = ["", "workflow", "bulk"];
const TYPE_LABELS = { "": "All Types", workflow: "Workflow", bulk: "Bulk" };

export default function EmailMarketing() {
  const { currentBranch, isAll } = useBranch();

  const today = new Date().toISOString().slice(0, 10);
  const [dateFrom, setDateFrom] = useState(() => {
    const d = new Date(); d.setMonth(d.getMonth() - 3);
    return d.toISOString().slice(0, 10);
  });
  const [dateTo, setDateTo] = useState(today);
  const [tab, setTab] = useState("overview");
  const [typeFilter, setTypeFilter] = useState("");

  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState(null);
  const [daily, setDaily] = useState([]);
  const [campaigns, setCampaigns] = useState([]);

  const ghlBranch = useMemo(() => isAll ? null : branchToGHL(currentBranch?.name), [currentBranch, isAll]);

  const params = useMemo(() => {
    const p = { date_from: dateFrom, date_to: dateTo };
    if (typeFilter) p.campaign_type = typeFilter;
    if (ghlBranch) p.branch_name = ghlBranch;
    return p;
  }, [dateFrom, dateTo, typeFilter, ghlBranch]);

  useEffect(() => {
    setLoading(true);

    if (tab === "overview") {
      Promise.all([
        getEmailSummary(params),
        getEmailDaily(params),
        getEmailByCampaign(params),
      ])
        .then(([s, d, c]) => {
          setSummary(s);
          setDaily(d || []);
          setCampaigns(c || []);
        })
        .catch(console.error)
        .finally(() => setLoading(false));
    } else if (tab === "campaigns") {
      getEmailByCampaign(params)
        .then(c => setCampaigns(c || []))
        .catch(console.error)
        .finally(() => setLoading(false));
    }
  }, [tab, params]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Email Marketing</h1>
          <p className="text-sm text-gray-500">
            GHL email performance{ghlBranch ? ` — ${ghlBranch}` : " — All Branches"}
            <SyncBadge timestamp={summary?.data_synced_at} />
          </p>
        </div>
        <div className="flex gap-2 items-center text-sm">
          <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}
            className="border border-gray-200 rounded-lg px-3 py-1.5" />
          <span className="text-gray-400">&rarr;</span>
          <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}
            className="border border-gray-200 rounded-lg px-3 py-1.5" />
        </div>
      </div>

      {/* Tabs + Type Filter */}
      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex gap-1 bg-gray-100 rounded-lg p-1">
          {TABS.map(t => (
            <button key={t} onClick={() => setTab(t)}
              className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${
                tab === t ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
              }`}>
              {TAB_LABELS[t]}
            </button>
          ))}
        </div>

        <div className="flex gap-1">
          {TYPE_FILTERS.map(f => (
            <button key={f} onClick={() => setTypeFilter(f)}
              className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                typeFilter === f
                  ? "bg-gray-800 text-white border-gray-800"
                  : "bg-white text-gray-600 border-gray-200 hover:border-gray-400"
              }`}>
              {TYPE_LABELS[f]}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="text-gray-400 animate-pulse">Loading...</div>
      ) : tab === "overview" ? (
        <OverviewTab summary={summary} daily={daily} campaigns={campaigns} />
      ) : (
        <CampaignsTab campaigns={campaigns} />
      )}
    </div>
  );
}

/* ── Overview Tab ────────────────────────────────────────────────────────── */
function OverviewTab({ summary, daily, campaigns }) {
  if (!summary || summary.total_sent === 0) {
    return <div className="bg-white rounded-xl border p-8 text-center text-gray-400">No email data for this range.</div>;
  }

  const kpis = [
    { label: "Total Sent", value: summary.total_sent.toLocaleString(), color: "text-indigo-700 bg-indigo-50" },
    { label: "Delivered", value: summary.total_delivered.toLocaleString(), color: "text-blue-700 bg-blue-50" },
    { label: "Open Rate", value: pct(summary.open_rate), color: rateBand(summary.open_rate, [0.25, 0.15, 0.08]) },
    { label: "Click Rate", value: pct(summary.click_rate), color: rateBand(summary.click_rate, [0.05, 0.02, 0.01]) },
    { label: "CRM Bookings", value: (summary.attributed_bookings || 0).toLocaleString(), color: "text-emerald-700 bg-emerald-50" },
    { label: "CRM Revenue (VND)", value: vnd(summary.attributed_revenue_vnd), color: "text-emerald-700 bg-emerald-50" },
    { label: "Bounce Rate", value: pct(summary.bounce_rate), color: badRate(summary.bounce_rate) },
    { label: "Unsub Rate", value: pct(summary.unsubscribe_rate), color: badRate(summary.unsubscribe_rate) },
  ];

  const workflowCampaigns = campaigns.filter(c => c.campaign_type === "workflow");
  const bulkCampaigns = campaigns.filter(c => c.campaign_type === "bulk");
  const wfSent = workflowCampaigns.reduce((s, c) => s + c.sent, 0);
  const bulkSent = bulkCampaigns.reduce((s, c) => s + c.sent, 0);

  return (
    <div className="space-y-6">
      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-8 gap-3">
        {kpis.map(k => (
          <div key={k.label} className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
            <p className="text-xs text-gray-500 mb-1">{k.label}</p>
            <p className={`text-lg font-bold ${k.color} px-2 py-0.5 rounded inline-block`}>{k.value}</p>
          </div>
        ))}
      </div>

      {/* Type Breakdown */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
          <div className="flex items-center gap-2 mb-2">
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-indigo-50 text-indigo-700 border border-indigo-200">Workflow</span>
            <span className="text-xs text-gray-400">Automated nurture flows</span>
          </div>
          <p className="text-2xl font-bold text-gray-800">{wfSent.toLocaleString()} <span className="text-sm font-normal text-gray-500">emails</span></p>
          <p className="text-xs text-gray-500 mt-1">{workflowCampaigns.length} workflows active</p>
        </div>
        <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
          <div className="flex items-center gap-2 mb-2">
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-amber-50 text-amber-700 border border-amber-200">Bulk</span>
            <span className="text-xs text-gray-400">One-time blast campaigns</span>
          </div>
          <p className="text-2xl font-bold text-gray-800">{bulkSent.toLocaleString()} <span className="text-sm font-normal text-gray-500">emails</span></p>
          <p className="text-xs text-gray-500 mt-1">{bulkCampaigns.length} campaigns sent</p>
        </div>
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TrendChart
          title="Daily Opens & Clicks"
          data={daily}
          xKey="date"
          lines={[
            { key: "opened", name: "Opens", color: "#10b981" },
            { key: "clicked", name: "Clicks", color: "#8b5cf6" },
          ]}
          formatY={v => v}
        />
        <TrendChart
          title="Daily Sent & Bounced"
          data={daily}
          xKey="date"
          bars={[{ key: "sent", name: "Sent", color: "#6366f1" }]}
          lines={[{ key: "bounced", name: "Bounced", color: "#ef4444" }]}
          formatY={v => v}
        />
      </div>

      {/* Top Campaigns */}
      {campaigns.length > 0 && <CampaignTable campaigns={campaigns.slice(0, 15)} title="Top Campaigns" />}
    </div>
  );
}

/* ── Campaigns Tab ───────────────────────────────────────────────────────── */
function CampaignsTab({ campaigns }) {
  if (campaigns.length === 0) {
    return <div className="bg-white rounded-xl border p-8 text-center text-gray-400">No campaign data.</div>;
  }
  return <CampaignTable campaigns={campaigns} title={null} />;
}

/* ── Shared Campaign Table ───────────────────────────────────────────────── */
function CampaignTable({ campaigns, title }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
      {title && <p className="px-4 pt-4 text-sm font-semibold text-gray-700">{title}</p>}
      <table className="w-full text-sm mt-2">
        <thead>
          <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-gray-100">
            <th className="px-4 py-2 text-left">Campaign</th>
            <th className="px-4 py-2 text-left">Branch</th>
            <th className="px-4 py-2 text-left">Type</th>
            <th className="px-4 py-2 text-right">Sent</th>
            <th className="px-4 py-2 text-right">Delivered</th>
            <th className="px-4 py-2 text-right">Opens</th>
            <th className="px-4 py-2 text-right">Open%</th>
            <th className="px-4 py-2 text-right">Clicks</th>
            <th className="px-4 py-2 text-right">Click%</th>
            <th className="px-4 py-2 text-right">Bounce%</th>
            <th className="px-4 py-2 text-right border-l border-gray-200">Bookings</th>
            <th className="px-4 py-2 text-right">Nights</th>
            <th className="px-4 py-2 text-right">Revenue (VND)</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-50">
          {campaigns.map(w => (
            <tr key={w.workflow_id} className="hover:bg-gray-50">
              <td className="px-4 py-2 text-gray-700 font-medium text-xs truncate max-w-[250px]">
                {w.workflow_name}
              </td>
              <td className="px-4 py-2">
                <span className={`px-2 py-0.5 rounded text-xs font-medium border ${BRANCH_BADGE[w.branch_name] || "bg-gray-50 text-gray-600 border-gray-200"}`}>
                  {w.branch_name || "—"}
                </span>
              </td>
              <td className="px-4 py-2">
                <span className={`px-2 py-0.5 rounded text-xs font-medium border ${TYPE_BADGE[w.campaign_type] || "bg-gray-50 text-gray-600"}`}>
                  {w.campaign_type}
                </span>
              </td>
              <td className="px-4 py-2 text-right tabular-nums">{w.sent.toLocaleString()}</td>
              <td className="px-4 py-2 text-right tabular-nums">{w.delivered.toLocaleString()}</td>
              <td className="px-4 py-2 text-right tabular-nums text-green-700">{w.unique_opened.toLocaleString()}</td>
              <td className="px-4 py-2 text-right">
                {w.campaign_type === "bulk" && w.open_rate === 0 ? (
                  <span className="text-xs text-gray-400">n/a</span>
                ) : (
                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${rateBand(w.open_rate, [0.25, 0.15, 0.08])}`}>
                    {pct(w.open_rate)}
                  </span>
                )}
              </td>
              <td className="px-4 py-2 text-right tabular-nums text-purple-700">{w.unique_clicked.toLocaleString()}</td>
              <td className="px-4 py-2 text-right">
                {w.campaign_type === "bulk" && w.click_rate === 0 ? (
                  <span className="text-xs text-gray-400">n/a</span>
                ) : (
                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${rateBand(w.click_rate, [0.05, 0.02, 0.01])}`}>
                    {pct(w.click_rate)}
                  </span>
                )}
              </td>
              <td className="px-4 py-2 text-right">
                <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${badRate(w.bounce_rate)}`}>
                  {pct(w.bounce_rate)}
                </span>
              </td>
              <td className="px-4 py-2 text-right tabular-nums text-emerald-700 font-medium border-l border-gray-100">
                {(w.attributed_bookings || 0) > 0 ? w.attributed_bookings.toLocaleString() : <span className="text-gray-300">—</span>}
                {(w.attributed_canceled || 0) > 0 && (
                  <span className="text-xs text-red-500 ml-1">(−{w.attributed_canceled})</span>
                )}
              </td>
              <td className="px-4 py-2 text-right tabular-nums text-gray-600">
                {(w.attributed_nights || 0) > 0 ? w.attributed_nights.toLocaleString() : <span className="text-gray-300">—</span>}
              </td>
              <td className="px-4 py-2 text-right tabular-nums text-emerald-700 font-semibold">
                {vnd(w.attributed_revenue_vnd)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
