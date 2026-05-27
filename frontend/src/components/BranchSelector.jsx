import { useBranch, CURRENCY_SYMBOLS } from '../context/BranchContext'

export default function BranchSelector() {
  const { branches, selected, selectBranch, loading, canSelectAll } = useBranch()

  if (loading) {
    return (
      <div className="sticky top-0 z-50 bg-gray-900 border-b border-gray-700 px-4 py-2">
        <div className="flex gap-2 animate-pulse">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-8 w-24 bg-gray-700 rounded-full" />
          ))}
        </div>
      </div>
    )
  }

  const tabs = [
    ...(canSelectAll ? [{ id: 'all', label: 'All Branches', symbol: null }] : []),
    ...branches.map(b => ({
      id: b.id,
      label: b.name,
      symbol: CURRENCY_SYMBOLS[b.native_currency || b.currency] || '',
    })),
  ]

  return (
    <div className="sticky top-0 z-50 bg-gray-900 border-b border-gray-700 shadow-md">
      <div className="flex items-center gap-1 px-4 py-2 overflow-x-auto scrollbar-hide">
        {tabs.map(tab => {
          const active = selected === tab.id
          return (
            <button
              key={tab.id}
              onClick={() => selectBranch(tab.id)}
              className={`
                flex-shrink-0 px-4 py-1.5 rounded-full text-sm font-medium
                transition-all duration-150 whitespace-nowrap
                ${active
                  ? 'bg-blue-600 text-white shadow-lg shadow-blue-900/40'
                  : 'text-gray-400 hover:text-white hover:bg-gray-700'
                }
              `}
            >
              {tab.symbol && (
                <span className="mr-1 opacity-70">{tab.symbol}</span>
              )}
              {tab.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}
