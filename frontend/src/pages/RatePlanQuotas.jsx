import { useState, useEffect, useCallback, useMemo } from "react";
import { useBranch } from "../context/BranchContext";
import {
  listQuotas,
  createQuota,
  updateQuota,
  deleteQuota,
  refreshQuotas,
} from "../api/ratePlanQuotas";

/* Color tiers track the engine's bucket ladder (90/95/100). Below threshold
   stays neutral so the user only sees red when the email also fires. */
function tierColors(consumedPct, threshold) {
  const t = Number(threshold || 90);
  if (consumedPct >= 100) return { bar: "bg-red-600", text: "text-red-700", bg: "bg-red-50", border: "border-red-200" };
  if (consumedPct >= t) return { bar: "bg-amber-500", text: "text-amber-700", bg: "bg-amber-50", border: "border-amber-200" };
  if (consumedPct >= t - 10) return { bar: "bg-yellow-400", text: "text-yellow-700", bg: "bg-yellow-50", border: "border-yellow-200" };
  return { bar: "bg-emerald-500", text: "text-emerald-700", bg: "bg-emerald-50", border: "border-emerald-200" };
}

function fmtTimestamp(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-GB", {
      year: "numeric", month: "short", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return iso; }
}

/* ── Quota card ─────────────────────────────────────────────────────────── */
function QuotaCard({ quota, onEdit, onDelete }) {
  const s = quota.status || {};
  const totalCap = Number(quota.total_cap || 0);
  const totalActive = Number(s.active_count || 0);
  const overallPct = Number(s.consumed_pct || 0);
  const threshold = Number(quota.alert_threshold_pct || 90);
  const breakdown = s.by_branch || [];

  // Card color follows the worst-loaded branch — a single 100% branch should
  // make the card scream red even if the overall total is well below cap.
  const worstPct = breakdown.reduce(
    (m, r) => Math.max(m, Number(r.consumed_pct || 0)),
    overallPct,
  );
  const c = tierColors(worstPct, threshold);

  const branchCount = Object.keys(quota.branch_limits || {}).length;

  return (
    <div className={`border ${c.border} ${c.bg} rounded-lg p-5 shadow-sm`}>
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex-1">
          <h3 className="font-semibold text-gray-900 text-base">
            {quota.display_name || quota.rate_plan_name}
          </h3>
          <p className="text-xs text-gray-500 mt-0.5">
            Pattern: <code className="bg-white px-1.5 py-0.5 rounded text-[11px]">{quota.rate_plan_name}</code>
          </p>
        </div>
        <div className="flex gap-1 shrink-0">
          <button onClick={() => onEdit(quota)}
                  className="px-2.5 py-1 text-xs rounded border border-gray-300 hover:bg-white text-gray-600">
            Edit
          </button>
          <button onClick={() => onDelete(quota)}
                  className="px-2.5 py-1 text-xs rounded border border-red-300 hover:bg-red-50 text-red-600">
            Delete
          </button>
        </div>
      </div>

      <div className="flex items-end justify-between mb-2">
        <div>
          <span className={`text-3xl font-bold ${c.text}`}>{totalActive}</span>
          <span className="text-gray-400 text-lg"> / {totalCap}</span>
          <span className="text-[11px] text-gray-500 ml-2">overall</span>
        </div>
        <div className={`text-2xl font-bold ${c.text}`}>{overallPct.toFixed(2)}%</div>
      </div>

      <div className="w-full h-2.5 bg-gray-200 rounded-full overflow-hidden mb-3">
        <div className={`h-full ${c.bar} transition-all`}
             style={{ width: `${Math.min(overallPct, 100)}%` }} />
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600 mb-3">
        <span>Threshold: <strong>{threshold}%</strong></span>
        <span>Branches: <strong>{branchCount}</strong></span>
        <span>Canceled (ref): <strong className="text-gray-500">{s.canceled_count ?? 0}</strong></span>
        {s.last_alerted_at && (
          <span>Last email: @ {fmtTimestamp(s.last_alerted_at)}</span>
        )}
      </div>

      {breakdown.length > 0 && (
        <details className="text-xs" open>
          <summary className="cursor-pointer text-gray-600 hover:text-gray-900 font-medium">
            By branch ({breakdown.length})
          </summary>
          <table className="w-full mt-2 border-collapse">
            <thead>
              <tr className="text-gray-500 text-[11px]">
                <th className="text-left py-1 px-2">Branch</th>
                <th className="text-right py-1 px-2">Active / Cap</th>
                <th className="text-right py-1 px-2">%</th>
                <th className="text-right py-1 px-2 text-gray-400">Canceled</th>
                <th className="text-right py-1 px-2 text-gray-400">Alert</th>
              </tr>
            </thead>
            <tbody>
              {breakdown.map(b => {
                const pct = Number(b.consumed_pct || 0);
                const rowC = tierColors(pct, threshold);
                const bucket = Number(b.last_alert_bucket || 0);
                return (
                  <tr key={b.branch_id} className="border-t border-gray-200">
                    <td className="py-1 px-2 text-gray-700">{b.branch_name}</td>
                    <td className="py-1 px-2 text-right font-semibold text-gray-900">
                      {b.active} / {b.limit}
                    </td>
                    <td className={`py-1 px-2 text-right font-semibold ${rowC.text}`}>
                      {pct.toFixed(2)}%
                    </td>
                    <td className="py-1 px-2 text-right text-gray-400">{b.canceled}</td>
                    <td className="py-1 px-2 text-right text-[10px]">
                      {bucket > 0 ? (
                        <span className={`px-1.5 py-0.5 rounded ${
                          bucket >= 100 ? "bg-red-100 text-red-700"
                          : bucket >= 95 ? "bg-amber-100 text-amber-700"
                          : "bg-yellow-100 text-yellow-700"
                        }`}>
                          {bucket}%
                        </span>
                      ) : <span className="text-gray-300">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </details>
      )}

      <p className="text-[10px] text-gray-400 mt-3">
        Evaluated: {fmtTimestamp(s.evaluated_at)} · Auto-refreshes every 30 min
      </p>
    </div>
  );
}

/* ── Form modal ─────────────────────────────────────────────────────────── */
function QuotaForm({ initial, onSave, onCancel }) {
  const { branches } = useBranch();
  // Branches user can pick from. Oani stays selectable for the rare case
  // someone wants to include it — we don't hard-hide it. Defaults below
  // pre-uncheck Oani so the common case is one click away.
  const selectable = useMemo(
    () => (branches || []).filter(b => b.is_active !== false),
    [branches],
  );
  const oaniRe = /oani/i;

  // limits = { branch_id: cap }. Presence in this map means "selected".
  // Caps are stored as strings while typing so partial input ("", "1") works
  // before Save coerces them to ints.
  const [limits, setLimits] = useState(() => {
    const m = initial?.branch_limits || {};
    const out = {};
    for (const [k, v] of Object.entries(m)) out[k] = String(v);
    return out;
  });

  // Default cap pre-fills new selections. Seeded from the most common cap in
  // the existing quota so editing feels predictable; falls back to 100 for
  // brand-new quotas.
  const seedDefault = useMemo(() => {
    const vals = Object.values(initial?.branch_limits || {}).map(Number);
    if (vals.length === 0) return 100;
    const counts = vals.reduce((acc, v) => ((acc[v] = (acc[v] || 0) + 1), acc), {});
    return Number(Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0]);
  }, [initial]);
  const [defaultCap, setDefaultCap] = useState(seedDefault);

  const [form, setForm] = useState(initial || {
    rate_plan_name: "",
    display_name: "",
    alert_threshold_pct: 90,
    notify_email: true,
    is_active: true,
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const set = (k, v) => setForm({ ...form, [k]: v });

  const toggleBranch = (id) => {
    setLimits(prev => {
      if (id in prev) {
        const { [id]: _drop, ...rest } = prev;
        return rest;
      }
      return { ...prev, [id]: String(defaultCap) };
    });
  };

  const setBranchCap = (id, raw) => {
    // Typing a cap auto-selects the branch. Empty string keeps the row
    // selected but invalid until the user types or blurs.
    setLimits(prev => ({ ...prev, [id]: raw }));
  };

  const applyDefaultToSelected = () => {
    setLimits(prev => {
      const out = {};
      for (const k of Object.keys(prev)) out[k] = String(defaultCap);
      return out;
    });
  };

  const selectAllExclOani = () => {
    const out = {};
    for (const b of selectable) {
      if (!oaniRe.test(b.name || "")) out[b.id] = String(defaultCap);
    }
    setLimits(out);
  };
  const selectAll = () => {
    const out = {};
    for (const b of selectable) out[b.id] = String(defaultCap);
    setLimits(out);
  };
  const clearAll = () => setLimits({});

  const selectedIds = Object.keys(limits);
  const totalCap = selectedIds.reduce(
    (s, k) => s + (parseInt(limits[k], 10) || 0), 0,
  );

  const submit = async () => {
    if (selectedIds.length === 0) {
      setError("Pick at least one branch and set a cap");
      return;
    }
    // Coerce all caps to ints; reject < 1.
    const cleaned = {};
    for (const [k, v] of Object.entries(limits)) {
      const n = parseInt(v, 10);
      if (!Number.isFinite(n) || n < 1) {
        setError(`Cap for ${
          (selectable.find(b => b.id === k) || {}).name || k
        } must be at least 1`);
        return;
      }
      cleaned[k] = n;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave({ ...form, branch_limits: cleaned });
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-lg shadow-xl max-w-md w-full p-6 max-h-[90vh] overflow-y-auto">
        <h2 className="text-lg font-semibold mb-4">
          {initial ? "Edit Quota" : "New Quota"}
        </h2>

        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Rate Plan Name (substring match)
            </label>
            <input value={form.rate_plan_name}
                   onChange={e => set("rate_plan_name", e.target.value)}
                   placeholder="e.g. CRM_June 2026 Events"
                   className="w-full border border-gray-300 rounded px-3 py-2 text-sm" />
            <p className="text-[10px] text-gray-500 mt-1">
              Matches reservations where <code>rate_plan_name</code> or <code>room_type</code> contains this string.
            </p>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              Display Name (optional)
            </label>
            <input value={form.display_name || ""}
                   onChange={e => set("display_name", e.target.value)}
                   placeholder="e.g. June 2026 Event"
                   className="w-full border border-gray-300 rounded px-3 py-2 text-sm" />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Default cap
              </label>
              <input type="number" min={1} value={defaultCap}
                     onChange={e => setDefaultCap(parseInt(e.target.value || 0, 10))}
                     className="w-full border border-gray-300 rounded px-3 py-2 text-sm" />
              <p className="text-[10px] text-gray-500 mt-1">
                Pre-fills new branch selections.
              </p>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Alert at (%)
              </label>
              <input type="number" min={0} max={100} step="0.1"
                     value={form.alert_threshold_pct}
                     onChange={e => set("alert_threshold_pct", parseFloat(e.target.value || 0))}
                     className="w-full border border-gray-300 rounded px-3 py-2 text-sm" />
            </div>
          </div>

          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-medium text-gray-600">
                Branches & per-branch caps
              </label>
              <div className="flex gap-2 text-[11px]">
                <button type="button" onClick={selectAllExclOani}
                        className="text-indigo-600 hover:underline">
                  All except Oani
                </button>
                <span className="text-gray-300">|</span>
                <button type="button" onClick={selectAll}
                        className="text-indigo-600 hover:underline">
                  All
                </button>
                <span className="text-gray-300">|</span>
                <button type="button" onClick={applyDefaultToSelected}
                        className="text-indigo-600 hover:underline"
                        disabled={selectedIds.length === 0}>
                  Apply default
                </button>
                <span className="text-gray-300">|</span>
                <button type="button" onClick={clearAll}
                        className="text-gray-500 hover:underline">
                  Clear
                </button>
              </div>
            </div>
            <div className="border border-gray-300 rounded px-2 py-2 max-h-56 overflow-y-auto space-y-1">
              {selectable.length === 0 ? (
                <p className="text-xs text-gray-500">No branches loaded.</p>
              ) : selectable.map(b => {
                const checked = b.id in limits;
                return (
                  <label key={b.id}
                         className="flex items-center gap-2 text-sm cursor-pointer hover:bg-gray-50 px-1 py-0.5 rounded">
                    <input type="checkbox"
                           checked={checked}
                           onChange={() => toggleBranch(b.id)} />
                    <span className="text-gray-800 flex-1 truncate">{b.name}</span>
                    {oaniRe.test(b.name || "") && !checked && (
                      <span className="text-[10px] text-gray-400">excl. by default</span>
                    )}
                    <input
                      type="number"
                      min={1}
                      placeholder="cap"
                      value={checked ? limits[b.id] : ""}
                      disabled={!checked}
                      onChange={e => setBranchCap(b.id, e.target.value)}
                      onClick={e => e.stopPropagation()}
                      className="w-20 border border-gray-300 rounded px-2 py-1 text-sm text-right disabled:bg-gray-50 disabled:text-gray-400"
                    />
                  </label>
                );
              })}
            </div>
            <p className="text-[10px] text-gray-500 mt-1">
              {selectedIds.length} of {selectable.length} branch{selectedIds.length === 1 ? "" : "es"} selected
              {selectedIds.length > 0 && (
                <> · Total cap: <strong className="text-gray-700">{totalCap}</strong></>
              )}
            </p>
          </div>

          <div className="flex items-center gap-4 pt-1">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.notify_email}
                     onChange={e => set("notify_email", e.target.checked)} />
              Send email alerts
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.is_active}
                     onChange={e => set("is_active", e.target.checked)} />
              Active
            </label>
          </div>
        </div>

        {error && (
          <div className="mt-3 text-xs text-red-600 bg-red-50 border border-red-200 rounded p-2">
            {String(error)}
          </div>
        )}

        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onCancel}
                  className="px-4 py-2 text-sm border border-gray-300 rounded hover:bg-gray-50">
            Cancel
          </button>
          <button onClick={submit}
                  disabled={saving || !form.rate_plan_name?.trim() || selectedIds.length === 0}
                  className="px-4 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700 disabled:opacity-50">
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ── Page ───────────────────────────────────────────────────────────────── */
export default function RatePlanQuotas() {
  const [quotas, setQuotas] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [editing, setEditing] = useState(null);     // quota object or {} for new
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const data = await listQuotas();
      setQuotas(data || []);
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Load failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    // Page-level poll: pick up new counts from the 30-min cron without a
    // manual refresh. 60s is fine — counts only change on cron tick anyway.
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, [load]);

  const handleSave = async (form) => {
    if (editing && editing.id) {
      await updateQuota(editing.id, form);
    } else {
      await createQuota(form);
    }
    setEditing(null);
    await load();
  };

  const handleDelete = async (quota) => {
    if (!confirm(`Delete quota for "${quota.display_name || quota.rate_plan_name}"?`)) return;
    await deleteQuota(quota.id);
    await load();
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await refreshQuotas();
      await load();
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Refresh failed");
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="max-w-6xl mx-auto">
      <div className="flex items-start justify-between mb-6">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Rate Plan Quotas</h1>
          <p className="text-sm text-gray-500 mt-1">
            Track booking caps per CRM/event rate plan, scoped per branch. Counts refresh every 30 min from Cloudbeds.
            Each branch fires email at most once per 90% / 95% / 100% bucket.
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={handleRefresh} disabled={refreshing}
                  className="px-3 py-2 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50">
            {refreshing ? "Refreshing…" : "↻ Refresh"}
          </button>
          <button onClick={() => setEditing({})}
                  className="px-3 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700">
            + New Quota
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-4 text-sm text-red-700 bg-red-50 border border-red-200 rounded p-3">
          {String(error)}
        </div>
      )}

      {loading ? (
        <div className="text-gray-500 text-sm">Loading…</div>
      ) : quotas.length === 0 ? (
        <div className="border border-dashed border-gray-300 rounded-lg p-12 text-center">
          <p className="text-gray-500 text-sm mb-3">No quotas yet.</p>
          <button onClick={() => setEditing({})}
                  className="px-3 py-2 text-sm bg-indigo-600 text-white rounded hover:bg-indigo-700">
            Create the first quota
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {quotas.map(q => (
            <QuotaCard key={q.id} quota={q}
                       onEdit={(qq) => setEditing(qq)}
                       onDelete={handleDelete} />
          ))}
        </div>
      )}

      {editing !== null && (
        <QuotaForm initial={editing.id ? editing : null}
                   onSave={handleSave}
                   onCancel={() => setEditing(null)} />
      )}
    </div>
  );
}
