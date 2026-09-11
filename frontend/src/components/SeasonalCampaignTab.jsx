/**
 * Marketing Activity → Seasonal Campaign.
 *
 * A seasonal push is bought through ads and sold through a rate plan, so the
 * table puts both sides on one row: what the ad platform spent and could
 * attribute, next to what actually booked on the rate plan. The two never
 * agree — ad matching misses the guest who saw the ad Tuesday and phoned in
 * Friday — and showing the gap is the point.
 *
 * Set-up is two lists of names the team types once (ad campaign, rate plan)
 * plus the campaign's cost %, which is charged against ACTUAL revenue.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { useBranch } from "../context/BranchContext";
import {
  getSeasonalCampaigns,
  getSeasonalCampaignPerformance,
  getSeasonalBranchComparison,
  createSeasonalCampaign,
  updateSeasonalCampaign,
  deleteSeasonalCampaign,
} from "../api/marketingActivity";
import ComparisonMatrix from "./ComparisonMatrix";

function fmtNum(val) {
  if (val == null) return "—";
  return new Intl.NumberFormat("en").format(Math.round(val));
}

/* ── Month on month ──────────────────────────────────────────────────
 * "Is this campaign doing better than last month?" is the question the table
 * could not answer: it showed one month at a time and left the reader to
 * remember September while looking at October.
 *
 * The previous month comes from the SAME endpoint under a different month,
 * as its own query — so the table paints on the current month's data and the
 * deltas arrive after, instead of every page load waiting on two Ads
 * Platform round trips. For the same reason the comparison is off until
 * asked for, and the choice is remembered.
 */
const CMP_KEY = "hid.seasonal.compareMoM";

const prevMonthOf = (m) => {
  const [y, mo] = String(m || "").split("-").map(Number);
  if (!y || !mo) return null;
  const d = new Date(y, mo - 2, 1); // mo-1 is this month, one less is before it
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
};

const monthLabel = (m) =>
  new Date(m + "-01").toLocaleDateString("en", { month: "short", year: "numeric" });

/* The change, printed under the number rather than beside it: eleven columns
 * already fight for width, and "▲ 12.4%" in its own column would push ROAS
 * off the right edge.
 *
 * Cost columns are grey on purpose. Spend up 40% is not the good news that
 * revenue up 40% is, and a green arrow would claim it was.
 *
 * ROAS moves in points, not percent: 1.05x → 1.33x is "+0.28x". A percentage
 * change of a ratio is a number nobody can act on.
 */
function Delta({ current, previous, prevLabel, ratio = false, cost = false }) {
  if (current == null || previous == null) return null;

  const grey = "text-gray-400";
  const good = cost ? "text-gray-500" : "text-green-600";
  const bad = cost ? "text-gray-500" : "text-red-600";

  let text;
  let cls;
  if (ratio) {
    const diff = current - previous;
    if (Math.abs(diff) < 0.005) {
      text = "—";
      cls = grey;
    } else {
      text = `${diff > 0 ? "▲" : "▼"}${Math.abs(diff).toFixed(2)}x`;
      cls = diff > 0 ? good : bad;
    }
  } else if (!previous) {
    // No base to divide by. A campaign that sold nothing last month and
    // something this month is "new", not up by infinity.
    if (!current) return null;
    text = "new";
    cls = cost ? "text-gray-500" : "text-green-600";
  } else {
    const pct = ((current - previous) / previous) * 100;
    if (Math.abs(pct) < 0.05) {
      text = "—";
      cls = grey;
    } else {
      const abs = Math.abs(pct);
      text = `${pct > 0 ? "▲" : "▼"}${abs >= 100 ? abs.toFixed(0) : abs.toFixed(1)}%`;
      cls = pct > 0 ? good : bad;
    }
  }

  const was = ratio ? `${previous.toFixed(2)}x` : fmtNum(previous);
  return (
    <span
      title={`${was} in ${prevLabel}`}
      className={"block text-[11px] leading-tight font-medium tabular-nums cursor-help " + cls}
    >
      {text}
    </span>
  );
}

/* ── Tooltips ─────────────────────────────────────────────────────────────
 * Every column here is either read from a different system or derived from
 * two others, and the table alone can't say which. So each header explains
 * where its number comes from, and each derived cell shows the arithmetic on
 * THIS row's figures — "366,943,000 x 11% = 40,363,730" settles an argument
 * that a formula in the abstract does not.
 *
 * Positioned `fixed` off the trigger's own rect rather than absolutely: the
 * table sits in an overflow-x-auto box, which clips an absolutely positioned
 * child on both axes.
 */
