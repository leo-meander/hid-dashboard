/**
 * Persona — per-branch guest persona derived from reservation data.
 *
 * "All Branches" → 5 branch persona cards side by side for comparison.
 * A single branch → a full deep-dive with charts.
 *
 * Demographics (gender, age) are backfilled asynchronously from Cloudbeds, so
 * each card surfaces coverage and the headline omits demographic claims until
 * coverage is meaningful (handled server-side).
 */
import { useEffect, useState } from "react";
import {
  PieChart, Pie, Cell,
  BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, LabelList,
} from "recharts";
import SyncBadge from "../components/SyncBadge";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";
import { getPersonas } from "../api/persona";

const COLORS = [
  "#6366f1", "#10b981", "#f59e0b", "#ef4444", "#3b82f6",
  "#a855f7", "#06b6d4", "#ec4899", "#84cc16", "#f97316",
];
const GENDER_COLORS = { female: "#ec4899", male: "#3b82f6" };

function fmtNum(val) {
  if (val == null) return "—";
  return new Intl.NumberFormat("en").format(Math.round(val));
}
function fmtPct(val) {
  if (val == null) return "—";
  return `${val.toFixed(0)}%`;
}
function fmtMoney(val, cur) {
  if (val == null) return "—";
  const sym = CURRENCY_SYMBOLS[cur] || "";
  return sym + new Intl.NumberFormat("en").format(Math.round(val));
}

/* ── small presentational helpers ─────────────────────────────────────── */

function Stat({ label, value, sub }) {
  return (
    <div>
      <p className="text-[10px] text-gray-400 uppercase tracking-wider">{label}</p>
      <p className="text-sm font-semibold text-gray-900">{value}</p>
      {sub != null && <p className="text-[10px] text-gray-400">{sub}</p>}
    </div>
  );
}

function GenderBar({ gender }) {
  const f = gender?.female_pct ?? 0;
  const m = gender?.male_pct ?? 0;
  if (!gender?.known) {
    return <p className="text-[11px] text-gray-400 italic">No gender data yet</p>;
  }
  return (
    <div>
      <div className="flex h-3 rounded-full overflow-hidden bg-gray-100">
        <div style={{ width: `${f}%`, background: GENDER_COLORS.female }} />
        <div style={{ width: `${m}%`, background: GENDER_COLORS.male }} />
      </div>
      <div className="flex justify-between text-[10px] mt-0.5">
        <span style={{ color: GENDER_COLORS.female }}>♀ {fmtPct(f)}</span>
        <span style={{ color: GENDER_COLORS.male }}>♂ {fmtPct(m)}</span>
      </div>
    </div>
  );
}

function CoverageNote({ p }) {
  const g = p.gender?.coverage_pct ?? 0;
  const a = p.age?.coverage_pct ?? 0;
  const thin = g < 50 || a < 50;
  return (
    <p className={"text-[10px] " + (thin ? "text-amber-600" : "text-gray-400")}>
      Demographic coverage — gender {fmtPct(g)}, age {fmtPct(a)}
      {thin && " · backfill in progress"}
    </p>
  );
}

function topBand(age) {
  if (!age?.known) return null;
  return age.bands.reduce((a, b) => (b.pct > (a?.pct ?? -1) ? b : a), null);
}
function dominant(items, key) {
  if (!items?.length) return null;
  return items.reduce((a, b) => (b.pct > (a?.pct ?? -1) ? b : a), null)?.[key];
}

/* ── comparison card (All Branches view) ──────────────────────────────── */

