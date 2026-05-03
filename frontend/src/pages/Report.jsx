/**
 * Weekly Report — Phase 3 + Email Preview & Schedule
 */
import { useEffect, useState, useRef } from "react";
import axios from "axios";
import { useBranch, CURRENCY_SYMBOLS } from "../context/BranchContext";

function fmt(val, currency) {
  if (val == null) return "\u2014";
  const sym = CURRENCY_SYMBOLS[currency] || "";
  return sym + new Intl.NumberFormat("en").format(Math.round(val));
}

function WoWBadge({ pct }) {
  if (pct == null) return <span className="text-gray-400">{"\u2014"}</span>;
  const cls = pct >= 0 ? "text-green-600" : "text-red-500";
  return <span className={cls + " font-semibold text-sm"}>{pct >= 0 ? "+" : ""}{pct}%</span>;
}

function AchievementBar({ pct }) {
  if (pct == null) return null;
  const color = pct >= 100 ? "bg-green-500" : pct >= 80 ? "bg-yellow-400" : "bg-red-400";
  const width = Math.min(pct, 100);
  return (
    <div className="flex items-center gap-2 mt-1">
      <div className="flex-1 bg-gray-200 rounded-full h-1.5">
        <div className={color + " h-1.5 rounded-full"} style={{ width: width + "%" }} />
      </div>
      <span className="text-xs font-medium text-gray-600 w-10 text-right">{Math.round(pct)}%</span>
    </div>
  );
}

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

