import { useState, useEffect, useCallback } from "react"

const API = "http://localhost:5174"

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export default function JunkCleanup({ root, onClose }) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [files, setFiles] = useState([])
  const [deleting, setDeleting] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [results, setResults] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setResults(null)
    setConfirming(false)
    try {
      const res = await fetch(`${API}/api/junk-files`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root_folder: root }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Could not scan for junk files")
      setFiles(data.files.map(f => ({ ...f, selected: true })))
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [root])

  useEffect(() => { load() }, [load])

  const toggle = (i) =>
    setFiles(prev => prev.map((f, idx) => idx === i ? { ...f, selected: !f.selected } : f))
  const setAll = (val) =>
    setFiles(prev => prev.map(f => ({ ...f, selected: val })))

  const selected = files.filter(f => f.selected)

  const doDelete = async () => {
    setDeleting(true)
    setError(null)
    try {
      const res = await fetch(`${API}/api/delete-junk`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root_folder: root, files: selected.map(f => f.path) }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Delete failed")
      setResults(data.results)
      const deleted = new Set(data.results.filter(r => r.ok).map(r => r.path))
      setFiles(prev => prev.filter(f => !deleted.has(f.path)))
      setConfirming(false)
    } catch (e) {
      setError(e.message)
    } finally {
      setDeleting(false)
    }
  }

  const okCount = results?.filter(r => r.ok).length ?? 0
  const failCount = results ? results.length - okCount : 0

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal orphan-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Clean up junk files</h3>
          <button className="modal-close" onClick={onClose} title="Close">✕</button>
        </div>

        <p className="modal-sub">
          Readmes, shortcuts (<code>.url</code>, <code>.lnk</code>) and release leftovers
          such as checksums and torrent files. Posters, subtitles and <code>.nfo</code> files stay.
          Uncheck anything you want to keep.
        </p>

        {error && <div className="modal-error">{error}</div>}

        {results && (
          <div className="modal-results">
            Deleted {okCount} file{okCount !== 1 ? "s" : ""}.
            {failCount > 0 && ` ${failCount} could not be removed.`}
            {" "}Folders left empty can be removed with Clean up orphaned folders.
          </div>
        )}

        <div className="orphan-body">
          {loading ? (
            <div className="orphan-loading"><div className="spinner-sm" /> Scanning library...</div>
          ) : files.length === 0 ? (
            <div className="orphan-empty">No junk files found. Your library is clean.</div>
          ) : (
            <>
              <div className="orphan-toolbar">
                <span>{selected.length} of {files.length} selected</span>
                <div>
                  <button className="btn-ghost" onClick={() => setAll(true)}>Select all</button>
                  <button className="btn-ghost" onClick={() => setAll(false)}>Select none</button>
                </div>
              </div>
              <div className="orphan-list">
                {files.map((f, i) => (
                  <label key={f.path} className="orphan-item">
                    <input type="checkbox" checked={f.selected} onChange={() => toggle(i)} />
                    <span className="orphan-name">{f.rel_path}</span>
                    <span className="orphan-meta">{f.kind} · {formatSize(f.size)}</span>
                  </label>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="modal-footer">
          <button className="btn-secondary" onClick={onClose}>Close</button>
          {files.length > 0 && !confirming && (
            <button
              className="btn-danger"
              onClick={() => setConfirming(true)}
              disabled={selected.length === 0}
            >
              Delete {selected.length} file{selected.length !== 1 ? "s" : ""}
            </button>
          )}
          {confirming && (
            <div className="confirm-inline">
              <span>Permanently delete {selected.length} file{selected.length !== 1 ? "s" : ""}?</span>
              <button className="btn-ghost" onClick={() => setConfirming(false)} disabled={deleting}>Cancel</button>
              <button className="btn-danger" onClick={doDelete} disabled={deleting}>
                {deleting ? "Deleting..." : "Yes, delete"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
