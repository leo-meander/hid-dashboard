import axios from "axios";
const BASE = "/api/marketing-budget";

export const getYearlyBudget = (params) =>
  axios.get(`${BASE}/yearly`, { params }).then(r => r.data.data);

export const getMonthlyBudget = (params) =>
  axios.get(`${BASE}/monthly`, { params }).then(r => r.data.data);

export const getChannelSplits = (params) =>
  axios.get(`${BASE}/channel-splits`, { params }).then(r => r.data.data);

export const getBudgetSetup = (params) =>
  axios.get(`${BASE}/setup`, { params }).then(r => r.data.data);

export const upsertBudget = (item) =>
  axios.put(`${BASE}/`, item).then(r => r.data.data);

export const upsertBudgetBulk = (items) =>
  axios.put(`${BASE}/bulk`, { items }).then(r => r.data.data);
