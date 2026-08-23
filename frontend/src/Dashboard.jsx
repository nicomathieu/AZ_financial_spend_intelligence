import { useState, useEffect } from 'react'

const API = 'http://localhost:8000'

function SummaryCard({ label, value, accent }) {
  return (
    <div style={{
      background: '#fff',
      border: '1px solid #e2e8f0',
      borderRadius: 8,
      padding: '18px 22px',
      flex: '1 1 160px',
      minWidth: 150,
    }}>
      <div style={{ fontSize: 12, color: '#64748b', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600 }}>
        {label}
      </div>
      <div style={{ fontSize: 26, fontWeight: 700, color: accent ?? '#1e293b' }}>
        {value ?? '—'}
      </div>
    </div>
  )
}

function FlagTag({ flag }) {
  return (
    <span style={{
      display: 'inline-block',
      marginRight: 4,
      marginBottom: 2,
      padding: '1px 6px',
      borderRadius: 3,
      background: '#fef3c7',
      color: '#92400e',
      fontSize: 11,
      fontWeight: 600,
    }}>
      {flag}
    </span>
  )
}

export default function Dashboard() {
  const [report, setReport] = useState(null)
  const [invoices, setInvoices] = useState([])
  const [flagFilter, setFlagFilter] = useState('')
  const [loadingReport, setLoadingReport] = useState(true)
  const [loadingInvoices, setLoadingInvoices] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    setLoadingReport(true)
    fetch(`${API}/quality-report`)
      .then(r => {
        if (!r.ok) return r.json().then(d => Promise.reject(d.detail ?? `HTTP ${r.status}`))
        return r.json()
      })
      .then(setReport)
      .catch(err => setError(String(err)))
      .finally(() => setLoadingReport(false))
  }, [])

  useEffect(() => {
    setLoadingInvoices(true)
    const qs = flagFilter ? `?flag_type=${encodeURIComponent(flagFilter)}` : ''
    fetch(`${API}/invoices/flagged${qs}`)
      .then(r => {
        if (!r.ok) return r.json().then(d => Promise.reject(d.detail ?? `HTTP ${r.status}`))
        return r.json()
      })
      .then(setInvoices)
      .catch(err => setError(String(err)))
      .finally(() => setLoadingInvoices(false))
  }, [flagFilter])

  const flagTypes = report ? Object.keys(report.flags) : []
  const totalFlags = report
    ? Object.values(report.flags).reduce((sum, f) => sum + f.count, 0)
    : null
  const topFlagType = report
    ? (Object.entries(report.flags).sort((a, b) => b[1].count - a[1].count)[0]?.[0] ?? '—')
    : null

  return (
    <div className="dashboard">
      {error && (
        <div style={{
          padding: '10px 14px',
          background: '#fef2f2',
          border: '1px solid #fecaca',
          color: '#b91c1c',
          borderRadius: 6,
          marginBottom: 20,
          fontSize: 13,
        }}>
          Error: {error}
        </div>
      )}

      <section style={{ marginBottom: 32 }}>
        <h2 className="section-heading">Summary</h2>
        {loadingReport ? (
          <div style={{ color: '#94a3b8', fontSize: 14 }}>Loading…</div>
        ) : (
          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
            <SummaryCard
              label="Total Invoices"
              value={report?.total_invoices?.toLocaleString()}
            />
            <SummaryCard
              label="Total Flags"
              value={totalFlags?.toLocaleString()}
              accent="#f97316"
            />
            <SummaryCard
              label="Quarantined Rows"
              value={report?.quarantined_rows?.toLocaleString()}
              accent="#ef4444"
            />
            <SummaryCard
              label="Top Flag Type"
              value={topFlagType}
              accent="#8b5cf6"
            />
          </div>
        )}
      </section>

      <section>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 14 }}>
          <h2 className="section-heading" style={{ margin: 0 }}>Flagged Invoices</h2>
          <select
            value={flagFilter}
            onChange={e => setFlagFilter(e.target.value)}
            style={{
              padding: '5px 10px',
              borderRadius: 5,
              border: '1px solid #e2e8f0',
              fontSize: 13,
              background: '#fff',
              color: '#334155',
            }}
          >
            <option value="">All flag types</option>
            {flagTypes.map(ft => (
              <option key={ft} value={ft}>{ft}</option>
            ))}
          </select>
          {loadingInvoices && (
            <span style={{ fontSize: 12, color: '#94a3b8' }}>Loading…</span>
          )}
        </div>

        <div style={{ overflowX: 'auto' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Invoice #</th>
                <th>Date</th>
                <th>Vendor</th>
                <th style={{ textAlign: 'right' }}>Amount</th>
                <th>Currency</th>
                <th>Flags</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {!loadingInvoices && invoices.length === 0 && (
                <tr>
                  <td colSpan={7} style={{ textAlign: 'center', color: '#94a3b8', padding: 28 }}>
                    No flagged invoices found.
                  </td>
                </tr>
              )}
              {invoices.map((inv, i) => (
                <tr key={i}>
                  <td style={{ fontFamily: 'monospace', fontSize: 12 }}>{inv.invoice_number}</td>
                  <td>{inv.invoice_date}</td>
                  <td>{inv.vendor_name ?? '—'}</td>
                  <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                    {inv.amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </td>
                  <td>{inv.currency}</td>
                  <td>{inv.flags.map(f => <FlagTag key={f} flag={f} />)}</td>
                  <td>
                    {inv.quarantined
                      ? <span style={{ color: '#ef4444', fontWeight: 700, fontSize: 12 }}>QUARANTINED</span>
                      : <span style={{ color: '#22c55e', fontSize: 12 }}>OK</span>
                    }
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
