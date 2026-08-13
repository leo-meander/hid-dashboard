/**
 * Bi-Weekly Branch Manager Report.
 *
 * A period is two ISO weeks of the year (Week 29–30, Week 31–32, …). The
 * backend renders the whole report as inline-styled HTML — the same markup
 * that will be emailed once delivery is wired — and this page slices the
 * per-branch blocks out of it on the `.hid-bw-branch` anchor so switching
 * branches costs nothing.
 *
 * Two ways to leave a comment, both on the Weekly Report's comment table
 * (tagged report_type='biweekly'):
 *   - Click any card/row in the rendered report — MetricCommentDrawer opens
 *     a thread scoped to that exact (period, branch, metric_key), same UX
 *     the Weekly Report already has.
 *   - The three note boards below the report are threads NOT tied to one
 *     metric (bw._general / bw._growth / bw._support) — running logs rather
 *     than a discussion under one number.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import {
  getPeriods,
  getPreviewHtml,
  getNotes,
  createNote,
  updateNote,
  deleteNote,
  getFlagOverrides,
  putFlagOverride,
  deleteFlagOverride,
} from "../api/biweekly";
import { useAuth } from "../context/AuthContext";
import { useBranch } from "../context/BranchContext";

/**
 * The three note boards under each branch.
 *
 * All three are rows in the same comments table, separated only by
 * `metric_key` — no schema change was needed to add the second and third.
 * `bw._general` is the original board, so its key must not be renamed:
 * notes already written by managers are stored under it.
 *
 * `resolvable` turns on the Done toggle. Only the support board has it: an
 * ask of the branch team is the one kind of note that has a finished state,
 * and it maps onto the `is_resolved` column the weekly report already uses.
 */
const NOTE_BOARDS = [
  {
    key: "bw._general",
    icon: "📝",
    title: "Branch Manager's Notes",
    hint: "Operational context the data can't show — renovations, local events, rate changes, group bookings.",
    placeholder: "e.g. Lift out of service Jul 18–22, 8 rooms blocked.",
    resolvable: false,
  },
  {
    key: "bw._growth",
    icon: "📈",
    title: "Growth Team — What We Did & How It Went",
    hint: "Campaigns, tests and changes the Growth team ran this period, and the result each one produced.",
    placeholder:
      "e.g. Raised Meta budget on Couple PH by 30% from Jul 15 — bookings +18%, ROAS held at 4.2×.",
    resolvable: false,
  },
  {
    key: "bw._support",
    icon: "🙋",
    title: "Support Needed From The Branch",
    hint: "What Growth needs the branch team to do. Mark Done once it's handled so the next period starts clean.",
    placeholder:
      "e.g. Need 6 fresh photos of the renovated dorm by Aug 20 for the new ad set.",
    resolvable: true,
  },
];

/**
 * The two "add your own" boards portalled into the Highlights & Watch-outs
 * section (see FlagsEditor below) — separate from NOTE_BOARDS because these
 * render inside the report body itself, not in the running-log list under it.
 */
const FLAG_BOARDS = [
  {
    key: "bw._highlight",
    icon: "▲",
    title: "Add a highlight",
    hint: "The list above is rule-driven from the numbers — add what it can't see.",
    placeholder: "e.g. Signed a new corporate rate deal with ABC Corp.",
    resolvable: false,
    accent: "good",
  },
  {
    key: "bw._watchout",
    icon: "!",
    title: "Add a watch-out",
    hint: "The list above is rule-driven from the numbers — add what it can't see.",
    placeholder: "e.g. Front desk short-staffed through August.",
    resolvable: false,
    accent: "warn",
  },
];

