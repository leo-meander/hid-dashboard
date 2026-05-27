import { createContext, useContext, useState, useEffect, useCallback } from "react";
import axios from "axios";
import { groupAllowed, firstAllowedPath as computeFirstAllowedPath } from "../constants/pageGroups";

const AuthContext = createContext(null);

const TOKEN_KEY = "hid_token";
const USER_KEY  = "hid_user";

// Attach JWT to every request
axios.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// Dev mode: skip auth when running locally without backend
const DEV_USER = import.meta.env.DEV ? {
  id: "dev-admin",
  email: "admin@hid.local",
  name: "Dev Admin",
  role: "admin",
  allowed_branches: [],
  allowed_pages: [],
} : null;

export function AuthProvider({ children }) {
  const [user,    setUser]    = useState(() => {
    if (DEV_USER) return DEV_USER;
    try { return JSON.parse(localStorage.getItem(USER_KEY)); } catch { return null; }
  });
  const [loading, setLoading] = useState(!DEV_USER);

  // Validate stored token on mount (skip in dev mode)
  useEffect(() => {
    if (DEV_USER) { setLoading(false); return; }
    const token = localStorage.getItem(TOKEN_KEY);
    if (!token) { setLoading(false); return; }
    axios.get("/api/auth/me")
      .then(r => setUser(r.data.data))
      .catch(() => { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); setUser(null); })
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email, password) => {
    const r = await axios.post("/api/auth/login", { email, password });
    const { token, user: u } = r.data.data;
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY,  JSON.stringify(u));
    setUser(u);
    return u;
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    setUser(null);
  }, []);

  const isAdmin = user?.role === "admin";

  // Access scope — admins ignore both; empty arrays mean "all".
  const allowedBranches = isAdmin ? [] : (user?.allowed_branches || []);
  const allowedPages    = isAdmin ? [] : (user?.allowed_pages || []);

  // Can the user open a given sidebar group?
  const canViewGroup = useCallback(
    (key) => isAdmin || groupAllowed(allowedPages, key),
    [isAdmin, allowedPages],
  );

  // Can the user see a given branch id? (admins + unrestricted users: yes)
  const canViewBranch = useCallback(
    (branchId) => isAdmin || allowedBranches.length === 0 || allowedBranches.includes(branchId),
    [isAdmin, allowedBranches],
  );

  // Where to send the user if they hit a page they can't view.
  const firstAllowedPath = isAdmin ? "/home" : computeFirstAllowedPath(allowedPages);

  return (
    <AuthContext.Provider value={{
      user, loading, login, logout, isAdmin,
      allowedBranches, allowedPages,
      canViewGroup, canViewBranch, firstAllowedPath,
    }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
