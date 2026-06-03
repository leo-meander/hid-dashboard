/**
 * Budget Planner — 4 tabs, Yearly Plan first.
 *
 *   Yearly Plan    : enter yearly total VND + monthly % (cascades to splits)
 *   Channel Splits : per-month split into Paid Ads / KOL / CRM %
 *   Monthly        : current-month status per channel
 *   Yearly         : 12-month allocate vs actual table
 *
 * Layout mirrors the Ads Platform UI screenshots — branch button row,
 * native-currency display, simple month tables.
 */
import { useEffect, useMemo, useState } from "react";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";
import SyncBadge from "../components/SyncBadge";
import {
  getYearlyBudget,
  getMonthlyBudget,
  getChannelSplits,
  getYearlyPlan,
  saveYearlyPlan,
  upsertBudgetBulk,
  upsertManualActual,
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

const TABS = [
  { key: "yearly-plan", label: "Yearly Plan" },
  { key: "channel", label: "Channel Splits" },
  { key: "monthly", label: "Monthly" },
  { key: "yearly", label: "Yearly" },
];

function fmt(val) {
  if (val == null || isNaN(val)) return "—";
  return new Intl.NumberFormat("en").format(Math.round(val));
}
function fmtDot(val) {
  if (val == null || isNaN(val)) return "—";
  return new Intl.NumberFormat("de-DE").format(Math.round(val));
}
function pctClass(pct) {
  if (pct == null || pct === 0) return "text-gray-400";
  if (pct > 100) return "text-red-600";
  if (pct >= 80) return "text-yellow-600";
  return "text-green-600";
}
function ProgressBar({ pct }) {
  const v = Math.min(Math.max(pct || 0, 0), 100);
  const fill = v >= 100 ? "bg-red-500" : v >= 80 ? "bg-amber-500" : "bg-amber-400";
  return (
    <div className="w-full h-2 bg-gray-200 rounded-full overflow-hidden">
      <div className={"h-full " + fill} style={{ width: `${v}%` }} />
    </div>
  );
}
function StatusBadge({ status }) {
  const map = {
    Under: "bg-yellow-50 text-yellow-700 border border-yellow-200",
    "On Track": "bg-green-50 text-green-700 border border-green-200",
    Over: "bg-red-50 text-red-700 border border-red-200",
  };
  const cls = map[status] || "bg-gray-50 text-gray-500 border border-gray-200";
  return (
    <span className={"inline-block px-2 py-0.5 rounded text-xs font-medium " + cls}>{status}</span>
  );
}

/* Branch button row used by every tab so layout matches Ads Platform UI. */
function BranchButtons({ branches, value, onChange }) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-sm text-gray-600">Branch:</span>
      {branches.map((b) => {
        const active = b.id === value;
        return (
          <button
            key={b.id}
            onClick={() => onChange(b.id)}
            className={`px-4 py-1.5 rounded text-sm font-medium border transition-colors ${
              active
                ? "bg-indigo-600 text-white border-indigo-600"
                : "bg-white text-gray-700 border-gray-300 hover:bg-gray-50"
            }`}
          >
            {b.name.replace(/^MEANDER\s+/i, "")}
          </button>
        );
      })}
    </div>
  );
}

