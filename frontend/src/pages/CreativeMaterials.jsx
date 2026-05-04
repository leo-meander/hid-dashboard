/**
 * CreativeMaterials — Visual assets + KOL videos component library.
 * Conditional fields for KOL vs non-KOL materials.
 */
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useBranch } from "../context/BranchContext";
import { listMaterials, createMaterial, getMaterial } from "../api/materials";
import { listAngles } from "../api/angles";
import VerdictBadge from "../components/VerdictBadge";
import SyncBadge from "../components/SyncBadge";
import { AUDIENCES, getTAClasses } from "../constants/audiences";

const TYPES = ["image", "video", "kol_video", "gif", "carousel_set", "story_template"];
const DESIGN_TYPES = ["Static", "Animated", "Short Video (<30s)", "Long Video (>30s)", "KOL Edit"];
const RATIOS = ["1:1", "4:5", "9:16", "16:9"];
const LANGUAGES = ["Vietnamese", "English", "Japanese", "Korean", "Thai", "Indonesian", "Malay"];
const ORDER_STATUSES = ["Briefing", "In Progress", "Done", "Cancelled"];

const EMPTY = {
  angle_id: "", branch_id: "", material_type: "", design_type: "",
  format_ratio: [], channel: "", target_audience: [], language: "",
  file_link: "", kol_name: "", kol_nationality: "", paid_ads_eligible: false,
  paid_ads_channel: "", usage_rights_until: "", assigned_to: "", order_status: "", tags: "",
};

