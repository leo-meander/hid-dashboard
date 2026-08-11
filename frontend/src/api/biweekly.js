import axios from "axios";

const BASE = "/api/biweekly";

/** Selectable ISO-week-pair periods, newest first. */
export const getPeriods = (params = {}) =>
  axios.get(`${BASE}/periods`, { params }).then(r => r.data.data);

/**
 * The rendered report HTML.
 *
 * Returned as text rather than JSON on purpose: the backend renders the
 * report so the exact same markup can be emailed later, and the page slices
 * per-branch blocks out of it. Same approach the Weekly Report page uses.
 */
export const getPreviewHtml = async (period, { fresh = false } = {}) => {
  const p = new URLSearchParams();
  if (period) p.set("period", period);
  if (fresh) p.set("fresh", "1");
  const res = await fetch(`${BASE}/preview?${p.toString()}`, {
    credentials: "same-origin",
  });
  if (!res.ok) throw new Error(`Preview failed (${res.status})`);
  return res.text();
};

/**
 * Notes/comments for a (period, branch), optionally narrowed to one metric.
 *
 * Two callers, same endpoint: `metricKey` set is a single thread — one of
 * the three note boards, or a discussion opened by clicking a report cell
 * (see NOTE_BOARDS / MetricCommentDrawer in BiWeeklyReport.jsx). `metricKey`
 * omitted returns every comment for the branch+period at once, which is how
 * the page computes per-cell "has discussion" badge counts in one request
 * instead of one per metric.
 */
export const getNotes = (period, branchId, metricKey) =>
  axios
    .get(`${BASE}/comments`, {
      params: {
        period,
        branch_id: branchId || undefined,
        metric_key: metricKey || undefined,
      },
    })
    .then(r => r.data.data);

export const createNote = ({ period, branch_id, body, metric_key }) =>
  axios
    .post(`${BASE}/comments`, { period, branch_id, body, metric_key })
    .then(r => r.data.data);

export const updateNote = (id, patch) =>
  axios.patch(`${BASE}/comments/${id}`, patch).then(r => r.data.data);

export const deleteNote = (id) =>
  axios.delete(`${BASE}/comments/${id}`).then(r => r.data.data);