export default function BudgetPlanner() {
  const { branches, selected, isAll, selectBranch } = useBranch();
  const today = new Date();
  const [tab, setTab] = useState("yearly-plan");
  const [year, setYear] = useState(today.getFullYear());
  const [month, setMonth] = useState(today.getMonth() + 1);

  const effectiveBranchId = useMemo(() => {
    if (!isAll) return selected;
    return branches[0]?.id || null;
  }, [isAll, selected, branches]);

  if (!effectiveBranchId) {
    return <div className="text-center text-gray-400 py-16 text-sm">Loading branches…</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-lg font-bold text-blue-600">Budget Planner</h1>
        <div className="flex items-center gap-2">
          {tab === "monthly" ? (
            <input
              type="month"
              value={`${year}-${String(month).padStart(2, "0")}`}
              onChange={(e) => {
                const [y, m] = e.target.value.split("-");
                setYear(Number(y));
                setMonth(Number(m));
              }}
              className="border rounded px-2 py-1.5 text-sm"
            />
          ) : (
            <select
              value={year}
              onChange={(e) => setYear(Number(e.target.value))}
              className="border rounded px-3 py-1.5 text-sm"
            >
              {[year - 1, year, year + 1].map((y) => (
                <option key={y} value={y}>{y}</option>
              ))}
            </select>
          )}
        </div>
      </div>

      <div className="flex gap-1 border-b">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === t.key
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      <BranchButtons
        branches={branches}
        value={effectiveBranchId}
        onChange={selectBranch}
      />

      {tab === "yearly-plan" && <YearlyPlanTab branchId={effectiveBranchId} year={year} />}
      {tab === "channel" && <ChannelSplitsTab branchId={effectiveBranchId} year={year} />}
      {tab === "monthly" && <MonthlyTab branchId={effectiveBranchId} year={year} month={month} />}
      {tab === "yearly" && <YearlyTab branchId={effectiveBranchId} year={year} />}
    </div>
  );
}