function BranchCard({ p, color }) {
  if (p.empty) {
    return (
      <div className="shrink-0 w-72 bg-white rounded-lg border p-4">
        <h3 className="font-bold text-gray-900">{p.branch_name}</h3>
        <p className="text-sm text-gray-400 mt-4">No bookings in this window.</p>
      </div>
    );
  }
  const band = topBand(p.age);
  const country = p.top_countries?.[0];
  const chan = dominant(p.source_mix, "category");
  return (
    <div className="shrink-0 w-72 bg-white rounded-lg border p-4 space-y-3">
      <div className="flex items-center gap-2 border-b pb-2" style={{ borderColor: color }}>
        <span className="w-2.5 h-2.5 rounded-full" style={{ background: color }} />
        <h3 className="font-bold text-gray-900">{p.branch_name}</h3>
      </div>

      <p className="text-xs text-gray-600 leading-snug min-h-[48px]">{p.headline}</p>

      <GenderBar gender={p.gender} />

      <div className="grid grid-cols-2 gap-x-3 gap-y-2 pt-1">
        <Stat label="Age" value={band ? band.label : "—"} sub={p.age?.avg ? `avg ${p.age.avg}` : null} />
        <Stat label="Top market" value={country ? country.country : "—"} sub={country ? fmtPct(country.pct) : null} />
        <Stat label="Channel" value={chan || "—"} />
        <Stat label="Room / Dorm" value={`${fmtPct(p.room_type?.room_pct)} / ${fmtPct(p.room_type?.dorm_pct)}`} />
        <Stat label="Party" value={`${fmtPct(p.party?.couple_pct)} couples`} sub={`avg ${p.party?.avg_adults ?? "—"} pax`} />
        <Stat label="Stay" value={`${p.length_of_stay?.median_nights ?? "—"} nights`} />
        <Stat label="Lead time" value={`${p.lead_time?.median_days ?? "—"} d`} />
        <Stat label="ADR" value={fmtMoney(p.value?.adr_native, p.currency)} />
        <Stat label="Cancel rate" value={fmtPct(p.cancellation_rate_pct)} />
        <Stat label="Bookings" value={fmtNum(p.total_bookings)} />
      </div>

      <div className="pt-1 border-t">
        <CoverageNote p={p} />
      </div>
    </div>
  );
}

/* ── deep dive (single branch view) ───────────────────────────────────── */

function ChartCard({ title, children }) {
  return (
    <div className="bg-white rounded-lg border p-4">
      <h3 className="text-sm font-semibold text-gray-700 mb-3">{title}</h3>
      {children}
    </div>
  );
}

