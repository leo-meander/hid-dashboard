/**
 * AdCombos — PRIMARY Phase 4 page. Copy × Material = verdict.
 * Filter bar, combo cards, detail drawer, add combo modal.
 */
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useBranch } from "../context/BranchContext";
import { useAuth } from "../context/AuthContext";
import { listCombos, getCombo, createCombo, updateCombo, deleteCombo, triggerSync, comboInsights, submitForApproval, reviewCombo, listPending, listUsers, autoClassifyAngles } from "../api/combos";
import { listCopies } from "../api/copies";
import { listMaterials } from "../api/materials";
import { listAngles } from "../api/angles";
import { listKolRecords } from "../api/kol";
import ComboCard from "../components/ComboCard";
import VerdictBadge from "../components/VerdictBadge";
import { AUDIENCES } from "../constants/audiences";
const CHANNELS = ["Facebook", "Instagram", "TikTok", "YouTube", "Meta", "Google"];
const LANGUAGES = ["Vietnamese", "English", "Japanese", "Korean", "Thai", "Indonesian", "Malay"];
const COUNTRIES = ["Vietnam", "Philippines", "Indonesia", "Australia", "Taiwan", "South Korea", "Japan", "Thailand", "Malaysia", "Singapore", "United Kingdom", "United States", "India", "China"];
const VERDICTS = ["WIN", "TEST", "LOSE"];
const RUN_STATUSES = ["Active", "Paused", "Ended"];

