/**
 * Weekly Report — two tabs:
 *
 *   1. Settings   — recipients / schedule / cron config (the previous
 *                   page content). Fast: no report generation involved.
 *
 *   2. Weekly Report — interactive viewer. Fetches /api/report/preview
 *                      (the slow full HTML the email used to ship), parses
 *                      out per-branch sections via the `hid-branch-card` /
 *                      `hid-ad-optimizer` / `#exec-summary` anchors the
 *                      backend now adds, and lets operators jump between
 *                      branches via tabs without re-fetching.
 *
 * Email sends only the Executive Summary + a CTA back to this page for
 * the per-branch drill-down. Deep-link via /report?view=full[&branch=ID]
 * — used by the email's "Branch quick-jump" chips.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import axios from "axios";
import { useAuth } from "../context/AuthContext";

const DAYS = [
  { value: "mon", label: "Mon" },
  { value: "tue", label: "Tue" },
  { value: "wed", label: "Wed" },
  { value: "thu", label: "Thu" },
  { value: "fri", label: "Fri" },
  { value: "sat", label: "Sat" },
  { value: "sun", label: "Sun" },
];

const HOURS = Array.from({ length: 24 }, (_, i) => i);

// ── Metric label map ────────────────────────────────────────────────────────
// Used as drawer header when a user clicks a cell — the data-metric-key
// attribute is a stable id, the labels here are the human display strings.
// `_general` is a synthetic metric_key used for page-level notes — not
// tied to any specific data cell. Each page (All Branches summary +
// each branch tab) has its own thread, scoped by branch_id.
const GENERAL_KEY = "_general";

const METRIC_LABELS = {
  [GENERAL_KEY]: "General notes",
  revenue_mtd: "Revenue MTD",
  target: "Revenue Target",
  pacing: "Pacing %",
  forecast: "Forecast (Adjusted)",
  occ: "Occupancy %",
  adr: "ADR",
  revpar: "RevPAR",
  wow_revenue: "Week-over-week Revenue",
  yoy_revenue: "Year-over-year Revenue",
  "branch.revenue": "Branch Revenue (MTD)",
  "branch.target": "Branch Target",
  "branch.adr": "Branch ADR",
  "branch.occ_actual": "Branch Actual OCC%",
  "branch.occ_forecast": "Branch Forecast OCC%",
  "branch.forecast": "Branch Forecast (Adjusted)",
  "branch.next_revenue": "Next Month Revenue (booked)",
  "branch.next_target": "Next Month Target",
  "branch.next_adr": "Next Month ADR",
  "branch.next_occ_forecast": "Next Month Forecast OCC%",
  "branch.next_forecast": "Next Month Forecast (Adjusted)",
};

// Prefix → label-template for dynamic metric_keys emitted by the backend
// (per-source / per-country / per-bucket rows). Static keys in
// METRIC_LABELS still win; this only kicks in when there's no exact
// match. Lets the All-Comments panel render readable thread titles even
// for threads whose `data-metric-label` attribute is no longer in the DOM.
const DYNAMIC_PREFIXES = [
  ["channel.source.", "Top source — "],
  ["channel.category.", "Channel category — "],
  ["channel.direct_trend.", "Direct trend — "],
  ["behavior.cancellation.", "Cancellation — "],
  ["behavior.lead_time.", "Lead time — "],
  ["behavior.los.", "LOS — "],
  ["country.book.", "Country (booked) — "],
  ["country.stay.", "Country (check-in) — "],
  ["outlier.", "Outlier — "],
];

const metricLabel = (key, fallback) => {
  if (METRIC_LABELS[key]) return METRIC_LABELS[key];
  if (fallback) return fallback;
  for (const [prefix, label] of DYNAMIC_PREFIXES) {
    if (key.startsWith(prefix)) return label + key.slice(prefix.length);
  }
  return key;
};

// ── Week helpers ────────────────────────────────────────────────────────────
function thisMonday() {
  const d = new Date();
  const day = d.getDay(); // 0 = Sun, 1 = Mon ...
  const offset = (day + 6) % 7; // days since Monday
  d.setDate(d.getDate() - offset);
  d.setHours(0, 0, 0, 0);
  return d;
}

function fmtIsoDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function fmtWeekLabel(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  const start = new Date(y, m - 1, d);
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  const opts = { month: "short", day: "numeric" };
  return `${start.toLocaleDateString(undefined, opts)} – ${end.toLocaleDateString(undefined, opts)}, ${start.getFullYear()}`;
}

function Toast({ message, type, onClose }) {
  useEffect(() => {
    const t = setTimeout(onClose, 4000);
    return () => clearTimeout(t);
  }, [onClose]);

  const bg = type === "success" ? "bg-green-600" : type === "error" ? "bg-red-600" : "bg-indigo-600";

  return (
    <div className={`fixed bottom-6 right-6 ${bg} text-white px-5 py-3 rounded-lg shadow-lg text-sm z-50 flex items-center gap-3`}>
      <span>{message}</span>
      <button onClick={onClose} className="text-white/70 hover:text-white">&times;</button>
    </div>
  );
}

// ── URL helpers ─────────────────────────────────────────────────────────────

function readUrlState() {
  const p = new URLSearchParams(window.location.search);
  return {
    tab: p.get("tab") === "settings" ? "settings" : "report",
    branch: p.get("branch") || "all",
  };
}

function writeUrlState(tab, branch) {
  const p = new URLSearchParams(window.location.search);
  if (tab === "settings") {
    p.set("tab", "settings");
    p.delete("view");
    p.delete("branch");
  } else {
    p.delete("tab");
    p.delete("view");
  }
  if (tab === "report" && branch && branch !== "all") {
    p.set("branch", branch);
  } else {
    p.delete("branch");
  }
  const next = `${window.location.pathname}${p.toString() ? "?" + p.toString() : ""}`;
  window.history.replaceState({}, "", next);
}

// ── Parse the email-style HTML returned by /api/report/preview ─────────────

function parseReportHtml(htmlText) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(htmlText, "text/html");

  // Header is the gradient div at the top of the email body
  const headerEl = doc.querySelector("body > div > div");
  const headerHtml = headerEl ? headerEl.outerHTML : "";

  const execEl = doc.querySelector("#exec-summary");
  const execSummaryHtml = execEl ? execEl.outerHTML : "";

  const optimizers = {};
  doc.querySelectorAll(".hid-ad-optimizer").forEach(el => {
    const bid = el.dataset.branchId;
    if (bid) optimizers[bid] = el.outerHTML;
  });

  const branches = Array.from(doc.querySelectorAll(".hid-branch-card")).map(el => ({
    id: el.dataset.branchId,
    name: el.dataset.branchName || el.querySelector("h3")?.textContent || "Branch",
    html: el.outerHTML,
    optimizerHtml: optimizers[el.dataset.branchId] || "",
  }));

  return { headerHtml, execSummaryHtml, branches };
}

// ── Settings tab content ────────────────────────────────────────────────────

function SettingsTab({ toast, setToast }) {
  const [testEmail, setTestEmail] = useState("");
  const [members, setMembers] = useState([]);
  const [selectedMemberIds, setSelectedMemberIds] = useState([]);
  const [membersLoading, setMembersLoading] = useState(false);
  const [sending, setSending] = useState(false);

  const [schedule, setSchedule] = useState(null);
  const [scheduleLoading, setScheduleLoading] = useState(false);
  const [newRecipient, setNewRecipient] = useState("");
  const [savingSchedule, setSavingSchedule] = useState(false);

  const [emailConfig, setEmailConfig] = useState(null);
  const [configLoading, setConfigLoading] = useState(false);

  const loadSchedule = () => {
    setScheduleLoading(true);
    axios.get("/api/report/schedule")
      .then(r => setSchedule(r.data.data))
      .catch(() => {})
      .finally(() => setScheduleLoading(false));
  };
  const loadMembers = () => {
    setMembersLoading(true);
    axios.get("/api/auth/users")
      .then(r => setMembers(r.data.data || []))
      .catch(() => setMembers([]))
      .finally(() => setMembersLoading(false));
  };
  const loadEmailConfig = () => {
    setConfigLoading(true);
    axios.get("/api/report/email-config")
      .then(r => setEmailConfig(r.data.data))
      .catch(() => setEmailConfig(null))
      .finally(() => setConfigLoading(false));
  };

  useEffect(() => {
    loadSchedule();
    loadMembers();
    loadEmailConfig();
  }, []);

  const toggleMember = (id) => setSelectedMemberIds(prev =>
    prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]
  );
  const selectAllMembers = () => setSelectedMemberIds(members.filter(m => m.is_active).map(m => m.id));
  const clearMembers = () => setSelectedMemberIds([]);

  const sendNow = async () => {
    const rawEmails = testEmail.trim();
    if (selectedMemberIds.length === 0 && !rawEmails) {
      setToast({ message: "Select at least one member or enter an email", type: "error" });
      return;
    }
    setSending(true);
    try {
      const params = new URLSearchParams();
      if (selectedMemberIds.length) params.set("user_ids", selectedMemberIds.join(","));
      if (rawEmails) params.set("to", rawEmails);
      const r = await axios.post(`/api/report/send-weekly?${params}`);
      const count = r.data?.data?.sent_to?.length ?? 0;
      setToast({ message: `Email sent to ${count} recipient${count === 1 ? "" : "s"}`, type: "success" });
    } catch (e) {
      setToast({ message: e.response?.data?.detail || "Failed to send email", type: "error" });
    } finally {
      setSending(false);
    }
  };

  const saveSchedule = async () => {
    if (!schedule) return;
    setSavingSchedule(true);
    try {
      const r = await axios.patch("/api/report/schedule", {
        enabled: schedule.enabled,
        day_of_week: schedule.day_of_week,
        hour: schedule.hour,
        minute: schedule.minute,
        recipients: schedule.recipients,
      });
      setSchedule(r.data.data);
      setToast({ message: "Schedule saved successfully", type: "success" });
    } catch (e) {
      setToast({ message: e.response?.data?.detail || "Failed to save schedule", type: "error" });
    } finally {
      setSavingSchedule(false);
    }
  };

  const addRecipient = () => {
    const email = newRecipient.trim();
    if (!email || !email.includes("@")) return;
    if (schedule.recipients.includes(email)) return;
    setSchedule({ ...schedule, recipients: [...schedule.recipients, email] });
    setNewRecipient("");
  };
  const removeRecipient = (email) =>
    setSchedule({ ...schedule, recipients: schedule.recipients.filter(r => r !== email) });

  const recipientsCount = selectedMemberIds.length + (testEmail.trim() ? testEmail.split(",").filter(x => x.trim()).length : 0);

  return (
    <div className="space-y-5">
      {/* Cron config status (read-only — driven by Zeabur env) */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-semibold text-gray-800 text-sm">Automated Cron Config (Zeabur env)</h3>
          <button onClick={loadEmailConfig} className="text-xs text-indigo-600 hover:text-indigo-700 font-medium">
            Refresh
          </button>
        </div>

        {configLoading ? (
          <div className="text-xs text-gray-400 animate-pulse">Loading...</div>
        ) : emailConfig ? (
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3 text-xs">
              <div className="bg-gray-50 rounded-lg p-3">
                <p className="text-gray-500 mb-1">Active provider</p>
                <p className="font-semibold text-gray-800 capitalize">
                  {emailConfig.active_provider === "none" ? (
                    <span className="text-red-600">Not configured</span>
                  ) : (
                    emailConfig.active_provider
                  )}
                </p>
              </div>
              <div className="bg-gray-50 rounded-lg p-3">
                <p className="text-gray-500 mb-1">Sender (EMAIL_FROM)</p>
                <p className="font-semibold text-gray-800 truncate">
                  {emailConfig.email_from || <span className="text-red-600">Not set</span>}
                </p>
              </div>
            </div>

            <div className="bg-gray-50 rounded-lg p-3">
              <p className="text-xs text-gray-500 mb-2">
                Cron recipients (EMAIL_RECIPIENTS) — {emailConfig.recipients_count} address{emailConfig.recipients_count === 1 ? "" : "es"}
              </p>
              {emailConfig.recipients_masked && emailConfig.recipients_masked.length > 0 ? (
                <div className="flex flex-wrap gap-1.5">
                  {emailConfig.recipients_masked.map((r, i) => (
                    <span key={i} className="bg-white border border-gray-200 text-xs px-2 py-1 rounded">
                      {r}
                    </span>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-red-600">No recipients configured</p>
              )}
              <p className="text-[11px] text-gray-400 mt-2">
                These are the recipients used by the GitHub Actions weekly cron (Mon 07:00 ICT).
                Edit by updating the <code>EMAIL_RECIPIENTS</code> env var on Zeabur (comma-separated).
              </p>
            </div>

            {emailConfig.hints && emailConfig.hints.length > 0 && (
              <div className="bg-amber-50 border border-amber-200 rounded-lg p-3">
                <p className="text-xs font-medium text-amber-800 mb-1">Heads-up:</p>
                <ul className="text-xs text-amber-700 list-disc pl-5 space-y-0.5">
                  {emailConfig.hints.map((h, i) => <li key={i}>{h}</li>)}
                </ul>
              </div>
            )}
          </div>
        ) : (
          <div className="text-xs text-red-400">Failed to load config</div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
        {/* Send Now */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <div className="flex items-center justify-between mb-3">
            <div>
              <h3 className="font-semibold text-gray-800 text-sm">Send Now</h3>
              <p className="text-[11px] text-gray-500 mt-0.5">
                Generates and sends the compact weekly email immediately (~20-40s).
              </p>
            </div>
            <div className="flex gap-2 text-xs">
              <button onClick={selectAllMembers} className="text-indigo-600 hover:text-indigo-700 font-medium">
                Select all
              </button>
              <span className="text-gray-300">·</span>
              <button onClick={clearMembers} className="text-gray-500 hover:text-gray-700 font-medium">
                Clear
              </button>
            </div>
          </div>

          <div className="border border-gray-200 rounded-lg max-h-72 overflow-y-auto mb-3">
            {membersLoading ? (
              <div className="p-3 text-xs text-gray-400 animate-pulse">Loading members...</div>
            ) : members.length === 0 ? (
              <div className="p-3 text-xs text-gray-400">No members found</div>
            ) : (
              members.map(m => {
                const checked = selectedMemberIds.includes(m.id);
                const disabled = !m.is_active;
                return (
                  <label
                    key={m.id}
                    className={`flex items-center gap-2 px-3 py-2 border-b border-gray-100 last:border-b-0 cursor-pointer ${disabled ? "opacity-50 cursor-not-allowed" : "hover:bg-gray-50"}`}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={disabled}
                      onChange={() => !disabled && toggleMember(m.id)}
                      className="h-4 w-4 text-indigo-600 rounded focus:ring-indigo-500"
                    />
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-medium text-gray-800 truncate">{m.name || m.email}</p>
                      <p className="text-[11px] text-gray-500 truncate">
                        {m.email} · {m.role}{!m.is_active && " · inactive"}
                      </p>
                    </div>
                  </label>
                );
              })
            )}
          </div>

          <div className="mb-3">
            <p className="text-[11px] text-gray-500 mb-1">Also send to (optional, comma-separated):</p>
            <input
              type="text"
              value={testEmail}
              onChange={e => setTestEmail(e.target.value)}
              placeholder="extra@example.com, other@example.com"
              className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
              onKeyDown={e => e.key === "Enter" && sendNow()}
            />
          </div>

          <button
            onClick={sendNow}
            disabled={sending || (selectedMemberIds.length === 0 && !testEmail.trim())}
            className="w-full px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {sending ? (
              <>
                <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                Generating + sending...
              </>
            ) : (
              <>Send to {recipientsCount} recipient(s)</>
            )}
          </button>
        </div>

        {/* In-process schedule */}
        <div className="bg-white rounded-xl border border-gray-200 p-5">
          <h3 className="font-semibold text-gray-800 text-sm mb-1">In-process Schedule (legacy)</h3>
          <p className="text-[11px] text-gray-500 mb-4">
            Production cron is GitHub Actions at <code>0 0 * * 1</code> UTC (Mon 07:00 ICT) using <code>EMAIL_RECIPIENTS</code> above.
            This panel only matters if you also want the FastAPI process to schedule sends.
          </p>

          {scheduleLoading ? (
            <div className="text-sm text-gray-400 animate-pulse">Loading schedule...</div>
          ) : schedule ? (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-sm text-gray-700">Enable in-process schedule</span>
                <button
                  onClick={() => setSchedule({ ...schedule, enabled: !schedule.enabled })}
                  className={`relative w-11 h-6 rounded-full transition-colors ${schedule.enabled ? "bg-indigo-600" : "bg-gray-300"}`}
                >
                  <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${schedule.enabled ? "translate-x-5" : ""}`} />
                </button>
              </div>

              <div>
                <p className="text-xs text-gray-500 mb-2">Day of week</p>
                <div className="flex flex-wrap gap-1">
                  {DAYS.map(d => (
                    <button
                      key={d.value}
                      onClick={() => setSchedule({ ...schedule, day_of_week: d.value })}
                      className={`px-2.5 py-1.5 text-xs rounded-md font-medium transition ${
                        schedule.day_of_week === d.value
                          ? "bg-indigo-600 text-white"
                          : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                      }`}
                    >
                      {d.label}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <p className="text-xs text-gray-500 mb-2">Time (ICT)</p>
                <div className="flex gap-2">
                  <select
                    value={schedule.hour}
                    onChange={e => setSchedule({ ...schedule, hour: parseInt(e.target.value) })}
                    className="flex-1 px-3 py-2 border border-gray-200 rounded-lg text-sm bg-white"
                  >
                    {HOURS.map(h => <option key={h} value={h}>{String(h).padStart(2, "0")}:00</option>)}
                  </select>
                  <select
                    value={schedule.minute}
                    onChange={e => setSchedule({ ...schedule, minute: parseInt(e.target.value) })}
                    className="w-20 px-3 py-2 border border-gray-200 rounded-lg text-sm bg-white"
                  >
                    {[0, 15, 30, 45].map(m => <option key={m} value={m}>:{String(m).padStart(2, "0")}</option>)}
                  </select>
                </div>
              </div>

              <div>
                <p className="text-xs text-gray-500 mb-2">In-process recipients</p>
                <div className="space-y-1.5 mb-2 max-h-40 overflow-y-auto">
                  {schedule.recipients.map(email => (
                    <div key={email} className="flex items-center justify-between bg-gray-50 rounded-lg px-3 py-1.5">
                      <span className="text-xs text-gray-700 truncate">{email}</span>
                      <button onClick={() => removeRecipient(email)} className="text-gray-400 hover:text-red-500 text-sm ml-2 flex-shrink-0">
                        &times;
                      </button>
                    </div>
                  ))}
                  {schedule.recipients.length === 0 && (
                    <p className="text-xs text-gray-400 italic">No recipients added</p>
                  )}
                </div>
                <div className="flex gap-1">
                  <input
                    type="email"
                    value={newRecipient}
                    onChange={e => setNewRecipient(e.target.value)}
                    placeholder="Add email..."
                    className="flex-1 px-3 py-1.5 border border-gray-200 rounded-lg text-xs focus:outline-none focus:ring-1 focus:ring-indigo-500"
                    onKeyDown={e => e.key === "Enter" && addRecipient()}
                  />
                  <button onClick={addRecipient} className="px-3 py-1.5 bg-gray-100 text-gray-600 text-xs rounded-lg hover:bg-gray-200 font-medium">
                    Add
                  </button>
                </div>
              </div>

              {schedule.next_run && (
                <div className="bg-indigo-50 rounded-lg p-3">
                  <p className="text-xs text-indigo-600 font-medium">Next in-process scheduled send</p>
                  <p className="text-sm text-indigo-800 font-semibold mt-0.5">{new Date(schedule.next_run).toLocaleString()}</p>
                </div>
              )}

              <button
                onClick={saveSchedule}
                disabled={savingSchedule}
                className="w-full px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {savingSchedule ? (
                  <>
                    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                    Saving...
                  </>
                ) : "Save schedule"}
              </button>
            </div>
          ) : (
            <div className="text-sm text-red-400">Failed to load schedule</div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Comment drawer (slide-in side panel) ───────────────────────────────────
//
// Opens when a user clicks any [data-metric-key] cell in the rendered
// report HTML. Threads are scoped by (week_start, branch_id, metric_key)
// — same key the backend uses — so the same drawer can show the past
// week's discussion when the user filters to an archived week.

function CommentDrawer({ context, currentUser, initialComments = [], onClose, onChanged }) {
  // Render synchronously from the parent's prefetched list. Mutations
  // (post / patch / delete) call onChanged() so the parent refetches
  // the whole-week list, and the new initialComments prop flows back in.
  const [submitting, setSubmitting] = useState(false);
  const [draft, setDraft] = useState("");
  const [markAction, setMarkAction] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [editingText, setEditingText] = useState("");

  const comments = initialComments;

  // POST schema doesn't accept is_action_item — patch right after if checked
  const submitWithActionFlag = async () => {
    const text = draft.trim();
    if (!text) return;
    setSubmitting(true);
    try {
      const r = await axios.post("/api/report/comments", {
        week_start: context.weekStart,
        branch_id: context.branchId || null,
        metric_key: context.metricKey,
        body: text,
      });
      if (markAction) {
        await axios.patch(`/api/report/comments/${r.data.data.id}`, { is_action_item: true });
      }
      setDraft("");
      setMarkAction(false);
      onChanged?.();
    } catch (e) {
      alert(e.response?.data?.detail || "Failed to post comment");
    } finally {
      setSubmitting(false);
    }
  };

  const toggleAction = async (c) => {
    try {
      await axios.patch(`/api/report/comments/${c.id}`, { is_action_item: !c.is_action_item });
      onChanged?.();
    } catch (e) {
      alert(e.response?.data?.detail || "Failed to update comment");
    }
  };

  const toggleResolved = async (c) => {
    try {
      await axios.patch(`/api/report/comments/${c.id}`, { is_resolved: !c.is_resolved });
      onChanged?.();
    } catch (e) {
      alert(e.response?.data?.detail || "Failed to update comment");
    }
  };

  const saveEdit = async (c) => {
    const text = editingText.trim();
    if (!text) return;
    try {
      await axios.patch(`/api/report/comments/${c.id}`, { body: text });
      setEditingId(null);
      setEditingText("");
      onChanged?.();
    } catch (e) {
      alert(e.response?.data?.detail || "Failed to save edit");
    }
  };

  const remove = async (c) => {
    if (!confirm("Delete this comment? This cannot be undone.")) return;
    try {
      await axios.delete(`/api/report/comments/${c.id}`);
      onChanged?.();
    } catch (e) {
      alert(e.response?.data?.detail || "Failed to delete comment");
    }
  };

  const canEdit = (c) => currentUser && (c.author_id === currentUser.id || currentUser.role === "admin");

  if (!context) return null;

  return (
    <>
      <div
        className="fixed inset-0 bg-black/30 z-40"
        onClick={onClose}
      />
      <div className="fixed top-0 right-0 bottom-0 w-full sm:w-[440px] bg-white shadow-2xl z-50 flex flex-col">
        {/* Header */}
        <div className="px-5 py-4 border-b border-gray-200 flex items-start justify-between">
          <div className="min-w-0">
            <p className="text-[11px] text-gray-500 uppercase tracking-wide">Discussion</p>
            <h3 className="text-base font-semibold text-gray-900 truncate">
              {metricLabel(context.metricKey, context.metricLabelOverride)}
            </h3>
            <p className="text-xs text-gray-500 mt-0.5 truncate">
              {context.branchName ? `${context.branchName} · ` : ""}Week of {fmtWeekLabel(context.weekStart)}
            </p>
          </div>
          <button
            onClick={onClose}
            className="ml-3 text-gray-400 hover:text-gray-700 text-2xl leading-none"
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        {/* Comment list */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
          {comments.length === 0 ? (
            <p className="text-sm text-gray-400">No discussion yet. Start the thread below.</p>
          ) : (
            comments.map(c => (
              <div
                key={c.id}
                className={`rounded-lg border p-3 ${
                  c.is_resolved
                    ? "bg-gray-50 border-gray-200 opacity-70"
                    : c.is_action_item
                    ? "bg-amber-50 border-amber-200"
                    : "bg-white border-gray-200"
                }`}
              >
                <div className="flex items-center justify-between gap-2 mb-1">
                  <div className="min-w-0">
                    <p className="text-xs font-semibold text-gray-800 truncate">
                      {c.author_name || c.author_email || "Unknown"}
                      <span className="ml-1 text-[10px] text-gray-400 font-normal uppercase">{c.author_role}</span>
                    </p>
                    <p className="text-[10px] text-gray-400">
                      {c.created_at ? new Date(c.created_at).toLocaleString() : ""}
                      {c.updated_at && c.updated_at !== c.created_at && " · edited"}
                    </p>
                  </div>
                  <div className="flex gap-1 text-[10px]">
                    {c.is_action_item && (
                      <span className="bg-amber-200 text-amber-900 px-1.5 py-0.5 rounded font-semibold">ACTION</span>
                    )}
                    {c.is_resolved && (
                      <span className="bg-green-200 text-green-900 px-1.5 py-0.5 rounded font-semibold">RESOLVED</span>
                    )}
                  </div>
                </div>
                {editingId === c.id ? (
                  <div>
                    <textarea
                      value={editingText}
                      onChange={e => setEditingText(e.target.value)}
                      rows={3}
                      className="w-full text-sm px-2 py-1.5 border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                    <div className="flex gap-2 mt-2 text-xs">
                      <button onClick={() => saveEdit(c)} className="px-2 py-1 bg-indigo-600 text-white rounded hover:bg-indigo-700">Save</button>
                      <button onClick={() => { setEditingId(null); setEditingText(""); }} className="px-2 py-1 bg-gray-100 text-gray-600 rounded hover:bg-gray-200">Cancel</button>
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-gray-800 whitespace-pre-wrap break-words">{c.body}</p>
                )}
                <div className="flex flex-wrap gap-2 mt-2 text-[11px]">
                  <button
                    onClick={() => toggleAction(c)}
                    className="text-gray-500 hover:text-amber-700"
                    title={c.is_action_item ? "Unmark action item" : "Mark as action item"}
                  >
                    {c.is_action_item ? "✓ Action item" : "Mark action"}
                  </button>
                  <span className="text-gray-300">·</span>
                  <button
                    onClick={() => toggleResolved(c)}
                    className="text-gray-500 hover:text-green-700"
                  >
                    {c.is_resolved ? "Reopen" : "Resolve"}
                  </button>
                  {canEdit(c) && editingId !== c.id && (
                    <>
                      <span className="text-gray-300">·</span>
                      <button
                        onClick={() => { setEditingId(c.id); setEditingText(c.body); }}
                        className="text-gray-500 hover:text-indigo-700"
                      >
                        Edit
                      </button>
                      <span className="text-gray-300">·</span>
                      <button
                        onClick={() => remove(c)}
                        className="text-gray-500 hover:text-red-600"
                      >
                        Delete
                      </button>
                    </>
                  )}
                </div>
              </div>
            ))
          )}
        </div>

        {/* Composer */}
        <div className="border-t border-gray-200 px-5 py-3 bg-gray-50">
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder="Add a comment or question…"
            rows={3}
            className="w-full text-sm px-3 py-2 border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 bg-white"
          />
          <div className="flex items-center justify-between mt-2">
            <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
              <input
                type="checkbox"
                checked={markAction}
                onChange={e => setMarkAction(e.target.checked)}
                className="h-3.5 w-3.5 text-indigo-600 rounded"
              />
              Mark as action item
            </label>
            <button
              onClick={submitWithActionFlag}
              disabled={submitting || !draft.trim()}
              className="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50"
            >
              {submitting ? "Posting…" : "Post"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

// ── General Notes card ──────────────────────────────────────────────────────
//
// One thread per (week_start, branch_id) using metric_key=_general. Lives
// at the top of each branch view + the All-Branches summary view. Shows
// a 1-line preview of the most recent note + open count; clicking opens
// the standard CommentDrawer.

function GeneralNotesCard({ branchId, branchName, allComments, onOpen }) {
  const notes = useMemo(() => allComments.filter(c =>
    c.metric_key === GENERAL_KEY && (c.branch_id || null) === (branchId || null)
  ), [allComments, branchId]);

  const openCount = notes.filter(c => !c.is_resolved).length;
  const actionCount = notes.filter(c => !c.is_resolved && c.is_action_item).length;
  const latest = notes.length ? notes[notes.length - 1] : null;
  const scope = branchName ? branchName : "All Branches Summary";

  return (
    <div
      onClick={onOpen}
      className="bg-white rounded-xl border border-gray-200 hover:border-indigo-400 hover:bg-indigo-50/30 p-4 cursor-pointer transition flex items-start gap-3"
    >
      <div className="text-xl leading-none mt-0.5">📌</div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <h4 className="font-semibold text-gray-800 text-sm">General notes · {scope}</h4>
          {openCount > 0 && (
            <span className="text-[10px] bg-indigo-100 text-indigo-800 px-1.5 py-0.5 rounded font-semibold">
              {openCount} open
            </span>
          )}
          {actionCount > 0 && (
            <span className="text-[10px] bg-amber-200 text-amber-900 px-1.5 py-0.5 rounded font-semibold">
              ⚡{actionCount} action
            </span>
          )}
        </div>
        {latest ? (
          <p className="text-xs text-gray-600 mt-1 truncate">
            <span className="text-gray-500">{latest.author_name || latest.author_email || "Unknown"}:</span>{" "}
            {latest.body}
          </p>
        ) : (
          <p className="text-xs text-gray-400 mt-1">No general notes yet — click to add one.</p>
        )}
      </div>
      <span className="text-xs text-indigo-600 font-medium whitespace-nowrap">Open →</span>
    </div>
  );
}

// ── All-Comments panel ──────────────────────────────────────────────────────
//
// Side panel showing every thread in the active week. Each entry is a
// row in the (branch_id, metric_key) group; clicking jumps to that cell
// (switching branches + scrolling into view) and opens the standard
// CommentDrawer. Mirrors the Google Sheets "Comments" UX.

function AllCommentsPanel({ allComments, branches, onClose, onJump, weekStart }) {
  const [filter, setFilter] = useState("open"); // open / action / resolved / all

  // Group by (branch_id, metric_key). Newest thread (most recent comment) first.
  const threads = useMemo(() => {
    const map = new Map();
    allComments.forEach(c => {
      const key = `${c.branch_id || ""}::${c.metric_key}`;
      if (!map.has(key)) {
        map.set(key, {
          key,
          branchId: c.branch_id || null,
          metricKey: c.metric_key,
          metricLabel: metricLabel(c.metric_key),
          comments: [],
          lastAt: c.created_at,
          openCount: 0,
          hasAction: false,
        });
      }
      const t = map.get(key);
      t.comments.push(c);
      if (c.created_at > t.lastAt) t.lastAt = c.created_at;
      if (!c.is_resolved) t.openCount += 1;
      if (!c.is_resolved && c.is_action_item) t.hasAction = true;
    });
    return Array.from(map.values()).sort((a, b) => (a.lastAt < b.lastAt ? 1 : -1));
  }, [allComments]);

  const filtered = useMemo(() => threads.filter(t => {
    if (filter === "all") return true;
    if (filter === "resolved") return t.openCount === 0;
    if (filter === "action") return t.hasAction;
    return t.openCount > 0; // "open"
  }), [threads, filter]);

  const branchName = (bid) => {
    if (!bid) return "All Branches";
    const b = branches.find(x => x.id === bid);
    return b?.name || "Unknown branch";
  };

  return (
    <>
      <div className="fixed inset-0 bg-black/30 z-40" onClick={onClose} />
      <div className="fixed top-0 right-0 bottom-0 w-full sm:w-[460px] bg-white shadow-2xl z-50 flex flex-col">
        <div className="px-5 py-4 border-b border-gray-200 flex items-start justify-between">
          <div>
            <p className="text-[11px] text-gray-500 uppercase tracking-wide">All Comments</p>
            <h3 className="text-base font-semibold text-gray-900">
              Week of {fmtWeekLabel(weekStart)}
            </h3>
            <p className="text-xs text-gray-500 mt-0.5">
              {threads.length} thread{threads.length === 1 ? "" : "s"} · click to jump to the metric
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700 text-2xl leading-none ml-3" aria-label="Close">&times;</button>
        </div>

        {/* Filter bar */}
        <div className="px-5 py-2 border-b border-gray-100 flex gap-1.5 text-xs">
          {[
            { v: "open", label: "Open" },
            { v: "action", label: "Action items" },
            { v: "resolved", label: "Resolved" },
            { v: "all", label: "All" },
          ].map(opt => (
            <button
              key={opt.v}
              onClick={() => setFilter(opt.v)}
              className={`px-2.5 py-1 rounded-md font-medium ${
                filter === opt.v ? "bg-indigo-600 text-white" : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-3 space-y-2">
          {filtered.length === 0 ? (
            <p className="text-sm text-gray-400">No threads match this filter.</p>
          ) : (
            filtered.map(t => {
              const latest = t.comments[t.comments.length - 1];
              const resolved = t.openCount === 0;
              return (
                <div
                  key={t.key}
                  onClick={() => onJump(t)}
                  className={`rounded-lg border p-3 cursor-pointer hover:border-indigo-400 hover:shadow-sm transition ${
                    resolved ? "bg-gray-50 border-gray-200 opacity-70" : "bg-white border-gray-200"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <div className="min-w-0 flex-1">
                      <p className="text-[11px] text-gray-500 truncate">
                        {branchName(t.branchId)}
                      </p>
                      <p className="text-sm font-semibold text-gray-800 truncate">
                        {t.metricLabel}
                      </p>
                    </div>
                    <div className="flex flex-col items-end gap-1 text-[10px]">
                      {t.hasAction && (
                        <span className="bg-amber-200 text-amber-900 px-1.5 py-0.5 rounded font-semibold">⚡ACTION</span>
                      )}
                      {resolved && (
                        <span className="bg-green-200 text-green-900 px-1.5 py-0.5 rounded font-semibold">RESOLVED</span>
                      )}
                      <span className="text-gray-400">{t.comments.length} msg</span>
                    </div>
                  </div>
                  {latest && (
                    <p className="text-xs text-gray-600 line-clamp-2">
                      <span className="text-gray-500">{latest.author_name || latest.author_email || "?"}:</span>{" "}
                      {latest.body}
                    </p>
                  )}
                  <p className="text-[10px] text-gray-400 mt-1">
                    {latest?.created_at ? new Date(latest.created_at).toLocaleString() : ""}
                  </p>
                </div>
              );
            })
          )}
        </div>
      </div>
    </>
  );
}

// ── Weekly Report viewer tab ────────────────────────────────────────────────

function WeeklyReportTab({ initialBranch, onBranchChange }) {
  const { user: currentUser } = useAuth();
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState(null);
  const [parsed, setParsed] = useState(null);
  const [loadedAt, setLoadedAt] = useState(null);
  const [selectedBranch, setSelectedBranch] = useState(initialBranch || "all");

  // Week filter: "current" means live cache; YYYY-MM-DD means archive snapshot.
  const [selectedWeek, setSelectedWeek] = useState("current");
  const [archives, setArchives] = useState([]);

  // Active week_start for comment scope (always a real date — `current` is
  // resolved to this Monday).
  const activeWeekStart = selectedWeek === "current" ? fmtIsoDate(thisMonday()) : selectedWeek;

  // Preloaded comments for the whole week — single fetch, then drawer
  // reads from this in-memory cache so opening a thread is instant.
  // (Previous design hit /comments on every drawer open — perceptibly
  // slow when clicking through cells.)
  const [allComments, setAllComments] = useState([]);
  const [drawer, setDrawer] = useState(null);
  const [allCommentsOpen, setAllCommentsOpen] = useState(false);
  const reportContainerRef = useRef(null);

  const loadArchives = useCallback(async () => {
    try {
      const r = await axios.get("/api/report/archives");
      setArchives(r.data.data || []);
    } catch (e) {
      console.error("Failed to load archives", e);
    }
  }, []);

  const loadAllComments = useCallback(async (weekStart) => {
    try {
      const r = await axios.get("/api/report/comments", { params: { week_start: weekStart } });
      setAllComments(r.data.data || []);
    } catch (e) {
      console.error("Failed to load comments", e);
    }
  }, []);

  // Counts derived from allComments (open threads + action-item flag per cell).
  // Recomputed cheaply on any change — no extra API call.
  const commentCounts = useMemo(() => {
    const map = {};
    allComments.forEach(c => {
      if (c.is_resolved) return;
      const k = `${c.branch_id || ""}::${c.metric_key}`;
      if (!map[k]) map[k] = { count: 0, actionItems: 0 };
      map[k].count += 1;
      if (c.is_action_item) map[k].actionItems += 1;
    });
    return map;
  }, [allComments]);

  const loadReport = useCallback(async () => {
    setReportLoading(true);
    setReportError(null);
    try {
      const url = selectedWeek === "current"
        ? "/api/report/preview"
        : `/api/report/archives/${selectedWeek}/preview`;
      const r = await fetch(url);
      if (!r.ok) {
        if (r.status === 404 && selectedWeek !== "current") {
          throw new Error(`No archive saved for the week of ${fmtWeekLabel(selectedWeek)}.`);
        }
        throw new Error(`Failed to load report (HTTP ${r.status})`);
      }
      const text = await r.text();
      const p = parseReportHtml(text);
      setParsed(p);
      setLoadedAt(new Date());
      if (selectedBranch !== "all" && !p.branches.find(b => b.id === selectedBranch)) {
        setSelectedBranch("all");
        onBranchChange?.("all");
      }
    } catch (e) {
      setReportError(e.message || "Failed to load report");
    } finally {
      setReportLoading(false);
    }
  }, [selectedWeek, selectedBranch, onBranchChange]);

  // Initial load
  useEffect(() => { loadArchives(); }, [loadArchives]);
  useEffect(() => { loadReport(); }, [loadReport]);
  useEffect(() => { loadAllComments(activeWeekStart); }, [activeWeekStart, loadAllComments]);

  const selectBranch = (id) => {
    setSelectedBranch(id);
    onBranchChange?.(id);
  };

  const renderedHtml = useMemo(() => {
    if (!parsed) return "";
    if (selectedBranch === "all") {
      return parsed.execSummaryHtml;
    }
    const b = parsed.branches.find(x => x.id === selectedBranch);
    if (!b) return parsed.execSummaryHtml;
    return b.html + b.optimizerHtml;
  }, [parsed, selectedBranch]);

  // Click delegation: capture clicks anywhere inside the rendered HTML
  // and resolve them to the closest [data-metric-key] cell.
  const onReportClick = (e) => {
    const cell = e.target.closest("[data-metric-key]");
    if (!cell || !reportContainerRef.current?.contains(cell)) return;
    const metricKey = cell.dataset.metricKey;
    const branchId = cell.dataset.branchId || null;
    const metricLabelOverride = cell.dataset.metricLabel || null;
    // Branch name lookup — for the drawer header
    let branchName = null;
    if (branchId && parsed) {
      const b = parsed.branches.find(x => x.id === branchId);
      branchName = b?.name || null;
    }
    setDrawer({
      weekStart: activeWeekStart,
      branchId,
      branchName,
      metricKey,
      metricLabelOverride,
    });
  };

  // Open the page-level "General notes" thread for the active view.
  // branchId = null when viewing the All-Branches summary; otherwise the
  // selected branch's id. Reuses the standard CommentDrawer.
  const openGeneralNotes = () => {
    const branchId = selectedBranch === "all" ? null : selectedBranch;
    const branchName = branchId && parsed
      ? parsed.branches.find(x => x.id === branchId)?.name
      : null;
    setDrawer({
      weekStart: activeWeekStart,
      branchId,
      branchName: branchName || (branchId ? null : "All Branches Summary"),
      metricKey: GENERAL_KEY,
      metricLabelOverride: null,
    });
  };

  // Called from AllCommentsPanel — navigates to the matching cell (switching
  // branches if needed), highlights it briefly, then opens the drawer.
  const jumpToThread = (thread) => {
    setAllCommentsOpen(false);
    // General-notes threads don't have a cell to scroll to — just open
    // the drawer.
    if (thread.metricKey === GENERAL_KEY) {
      const branchName = thread.branchId && parsed
        ? parsed.branches.find(x => x.id === thread.branchId)?.name
        : null;
      setDrawer({
        weekStart: activeWeekStart,
        branchId: thread.branchId,
        branchName: branchName || (thread.branchId ? null : "All Branches Summary"),
        metricKey: GENERAL_KEY,
        metricLabelOverride: null,
      });
      // Also switch to the right view so general-notes context matches.
      const wantBranch = thread.branchId || "all";
      if (wantBranch !== selectedBranch) selectBranch(wantBranch);
      return;
    }
    // Regular metric — switch branches if needed, then on next tick
    // find the cell, scroll into view + flash a ring around it, then
    // open the drawer.
    const wantBranch = thread.branchId || "all";
    if (wantBranch !== selectedBranch) {
      selectBranch(wantBranch);
    }
    // Defer to after React has re-rendered the new branch view.
    setTimeout(() => {
      const root = reportContainerRef.current;
      if (!root) return;
      const sel = thread.branchId
        ? `[data-metric-key="${CSS.escape(thread.metricKey)}"][data-branch-id="${thread.branchId}"]`
        : `[data-metric-key="${CSS.escape(thread.metricKey)}"]`;
      const cell = root.querySelector(sel);
      if (cell) {
        cell.scrollIntoView({ behavior: "smooth", block: "center" });
        cell.classList.add("hid-flash");
        setTimeout(() => cell.classList.remove("hid-flash"), 1800);
      }
      const branchName = thread.branchId && parsed
        ? parsed.branches.find(x => x.id === thread.branchId)?.name
        : null;
      setDrawer({
        weekStart: activeWeekStart,
        branchId: thread.branchId,
        branchName,
        metricKey: thread.metricKey,
        metricLabelOverride: thread.metricLabel,
      });
    }, 80);
  };

  // After HTML renders or counts change, inject comment badges into each
  // [data-metric-key] cell so users can see at a glance which cells have
  // active discussion.
  useEffect(() => {
    const root = reportContainerRef.current;
    if (!root) return;
    root.querySelectorAll(".hid-comment-badge").forEach(el => el.remove());
    root.querySelectorAll("[data-metric-key]").forEach(cell => {
      const branchId = cell.dataset.branchId || "";
      const metricKey = cell.dataset.metricKey;
      const k = `${branchId}::${metricKey}`;
      const info = commentCounts[k];
      if (!info || !info.count) return;
      const badge = document.createElement("span");
      badge.className = "hid-comment-badge";
      badge.textContent = info.actionItems > 0
        ? `⚡${info.count}`
        : `💬${info.count}`;
      badge.title = info.actionItems > 0
        ? `${info.count} open comment(s), ${info.actionItems} action item(s)`
        : `${info.count} open comment(s)`;
      // <tr> can't directly hold a <span>; append to the row's last <td>
      // instead so the badge sits at the visual end of the row.
      if (cell.tagName === "TR") {
        const lastCell = cell.querySelector("td:last-child");
        (lastCell || cell).appendChild(badge);
      } else {
        cell.appendChild(badge);
      }
    });
  }, [renderedHtml, commentCounts]);

  return (
    <div className="space-y-4">
      {/* Inline styles: hover effect + badge appearance. Scoped under
          .hid-report-body so they don't leak into the email render. */}
      <style>{`
        .hid-report-body .hid-metric-cell {
          cursor: pointer;
          position: relative;
          transition: background-color 0.15s ease;
        }
        .hid-report-body .hid-metric-cell:hover {
          background-color: rgba(99, 102, 241, 0.10) !important;
          outline: 1px dashed rgba(99, 102, 241, 0.6);
          outline-offset: -2px;
        }
        .hid-comment-badge {
          display: inline-block;
          margin-left: 6px;
          padding: 1px 6px;
          font-size: 10px;
          font-weight: 600;
          color: #4338ca;
          background: #eef2ff;
          border: 1px solid #c7d2fe;
          border-radius: 999px;
          vertical-align: middle;
          line-height: 1.3;
        }
        @keyframes hid-flash-bg {
          0%, 100% { background-color: transparent; }
          50% { background-color: rgba(250, 204, 21, 0.5); }
        }
        .hid-report-body .hid-flash {
          animation: hid-flash-bg 0.9s ease-in-out 2;
          outline: 2px solid rgba(245, 158, 11, 0.8) !important;
          outline-offset: -2px;
        }
      `}</style>

      {/* Top bar: header info + week filter + refresh */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 flex items-center justify-between flex-wrap gap-3">
        <div>
          <h3 className="font-semibold text-gray-800 text-sm">📊 Weekly Report</h3>
          <p className="text-[11px] text-gray-500 mt-0.5">
            Click any KPI cell to start or join a discussion. Switch weeks below to review past reports.
            {loadedAt && <span> Loaded at {loadedAt.toLocaleTimeString()}.</span>}
          </p>
        </div>
        <div className="flex gap-2 items-center">
          <select
            value={selectedWeek}
            onChange={e => setSelectedWeek(e.target.value)}
            className="px-3 py-1.5 border border-gray-200 text-sm rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-indigo-500"
            title="Filter by week"
          >
            <option value="current">This week (live)</option>
            {archives
              .filter(a => a.week_start !== fmtIsoDate(thisMonday()))
              .map(a => (
                <option key={a.week_start} value={a.week_start}>
                  {fmtWeekLabel(a.week_start)}
                  {a.open_comment_count > 0 ? ` · 💬${a.open_comment_count}` : ""}
                </option>
              ))}
          </select>
          <button
            onClick={() => setAllCommentsOpen(true)}
            className="px-3 py-1.5 border border-gray-200 text-gray-700 text-sm rounded-lg hover:bg-gray-50 flex items-center gap-1.5"
            title="View all comments for this week"
          >
            💬 All comments
            {allComments.filter(c => !c.is_resolved).length > 0 && (
              <span className="bg-indigo-600 text-white text-[10px] font-semibold px-1.5 py-0.5 rounded-full leading-none">
                {allComments.filter(c => !c.is_resolved).length}
              </span>
            )}
          </button>
          <a
            href={selectedWeek === "current"
              ? "/api/report/preview"
              : `/api/report/archives/${selectedWeek}/preview`}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 border border-gray-200 text-gray-600 text-sm rounded-lg hover:bg-gray-50"
          >
            Open raw preview ↗
          </a>
          <button
            onClick={loadReport}
            disabled={reportLoading}
            className="px-3 py-1.5 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50"
          >
            {reportLoading ? "Loading..." : "Refresh"}
          </button>
        </div>
      </div>

      {/* Loading state */}
      {reportLoading && !parsed && (
        <div className="bg-white rounded-xl border border-gray-200 p-12 text-center">
          <svg className="animate-spin h-8 w-8 text-indigo-600 mx-auto mb-3" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          <p className="text-sm text-gray-600">Generating weekly report...</p>
          <p className="text-xs text-gray-400 mt-1">Pulling Cloudbeds Insights for all branches — typically 30-90 seconds.</p>
        </div>
      )}

      {/* Error state */}
      {reportError && !reportLoading && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-5 text-center">
          <p className="text-sm text-red-700 font-medium mb-1">Failed to load report</p>
          <p className="text-xs text-red-600">{reportError}</p>
          <button onClick={loadReport} className="mt-3 px-4 py-1.5 bg-red-600 text-white text-xs rounded-lg hover:bg-red-700">
            Retry
          </button>
        </div>
      )}

      {/* Loaded state */}
      {parsed && (
        <>
          {/* Branch selector tabs */}
          <div className="bg-white rounded-xl border border-gray-200 p-3">
            <div className="flex flex-wrap gap-1.5">
              <button
                onClick={() => selectBranch("all")}
                className={`px-3 py-1.5 text-sm rounded-md font-medium transition ${
                  selectedBranch === "all"
                    ? "bg-indigo-600 text-white"
                    : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                }`}
              >
                All Branches (Summary)
              </button>
              {parsed.branches.map(b => (
                <button
                  key={b.id}
                  onClick={() => selectBranch(b.id)}
                  className={`px-3 py-1.5 text-sm rounded-md font-medium transition ${
                    selectedBranch === b.id
                      ? "bg-indigo-600 text-white"
                      : "bg-gray-100 text-gray-600 hover:bg-gray-200"
                  }`}
                >
                  {b.name}
                </button>
              ))}
            </div>
          </div>

          {/* General notes card — one thread per (week, branch) for
              page-wide discussion that isn't tied to a specific metric. */}
          <GeneralNotesCard
            branchId={selectedBranch === "all" ? null : selectedBranch}
            branchName={
              selectedBranch === "all"
                ? null
                : parsed.branches.find(b => b.id === selectedBranch)?.name
            }
            allComments={allComments}
            onOpen={openGeneralNotes}
          />

          {/* Rendered HTML — wrapped in a container that strips email-specific
              body padding so it fits the dashboard layout. The HTML uses
              inline styles so it renders correctly without extra CSS. */}
          <div className="bg-gray-50 rounded-xl p-1 border border-gray-200">
            <div
              ref={reportContainerRef}
              onClick={onReportClick}
              className="hid-report-body"
              style={{ background: "#f3f4f6", padding: "16px", borderRadius: "12px" }}
              dangerouslySetInnerHTML={{ __html: renderedHtml }}
            />
          </div>
        </>
      )}

      {/* All-comments side panel */}
      {allCommentsOpen && parsed && (
        <AllCommentsPanel
          allComments={allComments}
          branches={parsed.branches}
          weekStart={activeWeekStart}
          onClose={() => setAllCommentsOpen(false)}
          onJump={jumpToThread}
        />
      )}

      {/* Discussion drawer — filter the prefetched list for this cell so
          the drawer can render synchronously (no spinner on open). */}
      {drawer && (
        <CommentDrawer
          context={drawer}
          currentUser={currentUser}
          initialComments={allComments.filter(c =>
            c.metric_key === drawer.metricKey &&
            (c.branch_id || null) === (drawer.branchId || null)
          )}
          onClose={() => setDrawer(null)}
          onChanged={() => loadAllComments(activeWeekStart)}
        />
      )}
    </div>
  );
}

// ── Page shell ──────────────────────────────────────────────────────────────

export default function Report() {
  const [tab, setTab] = useState(() => readUrlState().tab);
  const [initialBranch] = useState(() => readUrlState().branch);
  const [toast, setToast] = useState(null);

  // Sync URL whenever tab changes
  const switchTab = (newTab) => {
    setTab(newTab);
    writeUrlState(newTab, initialBranch);
  };
  const onBranchChange = (id) => {
    writeUrlState("report", id);
  };

  return (
    <div className="space-y-5 max-w-7xl mx-auto">
      {/* Page header + tab switcher */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Weekly Report</h1>
          <p className="text-xs text-gray-500 mt-0.5">
            View per-branch detail in the report tab, or manage email recipients in settings.
          </p>
        </div>
        <div className="flex bg-gray-100 rounded-lg p-0.5">
          <button
            onClick={() => switchTab("report")}
            className={`px-4 py-1.5 text-sm rounded-md transition ${
              tab === "report" ? "bg-white shadow text-gray-800 font-medium" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            📊 Weekly Report
          </button>
          <button
            onClick={() => switchTab("settings")}
            className={`px-4 py-1.5 text-sm rounded-md transition ${
              tab === "settings" ? "bg-white shadow text-gray-800 font-medium" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            ⚙️ Settings
          </button>
        </div>
      </div>

      {tab === "report" ? (
        <WeeklyReportTab initialBranch={initialBranch} onBranchChange={onBranchChange} />
      ) : (
        <SettingsTab toast={toast} setToast={setToast} />
      )}

      {/* Toast notification */}
      {toast && <Toast message={toast.message} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
