/**
 * Paid Ads Performance — Phase 3
 */
import { useEffect, useState } from "react";
import axios from "axios";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";

const CHANNELS = ["Meta", "Google", "TikTok"];

function fmt(val, currency) {
  if (val == null) return "—";
  const sym = CURRENCY_SYMBOLS[currency] || "";
  return sym + new Intl.NumberFormat("en").format(Math.round(val));
}

function RoasBadge({ value }) {
  if (value == null) return <span className="text-gray-400">—</span>;
  const cls = value >= 3 ? "text-green-700 bg-green-50" : value >= 1.5 ? "text-yellow-700 bg-yellow-50" : "text-red-600 bg-red-50";
  return <span className={"px-2 py-0.5 rounded text-xs font-semibold " + cls}>{value.toFixed(2)}x</span>;
}

function FunnelBadge({ stage }) {
  const colors = { TOF: "bg-sky-100 text-sky-700", MOF: "bg-amber-100 text-amber-700", BOF: "bg-green-100 text-green-700" };
  if (!stage) return <span className="text-gray-400">—</span>;
  return <span className={"px-2 py-0.5 rounded text-xs font-medium " + (colors[stage] || "bg-gray-100 text-gray-600")}>{stage}</span>;
}

function ChannelBadge({ channel }) {
  const colors = { Meta: "bg-blue-100 text-blue-700", Google: "bg-green-100 text-green-700", TikTok: "bg-pink-100 text-pink-700" };
  return <span className={"px-2 py-0.5 rounded text-xs font-medium " + (colors[channel] || "bg-gray-100 text-gray-600")}>{channel || "—"}</span>;
}

const EMPTY_FORM = {
  branch_id: "", campaign_name: "", channel: "", target_country: "",
  target_audience: "", funnel_stage: "", date_from: "", date_to: "",
  cost_native: "", impressions: "", clicks: "", leads: "", bookings: "", revenue_native: "",
};

