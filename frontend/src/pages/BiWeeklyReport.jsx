/**
 * Bi-Weekly Branch Manager Report.
 *
 * A period is two ISO weeks of the year (Week 29–30, Week 31–32, …). The
 * backend renders the whole report as inline-styled HTML — the same markup
 * that will be emailed once delivery is wired — and this page slices the
 * per-branch blocks out of it on the `.hid-bw-branch` anchor so switching
 * branches costs nothing.
 *
 * Manager's Notes reuse the Weekly Report's comment table, tagged
 * report_type='biweekly' and scoped to (period, branch).
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import {
  getPeriods,
  getPreviewHtml,
  getNotes,
  createNote,
  deleteNote,
} from "../api/biweekly";
import { useAuth } from "../context/AuthContext";

const GENERAL_KEY = "bw._general";

/** Slice the rendered report into a header plus one block per branch. */
function parseBiweeklyHtml(htmlText) {
  const doc = new DOMParser().parseFromString(htmlText, "text/html");
  const headerEl = doc.querySelector("#bw-header");
  const branches = Array.from(doc.querySelectorAll(".hid-bw-branch")).map(el => ({
    id: el.dataset.branchId,
    name: el.dataset.branchName || "Branch",
    html: el.innerHTML,
  }));
  return { headerHtml: headerEl ? headerEl.outerHTML : "", branches };
}

function ErrorBox({ title, detail, onRetry }) {
  return (
    <div className="bg-red-50 border border-red-200 rounded-xl p-6 text-center">
      <p className="text-red-800 font-semibold text-sm">{title}</p>
      {detail && <p className="text-red-600 text-xs mt-1">{detail}</p>}
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-3 px-3 py-1.5 bg-red-600 text-white text-sm rounded-lg hover:bg-red-700"
        >
          Try again
        </button>
      )}
    </div>
  );
}

/** Manager's notes for one (period, branch). */
function ManagerNotes({ period, branchId, branchName }) {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const key = ["biweekly-notes", period, branchId];
  const { data: notes = [], isPending } = useQuery({
    queryKey: key,
    queryFn: () => getNotes(period, branchId),
    enabled: Boolean(period && branchId),
    placeholderData: keepPreviousData,
  });

  async function submit() {
    const body = draft.trim();
    if (!body) return;
    setBusy(true);
    setError(null);
    try {
      await createNote({ period, branch_id: branchId, body, metric_key: GENERAL_KEY });
      setDraft("");
      queryClient.invalidateQueries({ queryKey: key });
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Could not save the note");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id) {
    try {
      await deleteNote(id);
      queryClient.invalidateQueries({ queryKey: key });
    } catch (e) {
      setError(e?.response?.data?.detail || e.message || "Could not delete the note");
    }
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 mt-6">
      <h3 className="font-semibold text-gray-800 text-sm">
        📝 Branch Manager's Notes — {branchName}
      </h3>
      <p className="text-[11px] text-gray-500 mt-0.5 mb-3">
        Operational context the data can't show — renovations, local events, rate
        changes, group bookings. Saved against {period} and visible to the team.
      </p>

      {isPending ? (
        <p className="text-xs text-gray-400">Loading notes…</p>
      ) : notes.length === 0 ? (
        <p className="text-xs text-gray-400 italic">No notes for this period yet.</p>
      ) : (
        <ul className="space-y-2 mb-3">
          {notes.map(n => (
            <li key={n.id} className="bg-gray-50 border border-gray-100 rounded-lg p-3">
              <div className="flex justify-between items-start gap-3">
                <p className="text-sm text-gray-800 whitespace-pre-wrap flex-1">{n.body}</p>
                {(n.author_id === user?.id || user?.role === "admin") && (
                  <button
                    onClick={() => remove(n.id)}
                    className="text-[11px] text-gray-400 hover:text-red-600 shrink-0"
                    title="Delete this note"
                  >
                    Delete
                  </button>
                )}
              </div>
              <p className="text-[10px] text-gray-400 mt-1">
                {n.author_name || "Unknown"}
                {n.created_at ? ` · ${new Date(n.created_at).toLocaleString()}` : ""}
              </p>
            </li>
          ))}
        </ul>
      )}

      <textarea
        value={draft}
        onChange={e => setDraft(e.target.value)}
        rows={3}
        placeholder="Add a note for this period…"
        className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-teal-500"
      />
      {error && <p className="text-xs text-red-600 mt-1">{error}</p>}
      <div className="flex justify-end mt-2">
        <button
          onClick={submit}
          disabled={busy || !draft.trim()}
          className="px-4 py-1.5 bg-teal-700 text-white text-sm rounded-lg hover:bg-teal-800 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save note"}
        </button>
      </div>
    </div>
  );
}