/* ── Yearly Plan Tab ──────────────────────────────────────────────────────── */
function YearlyPlanTab({ branchId, year }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [total, setTotal] = useState("");
  const [pcts, setPcts] = useState(Array(12).fill("8.33"));
  const [saving, setSaving] = useState(false);

  const load = () => {
    setLoading(true);
    getYearlyPlan({ branch_id: branchId, year })
      .then((d) => {
        setData(d);
        setTotal(String(Math.round(d.total_vnd || 0)));
        const arr = Array(12).fill("8.33");
        for (const m of d.months || []) {
          arr[m.month - 1] = String(m.pct);
        }
        setPcts(arr);
      })
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  };
  useEffect(load, [branchId, year]);

  if (loading || !data) {
    return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading…</div>;
  }

  const cur = data.currency || "VND";
  const rate = data.rate_to_vnd || 1;
  const totalNum = Number(total) || 0;
  const sumPct = pcts.reduce((s, p) => s + (Number(p) || 0), 0);
  const sumOK = Math.abs(sumPct - 100) < 0.5;
  const totalNative = cur === "VND" ? totalNum : totalNum / rate;

  const distributeEvenly = () => {
    const arr = Array(12).fill("8.33");
    arr[11] = "8.37";
    setPcts(arr);
  };

  const save = async () => {
    if (!sumOK) {
      if (!confirm(`% sum = ${sumPct.toFixed(2)}%, not 100%. Save anyway?`)) return;
    }
    setSaving(true);
    try {
      const monthly_pcts = {};
      for (let i = 0; i < 12; i++) monthly_pcts[String(i + 1)] = Number(pcts[i]) || 0;
      await saveYearlyPlan({
        branch_id: branchId,
        year,
        total_vnd: totalNum,
        monthly_pcts,
        cascade_to_channels: true,
      });
      load();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-white rounded-lg border p-5 space-y-4">
      <div>
        <h2 className="font-semibold text-gray-900">Yearly plan editor</h2>
        <p className="text-xs text-gray-500 mt-0.5">
          Enter the yearly total in VND, then allocate % across the 12 months.
          Each month&apos;s derived budget cascades to Channel Splits.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-6">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Yearly total (VND)</label>
          <input
            type="number"
            value={total}
            onChange={(e) => setTotal(e.target.value)}
            className="w-56 border rounded px-3 py-2 text-sm"
          />
        </div>
        <div>
          <p className="text-xs text-gray-500">Native ({cur})</p>
          <p className="text-sm font-medium mt-2">{fmtDot(totalNative)}</p>
        </div>
        <div>
          <p className="text-xs text-gray-500">% sum</p>
          <p className={"text-sm font-medium mt-2 " + (sumOK ? "text-green-600" : "text-amber-600")}>
            {sumPct.toFixed(1)}%
          </p>
        </div>
        <button
          onClick={distributeEvenly}
          className="px-3 py-2 text-xs font-medium border rounded hover:bg-gray-50"
        >
          Distribute evenly (8.33% each)
        </button>
        <button
          onClick={save}
          disabled={saving}
          className="ml-auto px-4 py-2 text-sm font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save yearly plan"}
        </button>
      </div>

      <div className="overflow-hidden rounded border">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="text-left px-4 py-2 font-medium text-gray-600 w-24">Month</th>
              <th className="text-left px-4 py-2 font-medium text-gray-600 w-40">% of year</th>
              <th className="text-right px-4 py-2 font-medium text-gray-600">Budget (VND)</th>
              <th className="text-right px-4 py-2 font-medium text-gray-600">Native ({cur})</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {pcts.map((p, i) => {
              const pct = Number(p) || 0;
              const budget = totalNum * pct / 100;
              const budgetNative = cur === "VND" ? budget : budget / rate;
              return (
                <tr key={i}>
                  <td className="px-4 py-2 font-medium text-gray-900">{MONTH_LABELS[i]}</td>
                  <td className="px-4 py-2">
                    <input
                      type="number"
                      step="0.01"
                      value={p}
                      onChange={(e) => {
                        const next = [...pcts];
                        next[i] = e.target.value;
                        setPcts(next);
                      }}
                      className="w-20 border rounded px-2 py-1 text-right text-xs"
                    /> <span className="text-xs text-gray-400 ml-1">%</span>
                  </td>
                  <td className="px-4 py-2 text-right">{fmtDot(budget)}</td>
                  <td className="px-4 py-2 text-right">{fmtDot(budgetNative)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ── Channel Splits Tab ───────────────────────────────────────────────────── */
function ChannelSplitsTab({ branchId, year }) {
  const [data, setData] = useState(null);
  const [yearly, setYearly] = useState(null);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState({});
  const [savingMonth, setSavingMonth] = useState(null);
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
    // Actual spend (per-channel allocate vs actual) for the bars below.
    getYearlyBudget({ branch_id: branchId, year })
      .then(setYearly)
      .catch(() => setYearly(null));
  };
  useEffect(load, [branchId, year]);

  if (loading || !data) {
    return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading…</div>;
  }
  const cur = data.currency || "VND";
  const rate = data.rate_to_vnd || 1;

  const sumPct = (m) => {
    const d = draft[m] || {};
    return (Number(d.paid_ads_pct) || 0) + (Number(d.kol_pct) || 0) + (Number(d.crm_pct) || 0);
  };

  const saveMonth = async (m) => {
    const d = draft[m];
    if (!d) return;
    const totalVnd = (Number(d.total) || 0) * rate;
    const items = CHANNELS.map(({ key }) => ({
      branch_id: branchId,
      year,
      month: m,
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
      const totalVnd = (Number(draft[m.month]?.total) || 0) * rate;
      for (const ch of CHANNELS) {
        items.push({
          branch_id: branchId,
          year,
          month: m.month,
          channel: ch.key,
          allocated_vnd: totalVnd * pcts[ch.key] / 100,
        });
      }
    }
    await upsertBudgetBulk(items);
    load();
  };

  return (
    <div className="bg-white rounded-lg border">
      <div className="p-5 border-b">
        <h2 className="font-semibold text-gray-900">
          {data.branch_name.replace(/^MEANDER\s+/i, "")} — {year} channel splits
        </h2>
        <p className="text-xs text-gray-500 mt-0.5">
          Monthly totals come from the Yearly tab. Set channel % to allocate
          each month across {CHANNELS.map((c) => c.label.toLowerCase()).join(" / ")}.
        </p>
      </div>

      {yearly && (
        <div className="p-5 border-b space-y-5">
          <div>
            <h3 className="font-semibold text-gray-900">Actual spend</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Live actuals vs the allocations below — overall and per channel.
            </p>
          </div>

          {/* Branch-level: same two bars as the Yearly tab. */}
          <div className="space-y-3">
            <FullYearBar
              title={data.branch_name.replace(/^MEANDER\s+/i, "") + " — all channels"}
              actual={yearly.total_actual_native}
              allocated={yearly.total_allocated_native}
              currency={cur}
              syncedAt={yearly.data_synced_at}
            />
            <YtdPaceBar months={yearly.months} year={year} currency={cur} />
          </div>

          {/* Per-channel: the same two bars, one block per channel. */}
          {CHANNELS.map((ch) => {
            const series = channelSeries(yearly.months, ch.key);
            const tot = sumSeries(series);
            return (
              <div key={ch.key} className="rounded-lg border p-4 space-y-3 bg-white">
                <FullYearBar
                  title={ch.label}
                  actual={tot.actual}
                  allocated={tot.allocated}
                  currency={cur}
                />
                <YtdPaceBar
                  months={series}
                  year={year}
                  currency={cur}
                  title={ch.label + " · YTD pace"}
                />
              </div>
            );
          })}
        </div>
      )}

      <div className="p-5 border-b flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h3 className="font-semibold text-gray-900">Allocate</h3>
          <p className="text-xs text-gray-500 mt-0.5">
            Set channel % per month, then Save each row.
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
          <button
            onClick={applyAllMonths}
            className="px-2 py-1 text-xs font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700"
          >↵ apply</button>
        </div>
      </div>

      <table className="w-full text-sm">
        <thead className="bg-gray-50 text-gray-600">
          <tr>
            <th className="text-left px-4 py-2 font-medium">Month</th>
            <th className="text-right px-4 py-2 font-medium">Total ({cur})</th>
            {CHANNELS.map((c) => (
              <th key={c.key} className="text-right px-4 py-2 font-medium">{c.label} %</th>
            ))}
            <th className="text-right px-4 py-2 font-medium">Sum</th>
            <th className="text-right px-4 py-2 font-medium">Native</th>
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
                    className="w-32 border rounded px-2 py-1 text-right"
                  />
                </td>
                {CHANNELS.map((c) => (
                  <td key={c.key} className="px-4 py-2 text-right">
                    <input
                      type="number"
                      value={d[c.key + "_pct"] || ""}
                      onChange={(e) => setDraft({ ...draft, [m.month]: { ...d, [c.key + "_pct"]: e.target.value } })}
                      className="w-16 border rounded px-2 py-1 text-right"
                    />
                  </td>
                ))}
                <td className={"px-4 py-2 text-right font-medium " + (sumOK ? "text-green-600" : "text-red-600")}>
                  {sum.toFixed(0)}%
                </td>
                <td className="px-4 py-2 text-right text-xs text-gray-500">
                  {fmtDot(Number(d.total) || 0)} {cur}
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
  );
}

/* ── Monthly Tab ──────────────────────────────────────────────────────────── */
function MonthlyTab({ branchId, year, month }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    getMonthlyBudget({ branch_id: branchId, year, month })
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  };
  useEffect(load, [branchId, year, month]);

  if (loading || !data) {
    return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading…</div>;
  }
  const cur = data.currency || "VND";
  const total = data.total;

  return (
    <div className="bg-white rounded-lg border p-5 space-y-4">
      <div>
        <h2 className="font-semibold text-gray-900">{data.branch_name.replace(/^MEANDER\s+/i, "")}</h2>
        <p className="text-xs text-gray-400 mt-0.5">
          Actuals from Paid Ads · KOL · CRM
          <SyncBadge timestamp={data.data_synced_at} />
        </p>
      </div>

      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-sm">
          <span className="text-gray-700">Total</span>
          <span className="px-2 py-0.5 rounded text-xs font-medium bg-yellow-50 text-yellow-700">
            {total.pct.toFixed(1)}%
          </span>
        </div>
        <div className="flex items-center justify-between text-xs text-gray-500">
          <span>Spent</span>
          <span>{fmtDot(total.actual_native)} / {fmtDot(total.allocated_native)} {cur}</span>
        </div>
        <ProgressBar pct={total.pct} />
        <div className="flex items-center justify-between text-xs text-gray-400 pt-1">
          <span>Projected: {fmtDot(total.projected_native)}</span>
          <span>{data.days_remaining}d remaining</span>
        </div>
      </div>

      <div className="border-t pt-4 space-y-4">
        {data.channels.map((c) => (
          <ChannelMonthlyCard
            key={c.channel}
            c={c}
            cur={cur}
            rate={data.rate_to_vnd || 1}
            branchId={branchId}
            year={year}
            month={month}
            onSaved={load}
          />
        ))}
      </div>
    </div>
  );
}

function ChannelMonthlyCard({ c, cur, rate, branchId, year, month, onSaved }) {
  const isCrm = c.channel === "crm";
  const [crmInput, setCrmInput] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (isCrm) setCrmInput(c.actual_native ? String(Math.round(c.actual_native)) : "");
  }, [c.actual_native, isCrm, month]);

  const saveCrm = async () => {
    setSaving(true);
    try {
      const native = Number(crmInput || 0);
      const vnd = native * (rate || 1);
      await upsertManualActual({
        branch_id: branchId,
        year,
        month,
        channel: "crm",
        manual_actual_vnd: vnd > 0 ? vnd : null,
      });
      onSaved();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="font-medium text-gray-900">{c.label}</span>
        <StatusBadge status={c.status} />
      </div>
      <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
        <span>Spent</span>
        <span>{fmtDot(c.actual_native)} / {fmtDot(c.allocated_native)} {cur}</span>
      </div>
      <ProgressBar pct={c.pct} />
      <div className="text-xs text-gray-400 pt-1">Projected: {fmtDot(c.projected_native)}</div>

      {isCrm && (
        <div className="mt-2 flex items-center gap-2 text-xs">
          <span className="text-gray-600">Set actual:</span>
          <input
            type="number"
            value={crmInput}
            onChange={(e) => setCrmInput(e.target.value)}
            placeholder="0"
            className="w-32 border rounded px-2 py-1 text-right"
          />
          <span className="text-gray-500">{cur}</span>
          <button
            onClick={saveCrm}
            disabled={saving}
            className="ml-auto px-3 py-1 rounded bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            {saving ? "…" : "Save"}
          </button>
        </div>
      )}
    </div>
  );
}

/* ── Budget bar helpers ───────────────────────────────────────────────────────
 * Shared between the Yearly tab and the Channel Splits "Actual" panel so the
 * branch-level and per-channel bars look identical.
 */

/* Project a months[] series down to a single channel's allocate/actual. */
function channelSeries(months, channelKey) {
  return (months || []).map((m) => {
    const ch = (m.channels || []).find((c) => c.channel === channelKey) || {};
    return {
      month: m.month,
      allocated_native: ch.allocated_native || 0,
      actual_native: ch.actual_native || 0,
    };
  });
}

function sumSeries(months) {
  return (months || []).reduce(
    (acc, m) => {
      acc.allocated += m.allocated_native || 0;
      acc.actual += m.actual_native || 0;
      return acc;
    },
    { allocated: 0, actual: 0 }
  );
}

/* Full-year cumulative actual-vs-allocate bar (matches the Yearly tab header). */
function FullYearBar({ title, actual, allocated, currency, syncedAt }) {
  const pct = allocated > 0 ? (actual / allocated) * 100 : 0;
  return (
    <div className="space-y-2">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
          <p className="text-xs text-gray-500 mt-0.5">
            {fmtDot(actual)} / {fmtDot(allocated)} {currency}
            {syncedAt != null && <SyncBadge timestamp={syncedAt} className="text-gray-400" />}
          </p>
        </div>
        <span className="px-2 py-0.5 rounded text-xs font-medium bg-yellow-50 text-yellow-700">
          {pct.toFixed(1)}%
        </span>
      </div>
      <ProgressBar pct={pct} />
    </div>
  );
}

/* ── YTD Pace Bar ─────────────────────────────────────────────────────────────
 * Compares cumulative actual vs allocate from Jan through the current month
 * (or Dec for past years). Lets the team see whether YTD spend is pacing
 * ahead or behind the planned budget at this point in the year.
 */
function YtdPaceBar({ months, year, currency, title = "YTD pace" }) {
  const now = new Date();
  const currentYear = now.getFullYear();
  const currentMonth = now.getMonth() + 1;

  if (year > currentYear) return null;
  const cutoff = year < currentYear ? 12 : currentMonth;

  const ytd = (months || [])
    .filter((m) => m.month <= cutoff)
    .reduce(
      (acc, m) => {
        acc.allocated += m.allocated_native || 0;
        acc.actual += m.actual_native || 0;
        return acc;
      },
      { allocated: 0, actual: 0 }
    );
  const remaining = ytd.allocated - ytd.actual;
  const pct = ytd.allocated > 0 ? (ytd.actual / ytd.allocated) * 100 : 0;
  const badgeCls =
    pct > 100
      ? "bg-red-50 text-red-700"
      : pct >= 80
      ? "bg-yellow-50 text-yellow-700"
      : "bg-green-50 text-green-700";

  const rangeLabel = `Jan–${MONTH_LABELS[cutoff - 1]}`;

  return (
    <div className="border rounded-lg p-3 bg-gray-50">
      <div className="flex items-start justify-between flex-wrap gap-2 mb-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">
            {title} ({rangeLabel})
          </h3>
          <p className="text-xs text-gray-500 mt-0.5">
            {fmtDot(ytd.actual)} / {fmtDot(ytd.allocated)} {currency}
            <span className={"ml-1 " + (remaining < 0 ? "text-red-600 font-medium" : "text-gray-500")}>
              · {remaining < 0 ? "over by" : "remaining"} {fmtDot(Math.abs(remaining))}
            </span>
          </p>
        </div>
        <span className={"px-2 py-0.5 rounded text-xs font-medium " + badgeCls}>
          {pct.toFixed(1)}%
        </span>
      </div>
      <ProgressBar pct={pct} />
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

  if (loading || !data) {
    return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading…</div>;
  }
  const cur = data.currency || "VND";

  return (
    <div className="bg-white rounded-lg border p-5 space-y-4">
      <FullYearBar
        title={data.branch_name.replace(/^MEANDER\s+/i, "")}
        actual={data.total_actual_native}
        allocated={data.total_allocated_native}
        currency={cur}
        syncedAt={data.data_synced_at}
      />

      <YtdPaceBar months={data.months} year={year} currency={cur} />

      <table className="w-full text-sm">
        <thead className="bg-gray-50 text-gray-600">
          <tr>
            <th className="text-left px-4 py-2 font-medium">Month</th>
            <th className="text-right px-4 py-2 font-medium">Allocate</th>
            <th className="text-right px-4 py-2 font-medium">Actual Spend</th>
            <th className="text-right px-4 py-2 font-medium">Remaining</th>
            <th className="text-right px-4 py-2 font-medium">%</th>
            <th className="px-4 py-2 font-medium w-32"></th>
            <th className="text-left px-4 py-2 font-medium">Note</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {data.months.map((m) => {
            const note = (m.channels || [])
              .map((c) => c.note).filter(Boolean).join(" · ");
            return (
              <tr key={m.month} className="hover:bg-gray-50">
                <td className="px-4 py-2 font-medium">{MONTH_LABELS[m.month - 1]}</td>
                <td className="px-4 py-2 text-right">{fmtDot(m.allocated_native)}</td>
                <td className="px-4 py-2 text-right">
                  {m.actual_native > 0 ? fmtDot(m.actual_native) : "—"}
                </td>
                <td className={"px-4 py-2 text-right " + (m.remaining_native < 0 ? "text-red-600 font-medium" : "")}>
                  {fmtDot(m.remaining_native)}
                </td>
                <td className={"px-4 py-2 text-right " + pctClass(m.pct)}>{m.pct.toFixed(1)}%</td>
                <td className="px-4 py-2"><ProgressBar pct={m.pct} /></td>
                <td className="px-4 py-2 text-gray-500 text-xs">{note || "—"}</td>
              </tr>
            );
          })}
          <tr className="bg-gray-50 font-semibold">
            <td className="px-4 py-2">Total</td>
            <td className="px-4 py-2 text-right">{fmtDot(data.total_allocated_native)}</td>
            <td className="px-4 py-2 text-right">{fmtDot(data.total_actual_native)}</td>
            <td className={"px-4 py-2 text-right " + (data.total_remaining_native < 0 ? "text-red-600" : "")}>
              {fmtDot(data.total_remaining_native)}
            </td>
            <td className={"px-4 py-2 text-right " + pctClass(data.pct)}>{data.pct.toFixed(1)}%</td>
            <td className="px-4 py-2"></td>
            <td className="px-4 py-2"></td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
