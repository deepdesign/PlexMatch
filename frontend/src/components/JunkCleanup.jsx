import { useState, useEffect, useCallback, useRef } from "react"

const API = "http://localhost:5174"

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function matePath(file) {
  const lower = file.path.toLowerCase()
  if (lower.endsWith(".idx")) return file.path.slice(0, -4) + ".sub"
  if (lower.endsWith(".sub")) return file.path.slice(0, -4) + ".idx"
  return null
}

function GroupCheck({ checked, indeterminate, onChange, label }) {
  const ref = useRef(null)
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate
  }, [indeterminate])
  return (
    <input
      ref={ref}
      type="checkbox"
      checked={checked}
      aria-label={label}
      onClick={e => e.stopPropagation()}
      onChange={onChange}
    />
  )
}

export default function JunkCleanup({ root, onClose }) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [files, setFiles] = useState([])
  const [kinds, setKinds] = useState([])
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
      setKinds(data.kinds || [])
      setFiles((data.files || []).map(f => ({ ...f, selected: !!f.default_selected })))
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [root])

  useEffect(() => { load() }, [load])

  const toggle = (path) => {
    setFiles(prev => {
      const file = prev.find(f => f.path === path)
      if (!file) return prev
      const next = !file.selected
      const mate = file.kind_id === "vobsub" ? matePath(file) : null
      const mateKey = mate ? mate.toLowerCase() : null
      return prev.map(f => {
        if (f.path === path) return { ...f, selected: next }
        if (mateKey && f.path.toLowerCase() === mateKey) return { ...f, selected: next }
        return f
      })
    })
  }

  const setGroup = (kindId, value) =>
    setFiles(prev => prev.map(f => f.kind_id === kindId ? { ...f, selected: value } : f))

  const setAll = (value) =>
    setFiles(prev => prev.map(f => ({ ...f, selected: value })))

  const applyDefaults = () =>
    setFiles(prev => prev.map(f => ({ ...f, selected: !!f.default_selected })))

  const selected = files.filter(f => f.selected)
  const groups = (kinds.length ? kinds : []).map(kind => ({
    ...kind,
    items: files.filter(f => f.kind_id === kind.id),
  })).filter(group => group.items.length)

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
      <div className="modal junk-modal" onClick={e => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Clean up junk files</h3>
          <button className="modal-close" onClick={onClose} title="Close">✕</button>
        </div>

        <p className="modal-sub">
          Checked files will be deleted. Sample videos and pictures are included. Text subtitles (<code>.srt</code>) are never listed.
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
            <div className="orphan-empty">No leftover files found.</div>
          ) : (
            <>
              <div className="orphan-toolbar">
                <span>{selected.length} of {files.length} selected</span>
                <div>
                  <button className="btn-ghost" onClick={applyDefaults}>Safe defaults</button>
                  <button className="btn-ghost" onClick={() => setAll(true)}>Select all</button>
                  <button className="btn-ghost" onClick={() => setAll(false)}>Select none</button>
                </div>
              </div>
              <div className="junk-groups">
                {groups.map(group => {
                  const picked = group.items.filter(f => f.selected).length
                  const all = picked === group.items.length
                  return (
                    <section key={group.id} className={`junk-group${group.default_selected ? "" : " keep"}`}>
                      <div
                        className="junk-group-head"
                        onClick={() => setGroup(group.id, !all)}
                      >
                        <GroupCheck
                          checked={all}
                          indeterminate={picked > 0 && !all}
                          label={`${group.label} files`}
                          onChange={() => setGroup(group.id, !all)}
                        />
                        <div>
                          <div className="junk-group-title">{group.label}</div>
                          <p className="junk-group-detail">{group.detail}</p>
                        </div>
                        <span className={`junk-badge${group.default_selected ? "" : " keep"}`}>
                          {group.default_selected ? "Safe to delete" : "Leave these"}
                          <span className="junk-count">{picked} of {group.items.length}</span>
                        </span>
                      </div>
                      <div className="junk-files">
                        {group.items.map(f => (
                          <label key={f.path} className="junk-file">
                            <input
                              type="checkbox"
                              checked={f.selected}
                              onChange={() => toggle(f.path)}
                            />
                            <span className="orphan-name" title={f.rel_path}>{f.rel_path}</span>
                            <span className="orphan-meta">{formatSize(f.size)}</span>
                          </label>
                        ))}
                      </div>
                    </section>
                  )
                })}
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
