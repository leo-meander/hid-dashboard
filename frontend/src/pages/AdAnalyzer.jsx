/**
 * AdAnalyzer — AI-powered ad analysis dashboard.
 * Search + Filters, Funnel chart, Angle performance, TA×Angle heatmap,
 * Detailed AI evaluation with optimization actions.
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useBranch } from "../context/BranchContext";
import SyncBadge from "../components/SyncBadge";
import { listResults, getInsights, analyzeBatch } from "../api/analyzer";
import { getTAClasses, AUDIENCES } from "../constants/audiences";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from "recharts";

const REC_COLORS = {
  scale_up: "bg-green-100 text-green-700",
  optimize: "bg-amber-100 text-amber-700",
  pause: "bg-red-100 text-red-700",
  reduce: "bg-orange-100 text-orange-700",
  maintain: "bg-blue-100 text-blue-700",
  test_new: "bg-blue-100 text-blue-700",
  insufficient_data: "bg-gray-100 text-gray-500",
};
const REC_LABELS = {
  scale_up: "Scale Up", optimize: "Optimize", pause: "Pause",
  reduce: "Reduce", maintain: "Maintain", test_new: "Test New",
  insufficient_data: "Need Data",
};
const VERDICT_COLORS = {
  STRONG: "bg-green-100 text-green-700", MODERATE: "bg-amber-100 text-amber-700",
  WEAK: "bg-red-100 text-red-700", INSUFFICIENT_DATA: "bg-gray-100 text-gray-500",
};
const PRIORITY_COLORS = { HIGH: "text-red-600", MEDIUM: "text-amber-600", LOW: "text-gray-500" };

const ANGLE_COLORS = ["#6366f1", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#ec4899", "#06b6d4"];
const REC_TYPES = ["scale_up", "optimize", "pause", "reduce", "maintain", "test_new", "insufficient_data"];
const VERDICTS = ["WIN", "TEST", "LOSE"];

const fmt = (v) => v != null ? new Intl.NumberFormat("en").format(Math.round(v)) : "—";

export default function AdAnalyzer() {
  const { selected, isAll } = useBranch();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [analyzing, setAnalyzing] = useState(false);
  const [expanded, setExpanded] = useState(null); // expanded card id

  // Filters from URL params
  const search = searchParams.get("search") || "";
  const fRecType = searchParams.get("recommendation_type") || "";
  const fVerdict = searchParams.get("verdict") || "";

  const setFilter = (key, val) => {
    const p = new URLSearchParams(searchParams);
    if (val) p.set(key, val); else p.delete(key);
    setSearchParams(p);
  };

  const branchParam = !isAll && selected ? selected : undefined;
  const resultsParams = (() => {
    const p = {};
    if (branchParam) p.branch_id = branchParam;
    if (search) p.search = search;
    if (fRecType) p.recommendation_type = fRecType;
    if (fVerdict) p.verdict = fVerdict;
    return p;
  })();

  const insightsQuery = useQuery({
    queryKey: ["analyzer", "insights", branchParam],
    queryFn: () => getInsights(branchParam ? { branch_id: branchParam } : {}),
  });
  const resultsQuery = useQuery({
    queryKey: ["analyzer", "results", resultsParams],
    queryFn: () => listResults(resultsParams),
  });

  const insights = insightsQuery.data || null;
  const results = resultsQuery.data || [];
  const loading = insightsQuery.isLoading || resultsQuery.isLoading;

  const invalidateAnalyzer = () => {
    queryClient.invalidateQueries({ queryKey: ["analyzer"] });
  };

  const handleBatchAnalyze = () => {
    if (!selected || isAll) return;
    setAnalyzing(true);
    analyzeBatch(selected, true)
      .then(() => setTimeout(invalidateAnalyzer, 5000))
      .finally(() => setAnalyzing(false));
  };

  if (loading) return <div className="text-gray-400 text-sm animate-pulse">Loading analyzer...</div>;

  const funnel = insights?.funnel_aggregate || {};
  const anglePerfData = insights?.angle_performance || [];
  const taAngleData = insights?.ta_angle_matrix || [];
  const recSummary = insights?.recommendation_summary || {};

  const funnelStages = [
    { label: "Impressions", value: funnel.impressions, rate: null },
    { label: "Clicks", value: funnel.clicks, rate: funnel.ctr, rateLabel: "CTR" },
    { label: "LP Views", value: funnel.lp_views, rate: funnel.lp_view_rate, rateLabel: "LP %" },
    { label: "Add to Cart", value: funnel.add_to_cart, rate: funnel.atc_rate, rateLabel: "ATC %" },
    { label: "Checkout", value: funnel.checkout, rate: funnel.checkout_rate, rateLabel: "CO %" },
    { label: "Purchase", value: funnel.purchases, rate: funnel.purchase_rate, rateLabel: "Purch %" },
  ];

  const allAngles = [...new Set(taAngleData.map(d => d.angle))];
  const allTAs = [...new Set(taAngleData.map(d => d.ta))];

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Ad Analyzer</h1>
          <p className="text-sm text-gray-400">
            {insights?.total_analyzed || 0} ads analyzed · Last 14 days
            <SyncBadge timestamp={insights?.data_synced_at} />
          </p>
        </div>
        <button onClick={handleBatchAnalyze}
          disabled={analyzing || isAll || !selected}
          className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50">
          {analyzing ? "Analyzing..." : "⬡ Analyze All Ads"}
        </button>
      </div>

      {/* Search + Filters */}
      <div className="flex gap-2 mb-5 flex-wrap">
        <input type="text" placeholder="Search combo, headline, ad name..." value={search}
          onChange={e => setFilter("search", e.target.value)}
          className="border rounded-lg px-3 py-1.5 text-sm w-64 focus:border-indigo-400 focus:outline-none" />
        <select value={fRecType} onChange={e => setFilter("recommendation_type", e.target.value)}
          className="border rounded px-2 py-1.5 text-sm">
          <option value="">All Recommendations</option>
          {REC_TYPES.map(t => <option key={t} value={t}>{REC_LABELS[t]}</option>)}
        </select>
        <select value={fVerdict} onChange={e => setFilter("verdict", e.target.value)}
          className="border rounded px-2 py-1.5 text-sm">
          <option value="">All Verdicts</option>
          {VERDICTS.map(v => <option key={v} value={v}>{v}</option>)}
        </select>
        {(search || fRecType || fVerdict) && (
          <button onClick={() => setSearchParams({})} className="text-xs text-gray-400 hover:text-red-500">Clear filters</button>
        )}
      </div>

      {/* Funnel Visualization */}
      <div className="bg-white rounded-xl border p-5 mb-6">
        <h2 className="text-sm font-semibold text-gray-700 mb-4">Conversion Funnel (Aggregated)</h2>
        <div className="flex items-end gap-1">
          {funnelStages.map((stage, i) => {
            const maxVal = funnelStages[0].value || 1;
            const pct = stage.value ? Math.max((stage.value / maxVal) * 100, 8) : 8;
            const isLow = stage.rate != null && (
              (stage.rateLabel === "CTR" && stage.rate < 1) ||
              (stage.rateLabel === "ATC %" && stage.rate < 5) ||
              (stage.rateLabel === "CO %" && stage.rate < 50) ||
              (stage.rateLabel === "Purch %" && stage.rate < 60)
            );
            return (
              <div key={i} className="flex-1 flex flex-col items-center gap-1">
                <span className="text-xs font-medium text-gray-700">{fmt(stage.value)}</span>
                <div className={`w-full rounded-t-md transition-all ${isLow ? "bg-red-400" : "bg-indigo-500"}`}
                  style={{ height: `${pct}px`, minHeight: "8px", maxHeight: "120px" }} />
                <span className="text-[10px] text-gray-500 text-center">{stage.label}</span>
                {stage.rate != null && (
                  <span className={`text-[10px] font-medium ${isLow ? "text-red-600" : "text-green-600"}`}>{stage.rate}%</span>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        {/* Angle Performance Chart */}
        <div className="bg-white rounded-xl border p-5">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Angle Performance (Avg CTR %)</h2>
          {anglePerfData.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={anglePerfData} layout="vertical" margin={{ left: 80 }}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis type="number" tick={{ fontSize: 11 }} />
                <YAxis type="category" dataKey="angle" tick={{ fontSize: 11 }} width={75} />
                <Tooltip formatter={(v) => `${v}%`} />
                <Bar dataKey="avg_ctr" radius={[0, 4, 4, 0]}>
                  {anglePerfData.map((_, i) => <Cell key={i} fill={ANGLE_COLORS[i % ANGLE_COLORS.length]} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <p className="text-gray-400 text-sm">No angle data yet.</p>}
        </div>

        {/* Recommendation Summary */}
        <div className="bg-white rounded-xl border p-5">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">Budget Recommendations</h2>
          <div className="space-y-2">
            {Object.entries(recSummary).map(([type, count]) => (
              <div key={type} className="flex items-center justify-between cursor-pointer hover:bg-gray-50 rounded px-2 py-1"
                onClick={() => setFilter("recommendation_type", type)}>
                <span className={`text-xs px-2 py-1 rounded font-medium ${REC_COLORS[type] || "bg-gray-100 text-gray-600"}`}>
                  {REC_LABELS[type] || type}
                </span>
                <span className="text-sm font-semibold text-gray-700">{count}</span>
              </div>
            ))}
            {Object.keys(recSummary).length === 0 && <p className="text-gray-400 text-sm">No recommendations yet.</p>}
          </div>
        </div>
      </div>

      {/* TA × Angle Matrix */}
      {taAngleData.length > 0 && (
        <div className="bg-white rounded-xl border p-5 mb-6 overflow-x-auto">
          <h2 className="text-sm font-semibold text-gray-700 mb-4">TA × Angle Matrix (Avg ROAS)</h2>
          <table className="text-xs w-full">
            <thead>
              <tr>
                <th className="text-left py-1 px-2 text-gray-500">TA \ Angle</th>
                {allAngles.map(a => <th key={a} className="py-1 px-2 text-gray-500 text-center">{a}</th>)}
              </tr>
            </thead>
            <tbody>
              {allTAs.map(ta => (
                <tr key={ta}>
                  <td className="py-1 px-2 font-medium">{ta}</td>
                  {allAngles.map(angle => {
                    const cell = taAngleData.find(d => d.ta === ta && d.angle === angle);
                    if (!cell) return <td key={angle} className="py-1 px-2 text-center text-gray-300">—</td>;
                    const roas = cell.avg_roas;
                    const bg = roas >= 3 ? "bg-green-100 text-green-700" : roas >= 1 ? "bg-amber-100 text-amber-700" : "bg-red-100 text-red-700";
                    return (
                      <td key={angle} className="py-1 px-2 text-center">
                        <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-medium ${bg}`}>
                          {roas.toFixed(1)}x <span className="text-[9px] opacity-60">({cell.count})</span>
                        </span>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Analysis Results */}
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-gray-700">Campaign Analysis ({results.length})</h2>
      </div>
      {results.length === 0 ? (
        <div className="text-gray-400 text-sm">No results found. {!insights?.total_analyzed ? 'Click "Analyze All Ads" to start.' : "Try adjusting filters."}</div>
      ) : (
        <div className="space-y-4">
          {results.map(r => {
            const fa = r.funnel_analysis || {};
            const isExpanded = expanded === r.id;
            const perfVerdict = fa.performance_verdict;
            const actions = fa.optimization_actions || [];
            const tests = fa.testing_suggestions || [];

            return (
              <div key={r.id} className="bg-white rounded-xl border hover:shadow-md transition-shadow"
                onClick={() => setExpanded(isExpanded ? null : r.id)}>
                {/* Card Header */}
                <div className="p-4 cursor-pointer">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      <span className="font-mono text-indigo-600 text-[10px]">{r.combo_code}</span>
                      {r.combo?.meta_ad_name && (
                        <span className="text-xs font-medium text-gray-800 truncate" title={r.combo.meta_ad_name}>{r.combo.meta_ad_name}</span>
                      )}
                      {perfVerdict && (
                        <span className={`text-[10px] px-2 py-0.5 rounded font-semibold ${VERDICT_COLORS[perfVerdict] || "bg-gray-100"}`}>
                          {perfVerdict}
                        </span>
                      )}
                      <span className={`text-[10px] px-2 py-0.5 rounded font-medium ${REC_COLORS[r.recommendation_type] || "bg-gray-100"}`}>
                        {REC_LABELS[r.recommendation_type] || r.recommendation_type}
                      </span>
                    </div>
                    <div className="flex items-center gap-3 text-xs">
                      <span className={`font-semibold ${r.combo?.roas >= 3 ? "text-green-600" : r.combo?.roas >= 1 ? "text-amber-500" : "text-red-500"}`}>
                        ROAS {r.combo?.roas?.toFixed(2) || "—"}
                      </span>
                      <span className="text-gray-400">Spend {fmt(r.combo?.spend_vnd)}</span>
                      <span className="text-gray-400">{r.combo?.purchases ?? 0} purchases</span>
                      <span className="text-gray-300">{isExpanded ? "▲" : "▼"}</span>
                    </div>
                  </div>

                  {/* Headline + tags */}
                  <p className="text-sm font-medium truncate">{r.combo?.copy_headline || "—"}</p>
                  <div className="flex gap-1 mt-1.5 flex-wrap">
                    {r.detected_angles?.map(a => (
                      <span key={a} className="text-[10px] px-1.5 py-0.5 bg-violet-50 text-violet-600 rounded">{a}</span>
                    ))}
                    {r.detected_ta?.map(ta => {
                      const tc = getTAClasses(ta);
                      return <span key={ta} className={`text-[10px] px-1.5 py-0.5 rounded ${tc.bg} ${tc.text}`}>{ta}</span>;
                    })}
                  </div>

                  {/* AI Performance Summary (always visible) */}
                  {fa.performance_summary && (
                    <p className="text-xs text-gray-600 mt-2 leading-relaxed">{fa.performance_summary}</p>
                  )}
                </div>

                {/* Expanded Detail Panel */}
                {isExpanded && (
                  <div className="border-t px-4 pb-4 pt-3 space-y-4 bg-gray-50/50">
                    {/* Funnel Diagnosis */}
                    {fa.funnel_diagnosis && (
                      <div className="bg-amber-50 border border-amber-200 rounded-lg p-3">
                        <p className="text-xs font-semibold text-amber-700 mb-1">Funnel Bottleneck</p>
                        <p className="text-xs text-amber-800">{fa.funnel_diagnosis}</p>
                      </div>
                    )}

                    {/* Optimization Actions */}
                    {actions.length > 0 && (
                      <div>
                        <p className="text-xs font-semibold text-gray-700 mb-2">Optimization Actions</p>
                        <div className="space-y-2">
                          {actions.map((act, i) => (
                            <div key={i} className="flex gap-3 bg-white rounded-lg border p-3">
                              <div className="flex-shrink-0">
                                <span className={`text-[10px] font-bold ${PRIORITY_COLORS[act.priority] || "text-gray-500"}`}>
                                  {act.priority}
                                </span>
                              </div>
                              <div className="flex-1 min-w-0">
                                <span className="text-[10px] px-1.5 py-0.5 bg-indigo-50 text-indigo-600 rounded">{act.area}</span>
                                <p className="text-xs text-gray-700 mt-1">{act.action}</p>
                                {act.expected_impact && (
                                  <p className="text-[10px] text-green-600 mt-0.5">Expected: {act.expected_impact}</p>
                                )}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Budget Recommendation */}
                    {fa.budget_recommendation && (
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-semibold text-gray-700">Budget:</span>
                        <span className={`text-xs px-2 py-0.5 rounded font-medium ${REC_COLORS[fa.budget_recommendation?.toLowerCase()] || "bg-gray-100"}`}>
                          {fa.budget_recommendation}
                        </span>
                        {fa.budget_reasoning && <span className="text-xs text-gray-500">— {fa.budget_reasoning}</span>}
                      </div>
                    )}

                    {/* Testing Suggestions */}
                    {tests.length > 0 && (
                      <div>
                        <p className="text-xs font-semibold text-gray-700 mb-1">A/B Test Ideas</p>
                        <ul className="space-y-1">
                          {tests.map((t, i) => (
                            <li key={i} className="text-xs text-gray-600 flex items-start gap-1.5">
                              <span className="text-blue-400 mt-px">◆</span> {t}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {/* Keypoints */}
                    {r.keypoints?.length > 0 && (
                      <div>
                        <p className="text-xs font-semibold text-gray-700 mb-1">Ad Keypoints</p>
                        <ul className="space-y-0.5">
                          {r.keypoints.map((kp, i) => (
                            <li key={i} className="text-xs text-gray-500 flex items-start gap-1.5">
                              <span className="text-indigo-400 mt-px">•</span> {kp}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
