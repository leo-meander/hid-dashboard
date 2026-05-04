/**
 * KOL Management — aggregated from reservations (room_type KOL_ pattern)
 */
import { useEffect, useState, useMemo, useRef } from "react";
import axios from "axios";
import { useBranch } from "../context/BranchContext";
import SyncBadge from "../components/SyncBadge";

// ─── helpers ────────────────────────────────────────────────────────────────

function fmt(n, decimals = 0) {
  if (n == null || n === "" || isNaN(Number(n))) return "—";
  return Number(n).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function StatusBadge({ value }) {
  if (!value) return <span className="text-gray-300 text-xs">—</span>;
  const v = value.toLowerCase();
  const cls =
    v.includes("signed") || v.includes("done") || v.includes("active") || v.includes("published")
      ? "bg-green-100 text-green-700"
      : v.includes("progress") || v.includes("nego")
      ? "bg-blue-100 text-blue-700"
      : v.includes("edit")
      ? "bg-yellow-100 text-yellow-700"
      : v.includes("draft") || v.includes("start") || v.includes("pending")
      ? "bg-gray-100 text-gray-500"
      : v.includes("cancel") || v.includes("expired")
      ? "bg-red-100 text-red-600"
      : "bg-purple-100 text-purple-600";
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium whitespace-nowrap ${cls}`}>
      {value}
    </span>
  );
}

function ExpiryCell({ days, label }) {
  if (!label) return <span className="text-gray-300 text-xs">—</span>;
  if (days == null) return <span className="text-xs text-gray-500">{label}</span>;
  const badge =
    days < 0 ? (
      <span className="ml-1 px-1.5 py-0.5 rounded bg-red-100 text-red-700 text-xs font-medium">Expired</span>
    ) : days <= 14 ? (
      <span className="ml-1 px-1.5 py-0.5 rounded bg-red-100 text-red-700 text-xs font-medium">{days}d left</span>
    ) : days <= 30 ? (
      <span className="ml-1 px-1.5 py-0.5 rounded bg-orange-100 text-orange-700 text-xs font-medium">{days}d left</span>
    ) : null;
  return (
    <span className="text-xs text-gray-500 whitespace-nowrap">
      {label}{badge}
    </span>
  );
}

function VideoLinks({ ig, tiktok, youtube }) {
  const links = [
    ig && { href: ig, label: "IG", cls: "text-pink-500 hover:text-pink-700" },
    tiktok && { href: tiktok, label: "TT", cls: "text-gray-600 hover:text-gray-800" },
    youtube && { href: youtube, label: "YT", cls: "text-red-500 hover:text-red-700" },
  ].filter(Boolean);
  if (!links.length) return <span className="text-gray-300 text-xs">—</span>;
  return (
    <div className="flex gap-2">
      {links.map(({ href, label, cls }) => (
        <a key={label} href={href} target="_blank" rel="noopener noreferrer"
          className={`text-xs px-1.5 py-0.5 rounded bg-gray-50 hover:bg-gray-100 font-medium ${cls}`}>
          {label}
        </a>
      ))}
    </div>
  );
}

function SummaryCard({ label, value, sub, color = "indigo" }) {
  const palette = {
    indigo: ["border-indigo-100 bg-indigo-50", "text-indigo-700"],
    green:  ["border-green-100 bg-green-50",   "text-green-700"],
    amber:  ["border-amber-100 bg-amber-50",   "text-amber-700"],
    rose:   ["border-rose-100 bg-rose-50",     "text-rose-700"],
  };
  const [ring, text] = palette[color] || palette.indigo;
  return (
    <div className={`rounded-xl border p-4 ${ring}`}>
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={`text-xl font-bold ${text}`}>{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
    </div>
  );
}

// ─── Edit Modal ──────────────────────────────────────────────────────────────

const DELIVERABLE_STATUSES = ["Not Started", "In Progress", "Editing", "Done"];
const CONTRACT_STATUSES = ["Draft", "Negotiating", "Signed", "Cancelled"];

function EditModal({ row, branches, onClose, onSaved }) {
  const isNew = !row.kol_record_id;
  const matchedBranch = branches.find((b) => b.name === row.branch);

  const [form, setForm] = useState({
    branch_id: matchedBranch?.id || "",
    kol_name: row.kol_rate_plan_name || "",
    kol_nationality: row.kol_nationality || "",
    language: row.language || "",
    target_audience: row.target_audience || "",
    cost_native: row.cost_native ?? "",
    is_gifted_stay: false,
    invitation_date: "",
    published_date: "",
    link_ig: row.link_ig || "",
    link_tiktok: row.link_tiktok || "",
    link_youtube: row.link_youtube || "",
    deliverable_status: CONTRACT_STATUSES.includes(row.status) ? "" : (row.status || ""),
    contract_status: CONTRACT_STATUSES.includes(row.status) ? (row.status || "") : "",
    paid_ads_eligible: false,
    paid_ads_usage_fee_vnd: row.ads_usage_fee_vnd ?? "",
    paid_ads_channel: row.channel || "",
    usage_rights_expiry_date: row.usage_rights_until || "",
    notes: row.notes || "",
  });
  const [saving, setSaving] = useState(false);
  const set = (k, v) => setForm((p) => ({ ...p, [k]: v }));

  const save = async () => {
    if (!form.kol_name || !form.branch_id) {
      alert("KOL Name and Branch are required");
      return;
    }
    setSaving(true);
    const payload = {
      ...form,
      cost_native: form.cost_native !== "" ? Number(form.cost_native) : null,
      paid_ads_usage_fee_vnd: form.paid_ads_usage_fee_vnd !== "" ? Number(form.paid_ads_usage_fee_vnd) : null,
      paid_ads_eligible: !!form.paid_ads_usage_fee_vnd,
    };
    try {
      if (isNew) {
        await axios.post("/api/kol", payload);
      } else {
        await axios.put("/api/kol/" + row.kol_record_id, payload);
      }
      onSaved();
    } catch (e) {
      alert("Save failed: " + (e.response?.data?.detail || e.message));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-2xl mx-4 p-6 space-y-4 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold text-gray-800">
            {isNew ? "Add KOL Details" : "Edit KOL Details"}
          </h2>
          <span className="text-sm font-medium text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded">
            {row.kol_rate_plan_name}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-3">
          {/* Branch (read-only hint) */}
          <div>
            <label className="text-xs text-gray-500 mb-1 block">Branch *</label>
            <select value={form.branch_id} onChange={(e) => set("branch_id", e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
              <option value="">— select —</option>
              {branches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          </div>

          {/* KOL Name (pre-filled, editable) */}
          <div>
            <label className="text-xs text-gray-500 mb-1 block">KOL Rate Plan Name *</label>
            <input value={form.kol_name} onChange={(e) => set("kol_name", e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
          </div>

          {[
            { k: "kol_nationality", l: "KOL Nationality" },
            { k: "language", l: "Language" },
            { k: "target_audience", l: "Target Audience" },
          ].map(({ k, l }) => (
            <div key={k}>
              <label className="text-xs text-gray-500 mb-1 block">{l}</label>
              <input value={form[k]} onChange={(e) => set(k, e.target.value)}
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
            </div>
          ))}

          <div>
            <label className="text-xs text-gray-500 mb-1 block">Cost (native currency)</label>
            <input type="number" value={form.cost_native} onChange={(e) => set("cost_native", e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
          </div>

          {[
            { k: "link_ig", l: "Instagram Link" },
            { k: "link_tiktok", l: "TikTok Link" },
            { k: "link_youtube", l: "YouTube Link" },
          ].map(({ k, l }) => (
            <div key={k}>
              <label className="text-xs text-gray-500 mb-1 block">{l}</label>
              <input value={form[k]} onChange={(e) => set(k, e.target.value)}
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
            </div>
          ))}

          <div>
            <label className="text-xs text-gray-500 mb-1 block">Deliverable Status</label>
            <select value={form.deliverable_status} onChange={(e) => set("deliverable_status", e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
              <option value="">— select —</option>
              {DELIVERABLE_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>

          <div>
            <label className="text-xs text-gray-500 mb-1 block">Contract Status</label>
            <select value={form.contract_status} onChange={(e) => set("contract_status", e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
              <option value="">— select —</option>
              {CONTRACT_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>

          <div>
            <label className="text-xs text-gray-500 mb-1 block">Ads Usage Fee (VND)</label>
            <input type="number" value={form.paid_ads_usage_fee_vnd}
              onChange={(e) => set("paid_ads_usage_fee_vnd", e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
          </div>

          <div>
            <label className="text-xs text-gray-500 mb-1 block">Channel</label>
            <input value={form.paid_ads_channel} onChange={(e) => set("paid_ads_channel", e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
          </div>

          <div>
            <label className="text-xs text-gray-500 mb-1 block">Usage Rights Until</label>
            <input type="date" value={form.usage_rights_expiry_date}
              onChange={(e) => set("usage_rights_expiry_date", e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
          </div>

          <div className="col-span-2">
            <label className="text-xs text-gray-500 mb-1 block">Notes</label>
            <textarea value={form.notes} onChange={(e) => set("notes", e.target.value)}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" rows={2} />
          </div>
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">Cancel</button>
          <button onClick={save} disabled={saving}
            className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50">
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Main page ───────────────────────────────────────────────────────────────

export default function KOL() {
  const { branches, selected, isAll } = useBranch();

  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState(null);

  const [search, setSearch] = useState("");
  const [filterBranch, setFilterBranch] = useState("all");
  const [filterStatus, setFilterStatus] = useState("all");

  const [editRow, setEditRow] = useState(null); // row being edited
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState(null);
  const fileRef = useRef(null);

  const handleImportCSV = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setImporting(true);
    setImportResult(null);
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await axios.post("/api/kol/sync-csv", fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setImportResult(res.data.data);
      load();
    } catch (err) {
      setImportResult({ error: err.response?.data?.detail || err.message });
    } finally {
      setImporting(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const load = () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (!isAll && selected) params.set("branch_id", selected);
    axios
      .get("/api/kol/summary?" + params)
      .then((r) => {
        setRows(r.data.data || []);
        setLastRefresh(new Date());
      })
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, [selected, isAll]);

  // ── filters ──────────────────────────────────────────────────────────

  const allBranches = useMemo(() => [...new Set(rows.map((r) => r.branch).filter(Boolean))].sort(), [rows]);
  const allStatuses = useMemo(() => [...new Set(rows.map((r) => r.status).filter(Boolean))].sort(), [rows]);

  const filtered = useMemo(() => rows.filter((r) => {
    if (search && ![r.kol_rate_plan_name, r.branch, r.kol_nationality, r.language]
      .join(" ").toLowerCase().includes(search.toLowerCase())) return false;
    if (filterBranch !== "all" && r.branch !== filterBranch) return false;
    if (filterStatus !== "all" && r.status !== filterStatus) return false;
    return true;
  }), [rows, search, filterBranch, filterStatus]);

  // ── summary stats ─────────────────────────────────────────────────────

  const totalBookings = filtered.reduce((s, r) => s + (r.organic_booking || 0), 0);
  const totalRevenue  = filtered.reduce((s, r) => s + (r.organic_revenue || 0), 0);
  const totalCost     = filtered.reduce((s, r) => s + (r.cost_vnd || 0), 0);
  const expiryAlerts  = filtered.filter((r) => r.expiry_days != null && r.expiry_days <= 30);

  // ─────────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-5">
      {/* header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">KOL Management</h1>
          <p className="text-xs text-gray-400 mt-0.5">
            Aggregated from reservations · Room type pattern: <code className="bg-gray-100 px-1 rounded">XYZ (KOL_Name)</code>
            <SyncBadge timestamp={rows[0]?.data_synced_at} />
          </p>
        </div>
        <div className="flex gap-2">
          <input ref={fileRef} type="file" accept=".csv" onChange={handleImportCSV} className="hidden" />
          <button onClick={() => fileRef.current?.click()} disabled={importing}
            className="px-3 py-2 text-sm rounded-lg border border-indigo-200 bg-indigo-50 hover:bg-indigo-100 text-indigo-600 disabled:opacity-50 flex items-center gap-1.5">
            {importing ? "Importing…" : "Import CSV"}
          </button>
          <button onClick={load} disabled={loading}
            className="px-3 py-2 text-sm rounded-lg border border-gray-200 hover:bg-gray-50 text-gray-600 disabled:opacity-50 flex items-center gap-1.5">
            <span className={loading ? "animate-spin inline-block" : ""}>↻</span>
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </div>

      {/* import result banner */}
      {importResult && (
        <div className={`rounded-xl px-5 py-3 text-sm flex items-start gap-2 ${
          importResult.error
            ? "bg-red-50 border border-red-200 text-red-700"
            : "bg-green-50 border border-green-200 text-green-700"
        }`}>
          {importResult.error ? (
            <span>Import failed: {importResult.error}</span>
          ) : (
            <span>
              Import complete: <strong>{importResult.created}</strong> created,{" "}
              <strong>{importResult.updated}</strong> updated,{" "}
              <strong>{importResult.skipped}</strong> skipped
              {importResult.errors?.length > 0 && (
                <span className="text-orange-600 ml-2">
                  ({importResult.errors.length} warnings)
                </span>
              )}
            </span>
          )}
          <button onClick={() => setImportResult(null)} className="ml-auto text-xs opacity-50 hover:opacity-100">✕</button>
        </div>
      )}

      {/* expiry alerts */}
      {expiryAlerts.length > 0 && (
        <div className="bg-orange-50 border border-orange-200 rounded-xl px-5 py-3 text-sm text-orange-700 flex items-start gap-2">
          <span>⚠</span>
          <span>
            <strong>{expiryAlerts.length} KOL(s)</strong> usage rights expiring within 30 days:{" "}
            {expiryAlerts.map((r) => r.kol_rate_plan_name).join(", ")}
          </span>
        </div>
      )}

      {/* summary cards */}
      {!loading && rows.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <SummaryCard label="KOLs Found" value={filtered.length} sub={rows.length !== filtered.length ? `of ${rows.length}` : "from reservations"} color="indigo" />
          <SummaryCard label="Organic Bookings" value={fmt(totalBookings)} sub="non-cancelled" color="green" />
          <SummaryCard label="Organic Revenue" value={totalRevenue > 0 ? fmt(totalRevenue) : "—"} sub="native currency sum" color="green" />
          <SummaryCard label="Rights Expiring" value={expiryAlerts.length} sub="within 30 days" color="rose" />
        </div>
      )}

      {/* filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <input value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder="Search KOL name, branch, nationality…"
          className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm w-64 focus:outline-none focus:ring-1 focus:ring-indigo-300" />
        <select value={filterBranch} onChange={(e) => setFilterBranch(e.target.value)}
          className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-indigo-300">
          <option value="all">All Branches</option>
          {allBranches.map((b) => <option key={b} value={b}>{b}</option>)}
        </select>
        <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}
          className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-indigo-300">
          <option value="all">All Statuses</option>
          {allStatuses.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        {lastRefresh && (
          <span className="text-xs text-gray-400 ml-auto">
            Last refreshed {lastRefresh.toLocaleTimeString()}
          </span>
        )}
      </div>

      {/* table */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {loading ? (
          <div className="p-10 text-center text-gray-400 animate-pulse">Loading KOL data…</div>
        ) : filtered.length === 0 ? (
          <div className="p-10 text-center text-gray-400">
            {rows.length === 0
              ? "No KOL reservations found. Room types with pattern 'XYZ (KOL_Name)' will appear here."
              : "No KOLs match the current filters."}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide text-left border-b border-gray-100">
                  <th className="px-4 py-3 whitespace-nowrap">KOL Rate Plan</th>
                  <th className="px-4 py-3 whitespace-nowrap">Branch</th>
                  <th className="px-4 py-3 whitespace-nowrap text-right">Org. Bookings</th>
                  <th className="px-4 py-3 whitespace-nowrap text-right">Org. Revenue</th>
                  <th className="px-4 py-3 whitespace-nowrap">Nationality</th>
                  <th className="px-4 py-3 whitespace-nowrap">Language</th>
                  <th className="px-4 py-3 whitespace-nowrap text-right">Cost (VND)</th>
                  <th className="px-4 py-3 whitespace-nowrap">Target Audience</th>
                  <th className="px-4 py-3 whitespace-nowrap">Angle ID</th>
                  <th className="px-4 py-3 whitespace-nowrap">Angle Info</th>
                  <th className="px-4 py-3 whitespace-nowrap text-right">Ads Fee (VND)</th>
                  <th className="px-4 py-3 whitespace-nowrap">Channel</th>
                  <th className="px-4 py-3 whitespace-nowrap">Usage Rights Until</th>
                  <th className="px-4 py-3 whitespace-nowrap">Status</th>
                  <th className="px-4 py-3 whitespace-nowrap">Links</th>
                  <th className="px-4 py-3 whitespace-nowrap"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {filtered.map((r, i) => (
                  <tr key={i} className="hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-3 whitespace-nowrap">
                      <span className="font-medium text-indigo-600">{r.kol_rate_plan_name}</span>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-600 whitespace-nowrap">{r.branch}</td>
                    <td className="px-4 py-3 text-right">
                      <span className="text-sm font-semibold text-gray-800">{r.organic_booking || 0}</span>
                    </td>
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      {r.organic_revenue > 0 ? (
                        <span className="text-sm font-semibold text-green-700">
                          {fmt(r.organic_revenue)}
                          <span className="text-xs text-gray-400 ml-1">{r.currency}</span>
                        </span>
                      ) : <span className="text-gray-300 text-xs">—</span>}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-600 whitespace-nowrap">{r.kol_nationality || "—"}</td>
                    <td className="px-4 py-3 text-xs text-gray-500 whitespace-nowrap">{r.language || "—"}</td>
                    <td className="px-4 py-3 text-right text-xs text-gray-700 whitespace-nowrap">
                      {r.cost_vnd ? "₫" + fmt(r.cost_vnd) : "—"}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500 max-w-[120px] truncate" title={r.target_audience}>
                      {r.target_audience || "—"}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500 whitespace-nowrap">{r.angle_id || "—"}</td>
                    <td className="px-4 py-3 text-xs text-gray-500 max-w-[140px] truncate" title={r.angle_info}>
                      {r.angle_info || "—"}
                    </td>
                    <td className="px-4 py-3 text-right text-xs text-gray-700 whitespace-nowrap">
                      {r.ads_usage_fee_vnd ? "₫" + fmt(r.ads_usage_fee_vnd) : "—"}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500 whitespace-nowrap">{r.channel || "—"}</td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      <ExpiryCell days={r.expiry_days} label={r.usage_rights_until} />
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      <StatusBadge value={r.status} />
                    </td>
                    <td className="px-4 py-3">
                      <VideoLinks ig={r.link_ig} tiktok={r.link_tiktok} youtube={r.link_youtube} />
                    </td>
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      <button onClick={() => setEditRow(r)}
                        className={`text-xs px-2 py-1 rounded ${
                          r.kol_record_id
                            ? "text-indigo-500 hover:text-indigo-700"
                            : "text-gray-400 hover:text-indigo-500 border border-dashed border-gray-300 hover:border-indigo-300"
                        }`}>
                        {r.kol_record_id ? "Edit" : "+ Details"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* edit modal */}
      {editRow && (
        <EditModal
          row={editRow}
          branches={branches}
          onClose={() => setEditRow(null)}
          onSaved={() => { setEditRow(null); load(); }}
        />
      )}
    </div>
  );
}
