import axios from "axios";
const BASE = "/api/alerts";

export const getAlertsToday = (params = {}) =>
  axios.get(`${BASE}/today`, { params }).then(r => r.data.data);

export const getAlertsSummary = (params = {}) =>
  axios.get(`${BASE}/summary`, { params }).then(r => r.data.data);

export const getAlertsHistory = (params = {}) =>
  axios.get(`${BASE}/history`, { params }).then(r => r.data.data);

export const acknowledgeAlert = (alertId) =>
  axios.patch(`${BASE}/${alertId}/acknowledge`).then(r => r.data.data);

export const resolveAlert = (alertId) =>
  axios.patch(`${BASE}/${alertId}/resolve`).then(r => r.data.data);

export const getAlertRules = () =>
  axios.get(`${BASE}/rules`).then(r => r.data.data);

export const updateAlertRule = (ruleId, data) =>
  axios.put(`${BASE}/rules/${ruleId}`, data).then(r => r.data.data);

export const evaluateNow = () =>
  axios.post(`${BASE}/evaluate-now`).then(r => r.data.data);
