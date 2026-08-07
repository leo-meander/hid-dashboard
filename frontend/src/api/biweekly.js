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

export const getNotes = (period, branchId) =>
  axios
    .get(`${BASE}/comments`, {
      params: { period, branch_id: branchId || undefined },
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