export default function Ads() {
  const { branches, currentBranch, isAll, selected } = useBranch();
  const currency = currentBranch?.currency || currentBranch?.native_currency || "VND";
  const [summary, setSummary] = useState([]);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [filterChannel, setFilterChannel] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editId, setEditId] = useState(null);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [tab, setTab] = useState("summary");
  const [datePreset, setDatePreset] = useState("30d");

  const getDateRange = (preset) => {
    const today = new Date();
    const pad = n => String(n).padStart(2, '0');
    const fmt = d => d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate());
    const to = fmt(today);
    if (preset === '7d') { const d = new Date(today); d.setDate(d.getDate()-6); return { from: fmt(d), to }; }
    if (preset === '30d') { const d = new Date(today); d.setDate(d.getDate()-29); return { from: fmt(d), to }; }
    if (preset === 'month') { return { from: fmt(new Date(today.getFullYear(), today.getMonth(), 1)), to }; }
    return { from: null, to: null };
  };

  const load = () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (!isAll && selected) params.set('branch_id', selected);
    if (filterChannel) params.set('channel', filterChannel);
    const { from, to } = getDateRange(datePreset);
    if (from) params.set('date_from', from);
    if (to) params.set('date_to', to);
    Promise.all([
      axios.get('/api/ads/summary?' + params),
      axios.get('/api/ads?' + params),
    ])
      .then(([sRes, rRes]) => { setSummary(sRes.data.data || []); setRows(rRes.data.data || []); })
      .catch(() => { setSummary([]); setRows([]); })
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, [selected, filterChannel, datePreset]);

  const openNew = () => { setForm({ ...EMPTY_FORM, branch_id: currentBranch?.id || "" }); setEditId(null); setShowForm(true); };
  const openEdit = (row) => {
    setForm({
      branch_id: row.branch_id, campaign_name: row.campaign_name || "",
      channel: row.channel || "", target_country: row.target_country || "",
      target_audience: row.target_audience || "", funnel_stage: row.funnel_stage || "",
      date_from: row.date_from || "", date_to: row.date_to || "",
      cost_native: row.cost_native ?? "", impressions: row.impressions ?? "",
      clicks: row.clicks ?? "", leads: row.leads ?? "",
      bookings: row.bookings ?? "", revenue_native: row.revenue_native ?? "",
    });
    setEditId(row.id); setShowForm(true);
  };

  const save = async () => {
    setSaving(true);
    const payload = {
      ...form,
      cost_native: form.cost_native !== "" ? Number(form.cost_native) : null,
      impressions: form.impressions !== "" ? Number(form.impressions) : null,
      clicks: form.clicks !== "" ? Number(form.clicks) : null,
      leads: form.leads !== "" ? Number(form.leads) : null,
      bookings: form.bookings !== "" ? Number(form.bookings) : null,
      revenue_native: form.revenue_native !== "" ? Number(form.revenue_native) : null,
    };
    try {
      editId ? await axios.put("/api/ads/" + editId, payload) : await axios.post("/api/ads", payload);
      setShowForm(false); load();
    } catch (e) { alert("Save failed: " + (e.response?.data?.detail || e.message)); }
    finally { setSaving(false); }
  };

  const del = async (id) => { if (!confirm("Delete?")) return; await axios.delete("/api/ads/" + id); load(); };

  const syncAdsPlatform = async () => {
    setSyncing(true);
    try {
      const r = await axios.post(`/api/sync/ads-platform`);
      const d = r.data.data;
      alert(
        `Ads Platform sync done in ${d.duration_s}s:\n` +
        `• ${d.synced_daily_rows} daily rows\n` +
        `• ${d.synced_ads} ad rows\n` +
        `• ${d.synced_budgets} budget plans\n` +
        `• ${d.synced_booking_matches} booking matches`
      );
      load();
    } catch (e) { alert("Sync failed: " + (e.response?.data?.detail || e.message)); }
    finally { setSyncing(false); }
  };

  const recomputeMetrics = async () => {
    setRecomputing(true);
    const bParam = !isAll && selected ? `?branch_id=${selected}` : "";
    try {
      const r = await axios.post(`/api/sync/recompute${bParam}`);
      const branches = r.data.data.branches || [];
      const totalDays = branches.reduce((s, b) => s + (b.days_recomputed || 0), 0);
      alert(`Recompute done: ${totalDays} day-rows recomputed across ${branches.length} branch(es).`);
    } catch (e) { alert("Recompute failed: " + (e.response?.data?.detail || e.message)); }
    finally { setRecomputing(false); }
  };

  const importCsv = async () => {
    setRecomputing(true);
    try {
      const r = await axios.post("/api/sync/csv", {}, { timeout: 600_000 });
      const t = r.data.data.total || {};
      alert(`CSV import done: ${t.created || 0} created, ${t.updated || 0} updated, ${t.skipped || 0} skipped. Metrics recomputed.`);
    } catch (e) {
      if (e.code === "ECONNABORTED" || e.message?.includes("timeout")) {
        alert("Import is running in the background (large dataset). Check metrics in a few minutes — refresh the dashboard once complete.");
      } else {
        alert("CSV import failed: " + (e.response?.data?.detail || e.message));
      }
    }
    finally { setRecomputing(false); }
  };

  const totalSpend = summary.reduce((s, r) => s + (r.cost_native || 0), 0);
  const totalRev = summary.reduce((s, r) => s + (r.revenue_native || 0), 0);
  const blendedRoas = totalSpend > 0 ? (totalRev / totalSpend).toFixed(2) : null;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Paid Ads Performance</h1>
          <p className="text-xs text-gray-400 mt-0.5">Meta · Google · TikTok</p>
        </div>
        <div className="flex gap-2 flex-wrap">
          <button onClick={importCsv} disabled={recomputing}
            className="px-3 py-2 bg-gray-700 text-white text-sm rounded-lg hover:bg-gray-800 disabled:opacity-50">
            {recomputing ? "Working…" : "↻ Import CSV"}
          </button>
          <button onClick={recomputeMetrics} disabled={recomputing}
            className="px-3 py-2 bg-gray-600 text-white text-sm rounded-lg hover:bg-gray-700 disabled:opacity-50">
            {recomputing ? "Working…" : "⟳ Recompute Metrics"}
          </button>
          <button onClick={syncAdsPlatform} disabled={syncing}
            className="px-3 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50">
            {syncing ? "Syncing…" : "↻ Sync Ads Platform"}
          </button>
          <button onClick={openNew} className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700">+ Add Campaign</button>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        {[{ label: "Total Spend", value: fmt(totalSpend, currency) }, { label: "Total Revenue", value: fmt(totalRev, currency) }, { label: "Blended ROAS", value: blendedRoas ? blendedRoas + "x" : "—" }].map(s => (
          <div key={s.label} className="bg-white rounded-xl border border-gray-200 px-5 py-4">
            <p className="text-xs text-gray-400">{s.label}</p>
            <p className="text-2xl font-bold text-gray-800 mt-1">{s.value}</p>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex gap-2">
          <button onClick={() => setFilterChannel("")} className={"px-3 py-1 rounded text-xs font-medium border " + (!filterChannel ? "bg-gray-800 text-white border-gray-800" : "text-gray-500 border-gray-200")}>All</button>
          {CHANNELS.map(c => (
            <button key={c} onClick={() => setFilterChannel(filterChannel === c ? "" : c)} className={"px-3 py-1 rounded text-xs font-medium border " + (filterChannel === c ? "bg-gray-800 text-white border-gray-800" : "text-gray-500 border-gray-200")}>{c}</button>
          ))}
        </div>
        <div className="flex gap-1">
          {[{ v: "7d", l: "7 days" }, { v: "30d", l: "30 days" }, { v: "month", l: "This month" }, { v: "all", l: "All time" }].map(({ v, l }) => (
            <button key={v} onClick={() => setDatePreset(v)} className={"px-3 py-1 rounded text-xs font-medium border " + (datePreset === v ? "bg-indigo-600 text-white border-indigo-600" : "text-gray-500 border-gray-200 hover:border-gray-400")}>{l}</button>
          ))}
        </div>
      </div>

      <div className="flex gap-1 border-b border-gray-200">
        {["summary", "campaigns"].map(t => (
          <button key={t} onClick={() => setTab(t)} className={"px-4 py-2 text-sm capitalize " + (tab === t ? "border-b-2 border-indigo-600 text-indigo-600 font-medium" : "text-gray-500 hover:text-gray-700")}>{t}</button>
        ))}
      </div>

      {loading ? (
        <div className="bg-white rounded-xl border p-8 text-center text-gray-400 animate-pulse">Loading…</div>
      ) : tab === "summary" ? (
        <div className="bg-white rounded-xl border border-gray-200 overflow-x-auto">
          {summary.length === 0 ? <div className="p-8 text-center text-gray-400">No data yet. Add campaigns to see summary.</div> : (
            <table className="w-full text-sm">
              <thead><tr className="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide text-left">
                <th className="px-5 py-3">Channel</th><th className="px-4 py-3 text-right">Spend</th>
                <th className="px-4 py-3 text-right">Revenue</th><th className="px-4 py-3 text-center">ROAS</th>
                <th className="px-4 py-3 text-right">Impressions</th><th className="px-4 py-3 text-right">Clicks</th>
                <th className="px-4 py-3 text-center">CTR%</th><th className="px-4 py-3 text-right">Bookings</th>
              </tr></thead>
              <tbody className="divide-y divide-gray-100">
                {summary.map(r => (
                  <tr key={r.channel} className="hover:bg-gray-50">
                    <td className="px-5 py-3"><ChannelBadge channel={r.channel} /></td>
                    <td className="px-4 py-3 text-right font-mono">{fmt(r.cost_native, currency)}</td>
                    <td className="px-4 py-3 text-right font-mono">{fmt(r.revenue_native, currency)}</td>
                    <td className="px-4 py-3 text-center"><RoasBadge value={r.roas} /></td>
                    <td className="px-4 py-3 text-right">{(r.impressions || 0).toLocaleString()}</td>
                    <td className="px-4 py-3 text-right">{(r.clicks || 0).toLocaleString()}</td>
                    <td className="px-4 py-3 text-center text-gray-600">{r.ctr_pct != null ? r.ctr_pct + "%" : "—"}</td>
                    <td className="px-4 py-3 text-right">{r.bookings || 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 overflow-x-auto">
          {rows.length === 0 ? <div className="p-8 text-center text-gray-400">No campaigns yet.</div> : (
            <table className="w-full text-sm">
              <thead><tr className="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide text-left">
                <th className="px-5 py-3">Campaign</th><th className="px-4 py-3">Channel</th>
                <th className="px-4 py-3">TA</th><th className="px-4 py-3">Country</th>
                <th className="px-4 py-3">Funnel</th><th className="px-4 py-3">Date</th>
                <th className="px-4 py-3 text-right">Spend</th>
                <th className="px-4 py-3 text-right">Revenue</th><th className="px-4 py-3 text-center">ROAS</th>
                <th className="px-4 py-3 text-right">Clicks</th><th className="px-4 py-3 text-right">Leads</th>
                <th className="px-4 py-3"></th>
              </tr></thead>
              <tbody className="divide-y divide-gray-100">
                {rows.map(r => (
                  <tr key={r.id} className="hover:bg-gray-50">
                    <td className="px-5 py-3 max-w-[200px]">
                      <p className="font-medium text-gray-800 text-xs truncate" title={r.campaign_name}>{r.campaign_name || "—"}</p>
                      {r.ad_name && r.ad_name !== r.campaign_name && <p className="text-gray-400 text-xs truncate">{r.ad_name}</p>}
                    </td>
                    <td className="px-4 py-3"><ChannelBadge channel={r.channel} /></td>
                    <td className="px-4 py-3 text-xs text-gray-600 whitespace-nowrap">{r.target_audience || "—"}</td>
                    <td className="px-4 py-3 text-gray-600 text-xs">{r.target_country || "—"}</td>
                    <td className="px-4 py-3 text-xs"><FunnelBadge stage={r.funnel_stage} /></td>
                    <td className="px-4 py-3 text-gray-500 text-xs whitespace-nowrap">{r.date_from || "—"}</td>
                    <td className="px-4 py-3 text-right font-mono text-xs">{fmt(r.cost_native || r.cost_vnd, currency)}</td>
                    <td className="px-4 py-3 text-right font-mono text-xs">{fmt(r.revenue_native || r.revenue_vnd, currency)}</td>
                    <td className="px-4 py-3 text-center"><RoasBadge value={r.roas} /></td>
                    <td className="px-4 py-3 text-right text-xs text-gray-500">{r.clicks?.toLocaleString() || "—"}</td>
                    <td className="px-4 py-3 text-right text-xs text-gray-500">{r.leads?.toLocaleString() || "—"}</td>
                    <td className="px-4 py-3 text-right">
                      <button onClick={() => openEdit(r)} className="text-indigo-500 hover:text-indigo-700 text-xs mr-3">Edit</button>
                      <button onClick={() => del(r.id)} className="text-red-400 hover:text-red-600 text-xs">Del</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-2xl mx-4 p-6 space-y-4 max-h-[90vh] overflow-y-auto">
            <h2 className="font-semibold text-gray-800">{editId ? "Edit Campaign" : "New Campaign"}</h2>
            <div className="grid grid-cols-2 gap-3">
              <div className="col-span-2">
                <label className="text-xs text-gray-500 mb-1 block">Branch</label>
                <select value={form.branch_id} onChange={e => setForm(p => ({ ...p, branch_id: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
                  <option value="">— select branch —</option>
                  {branches.map(b => <option key={b.id} value={b.id}>{b.name}</option>)}
                </select>
              </div>
              <div className="col-span-2">
                <label className="text-xs text-gray-500 mb-1 block">Campaign Name</label>
                <input value={form.campaign_name} onChange={e => setForm(p => ({ ...p, campaign_name: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" />
              </div>
              <div>
                <label className="text-xs text-gray-500 mb-1 block">Channel</label>
                <select value={form.channel} onChange={e => setForm(p => ({ ...p, channel: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
                  <option value="">— select —</option>
                  {["Meta", "Google", "TikTok"].map(o => <option key={o} value={o}>{o}</option>)}
                </select>
              </div>
              <div>
                <label className="text-xs text-gray-500 mb-1 block">Funnel Stage</label>
                <select value={form.funnel_stage} onChange={e => setForm(p => ({ ...p, funnel_stage: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm">
                  <option value="">— select —</option>
                  {["TOF", "MOF", "BOF"].map(o => <option key={o} value={o}>{o}</option>)}
                </select>
              </div>
              {[{ k: "target_country", l: "Target Country" }, { k: "target_audience", l: "Target Audience" }].map(({ k, l }) => (
                <div key={k}><label className="text-xs text-gray-500 mb-1 block">{l}</label><input value={form[k]} onChange={e => setForm(p => ({ ...p, [k]: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" /></div>
              ))}
              {[{ k: "date_from", l: "Date From" }, { k: "date_to", l: "Date To" }].map(({ k, l }) => (
                <div key={k}><label className="text-xs text-gray-500 mb-1 block">{l}</label><input type="date" value={form[k]} onChange={e => setForm(p => ({ ...p, [k]: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" /></div>
              ))}
              {[{ k: "cost_native", l: `Spend (${currency})` }, { k: "revenue_native", l: `Revenue (${currency})` }, { k: "impressions", l: "Impressions" }, { k: "clicks", l: "Clicks" }, { k: "leads", l: "Leads" }, { k: "bookings", l: "Bookings" }].map(({ k, l }) => (
                <div key={k}><label className="text-xs text-gray-500 mb-1 block">{l}</label><input type="number" value={form[k]} onChange={e => setForm(p => ({ ...p, [k]: e.target.value }))} className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm" /></div>
              ))}
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
