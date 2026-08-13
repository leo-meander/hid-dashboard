/**
 * Home — v1.5
 * All Branches selected → Group Summary Table with persistent deduction %
 * Single branch selected → KPI card + OCC heatmap
 */
import { useEffect, useState, useMemo, useCallback, useRef } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import axios from "axios";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";
import KPICard from "../components/KPICard";
import OCCHeatmap from "../components/OCCHeatmap";
import SyncBadge from "../components/SyncBadge";

const now        = new Date();
const YEAR       = now.getFullYear();
const MONTH      = now.getMonth() + 1;
const MONTH_NAME = now.toLocaleString("en-US", { month: "long", year: "numeric" });

// Shared so SingleBranchView can seed itself from whatever the All Branches
// table already fetched — see the comment where it reads this key.
const ALL_BRANCHES_KEY = ["home-all-branches", YEAR, MONTH];
const branchKpiKey = (branchId) => ["home-branch-kpi", branchId];


function fmt(value, currency) {
  if (value == null) return "—";
  const sym = CURRENCY_SYMBOLS[currency] || currency || "";
  return sym + new Intl.NumberFormat("en").format(Math.round(value));
}

function fmtPlain(value) {
  if (value == null) return "—";
  return new Intl.NumberFormat("en").format(Math.round(value));
}

function fmtPctRound(p) {
  if (p == null) return "—";
  return Math.round(p * 100) + "%";
}

// Hover tooltip — uses fixed positioning so it isn't clipped by the
// table's overflow-x-auto wrapper. Positions itself above the trigger.
function HoverTooltip({ children, content, className = "" }) {
  const [show, setShow] = useState(false);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const onEnter = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setPos({ x: r.left + r.width / 2, y: r.top });
    setShow(true);
  };
  const onLeave = () => setShow(false);
  return (
    <span
      className={"cursor-help " + className}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      {children}
      {show && (
        <div
          className="fixed z-50 w-80 p-3 bg-gray-900 text-white text-[11px] leading-relaxed rounded-lg shadow-xl pointer-events-none text-left"
          style={{ left: pos.x, top: pos.y - 10, transform: "translate(-50%, -100%)" }}
        >
          {content}
        </div>
      )}
    </span>
  );
}

