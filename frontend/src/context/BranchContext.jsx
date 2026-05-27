import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { useAuth } from './AuthContext'

const BranchContext = createContext(null)

const STORAGE_KEY = 'hid_selected_branch'

// Currency symbols per ISO code
export const CURRENCY_SYMBOLS = {
  VND: '₫',
  TWD: 'NT$',
  THB: '฿',
  USD: '$',
  EUR: '€',
  JPY: '¥',
}

export function BranchProvider({ children }) {
  const { canViewBranch, allowedBranches, isAdmin } = useAuth()
  const [allBranches, setAllBranches] = useState([])
  const [selected, setSelected] = useState(() => {
    return localStorage.getItem(STORAGE_KEY) || 'all'
  })
  const [loading, setLoading] = useState(true)

  // Fetch branch list on mount
  useEffect(() => {
    fetch('/api/branches')
      .then(r => r.json())
      .then(data => {
        setAllBranches(data.data || data || [])
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Only the branches this user is allowed to see.
  const branches = allBranches.filter(b => canViewBranch(b.id))

  // A user restricted to a subset of branches loses the aggregate "All" tab —
  // "All" would otherwise pull data across every branch (no branch_id filter).
  const restricted = !isAdmin && Array.isArray(allowedBranches) && allowedBranches.length > 0
  const canSelectAll = !restricted

  const selectBranch = useCallback((id) => {
    setSelected(id)
    localStorage.setItem(STORAGE_KEY, id)
  }, [])

  // Keep the active tab valid: if a restricted user has 'all' or a branch they
  // can't see selected, snap to their first allowed branch once loaded.
  useEffect(() => {
    if (loading || branches.length === 0) return
    const validIds = branches.map(b => b.id)
    if (!canSelectAll && (selected === 'all' || !validIds.includes(selected))) {
      selectBranch(validIds[0])
    }
  }, [loading, branches, canSelectAll, selected, selectBranch])

  // Current branch object (null when 'all')
  const currentBranch = selected === 'all'
    ? null
    : branches.find(b => b.id === selected) || null

  const currency = currentBranch?.native_currency
    || currentBranch?.currency
    || 'VND'

  const currencySymbol = CURRENCY_SYMBOLS[currency] || currency

  // Build query param string for API calls
  const branchParam = selected === 'all' ? '' : `branch_id=${selected}`

  return (
    <BranchContext.Provider value={{
      branches,
      selected,
      selectBranch,
      currentBranch,
      currency,
      currencySymbol,
      branchParam,
      canSelectAll,
      isAll: selected === 'all',
      loading,
    }}>
      {children}
    </BranchContext.Provider>
  )
}

export function useBranch() {
  const ctx = useContext(BranchContext)
  if (!ctx) throw new Error('useBranch must be used inside BranchProvider')
  return ctx
}

export default BranchContext