function Tip({ text, children, className = "" }) {
  const [pos, setPos] = useState(null);

  const show = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setPos({
      // Keep the bubble on screen at either edge of a wide table.
      x: Math.min(Math.max(r.left + r.width / 2, 150), window.innerWidth - 150),
      y: r.bottom + 6,
    });
  };

  return (
    <span
      className={"relative " + className}
      onMouseEnter={show}
      onMouseLeave={() => setPos(null)}
    >
      {children}
      {pos && (
        <span
          role="tooltip"
          style={{ left: pos.x, top: pos.y, transform: "translateX(-50%)" }}
          className="fixed z-50 w-72 rounded-lg bg-gray-900 px-3 py-2 text-xs font-normal leading-relaxed text-gray-100 shadow-lg pointer-events-none normal-case tracking-normal text-left"
        >
          {text}
        </span>
      )}
    </span>
  );
}

// Header cell that carries an explanation. The dotted underline is the only
// hint a reader gets that there is one, so it is always visible, not on hover.
function Th({ tip, label, sub, className = "" }) {
  return (
    <th className={"px-3 py-3 font-semibold text-gray-600 align-bottom " + className}>
      <Tip text={tip} className="inline-block cursor-help">
        <span className="border-b border-dotted border-gray-400">{label}</span>
        {sub && (
          <>
            <br />
            <span className="font-normal text-gray-400">{sub}</span>
          </>
        )}
      </Tip>
    </th>
  );
}

// The four derived cells, each shown as the sum it actually performed. A
// formula in the abstract still leaves "but why is MY number that?" open.
function workedOut(r) {
  const n = fmtNum;
  const pct = r.cost_pct || 0;
  return {
    roasAds:
      r.spend == null
        ? "No ROAS: ad spend could not be read from the Ads Platform for this period."
        : r.spend <= 0
        ? "No ROAS: this campaign's ads spent nothing in this period."
        : `${n(r.ads_revenue)} revenue from ads ÷ ${n(r.spend)} spend = ${(r.roas_ads ?? 0).toFixed(2)}x`,
    campaignCost: pct
      ? `${n(r.actual_revenue)} actual revenue × ${pct}% = ${n(r.campaign_cost)}`
      : "Cost % is 0, so the campaign is charged nothing beyond its ad spend.",
    totalCost:
      r.total_cost == null
        ? "Unknown: ad spend could not be read, so the full cost cannot be added up."
        : `${n(r.spend)} ad spend + ${n(r.campaign_cost)} campaign cost = ${n(r.total_cost)}`,
    roasActual:
      r.roas_actual == null
        ? "No ROAS: total cost is unknown or zero, so there is nothing to divide by."
        : `${n(r.actual_revenue)} actual revenue ÷ ${n(r.total_cost)} total cost = ${r.roas_actual.toFixed(2)}x`,
  };
}

const TIPS = (cur) => ({
  campaign:
    "Set up by hand. The ad campaign name(s) you enter drive the four Ads columns; " +
    "the Rate Plan Name(s) drive the four actual columns. Both match as " +
    "case-insensitive substrings, so a partial tag catches every decorated variant.",
  spend:
    `What the named ad campaigns spent over this period, summed from their ads on ` +
    `the Ads Platform and converted to ${cur} at each ad account's own currency. ` +
    `Meta is complete; a Google campaign with no ad-level rows can read low.`,
  adsBookings:
    "Purchases the ad platform itself counted for those campaigns. It only sees what " +
    "it can tie back to a click, so it misses the guest who saw the ad and booked " +
    "days later by phone or OTA.",
  adsRevenue:
    "Revenue the ad platform attributed to those same ads — the money behind the " +
    "Bookings from Ads count, not all revenue the campaign caused.",
  roasAds:
    "Revenue from Ads ÷ Spend. Both come off the same ad rows, so it stays a fair " +
    "ratio even where ad attribution under-counts — but it is the ad platform's " +
    "view of itself, not the whole campaign.",
  actualBookings:
    "Every reservation that came in on this campaign's Rate Plan Name(s), counted by " +
    "Date Booked (not stay date). Excludes cancelled bookings and non-paying sources " +
    "(Blogger / House Use / Special Case / Work Exchange).",
  actualRevenue:
    "What those same reservations actually billed. Normally larger than Revenue from " +
    "Ads, because it includes every booking on the rate plan however the guest found it.",
  costPct:
    "Typed by hand — the campaign's own cost (room discount, amenity, gift) as a share " +
    "of the revenue it brought in. Click the cell to change it; nothing recomputes it.",
  campaignCost: "Revenue actual × Cost %.",
  totalCost:
    "Spend + Campaign cost — everything the campaign cost you, ad money and giveaway " +
    "together.",
  roasActual:
    "Revenue actual ÷ Total cost. The number to plan on: every booking the rate plan " +
    "brought in, against everything the campaign cost.",
});