export default function AdCombos() {
  const { selected, isAll } = useBranch();
  const { user, isAdmin } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();

  const [detail, setDetail] = useState(null);
  const [showAdd, setShowAdd] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState(null);
  const [tab, setTab] = useState("all"); // "all" | "pending"

  // Filters from URL params
  const f = {
    target_audience: searchParams.get("target_audience") || "",
    channel: searchParams.get("channel") || "",
    language: searchParams.get("language") || "",
    country_target: searchParams.get("country_target") || "",
    angle_id: searchParams.get("angle_id") || "",
    verdict: searchParams.get("verdict") || "",
    run_status: searchParams.get("run_status") || "",
  };

  const setFilter = (key, val) => {
    const p = new URLSearchParams(searchParams);
    if (val) p.set(key, val); else p.delete(key);
    setSearchParams(p);
  };

  const branchParam = !isAll && selected ? selected : undefined;
  const combosParams = (() => {
    const p = { ...f };
    if (branchParam) p.branch_id = branchParam;
    Object.keys(p).forEach(k => { if (!p[k]) delete p[k]; });
    return p;
  })();

  const combosQuery = useQuery({
    queryKey: ["combos", combosParams],
    queryFn: () => listCombos(combosParams),
  });
  const insightsQuery = useQuery({
    queryKey: ["combos", "insights", branchParam],
    queryFn: () => comboInsights({ branch_id: branchParam }),
  });
  const anglesQuery = useQuery({
    queryKey: ["angles", branchParam],
    queryFn: () => listAngles({ branch_id: branchParam }),
  });
  const pendingQuery = useQuery({
    queryKey: ["combos", "pending", user?.id],
    queryFn: () => listPending({ reviewer_id: user?.id }),
    enabled: !!user,
  });

  const combos = combosQuery.data || [];
  const insights = insightsQuery.data || null;
  const angles = anglesQuery.data || [];
  const pendingCombos = pendingQuery.data || [];
  const loading = combosQuery.isLoading || insightsQuery.isLoading || anglesQuery.isLoading;
  const pendingLoading = pendingQuery.isLoading;

  const invalidateCombos = () => {
    queryClient.invalidateQueries({ queryKey: ["combos"] });
  };

  const openDetail = (id) => {
    getCombo(id).then(setDetail);
  };

  const handleDelete = (id, code) => {
    if (!confirm(`Delete combo ${code}? This cannot be undone.`)) return;
    deleteCombo(id).then(invalidateCombos);
  };

  const saveVerdict = (id, verdict, notes) => {
    updateCombo(id, { verdict, verdict_notes: notes }).then(d => { setDetail(d); invalidateCombos(); });
  };

  return (
    <div>
      {/* Header + Insights Strip */}
      <div className="flex items-center justify-between mb-3">
        <h1 className="text-xl font-bold text-gray-900">Ad Combos</h1>
        <div className="flex gap-2">
          {isAdmin && (
            <>
              <button onClick={() => triggerSync().then(invalidateCombos)}
                className="px-3 py-1.5 text-xs border rounded text-gray-600 hover:bg-gray-50">Sync ROAS</button>
              <button
                onClick={() => {
                  setImporting(true);
                  autoClassifyAngles({
                    branch_id: !isAll ? selected : undefined,
                  }).then(r => {
                    setImportResult(r);
                    invalidateCombos();
                  }).catch(err => {
                    setImportResult({ error: err.response?.data?.detail || "Classification failed" });
                  }).finally(() => setImporting(false));
                }}
                disabled={importing}
                className="px-3 py-1.5 text-xs border border-violet-300 rounded text-violet-600 hover:bg-violet-50 disabled:opacity-50"
              >
                {importing ? "Classifying..." : "AI Detect Angles"}
              </button>
            </>
          )}
          <button onClick={() => setShowAdd(true)}
            className="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700">+ Add Combo</button>
        </div>
      </div>

      {/* Import result banner */}
      {importResult && (
        <div className={`mb-3 p-3 rounded text-sm flex items-center justify-between ${
          importResult.error ? "bg-red-50 text-red-700" : "bg-green-50 text-green-700"
        }`}>
          {importResult.error ? (
            <span>{importResult.error}</span>
          ) : (
            <span>
              AI Angles: {importResult.classified || 0} classified, {importResult.skipped || 0} skipped out of {importResult.total || 0}
              {importResult.errors?.length > 0 && (
                <span className="text-xs text-gray-500 ml-2">({importResult.errors[0]})</span>
              )}
            </span>
          )}
          <button onClick={() => setImportResult(null)} className="ml-2 text-gray-400 hover:text-gray-600">&times;</button>
        </div>
      )}

      {/* Tabs: All Combos | Pending Review */}
      <div className="flex gap-1 mb-4 border-b">
        <button onClick={() => setTab("all")}
          className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px ${tab === "all" ? "border-indigo-600 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}>
          All Combos
        </button>
        <button onClick={() => { setTab("pending"); queryClient.invalidateQueries({ queryKey: ["combos", "pending"] }); }}
          className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px flex items-center gap-1.5 ${tab === "pending" ? "border-amber-500 text-amber-600" : "border-transparent text-gray-500 hover:text-gray-700"}`}>
          Pending Review
          {pendingCombos.length > 0 && (
            <span className="px-1.5 py-0.5 rounded-full bg-amber-100 text-amber-700 text-[10px] font-bold">{pendingCombos.length}</span>
          )}
        </button>
      </div>

      {/* ── PENDING REVIEW TAB ── */}
      {tab === "pending" && (
        <div>
          {pendingLoading ? (
            <div className="text-gray-400 text-sm animate-pulse">Loading...</div>
          ) : pendingCombos.length === 0 ? (
            <div className="text-center py-12 text-gray-400">
              <div className="text-3xl mb-2">✓</div>
              <div className="text-sm">No combos pending review</div>
            </div>
          ) : (
            <div className="space-y-3">
              {pendingCombos.map(cb => (
                <div key={cb.id} className="border rounded-lg p-4 bg-white hover:shadow-md transition-shadow">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-indigo-600 text-xs font-medium">{cb.combo_code}</span>
                      <span className="px-2 py-0.5 rounded-full bg-yellow-100 text-yellow-700 text-[10px] font-medium">Pending</span>
                      {cb.approval_deadline && (
                        <span className="text-[10px] text-gray-400">Deadline: {cb.approval_deadline}</span>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <button onClick={() => openDetail(cb.id)}
                        className="px-2 py-1 text-xs bg-indigo-600 text-white rounded hover:bg-indigo-700">Review</button>
                      {cb.approval_status !== "Approved" && (
                        <button onClick={() => handleDelete(cb.id, cb.combo_code)}
                          className="px-2 py-1 text-xs border border-red-300 text-red-600 rounded hover:bg-red-50">Delete</button>
                      )}
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-3 text-xs text-gray-600">
                    <div>
                      <span className="text-gray-400">Copy:</span> {cb.copy?.copy_code} — {cb.copy?.headline?.substring(0, 40) || "No headline"}
                    </div>
                    <div>
                      <span className="text-gray-400">Material:</span> {cb.material?.material_code} — {cb.material?.material_type}
                      {cb.material?.kol_name && <span className="text-purple-600 ml-1">(KOL: {cb.material.kol_name})</span>}
                    </div>
                  </div>
                  {cb.submitted_by && (
                    <div className="text-[10px] text-gray-400 mt-1">Submitted by: {cb.submitted_by}</div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── ALL COMBOS TAB ── */}
      {tab === "all" && <>
      {insights && (
        <div className="flex gap-3 mb-4 text-xs">
          <span className="px-2 py-1 bg-gray-100 rounded">Total: {insights.total_combos}</span>
          <span className="px-2 py-1 bg-green-100 text-green-700 rounded">WIN: {insights.win_count ?? insights.winning_count}</span>
          {insights.top_by_roas?.[0] && (
            <span className="px-2 py-1 bg-blue-100 text-blue-700 rounded">
              Top ROAS: {insights.top_by_roas[0].roas.toFixed(2)} ({insights.top_by_roas[0].combo_code})
            </span>
          )}
        </div>
      )}

      {/* Filters */}
      <div className="flex gap-2 mb-4 flex-wrap">
        {[
          ["country_target", "Country", COUNTRIES],
          ["target_audience", "Audience", AUDIENCES],
          ["language", "Language", LANGUAGES],
          ["channel", "Channel", CHANNELS],
          ["verdict", "Verdict", VERDICTS],
          ["run_status", "Status", RUN_STATUSES],
        ].map(([key, label, opts]) => (
          <select key={key} value={f[key]} onChange={e => setFilter(key, e.target.value)}
            className="border rounded px-2 py-1 text-sm">
            <option value="">All {label}</option>
            {opts.map(o => <option key={o} value={o}>{o}</option>)}
          </select>
        ))}
        <select value={f.angle_id} onChange={e => setFilter("angle_id", e.target.value)}
          className="border rounded px-2 py-1 text-sm">
          <option value="">All Angles</option>
          {angles.map(a => <option key={a.id} value={a.id}>{a.angle_code} — {a.name}</option>)}
        </select>
      </div>

      {/* Combo Cards */}
      {loading ? (
        <div className="text-gray-400 text-sm animate-pulse">Loading...</div>
      ) : combos.length === 0 ? (
        <div className="text-gray-400 text-sm">No combos found. Create one to get started.</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {combos.map(cb => (
            <ComboCard key={cb.id} combo={cb} onClick={() => openDetail(cb.id)} />
          ))}
        </div>
      )}
      </>}

      {/* Detail Drawer */}
      {detail && (
        <div className="fixed inset-0 z-50 flex items-center justify-end">
          <div className="absolute inset-0 bg-black/30" onClick={() => setDetail(null)} />
          <div className="relative w-[480px] h-full bg-white shadow-xl overflow-y-auto">
            <div className="sticky top-0 bg-white border-b px-4 py-3 flex items-center justify-between">
              <h3 className="font-semibold text-sm">
                <span className="font-mono text-indigo-600">{detail.combo_code}</span>
                {detail.approval_status && (
                  <span className={`ml-2 text-[10px] px-1.5 py-0.5 rounded-full ${
                    detail.approval_status === "Approved" ? "bg-green-100 text-green-700" :
                    detail.approval_status === "Rejected" ? "bg-red-100 text-red-700" :
                    detail.approval_status === "Needs Revision" ? "bg-amber-100 text-amber-700" :
                    "bg-yellow-100 text-yellow-700"
                  }`}>{detail.approval_status}</span>
                )}
              </h3>
              <div className="flex items-center gap-2">
                {detail.approval_status !== "Approved" && (
                  <button onClick={() => {
                    handleDelete(detail.id, detail.combo_code);
                    setDetail(null);
                  }} className="text-xs text-red-500 hover:text-red-700">Delete</button>
                )}
                <button onClick={() => setDetail(null)} className="text-gray-400 hover:text-gray-600 text-lg">&times;</button>
              </div>
            </div>
            <div className="p-4 space-y-4">
              {/* Verdict */}
              <div className="space-y-1">
                <div className="flex items-center gap-3">
                  <VerdictBadge verdict={detail.verdict} reason={detail.verdict_notes} />
                  {detail.verdict_source && <span className="text-xs text-gray-400">({detail.verdict_source})</span>}
                  {detail.roas != null && (
                    <span className={`text-sm font-medium ${
                      detail.roas >= 3 ? "text-green-600" : detail.roas >= 1 ? "text-amber-500" : "text-red-500"
                    }`}>ROAS {detail.roas.toFixed(2)}</span>
                  )}
                </div>
                {detail.verdict_notes && (
                  <p className="text-[11px] text-gray-500 bg-gray-50 px-2 py-1 rounded">{detail.verdict_notes}</p>
                )}
              </div>

              {/* Copy */}
              <div className="bg-gray-50 rounded p-3">
                <label className="text-xs font-medium text-gray-500">Copy — {detail.copy?.copy_code}</label>
                <p className="text-sm font-medium mt-1">{detail.copy?.headline || "—"}</p>
                <p className="text-sm mt-1 whitespace-pre-wrap text-gray-700">{detail.copy?.primary_text || "—"}</p>
                {detail.copy?.landing_page_url && (
                  <a href={detail.copy.landing_page_url} target="_blank" rel="noreferrer"
                    className="text-xs text-blue-500 hover:underline mt-1 block">Landing page</a>
                )}
              </div>

              {/* Material */}
              <div className="bg-gray-50 rounded p-3">
                <label className="text-xs font-medium text-gray-500">Material — {detail.material?.material_code}</label>
                <div className="grid grid-cols-2 gap-2 mt-1 text-xs">
                  <div><span className="text-gray-400">Type:</span> {detail.material?.material_type}</div>
                  <div><span className="text-gray-400">Design:</span> {detail.material?.design_type || "—"}</div>
                  {detail.material?.kol_name && <div className="col-span-2 text-purple-600">KOL: {detail.material.kol_name}</div>}
                </div>
                {detail.material?.file_link && (
                  <a href={detail.material.file_link} target="_blank" rel="noreferrer"
                    className="text-xs text-blue-500 hover:underline mt-1 block">Open file</a>
                )}
              </div>

              {/* Primary Metrics: Cost · Purchase · Revenue · ROAS */}
              <div className="grid grid-cols-4 gap-2 text-xs">
                <div className="bg-blue-50 rounded p-2 text-center">
                  <span className="text-blue-400 text-[10px]">Cost</span>
                  <p className="font-semibold text-blue-700">{detail.spend_vnd != null ? new Intl.NumberFormat("en").format(Math.round(detail.spend_vnd)) : "—"}</p>
                </div>
                <div className="bg-purple-50 rounded p-2 text-center">
                  <span className="text-purple-400 text-[10px]">Purchase</span>
                  <p className="font-semibold text-purple-700">{detail.purchases ?? "—"}</p>
                </div>
                <div className="bg-green-50 rounded p-2 text-center">
                  <span className="text-green-400 text-[10px]">Revenue</span>
                  <p className="font-semibold text-green-700">{detail.revenue_vnd != null ? new Intl.NumberFormat("en").format(Math.round(detail.revenue_vnd)) : "—"}</p>
                </div>
                <div className={`rounded p-2 text-center ${
                  detail.roas >= 3 ? "bg-green-50" : detail.roas >= 1 ? "bg-amber-50" : detail.roas != null ? "bg-red-50" : "bg-gray-50"
                }`}>
                  <span className="text-gray-400 text-[10px]">ROAS</span>
                  <p className={`font-bold ${
                    detail.roas >= 3 ? "text-green-700" : detail.roas >= 1 ? "text-amber-600" : detail.roas != null ? "text-red-600" : "text-gray-400"
                  }`}>{detail.roas != null ? detail.roas.toFixed(2) : "—"}</p>
                </div>
              </div>

              {/* Secondary Metrics */}
              <div className="grid grid-cols-3 gap-2 text-xs">
                <div className="bg-gray-50 rounded p-2 text-center">
                  <span className="text-gray-400 text-[10px]">Impressions</span>
                  <p className="font-medium">{detail.impressions?.toLocaleString() || "—"}</p>
                </div>
                <div className="bg-gray-50 rounded p-2 text-center">
                  <span className="text-gray-400 text-[10px]">Clicks</span>
                  <p className="font-medium">{detail.clicks?.toLocaleString() || "—"}</p>
                </div>
                <div className="bg-gray-50 rounded p-2 text-center">
                  <span className="text-gray-400 text-[10px]">Leads</span>
                  <p className="font-medium">{detail.leads ?? "—"}</p>
                </div>
              </div>

              {/* Verdict editor */}
              <div className="border-t pt-3">
                <label className="text-xs font-medium text-gray-500">Set Verdict</label>
                <div className="flex gap-1 mt-1 flex-wrap">
                  {VERDICTS.map(v => (
                    <button key={v} onClick={() => {
                      const notes = v !== detail.verdict ? prompt("Verdict notes (optional):") : null;
                      saveVerdict(detail.id, v, notes);
                    }}
                      className={`text-xs px-2 py-1 rounded border ${
                        detail.verdict === v ? "bg-indigo-600 text-white border-indigo-600" : "text-gray-600 hover:bg-gray-100"
                      }`}>{v}</button>
                  ))}
                </div>
                {detail.verdict_notes && (
                  <p className="text-xs text-gray-500 mt-1">Notes: {detail.verdict_notes}</p>
                )}
              </div>

              {/* Approval Status */}
              {detail.approval_status && (
                <div className="border-t pt-3">
                  <div className="flex items-center justify-between mb-2">
                    <label className="text-xs font-medium text-gray-500">Approval</label>
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                      detail.approval_status === "Approved" ? "bg-green-100 text-green-700" :
                      detail.approval_status === "Rejected" ? "bg-red-100 text-red-700" :
                      detail.approval_status === "Needs Revision" ? "bg-amber-100 text-amber-700" :
                      "bg-yellow-100 text-yellow-700"
                    }`}>{detail.approval_status}</span>
                  </div>
                  <div className="text-xs text-gray-500 space-y-1">
                    {detail.submitted_by && <div>Submitted by: {detail.submitted_by}</div>}
                    {detail.reviewer_name && <div>Reviewer: {detail.reviewer_name}</div>}
                    {detail.approval_deadline && <div>Deadline: {detail.approval_deadline}</div>}
                    {detail.approval_feedback && (
                      <div className="bg-gray-50 rounded p-2 mt-1">Feedback: {detail.approval_feedback}</div>
                    )}
                  </div>

                  {/* Review buttons — show if current user is reviewer and status is Pending */}
                  {detail.approval_status === "Pending" && user?.id === detail.reviewer_id && (
                    <div className="mt-3 space-y-2">
                      <textarea placeholder="Feedback (optional)" id="review-feedback"
                        className="w-full border rounded px-2 py-1 text-xs" rows={2} />
                      <div className="flex gap-2">
                        <button onClick={() => {
                          const fb = document.getElementById("review-feedback")?.value;
                          reviewCombo(detail.id, { approval_status: "Approved", feedback: fb || null })
                            .then(d => { setDetail(d); invalidateCombos(); });
                        }} className="flex-1 px-3 py-1.5 bg-green-600 text-white text-xs rounded hover:bg-green-700">
                          Approve
                        </button>
                        <button onClick={() => {
                          const fb = document.getElementById("review-feedback")?.value;
                          reviewCombo(detail.id, { approval_status: "Needs Revision", feedback: fb || null })
                            .then(d => { setDetail(d); invalidateCombos(); });
                        }} className="flex-1 px-3 py-1.5 bg-amber-500 text-white text-xs rounded hover:bg-amber-600">
                          Needs Revision
                        </button>
                        <button onClick={() => {
                          const fb = document.getElementById("review-feedback")?.value;
                          reviewCombo(detail.id, { approval_status: "Rejected", feedback: fb || null })
                            .then(d => { setDetail(d); invalidateCombos(); });
                        }} className="flex-1 px-3 py-1.5 bg-red-600 text-white text-xs rounded hover:bg-red-700">
                          Reject
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* KOL info */}
              {detail.kol && (
                <div className="border-t pt-3">
                  <label className="text-xs font-medium text-gray-500">KOL</label>
                  <div className="text-xs mt-1 bg-purple-50 rounded p-2">
                    <span className="text-purple-700 font-medium">{detail.kol.kol_name}</span>
                    {detail.kol.kol_nationality && <span className="text-purple-500 ml-1">({detail.kol.kol_nationality})</span>}
                  </div>
                </div>
              )}

              {/* Meta ad name + run status */}
              <div className="border-t pt-3 space-y-2">
                <div>
                  <label className="text-xs text-gray-500">Meta Ad Name</label>
                  <input defaultValue={detail.meta_ad_name || ""} onBlur={e => {
                    if (e.target.value !== (detail.meta_ad_name || ""))
                      updateCombo(detail.id, { meta_ad_name: e.target.value }).then(d => setDetail(d));
                  }} className="w-full border rounded px-2 py-1 text-xs font-mono mt-1" placeholder="Paste from Meta Ads Manager" />
                </div>
                <div>
                  <label className="text-xs text-gray-500">Run Status</label>
                  <select defaultValue={detail.run_status || ""} onChange={e => {
                    updateCombo(detail.id, { run_status: e.target.value || null }).then(d => { setDetail(d); invalidateCombos(); });
                  }} className="w-full border rounded px-2 py-1 text-xs mt-1">
                    <option value="">Not set</option>
                    {RUN_STATUSES.map(s => <option key={s}>{s}</option>)}
                  </select>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Add Combo Modal */}
      {showAdd && <AddComboModal branchId={!isAll ? selected : null} userName={user?.name} onClose={() => setShowAdd(false)} onCreated={() => { setShowAdd(false); invalidateCombos(); }} />}
    </div>
  );
}