function DeepDive({ p }) {
  if (p.empty) {
    return <div className="bg-white rounded-lg border p-10 text-center text-gray-400">No bookings in this window for {p.branch_name}.</div>;
  }
  const genderData = [
    { name: "Female", value: p.gender?.female ?? 0, key: "female" },
    { name: "Male", value: p.gender?.male ?? 0, key: "male" },
  ].filter((d) => d.value > 0);
  const channelData = (p.source_mix || []).map((s) => ({ name: s.category, value: s.count }));
  const ageData = (p.age?.bands || []).map((b) => ({ name: b.label, pct: b.pct }));
  const countryData = (p.top_countries || []).slice(0, 8).map((c) => ({ name: c.country, pct: c.pct }));

  return (
    <div className="space-y-5">
      {/* Headline */}
      <div className="bg-gradient-to-r from-indigo-50 to-white rounded-lg border border-indigo-100 p-5">
        <p className="text-[11px] uppercase tracking-wider text-indigo-400 mb-1">{p.branch_name} · guest persona</p>
        <p className="text-lg font-semibold text-gray-900 leading-snug">{p.headline}</p>
        <div className="mt-2"><CoverageNote p={p} /></div>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KpiCard label="Bookings (window)" value={fmtNum(p.total_bookings)} />
        <KpiCard label="ADR" value={fmtMoney(p.value?.adr_native, p.currency)} sub={`avg booking ${fmtMoney(p.value?.avg_booking_native, p.currency)}`} />
        <KpiCard label="Median stay" value={`${p.length_of_stay?.median_nights ?? "—"} nights`} sub={`avg ${p.length_of_stay?.avg_nights ?? "—"}`} />
        <KpiCard label="Cancel rate" value={fmtPct(p.cancellation_rate_pct)} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Gender */}
        <ChartCard title={`Gender (${fmtPct(p.gender?.coverage_pct)} coverage)`}>
          {genderData.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={genderData} dataKey="value" nameKey="name" innerRadius={50} outerRadius={80} label={(e) => `${e.name} ${fmtPct((e.value / (p.gender.known || 1)) * 100)}`}>
                  {genderData.map((d) => <Cell key={d.key} fill={GENDER_COLORS[d.key]} />)}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          ) : <Empty label="No gender data yet — backfilling" />}
        </ChartCard>

        {/* Age */}
        <ChartCard title={`Age distribution (${fmtPct(p.age?.coverage_pct)} coverage${p.age?.avg ? `, avg ${p.age.avg}` : ""})`}>
          {p.age?.known ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={ageData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} unit="%" />
                <Tooltip formatter={(v) => [`${v}%`, "share"]} />
                <Bar dataKey="pct" fill="#6366f1" radius={[4, 4, 0, 0]}>
                  <LabelList dataKey="pct" position="top" formatter={(v) => `${v}%`} style={{ fontSize: 10, fill: "#888" }} />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty label="No age data yet — backfilling" />}
        </ChartCard>

        {/* Channel */}
        <ChartCard title="Booking channel">
          <ResponsiveContainer width="100%" height={220}>
            <PieChart>
              <Pie data={channelData} dataKey="value" nameKey="name" outerRadius={80} label={(e) => e.name}>
                {channelData.map((d, i) => <Cell key={d.name} fill={COLORS[i % COLORS.length]} />)}
              </Pie>
              <Tooltip />
            </PieChart>
          </ResponsiveContainer>
        </ChartCard>

        {/* Top countries */}
        <ChartCard title="Top markets">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={countryData} layout="vertical" margin={{ left: 20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
              <XAxis type="number" tick={{ fontSize: 11 }} unit="%" />
              <YAxis type="category" dataKey="name" tick={{ fontSize: 11 }} width={90} />
              <Tooltip formatter={(v) => [`${v}%`, "share"]} />
              <Bar dataKey="pct" fill="#10b981" radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Behaviour strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KpiCard label="Median lead time" value={`${p.lead_time?.median_days ?? "—"} days`} sub={`avg ${p.lead_time?.avg_days ?? "—"}`} />
        <KpiCard label="Room vs Dorm" value={`${fmtPct(p.room_type?.room_pct)} / ${fmtPct(p.room_type?.dorm_pct)}`} sub="private / dorm" />
        <KpiCard label="Party mix" value={`${fmtPct(p.party?.couple_pct)} couples`} sub={`solo ${fmtPct(p.party?.solo_pct)} · 3+ ${fmtPct(p.party?.group_pct)}`} />
        <KpiCard label="Avg party size" value={`${p.party?.avg_adults ?? "—"} pax`} />
      </div>
    </div>
  );
}

function KpiCard({ label, value, sub }) {
  return (
    <div className="bg-white rounded-lg border p-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider mb-1">{label}</p>
      <p className="text-2xl font-bold text-gray-900">{value}</p>
      {sub && <p className="text-[11px] text-gray-400 mt-0.5">{sub}</p>}
    </div>
  );
}

function Empty({ label }) {
  return <div className="h-[220px] flex items-center justify-center text-sm text-gray-400 italic">{label}</div>;
}

/* ── page ─────────────────────────────────────────────────────────────── */

export default function Persona() {
  const { isAll, selected, branches } = useBranch();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    const params = { months: 12 };
    if (!isAll && selected) params.branch_id = selected;
    getPersonas(params)
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  }, [isAll, selected]);

  const personas = data?.personas || [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-lg font-bold text-gray-900">Guest Persona</h1>
          <p className="text-sm text-gray-500">
            Who books each branch — last 12 months <SyncBadge timestamp={data?.data_synced_at} />
          </p>
        </div>
      </div>

      {loading ? (
        <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Building personas…</div>
      ) : !personas.length ? (
        <div className="text-center text-gray-400 py-16 text-sm">No data available.</div>
      ) : isAll ? (
        <div className="flex gap-4 overflow-x-auto pb-2">
          {personas.map((p, i) => (
            <BranchCard key={p.branch_id} p={p} color={COLORS[i % COLORS.length]} />
          ))}
        </div>
      ) : (
        <DeepDive p={personas[0]} />
      )}
    </div>
  );
}
