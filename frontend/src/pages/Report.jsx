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
import { useEffect, useMemo, useState } from "react";
import axios from "axios";

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
    tab: p.get("view") === "full" ? "report" : (p.get("tab") === "report" ? "report" : "settings"),
    branch: p.get("branch") || "all",
  };
}

function writeUrlState(tab, branch) {
  const p = new URLSearchParams(window.location.search);
  if (tab === "report") {
    p.set("view", "full");
  } else {
    p.delete("view");
    p.delete("branch");
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

// ── Weekly Report viewer tab ────────────────────────────────────────────────

function WeeklyReportTab({ initialBranch, onBranchChange }) {
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState(null);
  const [parsed, setParsed] = useState(null);
  const [loadedAt, setLoadedAt] = useState(null);
  const [selectedBranch, setSelectedBranch] = useState(initialBranch || "all");

  const loadReport = async () => {
    setReportLoading(true);
    setReportError(null);
    try {
      const r = await fetch("/api/report/preview");
      if (!r.ok) {
        throw new Error(`Failed to load report (HTTP ${r.status})`);
      }
      const text = await r.text();
      const p = parseReportHtml(text);
      setParsed(p);
      setLoadedAt(new Date());
      // If URL specified a branch but it's not in the data, fallback to "all"
      if (selectedBranch !== "all" && !p.branches.find(b => b.id === selectedBranch)) {
        setSelectedBranch("all");
        onBranchChange?.("all");
      }
    } catch (e) {
      setReportError(e.message || "Failed to load report");
    } finally {
      setReportLoading(false);
    }
  };

  // Auto-load on first mount
  useEffect(() => {
    loadReport();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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

  return (
    <div className="space-y-4">
      {/* Top bar: header info + refresh */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 flex items-center justify-between flex-wrap gap-3">
        <div>
          <h3 className="font-semibold text-gray-800 text-sm">📊 Weekly Report</h3>
          <p className="text-[11px] text-gray-500 mt-0.5">
            Same content as the email&apos;s full version. Served from cache (refreshed Mon 03:00 ICT) for instant load.
            {loadedAt && (
              <span> Loaded at {loadedAt.toLocaleTimeString()}.</span>
            )}
          </p>
        </div>
        <div className="flex gap-2">
          <a
            href="/api/report/preview"
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

          {/* Rendered HTML — wrapped in a container that strips email-specific
              body padding so it fits the dashboard layout. The HTML uses
              inline styles so it renders correctly without extra CSS. */}
          <div className="bg-gray-50 rounded-xl p-1 border border-gray-200">
            <div
              className="hid-report-body"
              style={{ background: "#f3f4f6", padding: "16px", borderRadius: "12px" }}
              dangerouslySetInnerHTML={{ __html: renderedHtml }}
            />
          </div>
        </>
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
