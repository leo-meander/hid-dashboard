import { createContext, useContext, useState, useEffect, useCallback } from 'react'

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
  const [branches, setBranches] = useState([])
  const [selected, setSelected] = useState(() => {
    return localStorage.getItem(STORAGE_KEY) || 'all'
  })
  const [loading, setLoading] = useState(true)

  // Fetch branch list on mount
  useEffect(() => {
    fetch('/api/branches')
      .then(r => r.json())
      .then(data => {
        setBranches(data.data || data || [])
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const selectBranch = useCallback((id) => {
    setSelected(id)
    localStorage.setItem(STORAGE_KEY, id)
  }, [])

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