function SearchSelect({ label, items, value, onChange, renderItem, placeholder, required }) {
  const [search, setSearch] = useState("");
  const filtered = items.filter(item => {
    if (!search) return true;
    const s = search.toLowerCase();
    return renderItem(item).toLowerCase().includes(s);
  });
  return (
    <div>
      <label className="text-xs text-gray-500">{label}{required ? " *" : ""}</label>
      <input placeholder={`Search ${label.toLowerCase()}...`} value={search}
        onChange={e => setSearch(e.target.value)}
        className="w-full border rounded px-3 py-1.5 text-xs mt-1 mb-1" />
      <select value={value} onChange={e => onChange(e.target.value)}
        className="w-full border rounded px-3 py-2 text-sm" size={Math.min(filtered.length + 1, 6)}>
        <option value="">{placeholder || `Select ${label.toLowerCase()}...`}</option>
        {filtered.map(item => (
          <option key={item.id} value={item.id}>{renderItem(item)}</option>
        ))}
      </select>
    </div>
  );
}

function AddComboModal({ branchId, onClose, onCreated, userName }) {
  const [copies, setCopies] = useState([]);
  const [materials, setMaterials] = useState([]);
  const [kols, setKols] = useState([]);
  const [users, setUsers] = useState([]);
  const [mode, setMode] = useState("custom"); // "custom" | "kol"
  const [copyId, setCopyId] = useState("");
  const [materialId, setMaterialId] = useState("");
  const [kolId, setKolId] = useState("");
  const [metaAdName, setMetaAdName] = useState("");
  const [runStatus, setRunStatus] = useState("");
  // Approval
  const [submitApproval, setSubmitApproval] = useState(false);
  const [reviewerId, setReviewerId] = useState("");
  const [deadline, setDeadline] = useState("");

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const p = branchId ? { branch_id: branchId } : {};
    Promise.all([
      listCopies(p),
      listMaterials(p),
      listKolRecords({ ...p, ads_usage_status: "Available" }),
      listUsers().catch(() => []),
    ]).then(([c, m, k, u]) => {
      setCopies(c);
      setMaterials(m);
      setKols(Array.isArray(k) ? k : []);
      setUsers(Array.isArray(u) ? u : []);
    });
  }, [branchId]);

  const filteredMaterials = mode === "kol"
    ? materials.filter(m => m.material_type === "kol_video")
    : materials;

  const save = () => {
    setSaving(true);
    setError("");
    createCombo({
      copy_id: copyId,
      material_id: materialId,
      kol_id: kolId || undefined,
      meta_ad_name: metaAdName || undefined,
      run_status: runStatus || undefined,
      submit_approval: submitApproval,
      reviewer_id: submitApproval ? reviewerId : undefined,
      approval_deadline: submitApproval ? deadline : undefined,
      submitted_by: userName || "Unknown",
    }).then(onCreated).catch(err => {
      setError(err.response?.data?.detail || "Failed to create combo");
      setSaving(false);
    });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-xl p-6 max-h-[90vh] overflow-y-auto">
        <h2 className="text-lg font-semibold mb-4">Add Combo</h2>
        {error && <div className="mb-3 p-2 bg-red-50 text-red-700 text-sm rounded">{error}</div>}

        {/* Step 1: Content Type */}
        <div className="flex gap-2 mb-4">
          <button onClick={() => { setMode("custom"); setKolId(""); }}
            className={`flex-1 px-3 py-2 text-sm rounded border ${mode === "custom" ? "bg-indigo-600 text-white border-indigo-600" : "text-gray-600 hover:bg-gray-50"}`}>
            Custom Copy + Material
          </button>
          <button onClick={() => setMode("kol")}
            className={`flex-1 px-3 py-2 text-sm rounded border ${mode === "kol" ? "bg-purple-600 text-white border-purple-600" : "text-gray-600 hover:bg-gray-50"}`}>
            KOL Video Ads
          </button>
        </div>

        <div className="space-y-3">
          {/* KOL selector (only in KOL mode) */}
          {mode === "kol" && (
            <div>
              <label className="text-xs text-gray-500">KOL (Available for Ads)</label>
              <select value={kolId} onChange={e => setKolId(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm mt-1">
                <option value="">Select KOL...</option>
                {kols.map(k => (
                  <option key={k.id} value={k.id}>{k.kol_name} — {k.kol_nationality || "N/A"} {k.language ? `[${k.language}]` : ""}</option>
                ))}
              </select>
              {kols.length === 0 && (
                <p className="text-[10px] text-amber-600 mt-1">No KOLs with ads_usage_status = "Available". Set it in KOL management first.</p>
              )}
            </div>
          )}

          {/* Copy selector with search */}
          <SearchSelect label="Copy" items={copies} value={copyId} onChange={setCopyId} required
            renderItem={c => `${c.copy_code} — ${c.headline?.substring(0, 50) || c.primary_text?.substring(0, 50) || "No text"}`}
            placeholder="Select copy..." />

          {/* Material selector with search */}
          <SearchSelect label="Material" items={filteredMaterials} value={materialId} onChange={setMaterialId} required
            renderItem={m => `${m.material_code} — ${m.material_type} ${m.kol_name ? `(KOL: ${m.kol_name})` : ""}`}
            placeholder="Select material..." />

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-gray-500">Meta Ad Name</label>
              <input value={metaAdName} onChange={e => setMetaAdName(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm mt-1 font-mono" placeholder="Paste from Meta" />
            </div>
            <div>
              <label className="text-xs text-gray-500">Run Status</label>
              <select value={runStatus} onChange={e => setRunStatus(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm mt-1">
                <option value="">Not set</option>
                <option>Active</option><option>Paused</option><option>Ended</option>
              </select>
            </div>
          </div>

          {/* Approval section */}
          <div className="border-t pt-3 mt-2">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input type="checkbox" checked={submitApproval} onChange={e => setSubmitApproval(e.target.checked)}
                className="rounded border-gray-300" />
              Submit for approval
            </label>
            {submitApproval && (
              <div className="mt-2 space-y-2 bg-yellow-50 rounded p-3">
                <div>
                  <label className="text-xs text-gray-500">Reviewer *</label>
                  <select value={reviewerId} onChange={e => setReviewerId(e.target.value)}
                    className="w-full border rounded px-3 py-2 text-sm mt-1">
                    <option value="">Select reviewer...</option>
                    {users.filter(u => u.role === "admin" || u.role === "editor").map(u => (
                      <option key={u.id} value={u.id}>{u.name} ({u.role})</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-500">Approval Deadline</label>
                  <input type="date" value={deadline} onChange={e => setDeadline(e.target.value)}
                    className="w-full border rounded px-3 py-2 text-sm mt-1" />
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose} className="px-3 py-1.5 text-sm text-gray-600">Cancel</button>
          <button onClick={save}
            disabled={!copyId || !materialId || saving || (submitApproval && !reviewerId)}
            className="px-4 py-1.5 bg-indigo-600 text-white text-sm rounded disabled:opacity-50">
            {saving ? "Creating..." : submitApproval ? "Create & Submit for Approval" : "Create Combo"}
          </button>
        </div>
      </div>
    </div>
  );
}
