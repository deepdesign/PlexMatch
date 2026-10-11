import { useState, useMemo } from "react"

const API = "http://localhost:5174"

const CONFIDENCE_LABEL = (c) => {
  if (c >= 0.85) return { label: "High", cls: "conf-high" }
  if (c >= 0.6) return { label: "Medium", cls: "conf-med" }
  if (c > 0) return { label: "Low", cls: "conf-low" }
  return { label: "None", cls: "conf-none" }
}

function ConfidencePip({ value }) {
  const { label, cls } = CONFIDENCE_LABEL(value)
  const pct = Math.round(value * 100)
  return (
    <div className={`confidence ${cls}`}>
      <div className="conf-bar">
        <div className="conf-fill" style={{ width: `${pct}%` }} />
      </div>
      <span>{pct}% {label}</span>
    </div>
  )
}

function ProposalRow({ proposal, index, onChange, onRemove, root, apiKey, omdbKey, split, embedIds, restructure, aiSettings, nested, proposals = [] }) {
  const [expanded, setExpanded] = useState(false)
  const [manualTitle, setManualTitle] = useState("")
  const [manualYear, setManualYear] = useState("")
  const [linkUrl, setLinkUrl] = useState("")
  const [busy, setBusy] = useState(false)
  const [searchError, setSearchError] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const stayFolder = proposal.folder && proposal.folder !== "." ? proposal.folder : ""
  const shownFolder = restructure ? proposal.proposed_folder : stayFolder

  const toggle = () => {
    onChange(index, { ...proposal, approved: !proposal.approved })
  }

  // Ask the backend to rebuild a proposal (correct root-prefixed full path,
  // episode-name lookup for TV, etc). Handles both alt selection and manual search.
  const rematch = async (payload) => {
    setBusy(true)
    setSearchError(null)
    try {
      const res = await fetch(`${API}/api/rematch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root_folder: root,
          tmdb_api_key: apiKey,
          full_path: proposal.full_path,
          rel_path: proposal.rel_path,
          filename: proposal.filename,
          folder: proposal.folder,
          ext: proposal.ext,
          parsed: proposal.parsed,
          split_libraries: split,
          embed_ids: embedIds,
          restructure_folders: restructure,
          omdb_api_key: omdbKey,
          ...aiSettings,
          matched_title: proposal.matched_title,
          confidence: proposal.confidence,
          alternatives: proposal.alternatives,
          ...payload,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Search failed")
      if (!data.matched) {
        setSearchError(data.message || (payload.ai
          ? "AI could not suggest a confident match. Try manual search or Use as-is."
          : "No TMDB match found. Try a different title or add a year."))
        return
      }
      onChange(index, {
        ...proposal,
        ...data,
        // alt selection keeps the alternatives list; manual search replaces it
        selectedAlt: payload.tmdb_id ? { title: data.matched_title, year: data.matched_year, type: data.type } : null,
        approved: true,
      })
      setExpanded(false)
    } catch (e) {
      setSearchError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const selectAlt = (alt) =>
    rematch({ tmdb_id: alt.tmdb_id, media_type: alt.type, confidence: alt.confidence })

  const manualSearch = () => {
    if (!manualTitle.trim()) return
    rematch({ manual_title: manualTitle.trim(), manual_year: manualYear.trim() })
  }

  const applyLink = () => {
    if (!linkUrl.trim()) return
    rematch({ link: linkUrl.trim() })
  }

  const askAi = () => rematch({
    ai: true,
    rejected_title: proposal.matched_title,
    rejected_tmdb_id: proposal.tmdb_id,
    siblings: proposals
      .filter(p => p.folder === proposal.folder && p.filename !== proposal.filename)
      .map(p => p.filename),
  })

  const aiReady = aiSettings?.ai_enabled && aiSettings.ai_provider && (
    (aiSettings.ai_provider === "openai" && aiSettings.openai_api_key) ||
    (aiSettings.ai_provider === "anthropic" && aiSettings.anthropic_api_key)
  )

  // Rebuild the name locally (no TMDB) using the typed title/year. Useful for
  // titles not in any database, or just to add a missing year for Plex matching.
  const applyLocal = async () => {
    setBusy(true)
    setSearchError(null)
    try {
      const res = await fetch(`${API}/api/rematch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          root_folder: root,
          full_path: proposal.full_path,
          rel_path: proposal.rel_path,
          filename: proposal.filename,
          folder: proposal.folder,
          ext: proposal.ext,
          parsed: proposal.parsed,
          split_libraries: split,
          embed_ids: embedIds,
          restructure_folders: restructure,
          local: true,
          manual_title: manualTitle.trim(),
          manual_year: manualYear.trim(),
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Could not rebuild name")
      onChange(index, { ...proposal, ...data, selectedAlt: null, approved: true })
      setExpanded(false)
    } catch (e) {
      setSearchError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const deleteSource = async () => {
    setBusy(true)
    setSearchError(null)
    try {
      const res = await fetch(`${API}/api/delete-files`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root_folder: root, files: [proposal.full_path] }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Delete failed")
      const r = data.results?.[0]
      if (!r?.ok) throw new Error(r?.error || "Could not delete file")
      onRemove(index)
    } catch (e) {
      setSearchError(e.message)
      setBusy(false)
    }
  }

  const isUnmatched = proposal.status === "unmatched"
  const isError = proposal.status === "error"
  const isOrganised = proposal.status === "organised"
  const isConflict = proposal.status === "conflict"
  const isCleanup = proposal.status === "cleanup"
  const isWeb = isCleanup && proposal.source === "wikipedia"
  const isAiSource = proposal.source === "ai" || proposal.source === "ai+tmdb"

  return (
    <>
      <tr className={`proposal-row ${nested ? "row-episode" : ""} ${isOrganised ? "row-organised" : isConflict ? "row-conflict" : proposal.approved ? "row-approved" : ""} ${isUnmatched ? "row-unmatched" : ""}`}>
        <td className="col-approve">
          {isOrganised && (
            <span className="organised-icon" title="Already in Plex format - no rename needed">
              <svg viewBox="0 0 14 14" fill="none"><path d="M2 7l4 4 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
            </span>
          )}
          {isConflict && (
            <span className="conflict-icon" title="A file already exists at the target location">!</span>
          )}
          {!isOrganised && !isConflict && !isUnmatched && !isError && (
            <button
              className={`approve-btn ${proposal.approved ? "approved" : "waiting"}`}
              onClick={toggle}
              title={proposal.approved
                ? "Included in the rename. Click to leave this file as it is."
                : "Not included. Click to include this file when you click Rename."}
            >
              {proposal.approved && (
                <svg viewBox="0 0 14 14" fill="none"><path d="M2 7l4 4 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
              )}
            </button>
          )}
          {!isOrganised && !isConflict && (isUnmatched || isError) && (
            <span className="unmatched-icon" title="No TMDB match found">?</span>
          )}
        </td>

        <td className="col-original">
          <span className="path-text">{proposal.rel_path}</span>
        </td>

        <td className="col-arrow">→</td>

        <td className="col-proposed">
          {isUnmatched ? (
            <span className="unmatched-label">No match found</span>
          ) : isError ? (
            <span className="error-label">{proposal.error}</span>
          ) : (
            <div className="proposed-names">
              {isOrganised && <span className="organised-label">Already organised</span>}
              {isConflict && <span className="conflict-label">Target already exists - duplicate or wrong match</span>}
              {isAiSource && proposal.ai_reason && (
                <span className="ai-reason-label" title={proposal.ai_reason}>AI: {proposal.ai_reason}</span>
              )}
              {isCleanup && (
                <span className="cleanup-label">
                  {isWeb
                    ? "Suggestion from Wikipedia. Nothing is renamed until you include it."
                    : "Suggested tidy name. No TMDB match. Nothing is renamed until you include it."}
                </span>
              )}
              {proposal.missing_year && (
                <span className="missing-year-label" title="Plex matches best with a year. Expand to add one.">
                  No year yet. Expand the row to add one.
                </span>
              )}
              {shownFolder && <span className="proposed-folder">{shownFolder}/</span>}
              <span className="proposed-file">{proposal.proposed_filename}</span>
            </div>
          )}
        </td>

        <td className="col-confidence">
          {isAiSource ? (
            <span className="ai-badge" title={proposal.source === "ai+tmdb" ? "AI suggested title, matched on TMDB" : "AI-suggested name"}>
              AI{proposal.source === "ai+tmdb" ? "+TMDB" : ""}
            </span>
          ) : isCleanup ? (
            isWeb
              ? <span className="web-badge" title="Title/year from Wikipedia">Web</span>
              : <span className="local-badge" title="Named from the filename/folder; not verified against TMDB">Local</span>
          ) : !isUnmatched && !isError && (
            <ConfidencePip value={proposal.confidence} />
          )}
        </td>

        <td className="col-tmdb">
          {proposal.tmdb_url ? (
            <a href={proposal.tmdb_url} target="_blank" rel="noreferrer" className="tmdb-link">
              TMDB ↗
            </a>
          ) : proposal.imdb_url ? (
            <a href={proposal.imdb_url} target="_blank" rel="noreferrer" className="tmdb-link">
              IMDb ↗
            </a>
          ) : proposal.wiki_url && (
            <a href={proposal.wiki_url} target="_blank" rel="noreferrer" className="tmdb-link">
              Wiki ↗
            </a>
          )}
        </td>

        <td className="col-expand">
          <button
            className={`expand-btn ${expanded ? "expanded" : ""}`}
            onClick={() => setExpanded(!expanded)}
            title={isUnmatched || isError ? "Search manually" : "Alternatives / manual search"}
          >
            <svg viewBox="0 0 12 12" fill="none">
              <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          </button>
        </td>
      </tr>

      {expanded && (
        <tr className="alt-row">
          <td colSpan={7}>
            <div className="alt-panel">
              {isConflict && (
                <div className="conflict-resolve">
                  <span className="alt-heading">A file already exists at the target</span>
                  <p className="conflict-help">
                    If this source is a leftover duplicate of an already-organised title,
                    delete it. If it was matched to the wrong title, search again below.
                  </p>
                  {!confirmDelete ? (
                    <button className="btn-danger" onClick={() => setConfirmDelete(true)} disabled={busy}>
                      Delete this duplicate source file
                    </button>
                  ) : (
                    <div className="confirm-inline">
                      <span>Permanently delete the source file?</span>
                      <button className="btn-ghost" onClick={() => setConfirmDelete(false)} disabled={busy}>Cancel</button>
                      <button className="btn-danger" onClick={deleteSource} disabled={busy}>
                        {busy ? "Deleting..." : "Yes, delete"}
                      </button>
                    </div>
                  )}
                </div>
              )}

              {proposal.alternatives?.length > 0 && (
                <>
                  <span className="alt-heading">Alternative matches - click to use</span>
                  <div className="alt-list">
                    {proposal.alternatives.map((alt, i) => (
                      <button
                        key={i}
                        className="alt-item"
                        onClick={() => selectAlt(alt)}
                        disabled={busy}
                      >
                        <span className="alt-title">{alt.title}</span>
                        <span className="alt-year">{alt.year}</span>
                        <span className={`alt-type ${alt.type}`}>{alt.type === "movie" ? "Film" : "TV Series"}</span>
                        <ConfidencePip value={alt.confidence} />
                      </button>
                    ))}
                  </div>
                </>
              )}

              <div className="manual-search">
                <span className="alt-heading">Paste a link, search, or use as-is</span>
                <div className="manual-search-row">
                  <input
                    className="search-input manual-link"
                    placeholder="TMDB or IMDb link"
                    value={linkUrl}
                    onChange={e => setLinkUrl(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter") applyLink() }}
                  />
                  <button
                    className="btn-secondary"
                    onClick={applyLink}
                    disabled={busy || !linkUrl.trim()}
                    title="Use this TMDB movie, TMDB series, or IMDb title"
                  >
                    Use link
                  </button>
                </div>
                <div className="manual-search-row">
                  <input
                    className="search-input manual-title"
                    placeholder="Correct title (e.g. Apollo 11)"
                    value={manualTitle}
                    onChange={e => setManualTitle(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter") manualSearch() }}
                  />
                  <input
                    className="search-input manual-year"
                    placeholder="Year"
                    value={manualYear}
                    onChange={e => setManualYear(e.target.value)}
                    onKeyDown={e => { if (e.key === "Enter") manualSearch() }}
                  />
                  <button
                    className="btn-primary"
                    onClick={manualSearch}
                    disabled={busy || !manualTitle.trim()}
                  >
                    {busy ? "Searching..." : "Search"}
                  </button>
                  <button
                    className="btn-secondary"
                    onClick={applyLocal}
                    disabled={busy}
                    title="Build the name from your title/year without checking TMDB (good for titles not in any database, or to add a missing year)"
                  >
                    Use as-is
                  </button>
                  <button
                    className="btn-secondary"
                    onClick={askAi}
                    disabled={busy || !aiReady}
                    title={aiReady
                      ? "Look this file up as a series again. AI is asked only if that search still returns the current match"
                      : "Turn on AI in Settings and add an API key"}
                  >
                    {busy ? "…" : "Ask AI"}
                  </button>
                </div>
                {searchError && <span className="field-error">{searchError}</span>}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function needsAttention(p) {
  if (p.status === "conflict" || p.status === "unmatched" || p.status === "error") return true
  if (p.missing_year || p.needs_choice) return true
  if (p.status === "cleanup" && (p.episode || p.season)) return true
  return p.confidence > 0 && p.confidence < 0.6 && p.status !== "organised"
}

function seasonKey(p) {
  const season = p.season ?? p.parsed?.season
  if (season == null) return null
  if (p.type !== "tv" && !p.parsed?.is_tv) return null
  const title = (p.matched_title || p.parsed?.title || "").trim().toLowerCase()
  if (!title) return null
  const year = p.matched_year || p.parsed?.year || ""
  return `${title}|${year}|${season}`
}

function groupRows(list) {
  const buckets = new Map()
  const order = []
  list.forEach((proposal, index) => {
    const key = seasonKey(proposal)
    if (!key) {
      order.push({ kind: "file", proposal, index })
      return
    }
    let group = buckets.get(key)
    if (!group) {
      group = { kind: "season", key, items: [] }
      buckets.set(key, group)
      order.push(group)
    }
    group.items.push({ proposal, index })
  })
  return order.flatMap(row => (
    row.kind === "season" && row.items.length < 2
      ? [{ kind: "file", proposal: row.items[0].proposal, index: row.items[0].index }]
      : [row]
  ))
}

const SEASON_DIR = /^(season[\s._-]*\d+|series[\s._-]*\d+|specials|s\d{1,2})$/i

function seriesKey(p) {
  const isTv = p.type === "tv" || p.parsed?.is_tv || p.season != null || p.parsed?.season != null
  if (!isTv) return null
  const parts = String(p.folder || "").replace(/\\/g, "/").split("/").filter(Boolean)
  while (parts.length && SEASON_DIR.test(parts[parts.length - 1])) parts.pop()
  if (parts.length) return `folder:${parts.join("/").toLowerCase()}`
  const title = (p.matched_title || p.parsed?.title || "").trim().toLowerCase()
  if (!title) return null
  return `title:${title}`
}

function seriesLabel(proposal) {
  const title = proposal.matched_title || proposal.parsed?.title
  const year = proposal.matched_year || proposal.parsed?.year
  if (title) return `${title}${year ? ` (${year})` : ""}`
  const parts = String(proposal.folder || "").replace(/\\/g, "/").split("/").filter(Boolean)
  while (parts.length && SEASON_DIR.test(parts[parts.length - 1])) parts.pop()
  return parts[parts.length - 1] || "TV series"
}

function groupSeries(rows) {
  const blocks = []
  const at = new Map()
  for (const row of rows) {
    const sample = row.kind === "season" ? row.items[0].proposal : row.proposal
    const key = seriesKey(sample)
    if (!key) {
      blocks.push(row)
      continue
    }
    let block = at.get(key)
    if (!block) {
      block = { kind: "series", key, rows: [], count: 0 }
      at.set(key, block)
      blocks.push(block)
    }
    block.rows.push(row)
    block.count += row.kind === "season" ? row.items.length : 1
  }
  return blocks.flatMap(block => (
    block.kind === "series" && block.count < 2 ? block.rows : [block]
  ))
}

const APPROVABLE_ROW = new Set(["pending", "cleanup"])

function SeasonGroup({ group, rowProps, onSetApproved }) {
  const [open, setOpen] = useState(false)
  const first = group.items[0].proposal
  const title = first.matched_title || first.parsed?.title || "Unknown show"
  const year = first.matched_year || first.parsed?.year
  const season = first.season ?? first.parsed?.season
  const approvable = group.items.filter(item => APPROVABLE_ROW.has(item.proposal.status))
  const approvedCount = approvable.filter(item => item.proposal.approved).length
  const allOn = approvable.length > 0 && approvedCount === approvable.length
  const mixed = approvedCount > 0 && !allOn
  const label = `${title}${year ? ` (${year})` : ""} · Season ${String(season).padStart(2, "0")}`

  const toggle = () => {
    onSetApproved(approvable.map(item => item.proposal.full_path), !allOn)
  }

  return (
    <>
      <tr className="season-row">
        <td className="col-approve">
          {approvable.length === 0 ? (
            <span className="organised-icon" title="Nothing in this season needs a rename">
              <svg viewBox="0 0 14 14" fill="none"><path d="M2 7l4 4 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
            </span>
          ) : (
            <button
              className={`approve-btn ${allOn ? "approved" : mixed ? "mixed" : "waiting"}`}
              onClick={toggle}
              title={allOn
                ? "Every episode is included. Click to leave this season as it is."
                : "Include every episode in this season in the rename."}
            >
              {allOn
                ? <svg viewBox="0 0 14 14" fill="none"><path d="M2 7l4 4 6-6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
                : mixed
                  ? <svg viewBox="0 0 14 14" fill="none"><path d="M3 7h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>
                  : null
              }
            </button>
          )}
        </td>
        <td className="col-original">
          <button type="button" className="season-toggle" onClick={() => setOpen(value => !value)}>
            <svg className={`season-chevron ${open ? "open" : ""}`} viewBox="0 0 12 12" fill="none">
              <path d="M4 2l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
            <span>{label}</span>
          </button>
        </td>
        <td className="col-arrow" />
        <td className="col-proposed">
          <span className="season-count">{group.items.length} episodes</span>
        </td>
        <td className="col-confidence" />
        <td className="col-tmdb" />
        <td className="col-expand">
          <button type="button" className="expand-btn" onClick={() => setOpen(value => !value)} title={open ? "Collapse season" : "Show episodes"}>
            <svg viewBox="0 0 12 12" fill="none" style={{ transform: open ? "rotate(180deg)" : "none" }}>
              <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
            </svg>
          </button>
        </td>
      </tr>
      {open && group.items.map(item => (
        <ProposalRow
          key={item.proposal.full_path}
          proposal={item.proposal}
          index={item.index}
          nested
          {...rowProps}
        />
      ))}
    </>
  )
}

async function rematchFile(proposal, payload, { root, apiKey, omdbKey, split, embedIds, restructure, aiSettings }) {
  const res = await fetch(`${API}/api/rematch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      root_folder: root,
      tmdb_api_key: apiKey,
      full_path: proposal.full_path,
      rel_path: proposal.rel_path,
      filename: proposal.filename,
      folder: proposal.folder,
      ext: proposal.ext,
      parsed: proposal.parsed,
      split_libraries: split,
      embed_ids: embedIds,
      restructure_folders: restructure,
      omdb_api_key: omdbKey,
      ...aiSettings,
      matched_title: proposal.matched_title,
      confidence: proposal.confidence,
      alternatives: proposal.alternatives,
      ...payload,
    }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.error || "Search failed")
  return data
}

function SeriesBlock({ block, proposals, onChange, rowProps, onSetApproved }) {
  const [linkUrl, setLinkUrl] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const members = proposals.filter(p => seriesKey(p) === block.key)
  const first = members[0] || (block.rows[0].kind === "season" ? block.rows[0].items[0].proposal : block.rows[0].proposal)
  const label = seriesLabel(first)

  const apply = async () => {
    const link = linkUrl.trim()
    if (!link || busy) return
    setBusy(true)
    setError(null)
    const updates = new Map()
    let failed = 0
    let message = ""
    const queue = [...members]
    const workers = Array.from({ length: Math.min(3, queue.length) }, async () => {
      while (queue.length) {
        const proposal = queue.shift()
        try {
          const data = await rematchFile(proposal, { link, series: true }, rowProps)
          if (!data.matched) {
            failed += 1
            message = data.message || "No TMDB match found."
            continue
          }
          updates.set(proposal.full_path, data)
        } catch (e) {
          failed += 1
          message = e.message
        }
      }
    })
    await Promise.all(workers)
    if (updates.size) {
      onChange(proposals.map(p => (
        updates.has(p.full_path) ? { ...p, ...updates.get(p.full_path), approved: true } : p
      )))
    }
    setError(failed ? message : null)
    setBusy(false)
  }

  return (
    <>
      <tr className="series-row">
        <td className="col-approve" />
        <td className="col-original">
          <span className="series-label">{label}</span>
          <span className="season-count">{members.length} episodes</span>
        </td>
        <td className="col-arrow" />
        <td className="col-proposed" colSpan={3}>
          <div className="series-link">
            <input
              className="search-input"
              placeholder="TMDB or IMDb series link"
              value={linkUrl}
              onChange={e => setLinkUrl(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") apply() }}
              disabled={busy}
            />
            <button
              type="button"
              className="btn-secondary"
              onClick={apply}
              disabled={busy || !linkUrl.trim()}
              title="Set this show for every episode. Each file keeps its own season and episode number."
            >
              {busy ? "Matching..." : "Use for series"}
            </button>
            {error && <span className="series-link-error">{error}</span>}
          </div>
        </td>
        <td className="col-expand" />
      </tr>
      {block.rows.map(row => (
        row.kind === "season" ? (
          <SeasonGroup
            key={row.key}
            group={row}
            rowProps={rowProps}
            onSetApproved={onSetApproved}
          />
        ) : (
          <ProposalRow
            key={row.proposal.full_path}
            proposal={row.proposal}
            index={row.index}
            {...rowProps}
          />
        )
      ))}
    </>
  )
}

export default function ProposalTable({ proposals, onChange, root, apiKey, omdbKey, split, embedIds, restructure, aiSettings }) {
  const [filter, setFilter] = useState(() => (proposals.some(needsAttention) ? "attention" : "all"))
  const [search, setSearch] = useState("")
  const [showOrganised, setShowOrganised] = useState(false)

  const organisedCount = useMemo(
    () => proposals.filter(p => p.status === "organised").length,
    [proposals]
  )

  const filtered = useMemo(() => {
    return proposals.filter(p => {
      if (!showOrganised && p.status === "organised") return false
      if (filter === "rename" && !(p.approved && APPROVABLE_ROW.has(p.status))) return false
      if (filter === "attention" && !needsAttention(p)) return false
      if (filter === "matched" && (!p.matched || p.status === "unmatched")) return false
      if (filter === "unmatched" && p.matched && p.status !== "unmatched") return false
      if (search) {
        const q = search.toLowerCase()
        return (
          p.rel_path?.toLowerCase().includes(q) ||
          p.proposed_filename?.toLowerCase().includes(q) ||
          p.matched_title?.toLowerCase().includes(q)
        )
      }
      return true
    })
  }, [proposals, filter, search, showOrganised])

  const updateRow = (index, updated) => {
    // index here is into filtered, map back to original
    const original = proposals.findIndex(p => p.full_path === filtered[index].full_path)
    const next = [...proposals]
    next[original] = updated
    onChange(next)
  }

  const removeRow = (index) => {
    const target = filtered[index].full_path
    onChange(proposals.filter(p => p.full_path !== target))
  }

  const approveAll = () => onChange(proposals.map(p =>
    p.status === "pending" || p.status === "cleanup" ? { ...p, approved: true } : p
  ))
  const rejectAll = () => onChange(proposals.map(p => ({ ...p, approved: false })))

  const setPathsApproved = (paths, approved) => {
    const want = new Set(paths)
    onChange(proposals.map(p => (
      want.has(p.full_path) && APPROVABLE_ROW.has(p.status) ? { ...p, approved } : p
    )))
  }

  const grouped = groupSeries(groupRows(filtered))
  const attentionCount = proposals.filter(needsAttention).length
  const includedCount = proposals.filter(p => p.approved && APPROVABLE_ROW.has(p.status)).length
  const rowProps = { onChange: updateRow, onRemove: removeRow, root, apiKey, omdbKey, split, embedIds, restructure, aiSettings, proposals }
  const tabs = [
    ["all", "All"],
    ["rename", `Rename (${includedCount})`],
    ["matched", "Matched"],
    ["attention", attentionCount ? `Needs attention (${attentionCount})` : "Needs attention"],
  ]
  const hint = filter === "attention"
    ? "Missing year, name clash, weak match, or an episode tidy-up."
    : filter === "rename"
    ? "These files will be renamed."
    : filter === "matched"
    ? "Matched a title. Check to include."
    : "Only checked files are renamed."

  return (
    <div className="proposal-panel">
      <div className="proposal-toolbar">
        <div className="toolbar-left">
          <input
            className="search-input"
            placeholder="Filter files..."
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
          <div className="filter-tabs">
            {tabs.map(([id, label]) => (
              <button
                key={id}
                className={`filter-tab ${filter === id ? "active" : ""}`}
                onClick={() => setFilter(id)}
              >
                {label}
              </button>
            ))}
          </div>
          {organisedCount > 0 && (
            <label className="organised-toggle" title="Files already in Plex format that need no rename">
              <input
                type="checkbox"
                checked={showOrganised}
                onChange={e => setShowOrganised(e.target.checked)}
              />
              Show {organisedCount} already organised
            </label>
          )}
        </div>
        <div className="toolbar-right">
          <button className="btn-ghost" onClick={approveAll} title="Include every suggested rename, including filename tidy-ups">Include all</button>
          <button className="btn-ghost" onClick={rejectAll} title="Leave every file as it is">Include none</button>
        </div>
      </div>
      <p className="review-hint">{hint}</p>

      <div className="table-scroll">
        <table className="proposal-table">
          <thead>
            <tr>
              <th className="col-approve"></th>
              <th className="col-original">Current path</th>
              <th className="col-arrow"></th>
              <th className="col-proposed">Proposed Plex name</th>
              <th className="col-confidence">Confidence</th>
              <th className="col-tmdb">TMDB</th>
              <th className="col-expand"></th>
            </tr>
          </thead>
          <tbody>
            {grouped.map(row => (
              row.kind === "series" ? (
                <SeriesBlock
                  key={row.key}
                  block={row}
                  proposals={proposals}
                  onChange={onChange}
                  rowProps={rowProps}
                  onSetApproved={setPathsApproved}
                />
              ) : row.kind === "season" ? (
                <SeasonGroup
                  key={row.key}
                  group={row}
                  rowProps={rowProps}
                  onSetApproved={setPathsApproved}
                />
              ) : (
                <ProposalRow
                  key={row.proposal.full_path}
                  proposal={row.proposal}
                  index={row.index}
                  {...rowProps}
                />
              )
            ))}
          </tbody>
        </table>

        {filtered.length === 0 && (
          <div className="table-empty">
            {filter === "rename"
              ? "Nothing is included yet. Open Needs attention and check the files you want Rename to move."
              : !showOrganised && organisedCount > 0 && proposals.length === organisedCount
              ? `All ${organisedCount} files are already organised - nothing to rename.`
              : "No files match the current filter."}
          </div>
        )}
      </div>
    </div>
  )
}
