import { useState, useEffect, useCallback } from "react";
import { useBranch } from "../context/BranchContext";
import { useAuth } from "../context/AuthContext";
import {
  getAlertsToday,
  getAlertsSummary,
  getAlertsHistory,
  acknowledgeAlert,
  resolveAlert,
  getAlertRules,
  updateAlertRule,
  evaluateNow,
} from "../api/alerts";

/* ── Severity styling ──────────────────────────────────────────────────── */
const SEV = {
  CRITICAL: { bg: "bg-red-50 border-red-200", badge: "bg-red-600 text-white", text: "text-red-700", card: "border-l-red-600" },
  WARNING:  { bg: "bg-amber-50 border-amber-200", badge: "bg-amber-500 text-white", text: "text-amber-700", card: "border-l-amber-500" },
  INFO:     { bg: "bg-blue-50 border-blue-200", badge: "bg-blue-500 text-white", text: "text-blue-700", card: "border-l-blue-500" },
};

const CATEGORY_LABELS = {
  revenue: "Revenue & Pricing",
  occupancy: "Occupancy",
  bookings: "Bookings & Cancellations",
  channel: "Channel Mix",
  market: "Guest Markets",
};

/* ── Summary Card ──────────────────────────────────────────────────────── */
function SummaryCard({ label, count, color, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`flex-1 rounded-lg border-2 p-4 text-center transition-all ${
        active ? `${color} ring-2 ring-offset-1` : "border-gray-200 bg-white hover:border-gray-300"
      }`}
    >
      <div className={`text-3xl font-bold ${active ? "" : "text-gray-800"}`}>{count}</div>
      <div className="text-xs font-semibold uppercase tracking-wider mt-1 opacity-80">{label}</div>
    </button>
  );
}