/** Slice the rendered report into a header plus one block per branch. */
function parseBiweeklyHtml(htmlText) {
  const doc = new DOMParser().parseFromString(htmlText, "text/html");
  const headerEl = doc.querySelector("#bw-header");
  const branches = Array.from(doc.querySelectorAll(".hid-bw-branch")).map(el => ({
    id: el.dataset.branchId,
    name: el.dataset.branchName || "Branch",
    // The backend owns the brand palette, so the tab reads its colour off the
    // markup instead of keeping a second copy of the hex codes here that
    // could drift out of sync with the report itself.
    color: el.dataset.branchColor || "#028782",
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

/** One note board for a (period, branch, metric_key). */
function NoteBoard({ board, period, branchId }) {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // metric_key is part of the cache key, or the three boards on screen would
  // share one entry and overwrite each other's contents.
  const key = ["biweekly-notes", period, branchId, board.key];
  const { data: notes = [], isPending } = useQuery({
    queryKey: key,
    queryFn: () => getNotes(period, branchId, board.key),
    enabled: Boolean(period && branchId),
    placeholderData: keepPreviousData,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: key });

  function fail(e, fallback) {
    setError(e?.response?.data?.detail || e?.message || fallback);
  }

  async function submit() {
    const body = draft.trim();
    if (!body) return;
    setBusy(true);
    setError(null);
    try {
      await createNote({ period, branch_id: branchId, body, metric_key: board.key });
      setDraft("");
      refresh();
    } catch (e) {
      fail(e, "Could not save the note");
    } finally {
      setBusy(false);
    }
  }

  async function toggleDone(note) {
    setError(null);
    try {
      await updateNote(note.id, { is_resolved: !note.is_resolved });
      refresh();
    } catch (e) {
      fail(e, "Could not update the note");
    }
  }

  async function remove(id) {
    setError(null);
    try {
      await deleteNote(id);
      refresh();
    } catch (e) {
      fail(e, "Could not delete the note");
    }
  }

  const openCount = board.resolvable
    ? notes.filter(n => !n.is_resolved).length
    : 0;

  // `accent` visually ties this board to the auto-generated panel it
  // extends (green = Highlights, amber = Watch-outs) — the three original
  // boards pass none and stay neutral gray.
  const accentBorder = board.accent === "good" ? "border-l-4 border-l-green-400"
    : board.accent === "warn" ? "border-l-4 border-l-amber-400"
    : "";

  return (
    <div className={`bg-white rounded-xl border border-gray-200 p-5 ${accentBorder}`}>
      <div className="flex items-start justify-between gap-3">
        <h3 className="font-semibold text-gray-800 text-sm">
          {board.icon} {board.title}
        </h3>
        {board.resolvable && notes.length > 0 && (
          <span
            className={
              "text-[11px] px-2 py-0.5 rounded-full shrink-0 " +
              (openCount > 0
                ? "bg-amber-100 text-amber-800"
                : "bg-green-100 text-green-800")
            }
          >
            {openCount > 0 ? `${openCount} open` : "All done"}
          </span>
        )}
      </div>
      <p className="text-[11px] text-gray-500 mt-0.5 mb-3">{board.hint}</p>

      {isPending ? (
        <p className="text-xs text-gray-400">Loading…</p>
      ) : notes.length === 0 ? (
        <p className="text-xs text-gray-400 italic">Nothing noted for this period yet.</p>
      ) : (
        <ul className="space-y-2 mb-3">
          {notes.map(n => (
            <li
              key={n.id}
              className={
                "border rounded-lg p-3 " +
                (n.is_resolved
                  ? "bg-green-50/60 border-green-100"
                  : "bg-gray-50 border-gray-100")
              }
            >
              <div className="flex justify-between items-start gap-3">
                <p
                  className={
                    "text-sm whitespace-pre-wrap flex-1 " +
                    (n.is_resolved ? "text-gray-500 line-through" : "text-gray-800")
                  }
                >
                  {n.body}
                </p>
                <div className="flex gap-2 shrink-0">
                  {board.resolvable && (
                    <button
                      onClick={() => toggleDone(n)}
                      className="text-[11px] text-gray-400 hover:text-green-700"
                      title={n.is_resolved ? "Reopen this request" : "Mark as handled"}
                    >
                      {n.is_resolved ? "Reopen" : "Done"}
                    </button>
                  )}
                  {(n.author_id === user?.id || user?.role === "admin") && (
                    <button
                      onClick={() => remove(n.id)}
                      className="text-[11px] text-gray-400 hover:text-red-600"
                      title="Delete this note"
                    >
                      Delete
                    </button>
                  )}
                </div>
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
        placeholder={board.placeholder}
        className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-teal-500"
      />
      {error && <p className="text-xs text-red-600 mt-1">{error}</p>}
      <div className="flex justify-end mt-2">
        <button
          onClick={submit}
          disabled={busy || !draft.trim()}
          className="px-4 py-1.5 bg-teal-700 text-white text-sm rounded-lg hover:bg-teal-800 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}

/**
 * Portalled into `#bw-flags-anchor-{branchId}`, a marker div the backend
 * renders right inside the Highlights & Watch-outs section (see
 * `_render_flags` in biweekly_render.py) — so "add your own" reads as part
 * of that section instead of a generic board further down the page.
 */
function FlagsEditor({ period, branchId }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3.5 mt-3.5">
      {FLAG_BOARDS.map(board => (
        <NoteBoard key={board.key} board={board} period={period} branchId={branchId} />
      ))}
    </div>
  );
}

/**
 * Correct or hide one auto-generated Highlights / Watch-outs / Action line.
 *
 * Opens when a manager clicks a [data-flag-key] line in the rendered report.
 * The rules that write those lines are right most of the time and wrong some
 * of the time, and a wrong line in a report a branch manager reads is worse
 * than no line.
 *
 * Two things worth knowing about the semantics, both deliberate:
 *   - A correction is stored against the RULE key, so it survives the rebuild
 *     that rewrites the sentence with new numbers. It is then shown exactly as
 *     typed and never recomputed — which is why the report marks it "edited".
 *   - Editing is plain text. The generated lines carry <b> emphasis; a
 *     correction is one sentence in the operator's own words, and a textarea
 *     full of markup is a worse trade than losing the bold.
 */
function FlagEditDrawer({ context, canEdit, isOverridden, onClose, onSaved }) {
  const [draft, setDraft] = useState(context.text || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function run(fn) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onSaved();
      onClose();
    } catch (e) {
      setError(e?.response?.data?.detail || e?.message || "Could not save");
    } finally {
      setBusy(false);
    }
  }

  const save = () => {
    const body = draft.trim();
    if (!body) {
      setError("Write the corrected line, or use Hide to drop it.");
      return;
    }
    return run(() => putFlagOverride({
      period: context.period, branch_id: context.branchId,
      flag_key: context.flagKey, body,
    }));
  };
  const hide = () => run(() => putFlagOverride({
    period: context.period, branch_id: context.branchId,
    flag_key: context.flagKey, is_hidden: true,
  }));
  const revert = () => run(() =>
    deleteFlagOverride(context.period, context.branchId, context.flagKey));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4"
         onClick={onClose}>
      <div className="bg-white rounded-xl shadow-xl w-full max-w-xl p-5"
           onClick={e => e.stopPropagation()}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="font-semibold text-gray-900">Edit this line</h3>
            <p className="text-xs text-gray-500 mt-0.5">
              {context.branchName} · {context.periodLabel}
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-lg leading-none">×</button>
        </div>

        {!canEdit ? (
          <p className="text-sm text-gray-600 mt-4">
            You have view-only access — ask an editor or admin to correct this line.
          </p>
        ) : (
          <>
            <textarea
              value={draft}
              onChange={e => setDraft(e.target.value)}
              rows={4}
              className="mt-4 w-full border border-gray-200 rounded-lg p-2.5 text-sm focus:outline-none focus:border-gray-400"
              placeholder="The corrected line, in your own words."
            />
            <p className="text-[11px] text-gray-400 mt-1">
              Saved as plain text and shown exactly as typed — it is marked
              “edited” and never recomputed, so it will not follow the numbers
              if the period is rebuilt.
            </p>
            {error && <p className="text-xs text-red-600 mt-2">{error}</p>}
            <div className="flex items-center gap-2 mt-4">
              <button onClick={save} disabled={busy}
                      className="px-3 py-1.5 bg-gray-800 text-white text-sm rounded-lg disabled:opacity-50">
                {busy ? "Saving…" : "Save"}
              </button>
              <button onClick={hide} disabled={busy}
                      className="px-3 py-1.5 border border-gray-200 text-gray-600 text-sm rounded-lg disabled:opacity-50">
                Hide this line
              </button>
              {isOverridden && (
                <button onClick={revert} disabled={busy}
                        className="px-3 py-1.5 border border-gray-200 text-gray-600 text-sm rounded-lg ml-auto disabled:opacity-50">
                  Revert to generated
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/**
 * Discussion thread for one clicked metric cell — opens when a manager
 * clicks any [data-metric-key] card/row in the rendered report body.
 *
 * Mirrors the Weekly Report's CommentDrawer (same table, same report_type
 * discriminator, same is_action_item/is_resolved columns) but doesn't need
 * a static metric_key → label map the way that one does: every card the
 * bi-weekly renderer emits already carries data-metric-label (see
 * `report_common.cell_attrs`), so the label travels with the click.
 */
function MetricCommentDrawer({ context, currentUser, onClose, onChanged }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [markAction, setMarkAction] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [editingText, setEditingText] = useState("");

  const key = ["biweekly-notes", context.period, context.branchId, context.metricKey];
  const { data: comments = [], isPending } = useQuery({
    queryKey: key,
    queryFn: () => getNotes(context.period, context.branchId, context.metricKey),
    placeholderData: keepPreviousData,
  });

  function fail(e, fallback) {
    setError(e?.response?.data?.detail || e?.message || fallback);
  }
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: key });
    onChanged?.();
  };

  async function submit() {
    const body = draft.trim();
    if (!body) return;
    setBusy(true);
    setError(null);
    try {
      const posted = await createNote({
        period: context.period,
        branch_id: context.branchId,
        body,
        metric_key: context.metricKey,
      });
      if (markAction) await updateNote(posted.id, { is_action_item: true });
      setDraft("");
      setMarkAction(false);
      refresh();
    } catch (e) {
      fail(e, "Could not post the comment");
    } finally {
      setBusy(false);
    }
  }

  async function toggleAction(c) {
    setError(null);
    try {
      await updateNote(c.id, { is_action_item: !c.is_action_item });
      refresh();
    } catch (e) {
      fail(e, "Could not update the comment");
    }
  }

  async function toggleResolved(c) {
    setError(null);
    try {
      await updateNote(c.id, { is_resolved: !c.is_resolved });
      refresh();
    } catch (e) {
      fail(e, "Could not update the comment");
    }
  }

  async function saveEdit(c) {
    const text = editingText.trim();
    if (!text) return;
    setError(null);
    try {
      await updateNote(c.id, { body: text });
      setEditingId(null);
      setEditingText("");
      refresh();
    } catch (e) {
      fail(e, "Could not save the edit");
    }
  }

  async function remove(c) {
    if (!confirm("Delete this comment? This cannot be undone.")) return;
    setError(null);
    try {
      await deleteNote(c.id);
      refresh();
    } catch (e) {
      fail(e, "Could not delete the comment");
    }
  }

  const canEdit = (c) => currentUser && (c.author_id === currentUser.id || currentUser.role === "admin");

  return (
    <>
      <div className="fixed inset-0 bg-black/30 z-40" onClick={onClose} />
      <div className="fixed top-0 right-0 bottom-0 w-full sm:w-[440px] bg-white shadow-2xl z-50 flex flex-col">
        <div className="px-5 py-4 border-b border-gray-200 flex items-start justify-between">
          <div className="min-w-0">
            <p className="text-[11px] text-gray-500 uppercase tracking-wide">Discussion</p>
            <h3 className="text-base font-semibold text-gray-900 truncate">{context.metricLabel}</h3>
            <p className="text-xs text-gray-500 mt-0.5 truncate">
              {context.branchName ? `${context.branchName} · ` : ""}{context.periodLabel}
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

        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
          {isPending ? (
            <p className="text-sm text-gray-400">Loading…</p>
          ) : comments.length === 0 ? (
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
                      {c.author_name || "Unknown"}
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
                      className="w-full text-sm px-2 py-1.5 border border-gray-200 rounded focus:outline-none focus:ring-2 focus:ring-teal-500"
                    />
                    <div className="flex gap-2 mt-2 text-xs">
                      <button onClick={() => saveEdit(c)} className="px-2 py-1 bg-teal-700 text-white rounded hover:bg-teal-800">Save</button>
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
                  <button onClick={() => toggleResolved(c)} className="text-gray-500 hover:text-green-700">
                    {c.is_resolved ? "Reopen" : "Resolve"}
                  </button>
                  {canEdit(c) && editingId !== c.id && (
                    <>
                      <span className="text-gray-300">·</span>
                      <button
                        onClick={() => { setEditingId(c.id); setEditingText(c.body); }}
                        className="text-gray-500 hover:text-teal-700"
                      >
                        Edit
                      </button>
                      <span className="text-gray-300">·</span>
                      <button onClick={() => remove(c)} className="text-gray-500 hover:text-red-600">Delete</button>
                    </>
                  )}
                </div>
              </div>
            ))
          )}
        </div>

        <div className="border-t border-gray-200 px-5 py-3 bg-gray-50">
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder="Add a comment or question…"
            rows={3}
            className="w-full text-sm px-3 py-2 border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-teal-500 bg-white"
          />
          {error && <p className="text-xs text-red-600 mt-1">{error}</p>}
          <div className="flex items-center justify-between mt-2">
            <label className="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
              <input
                type="checkbox"
                checked={markAction}
                onChange={e => setMarkAction(e.target.checked)}
                className="h-3.5 w-3.5 text-teal-600 rounded"
              />
              Mark as action item
            </label>
            <button
              onClick={submit}
              disabled={busy || !draft.trim()}
              className="px-3 py-1.5 bg-teal-700 text-white text-sm rounded-lg hover:bg-teal-800 disabled:opacity-50"
            >
              {busy ? "Posting…" : "Post"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

/** All three note boards for one (period, branch). */
function BranchNotes({ period, branchId, branchName }) {
  return (
    <div className="mt-6 space-y-4">
      <p className="text-[11px] text-gray-500">
        Notes below are saved against <b>{branchName}</b> for <b>{period}</b> and are
        visible to the whole team.
      </p>
      {NOTE_BOARDS.map(board => (
        <NoteBoard
          key={board.key}
          board={board}
          period={period}
          branchId={branchId}
        />
      ))}
    </div>
  );
}

export default function BiWeeklyReport() {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  // Branch selection is the app-wide one (top nav, persisted in localStorage),
  // the same hook TeamKPI uses — so picking a branch anywhere moves both this
  // page's tabs and the nav, instead of the page keeping a private second
  // answer to "which branch am I looking at". Costs nothing: every branch is
  // already in the one report response, so switching never refetches.
  const { selected: selectedBranch, selectBranch } = useBranch();
  const [selectedPeriod, setSelectedPeriod] = useState("");
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildError, setRebuildError] = useState(null);
  const [drawer, setDrawer] = useState(null);
  const [flagEdit, setFlagEdit] = useState(null);
  const [flagsAnchor, setFlagsAnchor] = useState(null);
  const reportBodyRef = useRef(null);

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
    // `raw` is kept alongside the parsed pieces so "Open raw preview" can hand
    // the browser the exact same HTML without a second request — see
    // `openRawPreview`.
    queryFn: async () => {
      const raw = await getPreviewHtml(selectedPeriod);
      return { raw, ...parseBiweeklyHtml(raw) };
    },
    enabled: Boolean(selectedPeriod),
    placeholderData: keepPreviousData,
  });

  const branches = reportQuery.data?.branches || [];

  /**
   * Open the unsliced report in a new tab.
   *
   * This was an `<a href="/api/biweekly/preview?period=…" target="_blank">`
   * until that endpoint started requiring a login — a plain link cannot carry
   * a Bearer token, so it just rendered a 401. The page already holds the
   * exact HTML that endpoint returns, so hand the browser that instead of
   * asking the server for it a second time.
   */
  function openRawPreview() {
    const raw = reportQuery.data?.raw;
    if (!raw) return;
    const url = URL.createObjectURL(new Blob([raw], { type: "text/html" }));
    window.open(url, "_blank", "noopener");
    // Long enough for the new tab to have loaded it; without this the object
    // URL leaks for the lifetime of the page.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  // No effect snapping the selection to the first branch: `active` already
  // falls back on its own, and writing to the shared selection here would
  // hijack the nav — choosing "All Branches" there would silently flip itself
  // to Taipei the moment this page rendered. A branch chosen on another page
  // still survives a period switch, which is what that effect was for.

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
      // Shape this exactly like the queryFn, `raw` included — "Open raw
      // preview" reads it, so writing only the parsed halves here left that
      // button permanently disabled after a rebuild.
      queryClient.setQueryData(
        ["biweekly-preview", selectedPeriod],
        { raw: html, ...parseBiweeklyHtml(html) }
      );
    } catch (e) {
      setRebuildError(e?.message || "Could not rebuild this period");
    } finally {
      setRebuilding(false);
    }
  }

  // "all" in the nav has no meaning here — the report is one branch at a time
  // by construction — so it falls through to the first branch rather than
  // blanking the page.
  const active = useMemo(
    () => branches.find(b => b.id === selectedBranch) || branches[0] || null,
    [branches, selectedBranch]
  );
  const period = periods.find(p => p.key === selectedPeriod);

  // Every comment for the active (period, branch) — one request covers both
  // the three note boards' history AND the per-metric badge counts below,
  // since branch is already fixed to one tab at a time on this page.
  const allCommentsKey = ["biweekly-all-comments", selectedPeriod, active?.id];
  const { data: allComments = [] } = useQuery({
    queryKey: allCommentsKey,
    queryFn: () => getNotes(selectedPeriod, active.id),
    enabled: Boolean(selectedPeriod && active?.id),
    placeholderData: keepPreviousData,
  });

  // Which flag lines already carry a correction — the rendered HTML shows the
  // corrected text but cannot say whether it came from a rule or a person, and
  // the editor needs that to offer "Revert to generated".
  const flagOverridesKey = ["biweekly-flag-overrides", selectedPeriod, active?.id];
  const { data: flagOverrides = [] } = useQuery({
    queryKey: flagOverridesKey,
    queryFn: () => getFlagOverrides(selectedPeriod, active.id),
    enabled: Boolean(selectedPeriod && active?.id),
    placeholderData: keepPreviousData,
  });
  const overriddenKeys = useMemo(
    () => new Set(flagOverrides.map(o => o.flag_key)),
    [flagOverrides]
  );

  const commentCounts = useMemo(() => {
    const map = {};
    allComments.forEach(c => {
      if (c.is_resolved) return;
      if (!map[c.metric_key]) map[c.metric_key] = { count: 0, actionItems: 0 };
      map[c.metric_key].count += 1;
      if (c.is_action_item) map[c.metric_key].actionItems += 1;
    });
    return map;
  }, [allComments]);

  // Click delegation: resolve any click inside the rendered report body to
  // the closest [data-metric-key] card/row and open its discussion thread.
  // A [data-flag-key] line is checked FIRST and wins — those lines sit inside
  // the flags section, and a click on one means "fix this sentence", not
  // "discuss the metric behind it".
  function onReportClick(e) {
    const flag = e.target.closest("[data-flag-key]");
    if (flag && reportBodyRef.current?.contains(flag)) {
      // Seed the editor from the renderer's [data-flag-text] span — the
      // sentence only, without the bullet glyph in front of it or the "edited"
      // marker after it. Reading the whole <li> swept up the bullet, which then
      // got saved into the text and rendered twice.
      const text = (flag.querySelector("[data-flag-text]")?.innerText ?? "").trim();
      setFlagEdit({
        period: selectedPeriod,
        periodLabel: period ? `${period.label} · ${period.date_label}` : selectedPeriod,
        branchId: active?.id || null,
        branchName: active?.name || null,
        flagKey: flag.dataset.flagKey,
        text,
      });
      return;
    }
    const cell = e.target.closest("[data-metric-key]");
    if (!cell || !reportBodyRef.current?.contains(cell)) return;
    setDrawer({
      period: selectedPeriod,
      periodLabel: period ? `${period.label} · ${period.date_label}` : selectedPeriod,
      branchId: cell.dataset.branchId || active?.id || null,
      branchName: active?.name || null,
      metricKey: cell.dataset.metricKey,
      metricLabel: cell.dataset.metricLabel || cell.dataset.metricKey,
    });
  }

  // Badge every [data-metric-key] cell that has open discussion, so a
  // manager scanning the report can see at a glance where the conversation
  // already is without opening each drawer.
  useEffect(() => {
    const root = reportBodyRef.current;
    if (!root) return;
    root.querySelectorAll(".hid-bw-comment-badge").forEach(el => el.remove());
    root.querySelectorAll("[data-metric-key]").forEach(cell => {
      const info = commentCounts[cell.dataset.metricKey];
      if (!info || !info.count) return;
      const badge = document.createElement("span");
      badge.className = "hid-bw-comment-badge";
      badge.textContent = info.actionItems > 0 ? `⚡${info.count}` : `💬${info.count}`;
      badge.title = info.actionItems > 0
        ? `${info.count} open comment(s), ${info.actionItems} action item(s)`
        : `${info.count} open comment(s)`;
      cell.appendChild(badge);
    });
  }, [active?.html, commentCounts]);

  // Re-locate the "add a highlight/watch-out" marker div every time the
  // report body's innerHTML is replaced (branch switch, rebuild) — the old
  // anchor node is gone the moment `dangerouslySetInnerHTML` re-renders, so
  // the portal target has to be re-found, not just remembered.
  useEffect(() => {
    const root = reportBodyRef.current;
    if (!root || !active?.id) {
      setFlagsAnchor(null);
      return;
    }
    setFlagsAnchor(root.querySelector(`#bw-flags-anchor-${active.id}`) || null);
  }, [active?.html, active?.id]);

  return (
    <div className="space-y-4">
      <style>{`
        .hid-bw-body { background: transparent; }
        .hid-bw-body a { color: #016b67; }
        .hid-bw-body .hid-metric-cell {
          cursor: pointer;
          position: relative;
          transition: background-color 0.15s ease;
        }
        .hid-bw-body .hid-metric-cell:hover {
          background-color: rgba(2, 135, 130, 0.08) !important;
          outline: 1px dashed rgba(2, 135, 130, 0.5);
          outline-offset: -2px;
        }
        .hid-bw-comment-badge {
          display: inline-block;
          margin-left: 6px;
          padding: 1px 6px;
          font-size: 10px;
          font-weight: 600;
          color: #016b67;
          background: #e6f2f1;
          border: 1px solid #b8dad7;
          border-radius: 999px;
          vertical-align: middle;
          line-height: 1.3;
        }
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
          <button
            onClick={openRawPreview}
            disabled={!reportQuery.data?.raw}
            className="px-3 py-1.5 border border-gray-200 text-gray-600 text-sm rounded-lg hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Open raw preview ↗
          </button>
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
            {branches.map(b => {
              const on = b.id === active?.id;
              return (
                <button
                  key={b.id}
                  onClick={() => selectBranch(b.id)}
                  style={
                    on
                      ? { background: b.color, borderColor: b.color, color: "#fff" }
                      : { borderColor: b.color, color: b.color }
                  }
                  className="px-3.5 py-1.5 text-sm rounded-lg border transition bg-white hover:opacity-80 flex items-center gap-2"
                >
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ background: on ? "#fff" : b.color }}
                  />
                  {b.name}
                </button>
              );
            })}
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
                ref={reportBodyRef}
                onClick={onReportClick}
                className="hid-bw-body bg-[#FBF7F4] rounded-xl border border-gray-200 px-6 py-4"
                dangerouslySetInnerHTML={{ __html: active.html }}
              />
              {flagsAnchor && createPortal(
                <FlagsEditor period={selectedPeriod} branchId={active.id} />,
                flagsAnchor
              )}
              <p className="text-[11px] text-gray-400 -mt-2 px-1">
                Click any card or row above to discuss that number.
              </p>
              <BranchNotes
                period={selectedPeriod}
                branchId={active.id}
                branchName={active.name}
              />
            </>
          )}
        </>
      )}

      {flagEdit && (
        <FlagEditDrawer
          context={flagEdit}
          canEdit={["admin", "editor"].includes(user?.role)}
          isOverridden={overriddenKeys.has(flagEdit.flagKey)}
          onClose={() => setFlagEdit(null)}
          onSaved={() => {
            // The override is folded in server-side, so the corrected line
            // only appears once the rendered HTML is refetched.
            queryClient.invalidateQueries({ queryKey: ["biweekly-preview", selectedPeriod] });
            queryClient.invalidateQueries({ queryKey: flagOverridesKey });
          }}
        />
      )}

      {drawer && (
        <MetricCommentDrawer
          context={drawer}
          currentUser={user}
          onClose={() => setDrawer(null)}
          onChanged={() => queryClient.invalidateQueries({ queryKey: allCommentsKey })}
        />
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
