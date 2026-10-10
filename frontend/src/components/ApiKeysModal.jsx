import { useEffect, useRef, useState, forwardRef } from "react"

const KeyInput = forwardRef(function KeyInput({ value, invalid, ...props }, ref) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const t = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(t)
  }, [copied])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      return
    } catch {
      // Clipboard API can be blocked (permissions, embedded browsers); use the legacy path
    }
    const ta = document.createElement("textarea")
    ta.value = value
    ta.setAttribute("readonly", "")
    ta.style.position = "fixed"
    ta.style.opacity = "0"
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand("copy")
    document.body.removeChild(ta)
    if (ok) setCopied(true)
  }

  return (
    <div className="key-input">
      <input
        ref={ref}
        type="password"
        className={`field-input ${invalid ? "field-input--error" : ""}`}
        value={value}
        {...props}
      />
      {value && (
        <button
          type="button"
          className={`key-copy ${copied ? "key-copy--done" : ""}`}
          onClick={copy}
          title={copied ? "Copied" : "Copy key"}
          aria-label={copied ? "Copied" : "Copy key"}
        >
          {copied ? (
            <svg viewBox="0 0 16 16" fill="none">
              <path d="M3.5 8.5l3 3 6-7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          ) : (
            <svg viewBox="0 0 16 16" fill="none">
              <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.3"/>
              <path d="M10.5 3.5v-.5A1.5 1.5 0 009 1.5H4A1.5 1.5 0 002.5 3v5A1.5 1.5 0 004 9.5h.5" stroke="currentColor" strokeWidth="1.3"/>
            </svg>
          )}
        </button>
      )}
    </div>
  )
})

function CheckCircle({ label }) {
  return (
    <span className="key-status key-status--ok" role="img" aria-label={label} title={label}>
      <svg viewBox="0 0 16 16" fill="none">
        <path d="M3.5 8.5l3 3 6-7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
      </svg>
    </span>
  )
}

function GetKeyButton({ url, title }) {
  return (
    <button
      type="button"
      className="btn-secondary"
      onClick={() => window.open(url, "_blank", "noopener,noreferrer")}
      title={title}
    >
      Get key ↗
    </button>
  )
}

export default function ApiKeysFields({
  settings, update, providers, aiProvider, changeProvider, keyCheck, focus, active,
}) {
  const tmdbRef = useRef(null)
  const aiRef = useRef(null)

  useEffect(() => {
    if (!active) return
    const target = focus === "ai" ? aiRef.current : !settings.tmdb_api_key ? tmdbRef.current : null
    target?.focus()
  }, [active, focus])

  const tmdbKey = (settings.tmdb_api_key || "").trim()
  const omdbKey = (settings.omdb_api_key || "").trim()

  return (
        <div className="keys-body">
          <section className="keys-group">
            <label className="field-label" htmlFor="tmdb-key">TMDB <span className="field-optional">(required)</span></label>
            <div className="ai-key-row">
              <KeyInput
                id="tmdb-key"
                ref={tmdbRef}
                placeholder="Enter your TMDB v3 key"
                value={settings.tmdb_api_key || ""}
                onChange={e => update("tmdb_api_key", e.target.value)}
              />
              {tmdbKey
                ? <CheckCircle label="TMDB key saved" />
                : <GetKeyButton url="https://www.themoviedb.org/settings/api" title="Get a free key at themoviedb.org" />}
            </div>
            <span className="field-hint">Main source for movie and TV matches. Free.</span>
          </section>

          <section className="keys-group">
            <label className="field-label" htmlFor="omdb-key">OMDb <span className="field-optional">(optional)</span></label>
            <div className="ai-key-row">
              <KeyInput
                id="omdb-key"
                placeholder="Enter your OMDb key"
                value={settings.omdb_api_key || ""}
                onChange={e => update("omdb_api_key", e.target.value)}
              />
              {omdbKey
                ? <CheckCircle label="OMDb key saved" />
                : <GetKeyButton url="https://www.omdbapi.com/apikey.aspx" title="Get a free key at omdbapi.com" />}
            </div>
            <span className="field-hint">Free IMDb-backed fallback when TMDB has no match; also adds <code>{"{imdb-…}"}</code> tags.</span>
          </section>

          <section className="keys-group">
            <label className="field-label" htmlFor="ai-provider">AI provider <span className="field-optional">(optional)</span></label>
            <select
              id="ai-provider"
              className="field-input"
              value={settings.ai_provider || "openai"}
              onChange={e => changeProvider(e.target.value)}
            >
              {Object.entries(providers).map(([id, p]) => (
                <option key={id} value={id}>{p.label}</option>
              ))}
            </select>

            {aiProvider && (
              <>
                <label className="field-label" htmlFor="ai-key">{aiProvider.label} API key</label>
                <div className="ai-key-row">
                  <KeyInput
                    id="ai-key"
                    ref={aiRef}
                    invalid={keyCheck.status === "invalid"}
                    placeholder={aiProvider.keyPlaceholder}
                    value={settings[aiProvider.keyField] || ""}
                    onChange={e => update(aiProvider.keyField, e.target.value)}
                  />
                  {keyCheck.status === "checking" ? (
                    <span className="key-status key-status--checking" title="Checking key…">
                      <div className="spinner-sm" />
                    </span>
                  ) : keyCheck.status === "valid" ? (
                    <CheckCircle label={`${aiProvider.label} key verified`} />
                  ) : (
                    <GetKeyButton url={aiProvider.keyUrl} title={`Sign in to ${aiProvider.label} and create an API key`} />
                  )}
                </div>
                {keyCheck.status === "invalid"
                  ? <span className="field-error">{keyCheck.error}</span>
                  : <span className="field-hint">Only used when &ldquo;Use AI&rdquo; is on under Matching. Billed to your own account.</span>}
              </>
            )}
          </section>
        </div>
  )
}
