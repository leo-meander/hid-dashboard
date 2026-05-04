/**
 * CreativeCopies — Copy component library. Derived verdict from combos.
 */
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useBranch } from "../context/BranchContext";
import { listCopies, createCopy, getCopy } from "../api/copies";
import { listAngles } from "../api/angles";
import VerdictBadge from "../components/VerdictBadge";
import SyncBadge from "../components/SyncBadge";
import { AUDIENCES, getTAClasses } from "../constants/audiences";

const CHANNELS = ["Facebook", "Instagram", "TikTok", "YouTube", "Meta", "Google"];
const FORMATS = ["Single Image", "Carousel", "Video", "Reel", "Story", "Collection", "Text"];
const LANGUAGES = ["Vietnamese", "English", "Japanese", "Korean", "Thai", "Indonesian", "Malay"];

const EMPTY = {
  angle_id: "", branch_id: "", channel: "", ad_format: "", target_audience: [],
  country_target: "", language: "", headline: "", primary_text: "", landing_page_url: "", tags: "",
};

export default function CreativeCopies() {
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
    channel: searchParams.get("channel") || "",
    language: searchParams.get("language") || "",
    target_audience: searchParams.get("target_audience") || "",
    derived_verdict: searchParams.get("derived_verdict") || "",
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
    Promise.all([listCopies(p), listAngles({ branch_id: !isAll ? selected : undefined })])
      .then(([c, a]) => { setRows(c); setAngles(a); })
      .finally(() => setLoading(false));
  };
  useEffect(load, [selected, searchParams.toString()]);

  const openDetail = (id) => getCopy(id).then(setDetail);

  const save = () => {
    setSaving(true);
    const data = { ...form };
    if (!data.branch_id && selected && !isAll) data.branch_id = selected;
    if (!data.angle_id) delete data.angle_id;
    data.tags = data.tags ? data.tags.split(",").map(t => t.trim()).filter(Boolean) : undefined;
    createCopy(data).then(() => { setShowForm(false); setForm(EMPTY); load(); }).finally(() => setSaving(false));
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Copy Library</h1>
          <p className="text-xs text-gray-400 mt-0.5">
            Derived verdict from combos
            <SyncBadge timestamp={rows[0]?.data_synced_at} />
          </p>
        </div>
        <button onClick={() => setShowForm(true)}
          className="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded hover:bg-indigo-700">+ New Copy</button>
      </div>

      <div className="flex gap-2 mb-4 flex-wrap">
        {[["channel", "Channel", CHANNELS], ["language", "Language", LANGUAGES],
          ["target_audience", "Audience", AUDIENCES],
          ["derived_verdict", "Verdict", ["winning", "good", "neutral", "underperformer", "kill"]],
        ].map(([k, label, opts]) => (
          <select key={k} value={f[k]} onChange={e => setFilter(k, e.target.value)}
            className="border rounded px-2 py-1 text-sm">
            <option value="">All {label}</option>
            {opts.map(o => <option key={o}>{o}</option>)}
          </select>
        ))}
      </div>

      {loading ? <div className="text-gray-400 text-sm animate-pulse">Loading...</div> :
       rows.length === 0 ? <div className="text-gray-400 text-sm">No copies found.</div> : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {rows.map(c => (
            <div key={c.id} onClick={() => openDetail(c.id)}
              className="border rounded-lg p-4 bg-white hover:shadow-md cursor-pointer">
              <div className="flex items-center justify-between mb-2">
                <span className="font-mono text-indigo-600 text-xs">{c.copy_code}</span>
                <div className="flex items-center gap-2">
                  <VerdictBadge verdict={c.derived_verdict} derived />
                  {c.combo_count > 0 && <span className="text-[10px] text-gray-400">{c.combo_count} combos</span>}
                </div>
              </div>
              <p className="text-sm font-medium truncate">{c.headline || "No headline"}</p>
              <p className="text-xs text-gray-400 mt-0.5 truncate">{c.primary_text}</p>
              <div className="flex gap-1.5 mt-2 flex-wrap">
                {Array.isArray(c.target_audience) ? c.target_audience.map(ta => {
                  const tc = getTAClasses(ta);
                  return <span key={ta} className={`text-[10px] px-1.5 py-0.5 rounded ${tc.bg} ${tc.text}`}>{ta}</span>;
                }) : c.target_audience && <span className="text-[10px] px-1.5 py-0.5 bg-indigo-50 text-indigo-600 rounded">{c.target_audience}</span>}
                {c.language && <span className="text-[10px] px-1.5 py-0.5 bg-blue-50 text-blue-600 rounded">{c.language}</span>}
                {c.channel && <span className="text-[10px] px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded">{c.channel}</span>}
              </div>
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
              <h3 className="font-semibold text-sm"><span className="font-mono text-indigo-600">{detail.copy_code}</span></h3>
              <button onClick={() => setDetail(null)} className="text-gray-400 hover:text-gray-600">&times;</button>
            </div>
            <div className="p-4 space-y-4">
              <div className="flex items-center gap-2">
                <VerdictBadge verdict={detail.derived_verdict} derived />
                <span className="text-xs text-gray-400">{detail.combo_count} combos</span>
              </div>
              <div><label className="text-xs text-gray-500">Headline</label><p className="text-sm font-medium">{detail.headline || "—"}</p></div>
              <div><label className="text-xs text-gray-500">Primary Text</label><p className="text-sm whitespace-pre-wrap">{detail.primary_text}</p></div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div><span className="text-gray-400">Channel:</span> {detail.channel}</div>
                <div><span className="text-gray-400">Audience:</span> {Array.isArray(detail.target_audience) ? detail.target_audience.join(", ") : detail.target_audience}</div>
                <div><span className="text-gray-400">Language:</span> {detail.language}</div>
                <div><span className="text-gray-400">Country:</span> {detail.country_target || "—"}</div>
              </div>
              {detail.angle_info && (
                <div className="text-xs"><span className="text-gray-400">Angle:</span> {detail.angle_info.angle_code} — {detail.angle_info.name}</div>
              )}
              {detail.combos?.length > 0 && (
                <div>
                  <label className="text-xs font-medium text-gray-500">Combos using this copy</label>
                  <div className="mt-1 space-y-1">
                    {detail.combos.map(cb => (
                      <div key={cb.id} className="flex items-center justify-between text-xs bg-gray-50 rounded p-2">
                        <div>
                          <span className="font-mono text-indigo-600">{cb.combo_code}</span>
                          <span className="text-gray-400 ml-2">{cb.material_code} ({cb.material_type})</span>
                        </div>
                        <div className="flex items-center gap-2">
                          <VerdictBadge verdict={cb.verdict} />
                          {cb.roas != null && <span className={cb.roas >= 3 ? "text-green-600" : cb.roas >= 1 ? "text-amber-500" : "text-red-500"}>{cb.roas.toFixed(2)}</span>}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* New Copy Form */}
      {showForm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto">
            <h2 className="text-lg font-semibold mb-4">New Copy</h2>
            <div className="space-y-3">
              <select value={form.angle_id} onChange={e => setForm({...form, angle_id: e.target.value})}
                className="w-full border rounded px-3 py-2 text-sm">
                <option value="">Select Angle (optional)</option>
                {angles.map(a => <option key={a.id} value={a.id}>{a.angle_code} — {a.name} ({a.hook_type})</option>)}
              </select>
              <div className="grid grid-cols-2 gap-3">
                <select value={form.channel} onChange={e => setForm({...form, channel: e.target.value})} className="border rounded px-3 py-2 text-sm" required>
                  <option value="">Channel *</option>{CHANNELS.map(c => <option key={c}>{c}</option>)}
                </select>
                <select value={form.ad_format} onChange={e => setForm({...form, ad_format: e.target.value})} className="border rounded px-3 py-2 text-sm">
                  <option value="">Format</option>{FORMATS.map(f => <option key={f}>{f}</option>)}
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
                <select value={form.language} onChange={e => setForm({...form, language: e.target.value})} className="border rounded px-3 py-2 text-sm" required>
                  <option value="">Language *</option>{LANGUAGES.map(l => <option key={l}>{l}</option>)}
                </select>
              </div>
              <input placeholder="Country target" value={form.country_target} onChange={e => setForm({...form, country_target: e.target.value})} className="w-full border rounded px-3 py-2 text-sm" />
              <input placeholder="Headline" value={form.headline} onChange={e => setForm({...form, headline: e.target.value})} className="w-full border rounded px-3 py-2 text-sm" />
              <textarea placeholder="Primary Text *" value={form.primary_text} onChange={e => setForm({...form, primary_text: e.target.value})} className="w-full border rounded px-3 py-2 text-sm" rows={4} required />
              <input placeholder="Landing Page URL" value={form.landing_page_url} onChange={e => setForm({...form, landing_page_url: e.target.value})} className="w-full border rounded px-3 py-2 text-sm" />
              <input placeholder="Tags (comma-separated)" value={form.tags} onChange={e => setForm({...form, tags: e.target.value})} className="w-full border rounded px-3 py-2 text-sm" />
            </div>
            <div className="flex justify-end gap-2 mt-4">
              <button onClick={() => { setShowForm(false); setForm(EMPTY); }} className="px-3 py-1.5 text-sm text-gray-600">Cancel</button>
              <button onClick={save} disabled={!form.channel || form.target_audience.length === 0 || !form.language || !form.primary_text || saving}
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
