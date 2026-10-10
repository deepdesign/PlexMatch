import { useState, useEffect, useCallback, useRef } from "react"
import Settings from "./components/Settings"
import ScanPanel from "./components/ScanPanel"
import ProposalTable from "./components/ProposalTable"
import StatusBar from "./components/StatusBar"
import "./styles.css"

const API = "http://localhost:5174"

const STAGES = ["Configure", "Scan", "Review", "Rename"]

// Browser-local persistence so the TMDB API key (and other prefs) are
// remembered for subsequent operations even before/without the backend.
const STORAGE_KEY = "plexmatch.settings"

const DEFAULT_SETTINGS = {
  tmdb_api_key: "",
  root_folder: "",
  auto_pick_top: true,
  restructure_folders: true,
  clean_empty_folders: false,
  clean_unmatched: true,
  web_lookup: true,
  split_libraries: true,
  skip_output_folders: true,
  embed_ids: true,
  omdb_api_key: "",
  ai_enabled: false,
  ai_provider: "",
  openai_api_key: "",
  anthropic_api_key: "",
  ai_model: "",
  openai_base_url: "",
}

// Statuses the user can approve and rename
const APPROVABLE = new Set(["pending", "cleanup"])

function inPlacePath(proposal, root) {
  const sep = (root || "").includes("\\") ? "\\" : "/"
  const folder = proposal.folder && proposal.folder !== "."
    ? proposal.folder.replace(/[\\/]/g, sep)
    : ""
  const filename = proposal.proposed_filename || proposal.filename
  const base = (root || "").replace(/[\\/]+$/, "")
  return folder ? `${base}${sep}${folder}${sep}${filename}` : `${base}${sep}${filename}`
}

function pathInside(file, folder) {
  const norm = (p) => p.replace(/[\\/]+/g, "/").replace(/\/$/, "").toLowerCase()
  const target = norm(file)
  const dir = norm(folder)
  return target === dir || target.startsWith(`${dir}/`)
}

function plexLibraryPaths(root, results) {
  if (!root) return []
  const sep = root.includes("\\") ? "\\" : "/"
  const base = root.replace(/[\\/]+$/, "")
  return ["Movies", "TV"].flatMap(name => {
    const folder = `${base}${sep}${name}`
    const used = (results || []).some(r => r.ok && r.dst && pathInside(r.dst, folder))
    return used ? [{ label: name, path: folder }] : []
  })
}

function CopyPath({ label, path }) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(path)
      setCopied(true)
      return
    } catch {
      // Clipboard API can be blocked; fall back to the legacy command.
    }
    const field = document.createElement("textarea")
    field.value = path
    field.setAttribute("readonly", "")
    field.style.position = "fixed"
    field.style.opacity = "0"
    document.body.appendChild(field)
    field.select()
    const ok = document.execCommand("copy")
    document.body.removeChild(field)
    if (ok) setCopied(true)
  }

  return (
    <div className="library-path">
      <span className="library-path-label">{label}</span>
      <code className="library-path-value">{path}</code>
      <button
        type="button"
        className={`library-path-copy ${copied ? "library-path-copy--done" : ""}`}
        onClick={copy}
        title={copied ? "Copied" : `Copy ${label} path`}
        aria-label={copied ? "Copied" : `Copy ${label} path`}
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
    </div>
  )
}

function readCachedSettings() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function writeCachedSettings(data) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data))
  } catch {
    // ignore quota / privacy-mode errors
  }
}