export default function Report() {
  const { selected, isAll } = useBranch();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("report"); // "report" | "email"

  // Email section state
  const [testEmail, setTestEmail] = useState("");
  const [members, setMembers] = useState([]);
  const [selectedMemberIds, setSelectedMemberIds] = useState([]);
  const [membersLoading, setMembersLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [previewKey, setPreviewKey] = useState(0);
  const [toast, setToast] = useState(null);

  // Schedule state
  const [schedule, setSchedule] = useState(null);
  const [scheduleLoading, setScheduleLoading] = useState(false);
  const [newRecipient, setNewRecipient] = useState("");
  const [savingSchedule, setSavingSchedule] = useState(false);

  const iframeRef = useRef(null);

  const load = () => {
    setLoading(true);
    const params = new URLSearchParams();
    if (!isAll && selected) params.set("branch_id", selected);
    axios.get("/api/report/weekly?" + params)
      .then(r => setData(r.data.data))
      .catch(() => setData(null))
      .finally(() => setLoading(false));
  };

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

  useEffect(() => { load(); }, [selected, isAll]);
  useEffect(() => {
    if (tab === "email") {
      loadSchedule();
      loadMembers();
    }
  }, [tab]);

  const toggleMember = (id) => {
    setSelectedMemberIds(prev =>
      prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]
    );
  };
  const selectAllMembers = () => setSelectedMemberIds(members.filter(m => m.is_active).map(m => m.id));
  const clearMembers = () => setSelectedMemberIds([]);

  const sendTestEmail = async () => {
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

  const removeRecipient = (email) => {
    setSchedule({ ...schedule, recipients: schedule.recipients.filter(r => r !== email) });
  };

  if (loading) return <div className="p-8 text-center text-gray-400 animate-pulse">Generating report...</div>;
  if (!data) return <div className="p-8 text-center text-red-400">Failed to generate report.</div>;

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Weekly Report</h1>
          {data.period && (
            <p className="text-xs text-gray-400 mt-0.5">
              Week {data.period.week_start} &rarr; {data.period.week_end}
            </p>
          )}
        </div>
        <div className="flex gap-2">
          {/* Tab buttons */}
          <div className="flex bg-gray-100 rounded-lg p-0.5">
            <button
              onClick={() => setTab("report")}
              className={`px-3 py-1.5 text-sm rounded-md transition ${tab === "report" ? "bg-white shadow text-gray-800 font-medium" : "text-gray-500 hover:text-gray-700"}`}
            >
              Report
            </button>
            <button
              onClick={() => setTab("email")}
              className={`px-3 py-1.5 text-sm rounded-md transition ${tab === "email" ? "bg-white shadow text-gray-800 font-medium" : "text-gray-500 hover:text-gray-700"}`}
            >
              Email
            </button>
          </div>
          <button onClick={load} className="px-3 py-1.5 border border-gray-200 text-gray-600 text-sm rounded-lg hover:bg-gray-50">
            Refresh
          </button>
          <button onClick={() => window.print()} className="px-4 py-1.5 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700">
            Print / PDF
          </button>
        </div>
      </div>

      {/* Report Tab */}
      {tab === "report" && (
        <>
          {data.branches.length === 0 && (
            <div className="bg-white rounded-xl border p-8 text-center text-gray-400">No branch data available.</div>
          )}

          {data.branches.map(b => (
            <div key={b.branch_id} className="bg-white rounded-xl border border-gray-200 overflow-hidden">
              <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between">
                <div>
                  <h2 className="font-semibold text-gray-800">{b.branch_name}</h2>
                  <p className="text-xs text-gray-400 mt-0.5">{b.currency}</p>
                </div>
                {b.achievement_pct != null && (
                  <div className="text-right">
                    <p className="text-xs text-gray-400">KPI Achievement</p>
                    <p className={"text-xl font-bold " + (b.achievement_pct >= 100 ? "text-green-600" : b.achievement_pct >= 80 ? "text-yellow-600" : "text-red-500")}>
                      {Math.round(b.achievement_pct)}%
                    </p>
                  </div>
                )}
              </div>

              <div className="p-5 space-y-5">
                {/* Revenue */}
                <div>
                  <p className="text-xs text-gray-500 font-medium uppercase tracking-wide mb-2">Revenue</p>
                  <div className="flex items-end gap-3">
                    <p className="text-2xl font-bold text-gray-800">{fmt(b.actual_revenue || b.mtd_revenue, b.currency)}</p>
                    {b.target_revenue != null && (
                      <p className="text-sm text-gray-400 pb-1">/ {fmt(b.target_revenue, b.currency)} target</p>
                    )}
                  </div>
                  <AchievementBar pct={b.achievement_pct} />
                </div>

                {/* OCC & Forecast */}
                <div className="grid grid-cols-3 gap-3">
                  <div className="bg-gray-50 rounded-lg p-3">
                    <p className="text-xs text-gray-500 mb-1">OCC% (actual)</p>
                    <p className="text-lg font-bold text-gray-800">{b.avg_occ_pct != null ? b.avg_occ_pct + "%" : "\u2014"}</p>
                  </div>
                  <div className="bg-indigo-50 rounded-lg p-3">
                    <p className="text-xs text-indigo-500 mb-1">OCC% (forecast)</p>
                    <p className="text-lg font-bold text-indigo-600">{b.predicted_occ_pct != null ? b.predicted_occ_pct + "%" : "\u2014"}</p>
                  </div>
                  <div className="bg-gray-50 rounded-lg p-3">
                    <p className="text-xs text-gray-500 mb-1">ADR</p>
                    <p className="text-lg font-bold text-gray-800">{fmt(b.avg_adr, b.currency)}</p>
                  </div>
                </div>

                {/* WoW bookings — show if data available */}
                {b.this_week_bookings != null && (
                  <div className="grid grid-cols-2 gap-4">
                    <div className="bg-gray-50 rounded-lg p-4">
                      <p className="text-xs text-gray-500 mb-1">This Week Bookings</p>
                      <p className="text-xl font-bold text-gray-800">{b.this_week_bookings}</p>
                      <div className="flex items-center gap-2 mt-1">
                        <WoWBadge pct={b.wow_booking_pct} />
                        <span className="text-xs text-gray-400">vs last week ({b.last_week_bookings})</span>
                      </div>
                    </div>
                    <div className="bg-gray-50 rounded-lg p-4">
                      <p className="text-xs text-gray-500 mb-1">This Week Revenue</p>
                      <p className="text-xl font-bold text-gray-800">{fmt(b.this_week_revenue, b.currency)}</p>
                      <div className="flex items-center gap-2 mt-1">
                        <WoWBadge pct={b.wow_revenue_pct} />
                        <span className="text-xs text-gray-400">vs last week</span>
                      </div>
                    </div>
                  </div>
                )}

                {/* Top countries */}
                {b.top_countries && b.top_countries.length > 0 && (
                  <div>
                    <p className="text-xs text-gray-500 font-medium uppercase tracking-wide mb-2">Top Countries (90d)</p>
                    <div className="flex flex-wrap gap-2">
                      {b.top_countries.map((c, i) => (
                        <div key={c.country_code || c.country || i} className="flex items-center gap-2 bg-gray-50 rounded-lg px-3 py-2">
                          <span className="text-xs font-bold text-gray-400">#{i + 1}</span>
                          <span className="text-sm font-medium text-gray-700">{c.country || c.country_code}</span>
                          <span className="text-xs text-gray-500">({c.bookings})</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Country Intel */}
                {b.country_intel && b.country_intel.length > 0 && (
                  <div>
                    <p className="text-xs text-gray-500 font-medium uppercase tracking-wide mb-2">Country Intel</p>
                    <div className="flex flex-wrap gap-2">
                      {b.country_intel.slice(0, 5).map((c, i) => {
                        const tierColor = c.tier === "Hot" ? "bg-red-100 text-red-700" : c.tier === "Warm" ? "bg-amber-100 text-amber-700" : "bg-gray-100 text-gray-600";
                        return (
                          <div key={i} className={`${tierColor} rounded-lg px-3 py-1.5 text-xs font-medium`}>
                            {c.country} &middot; {c.tier} ({c.score})
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            </div>
          ))}

          <p className="text-xs text-gray-400 text-center">Generated {new Date(data.generated_at).toLocaleString()}</p>
        </>
      )}

      {/* Email Tab */}
      {tab === "email" && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          {/* Left: Email Preview (2 cols) */}
          <div className="lg:col-span-2 bg-white rounded-xl border border-gray-200 overflow-hidden">
            <div className="px-5 py-3 border-b border-gray-100 flex items-center justify-between">
              <h2 className="font-semibold text-gray-800 text-sm">Email Preview</h2>
              <button
                onClick={() => setPreviewKey(k => k + 1)}
                className="text-xs text-indigo-600 hover:text-indigo-700 font-medium"
              >
                Reload
              </button>
            </div>
            <div className="bg-gray-50 p-4" style={{ minHeight: "600px" }}>
              <iframe
                key={previewKey}
                ref={iframeRef}
                src={`/api/report/preview?_t=${previewKey}`}
                className="w-full bg-white rounded-lg shadow-sm border border-gray-200"
                style={{ height: "800px" }}
                title="Email Preview"
              />
            </div>
          </div>

          {/* Right: Send & Schedule (1 col) */}
          <div className="space-y-4">
            {/* Send Email to Members */}
            <div className="bg-white rounded-xl border border-gray-200 p-5">
              <div className="flex items-center justify-between mb-3">
                <h3 className="font-semibold text-gray-800 text-sm">Send Report</h3>
                <div className="flex gap-2 text-xs">
                  <button
                    onClick={selectAllMembers}
                    className="text-indigo-600 hover:text-indigo-700 font-medium"
                  >
                    Select all
                  </button>
                  <span className="text-gray-300">·</span>
                  <button
                    onClick={clearMembers}
                    className="text-gray-500 hover:text-gray-700 font-medium"
                  >
                    Clear
                  </button>
                </div>
              </div>

              {/* Member list */}
              <div className="border border-gray-200 rounded-lg max-h-60 overflow-y-auto mb-3">
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
                          <p className="text-xs font-medium text-gray-800 truncate">
                            {m.name || m.email}
                          </p>
                          <p className="text-[11px] text-gray-500 truncate">
                            {m.email} · {m.role}{!m.is_active && " · inactive"}
                          </p>
                        </div>
                      </label>
                    );
                  })
                )}
              </div>

              {/* Free-text override */}
              <div className="mb-3">
                <p className="text-[11px] text-gray-500 mb-1">
                  Also send to (optional, comma-separated):
                </p>
                <input
                  type="text"
                  value={testEmail}
                  onChange={e => setTestEmail(e.target.value)}
                  placeholder="extra@example.com"
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                  onKeyDown={e => e.key === "Enter" && sendTestEmail()}
                />
              </div>

              <button
                onClick={sendTestEmail}
                disabled={sending || (selectedMemberIds.length === 0 && !testEmail.trim())}
                className="w-full px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
              >
                {sending ? (
                  <>
                    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
                    Sending...
                  </>
                ) : (
                  <>Send to {selectedMemberIds.length + (testEmail.trim() ? testEmail.split(",").filter(x => x.trim()).length : 0)} recipient(s)</>
                )}
              </button>
            </div>

            {/* Schedule */}
            <div className="bg-white rounded-xl border border-gray-200 p-5">
              <h3 className="font-semibold text-gray-800 text-sm mb-4">Email Schedule</h3>

              {scheduleLoading ? (
                <div className="text-sm text-gray-400 animate-pulse">Loading schedule...</div>
              ) : schedule ? (
                <div className="space-y-4">
                  {/* Enable toggle */}
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-gray-700">Automatic sending</span>
                    <button
                      onClick={() => setSchedule({ ...schedule, enabled: !schedule.enabled })}
                      className={`relative w-11 h-6 rounded-full transition-colors ${schedule.enabled ? "bg-indigo-600" : "bg-gray-300"}`}
                    >
                      <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${schedule.enabled ? "translate-x-5" : ""}`} />
                    </button>
                  </div>

                  {/* Day of week */}
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

                  {/* Time */}
                  <div>
                    <p className="text-xs text-gray-500 mb-2">Time (ICT)</p>
                    <div className="flex gap-2">
                      <select
                        value={schedule.hour}
                        onChange={e => setSchedule({ ...schedule, hour: parseInt(e.target.value) })}
                        className="flex-1 px-3 py-2 border border-gray-200 rounded-lg text-sm bg-white"
                      >
                        {HOURS.map(h => (
                          <option key={h} value={h}>{String(h).padStart(2, "0")}:00</option>
                        ))}
                      </select>
                      <select
                        value={schedule.minute}
                        onChange={e => setSchedule({ ...schedule, minute: parseInt(e.target.value) })}
                        className="w-20 px-3 py-2 border border-gray-200 rounded-lg text-sm bg-white"
                      >
                        {[0, 15, 30, 45].map(m => (
                          <option key={m} value={m}>:{String(m).padStart(2, "0")}</option>
                        ))}
                      </select>
                    </div>
                  </div>

                  {/* Recipients */}
                  <div>
                    <p className="text-xs text-gray-500 mb-2">Recipients</p>
                    <div className="space-y-1.5 mb-2 max-h-40 overflow-y-auto">
                      {schedule.recipients.map(email => (
                        <div key={email} className="flex items-center justify-between bg-gray-50 rounded-lg px-3 py-1.5">
                          <span className="text-xs text-gray-700 truncate">{email}</span>
                          <button
                            onClick={() => removeRecipient(email)}
                            className="text-gray-400 hover:text-red-500 text-sm ml-2 flex-shrink-0"
                          >
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
                      <button
                        onClick={addRecipient}
                        className="px-3 py-1.5 bg-gray-100 text-gray-600 text-xs rounded-lg hover:bg-gray-200 font-medium"
                      >
                        Add
                      </button>
                    </div>
                  </div>

                  {/* Next run indicator */}
                  {schedule.next_run && (
                    <div className="bg-indigo-50 rounded-lg p-3">
                      <p className="text-xs text-indigo-600 font-medium">Next scheduled send</p>
                      <p className="text-sm text-indigo-800 font-semibold mt-0.5">
                        {new Date(schedule.next_run).toLocaleString()}
                      </p>
                    </div>
                  )}

                  {/* Save button */}
                  <button
                    onClick={saveSchedule}
                    disabled={savingSchedule}
                    className="w-full px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50 flex items-center justify-center gap-2"
                  >
                    {savingSchedule ? (
                      <>
                        <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
                        Saving...
                      </>
                    ) : "Save Schedule"}
                  </button>
                </div>
              ) : (
                <div className="text-sm text-red-400">Failed to load schedule</div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Toast notification */}
      {toast && <Toast message={toast.message} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
