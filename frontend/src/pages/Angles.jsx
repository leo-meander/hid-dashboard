/**
 * Ad Angles — WIN/TEST/LOSE based on TOF Sales campaign performance.
 * Verdict: Only TOF Sales combos count. Benchmark = AVG ROAS of branch.
 */
import { useEffect, useState } from "react";
import axios from "axios";
import SyncBadge from "../components/SyncBadge";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";

const STATUS_OPTIONS = ["WIN", "TEST", "LOSE"];

const STATUS_STYLE = {
  WIN:  { card: "border-green-200 bg-green-50",  badge: "bg-green-600 text-white",  label: "WIN" },
  TEST: { card: "border-yellow-200 bg-yellow-50", badge: "bg-yellow-500 text-white", label: "TEST" },
  LOSE: { card: "border-red-200 bg-red-50",       badge: "bg-red-500 text-white",    label: "LOSE" },
  null: { card: "border-gray-200 bg-white",        badge: "bg-gray-300 text-gray-600", label: "—" },
};

const HOOK_COLORS = {
  "Question": "bg-blue-100 text-blue-700",
  "Shock/Surprise": "bg-purple-100 text-purple-700",
  "Pain Point": "bg-red-100 text-red-700",
  "Aspiration": "bg-emerald-100 text-emerald-700",
  "Social Proof": "bg-amber-100 text-amber-700",
  "Story": "bg-indigo-100 text-indigo-700",
  "How-To": "bg-cyan-100 text-cyan-700",
  "Trend/Meme": "bg-pink-100 text-pink-700",
  "Seasonal": "bg-orange-100 text-orange-700",
  "Activity": "bg-teal-100 text-teal-700",
  "Reframing": "bg-violet-100 text-violet-700",
};

function ScoreBar({ score }) {
  if (score == null) return <span className="text-gray-300 text-xs">No data</span>;
  const color = score >= 70 ? "bg-green-500" : score >= 40 ? "bg-yellow-400" : "bg-red-400";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 bg-gray-200 rounded-full h-1.5">
        <div className={color + " h-1.5 rounded-full"} style={{ width: score + "%" }} />
      </div>
      <span className="text-xs font-semibold text-gray-700 w-8 text-right">{score}</span>
    </div>
  );
}

const EMPTY = { name: "", description: "", status: "", branch_id: "", created_by: "" };

