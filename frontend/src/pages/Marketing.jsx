/**
 * Marketing Activity Log — Phase 3
 */
import { useState } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import axios from "axios";
import { useBranch } from "../context/BranchContext";

const TYPES = ["PaidAds", "KOL", "CRM", "Event", "Organic"];

const TYPE_COLORS = {
  PaidAds: "bg-blue-100 text-blue-700",
  KOL: "bg-purple-100 text-purple-700",
  CRM: "bg-green-100 text-green-700",
  Event: "bg-yellow-100 text-yellow-700",
  Organic: "bg-gray-100 text-gray-600",
};

function Badge({ type }) {
  const cls = TYPE_COLORS[type] || "bg-gray-100 text-gray-600";
  return <span className={"px-2 py-0.5 rounded text-xs font-medium " + cls}>{type || "—"}</span>;
}

const EMPTY = {
  branch_id: "",
  target_country: "",
  activity_type: "",
  target_audience: "",
  description: "",
  result_notes: "",
  date_from: "",
  date_to: "",
};

export default function Marketing() {
  const { branches, currentBranch, isAll, selected } = useBranch();
  const branchNameMap = Object.fromEntries(branches.map(b => [b.id, b.name]));
  const queryClient = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [editId, setEditId] = useState(null);
  const [saving, setSaving] = useState(false);
  const [filterType, setFilterType] = useState("");

  const { data: rows, isPending, isPlaceholderData } = useQuery({
    queryKey: ["marketing-activities", selected, filterType],
    queryFn: () => {
      const params = new URLSearchParams();
      if (!isAll && selected) params.set("branch_id", selected);
      if (filterType) params.set("activity_type", filterType);
      return axios.get("/api/marketing?" + params).then(r => r.data.data || []);
    },
    placeholderData: keepPreviousData,
  });

  const openNew = () => {
    setForm({ ...EMPTY, branch_id: currentBranch?.id || "" });
    setEditId(null);
    setShowForm(true);
  };

  const openEdit = (row) => {
    setForm({
      branch_id: row.branch_id,
      target_country: row.target_country || "",
      activity_type: row.activity_type || "",
      target_audience: row.target_audience || "",
      description: row.description || "",
      result_notes: row.result_notes || "",
      date_from: row.date_from || "",
      date_to: row.date_to || "",
    });
    setEditId(row.id);
    setShowForm(true);
  };

  const save = async () => {
    setSaving(true);
    try {
      if (editId) {
        await axios.put("/api/marketing/" + editId, form);
      } else {
        await axios.post("/api/marketing", form);
      }
      setShowForm(false);
      queryClient.invalidateQueries({ queryKey: ["marketing-activities"] });
    } catch (e) {
      alert("Save failed: " + (e.response?.data?.detail || e.message));
    } finally {
      setSaving(false);
    }
  };

  const del = async (id) => {
    if (!confirm("Delete this activity?")) return;
    await axios.delete("/api/marketing/" + id);
    queryClient.invalidateQueries({ queryKey: ["marketing-activities"] });
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Marketing Activity Log</h1>
          <p className="text-xs text-gray-400 mt-0.5">Track all marketing actions across channels</p>
        </div>
        <button
          onClick={openNew}
          className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700"
        >
          + Add Activity
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-2 flex-wrap">
        <button
          onClick={() => setFilterType("")}
          className={"px-3 py-1 rounded text-xs font-medium border " + (!filterType ? "bg-gray-800 text-white border-gray-800" : "text-gray-500 border-gray-200 hover:bg-gray-50")}
        >
          All
        </button>
        {TYPES.map(t => (
          <button
            key={t}
            onClick={() => setFilterType(filterType === t ? "" : t)}
            className={"px-3 py-1 rounded text-xs font-medium border " + (filterType === t ? "bg-gray-800 text-white border-gray-800" : "text-gray-500 border-gray-200 hover:bg-gray-50")}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {isPending && !rows ? (
          <div className="p-8 text-center text-gray-400 animate-pulse">Loading…</div>
        ) : (rows || []).length === 0 ? (
          <div className="p-8 text-center text-gray-400">No activities yet. Click "+ Add Activity" to start.</div>
        ) : (
          <div className={"overflow-x-auto transition-opacity duration-150 " + (isPlaceholderData ? "opacity-40 pointer-events-none" : "")}>
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide text-left">
                  <th className="px-5 py-3">Date</th>
                  <th className="px-4 py-3">Branch</th>
                  <th className="px-4 py-3">Type</th>
                  <th className="px-4 py-3">Country</th>
                  <th className="px-4 py-3">Audience</th>
                  <th className="px-4 py-3">Description</th>
                  <th className="px-4 py-3">Result</th>
                  <th className="px-4 py-3"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {(rows || []).map(row => (
                  <tr key={row.id} className="hover:bg-gray-50">
                    <td className="px-5 py-3 text-gray-600 whitespace-nowrap">
                      {row.date_from || "—"}
                      {row.date_to && row.date_to !== row.date_from ? <span className="text-gray-400"> → {row.date_to}</span> : ""}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500">{branchNameMap[row.branch_id] || "—"}</td>
                    <td className="px-4 py-3"><Badge type={row.activity_type} /></td>
                    <td className="px-4 py-3 text-gray-700">{row.target_country || "—"}</td>
                    <td className="px-4 py-3 text-gray-500 text-xs">{row.target_audience || "—"}</td>
                    <td className="px-4 py-3 text-gray-700 max-w-xs truncate">{row.description || "—"}</td>
                    <td className="px-4 py-3 text-gray-500 text-xs max-w-xs truncate">{row.result_notes || "—"}</td>
                    <td className="px-4 py-3 text-right">
                      <button onClick={() => openEdit(row)} className="text-indigo-500 hover:text-indigo-700 text-xs mr-3">Edit</button>
                      <button onClick={() => del(row.id)} className="text-red-400 hover:text-red-600 text-xs">Delete</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Form modal */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg mx-4 p-6 space-y-4">
            <h2 className="font-semibold text-gray-800">{editId ? "Edit Activity" : "New Activity"}</h2>
            <div className="grid grid-cols-2 gap-3">
              <div className="col-span-2">
                <label className="text-xs text-gray-500 mb-1 block">Branch</label>
                <select value={form.branch_id} onChange={e => setForm(f => ({ ...f, branch_id: e.target.value }))}
                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
                  <option value="">— select branch —</option>
                  {branches.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
                </select>
              </div>
              <div className="col-span-2">
                <label className="text-xs text-gray-500 mb-1 block">Type</label>
                <select value={form.activity_type} onChange={e => setForm(f => ({ ...f, activity_type: e.target.value }))}
                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
                  <option value="">— select —</option>
                  {TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              <Field label="Target Country" value={form.target_country} onChange={v => setForm(f => ({ ...f, target_country: v }))} />
              <Field label="Target Audience" value={form.target_audience} onChange={v => setForm(f => ({ ...f, target_audience: v }))} />
              <Field label="Date From" type="date" value={form.date_from} onChange={v => setForm(f => ({ ...f, date_from: v }))} />
              <Field label="Date To" type="date" value={form.date_to} onChange={v => setForm(f => ({ ...f, date_to: v }))} />
              <div className="col-span-2">
                <label className="text-xs text-gray-500 mb-1 block">Description</label>
                <textarea value={form.description} onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" rows={3} />
              </div>
              <div className="col-span-2">
                <label className="text-xs text-gray-500 mb-1 block">Result Notes</label>
                <textarea value={form.result_notes} onChange={e => setForm(f => ({ ...f, result_notes: e.target.value }))}
                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" rows={2} />
              </div>
            </div>
            <div className="flex justify-end gap-3 pt-2">
              <button onClick={() => setShowForm(false)} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">Cancel</button>
              <button onClick={save} disabled={saving}
                className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50">
                {saving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, value, onChange, type = "text" }) {
  return (
    <div>
      <label className="text-xs text-gray-500 mb-1 block">{label}</label>
      <input type={type} value={value} onChange={e => onChange(e.target.value)}
        className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
    </div>
  );
}
