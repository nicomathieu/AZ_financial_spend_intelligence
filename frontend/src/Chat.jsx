import { useState, useRef, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'

const API = 'http://localhost:8000'

const CONFIDENCE_COLOR = { high: '#22c55e', medium: '#f97316', low: '#ef4444' }

function ConfidenceBadge({ level }) {
  return (
    <span style={{
      display: 'inline-block',
      padding: '2px 8px',
      borderRadius: 4,
      fontSize: 11,
      fontWeight: 700,
      textTransform: 'uppercase',
      background: CONFIDENCE_COLOR[level] ?? '#9ca3af',
      color: '#fff',
      letterSpacing: '0.04em',
    }}>
      {level}
    </span>
  )
}

function EvidencePanel({ evidence }) {
  const [open, setOpen] = useState(false)
  if (!evidence) return null

  return (
    <div style={{ marginTop: 10 }}>
      <button className="btn-link" onClick={() => setOpen(o => !o)}>
        {open ? '▼' : '▶'} Evidence
      </button>
      {open && (
        <div style={{
          marginTop: 8,
          padding: '12px 14px',
          background: '#f8fafc',
          border: '1px solid #e2e8f0',
          borderRadius: 6,
          fontSize: 13,
        }}>
          {evidence.policy_sources?.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <strong>Policy sections cited:</strong>
              <ul style={{ margin: '4px 0 0 18px', padding: 0, lineHeight: 1.7 }}>
                {evidence.policy_sources.map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            </div>
          )}
          {evidence.sql_query && (
            <div style={{ marginBottom: 10 }}>
              <strong>SQL query:</strong>
              <pre style={{
                margin: '6px 0 0',
                padding: '10px 12px',
                background: '#1e293b',
                color: '#e2e8f0',
                borderRadius: 5,
                overflowX: 'auto',
                fontSize: 12,
                lineHeight: 1.5,
              }}>
                {evidence.sql_query}
              </pre>
            </div>
          )}
          {evidence.sql_results != null && (
            <div>
              <strong>Results:</strong>{' '}
              {evidence.sql_results.length} row{evidence.sql_results.length !== 1 ? 's' : ''} returned
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function UserMessage({ content }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 14 }}>
      <div style={{
        maxWidth: '70%',
        background: '#3b82f6',
        color: '#fff',
        padding: '10px 14px',
        borderRadius: '12px 12px 2px 12px',
        fontSize: 14,
        lineHeight: 1.5,
      }}>
        {content}
      </div>
    </div>
  )
}

function AssistantMessage({ msg }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 14 }}>
      <div style={{
        maxWidth: '82%',
        background: '#fff',
        border: '1px solid #e2e8f0',
        padding: '12px 14px',
        borderRadius: '2px 12px 12px 12px',
        fontSize: 14,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <ConfidenceBadge level={msg.confidence} />
          {msg.source && (
            <span style={{ fontSize: 11, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
              {msg.source.replace('_', ' ')}
            </span>
          )}
        </div>
        <div className="markdown-body">
          <ReactMarkdown>{msg.answer}</ReactMarkdown>
        </div>
        <EvidencePanel evidence={msg.evidence} />
        {msg.disclaimer && (
          <div style={{ marginTop: 10, fontSize: 11, color: '#94a3b8', fontStyle: 'italic' }}>
            {msg.disclaimer}
          </div>
        )}
      </div>
    </div>
  )
}

export default function Chat() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  async function handleSend() {
    const question = input.trim()
    if (!question || loading) return
    setInput('')
    setError(null)
    setMessages(prev => [...prev, { role: 'user', content: question }])
    setLoading(true)
    try {
      const res = await fetch(`${API}/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `HTTP ${res.status}`)
      }
      const data = await res.json()
      setMessages(prev => [...prev, {
        role: 'assistant',
        answer: data.answer,
        evidence: data.evidence,
        confidence: data.confidence,
        disclaimer: data.disclaimer,
        source: data.evidence?.source,
      }])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="chat-container">
      <div className="message-list">
        {messages.length === 0 && (
          <div style={{ textAlign: 'center', color: '#94a3b8', marginTop: 64, fontSize: 14 }}>
            Ask a question about procurement policy or invoice spend.
          </div>
        )}
        {messages.map((msg, i) =>
          msg.role === 'user'
            ? <UserMessage key={i} content={msg.content} />
            : <AssistantMessage key={i} msg={msg} />
        )}
        {loading && (
          <div style={{ display: 'flex', justifyContent: 'flex-start', marginBottom: 14 }}>
            <div style={{
              background: '#fff',
              border: '1px solid #e2e8f0',
              padding: '14px 16px',
              borderRadius: '2px 12px 12px 12px',
            }}>
              <div className="spinner" />
            </div>
          </div>
        )}
        {error && (
          <div style={{
            padding: '10px 14px',
            background: '#fef2f2',
            border: '1px solid #fecaca',
            color: '#b91c1c',
            borderRadius: 6,
            fontSize: 13,
            marginBottom: 12,
          }}>
            Error: {error}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="input-bar">
        <textarea
          className="chat-input"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask about invoices or procurement policy… (Enter to send, Shift+Enter for newline)"
          rows={2}
        />
        <button
          className="btn-primary"
          onClick={handleSend}
          disabled={loading || !input.trim()}
        >
          Send
        </button>
      </div>
    </div>
  )
}