export default function Angles() {
  const { currentBranch, selected, isAll } = useBranch();
  const currency = currentBranch?.currency || currentBranch?.native_currency || "VND";
  const sym = CURRENCY_SYMBOLS[currency] || "";
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterStatus, setFilterStatus] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [editId, setEditId] = useState(null);
  const [saving, setSaving] = useState(false);

  const load = () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (!isAll && selected) params.set("branch_id", selected);
    if (filterStatus) params.set("status", filterStatus);
    axios.get("/api/angles?" + params)
      .then(r => setRows(r.data.data || []))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, [selected, isAll, filterStatus]);

  const openNew = () => { setForm({ ...EMPTY, branch_id: currentBranch?.id || "" }); setEditId(null); setShowForm(true); };
  const openEdit = (row) => {
    setForm({ name: row.name, description: row.description || "", status: row.status || "", branch_id: row.branch_id || "", created_by: row.created_by || "" });
    setEditId(row.id); setShowForm(true);
  };

  const save = async () => {
    setSaving(true);
    try {
      editId ? await axios.put("/api/angles/" + editId, form) : await axios.post("/api/angles", form);
      setShowForm(false); load();
    } catch (e) { alert("Save failed: " + (e.response?.data?.detail || e.message)); }
    finally { setSaving(false); }
  };

  const del = async (id) => { if (!confirm("Delete angle?")) return; await axios.delete("/api/angles/" + id); load(); };

  const grouped = {
    WIN:  rows.filter(r => r.status === "WIN"),
    TEST: rows.filter(r => r.status === "TEST"),
    LOSE: rows.filter(r => r.status === "LOSE"),
  };

  const counts = { WIN: grouped.WIN.length, TEST: grouped.TEST.length, LOSE: grouped.LOSE.length };
  const displayRows = filterStatus ? rows.filter(r => r.status === filterStatus) : rows;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Ad Angles</h1>
          <p className="text-xs text-gray-400 mt-0.5">
            Verdict: TOF Sales only · Benchmark = Branch AVG ROAS
            <SyncBadge timestamp={rows[0]?.data_synced_at} />
          </p>
        </div>
        <button onClick={openNew} className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700">+ New Angle</button>
      </div>

      {/* Status summary */}
      <div className="grid grid-cols-3 gap-4">
        {STATUS_OPTIONS.map(s => {
          const st = STATUS_STYLE[s];
          return (
            <div key={s} className={"rounded-xl border-2 px-5 py-4 cursor-pointer " + st.card + (filterStatus === s ? " ring-2 ring-indigo-400" : "")} onClick={() => setFilterStatus(filterStatus === s ? "" : s)}>
              <div className="flex items-center gap-2">
                <span className={"px-2 py-0.5 rounded text-xs font-bold " + st.badge}>{st.label}</span>
                <span className="text-2xl font-bold text-gray-800">{counts[s]}</span>
              </div>
              <p className="text-xs text-gray-500 mt-1">angles</p>
            </div>
          );
        })}
      </div>

      {/* Cards grid */}
      {loading ? (
        <div className="p-8 text-center text-gray-400 animate-pulse">Loading…</div>
      ) : displayRows.length === 0 ? (
        <div className="bg-white rounded-xl border p-8 text-center text-gray-400">No angles yet. Click "+ New Angle" to start.</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {displayRows.map(row => {
            const st = STATUS_STYLE[row.status] || STATUS_STYLE[null];
            const hookClass = HOOK_COLORS[row.hook_type] || "bg-gray-100 text-gray-600";
            const keypoints = [row.keypoint_1, row.keypoint_2, row.keypoint_3, row.keypoint_4, row.keypoint_5].filter(Boolean);
            return (
              <div key={row.id} className={"rounded-xl border-2 p-5 space-y-3 " + st.card}>
                <div className="flex items-start justify-between gap-2">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={"px-2 py-0.5 rounded text-xs font-bold " + st.badge}>{row.status || "—"}</span>
                      {row.hook_type && <span className={"px-2 py-0.5 rounded text-xs font-medium " + hookClass}>{row.hook_type}</span>}
                      {row.angle_code && <span className="text-xs text-gray-400 font-mono">{row.angle_code}</span>}
                    </div>
                    <h3 className="font-semibold text-gray-800 mt-2 leading-tight">{row.name}</h3>
                    {keypoints.length > 0 && (
                      <ul className="mt-1.5 space-y-0.5">
                        {keypoints.map((kp, i) => (
                          <li key={i} className="text-xs text-gray-500 flex items-start gap-1">
                            <span className="text-gray-300 mt-0.5">•</span>
                            <span>{kp}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {row.description && !keypoints.length && <p className="text-xs text-gray-500 mt-1">{row.description}</p>}
                  </div>
                  <div className="flex gap-1 shrink-0">
                    <button onClick={() => openEdit(row)} className="text-indigo-500 hover:text-indigo-700 text-xs">Edit</button>
                    <span className="text-gray-300">·</span>
                    <button onClick={() => del(row.id)} className="text-red-400 hover:text-red-600 text-xs">Del</button>
                  </div>
                </div>

                {/* TOF Sales verdict info */}
                <div className="pt-2 border-t border-black/5 space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-gray-500">TOF Sales Verdict</span>
                    {row.benchmark_tof != null && (
                      <span className="text-xs text-indigo-500 font-medium">Benchmark: {row.benchmark_tof}x</span>
                    )}
                  </div>
                  {row.status_reason === "no_tof_sales_data" ? (
                    <p className="text-xs text-gray-400 italic">No TOF Sales combos linked</p>
                  ) : row.status_reason === "insufficient_data" ? (
                    <p className="text-xs text-amber-500 italic">
                      Insufficient data: {row.tof_impressions < 20000 ? `${new Intl.NumberFormat("en").format(row.tof_impressions)}/${new Intl.NumberFormat("en").format(20000)} impr` : ""}{row.tof_impressions < 20000 && row.tof_bookings < 5 ? " · " : ""}{row.tof_bookings < 5 ? `${row.tof_bookings}/5 bookings` : ""}
                    </p>
                  ) : row.status_reason === "no_benchmark" ? (
                    <p className="text-xs text-gray-400 italic">No benchmark data for this branch</p>
                  ) : (
                    <div className="grid grid-cols-3 gap-1 text-xs">
                      <div>
                        <p className="text-gray-400">TOF ROAS</p>
                        <p className={"font-semibold " + (row.status === "WIN" ? "text-green-600" : row.status === "LOSE" ? "text-red-500" : "text-gray-700")}>{row.tof_roas != null ? row.tof_roas + "x" : "—"}</p>
                      </div>
                      <div>
                        <p className="text-gray-400">Impressions</p>
                        <p className="font-medium text-gray-700">{row.tof_impressions > 0 ? new Intl.NumberFormat("en").format(row.tof_impressions) : "—"}</p>
                      </div>
                      <div>
                        <p className="text-gray-400">Bookings</p>
                        <p className="font-medium text-gray-700">{row.tof_bookings || 0}</p>
                      </div>
                    </div>
                  )}
                </div>

                {/* All-campaign stats */}
                {(row.cost_native > 0 || row.combo_count > 0) && (
                  <div className="grid grid-cols-4 gap-1 pt-1 border-t border-black/5 text-xs">
                    <div>
                      <p className="text-gray-400">Total Spend</p>
                      <p className="font-medium text-gray-700">{row.cost_native > 0 ? sym + new Intl.NumberFormat("en").format(Math.round(row.cost_native)) : "—"}</p>
                    </div>
                    <div>
                      <p className="text-gray-400">Total ROAS</p>
                      <p className="font-medium text-gray-700">{row.roas != null ? row.roas + "x" : "—"}</p>
                    </div>
                    <div>
                      <p className="text-gray-400">Bookings</p>
                      <p className="font-medium text-gray-700">{row.bookings || 0}</p>
                    </div>
                    <div>
                      <p className="text-gray-400">TOF Combos</p>
                      <p className="font-medium text-gray-700">{row.qualifying_combos || 0} / {row.combo_count || 0}</p>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6 space-y-4">
            <h2 className="font-semibold text-gray-800">{editId ? "Edit Angle" : "New Angle"}</h2>
            <div className="space-y-3">
              <div>
                <label className="text-xs text-gray-500 mb-1 block">Angle Name *</label>
                <input value={form.name} onChange={e => setForm(p => ({ ...p, name: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" placeholder="e.g. Solo female travel safety" />
              </div>
              <div>
                <label className="text-xs text-gray-500 mb-1 block">Description</label>
                <textarea value={form.description} onChange={e => setForm(p => ({ ...p, description: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" rows={3} />
              </div>
              <div>
                <label className="text-xs text-gray-500 mb-1 block">Created By</label>
                <input value={form.created_by} onChange={e => setForm(p => ({ ...p, created_by: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
              </div>
            </div>
            <div className="flex justify-end gap-3 pt-2">
              <button onClick={() => setShowForm(false)} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">Cancel</button>
              <button onClick={save} disabled={saving} className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50">{saving ? "Saving…" : "Save"}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
