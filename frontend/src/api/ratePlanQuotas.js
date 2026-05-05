import axios from "axios";
const BASE = "/api/rate-plan-quotas";

export const listQuotas = () =>
  axios.get(`${BASE}/`).then(r => r.data.data);

export const createQuota = (payload) =>
  axios.post(`${BASE}/`, payload).then(r => r.data.data);

export const updateQuota = (id, payload) =>
  axios.patch(`${BASE}/${id}`, payload).then(r => r.data.data);

export const deleteQuota = (id) =>
  axios.delete(`${BASE}/${id}`).then(r => r.data.data);

export const refreshQuotas = () =>
  axios.post(`${BASE}/refresh`).then(r => r.data.data);