export default function App() {
  const [stage, setStage] = useState(0) // 0=configure 1=scan 2=review 3=done
  const [settings, setSettings] = useState(null)
  const [scanning, setScanning] = useState(false)
  const [scanProgress, setScanProgress] = useState({ current: 0, total: 0 })
  const [proposals, setProposals] = useState([])
  const [renaming, setRenaming] = useState(false)
  const [renameResults, setRenameResults] = useState([])
  const [error, setError] = useState(null)
  const [scanRoot, setScanRoot] = useState("")
  const [lastBatch, setLastBatch] = useState(null)
  const [undoing, setUndoing] = useState(false)
  const [resultNote, setResultNote] = useState(null)

  // Load persisted settings on mount: hydrate instantly from localStorage,
  // then reconcile with the backend's settings.json (the source of truth).
  useEffect(() => {
    const cached = readCachedSettings()
    if (cached) setSettings({ ...DEFAULT_SETTINGS, ...cached })

    const load = async () => {
      try {
        const res = await fetch(`${API}/api/settings`)
        const s = await res.json()
        const merged = { ...DEFAULT_SETTINGS, ...s }
        // Keep locally-stored keys if the backend somehow has none
        let restored = false
        for (const k of ["tmdb_api_key", "omdb_api_key", "openai_api_key", "anthropic_api_key"]) {
          if (!merged[k] && cached?.[k]) {
            merged[k] = cached[k]
            restored = true
          }
        }
        if (restored) {
          fetch(`${API}/api/settings`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(merged),
          }).catch(() => {})
        }
        // Self-heal: drop a stored library folder that no longer exists on disk
        if (merged.root_folder) {
          try {
            const v = await fetch(`${API}/api/validate-folder`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: merged.root_folder }),
            })
            const vd = await v.json()
            if (v.ok && vd.exists === false) merged.root_folder = ""
          } catch {
            // backend unreachable for validation: leave the value as-is
          }
        }
        setSettings(merged)
        writeCachedSettings(merged)
      } catch {
        if (!cached) setSettings({ ...DEFAULT_SETTINGS })
      }
    }

    load()
  }, [])

  const saveSettings = useCallback(async (updated) => {
    setSettings(updated)
    writeCachedSettings(updated)
    try {
      await fetch(`${API}/api/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updated),
      })
    } catch {
      // backend unavailable: settings are still cached locally
    }
  }, [])

  const scanAbort = useRef(null)

  const stopScan = useCallback(() => {
    scanAbort.current?.abort()
  }, [])

  const handleScan = useCallback(async (rootOverride) => {
    scanAbort.current?.abort()
    const controller = new AbortController()
    scanAbort.current = controller
    setError(null)
    setProposals([])
    setRenameResults([])
    setResultNote(null)
    setScanProgress({ current: 0, total: 0, skipped: [] })
    setScanning(true)
    setStage(1)

    const root = rootOverride || settings.root_folder
    let received = 0

    try {
      const res = await fetch(`${API}/api/scan/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          root_folder: root,
          tmdb_api_key: settings.tmdb_api_key,
          clean_unmatched: settings.clean_unmatched,
          web_lookup: settings.web_lookup,
          split_libraries: settings.split_libraries,
          skip_output_folders: settings.skip_output_folders !== false,
          embed_ids: settings.embed_ids,
          restructure_folders: settings.restructure_folders,
          auto_pick_top: settings.auto_pick_top,
          omdb_api_key: settings.omdb_api_key,
          ai_enabled: settings.ai_enabled,
          ai_provider: settings.ai_provider,
          openai_api_key: settings.openai_api_key,
          anthropic_api_key: settings.anthropic_api_key,
          ai_model: settings.ai_model,
          openai_base_url: settings.openai_base_url,
        }),
      })

      if (!res.ok || !res.body) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || "Scan failed")
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // SSE frames are separated by a blank line
        const frames = buffer.split("\n\n")
        buffer = frames.pop()

        for (const frame of frames) {
          const line = frame.replace(/^data:\s?/, "").trim()
          if (!line) continue
          const msg = JSON.parse(line)

          if (msg.type === "start") {
            setScanRoot(msg.root)
            setScanProgress({ current: 0, total: msg.total, skipped: msg.skipped || [] })
          } else if (msg.type === "proposal") {
            received += 1
            const p = {
              ...msg.proposal,
              approved: msg.proposal.status === "pending" && !msg.proposal.needs_choice,
              selectedAlt: null,
            }
            setProposals(prev => [...prev, p])
            setScanProgress(prev => ({ ...prev, current: prev.current + 1 }))
          } else if (msg.type === "error") {
            throw new Error(msg.error)
          }
        }
      }

      setStage(2)
    } catch (e) {
      if (e.name === "AbortError") {
        setStage(received > 0 ? 2 : 0)
        return
      }
      setError(e.message)
      setStage(0)
    } finally {
      if (scanAbort.current === controller) scanAbort.current = null
      setScanning(false)
    }
  }, [settings])

  const handleRename = useCallback(async () => {
    const approved = proposals
      .filter(p => p.approved && APPROVABLE.has(p.status))
      .map(p => ({
        full_path: p.full_path,
        proposed_full_path: settings?.restructure_folders
          ? p.proposed_full_path
          : inPlacePath(p, scanRoot),
      }))

    if (!approved.length) return
    setRenaming(true)
    setResultNote(null)

    try {
      const res = await fetch(`${API}/api/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root_folder: scanRoot,
          approved,
          clean_empty_folders: settings.clean_empty_folders,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Rename failed")
      setRenameResults(data.results)
      setLastBatch(data.batch_id || null)
      if (data.removed_folders?.length) {
        setResultNote(`Cleaned up ${data.removed_folders.length} empty folder${data.removed_folders.length !== 1 ? "s" : ""}.`)
      }
      setStage(3)
    } catch (e) {
      setError(e.message)
    } finally {
      setRenaming(false)
    }
  }, [proposals, scanRoot, settings])

  const handleUndo = useCallback(async () => {
    if (!lastBatch) return
    setUndoing(true)
    setError(null)
    try {
      const res = await fetch(`${API}/api/undo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batch_id: lastBatch }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Undo failed")
      setRenameResults(data.results)
      setLastBatch(null)
      const ok = data.results.filter(r => r.ok).length
      setResultNote(`Reverted ${ok} file${ok !== 1 ? "s" : ""} to original location${ok !== 1 ? "s" : ""}.`)
    } catch (e) {
      setError(e.message)
    } finally {
      setUndoing(false)
    }
  }, [lastBatch])

  const approvedCount = proposals.filter(p => p.approved && APPROVABLE.has(p.status)).length
  const matchedCount = proposals.filter(p => p.matched).length
  const cleanupCount = proposals.filter(p => p.status === "cleanup").length
  const unmatchedCount = proposals.filter(p => !p.matched && p.status !== "cleanup").length
  const organisedCount = proposals.filter(p => p.status === "organised").length
  const conflictCount = proposals.filter(p => p.status === "conflict").length
  const plexLibraries = settings && lastBatch && settings.split_libraries && settings.restructure_folders
    ? plexLibraryPaths(scanRoot, renameResults)
    : []

  if (!settings) {
    return (
      <div className="app-loading">
        <div className="spinner" />
        <span>Connecting to backend...</span>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-brand">
          <svg className="brand-icon" viewBox="0 0 24 24" fill="none">
            <path d="M12 2L2 7v10l10 5 10-5V7L12 2z" stroke="#F5A623" strokeWidth="1.5" fill="none"/>
            <path d="M12 2v15M2 7l10 5 10-5" stroke="#F5A623" strokeWidth="1.5"/>
          </svg>
          <span className="brand-name">PlexMatch</span>
          <span className="brand-sub">Plex Media Renamer</span>
        </div>
        <div className="pipeline">
          {STAGES.map((s, i) => (
            <div key={s} className={`pipeline-stage ${i === stage ? "active" : ""} ${i < stage ? "done" : ""}`}>
              <div className="pipeline-dot">
                {i < stage ? (
                  <svg viewBox="0 0 12 12" fill="none">
                    <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  </svg>
                ) : (
                  <span>{i + 1}</span>
                )}
              </div>
              <span>{s}</span>
              {i < STAGES.length - 1 && <div className="pipeline-line" />}
            </div>
          ))}
        </div>
      </header>

      <div className="app-body">
        <aside className="sidebar">
          <Settings
            settings={settings}
            onChange={saveSettings}
            onScan={handleScan}
            onStop={stopScan}
            scanning={scanning}
          />
        </aside>

        <main className="main-content">
          {error && (
            <div className="error-banner">
              <svg viewBox="0 0 16 16" fill="none">
                <circle cx="8" cy="8" r="7" stroke="#FF5F5F" strokeWidth="1.5"/>
                <path d="M8 4.5v4M8 10.5v1" stroke="#FF5F5F" strokeWidth="1.5" strokeLinecap="round"/>
              </svg>
              <span>{error}</span>
              <button onClick={() => setError(null)}>✕</button>
            </div>
          )}

          {stage === 0 && (
            <div className="empty-state">
              <div className="empty-icon">
                <svg viewBox="0 0 64 64" fill="none">
                  <rect x="8" y="16" width="48" height="36" rx="3" stroke="#F5A623" strokeWidth="2"/>
                  <path d="M8 24h48" stroke="#F5A623" strokeWidth="2"/>
                  <path d="M20 32h24M20 38h16" stroke="#3A3D42" strokeWidth="2" strokeLinecap="round"/>
                  <circle cx="48" cy="48" r="12" fill="#1A1D21" stroke="#F5A623" strokeWidth="2"/>
                  <path d="M44 48l3 3 5-5" stroke="#F5A623" strokeWidth="2" strokeLinecap="round"/>
                </svg>
              </div>
              <h2>Configure and scan your library</h2>
              <p>Choose a library folder on the left, then click <strong>Scan Library</strong>. Open a group to change matching, folders, or API keys.</p>
            </div>
          )}

          {scanning && (
            <div className="scanning-state">
              {scanProgress.total > 0 ? (
                <>
                  <div className="scan-progress">
                    <div
                      className="scan-progress-fill"
                      style={{ width: `${Math.round((scanProgress.current / scanProgress.total) * 100)}%` }}
                    />
                  </div>
                  <p>Matching against TMDB...</p>
                  <p className="scan-sub">
                    {scanProgress.current} of {scanProgress.total} files processed
                    {scanProgress.skipped?.length > 0 && ` · ${scanProgress.skipped.join(" and ")} not scanned`}
                  </p>
                </>
              ) : (
                <>
                  <div className="scan-animation">
                    <div className="scan-bar" />
                  </div>
                  <p>Discovering video files...</p>
                  <p className="scan-sub">This may take a moment for large libraries.</p>
                </>
              )}
              <button type="button" className="btn-secondary" onClick={stopScan}>
                Stop scan
              </button>
            </div>
          )}

          {!scanning && stage === 2 && proposals.length > 0 && (
            <ProposalTable
              proposals={proposals}
              onChange={setProposals}
              root={scanRoot}
              apiKey={settings.tmdb_api_key}
              omdbKey={settings.omdb_api_key}
              split={settings.split_libraries}
              embedIds={settings.embed_ids}
              restructure={settings.restructure_folders}
              aiSettings={{
                ai_enabled: settings.ai_enabled,
                ai_provider: settings.ai_provider,
                openai_api_key: settings.openai_api_key,
                anthropic_api_key: settings.anthropic_api_key,
                ai_model: settings.ai_model,
                openai_base_url: settings.openai_base_url,
              }}
            />
          )}

          {!scanning && stage === 2 && proposals.length === 0 && (
            <div className="empty-state">
              <h2>No video files found</h2>
              <p>Nothing to rename in <strong>{scanRoot}</strong>. Check the folder path and try again.</p>
              <button className="btn-secondary" onClick={() => setStage(0)}>← Back to settings</button>
            </div>
          )}

          {stage === 3 && (
            <div className="results-panel">
              <h2 className="results-heading">
                {lastBatch ? "Rename complete" : "Done"}
                <span className={`results-badge ${renameResults.filter(r => r.ok).length === renameResults.length ? "success" : "partial"}`}>
                  {renameResults.filter(r => r.ok).length} / {renameResults.length} succeeded
                </span>
              </h2>
              {resultNote && <p className="result-note">{resultNote}</p>}
              {plexLibraries.length > 0 && (
                <div className="library-paths">
                  <p className="library-paths-note">
                    {plexLibraries.length > 1
                      ? "Add each folder as its own Plex library."
                      : "Add this folder as its own Plex library."}
                  </p>
                  {plexLibraries.map(lib => (
                    <CopyPath key={lib.label} label={lib.label} path={lib.path} />
                  ))}
                </div>
              )}
              <div className="results-list">
                {renameResults.map((r, i) => (
                  <div key={i} className={`result-row ${r.ok ? "ok" : "fail"}`}>
                    <div className="result-icon">
                      {r.ok
                        ? <svg viewBox="0 0 12 12" fill="none"><path d="M2 6l3 3 5-5" stroke="#4ADE80" strokeWidth="1.5" strokeLinecap="round"/></svg>
                        : <svg viewBox="0 0 12 12" fill="none"><path d="M3 3l6 6M9 3l-6 6" stroke="#FF5F5F" strokeWidth="1.5" strokeLinecap="round"/></svg>
                      }
                    </div>
                    <div className="result-paths">
                      <span className="result-src">{r.src}</span>
                      <span className="result-arrow">→</span>
                      <span className="result-dst">{r.ok ? r.dst : r.error}</span>
                    </div>
                  </div>
                ))}
              </div>
              <div className="results-actions">
                <button className="btn-primary" onClick={() => { setStage(0); setProposals([]); setRenameResults([]); setResultNote(null) }}>
                  Scan another folder
                </button>
                {lastBatch && (
                  <button className="btn-secondary" onClick={handleUndo} disabled={undoing}>
                    {undoing ? "Undoing..." : "Undo this rename"}
                  </button>
                )}
              </div>
            </div>
          )}
        </main>
      </div>

      {stage === 2 && !scanning && proposals.length > 0 && (
        <footer className="app-footer">
          <div className="footer-stats">
            <span><strong>{proposals.length}</strong> files found</span>
            <span className="stat-dot" />
            <span><strong className="text-green">{matchedCount}</strong> matched</span>
            <span className="stat-dot" />
            <span><strong className="text-amber">{unmatchedCount}</strong> unmatched</span>
            {cleanupCount > 0 && (
              <>
                <span className="stat-dot" />
                <span><strong className="text-blue">{cleanupCount}</strong> name cleanup</span>
              </>
            )}
            {organisedCount > 0 && (
              <>
                <span className="stat-dot" />
                <span><strong className="text-green">{organisedCount}</strong> already organised</span>
              </>
            )}
            {conflictCount > 0 && (
              <>
                <span className="stat-dot" />
                <span><strong className="text-amber">{conflictCount}</strong> conflicts</span>
              </>
            )}
            {scanProgress.skipped?.length > 0 && (
              <>
                <span className="stat-dot" />
                <span>{scanProgress.skipped.join(" and ")} not scanned</span>
              </>
            )}
            <span className="stat-dot" />
            <span><strong>{approvedCount}</strong> included in rename</span>
          </div>
          <div className="footer-actions">
            <button className="btn-secondary" onClick={() => setStage(0)}>
              ← Back
            </button>
            <button
              className="btn-primary"
              onClick={handleRename}
              disabled={renaming || approvedCount === 0}
            >
              {renaming
                ? "Renaming..."
                : `${settings?.restructure_folders ? "Rename and move" : "Rename"} ${approvedCount} file${approvedCount !== 1 ? "s" : ""}`}
            </button>
          </div>
        </footer>
      )}
    </div>
  )
}