export default function BiWeeklyReport() {
  const queryClient = useQueryClient();
  const [selectedPeriod, setSelectedPeriod] = useState("");
  const [selectedBranch, setSelectedBranch] = useState(null);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildError, setRebuildError] = useState(null);

  const { data: periods = [], isPending: periodsLoading, error: periodsError } =
    useQuery({
      queryKey: ["biweekly-periods"],
      queryFn: () => getPeriods({ back: 13 }),
      placeholderData: keepPreviousData,
    });

  // Default to the newest completed period once the list arrives.
  useEffect(() => {
    if (!selectedPeriod && periods.length) setSelectedPeriod(periods[0].key);
  }, [periods, selectedPeriod]);

  const reportQuery = useQuery({
    queryKey: ["biweekly-preview", selectedPeriod],
    queryFn: async () => parseBiweeklyHtml(await getPreviewHtml(selectedPeriod)),
    enabled: Boolean(selectedPeriod),
    placeholderData: keepPreviousData,
  });

  const branches = reportQuery.data?.branches || [];

  // Keep the chosen branch across period switches when it still exists —
  // a manager reviewing one branch shouldn't be bounced back to the first
  // tab every time they step back a period.
  useEffect(() => {
    if (!branches.length) return;
    if (!branches.some(b => b.id === selectedBranch)) setSelectedBranch(branches[0].id);
  }, [branches, selectedBranch]);

  /**
   * Rebuild the period server-side, don't just refetch it.
   *
   * A plain `invalidateQueries` re-requests /preview, which is served from
   * `biweekly_report_cache` — so the page would redraw the exact same numbers
   * and look like Refresh did nothing. `fresh=1` is what recomputes the
   * snapshot, which is the whole point after upstream data is backfilled.
   */
  async function rebuild() {
    if (!selectedPeriod) return;
    setRebuilding(true);
    setRebuildError(null);
    try {
      const html = await getPreviewHtml(selectedPeriod, { fresh: true });
      queryClient.setQueryData(
        ["biweekly-preview", selectedPeriod],
        parseBiweeklyHtml(html)
      );
    } catch (e) {
      setRebuildError(e?.message || "Could not rebuild this period");
    } finally {
      setRebuilding(false);
    }
  }

  const active = useMemo(
    () => branches.find(b => b.id === selectedBranch) || branches[0] || null,
    [branches, selectedBranch]
  );
  const period = periods.find(p => p.key === selectedPeriod);

  return (
    <div className="space-y-4">
      <style>{`
        .hid-bw-body { background: transparent; }
        .hid-bw-body a { color: #016b67; }
      `}</style>

      <div className="bg-white rounded-xl border border-gray-200 p-4 flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="font-semibold text-gray-800 text-sm">
            🗓 Bi-Weekly Branch Manager Report
          </h2>
          <p className="text-[11px] text-gray-500 mt-0.5">
            Two ISO weeks per period, compared against the same weeks last year.
            {period && (
              <span> Showing <b>{period.label}</b> · {period.date_label} ({period.days} days).</span>
            )}
          </p>
        </div>
        <div className="flex gap-2 items-center">
          <select
            value={selectedPeriod}
            onChange={e => setSelectedPeriod(e.target.value)}
            disabled={periodsLoading}
            className="px-3 py-1.5 border border-gray-200 text-sm rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-teal-500"
            title="Choose a reporting period"
          >
            {periods.map(p => (
              <option key={p.key} value={p.key}>
                {p.label} · {p.date_label}
                {p.is_extended ? " (21d)" : ""}
              </option>
            ))}
          </select>
          <a
            href={`/api/biweekly/preview?period=${selectedPeriod}`}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 border border-gray-200 text-gray-600 text-sm rounded-lg hover:bg-gray-50"
          >
            Open raw preview ↗
          </a>
          <button
            onClick={rebuild}
            disabled={rebuilding || reportQuery.isFetching || !selectedPeriod}
            title="Recompute this period from the latest data"
            className="px-3 py-1.5 bg-teal-700 text-white text-sm rounded-lg hover:bg-teal-800 disabled:opacity-50"
          >
            {rebuilding
              ? "Rebuilding…"
              : reportQuery.isFetching
                ? "Loading…"
                : "Rebuild"}
          </button>
        </div>
      </div>

      {periodsError && (
        <ErrorBox
          title="Could not load the period list"
          detail={periodsError.message}
          onRetry={() => queryClient.invalidateQueries({ queryKey: ["biweekly-periods"] })}
        />
      )}

      {rebuildError && (
        <ErrorBox
          title="Could not rebuild this period"
          detail={rebuildError}
          onRetry={rebuild}
        />
      )}

      {reportQuery.isError && (
        <ErrorBox
          title="Could not load the report"
          detail={reportQuery.error?.message}
          onRetry={() =>
            queryClient.invalidateQueries({
              queryKey: ["biweekly-preview", selectedPeriod],
            })
          }
        />
      )}

      {reportQuery.isPending && !reportQuery.data && (
        <div className="bg-white rounded-xl border border-gray-200 p-10 text-center">
          <p className="text-sm text-gray-500">
            Building the report for this period…
          </p>
          <p className="text-[11px] text-gray-400 mt-1">
            The first load of a period computes it; later loads are served from cache.
          </p>
        </div>
      )}

      {branches.length > 0 && (
        <>
          <div className="flex gap-1.5 flex-wrap">
            {branches.map(b => (
              <button
                key={b.id}
                onClick={() => setSelectedBranch(b.id)}
                className={
                  "px-3.5 py-1.5 text-sm rounded-lg border transition " +
                  (b.id === active?.id
                    ? "bg-teal-700 text-white border-teal-700"
                    : "bg-white text-gray-700 border-gray-200 hover:bg-gray-50")
                }
              >
                {b.name}
              </button>
            ))}
          </div>

          {reportQuery.data?.headerHtml && (
            <div
              className="hid-bw-body rounded-xl overflow-hidden"
              dangerouslySetInnerHTML={{ __html: reportQuery.data.headerHtml }}
            />
          )}

          {active && (
            <>
              <div
                className="hid-bw-body bg-[#FBF7F4] rounded-xl border border-gray-200 px-6 py-4"
                dangerouslySetInnerHTML={{ __html: active.html }}
              />
              <ManagerNotes
                period={selectedPeriod}
                branchId={active.id}
                branchName={active.name}
              />
            </>
          )}
        </>
      )}

      {!reportQuery.isPending && !reportQuery.isError && branches.length === 0 && (
        <div className="bg-white rounded-xl border border-gray-200 p-10 text-center">
          <p className="text-sm text-gray-500">
            No active branches returned for this period.
          </p>
        </div>
      )}
    </div>
  );
}
