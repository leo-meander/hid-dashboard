/**
 * Budget Planner — monthly allocation vs actual spend per branch & channel.
 *
 * Three views:
 *   - Monthly: single month detail per channel (Paid Ads / KOL / CRM)
 *   - Yearly:  12-month table with allocate / actual / remaining / %
 *   - Channel Splits: per-month total + channel %, edit-in-place
 *
 * Allocation is stored in VND. The page displays in branch currency for a
 * single branch, falling back to VND in "All Branches" mode.
 */
import { useEffect, useMemo, useState } from "react";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";
import {
  getYearlyBudget,
  getMonthlyBudget,
  getChannelSplits,
  upsertBudget,
  upsertBudgetBulk,
} from "../api/budgetPlanner";

const CHANNELS = [
  { key: "paid_ads", label: "Paid Ads" },
  { key: "kol", label: "KOL" },
  { key: "crm", label: "CRM" },
];

const MONTH_LABELS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function fmt(val) {
  if (val == null || isNaN(val)) return "—";
  return new Intl.NumberFormat("en").format(Math.round(val));
}

function fmtCur(val, cur) {
  if (val == null) return "—";
  const sym = CURRENCY_SYMBOLS[cur] || "";
  return sym + fmt(val);
}

function pctClass(pct) {
  if (pct == null || pct === 0) return "text-gray-400";
  if (pct > 100) return "text-red-600";
  if (pct >= 80) return "text-yellow-600";
  return "text-green-600";
}

function StatusBadge({ status }) {
  const map = {
    "Under":    "bg-yellow-50 text-yellow-700 border border-yellow-200",
    "On Track": "bg-green-50 text-green-700 border border-green-200",
    "Over":     "bg-red-50 text-red-700 border border-red-200",
  };
  const cls = map[status] || "bg-gray-50 text-gray-500 border border-gray-200";
  return (
    <span className={"inline-block px-2 py-0.5 rounded text-xs font-medium " + cls}>
      {status}
    </span>
  );
}

function ProgressBar({ pct, color = "amber" }) {
  const clamped = Math.min(Math.max(pct || 0, 0), 100);
  const fill = clamped >= 100 ? "bg-red-500"
             : clamped >= 80 ? "bg-amber-500"
             : "bg-amber-400";
  return (
    <div className="w-full h-2 bg-gray-200 rounded-full overflow-hidden">
      <div className={"h-full " + fill} style={{ width: `${clamped}%` }} />
    </div>
  );
}