function RoasBadge({ value }) {
  if (value == null) return <span className="text-gray-400">—</span>;
  const cls =
    value >= 3 ? "text-green-700 bg-green-50"
    : value >= 1.5 ? "text-yellow-700 bg-yellow-50"
    : "text-red-600 bg-red-50";
  return (
    <span className={"px-2 py-0.5 rounded text-xs font-semibold " + cls}>
      {value.toFixed(2)}x
    </span>
  );
}

/* Cost % edited in place — the one number on this row a human tunes, and the
 * one they tune repeatedly while a campaign is running. Making them reopen
 * the set-up dialog for it turns a two-second correction into six clicks.
 * Same click-to-edit shape as the Campaign cell on the CRM tab. */
function CostPctCell({ value, onSave, saving }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef(null);

  const start = () => {
    setDraft(String(value ?? 0));
    setEditing(true);
    setTimeout(() => inputRef.current?.select(), 0);
  };

  const commit = () => {
    setEditing(false);
    const next = Number(draft);
    // A typo is not an instruction to set 0% — leave the old value alone.
    if (draft.trim() === "" || Number.isNaN(next)) return;
    const clamped = Math.min(100, Math.max(0, next));
    if (clamped !== Number(value ?? 0)) onSave(clamped);
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        type="number"
        step="0.01"
        min="0"
        max="100"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") setEditing(false);
        }}
        className="w-20 text-sm text-right border border-blue-400 rounded px-2 py-1 bg-white outline-none"
      />
    );
  }

  // Reads as a field, not as text: a permanent bordered box plus a pencil, so
  // nobody has to hover the cell to discover it is the one number here they
  // are allowed to change.
  return (
    <button
      onClick={start}
      title="Click to edit — charged on actual revenue"
      className="ml-auto flex items-center gap-1.5 rounded-md border border-dashed border-gray-300 bg-white px-2 py-1 text-sm tabular-nums transition-colors hover:border-indigo-400 hover:bg-indigo-50"
    >
      {saving ? (
        <span className="text-gray-400">Saving…</span>
      ) : (
        <>
          <span className={value ? "text-gray-800" : "text-gray-400"}>{value || 0}%</span>
          <PencilIcon />
        </>
      )}
    </button>
  );
}

function PencilIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"
         className="h-3 w-3 shrink-0 text-gray-400">
      <path d="M13.586 3.586a2 2 0 1 1 2.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793 3 14.172V17h2.828l8.379-8.379-2.828-2.828z" />
    </svg>
  );
}

// Names are typed one per line — the same shape they get pasted in from a
// campaign list, and no ambiguity about commas inside a campaign name.
const linesToList = (text) =>
  (text || "").split("\n").map((s) => s.trim()).filter(Boolean);
const listToLines = (list) => (list || []).join("\n");

const EMPTY_FORM = {
  name: "",
  ads: "",
  ratePlans: "",
  costPct: "0",
  notes: "",
  is_active: true,
};