// Build the breakdown content for the forecast tooltip.
// type: "current" | "next"
function forecastBreakdown(row, type) {
  const cur = row.currency || "VND";
  const isNext = type === "next";
  const days = isNext ? row.next_month_total_days : row.total_days;
  const adr = isNext ? row.next_month_adr : row.avg_adr_native;
  const roomAdr = isNext ? row.next_month_room_adr : row.room_adr_native;
  const dormAdr = isNext ? row.next_month_dorm_adr : row.dorm_adr_native;
  const rOcc = isNext ? row.predicted_room_occ_next : row.predicted_room_occ_pct;
  const dOcc = isNext ? row.predicted_dorm_occ_next : row.predicted_dorm_occ_pct;
  const fbOcc = isNext ? row.predicted_occ_next : row.predicted_occ_pct;
  const forecast = isNext ? row.next_month_forecast_native : row.occ_forecast_native;
  const totalRooms = row.total_rooms;
  const roomCount = row.total_room_count || 0;
  const dormCount = row.total_dorm_count || 0;
  const hasSplit = roomCount > 0 && dormCount > 0 && rOcc != null && dOcc != null;
  // Split ADR breakdown only when we have both per-segment ADRs from backend
  const hasSplitAdr = hasSplit && roomAdr && dormAdr;

  if (!adr || (!hasSplit && (fbOcc == null || !totalRooms))) {
    return (
      <>
        <div className="font-semibold text-white mb-1">
          {isNext ? "Next-Month Forecast" : "Forecast (this month)"}
        </div>
        <div className="text-gray-300">Insufficient data — set Predicted OCC% and confirm ADR.</div>
      </>
    );
  }

  let nightsBlock;
  let totalNights;
  let roomNights = 0;
  let dormNights = 0;
  if (hasSplit) {
    roomNights = Math.round(days * roomCount * rOcc);
    dormNights = Math.round(days * dormCount * dOcc);
    totalNights = roomNights + dormNights;
    nightsBlock = (
      <div className="space-y-0.5">
        <div className="text-gray-300">Predicted nights = days × rooms × OCC%</div>
        <div className="ml-2">
          Room: {days} × {roomCount} × {fmtPctRound(rOcc)} = <span className="font-mono">{fmtPlain(roomNights)}</span>
        </div>
        <div className="ml-2">
          Dorm: {days} × {dormCount} × {fmtPctRound(dOcc)} = <span className="font-mono">{fmtPlain(dormNights)}</span>
        </div>
        <div className="ml-2 text-white">
          Total: <span className="font-mono">{fmtPlain(totalNights)}</span> nights
        </div>
      </div>
    );
  } else {
    totalNights = Math.round(days * totalRooms * fbOcc);
    nightsBlock = (
      <div className="space-y-0.5">
        <div className="text-gray-300">Predicted nights = days × rooms × OCC%</div>
        <div className="ml-2">
          {days} × {totalRooms} × {fmtPctRound(fbOcc)} = <span className="font-mono">{fmtPlain(totalNights)}</span> nights
        </div>
      </div>
    );
  }

  // Forecast block: split when both per-segment ADRs available, otherwise single ADR
  let forecastBlock;
  if (hasSplitAdr) {
    const roomFc = roomAdr * roomNights;
    const dormFc = dormAdr * dormNights;
    forecastBlock = (
      <div className="border-t border-gray-700 pt-1.5 space-y-0.5">
        <div>Room ADR: <span className="font-mono">{fmt(roomAdr, cur)}</span></div>
        <div>Dorm ADR: <span className="font-mono">{fmt(dormAdr, cur)}</span></div>
        <div className="ml-2 text-gray-300 mt-1">
          Room: {fmt(roomAdr, cur)} × <span className="font-mono">{fmtPlain(roomNights)}</span> = <span className="font-mono">{fmt(roomFc, cur)}</span>
        </div>
        <div className="ml-2 text-gray-300">
          Dorm: {fmt(dormAdr, cur)} × <span className="font-mono">{fmtPlain(dormNights)}</span> = <span className="font-mono">{fmt(dormFc, cur)}</span>
        </div>
        <div className="text-white font-semibold mt-1">
          = <span className="font-mono">{fmt(forecast, cur)}</span>
        </div>
      </div>
    );
  } else {
    forecastBlock = (
      <div className="border-t border-gray-700 pt-1.5 space-y-0.5">
        <div>ADR: <span className="font-mono">{fmt(adr, cur)}</span></div>
        <div>
          {fmt(adr, cur)} × <span className="font-mono">{fmtPlain(totalNights)}</span>
        </div>
        <div className="text-white font-semibold">
          = <span className="font-mono">{fmt(forecast, cur)}</span>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="font-semibold text-white mb-1.5">
        {isNext ? "Next-Month Forecast" : "Forecast (this month)"}
      </div>
      <div className="text-gray-300 mb-1.5">
        {hasSplitAdr
          ? "Forecast = Room ADR × Room Nights + Dorm ADR × Dorm Nights"
          : "Forecast = ADR × Predicted Nights"}
      </div>
      <div className="border-t border-gray-700 pt-1.5 mb-1.5">{nightsBlock}</div>
      {forecastBlock}
    </>
  );
}

// Tooltip content for the Adjusted forecast (= forecast × (1 - dedPct) + otherRev).
function adjustedBreakdown(row, type) {
  const cur = row.currency || "VND";
  const isNext = type === "next";
  const base = isNext ? row.next_month_forecast_native : row.occ_forecast_native;
  const dedPct = row.deduction_pct_local || 0;
  const otherRev = row.other_revenue_local || 0;
  const adjusted = isNext ? row.adjusted_next_forecast : row.adjusted_forecast;
  return (
    <>
      <div className="font-semibold text-white mb-1.5">
        {isNext ? "Adjusted Next Forecast" : "Adjusted Forecast"}
      </div>
      <div className="text-gray-300 mb-1.5">Adjusted = Forecast × (1 − Deduct%) + Other Rev</div>
      <div className="border-t border-gray-700 pt-1.5 space-y-0.5">
        <div>Forecast: <span className="font-mono">{fmt(base, cur)}</span></div>
        <div>Deduction: <span className="font-mono">{dedPct}%</span></div>
        <div>Other Rev: <span className="font-mono">{fmt(otherRev, cur)}</span></div>
      </div>
      <div className="border-t border-gray-700 pt-1.5 mt-1.5 space-y-0.5">
        <div>
          {fmt(base, cur)} × {(1 - dedPct / 100).toFixed(2)} + {fmt(otherRev, cur)}
        </div>
        <div className="text-white font-semibold">
          = <span className="font-mono">{fmt(adjusted, cur)}</span>
        </div>
      </div>
    </>
  );
}

function AchievementBadge({ value }) {
  if (value == null) return <span className="text-gray-400">{"—"}</span>;
  const cls =
    value >= 100 ? "text-green-700 bg-green-50" :
    value >= 80  ? "text-yellow-700 bg-yellow-50" :
    value >= 60  ? "text-orange-600 bg-orange-50" :
                   "text-red-600 bg-red-50";
  return <span className={"px-2 py-0.5 rounded text-xs font-semibold " + cls}>{Math.round(value)}%</span>;
}

// The R:xx% · D:xx% sub-line that sits under a number. Falls back to the
// blended OCC when the split isn't available, and drops the Dorm half for
// rooms-only branches (where a "D:" reading would be meaningless).
function OccSplit({ room, dorm, fallback = null, hasDorm, className = "" }) {
  const cls = "text-[10px] text-gray-400 mt-0.5 " + className;
  if (room != null || dorm != null) {
    return (
      <div className={cls}>
        {room != null && <span>R:{Math.round(room * 100)}%</span>}
        {room != null && dorm != null && hasDorm && <span> · </span>}
        {dorm != null && hasDorm && <span>D:{Math.round(dorm * 100)}%</span>}
      </div>
    );
  }
  if (fallback != null) {
    const f = Math.round(fallback * 100);
    return <div className={cls}>{hasDorm ? `R:${f}% · D:${f}%` : `R:${f}%`}</div>;
  }
  return null;
}

// "OCC Forecast" never said WHICH occupancy it assumed, so the number read as
// a prediction of its own rather than as what it is: the revenue this branch
// lands on IF it finishes the month at the OCC% set on the KPI target.
function occForecastLabel(row) {
  const pct = (v) => Math.round(v * 100) + "%";
  const hasDorm = row.total_dorm_count > 0;
  const r = row.predicted_room_occ_pct;
  const d = row.predicted_dorm_occ_pct;
  let occ = null;
  if (r != null || d != null) {
    const parts = [];
    if (r != null) parts.push(hasDorm ? "Room " + pct(r) : pct(r));
    if (d != null && hasDorm) parts.push("Dorm " + pct(d));
    occ = parts.join(" · ");
  } else if (row.predicted_occ_pct != null) {
    occ = pct(row.predicted_occ_pct);
  }
  return occ
    ? "Revenue if OCC ends the month at " + occ
    : "Revenue at the predicted month-end OCC";
}

// Deduct % and Other Rev are fixed per-branch values (no monthly reset) that
// are edited in place and saved on a debounce. Both the group table and the
// single-branch view offer those edits, so the state, the debounce and the
// cache write-through live here once rather than in each of them.
function useBranchAdjustments(data) {
  const queryClient = useQueryClient();
  const [deductions, setDeductions] = useState({});
  const [otherRevs, setOtherRevs] = useState({});
  const [saving, setSaving] = useState({});
  const [savingOther, setSavingOther] = useState({});
  const saveTimers = useRef({});
  const otherTimers = useRef({});

  // Initialize from API data
  useEffect(() => {
    if (!data.length) return;
    const initDed = {};
    const initOther = {};
    for (const row of data) {
      initDed[row.branch_id] = row.deduction_pct || 0;
      initOther[row.branch_id] = row.other_revenue_native || 0;
    }
    setDeductions(initDed);
    setOtherRevs(initOther);
  }, [data]);

  // The group table and the branch page hold this branch's summary under two
  // different query keys. Patch both after a save so they can't disagree about
  // a value they both edit — the server stored exactly what we sent, so this
  // costs nothing next to refetching either summary.
  const patchCaches = useCallback((branchId, patch) => {
    queryClient.setQueryData(ALL_BRANCHES_KEY, prev =>
      Array.isArray(prev)
        ? prev.map(r => (r.branch_id === branchId ? { ...r, ...patch } : r))
        : prev);
    queryClient.setQueryData(branchKpiKey(branchId), prev =>
      prev ? { ...prev, ...patch } : prev);
  }, [queryClient]);

  // Save deduction to backend (debounced)
  const saveDeduction = useCallback((branchId, val) => {
    const num = Math.max(0, Math.min(100, parseFloat(val) || 0));

    // Clear previous timer
    if (saveTimers.current[branchId]) {
      clearTimeout(saveTimers.current[branchId]);
    }

    // Debounce 800ms
    saveTimers.current[branchId] = setTimeout(() => {
      setSaving(prev => ({ ...prev, [branchId]: true }));
      axios.put("/api/kpi/deduction", {
        branch_id: branchId,
        year: YEAR,
        month: MONTH,
        deduction_pct: num,
      })
        .then(() => {
          patchCaches(branchId, { deduction_pct: num });
          setSaving(prev => ({ ...prev, [branchId]: false }));
        })
        .catch(() => {
          setSaving(prev => ({ ...prev, [branchId]: false }));
        });
    }, 800);
  }, [patchCaches]);

  const setDeduction = (branchId, val) => {
    const num = Math.max(0, Math.min(100, parseFloat(val) || 0));
    setDeductions(prev => ({ ...prev, [branchId]: num }));
    saveDeduction(branchId, num);
  };

  // Save other revenue to backend (debounced)
  const saveOtherRev = useCallback((branchId, val) => {
    const num = Math.max(0, parseFloat(val) || 0);
    if (otherTimers.current[branchId]) clearTimeout(otherTimers.current[branchId]);
    otherTimers.current[branchId] = setTimeout(() => {
      setSavingOther(prev => ({ ...prev, [branchId]: true }));
      axios.put("/api/kpi/other-revenue", {
        branch_id: branchId,
        year: YEAR,
        month: MONTH,
        other_revenue_native: num,
      })
        .then(() => {
          patchCaches(branchId, { other_revenue_native: num });
          setSavingOther(prev => ({ ...prev, [branchId]: false }));
        })
        .catch(() => setSavingOther(prev => ({ ...prev, [branchId]: false })));
    }, 800);
  }, [patchCaches]);

  const setOtherRev = (branchId, val) => {
    const num = Math.max(0, parseFloat(val) || 0);
    setOtherRevs(prev => ({ ...prev, [branchId]: num }));
    saveOtherRev(branchId, num);
  };

  // Adjusted forecasts (= forecast × (1 − ded%) + other revenue)
  const rows = useMemo(() => {
    return data.map(row => {
      const dedPct = deductions[row.branch_id] ?? row.deduction_pct ?? 0;
      const otherRev = otherRevs[row.branch_id] ?? row.other_revenue_native ?? 0;
      const multiplier = 1 - dedPct / 100;
      return {
        ...row,
        deduction_pct_local: dedPct,
        other_revenue_local: otherRev,
        adjusted_forecast: row.occ_forecast_native != null
          ? row.occ_forecast_native * multiplier + otherRev
          : null,
        adjusted_next_forecast: row.next_month_forecast_native != null
          ? row.next_month_forecast_native * multiplier + otherRev
          : null,
      };
    });
  }, [data, deductions, otherRevs]);

  return { rows, setDeduction, setOtherRev, saving, savingOther };
}

// The two auto-saving number inputs, shared by the table row and the branch
// detail panel. `wide` is the Other Rev variant (money, right-aligned).
function AdjustmentInput({ value, onChange, isSaving, wide = false, max }) {
  return (
    <div className="relative inline-block">
      <input
        type="number"
        min="0"
        max={max}
        step="1"
        value={value || ""}
        onChange={e => onChange(e.target.value)}
        placeholder="0"
        className={
          (wide ? "w-24 text-right font-mono " : "w-14 text-center ") +
          "px-1.5 py-1 text-xs border rounded outline-none " +
          (isSaving
            ? "border-yellow-400 bg-yellow-50"
            : "border-gray-300 focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400")
        }
      />
      {isSaving && (
        <span className="absolute -top-1 -right-1 w-2 h-2 bg-yellow-400 rounded-full animate-pulse" />
      )}
    </div>
  );
}

function AllBranchesTable({ data, loading }) {
  const { rows, setDeduction, setOtherRev, saving, savingOther } = useBranchAdjustments(data);

  // Cross-branch average KPI achievement — branches use different currencies,
  // so we average the per-branch percentages rather than summing amounts.
  const avgPct = useMemo(() => {
    const collect = (toPct) => {
      const vals = rows.map(toPct).filter(v => v != null && isFinite(v));
      if (!vals.length) return null;
      return vals.reduce((a, b) => a + b, 0) / vals.length;
    };
    return {
      adjusted: collect(r => r.target_revenue_native && r.adjusted_forecast != null
        ? r.adjusted_forecast / r.target_revenue_native * 100 : null),
      next: collect(r => r.next_month_target_native && r.adjusted_next_forecast != null
        ? r.adjusted_next_forecast / r.next_month_target_native * 100 : null),
    };
  }, [rows]);

  if (loading && !data.length) return <SectionLoading />;
  if (!data.length) return <div className="bg-white rounded-xl border p-8 text-center text-gray-400">No data — add branches and set KPI targets.</div>;
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-4 border-b border-gray-100">
        <h2 className="font-semibold text-gray-800">Group Summary — {MONTH_NAME}</h2>
        <p className="text-xs text-gray-400 mt-0.5">
          Native currency per branch
          <SyncBadge timestamp={data[0]?.data_synced_at} />
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide text-left">
              <th className="px-5 py-3">Branch</th>
              <th className="px-3 py-3">Cur</th>
              <th className="px-3 py-3 text-right">Revenue</th>
              <th className="px-3 py-3 text-right">Target</th>
              <th className="px-3 py-3 text-center">KPI %</th>
              <th className="px-3 py-3 text-center">Forecast</th>
              <th className="px-3 py-3 text-center whitespace-nowrap">Deduct %</th>
              <th className="px-3 py-3 text-center whitespace-nowrap">Other Rev</th>
              <th className="px-3 py-3 text-center">Adjusted</th>
              <th className="px-3 py-3 text-center whitespace-nowrap">Next Rev</th>
              <th className="px-3 py-3 text-center whitespace-nowrap">Next Forecast</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((row) => {
              const cur = row.currency || "VND";
              const dedPct = row.deduction_pct_local;
              const otherRev = row.other_revenue_local;
              const isSaving = saving[row.branch_id];
              const isSavingOther = savingOther[row.branch_id];
              return (
                <tr key={row.branch_id} className="hover:bg-gray-50">
                  <td className="px-5 py-3.5 font-medium text-gray-800">{row.branch_name}</td>
                  <td className="px-3 py-3.5 text-gray-500 text-xs">{cur}</td>
                  <td className="px-3 py-3.5 text-right font-mono">
                    <div>{fmt(row.actual_revenue_native, cur)}</div>
                    <OccSplit
                      room={row.actual_room_occ_pct}
                      dorm={row.actual_dorm_occ_pct}
                      fallback={row.actual_occ_pct}
                      hasDorm={row.total_dorm_count > 0}
                      className="font-sans"
                    />
                  </td>
                  <td className="px-3 py-3.5 text-right font-mono text-gray-500">{fmt(row.target_revenue_native, cur)}</td>
                  <td className="px-3 py-3.5 text-center"><AchievementBadge value={row.achievement_pct != null ? row.achievement_pct * 100 : null} /></td>
                  {/* Forecast this month */}
                  <td className="px-3 py-3.5 text-center">
                    {row.occ_forecast_native != null
                      ? <div>
                          <HoverTooltip content={forecastBreakdown(row, "current")}>
                            <span className="text-indigo-700 font-medium border-b border-dotted border-indigo-300">
                              {fmt(row.occ_forecast_native, cur)}
                              {row.target_revenue_native
                                ? <span className="ml-1 text-xs text-gray-400 font-normal">
                                    ({Math.round(row.occ_forecast_native / row.target_revenue_native * 100)}%)
                                  </span>
                                : null}
                            </span>
                          </HoverTooltip>
                          <OccSplit
                            room={row.predicted_room_occ_pct}
                            dorm={row.predicted_dorm_occ_pct}
                            fallback={row.predicted_occ_pct}
                            hasDorm={row.total_dorm_count > 0}
                          />
                        </div>
                      : <span className="text-gray-300 text-xs">Enter OCC%</span>}
                  </td>
                  {/* Deduction % input — auto-saves */}
                  <td className="px-3 py-3.5 text-center">
                    <AdjustmentInput
                      value={dedPct}
                      max="100"
                      onChange={v => setDeduction(row.branch_id, v)}
                      isSaving={isSaving}
                    />
                  </td>
                  {/* Other Revenue input — auto-saves */}
                  <td className="px-3 py-3.5 text-center">
                    <AdjustmentInput
                      wide
                      value={otherRev}
                      onChange={v => setOtherRev(row.branch_id, v)}
                      isSaving={isSavingOther}
                    />
                  </td>
                  {/* Adjusted forecast — blue if ≥100% of target, red if below */}
                  <td className="px-3 py-3.5 text-center">
                    {row.adjusted_forecast != null
                      ? (() => {
                          const adjPct = row.target_revenue_native
                            ? row.adjusted_forecast / row.target_revenue_native * 100
                            : null;
                          const adjColor = adjPct == null
                            ? "text-indigo-700"
                            : adjPct >= 100 ? "text-blue-600" : "text-red-600";
                          return (
                            <HoverTooltip content={adjustedBreakdown(row, "current")}>
                              <span className={adjColor + " font-medium border-b border-dotted border-gray-300"}>
                                {fmt(row.adjusted_forecast, cur)}
                                {adjPct != null
                                  ? <span className="ml-1 text-xs text-gray-400 font-normal">
                                      ({adjPct.toFixed(2)}%)
                                    </span>
                                  : null}
                              </span>
                            </HoverTooltip>
                          );
                        })()
                      : <span className="text-gray-300">{"—"}</span>}
                  </td>
                  {/* Next month actual booked revenue */}
                  <td className="px-3 py-3.5 text-center">
                    {row.next_month_booked_revenue != null && row.next_month_booked_revenue > 0
                      ? <div>
                          <span className="text-gray-700 font-mono">
                            {fmt(row.next_month_booked_revenue, cur)}
                            {row.next_month_target_native
                              ? <span className="ml-1 text-xs text-gray-400 font-normal">
                                  ({Math.round(row.next_month_booked_revenue / row.next_month_target_native * 100)}%)
                                </span>
                              : null}
                          </span>
                          <OccSplit
                            room={row.booked_room_occ_next}
                            dorm={row.booked_dorm_occ_next}
                            hasDorm={row.total_dorm_count > 0}
                            className="font-sans"
                          />
                        </div>
                      : <span className="text-gray-300">{"—"}</span>}
                  </td>
                  {/* Next month forecast — blue if ≥100% of target, red if below */}
                  <td className="px-3 py-3.5 text-center">
                    {row.adjusted_next_forecast != null
                      ? (() => {
                          const nextPct = row.next_month_target_native
                            ? row.adjusted_next_forecast / row.next_month_target_native * 100
                            : null;
                          const nextColor = nextPct == null
                            ? "text-purple-700"
                            : nextPct >= 100 ? "text-blue-600" : "text-red-600";
                          return (
                      <div>
                          <HoverTooltip content={
                            <>
                              {forecastBreakdown(row, "next")}
                              {(dedPct > 0 || (row.other_revenue_local || 0) > 0) && (
                                <div className="border-t border-gray-700 mt-1.5 pt-1.5">
                                  {adjustedBreakdown(row, "next")}
                                </div>
                              )}
                            </>
                          }>
                            <span className={nextColor + " font-medium border-b border-dotted border-purple-300"}>
                              {fmt(row.adjusted_next_forecast, cur)}
                              {nextPct != null
                                ? <span className="ml-1 text-xs text-gray-400 font-normal">
                                    ({Math.round(nextPct)}%)
                                  </span>
                                : null}
                            </span>
                          </HoverTooltip>
                          <OccSplit
                            room={row.predicted_room_occ_next}
                            dorm={row.predicted_dorm_occ_next}
                            fallback={row.predicted_occ_next}
                            hasDorm={row.total_dorm_count > 0}
                          />
                        </div>
                          );
                        })()
                      : <span className="text-gray-300">{"—"}</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr className="border-t-2 border-gray-200 bg-gray-50 font-medium">
              <td colSpan={8} className="px-5 py-3 text-right text-xs text-gray-500 uppercase tracking-wide whitespace-nowrap">
                Avg KPI % (5 branches)
              </td>
              {/* Adjusted average */}
              <td className="px-3 py-3 text-center">
                {avgPct.adjusted != null
                  ? <span className={(avgPct.adjusted >= 100 ? "text-blue-600" : "text-red-600") + " font-bold"}>
                      {avgPct.adjusted.toFixed(2)}%
                    </span>
                  : <span className="text-gray-300">{"—"}</span>}
              </td>
              <td className="px-3 py-3" />
              {/* Next Forecast average */}
              <td className="px-3 py-3 text-center">
                {avgPct.next != null
                  ? <span className={(avgPct.next >= 100 ? "text-blue-600" : "text-red-600") + " font-bold"}>
                      {Math.round(avgPct.next)}%
                    </span>
                  : <span className="text-gray-300">{"—"}</span>}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}

// One cell of the branch detail grid. Hairlines come from the parent's
// `gap-px` over a grey background, so a tile just paints itself white.
function DetailTile({ label, hint, children }) {
  return (
    <div className="bg-white px-4 py-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">{label}</p>
      <div className="mt-1">{children}</div>
      {hint && <p className="text-[10px] text-gray-400 mt-1 leading-snug">{hint}</p>}
    </div>
  );
}

// The same numbers the Group Summary carries for this branch — including the
// two editable adjustments. Before this, clicking into a branch showed strictly
// less than the All Branches table did, so anyone wanting Adjusted or next
// month's figures had to go back to the group view.
//
// Laid out as a tile grid rather than as one labelled row per figure: the row
// form put a single number on each line and left the middle of a wide screen
// empty, so nine figures ran taller than the whole heatmap below them.
function BranchDetail({ row, setDeduction, setOtherRev, saving, savingOther }) {
  const cur = row.currency || "VND";
  const hasDorm = row.total_dorm_count > 0;
  const dedPct = row.deduction_pct_local;

  const pctOf = (value, target) =>
    value != null && target ? value / target * 100 : null;

  const adjPct = pctOf(row.adjusted_forecast, row.target_revenue_native);
  const nextPct = pctOf(row.adjusted_next_forecast, row.next_month_target_native);
  const fcPct = pctOf(row.occ_forecast_native, row.target_revenue_native);
  const bookedPct = pctOf(row.next_month_booked_revenue, row.next_month_target_native);

  const overUnder = (pct) => (pct == null ? "text-gray-700" : pct >= 100 ? "text-blue-600" : "text-red-600");
  const suffix = (pct, digits = 0) =>
    pct == null ? null : <span className="ml-1 text-xs text-gray-400 font-normal">({pct.toFixed(digits)}%)</span>;

  const nextMonthName = row.next_year && row.next_month
    ? new Date(row.next_year, row.next_month - 1, 1)
        .toLocaleString("en-US", { month: "long", year: "numeric" })
    : "Next month";

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-100 flex items-baseline justify-between gap-3 flex-wrap">
        <h2 className="font-semibold text-gray-800 text-sm">Details — {MONTH_NAME}</h2>
        <p className="text-xs text-gray-400">
          Native currency ({cur})
          <SyncBadge timestamp={row.data_synced_at} />
        </p>
      </div>

      {/* Column count is set by how wide a tile has to be, not by taste: a VND
          figure with its %-of-target suffix (₫2,357,135,067 (96.80%)) needs
          ~200px, and the sidebar takes a fixed slice off every viewport. Four
          columns only earn their place from xl up. */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-px bg-gray-100">
        <DetailTile label="Revenue">
          <span className="font-mono text-sm text-gray-800">{fmt(row.actual_revenue_native, cur)}</span>
          <OccSplit
            room={row.actual_room_occ_pct}
            dorm={row.actual_dorm_occ_pct}
            fallback={row.actual_occ_pct}
            hasDorm={hasDorm}
          />
        </DetailTile>

        <DetailTile label="Target">
          <span className="font-mono text-sm text-gray-500">{fmt(row.target_revenue_native, cur)}</span>
        </DetailTile>

        <DetailTile label="KPI %" hint="Revenue ÷ Target">
          <AchievementBadge value={row.achievement_pct != null ? row.achievement_pct * 100 : null} />
        </DetailTile>

        <DetailTile label="Forecast">
          {row.occ_forecast_native != null
            ? <>
                <HoverTooltip content={forecastBreakdown(row, "current")}>
                  <span className="font-mono text-sm text-indigo-700 font-medium border-b border-dotted border-indigo-300">
                    {fmt(row.occ_forecast_native, cur)}{suffix(fcPct)}
                  </span>
                </HoverTooltip>
                <OccSplit
                  room={row.predicted_room_occ_pct}
                  dorm={row.predicted_dorm_occ_pct}
                  fallback={row.predicted_occ_pct}
                  hasDorm={hasDorm}
                />
              </>
            : <span className="text-gray-300 text-xs">Enter OCC%</span>}
        </DetailTile>

        {/* Both adjustments share a tile — they are one decision, and the
            Adjusted figure beside them is what they add up to. */}
        <DetailTile label="Adjustments" hint="Deduct % comes off the forecast, Other Rev goes on top">
          <div className="flex flex-wrap items-end gap-x-3 gap-y-2">
            <label className="block">
              <span className="block text-[10px] text-gray-400 mb-0.5">Deduct %</span>
              <AdjustmentInput
                value={dedPct}
                max="100"
                onChange={v => setDeduction(row.branch_id, v)}
                isSaving={saving[row.branch_id]}
              />
            </label>
            <label className="block">
              <span className="block text-[10px] text-gray-400 mb-0.5">Other Rev</span>
              <AdjustmentInput
                wide
                value={row.other_revenue_local}
                onChange={v => setOtherRev(row.branch_id, v)}
                isSaving={savingOther[row.branch_id]}
              />
            </label>
          </div>
        </DetailTile>

        <DetailTile label="Adjusted" hint="Forecast × (1 − Deduct%) + Other Rev">
          {row.adjusted_forecast != null
            ? <HoverTooltip content={adjustedBreakdown(row, "current")}>
                <span className={"font-mono text-sm font-medium border-b border-dotted border-gray-300 " + overUnder(adjPct)}>
                  {fmt(row.adjusted_forecast, cur)}{suffix(adjPct, 2)}
                </span>
              </HoverTooltip>
            : <span className="text-gray-300">{"—"}</span>}
        </DetailTile>

        <DetailTile label={`Next Rev · ${nextMonthName}`} hint="Already on the books">
          {row.next_month_booked_revenue > 0
            ? <>
                <span className="font-mono text-sm text-gray-700">
                  {fmt(row.next_month_booked_revenue, cur)}{suffix(bookedPct)}
                </span>
                <OccSplit
                  room={row.booked_room_occ_next}
                  dorm={row.booked_dorm_occ_next}
                  hasDorm={hasDorm}
                />
              </>
            : <span className="text-gray-300">{"—"}</span>}
        </DetailTile>

        <DetailTile label="Next Forecast" hint="Next month, adjusted the same way">
          {row.adjusted_next_forecast != null
            ? <>
                <HoverTooltip content={
                  <>
                    {forecastBreakdown(row, "next")}
                    {(dedPct > 0 || (row.other_revenue_local || 0) > 0) && (
                      <div className="border-t border-gray-700 mt-1.5 pt-1.5">
                        {adjustedBreakdown(row, "next")}
                      </div>
                    )}
                  </>
                }>
                  <span className={"font-mono text-sm font-medium border-b border-dotted border-purple-300 " + overUnder(nextPct)}>
                    {fmt(row.adjusted_next_forecast, cur)}{suffix(nextPct)}
                  </span>
                </HoverTooltip>
                <OccSplit
                  room={row.predicted_room_occ_next}
                  dorm={row.predicted_dorm_occ_next}
                  fallback={row.predicted_occ_next}
                  hasDorm={hasDorm}
                />
              </>
            : <span className="text-gray-300">{"—"}</span>}
        </DetailTile>
      </div>
    </div>
  );
}

function SingleBranchView({ branch }) {
  const queryClient = useQueryClient();

  // The All Branches table has already fetched every branch's summary, and the
  // backend builds each of its rows with the very same `_branch_summary()`
  // that `/kpi/summary/{id}` returns. So switching from All Branches to a
  // branch tab can paint immediately from what is already in the cache instead
  // of waiting on a request that would answer with identical numbers.
  // `initialDataUpdatedAt` carries the original fetch time along, so React
  // Query still refreshes in the background once that data goes stale rather
  // than treating the seeded copy as fresh forever.
  const allState = queryClient.getQueryState(ALL_BRANCHES_KEY);
  const seededKpi = allState?.data?.find(r => r.branch_id === branch?.id);

  const {
    data: kpi, isPending: kpiPending, isPlaceholderData: kpiStale, error,
  } = useQuery({
    queryKey: branchKpiKey(branch?.id),
    queryFn: () =>
      axios.get("/api/kpi/summary/" + branch.id + "?year=" + YEAR + "&month=" + MONTH)
        .then(r => r.data.data || r.data),
    enabled: !!branch,
    initialData: seededKpi,
    initialDataUpdatedAt: seededKpi ? allState.dataUpdatedAt : undefined,
    placeholderData: keepPreviousData,
  });

  // Same adjustment machinery the group table uses, over a one-row list —
  // that keeps `adjusted_forecast` computed in exactly one place, so the two
  // views can never drift on what "Adjusted" means.
  const kpiRows = useMemo(() => (kpi ? [kpi] : []), [kpi]);
  const { rows, setDeduction, setOtherRev, saving, savingOther } = useBranchAdjustments(kpiRows);
  const row = rows[0];

  // The heatmap loads on its own query rather than sharing one Promise.all
  // with the KPI summary: the two have very different response times, and
  // bundling them meant the card sat blank until the slower one finished.
  const {
    data: occData = [], isPending: occPending, isPlaceholderData: occStale,
  } = useQuery({
    queryKey: ["home-branch-occ", branch?.id],
    queryFn: () =>
      axios.get("/api/metrics/daily?branch_id=" + branch.id + "&days=30")
        .then(r => r.data.data || []),
    enabled: !!branch,
    placeholderData: keepPreviousData,
  });

  if (error) return <div className="p-8 text-red-500">Error: {error.message}</div>;

  return (
    <div className="space-y-6">
      <div className={`space-y-6 transition-opacity duration-150 ${kpiStale ? "opacity-40 pointer-events-none" : "opacity-100"}`}>
        {kpiPending && !row
          ? <SectionLoading />
          : row && (
              <>
                <KPICard
                  label={branch.name + " — Revenue"}
                  actual={row.actual_revenue_native}
                  target={row.target_revenue_native}
                  currency={branch.currency || branch.native_currency}
                  forecast={{ occ: row.occ_forecast_native, occLabel: occForecastLabel(row) }}
                />
                <BranchDetail
                  row={row}
                  setDeduction={setDeduction}
                  setOtherRev={setOtherRev}
                  saving={saving}
                  savingOther={savingOther}
                />
              </>
            )}
      </div>
      <div className={`transition-opacity duration-150 ${occStale ? "opacity-40 pointer-events-none" : "opacity-100"}`}>
        {occPending && !occData.length
          ? <SectionLoading />
          : <OCCHeatmap data={occData} title={branch.name + " — Daily OCC% (30 days)"} />}
      </div>
    </div>
  );
}

function SectionLoading() {
  return (
    <div className="bg-white rounded-xl border p-8 text-center">
      <div className="text-gray-400 animate-pulse text-lg">Loading…</div>
      <p className="text-xs text-gray-300 mt-2">Loading data…</p>
    </div>
  );
}

export default function Home() {
  const { isAll, currentBranch } = useBranch();

  const { data: allData = [], isPending: allLoading } = useQuery({
    queryKey: ALL_BRANCHES_KEY,
    queryFn: () =>
      axios.get("/api/kpi/summary?year=" + YEAR + "&month=" + MONTH + "&months=current,next")
        .then(r => r.data.data || []),
    enabled: isAll,
    placeholderData: keepPreviousData,
  });

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-bold text-gray-800">
          {isAll ? "All Branches" : (currentBranch ? currentBranch.name : "Dashboard")}
        </h1>
        <p className="text-xs text-gray-400 mt-0.5">{MONTH_NAME}</p>
      </div>
      {isAll
        ? <AllBranchesTable data={allData} loading={allLoading} />
        : currentBranch
          ? <SingleBranchView branch={currentBranch} />
          : <div className="bg-white rounded-xl border p-8 text-center text-gray-400">Select a branch above.</div>
      }
    </div>
  );
}