export default function BudgetPlanner() {
  const { branches, selected, isAll, selectBranch } = useBranch();
  const today = new Date();
  const [tab, setTab] = useState("monthly");
  const [year, setYear] = useState(today.getFullYear());
  const [month, setMonth] = useState(today.getMonth() + 1);

  // The page is per-branch — pick the first branch when "all" is selected.
  const effectiveBranchId = useMemo(() => {
    if (!isAll) return selected;
    return branches[0]?.id || null;
  }, [isAll, selected, branches]);

  if (!effectiveBranchId) {
    return (
      <div className="text-center text-gray-400 py-16 text-sm">Loading branches…</div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-lg font-bold text-blue-600">Budget Planner</h1>
        <div className="flex items-center gap-2">
          <select
            value={effectiveBranchId}
            onChange={(e) => selectBranch(e.target.value)}
            className="border rounded px-2 py-1 text-sm"
          >
            {branches.map((b) => (
              <option key={b.id} value={b.id}>{b.name}</option>
            ))}
          </select>
          {tab === "monthly" && (
            <input
              type="month"
              value={`${year}-${String(month).padStart(2, "0")}`}
              onChange={(e) => {
                const [y, m] = e.target.value.split("-");
                setYear(Number(y));
                setMonth(Number(m));
              }}
              className="border rounded px-2 py-1 text-sm"
            />
          )}
          {tab !== "monthly" && (
            <select
              value={year}
              onChange={(e) => setYear(Number(e.target.value))}
              className="border rounded px-2 py-1 text-sm"
            >
              {[year - 1, year, year + 1].map((y) => (
                <option key={y} value={y}>{y}</option>
              ))}
            </select>
          )}
        </div>
      </div>

      <div className="flex gap-1 border-b">
        {[
          { key: "monthly", label: "Monthly" },
          { key: "yearly", label: "Yearly" },
          { key: "channel", label: "Channel Splits" },
        ].map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === t.key ? "border-indigo-600 text-indigo-600" : "border-transparent text-gray-500 hover:text-gray-700"
            }`}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "monthly" && (
        <MonthlyTab branchId={effectiveBranchId} year={year} month={month} />
      )}
      {tab === "yearly" && (
        <YearlyTab branchId={effectiveBranchId} year={year} />
      )}
      {tab === "channel" && (
        <ChannelSplitsTab branchId={effectiveBranchId} year={year} />
      )}
    </div>
  );
}

/* ── Monthly Tab ──────────────────────────────────────────────────────────── */
function MonthlyTab({ branchId, year, month }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({});

  const load = () => {
    setLoading(true);
    getMonthlyBudget({ branch_id: branchId, year, month })
      .then((d) => {
        setData(d);
        const init = {};
        for (const c of d.channels) init[c.channel] = String(c.allocated_vnd || 0);
        setDraft(init);
      })
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  };

  useEffect(load, [branchId, year, month]);

  if (loading) return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading…</div>;
  if (!data) return <div className="text-center text-gray-400 py-12 text-sm">No data available.</div>;

  const cur = data.currency || "VND";
  const total = data.total;

  const saveDraft = async () => {
    const items = data.channels.map((c) => ({
      branch_id: branchId,
      year, month,
      channel: c.channel,
      allocated_vnd: Number(draft[c.channel] || 0),
      note: c.note || null,
    }));
    await upsertBudgetBulk(items);
    setEditing(false);
    load();
  };

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg border p-4">
        <div className="flex items-center justify-between mb-1">
          <h3 className="font-semibold text-gray-900">{data.branch_name}</h3>
          <button
            onClick={() => editing ? saveDraft() : setEditing(true)}
            className="px-3 py-1 text-xs font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700"
          >
            {editing ? "Save" : "Edit Plan"}
          </button>
        </div>
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-600">Total</span>
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-yellow-50 text-yellow-700">
              {total.pct.toFixed(1)}%
            </span>
          </div>
          <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
            <span>Spent</span>
            <span>{fmt(total.actual_native)} / {fmt(total.allocated_native)} {cur}</span>
          </div>
          <ProgressBar pct={total.pct} />
          <div className="flex items-center justify-between text-xs text-gray-400 mt-1">
            <span>Projected: {fmt(total.projected_native)}</span>
            <span>{data.days_remaining}d remaining</span>
          </div>
        </div>
      </div>

      {data.channels.map((c) => (
        <div key={c.channel} className="bg-white rounded-lg border p-4">
          <div className="flex items-center justify-between mb-1">
            <span className="font-medium text-gray-900">{c.label}</span>
            <StatusBadge status={c.status} />
          </div>
          <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
            <span>Spent</span>
            {editing ? (
              <span>
                {fmt(c.actual_native)} /{" "}
                <input
                  type="number"
                  value={draft[c.channel] || ""}
                  onChange={(e) => setDraft({ ...draft, [c.channel]: e.target.value })}
                  className="w-32 border rounded px-1 py-0.5 text-right text-xs"
                />{" "}
                {cur} <span className="text-gray-400 ml-1">(VND value)</span>
              </span>
            ) : (
              <span>{fmt(c.actual_native)} / {fmt(c.allocated_native)} {cur}</span>
            )}
          </div>
          <ProgressBar pct={c.pct} />
          <div className="text-xs text-gray-400 mt-1">
            Projected: {fmt(c.projected_native)}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── Yearly Tab ───────────────────────────────────────────────────────────── */
function YearlyTab({ branchId, year }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    getYearlyBudget({ branch_id: branchId, year })
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [branchId, year]);

  if (loading) return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading…</div>;
  if (!data) return <div className="text-center text-gray-400 py-12 text-sm">No data available.</div>;

  const cur = data.currency || "VND";

  return (
    <div className="space-y-3">
      <div className="bg-white rounded-lg border p-4">
        <div className="flex items-center justify-between mb-1">
          <div>
            <h3 className="font-semibold text-gray-900">{data.branch_name}</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              {fmt(data.total_actual_native)} / {fmt(data.total_allocated_native)} {cur}
            </p>
          </div>
          <span className="px-2 py-0.5 rounded text-xs font-medium bg-yellow-50 text-yellow-700">
            {data.pct.toFixed(1)}%
          </span>
        </div>
        <ProgressBar pct={data.pct} />
      </div>

      <div className="bg-white rounded-lg border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="text-left px-4 py-2 font-medium text-gray-600">Month</th>
              <th className="text-right px-4 py-2 font-medium text-gray-600">Allocate</th>
              <th className="text-right px-4 py-2 font-medium text-gray-600">Actual Spend</th>
              <th className="text-right px-4 py-2 font-medium text-gray-600">Remaining</th>
              <th className="text-right px-4 py-2 font-medium text-gray-600">%</th>
              <th className="px-4 py-2 font-medium text-gray-600 w-32">Bar</th>
              <th className="text-left px-4 py-2 font-medium text-gray-600">Note</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {data.months.map((m) => {
              const note = (m.channels || [])
                .map((c) => c.note).filter(Boolean).join(" · ");
              return (
                <tr key={m.month} className="hover:bg-gray-50">
                  <td className="px-4 py-2 font-medium">{MONTH_LABELS[m.month - 1]}</td>
                  <td className="px-4 py-2 text-right">{fmt(m.allocated_native)}</td>
                  <td className="px-4 py-2 text-right">
                    {m.actual_native > 0 ? fmt(m.actual_native) : "—"}
                  </td>
                  <td className={"px-4 py-2 text-right " + (m.remaining_native < 0 ? "text-red-600 font-medium" : "")}>
                    {fmt(m.remaining_native)}
                  </td>
                  <td className={"px-4 py-2 text-right " + pctClass(m.pct)}>
                    {m.pct.toFixed(1)}%
                  </td>
                  <td className="px-4 py-2"><ProgressBar pct={m.pct} /></td>
                  <td className="px-4 py-2 text-gray-500 text-xs">{note || "—"}</td>
                </tr>
              );
            })}
            <tr className="bg-gray-50 font-semibold">
              <td className="px-4 py-2">Total</td>
              <td className="px-4 py-2 text-right">{fmt(data.total_allocated_native)}</td>
              <td className="px-4 py-2 text-right">{fmt(data.total_actual_native)}</td>
              <td className={"px-4 py-2 text-right " + (data.total_remaining_native < 0 ? "text-red-600" : "")}>
                {fmt(data.total_remaining_native)}
              </td>
              <td className={"px-4 py-2 text-right " + pctClass(data.pct)}>{data.pct.toFixed(1)}%</td>
              <td className="px-4 py-2"></td>
              <td className="px-4 py-2"></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ── Channel Splits Tab ───────────────────────────────────────────────────── */
function ChannelSplitsTab({ branchId, year }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState({});  // {month: {total, paid_ads_pct, kol_pct, crm_pct}}
  const [savingMonth, setSavingMonth] = useState(null);
  // 'Apply to all' inputs
  const [applyAll, setApplyAll] = useState({ paid_ads: "", kol: "", crm: "" });

  const load = () => {
    setLoading(true);
    getChannelSplits({ branch_id: branchId, year })
      .then((d) => {
        setData(d);
        const init = {};
        for (const m of d.months) {
          init[m.month] = {
            total: String(Math.round(m.total_native || 0)),
            paid_ads_pct: String(m.paid_ads_pct || 0),
            kol_pct: String(m.kol_pct || 0),
            crm_pct: String(m.crm_pct || 0),
          };
        }
        setDraft(init);
      })
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  };

  useEffect(load, [branchId, year]);

  if (loading) return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading…</div>;
  if (!data) return <div className="text-center text-gray-400 py-12 text-sm">No data available.</div>;

  const cur = data.currency || "VND";
  const rate = data.rate_to_vnd || 1;

  const sumPct = (m) => {
    const d = draft[m] || {};
    return (Number(d.paid_ads_pct) || 0) + (Number(d.kol_pct) || 0) + (Number(d.crm_pct) || 0);
  };

  const saveMonth = async (m) => {
    const d = draft[m];
    if (!d) return;
    const totalNative = Number(d.total) || 0;
    const totalVnd = totalNative * rate;  // user enters in branch currency
    const items = CHANNELS.map(({ key }) => ({
      branch_id: branchId,
      year, month: m,
      channel: key,
      allocated_vnd: totalVnd * (Number(d[key + "_pct"]) || 0) / 100,
    }));
    setSavingMonth(m);
    try {
      await upsertBudgetBulk(items);
      load();
    } finally {
      setSavingMonth(null);
    }
  };

  const applyAllMonths = async () => {
    const pcts = {
      paid_ads: Number(applyAll.paid_ads) || 0,
      kol: Number(applyAll.kol) || 0,
      crm: Number(applyAll.crm) || 0,
    };
    if (pcts.paid_ads + pcts.kol + pcts.crm === 0) return;
    const items = [];
    for (const m of data.months) {
      const totalNative = Number(draft[m.month]?.total) || 0;
      const totalVnd = totalNative * rate;
      for (const ch of CHANNELS) {
        items.push({
          branch_id: branchId,
          year, month: m.month,
          channel: ch.key,
          allocated_vnd: totalVnd * (pcts[ch.key]) / 100,
        });
      }
    }
    await upsertBudgetBulk(items);
    load();
  };

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-lg border p-4">
        <div className="flex items-center justify-between flex-wrap gap-3 mb-1">
          <div>
            <h3 className="font-semibold text-gray-900">{data.branch_name} — {year} channel splits</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Set total {cur} + channel %. Saving cascades to monthly budget plans (converted to VND).
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span className="text-gray-500">Apply to all months:</span>
            {CHANNELS.map((c) => (
              <label key={c.key} className="flex items-center gap-1">
                <span className="text-gray-600">{c.label}</span>
                <input
                  type="number"
                  value={applyAll[c.key]}
                  onChange={(e) => setApplyAll({ ...applyAll, [c.key]: e.target.value })}
                  placeholder="%"
                  className="w-16 border rounded px-1.5 py-0.5 text-right"
                />
              </label>
            ))}
            <button onClick={applyAllMonths}
              className="px-2 py-1 text-xs font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700">
              ↵ apply
            </button>
          </div>
        </div>
      </div>

      <div className="bg-white rounded-lg border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="text-left px-4 py-2 font-medium text-gray-600">Month</th>
              <th className="text-right px-4 py-2 font-medium text-gray-600">Total ({cur})</th>
              {CHANNELS.map((c) => (
                <th key={c.key} className="text-right px-4 py-2 font-medium text-gray-600">{c.label} %</th>
              ))}
              <th className="text-right px-4 py-2 font-medium text-gray-600">Sum</th>
              <th className="text-right px-4 py-2 font-medium text-gray-600">VND total</th>
              <th className="px-4 py-2"></th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {data.months.map((m) => {
              const d = draft[m.month] || {};
              const sum = sumPct(m.month);
              const sumOK = Math.abs(sum - 100) < 0.01 || sum === 0;
              return (
                <tr key={m.month}>
                  <td className="px-4 py-2 font-medium">{MONTH_LABELS[m.month - 1]}</td>
                  <td className="px-4 py-2 text-right">
                    <input
                      type="number"
                      value={d.total || ""}
                      onChange={(e) => setDraft({ ...draft, [m.month]: { ...d, total: e.target.value } })}
                      className="w-28 border rounded px-1.5 py-0.5 text-right"
                    />
                  </td>
                  {CHANNELS.map((c) => (
                    <td key={c.key} className="px-4 py-2 text-right">
                      <input
                        type="number"
                        value={d[c.key + "_pct"] || ""}
                        onChange={(e) => setDraft({ ...draft, [m.month]: { ...d, [c.key + "_pct"]: e.target.value } })}
                        className="w-16 border rounded px-1.5 py-0.5 text-right"
                      />
                    </td>
                  ))}
                  <td className={"px-4 py-2 text-right font-medium " + (sumOK ? "text-green-600" : "text-red-600")}>
                    {sum.toFixed(0)}%
                  </td>
                  <td className="px-4 py-2 text-right text-gray-500 text-xs">
                    {fmtCur((Number(d.total) || 0) * rate, "VND")}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <button
                      onClick={() => saveMonth(m.month)}
                      disabled={savingMonth === m.month || !sumOK}
                      className="px-3 py-1 text-xs font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50"
                    >
                      {savingMonth === m.month ? "…" : "Save"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
