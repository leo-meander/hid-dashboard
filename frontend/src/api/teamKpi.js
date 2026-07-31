import axios from "axios";

const BASE = "/api/team-kpi";

export const getRoles = () =>
  axios.get(`${BASE}/roles`).then(r => r.data.data);

export const getTeamKpiSummary = (role, year, branchId) =>
  axios.get(`${BASE}/summary`, {
    params: { role, year, branch_id: branchId || undefined },
  }).then(r => r.data.data);

export const upsertTarget = (payload) =>
  axios.put(`${BASE}/targets/upsert`, payload).then(r => r.data.data);

export const upsertActual = (payload) =>
  axios.put(`${BASE}/actuals/upsert`, payload).then(r => r.data.data);

export const deleteTarget = (id) =>
  axios.delete(`${BASE}/targets/${id}`).then(r => r.data);

export const getTaskOverview = (year) =>
  axios.get(`${BASE}/task-overview`, { params: { year } }).then(r => r.data.data);

// month is omitted for the no_deadline category — those tasks have no month
export const getTaskDetail = (picName, year, month, category) =>
  axios.get(`${BASE}/task-detail`, {
    params: { pic_name: picName, year, category, ...(month == null ? {} : { month }) },
  }).then(r => r.data.data);