export default function CreativeMaterials() {
  const { selected, isAll } = useBranch();
  const [searchParams, setSearchParams] = useSearchParams();
  const [rows, setRows] = useState([]);
  const [angles, setAngles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [saving, setSaving] = useState(false);
  const [detail, setDetail] = useState(null);

  const f = {
    material_type: searchParams.get("material_type") || "",
    target_audience: searchParams.get("target_audience") || "",
    derived_verdict: searchParams.get("derived_verdict") || "",
    paid_ads_eligible: searchParams.get("paid_ads_eligible") || "",
  };
  const setFilter = (k, v) => {
    const p = new URLSearchParams(searchParams);
    if (v) p.set(k, v); else p.delete(k);
    setSearchParams(p);
  };

  const load = () => {
    setLoading(true);
    const p = { ...f };
    if (!isAll && selected) p.branch_id = selected;
    Object.keys(p).forEach(k => { if (!p[k]) delete p[k]; });
    if (p.paid_ads_eligible) p.paid_ads_eligible = p.paid_ads_eligible === "true";
    Promise.all([listMaterials(p), listAngles({ branch_id: !isAll ? selected : undefined })])
      .then(([m, a]) => { setRows(m); setAngles(a); })
      .finally(() => setLoading(false));
  };
  useEffect(load, [selected, searchParams.toString()]);

  const openDetail = (id) => getMaterial(id).then(setDetail);

  const isKol = form.material_type === "kol_video";

  const save = () => {
    setSaving(true);
    const data = { ...form };
    if (!data.branch_id && selected && !isAll) data.branch_id = selected;
    if (!data.angle_id) delete data.angle_id;
    data.format_ratio = Array.isArray(data.format_ratio) ? data.format_ratio.join(", ") : data.format_ratio;
    if (!data.format_ratio) delete data.format_ratio;
    data.tags = data.tags ? data.tags.split(",").map(t => t.trim()).filter(Boolean) : undefined;
    if (!data.kol_name) delete data.kol_name;
    if (!data.kol_nationality) delete data.kol_nationality;
    if (!data.paid_ads_channel) delete data.paid_ads_channel;
    if (!data.usage_rights_until) delete data.usage_rights_until;
    if (!data.assigned_to) delete data.assigned_to;
    if (!data.order_status) delete data.order_status;
    createMaterial(data).then(() => { setShowForm(false); setForm(EMPTY); load(); }).finally(() => setSaving(false));
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Materials</h1>
          <p className="text-xs text-gray-400 mt-0.5">
            Visual + KOL assets · derived verdict from combos
            <SyncBadge timestamp={rows[0]?.data_synced_at} />
          </p>
        </div>
        <button onClick={() => setShowForm(true)}
          className="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700">+ New Material</button>
      </div>

      <div className="flex gap-2 mb-4 flex-wrap">
        <select value={f.material_type} onChange={e => setFilter("material_type", e.target.value)} className="border rounded px-2 py-1 text-sm">
          <option value="">All Types</option>{TYPES.map(t => <option key={t}>{t}</option>)}
        </select>
        <select value={f.target_audience} onChange={e => setFilter("target_audience", e.target.value)} className="border rounded px-2 py-1 text-sm">
          <option value="">All Audiences</option>{AUDIENCES.map(a => <option key={a}>{a}</option>)}
        </select>
        <select value={f.derived_verdict} onChange={e => setFilter("derived_verdict", e.target.value)} className="border rounded px-2 py-1 text-sm">
          <option value="">All Verdicts</option>
          {["winning", "good", "neutral", "underperformer", "kill"].map(v => <option key={v}>{v}</option>)}
        </select>
        <select value={f.paid_ads_eligible} onChange={e => setFilter("paid_ads_eligible", e.target.value)} className="border rounded px-2 py-1 text-sm">
          <option value="">Paid Ads: Any</option>
          <option value="true">Eligible</option><option value="false">Not Eligible</option>
        </select>
      </div>

      {loading ? <div className="text-gray-400 text-sm animate-pulse">Loading...</div> :
       rows.length === 0 ? <div className="text-gray-400 text-sm">No materials found.</div> : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {rows.map(m => (
            <div key={m.id} onClick={() => openDetail(m.id)}
              className="border rounded-lg p-4 bg-white hover:shadow-md cursor-pointer">
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-indigo-600 text-xs">{m.material_code}</span>
                <div className="flex items-center gap-2">
                  <VerdictBadge verdict={m.derived_verdict} derived />
                  {m.combo_count > 0 && <span className="text-[10px] text-gray-400">{m.combo_count} combos</span>}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs px-1.5 py-0.5 bg-gray-100 rounded">{m.material_type}</span>
                {m.kol_name && <span className="text-xs text-purple-600">KOL: {m.kol_name}</span>}
                {m.paid_ads_eligible && <span className="text-[10px] px-1 py-0.5 bg-green-100 text-green-700 rounded">Paid Ads</span>}
              </div>
              <div className="flex gap-1.5 mt-2 flex-wrap">
                {Array.isArray(m.target_audience) ? m.target_audience.map(ta => {
                  const tc = getTAClasses(ta);
                  return <span key={ta} className={`text-[10px] px-1.5 py-0.5 rounded ${tc.bg} ${tc.text}`}>{ta}</span>;
                }) : m.target_audience && <span className="text-[10px] px-1.5 py-0.5 bg-indigo-50 text-indigo-600 rounded">{m.target_audience}</span>}
                {m.language && <span className="text-[10px] px-1.5 py-0.5 bg-blue-50 text-blue-600 rounded">{m.language}</span>}
              </div>
              {m.file_link && <a href={m.file_link} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} className="text-xs text-blue-500 hover:underline mt-1 block">Open file</a>}
            </div>
          ))}
        </div>
      )}

      {/* Detail Drawer */}
      {detail && (
        <div className="fixed inset-0 z-50 flex items-center justify-end">
          <div className="absolute inset-0 bg-black/30" onClick={() => setDetail(null)} />
          <div className="relative w-[420px] h-full bg-white shadow-xl overflow-y-auto">
            <div className="sticky top-0 bg-white border-b px-4 py-3 flex items-center justify-between">
              <h3 className="font-semibold text-sm"><span className="font-mono text-indigo-600">{detail.material_code}</span></h3>
              <button onClick={() => setDetail(null)} className="text-gray-400 hover:text-gray-600">&times;</button>
            </div>
            <div className="p-4 space-y-3">
              <div className="flex items-center gap-2">
                <VerdictBadge verdict={detail.derived_verdict} derived />
                <span className="text-xs text-gray-400">{detail.combo_count} combos</span>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div><span className="text-gray-400">Type:</span> {detail.material_type}</div>
                <div><span className="text-gray-400">Design:</span> {detail.design_type || "—"}</div>
                <div><span className="text-gray-400">Ratio:</span> {detail.format_ratio || "—"}</div>
                <div><span className="text-gray-400">Audience:</span> {Array.isArray(detail.target_audience) ? detail.target_audience.join(", ") : detail.target_audience}</div>
                <div><span className="text-gray-400">Language:</span> {detail.language || "—"}</div>
                <div><span className="text-gray-400">Channel:</span> {detail.channel || "—"}</div>
              </div>
              {detail.kol_name && (
                <div className="bg-purple-50 rounded p-2 text-xs">
                  <p className="font-medium text-purple-700">KOL: {detail.kol_name}</p>
                  {detail.kol_nationality && <p>Nationality: {detail.kol_nationality}</p>}
                  <p>Paid Ads: {detail.paid_ads_eligible ? "Yes" : "No"} {detail.paid_ads_channel && `(${detail.paid_ads_channel})`}</p>
                  {detail.usage_rights_until && <p>Rights until: {detail.usage_rights_until}</p>}
                </div>
              )}
              {detail.assigned_to && <div className="text-xs"><span className="text-gray-400">Assigned to:</span> {detail.assigned_to}</div>}
              {detail.order_status && <div className="text-xs"><span className="text-gray-400">Status:</span> {detail.order_status}</div>}
              {detail.file_link && <a href={detail.file_link} target="_blank" rel="noreferrer" className="text-xs text-blue-500 hover:underline block">Open file</a>}
              {detail.combos?.length > 0 && (
                <div>
                  <label className="text-xs font-medium text-gray-500">Combos using this material</label>
                  <div className="mt-1 space-y-1">
                    {detail.combos.map(cb => (
                      <div key={cb.id} className="flex items-center justify-between text-xs bg-gray-50 rounded p-2">
                        <div><span className="font-mono text-indigo-600">{cb.combo_code}</span> <span className="text-gray-400">{cb.copy_code}</span></div>
                        <div className="flex items-center gap-2"><VerdictBadge verdict={cb.verdict} />{cb.roas != null && <span>{cb.roas.toFixed(2)}</span>}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* New Material Form */}
      {showForm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto">
            <h2 className="text-lg font-semibold mb-4">New Material</h2>
            <div className="space-y-3">
              <select value={form.angle_id} onChange={e => setForm({...form, angle_id: e.target.value})} className="w-full border rounded px-3 py-2 text-sm">
                <option value="">Select Angle (optional)</option>
                {angles.map(a => <option key={a.id} value={a.id}>{a.angle_code} — {a.name}</option>)}
              </select>
              <div className="grid grid-cols-2 gap-3">
                <select value={form.material_type} onChange={e => setForm({...form, material_type: e.target.value})} className="border rounded px-3 py-2 text-sm" required>
                  <option value="">Type *</option>{TYPES.map(t => <option key={t}>{t}</option>)}
                </select>
                <select value={form.design_type} onChange={e => setForm({...form, design_type: e.target.value})} className="border rounded px-3 py-2 text-sm">
                  <option value="">Design Type</option>{DESIGN_TYPES.map(d => <option key={d}>{d}</option>)}
                </select>
                <div className="border rounded px-3 py-2 text-sm col-span-2">
                  <p className="text-xs text-gray-500 mb-1">Audience * (select one or more)</p>
                  <div className="flex flex-wrap gap-2">
                    {AUDIENCES.map(a => {
                      const checked = form.target_audience.includes(a);
                      const tc = getTAClasses(a);
                      return (
                        <label key={a} className={`flex items-center gap-1 text-xs px-2 py-1 rounded cursor-pointer border ${checked ? `${tc.bg} ${tc.text} ${tc.border}` : "bg-white text-gray-500 border-gray-200"}`}>
                          <input type="checkbox" className="sr-only" checked={checked}
                            onChange={() => setForm({...form, target_audience: checked ? form.target_audience.filter(t => t !== a) : [...form.target_audience, a]})} />
                          {a}
                        </label>
                      );
                    })}
                  </div>
                </div>
                <select value={form.language} onChange={e => setForm({...form, language: e.target.value})} className="border rounded px-3 py-2 text-sm">
                  <option value="">Language</option>{LANGUAGES.map(l => <option key={l}>{l}</option>)}
                </select>
              </div>
              {/* Multi-select ratio */}
              <div className="border rounded px-3 py-2 text-sm">
                <span className="text-gray-500 text-xs block mb-1">Ratio (multi)</span>
                <div className="flex flex-wrap gap-3">
                  {RATIOS.map(r => (
                    <label key={r} className="flex items-center gap-1 cursor-pointer">
                      <input type="checkbox" checked={form.format_ratio.includes(r)}
                        onChange={e => {
                          const next = e.target.checked ? [...form.format_ratio, r] : form.format_ratio.filter(v => v !== r);
                          setForm({...form, format_ratio: next});
                        }} className="rounded border-gray-300" />
                      <span>{r}</span>
                    </label>
                  ))}
                </div>
              </div>
              <input placeholder="File Link (Google Drive)" value={form.file_link} onChange={e => setForm({...form, file_link: e.target.value})} className="w-full border rounded px-3 py-2 text-sm" />

              {/* KOL fields */}
              {isKol && (
                <div className="bg-purple-50 rounded p-3 space-y-2">
                  <p className="text-xs font-medium text-purple-700">KOL Fields</p>
                  <div className="grid grid-cols-2 gap-2">
                    <input placeholder="KOL Name" value={form.kol_name} onChange={e => setForm({...form, kol_name: e.target.value})} className="border rounded px-3 py-2 text-sm" />
                    <input placeholder="KOL Nationality" value={form.kol_nationality} onChange={e => setForm({...form, kol_nationality: e.target.value})} className="border rounded px-3 py-2 text-sm" />
                  </div>
                  <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.paid_ads_eligible} onChange={e => setForm({...form, paid_ads_eligible: e.target.checked})} /> Paid Ads Eligible</label>
                  {form.paid_ads_eligible && (
                    <div className="grid grid-cols-2 gap-2">
                      <input placeholder="Paid Ads Channel" value={form.paid_ads_channel} onChange={e => setForm({...form, paid_ads_channel: e.target.value})} className="border rounded px-3 py-2 text-sm" />
                      <input type="date" value={form.usage_rights_until} onChange={e => setForm({...form, usage_rights_until: e.target.value})} className="border rounded px-3 py-2 text-sm" />
                    </div>
                  )}
                </div>
              )}

              {/* Non-KOL fields */}
              {!isKol && (
                <div className="grid grid-cols-2 gap-3">
                  <input placeholder="Assigned to" value={form.assigned_to} onChange={e => setForm({...form, assigned_to: e.target.value})} className="border rounded px-3 py-2 text-sm" />
                  <select value={form.order_status} onChange={e => setForm({...form, order_status: e.target.value})} className="border rounded px-3 py-2 text-sm">
                    <option value="">Order Status</option>{ORDER_STATUSES.map(s => <option key={s}>{s}</option>)}
                  </select>
                </div>
              )}
              <input placeholder="Tags (comma-separated)" value={form.tags} onChange={e => setForm({...form, tags: e.target.value})} className="w-full border rounded px-3 py-2 text-sm" />
            </div>
            <div className="flex justify-end gap-2 mt-4">
              <button onClick={() => { setShowForm(false); setForm(EMPTY); }} className="px-3 py-1.5 text-sm text-gray-600">Cancel</button>
              <button onClick={save} disabled={!form.material_type || form.target_audience.length === 0 || saving}
                className="px-4 py-1.5 bg-indigo-600 text-white text-sm rounded disabled:opacity-50">
                {saving ? "Saving..." : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