/* ── Alert Card ────────────────────────────────────────────────────────── */
function AlertCard({ alert, onAcknowledge, onResolve }) {
  const s = SEV[alert.severity] || SEV.INFO;
  return (
    <div className={`border-l-4 ${s.card} bg-white rounded-lg shadow-sm p-4 mb-3`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-1">
            <span className={`px-2 py-0.5 rounded text-xs font-bold ${s.badge}`}>{alert.severity}</span>
            <span className="text-xs text-gray-500 font-medium">{alert.branch_name}</span>
            <span className="text-xs text-gray-400">|</span>
            <span className="text-xs text-gray-500">{CATEGORY_LABELS[alert.category] || alert.category}</span>
          </div>
          <p className="text-sm text-gray-800 font-medium mb-1">{alert.message}</p>
          <p className="text-xs text-gray-600 leading-relaxed">{alert.recommendation}</p>
          {alert.current_value != null && alert.threshold_value != null && (
            <div className="flex gap-4 mt-2 text-xs text-gray-500">
              <span>Current: <strong className="text-gray-700">{Number(alert.current_value).toLocaleString(undefined, { maximumFractionDigits: 2 })}</strong></span>
              <span>Threshold: <strong className="text-gray-700">{Number(alert.threshold_value).toLocaleString(undefined, { maximumFractionDigits: 2 })}</strong></span>
              {alert.deviation_pct != null && (
                <span>Deviation: <strong className={s.text}>{Number(alert.deviation_pct).toFixed(1)}%</strong></span>
              )}
            </div>
          )}
        </div>
        <div className="flex flex-col gap-1 shrink-0">
          {alert.status === "active" && (
            <>
              <button
                onClick={() => onAcknowledge(alert.id)}
                className="px-3 py-1 text-xs rounded border border-gray-300 hover:bg-gray-100 text-gray-600"
              >
                Acknowledge
              </button>
              <button
                onClick={() => onResolve(alert.id)}
                className="px-3 py-1 text-xs rounded border border-green-300 hover:bg-green-50 text-green-600"
              >
                Resolve
              </button>
            </>
          )}
          {alert.status === "acknowledged" && (
            <button
              onClick={() => onResolve(alert.id)}
              className="px-3 py-1 text-xs rounded border border-green-300 hover:bg-green-50 text-green-600"
            >
              Resolve
            </button>
          )}
          {alert.status !== "active" && (
            <span className={`px-2 py-0.5 rounded text-xs font-medium ${
              alert.status === "acknowledged" ? "bg-yellow-100 text-yellow-700" : "bg-green-100 text-green-700"
            }`}>
              {alert.status}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── Rule Row ──────────────────────────────────────────────────────────── */
function RuleRow({ rule, onUpdate }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(rule.threshold_value);

  const save = async () => {
    await onUpdate(rule.id, { threshold_value: parseFloat(value) });
    setEditing(false);
  };

  return (
    <tr className="border-b border-gray-100 hover:bg-gray-50">
      <td className="px-3 py-2 text-sm">{rule.display_name}</td>
      <td className="px-3 py-2 text-xs text-gray-500">{rule.category}</td>
      <td className="px-3 py-2">
        <span className={`px-2 py-0.5 rounded text-xs font-bold ${(SEV[rule.severity] || SEV.INFO).badge}`}>
          {rule.severity}
        </span>
      </td>
      <td className="px-3 py-2 text-sm">
        {editing ? (
          <div className="flex items-center gap-1">
            <input
              type="number"
              step="0.01"
              value={value}
              onChange={e => setValue(e.target.value)}
              className="w-20 px-2 py-1 border rounded text-sm"
            />
            <button onClick={save} className="text-xs text-green-600 font-medium">Save</button>
            <button onClick={() => setEditing(false)} className="text-xs text-gray-400">Cancel</button>
          </div>
        ) : (
          <span
            className="cursor-pointer hover:text-indigo-600"
            onClick={() => setEditing(true)}
          >
            {rule.threshold_value}
          </span>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-gray-500">{rule.lookback_days}d</td>
      <td className="px-3 py-2">
        <button
          onClick={() => onUpdate(rule.id, { is_active: !rule.is_active })}
          className={`px-2 py-0.5 rounded text-xs font-medium ${
            rule.is_active ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"
          }`}
        >
          {rule.is_active ? "Active" : "Disabled"}
        </button>
      </td>
    </tr>
  );
}

/* ── Main Alerts Page ──────────────────────────────────────────────────── */
export default function Alerts() {
  const { selected: selectedBranch } = useBranch();
  const { isAdmin } = useAuth();
  const [tab, setTab] = useState("today");       // today | history | rules
  const [alerts, setAlerts] = useState([]);
  const [summary, setSummary] = useState({ critical: 0, warning: 0, info: 0, total: 0 });
  const [sevFilter, setSevFilter] = useState(null);
  const [loading, setLoading] = useState(true);
  const [evaluating, setEvaluating] = useState(false);

  // History state
  const [history, setHistory] = useState({ items: [], total: 0 });
  const [historyPage, setHistoryPage] = useState(0);

  // Rules state
  const [rules, setRules] = useState([]);

  const branchParam = selectedBranch === "all" ? {} : { branch_id: selectedBranch };

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [todayData, summaryData] = await Promise.all([
        getAlertsToday({ ...branchParam, ...(sevFilter ? { severity: sevFilter } : {}) }),
        getAlertsSummary(branchParam),
      ]);
      setAlerts(todayData || []);
      setSummary(summaryData || { critical: 0, warning: 0, info: 0, total: 0 });
    } catch (e) {
      console.error("Failed to load alerts:", e);
    } finally {
      setLoading(false);
    }
  }, [selectedBranch, sevFilter]);

  useEffect(() => { load(); }, [load]);

  const loadHistory = useCallback(async () => {
    try {
      const data = await getAlertsHistory({ ...branchParam, limit: 50, offset: historyPage * 50 });
      setHistory(data || { items: [], total: 0 });
    } catch (e) {
      console.error("Failed to load history:", e);
    }
  }, [selectedBranch, historyPage]);

  useEffect(() => { if (tab === "history") loadHistory(); }, [tab, loadHistory]);

  const loadRules = useCallback(async () => {
    try {
      const data = await getAlertRules();
      setRules(data || []);
    } catch (e) {
      console.error("Failed to load rules:", e);
    }
  }, []);

  useEffect(() => { if (tab === "rules") loadRules(); }, [tab, loadRules]);

  const handleAcknowledge = async (id) => {
    await acknowledgeAlert(id);
    load();
  };

  const handleResolve = async (id) => {
    await resolveAlert(id);
    load();
  };

  const handleRuleUpdate = async (ruleId, data) => {
    await updateAlertRule(ruleId, data);
    loadRules();
  };

  const handleEvaluateNow = async () => {
    setEvaluating(true);
    try {
      await evaluateNow();
      await load();
    } catch (e) {
      console.error("Evaluation failed:", e);
    } finally {
      setEvaluating(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Performance Alerts</h1>
          <p className="text-sm text-gray-500 mt-0.5">Daily automated insights to optimize revenue</p>
        </div>
        {isAdmin && (
          <button
            onClick={handleEvaluateNow}
            disabled={evaluating}
            className="px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50"
          >
            {evaluating ? "Evaluating..." : "Evaluate Now"}
          </button>
        )}
      </div>

      {/* Summary Cards */}
      <div className="flex gap-3">
        <SummaryCard
          label="Critical" count={summary.critical}
          color="border-red-400 bg-red-50 ring-red-400"
          active={sevFilter === "CRITICAL"}
          onClick={() => setSevFilter(sevFilter === "CRITICAL" ? null : "CRITICAL")}
        />
        <SummaryCard
          label="Warning" count={summary.warning}
          color="border-amber-400 bg-amber-50 ring-amber-400"
          active={sevFilter === "WARNING"}
          onClick={() => setSevFilter(sevFilter === "WARNING" ? null : "WARNING")}
        />
        <SummaryCard
          label="Info" count={summary.info}
          color="border-blue-400 bg-blue-50 ring-blue-400"
          active={sevFilter === "INFO"}
          onClick={() => setSevFilter(sevFilter === "INFO" ? null : "INFO")}
        />
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-gray-200">
        {[
          { key: "today", label: "Today's Alerts" },
          { key: "history", label: "History" },
          ...(isAdmin ? [{ key: "rules", label: "Alert Rules" }] : []),
        ].map(t => (
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

      {/* Tab Content */}
      {tab === "today" && (
        <div>
          {loading ? (
            <div className="text-center py-12 text-gray-400 text-sm animate-pulse">Loading alerts...</div>
          ) : alerts.length === 0 ? (
            <div className="text-center py-12">
              <div className="text-4xl mb-2">&#10003;</div>
              <p className="text-gray-500 text-sm">No alerts — all metrics within normal range</p>
            </div>
          ) : (
            <div>
              {alerts.map(a => (
                <AlertCard
                  key={a.id}
                  alert={a}
                  onAcknowledge={handleAcknowledge}
                  onResolve={handleResolve}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {tab === "history" && (
        <div>
          {history.items.length === 0 ? (
            <p className="text-center py-8 text-gray-400 text-sm">No historical alerts</p>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 border-b">
                      <th className="px-3 py-2 text-left text-xs font-semibold text-gray-500">Date</th>
                      <th className="px-3 py-2 text-left text-xs font-semibold text-gray-500">Branch</th>
                      <th className="px-3 py-2 text-center text-xs font-semibold text-gray-500">Severity</th>
                      <th className="px-3 py-2 text-left text-xs font-semibold text-gray-500">Alert</th>
                      <th className="px-3 py-2 text-center text-xs font-semibold text-gray-500">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.items.map(a => {
                      const s = SEV[a.severity] || SEV.INFO;
                      return (
                        <tr key={a.id} className="border-b border-gray-100 hover:bg-gray-50">
                          <td className="px-3 py-2 text-xs text-gray-500">{a.alert_date}</td>
                          <td className="px-3 py-2 text-sm">{a.branch_name}</td>
                          <td className="px-3 py-2 text-center">
                            <span className={`px-2 py-0.5 rounded text-xs font-bold ${s.badge}`}>{a.severity}</span>
                          </td>
                          <td className="px-3 py-2 text-sm text-gray-700">{a.message}</td>
                          <td className="px-3 py-2 text-center">
                            <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                              a.status === "active" ? "bg-red-100 text-red-700"
                              : a.status === "acknowledged" ? "bg-yellow-100 text-yellow-700"
                              : "bg-green-100 text-green-700"
                            }`}>
                              {a.status}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="flex items-center justify-between mt-4">
                <span className="text-xs text-gray-500">{history.total} total alerts</span>
                <div className="flex gap-2">
                  <button
                    disabled={historyPage === 0}
                    onClick={() => setHistoryPage(p => p - 1)}
                    className="px-3 py-1 text-xs border rounded disabled:opacity-30"
                  >
                    Previous
                  </button>
                  <button
                    disabled={(historyPage + 1) * 50 >= history.total}
                    onClick={() => setHistoryPage(p => p + 1)}
                    className="px-3 py-1 text-xs border rounded disabled:opacity-30"
                  >
                    Next
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      )}

      {tab === "rules" && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b">
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-500">Rule</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-500">Category</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-500">Severity</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-500">Threshold</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-500">Lookback</th>
                <th className="px-3 py-2 text-left text-xs font-semibold text-gray-500">Status</th>
              </tr>
            </thead>
            <tbody>
              {rules.map(r => (
                <RuleRow key={r.id} rule={r} onUpdate={handleRuleUpdate} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
