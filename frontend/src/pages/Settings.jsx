/**
 * Settings — branch capacity management + API key management
 */
import { useEffect, useState } from "react";
import axios from "axios";
import { useAuth } from "../context/AuthContext";

// ── Branch Capacity Section ─────────────────────────────────────────────────

function BranchCapacity({ showToast }) {
  const [branches, setBranches] = useState([]);
  const [edits, setEdits] = useState({});
  const [saving, setSaving] = useState({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    axios.get("/api/branches")
      .then(r => {
        const data = r.data.data || [];
        setBranches(data);
        const initEdits = {};
        data.forEach(b => {
          initEdits[b.id] = {
            total_rooms: b.total_rooms ?? "",
            total_room_count: b.total_room_count ?? "",
            total_dorm_count: b.total_dorm_count ?? "",
          };
        });
        setEdits(initEdits);
      })
      .catch(() => setBranches([]))
      .finally(() => setLoading(false));
  }, []);

  const handleChange = (branchId, field, value) => {
    setEdits(prev => ({
      ...prev,
      [branchId]: { ...prev[branchId], [field]: value },
    }));
  };

  const save = async (branch) => {
    const e = edits[branch.id] || {};
    const payload = {};
    const toInt = v => v === "" || v === null ? null : parseInt(v, 10);

    if (toInt(e.total_rooms) !== branch.total_rooms) payload.total_rooms = toInt(e.total_rooms);
    if (toInt(e.total_room_count) !== branch.total_room_count) payload.total_room_count = toInt(e.total_room_count);
    if (toInt(e.total_dorm_count) !== branch.total_dorm_count) payload.total_dorm_count = toInt(e.total_dorm_count);

    if (Object.keys(payload).length === 0) {
      showToast("No changes to save", "info");
      return;
    }

    setSaving(prev => ({ ...prev, [branch.id]: true }));
    try {
      await axios.patch(`/api/branches/${branch.id}/capacity`, payload);
      setBranches(prev => prev.map(b => b.id === branch.id ? { ...b, ...payload } : b));
      showToast(`${branch.name} capacity updated`);
    } catch {
      showToast("Failed to save", "error");
    } finally {
      setSaving(prev => ({ ...prev, [branch.id]: false }));
    }
  };

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3 border-b border-gray-100">
        <h2 className="font-semibold text-gray-700 text-sm">Branch Capacity</h2>
        <p className="text-xs text-gray-400 mt-0.5">
          Total rooms = sellable units for OCC% calculation. Room count / Dorm count = private vs shared split.
        </p>
      </div>

      {loading ? (
        <div className="p-8 text-center text-gray-400 animate-pulse">Loading...</div>
      ) : (
        <div className="divide-y divide-gray-100">
          {branches.map(b => {
            const e = edits[b.id] || {};
            return (
              <div key={b.id} className="px-5 py-4">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <p className="font-medium text-gray-800">{b.name}</p>
                    <p className="text-xs text-gray-400">{b.city} · {b.native_currency}</p>
                  </div>
                  <button
                    onClick={() => save(b)}
                    disabled={saving[b.id]}
                    className="px-3 py-1.5 bg-indigo-600 text-white text-xs font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-50"
                  >
                    {saving[b.id] ? "Saving..." : "Save"}
                  </button>
                </div>
                <div className="grid grid-cols-3 gap-4">
                  <label className="block">
                    <span className="text-xs text-gray-500 font-medium">Total Rooms (OCC base)</span>
                    <input
                      type="number"
                      min="0"
                      value={e.total_rooms}
                      onChange={ev => handleChange(b.id, "total_rooms", ev.target.value)}
                      className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
                    />
                  </label>
                  <label className="block">
                    <span className="text-xs text-gray-500 font-medium">Private Rooms</span>
                    <input
                      type="number"
                      min="0"
                      value={e.total_room_count}
                      onChange={ev => handleChange(b.id, "total_room_count", ev.target.value)}
                      className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
                    />
                  </label>
                  <label className="block">
                    <span className="text-xs text-gray-500 font-medium">Dorm Beds</span>
                    <input
                      type="number"
                      min="0"
                      value={e.total_dorm_count}
                      onChange={ev => handleChange(b.id, "total_dorm_count", ev.target.value)}
                      className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
                    />
                  </label>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── API Keys Section ────────────────────────────────────────────────────────

function ApiKeys({ showToast }) {
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [newKeyName, setNewKeyName] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [newKeyValue, setNewKeyValue] = useState(null); // shown once after creation
  const [copied, setCopied] = useState(false);

  const fetchKeys = () => {
    axios.get("/api/api-keys")
      .then(r => setKeys(r.data.data || []))
      .catch(() => setKeys([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchKeys(); }, []);

  const createKey = async () => {
    if (!newKeyName.trim()) {
      showToast("Please enter a name for the API key", "error");
      return;
    }
    setCreating(true);
    try {
      const r = await axios.post("/api/api-keys", { name: newKeyName.trim() });
      const data = r.data.data;
      setNewKeyValue(data.key);
      setNewKeyName("");
      setShowCreate(false);
      fetchKeys();
      showToast("API key created successfully");
    } catch {
      showToast("Failed to create API key", "error");
    } finally {
      setCreating(false);
    }
  };

  const revokeKey = async (id, name) => {
    if (!window.confirm(`Revoke API key "${name}"? This cannot be undone.`)) return;
    try {
      await axios.delete(`/api/api-keys/${id}`);
      fetchKeys();
      showToast(`API key "${name}" revoked`);
    } catch {
      showToast("Failed to revoke key", "error");
    }
  };

  const copyToClipboard = (text) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3 border-b border-gray-100 flex items-center justify-between">
        <div>
          <h2 className="font-semibold text-gray-700 text-sm">API Keys</h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Generate keys for external systems to access reservation data via <code className="bg-gray-100 px-1 rounded text-xs">GET /api/public/reservations</code>
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="px-3 py-1.5 bg-indigo-600 text-white text-xs font-medium rounded-lg hover:bg-indigo-700"
        >
          + New Key
        </button>
      </div>

      {/* New key created — show once */}
      {newKeyValue && (
        <div className="mx-5 mt-4 p-4 bg-green-50 border border-green-200 rounded-lg">
          <p className="text-sm font-medium text-green-800 mb-2">
            API key created! Copy it now — it won't be shown again.
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 bg-white border border-green-300 rounded px-3 py-2 text-sm font-mono text-green-900 select-all break-all">
              {newKeyValue}
            </code>
            <button
              onClick={() => copyToClipboard(newKeyValue)}
              className="px-3 py-2 bg-green-600 text-white text-xs font-medium rounded-lg hover:bg-green-700 whitespace-nowrap"
            >
              {copied ? "Copied!" : "Copy"}
            </button>
          </div>
          <button
            onClick={() => setNewKeyValue(null)}
            className="mt-2 text-xs text-green-600 hover:text-green-800"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Create form */}
      {showCreate && (
        <div className="px-5 pt-4 pb-2">
          <div className="flex items-end gap-3">
            <label className="flex-1">
              <span className="text-xs text-gray-500 font-medium">Key Name</span>
              <input
                type="text"
                placeholder="e.g. PMS Integration, Revenue System"
                value={newKeyName}
                onChange={e => setNewKeyName(e.target.value)}
                onKeyDown={e => e.key === "Enter" && createKey()}
                className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
                autoFocus
              />
            </label>
            <button
              onClick={createKey}
              disabled={creating}
              className="px-4 py-1.5 bg-indigo-600 text-white text-xs font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-50"
            >
              {creating ? "Creating..." : "Generate"}
            </button>
            <button
              onClick={() => { setShowCreate(false); setNewKeyName(""); }}
              className="px-3 py-1.5 text-gray-500 text-xs font-medium rounded-lg hover:bg-gray-100"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Keys list */}
      {loading ? (
        <div className="p-8 text-center text-gray-400 animate-pulse">Loading...</div>
      ) : keys.length === 0 ? (
        <div className="p-8 text-center text-gray-400 text-sm">
          No API keys yet. Click "+ New Key" to generate one.
        </div>
      ) : (
        <div className="divide-y divide-gray-100">
          {keys.map(k => (
            <div key={k.id} className="px-5 py-3 flex items-center justify-between">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <p className="font-medium text-gray-800 text-sm">{k.name}</p>
                  {k.is_active ? (
                    <span className="px-1.5 py-0.5 bg-green-50 text-green-700 text-[10px] font-medium rounded">
                      Active
                    </span>
                  ) : (
                    <span className="px-1.5 py-0.5 bg-red-50 text-red-600 text-[10px] font-medium rounded">
                      Revoked
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 mt-0.5">
                  <code className="text-xs text-gray-400 font-mono">{k.key_prefix}...</code>
                  <span className="text-xs text-gray-300">|</span>
                  <span className="text-xs text-gray-400">
                    Created {new Date(k.created_at).toLocaleDateString()}
                  </span>
                  {k.last_used_at && (
                    <>
                      <span className="text-xs text-gray-300">|</span>
                      <span className="text-xs text-gray-400">
                        Last used {new Date(k.last_used_at).toLocaleDateString()}
                      </span>
                    </>
                  )}
                </div>
              </div>
              {k.is_active && (
                <button
                  onClick={() => revokeKey(k.id, k.name)}
                  className="ml-4 px-3 py-1.5 text-red-600 text-xs font-medium rounded-lg hover:bg-red-50 border border-red-200"
                >
                  Revoke
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Usage docs */}
      <div className="px-5 py-4 bg-gray-50 border-t border-gray-100">
        <p className="text-xs font-medium text-gray-500 mb-2">Usage</p>
        <div className="bg-white border border-gray-200 rounded-lg p-3">
          <code className="text-xs text-gray-600 block whitespace-pre-wrap font-mono">{`GET /api/public/reservations?date_from=2026-01-01&date_to=2026-01-31
Header: X-API-Key: hid_xxxxxxxxxxxxx

Query params:
  date_from   Check-in from date (YYYY-MM-DD)
  date_to     Check-in to date (YYYY-MM-DD)
  branch_id   Filter by branch UUID (optional)
  status      Filter by status (optional)
  limit       Max results (default 200, max 1000)
  offset      Pagination offset`}</code>
        </div>
      </div>
    </div>
  );
}

// ── Change Password Section ─────────────────────────────────────────────────

function ChangePassword({ showToast }) {
  const [oldPassword, setOldPassword]     = useState("");
  const [newPassword, setNewPassword]     = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [saving, setSaving] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!oldPassword || !newPassword) {
      showToast("Please fill in both old and new passwords", "error");
      return;
    }
    if (newPassword.length < 6) {
      showToast("New password must be at least 6 characters", "error");
      return;
    }
    if (newPassword !== confirmPassword) {
      showToast("New password and confirmation do not match", "error");
      return;
    }
    if (newPassword === oldPassword) {
      showToast("New password must differ from old password", "error");
      return;
    }
    setSaving(true);
    try {
      await axios.post("/api/auth/change-password", {
        old_password: oldPassword,
        new_password: newPassword,
      });
      setOldPassword(""); setNewPassword(""); setConfirmPassword("");
      showToast("Password changed successfully");
    } catch (err) {
      showToast(err.response?.data?.detail || "Failed to change password", "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3 border-b border-gray-100">
        <h2 className="font-semibold text-gray-700 text-sm">Change Password</h2>
        <p className="text-xs text-gray-400 mt-0.5">
          Enter your current password, then set a new one (minimum 6 characters).
        </p>
      </div>
      <form onSubmit={submit} className="px-5 py-5 space-y-4 max-w-md">
        <label className="block">
          <span className="text-xs text-gray-500 font-medium">Current Password</span>
          <input
            type="password"
            value={oldPassword}
            onChange={e => setOldPassword(e.target.value)}
            className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
            autoComplete="current-password"
          />
        </label>
        <label className="block">
          <span className="text-xs text-gray-500 font-medium">New Password</span>
          <input
            type="password"
            value={newPassword}
            onChange={e => setNewPassword(e.target.value)}
            className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
            autoComplete="new-password"
          />
        </label>
        <label className="block">
          <span className="text-xs text-gray-500 font-medium">Confirm New Password</span>
          <input
            type="password"
            value={confirmPassword}
            onChange={e => setConfirmPassword(e.target.value)}
            className="mt-1 w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
            autoComplete="new-password"
          />
        </label>
        <button
          type="submit"
          disabled={saving}
          className="px-4 py-1.5 bg-indigo-600 text-white text-xs font-medium rounded-lg hover:bg-indigo-700 disabled:opacity-50"
        >
          {saving ? "Saving..." : "Update Password"}
        </button>
      </form>
    </div>
  );
}

// ── Main Settings Page ──────────────────────────────────────────────────────

export default function Settings() {
  const { user } = useAuth();
  const [toast, setToast] = useState(null);
  const [tab, setTab] = useState("capacity");

  const showToast = (msg, type = "success") => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  };

  const isAdmin = user?.role === "admin";

  return (
    <div className="space-y-5 max-w-3xl">
      <div>
        <h1 className="text-xl font-bold text-gray-800">Settings</h1>
        <p className="text-xs text-gray-400 mt-0.5">Manage branch capacity, API access, and account security</p>
      </div>

      {toast && (
        <div className={`px-4 py-2.5 rounded-lg text-sm font-medium ${
          toast.type === "error" ? "bg-red-50 text-red-700 border border-red-200"
          : toast.type === "info" ? "bg-gray-50 text-gray-600 border border-gray-200"
          : "bg-green-50 text-green-700 border border-green-200"
        }`}>
          {toast.msg}
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-100 rounded-lg p-0.5 w-fit">
        <button
          onClick={() => setTab("capacity")}
          className={`px-4 py-1.5 text-xs font-medium rounded-md transition-colors ${
            tab === "capacity"
              ? "bg-white text-gray-800 shadow-sm"
              : "text-gray-500 hover:text-gray-700"
          }`}
        >
          Branch Capacity
        </button>
        {isAdmin && (
          <button
            onClick={() => setTab("api-keys")}
            className={`px-4 py-1.5 text-xs font-medium rounded-md transition-colors ${
              tab === "api-keys"
                ? "bg-white text-gray-800 shadow-sm"
                : "text-gray-500 hover:text-gray-700"
            }`}
          >
            API Keys
          </button>
        )}
        <button
          onClick={() => setTab("password")}
          className={`px-4 py-1.5 text-xs font-medium rounded-md transition-colors ${
            tab === "password"
              ? "bg-white text-gray-800 shadow-sm"
              : "text-gray-500 hover:text-gray-700"
          }`}
        >
          Password
        </button>
      </div>

      {tab === "capacity" && <BranchCapacity showToast={showToast} />}
      {tab === "api-keys" && isAdmin && <ApiKeys showToast={showToast} />}
      {tab === "password" && <ChangePassword showToast={showToast} />}
    </div>
  );
}