function SetupDialog({ campaign, onClose, onSaved }) {
  const [form, setForm] = useState(
    campaign
      ? {
          name: campaign.name,
          ads: listToLines(campaign.ads_campaign_names),
          ratePlans: listToLines(campaign.rate_plan_names),
          costPct: String(campaign.cost_pct ?? 0),
          notes: campaign.notes || "",
          is_active: campaign.is_active,
        }
      : EMPTY_FORM
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  const set = (k, v) => setForm((p) => ({ ...p, [k]: v }));

  const submit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    const body = {
      name: form.name.trim(),
      ads_campaign_names: linesToList(form.ads),
      rate_plan_names: linesToList(form.ratePlans),
      cost_pct: Number(form.costPct) || 0,
      notes: form.notes.trim() || null,
      is_active: form.is_active,
    };
    try {
      if (campaign) await updateSeasonalCampaign(campaign.id, body);
      else await createSeasonalCampaign(body);
      onSaved();
    } catch (err) {
      // Keep the dialog open with what they typed — a closed dialog after a
      // rejected save looks exactly like a successful one.
      setError(err?.response?.data?.detail || err?.message || "Could not save");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <form
        onSubmit={submit}
        className="bg-white rounded-lg shadow-xl w-full max-w-lg max-h-[90vh] overflow-y-auto"
      >
        <div className="px-5 py-4 border-b">
          <h3 className="font-semibold text-gray-900">
            {campaign ? "Edit campaign" : "New seasonal campaign"}
          </h3>
          <p className="text-xs text-gray-500 mt-1">
            Names match as case-insensitive substrings, so a partial tag like{" "}
            <code className="bg-gray-100 px-1 rounded">TET2027</code> catches every
            decorated variant.
          </p>
        </div>

        <div className="px-5 py-4 space-y-4">
          <div>
            <label className="block text-xs font-semibold text-gray-600 mb-1">
              Campaign name
            </label>
            <input
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              required
              maxLength={200}
              placeholder="Tet 2027"
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold text-gray-600 mb-1">
              Ads campaign name(s) — one per line
            </label>
            <textarea
              value={form.ads}
              onChange={(e) => set("ads", e.target.value)}
              rows={3}
              placeholder={"TET2027\nTet Early Bird"}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono"
            />
            <p className="text-xs text-gray-400 mt-1">
              Drives Spend, Bookings from Ads and Revenue from Ads.
            </p>
          </div>

          <div>
            <label className="block text-xs font-semibold text-gray-600 mb-1">
              Rate Plan Name(s) — one per line
            </label>
            <textarea
              value={form.ratePlans}
              onChange={(e) => set("ratePlans", e.target.value)}
              rows={3}
              placeholder={"TET 2027 Package"}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm font-mono"
            />
            <p className="text-xs text-gray-400 mt-1">
              Drives the real numbers. Matched on Rate Plan Name and Room Type,
              by Date Booked.
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">
                Cost % of campaign
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  max="100"
                  value={form.costPct}
                  onChange={(e) => set("costPct", e.target.value)}
                  className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm"
                />
                <span className="text-sm text-gray-500">%</span>
              </div>
              <p className="text-xs text-gray-400 mt-1">
                Charged on actual revenue.
              </p>
            </div>
            <div className="flex items-end pb-2">
              <label className="flex items-center gap-2 text-sm text-gray-700">
                <input
                  type="checkbox"
                  checked={form.is_active}
                  onChange={(e) => set("is_active", e.target.checked)}
                />
                Active
              </label>
            </div>
          </div>

          <div>
            <label className="block text-xs font-semibold text-gray-600 mb-1">
              Notes (optional)
            </label>
            <input
              value={form.notes}
              onChange={(e) => set("notes", e.target.value)}
              maxLength={500}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm"
            />
          </div>

          {error && (
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded px-3 py-2">
              {error}
            </p>
          )}
        </div>

        <div className="px-5 py-4 border-t flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 text-sm text-gray-600 hover:text-gray-800"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving}
            className="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}

/* ── Compare Branches — campaign × branch ────────────────────────────────── */
// Every figure is VND regardless of the branch's own currency: five branches
// on TWD/JPY/VND only become comparable once they're on one scale. That is
// also why this view ignores the page's branch selector — it IS the answer to
// "which branch did this land in".
const COMPARE_METRICS = [
  { key: "actual_revenue", label: "Revenue actual",
    tip: "Billed by every reservation on the campaign's rate plan, by Date Booked." },
  { key: "actual_bookings", label: "Bookings actual",
    tip: "Reservations that came in on the campaign's rate plan, by Date Booked." },
  { key: "spend", label: "Spend",
    tip: "What the campaign's ads spent, from the Ads Platform." },
  { key: "ads_revenue", label: "Revenue from Ads",
    tip: "Revenue the ad platform attributed to those ads — only what it could tie to a click." },
  { key: "ads_bookings", label: "Bookings from Ads",
    tip: "Purchases the ad platform counted for those ads." },
  { key: "total_cost", label: "Total cost",
    tip: "Ad spend + the campaign's own cost (actual revenue × cost %)." },
  { key: "roas_actual", label: "ROAS actual", ratio: true,
    tip: "Revenue actual ÷ total cost, per branch. No column total — adding ratios means nothing." },
];

/* Search matches the campaign name AND the ad-campaign / rate-plan names
 * behind it: six months from now the team will remember "the one on EARLY26"
 * long after they've forgotten what they called it. Filtering client-side is
 * deliberate — the definitions are already loaded, and a request per keystroke
 * would make a small list feel slower than a big one. */
function matchesSearch(row, needle) {
  if (!needle) return true;
  const q = needle.toLowerCase();
  return [
    row.name,
    ...(row.ads_campaign_names || []),
    ...(row.rate_plan_names || []),
    row.notes || "",
  ].some((s) => (s || "").toLowerCase().includes(q));
}

function SearchBox({ value, onChange, count, total }) {
  return (
    <div className="flex items-center gap-2">
      <div className="relative">
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Search campaigns…"
          className="border border-gray-200 rounded-lg pl-3 pr-7 py-1.5 text-sm w-56"
        />
        {value && (
          <button
            onClick={() => onChange("")}
            title="Clear"
            className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-sm leading-none"
          >
            ×
          </button>
        )}
      </div>
      {value && (
        <span className="text-xs text-gray-400 whitespace-nowrap">
          {count} of {total}
        </span>
      )}
    </div>
  );
}

function SeasonalBranchComparison({ month, ytd, periodLabel, search, searchBox }) {
  const { branches: allowedBranches } = useBranch();
  const [metric, setMetric] = useState("actual_revenue");

  const params = ytd ? { date_from: ytd.date_from, date_to: ytd.date_to } : { month };
  const { data, isPending, isPlaceholderData } = useQuery({
    queryKey: ["seasonal-branch-comparison", month, ytd?.date_from, ytd?.date_to],
    queryFn: () => getSeasonalBranchComparison(params),
    placeholderData: keepPreviousData,
  });

  // Only branches this user may see, in the backend's display order.
  const branches = useMemo(() => {
    if (!data?.branches) return [];
    const allowed = new Set(allowedBranches.map((b) => b.id));
    return data.branches.filter((b) => allowed.size === 0 || allowed.has(b.branch_id));
  }, [data, allowedBranches]);

  const allRows = data?.rows || [];
  const rows = allRows.filter((r) => matchesSearch(r, search));

  if (isPending && !data) {
    return <div className="text-center text-gray-400 py-12 text-sm animate-pulse">Loading…</div>;
  }
  if (!data || branches.length === 0 || allRows.length === 0) {
    return (
      <p className="text-gray-400 text-sm text-center py-8">
        No seasonal campaign data to compare for {periodLabel}.
      </p>
    );
  }

  const active = COMPARE_METRICS.find((m) => m.key === metric);
  const isRatio = Boolean(active?.ratio);

  return (
    <div className={"space-y-4 transition-opacity duration-150 " + (isPlaceholderData ? "opacity-40 pointer-events-none" : "")}>
      <div className="flex items-start justify-between flex-wrap gap-3">
        <p className="text-sm text-gray-500 max-w-2xl">
          Each seasonal campaign split across branches, in VND for cross-branch parity
          — so a Taipei row and a Saigon row can be read against each other. Filtered by
          Date Booked over {periodLabel}. Shading is per row: the darkest cell is the
          branch that led that campaign. Hover a metric button for what it counts.
        </p>
        <div className="flex items-center gap-2 flex-wrap">
          {searchBox(rows.length, allRows.length)}
          <div className="flex gap-1 bg-gray-100 rounded-lg p-1 w-fit flex-wrap">
            {COMPARE_METRICS.map((m) => (
              <button key={m.key} onClick={() => setMetric(m.key)} title={m.tip}
                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                  metric === m.key ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
                }`}>
                {m.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {rows.length === 0 ? (
        <p className="text-gray-400 text-sm text-center py-8">
          No campaign matches &ldquo;{search}&rdquo;.
        </p>
      ) : (
      <ComparisonMatrix
        title={`By Campaign × Branch — ${active.label}${isRatio ? "" : " (VND)"}`}
        subtitle={periodLabel}
        branches={branches}
        rows={rows}
        rowLabel="Campaign"
        metric={metric}
        rowTitle={(r) => r.name}
        rowHint={(r) => `Cost %: ${r.cost_pct || 0}%`}
        formatValue={isRatio ? fmtRoas : fmtNum}
        // A ROAS column has no meaningful column sum — the Total row would be
        // adding ratios together, which means nothing.
        totalMode={isRatio ? "none" : "sum"}
      />
      )}
    </div>
  );
}

function fmtRoas(val) {
  if (val == null) return "—";
  return `${val.toFixed(2)}x`;
}

export default function SeasonalCampaignTab({ branchId, month, ytd, cur, periodLabel }) {
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState(null); // null | {campaign?: row}
  const [error, setError] = useState(null);
  const [savingPct, setSavingPct] = useState(null);
  const [view, setView] = useState("campaign"); // "campaign" | "compare"
  const [search, setSearch] = useState("");
  // Remembered: somebody who works month-on-month does it every visit, and
  // re-ticking the box each time is the kind of small tax that gets a
  // feature written off as not worth using.
  const [compare, setCompare] = useState(() => {
    try {
      return localStorage.getItem(CMP_KEY) === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(CMP_KEY, compare ? "1" : "0");
    } catch {
      /* private mode — the toggle still works, it just won't be remembered */
    }
  }, [compare]);

  const params = {};
  if (branchId) params.branch_id = branchId;
  if (ytd) {
    params.date_from = ytd.date_from;
    params.date_to = ytd.date_to;
  } else {
    params.month = month;
  }

  const { data, isPending, isPlaceholderData } = useQuery({
    queryKey: ["seasonal-campaign-performance", branchId || "all", month, ytd?.date_from, ytd?.date_to],
    queryFn: () => getSeasonalCampaignPerformance(params),
    placeholderData: keepPreviousData,
  });

  /* Last month, off the same endpoint. YTD has no "previous month" — the
   * honest comparison there would be the year before, and outside
   * daily_metrics there are no 2025 rows to compare against — so the toggle
   * is not offered in that mode.
   *
   * The key is shaped exactly like the main query's, so this is the very
   * cache entry the page already holds (or will hold) when the month picker
   * is moved back one — flipping between two months costs one fetch, not
   * four. */
  const prevMonth = ytd ? null : prevMonthOf(month);
  const prevLabel = prevMonth ? monthLabel(prevMonth) : "";
  const { data: prevData, isFetching: prevFetching } = useQuery({
    queryKey: ["seasonal-campaign-performance", branchId || "all", prevMonth, undefined, undefined],
    queryFn: () =>
      getSeasonalCampaignPerformance({
        ...(branchId ? { branch_id: branchId } : {}),
        month: prevMonth,
      }),
    enabled: Boolean(compare && prevMonth && view === "campaign"),
    placeholderData: keepPreviousData,
  });

  // Definitions are fetched separately so the dialog can open on the full
  // record even while the numbers are still loading.
  const { data: definitions } = useQuery({
    queryKey: ["seasonal-campaigns"],
    queryFn: getSeasonalCampaigns,
  });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["seasonal-campaigns"] });
    queryClient.invalidateQueries({ queryKey: ["seasonal-campaign-performance"] });
  };

  const saveCostPct = async (row, pct) => {
    setSavingPct(row.id);
    setError(null);
    try {
      await updateSeasonalCampaign(row.id, { cost_pct: pct });
      refresh();
    } catch (e) {
      // Leave the old % on screen — the cell must never show a value the
      // server didn't accept.
      setError(
        `Could not save the cost % for "${row.name}": ` +
          (e?.response?.data?.detail || e?.message || "unknown error")
      );
    } finally {
      setSavingPct(null);
    }
  };

  const remove = async (row) => {
    if (!window.confirm(`Delete "${row.name}"? The bookings and ad data stay put — only the tracking setup is removed.`)) return;
    setError(null);
    try {
      await deleteSeasonalCampaign(row.id);
      refresh();
    } catch (e) {
      setError(e?.response?.data?.detail || e?.message || "Could not delete");
    }
  };

  const prevById = useMemo(() => {
    const m = new Map();
    for (const r of prevData?.rows || []) m.set(r.id, r);
    return m;
  }, [prevData]);
  const showDeltas = compare && Boolean(prevMonth) && Boolean(prevData);

  const allRows = data?.rows || [];
  const rows = allRows.filter((r) => matchesSearch(r, search));
  const anySpendMissing = rows.some((r) => r.spend == null);
  // Warn only about campaigns currently on screen — a search that hides the
  // half-configured one should hide its warning too.
  const unconfigured = (definitions || []).filter(
    (d) =>
      !d.ads_campaign_names?.length &&
      !d.rate_plan_names?.length &&
      matchesSearch(d, search)
  );

  const searchBox = (count, total) => (
    <SearchBox value={search} onChange={setSearch} count={count} total={total} />
  );

  const tips = TIPS(cur);

  const subToggle = (
    <div className="flex gap-1 bg-gray-100 rounded-lg p-1 w-fit">
      {[
        { key: "campaign", label: "By Campaign" },
        { key: "compare", label: "Compare Branches" },
      ].map((v) => (
        <button key={v.key} onClick={() => setView(v.key)}
          className={`px-3 py-1 rounded-md text-sm font-medium transition-colors ${
            view === v.key ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
          }`}>
          {v.label}
        </button>
      ))}
    </div>
  );

  if (view === "compare") {
    return (
      <div className="space-y-4">
        {subToggle}
        <SeasonalBranchComparison
          month={month}
          ytd={ytd}
          periodLabel={periodLabel}
          search={search}
          searchBox={searchBox}
        />
      </div>
    );
  }

  const header = (
    <div className="flex items-start justify-between gap-3 flex-wrap">
      <p className="text-sm text-gray-500 max-w-3xl">
        Each campaign twice over: what the ad platform spent and could attribute back
        to a click, next to what actually booked on the rate plan. Ad matching misses
        the guest who saw the ad and booked later by phone or OTA, so{" "}
        <span className="font-medium text-gray-600">actual</span> is normally the
        larger, and truer, number.
        <br />
        <span className="text-gray-400">
          Both sides cover {periodLabel}, filtered by Date Booked. Hover any column
          heading for where its number comes from, or a calculated cell for the sum
          on this row. Cost % is the only figure you type.
          {compare && prevMonth && (
            <>
              {" "}
              The small figure under each number is the change against{" "}
              {prevLabel} — hover it for what that month read. Spend and cost
              changes are grey, not green or red: spending more is neither on its
              own.
            </>
          )}
        </span>
      </p>
      <div className="flex items-center gap-2 flex-wrap">
        {searchBox(rows.length, allRows.length)}
        {prevMonth && (
          <button
            onClick={() => setCompare((v) => !v)}
            title={
              compare
                ? `Hide the change against ${prevLabel}.`
                : `Show every number's change against ${prevLabel}. That month is ` +
                  `fetched separately, so the table below stays up while it loads.`
            }
            className={
              "px-3 py-1.5 text-sm rounded-lg border whitespace-nowrap transition-colors " +
              (compare
                ? "border-indigo-200 bg-indigo-50 text-indigo-700"
                : "border-gray-200 text-gray-600 hover:bg-gray-50")
            }
          >
            {compare ? "✓ " : ""}vs {prevLabel}
          </button>
        )}
        <button
          onClick={() => setDialog({})}
          className="px-3 py-1.5 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 whitespace-nowrap"
        >
          + Add campaign
        </button>
      </div>
    </div>
  );

  if (isPending && !data) {
    return (
      <div className="space-y-4">
        {subToggle}
        {header}
        <div className="text-center text-gray-400 py-16 text-sm animate-pulse">Loading…</div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {subToggle}
      {header}

      {error && (
        <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded px-3 py-2">
          {error}
        </p>
      )}

      {compare && prevMonth && prevFetching && !prevData && (
        <p className="text-xs text-gray-400 animate-pulse">
          Reading {prevLabel} to compare against…
        </p>
      )}

      {anySpendMissing && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-3 py-2">
          Ad spend could not be read from the Ads Platform for this period, so Spend
          and ROAS show a dash rather than a zero. The rate-plan columns are unaffected.
        </p>
      )}

      {unconfigured.length > 0 && (
        <p className="text-xs text-gray-500">
          {unconfigured.map((d) => d.name).join(", ")} —{" "}
          {unconfigured.length === 1 ? "has" : "have"} no ad campaign or rate plan name
          set yet, so every column reads zero.
        </p>
      )}

      {rows.length === 0 ? (
        <p className="text-gray-400 text-sm text-center py-12">
          {search ? (
            <>No campaign matches &ldquo;{search}&rdquo;.</>
          ) : (
            <>
              No seasonal campaigns yet. Add one with the ad campaign name and the
              rate plan name it sells on.
            </>
          )}
        </p>
      ) : (
        <div
          className={
            "bg-white rounded-lg border overflow-x-auto transition-opacity duration-150 " +
            (isPlaceholderData ? "opacity-40 pointer-events-none" : "")
          }
        >
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <Th className="text-left" tip={tips.campaign} label="Campaign" />
                <Th className="text-right" tip={tips.spend} label={`Spend (${cur})`} />
                <Th className="text-right" tip={tips.adsBookings} label="Bookings" sub="from Ads" />
                <Th className="text-right" tip={tips.adsRevenue} label="Revenue" sub="from Ads" />
                <Th className="text-right" tip={tips.roasAds} label="ROAS" sub="Ads" />
                <Th className="text-right border-l" tip={tips.actualBookings} label="Bookings" sub="actual" />
                <Th className="text-right" tip={tips.actualRevenue} label="Revenue" sub="actual" />
                <Th className="text-right" tip={tips.costPct} label="Cost %" sub="editable" />
                <Th className="text-right" tip={tips.campaignCost} label="Campaign" sub="cost" />
                <Th className="text-right" tip={tips.totalCost} label="Total" sub="cost" />
                <Th className="text-right" tip={tips.roasActual} label="ROAS" sub="actual" />
                <th className="px-3 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((r) => {
                const prev = showDeltas ? prevById.get(r.id) : null;
                // Bound once per row so the eight call sites below read as one
                // word sitting next to the number they belong to.
                const d = (key, opts = {}) =>
                  prev ? (
                    <Delta
                      current={r[key]}
                      previous={prev[key]}
                      prevLabel={prevLabel}
                      {...opts}
                    />
                  ) : null;
                return (
                <tr key={r.id} className={"hover:bg-gray-50 " + (r.is_active ? "" : "opacity-50")}>
                  <td className="px-4 py-3">
                    <p className="font-medium text-gray-900">{r.name}</p>
                    <p
                      className="text-xs text-gray-400"
                      title={[
                        `Ads: ${r.ads_campaign_names.join(", ") || "—"}`,
                        `Rate plans: ${r.rate_plan_names.join(", ") || "—"}`,
                        r.ads_source === "booking_matches"
                          ? "Ad columns came from the booking matcher — the ad rows carried no conversion field."
                          : "Ad columns came from this campaign's own ad rows.",
                      ].join("\n")}
                    >
                      {r.matched_ads} ad{r.matched_ads === 1 ? "" : "s"} matched
                      {r.ads_source === "booking_matches" ? " · via matcher" : ""}
                      {r.is_active ? "" : " · inactive"}
                    </p>
                  </td>
                  <td className="px-3 py-3 text-right">
                    {fmtNum(r.spend)}
                    {d("spend", { cost: true })}
                  </td>
                  <td className="px-3 py-3 text-right">
                    {fmtNum(r.ads_bookings)}
                    {d("ads_bookings")}
                  </td>
                  <td className="px-3 py-3 text-right">
                    {fmtNum(r.ads_revenue)}
                    {d("ads_revenue")}
                  </td>
                  <td className="px-3 py-3 text-right">
                    <Tip text={workedOut(r).roasAds} className="cursor-help">
                      <RoasBadge value={r.roas_ads} />
                    </Tip>
                    {d("roas_ads", { ratio: true })}
                  </td>
                  <td className="px-3 py-3 text-right border-l font-medium">
                    {fmtNum(r.actual_bookings)}
                    {d("actual_bookings")}
                  </td>
                  <td className="px-3 py-3 text-right font-medium">
                    {fmtNum(r.actual_revenue)}
                    {d("actual_revenue")}
                  </td>
                  <td className="px-1 py-2">
                    <CostPctCell
                      value={r.cost_pct}
                      saving={savingPct === r.id}
                      onSave={(pct) => saveCostPct(r, pct)}
                    />
                  </td>
                  <td className="px-3 py-3 text-right">
                    <Tip text={workedOut(r).campaignCost} className="cursor-help border-b border-dotted border-gray-300">
                      {fmtNum(r.campaign_cost)}
                    </Tip>
                    {/* No delta here: cost % is one stored setting, not a
                        monthly figure, so this column could only repeat the
                        change already printed under Revenue actual. */}
                  </td>
                  <td className="px-3 py-3 text-right">
                    <Tip text={workedOut(r).totalCost} className="cursor-help border-b border-dotted border-gray-300">
                      {fmtNum(r.total_cost)}
                    </Tip>
                    {d("total_cost", { cost: true })}
                  </td>
                  <td className="px-3 py-3 text-right">
                    <Tip text={workedOut(r).roasActual} className="cursor-help">
                      <RoasBadge value={r.roas_actual} />
                    </Tip>
                    {d("roas_actual", { ratio: true })}
                  </td>
                  <td className="px-3 py-3 text-right whitespace-nowrap">
                    <button
                      onClick={() =>
                        setDialog({
                          campaign: (definitions || []).find((d) => d.id === r.id) || r,
                        })
                      }
                      className="text-xs text-indigo-600 hover:underline"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => remove(r)}
                      className="text-xs text-gray-400 hover:text-red-600 ml-3"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {dialog && (
        <SetupDialog
          campaign={dialog.campaign}
          onClose={() => setDialog(null)}
          onSaved={() => {
            setDialog(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}
