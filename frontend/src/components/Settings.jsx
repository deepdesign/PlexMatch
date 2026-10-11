import { useState, useEffect } from "react"
import OrphanCleanup from "./OrphanCleanup"
import JunkCleanup from "./JunkCleanup"
import ApiKeysFields from "./ApiKeysModal"

const API = "http://localhost:5174"
const GROUPS_KEY = "plexmatch.sidebar"

function loadOpenGroups(hasKey) {
  try {
    const raw = localStorage.getItem(GROUPS_KEY)
    if (raw) {
      const saved = JSON.parse(raw)
      if (saved && typeof saved === "object") {
        return {
          matching: saved.matching !== false,
          output: !!saved.output,
          documentaries: !!saved.documentaries,
          keys: !!saved.keys,
          tools: !!saved.tools,
        }
      }
    }
  } catch {
    // A bad cache falls through to the first-run defaults.
  }
  return { matching: true, output: false, documentaries: false, keys: !hasKey, tools: false }
}

const AI_PROVIDERS = {
  openai: {
    label: "OpenAI",
    keyField: "openai_api_key",
    keyPlaceholder: "sk-…",
    keyUrl: "https://platform.openai.com/api-keys",
    models: [
      { id: "gpt-4o-mini", label: "GPT-4o mini", tag: "Best value",
        note: "Fast and very cheap. Plenty for reading filenames - the best choice for most libraries." },
      { id: "gpt-4.1-mini", label: "GPT-4.1 mini", tag: "Cheap",
        note: "Still cheap, and a little better than GPT-4o mini at unusual or ambiguous names." },
      { id: "gpt-4o", label: "GPT-4o", tag: "Smarter",
        note: "Better at obscure or foreign titles, but costs many times more per file. Slower scans." },
      { id: "gpt-4.1", label: "GPT-4.1", tag: "Smartest",
        note: "OpenAI's strongest option here, and the most expensive. Rarely needed just to read filenames." },
    ],
  },
  anthropic: {
    label: "Anthropic",
    keyField: "anthropic_api_key",
    keyPlaceholder: "sk-ant-…",
    keyUrl: "https://console.anthropic.com/settings/keys",
    models: [
      { id: "claude-3-5-haiku-20241022", label: "Claude 3.5 Haiku", tag: "Best value",
        note: "Fast and cheap. Plenty for reading filenames - the best choice for most libraries." },
      { id: "claude-sonnet-4-20250514", label: "Claude Sonnet 4", tag: "Smarter",
        note: "Better at obscure, foreign or badly named titles, but costs several times more per file." },
      { id: "claude-opus-4-20250514", label: "Claude Opus 4", tag: "Smartest",
        note: "Anthropic's strongest model, and the slowest and most expensive. Rarely needed just to read filenames." },
    ],
  },
}

function CheckIcon() {
  return (
    <svg className="key-chip-check" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M3.5 8.5l3 3 6-7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  )
}

function Toggle({ label, sub, checked, onClick, disabled }) {
  return (
    <label className={`toggle-row ${disabled ? "toggle-row--disabled" : ""}`}>
      <div className="toggle-info">
        <span className="toggle-label">{label}</span>
        <span className="toggle-sub">{sub}</span>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={!!checked}
        className={`toggle ${checked ? "toggle--on" : ""}`}
        onClick={() => { if (!disabled) onClick() }}
        disabled={disabled}
      />
    </label>
  )
}

function Accordion({ title, open, onToggle, summary, children }) {
  return (
    <section className="settings-section accordion">
      <button type="button" className="accordion-head" aria-expanded={open} onClick={onToggle}>
        <span className="settings-heading">{title}</span>
        <svg className={`accordion-chevron ${open ? "accordion-chevron--open" : ""}`} viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </button>
      {!open && summary}
      {open && <div className="accordion-body">{children}</div>}
    </section>
  )
}

export default function Settings({ settings, onChange, onScan, onStop, scanning }) {
  const [folderValid, setFolderValid] = useState(null)
  const [validating, setValidating] = useState(false)
  const [localFolder, setLocalFolder] = useState(settings.root_folder || "")
  const [picking, setPicking] = useState(false)
  const [pickError, setPickError] = useState(null)
  const [cleaningOrphans, setCleaningOrphans] = useState(false)
  const [cleaningJunk, setCleaningJunk] = useState(false)
  const [openGroups, setOpenGroups] = useState(() => loadOpenGroups(!!settings.tmdb_api_key))
  const toggleGroup = (id) => setOpenGroups(groups => ({ ...groups, [id]: !groups[id] }))

  useEffect(() => {
    try {
      localStorage.setItem(GROUPS_KEY, JSON.stringify(openGroups))
    } catch {
      // Private mode can refuse storage; the groups still work for this visit.
    }
  }, [openGroups])

  // Keep the input in sync when the stored folder changes (load, self-heal, pick)
  useEffect(() => {
    setLocalFolder(settings.root_folder || "")
  }, [settings.root_folder])

  const update = (key, value) => {
    onChange({ ...settings, [key]: value })
  }

  const toggleAi = () => {
    const enabled = !settings.ai_enabled
    onChange({
      ...settings,
      ai_enabled: enabled,
      ai_provider: enabled && !settings.ai_provider ? "openai" : settings.ai_provider,
    })
    if (enabled && !aiKey) setOpenGroups(groups => ({ ...groups, keys: true }))
  }

  const changeProvider = (provider) => {
    // Model names differ per provider, so reset to that provider's default
    onChange({ ...settings, ai_provider: provider, ai_model: "" })
  }

  useEffect(() => {
    if (settings.ai_enabled && !AI_PROVIDERS[settings.ai_provider]) {
      onChange({ ...settings, ai_provider: "openai", ai_model: "" })
    }
  }, [settings.ai_enabled, settings.ai_provider])

  const aiProvider = AI_PROVIDERS[settings.ai_provider] || AI_PROVIDERS.openai
  const aiKey = (settings[aiProvider.keyField] || "").trim()

  // Verify the AI key with the provider shortly after the user stops typing
  const [keyCheck, setKeyCheck] = useState({ status: "idle", error: null })
  useEffect(() => {
    if (!aiKey) {
      setKeyCheck({ status: "idle", error: null })
      return
    }
    let cancelled = false
    setKeyCheck({ status: "checking", error: null })
    const timer = setTimeout(async () => {
      try {
        const res = await fetch(`${API}/api/ai/verify-key`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            provider: settings.ai_provider || "openai",
            api_key: aiKey,
            base_url: settings.openai_base_url,
          }),
        })
        const data = await res.json()
        if (cancelled) return
        setKeyCheck(data.valid
          ? { status: "valid", error: null, models: data.models || [] }
          : { status: "invalid", error: data.error || "Key not recognised", models: [] })
      } catch {
        if (!cancelled) setKeyCheck({ status: "invalid", error: "Couldn't check the key - is the backend running?" })
      }
    }, 600)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [settings.ai_provider, settings.openai_base_url, aiKey])
  const aiModelId = settings.ai_model || aiProvider.models[0].id
  const accountModels = (keyCheck.models || [])
    .filter(id => !aiProvider.models.some(m => m.id === id))
    .map(id => ({
      id,
      label: id,
      tag: "Your account",
      note: "Available on your account. PlexMatch doesn't have a cost note for this one.",
    }))
  const listed = [...aiProvider.models, ...accountModels]
  const aiModels = listed.some(m => m.id === aiModelId)
    ? listed
    : [{ id: aiModelId, label: aiModelId, tag: "Custom",
         note: "A custom model name you entered. PlexMatch can't describe its cost or accuracy." },
       ...listed]
  const aiModel = aiModels.find(m => m.id === aiModelId)

  const validateFolder = async (path) => {
    if (!path) return
    setValidating(true)
    try {
      const res = await fetch(`${API}/api/validate-folder`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      })
      const data = await res.json()
      setFolderValid(data.exists)
      if (data.exists) update("root_folder", path)
    } catch {
      setFolderValid(false)
    } finally {
      setValidating(false)
    }
  }

  const pickFolder = async () => {
    setPicking(true)
    setPickError(null)
    try {
      const res = await fetch(`${API}/api/pick-folder`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initial: localFolder }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.error || "Could not open folder picker")
      if (data.cancelled || !data.path) return // user cancelled the dialog
      setLocalFolder(data.path)
      setFolderValid(true)
      update("root_folder", data.path)
    } catch (e) {
      setPickError(e.message)
    } finally {
      setPicking(false)
    }
  }

  const canScan = settings.tmdb_api_key && settings.root_folder && folderValid !== false

  const aiChipOk = aiKey && keyCheck.status === "valid"
  const matchSummary = [
    settings.ai_enabled ? "AI on" : "AI off",
    settings.web_lookup ? "Wikipedia" : null,
  ].filter(Boolean).join(" · ")
  const outputSummary = [
    settings.restructure_folders ? "Plex folders" : "Rename in place",
    settings.embed_ids && settings.restructure_folders ? "IDs" : null,
  ].filter(Boolean).join(" · ")
  const docsSummary = [
    settings.split_libraries ? "Movies and TV subfolders" : "Don't separate",
    settings.skip_output_folders !== false ? "Skip Movies/TV" : null,
  ].filter(Boolean).join(" · ")

  const keyBadges = (
    <div className="accordion-summary">
      <span className="setup-chips">
        <span className={`key-chip ${settings.tmdb_api_key ? "key-chip--ok" : "key-chip--missing"}`}>
          {settings.tmdb_api_key && <CheckIcon />} TMDB
        </span>
        {settings.omdb_api_key && <span className="key-chip key-chip--ok"><CheckIcon /> OMDb</span>}
        {aiKey && (
          <span className={`key-chip ${aiChipOk ? "key-chip--ok" : keyCheck.status === "invalid" ? "key-chip--missing" : ""}`}>
            {keyCheck.status === "checking" ? <div className="spinner-sm" /> : aiChipOk ? <CheckIcon /> : null}
            {aiProvider.label}
          </span>
        )}
      </span>
      <span className="setup-summary">
        {settings.tmdb_api_key ? "Saved on this machine" : "A free TMDB key is needed before you can scan"}
      </span>
    </div>
  )

  return (
    <div className="settings">
      <div className="settings-scroll">
        <div className="settings-section">
          <h3 className="settings-heading">Library</h3>
          <label className="field-label">Library folder</label>
          <div className="folder-input-row">
            <input
              type="text"
              className={`field-input ${folderValid === false ? "field-input--error" : folderValid === true ? "field-input--ok" : ""}`}
              placeholder="Click Browse to choose a folder"
              value={localFolder}
              onChange={e => {
                setLocalFolder(e.target.value)
                setFolderValid(null)
              }}
              onBlur={e => validateFolder(e.target.value)}
            />
            {validating && <div className="spinner-sm" />}
            {!validating && folderValid === true && (
              <svg className="field-check" viewBox="0 0 16 16" fill="none">
                <path d="M3 8l4 4 6-6" stroke="#4ADE80" strokeWidth="1.5" strokeLinecap="round"/>
              </svg>
            )}
            {!validating && folderValid === false && (
              <svg className="field-cross" viewBox="0 0 16 16" fill="none">
                <path d="M4 4l8 8M12 4l-8 8" stroke="#FF5F5F" strokeWidth="1.5" strokeLinecap="round"/>
              </svg>
            )}
          </div>
          <button className="btn-secondary btn-browse" onClick={pickFolder} disabled={picking}>
            {picking ? (
              <>
                <div className="spinner-sm" />
                Waiting for dialog...
              </>
            ) : (
              <>
                <svg viewBox="0 0 16 16" fill="none">
                  <path d="M2 4.5A1.5 1.5 0 013.5 3h3l1.5 1.5h4.5A1.5 1.5 0 0114 6v6a1.5 1.5 0 01-1.5 1.5h-9A1.5 1.5 0 012 12V4.5z" stroke="currentColor" strokeWidth="1.2"/>
                </svg>
                Browse...
              </>
            )}
          </button>
          {pickError && <span className="field-error">{pickError}</span>}
          {folderValid === false && (
            <span className="field-error">Folder not found. Check the path is accessible from this machine.</span>
          )}
          {folderValid === null && !pickError && (
            <span className="field-hint">Type a path, or click Browse to pick a folder</span>
          )}
        </div>

        <Accordion
          title="Matching"
          open={openGroups.matching}
          onToggle={() => toggleGroup("matching")}
          summary={<span className="setup-summary">{matchSummary}</span>}
        >
          <Toggle
            label="Auto-pick top match"
            sub="On approves the top match. Off leaves a weak or close call unchecked so you can confirm it"
            checked={settings.auto_pick_top}
            onClick={() => update("auto_pick_top", !settings.auto_pick_top)}
          />
          <Toggle
            label="Use AI when TMDB/OMDb can't match"
            sub="Runs only on weak matches with your own OpenAI or Anthropic key; results are cached per file"
            checked={!!settings.ai_enabled}
            onClick={toggleAi}
          />
          {settings.ai_enabled && (
            <div className="ai-options">
              {keyCheck.status === "invalid" && (
                <button type="button" className="ai-key-warn" onClick={() => setOpenGroups(groups => ({ ...groups, keys: true }))}>
                  {aiProvider.label} key isn&apos;t working. Open API keys →
                </button>
              )}
              <label className="field-label" htmlFor="ai-model">{aiProvider.label} model</label>
              <select
                id="ai-model"
                className="field-input"
                value={aiModelId}
                onChange={e => update("ai_model", e.target.value)}
              >
                {aiModels.map(m => (
                  <option key={m.id} value={m.id}>{m.label} · {m.tag}</option>
                ))}
              </select>
              {aiModel && <span className="field-hint model-note">{aiModel.note}</span>}
            </div>
          )}
          <Toggle
            label="Look up unmatched titles on Wikipedia"
            sub="Use Wikipedia for a canonical title/year (and episode names) when TMDB fails"
            checked={settings.web_lookup}
            onClick={() => update("web_lookup", !settings.web_lookup)}
          />
          <Toggle
            label="Clean names without a match"
            sub="Tidy filenames/folders from parsed info even when nothing matches"
            checked={settings.clean_unmatched}
            onClick={() => update("clean_unmatched", !settings.clean_unmatched)}
          />
        </Accordion>

        <Accordion
          title="Plex output"
          open={openGroups.output}
          onToggle={() => toggleGroup("output")}
          summary={<span className="setup-summary">{outputSummary}</span>}
        >
          <Toggle
            label="Restructure into Plex folders"
            sub={<>Move files into <code>Title (Year)/</code> subfolders. Off keeps each file in its current folder and only renames it.</>}
            checked={settings.restructure_folders}
            onClick={() => update("restructure_folders", !settings.restructure_folders)}
          />
          <Toggle
            label="Add TMDB/IMDb IDs to folder names"
            sub={settings.restructure_folders
              ? <>Append <code>{"{tmdb-…}"}</code> so Plex matches exactly (recommended for obscure titles)</>
              : "Only applies when files move into Plex folders."}
            checked={settings.embed_ids}
            disabled={!settings.restructure_folders}
            onClick={() => update("embed_ids", !settings.embed_ids)}
          />
          <Toggle
            label="Clean up empty folders"
            sub="Remove source folders left empty after files move out"
            checked={settings.clean_empty_folders}
            onClick={() => update("clean_empty_folders", !settings.clean_empty_folders)}
          />
        </Accordion>

        <Accordion
          title="Documentaries"
          open={openGroups.documentaries}
          onToggle={() => toggleGroup("documentaries")}
          summary={<span className="setup-summary">{docsSummary}</span>}
        >
          <Toggle
            label="Separate into movies and TV subfolders"
            sub={settings.restructure_folders
              ? <>One-off documentaries go in <code>Movies/</code>. Documentary series go in <code>TV/</code>.</>
              : "Only applies when files move into Plex folders."}
            checked={settings.split_libraries}
            disabled={!settings.restructure_folders}
            onClick={() => update("split_libraries", !settings.split_libraries)}
          />
          <Toggle
            label="Skip Movies and TV subfolders"
            sub="Leave finished files in those subfolders out of the next scan. Episodes with no title are still checked."
            checked={settings.skip_output_folders !== false}
            onClick={() => update("skip_output_folders", !(settings.skip_output_folders !== false))}
          />
        </Accordion>

        <Accordion
          title="API keys"
          open={openGroups.keys}
          onToggle={() => toggleGroup("keys")}
          summary={keyBadges}
        >
          <ApiKeysFields
            settings={settings}
            update={update}
            providers={AI_PROVIDERS}
            aiProvider={aiProvider}
            changeProvider={changeProvider}
            keyCheck={keyCheck}
            focus={settings.ai_enabled && !aiKey ? "ai" : "keys"}
            active={openGroups.keys}
          />
        </Accordion>

        <Accordion
          title="Tools"
          open={openGroups.tools}
          onToggle={() => toggleGroup("tools")}
          summary={<span className="setup-summary">Orphan folders and junk files</span>}
        >
          {settings.root_folder && folderValid !== false ? (
            <div className="tool-list">
              <div className="tool-item">
                <button
                  type="button"
                  className="btn-secondary btn-cleanup"
                  onClick={() => setCleaningOrphans(true)}
                  title="Find and delete folders that contain no video files"
                >
                  <svg viewBox="0 0 16 16" fill="none">
                    <path d="M3 4h10M6 4V3a1 1 0 011-1h2a1 1 0 011 1v1M5 4l.5 9a1 1 0 001 1h3a1 1 0 001-1L11 4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
                  </svg>
                  Clean up orphaned folders
                </button>
                <span className="toggle-sub">Folders with no video files, only notes or images.</span>
              </div>
              <div className="tool-item">
                <button
                  type="button"
                  className="btn-secondary btn-cleanup"
                  onClick={() => setCleaningJunk(true)}
                  title="Choose readmes, release files, NFO and picture subtitles to delete"
                >
                  <svg viewBox="0 0 16 16" fill="none">
                    <path d="M4 2.5h5l3 3V13.5H4v-11z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round"/>
                    <path d="M9 2.5V6h3M6 8.5h4M6 11h2.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
                  </svg>
                  Clean up junk files
                </button>
                <span className="toggle-sub">Readmes and release files start selected. NFO and picture subtitles stay unless you include them.</span>
              </div>
            </div>
          ) : (
            <span className="toggle-sub">Choose a library folder first.</span>
          )}
        </Accordion>
      </div>

      <div className="settings-footer">
        <button
          className="btn-primary btn-scan"
          onClick={() => (scanning ? onStop() : onScan(settings.root_folder))}
          disabled={!scanning && !canScan}
        >
          {scanning ? (
            <>
              <svg viewBox="0 0 16 16" fill="none">
                <rect x="4" y="4" width="8" height="8" rx="1" fill="currentColor"/>
              </svg>
              Stop
            </>
          ) : (
            <>
              <svg viewBox="0 0 16 16" fill="none">
                <circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.5"/>
                <path d="M11 11l3 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
              </svg>
              Scan Library
            </>
          )}
        </button>
        {!settings.tmdb_api_key ? (
          <button type="button" className="settings-warn" onClick={() => setOpenGroups(groups => ({ ...groups, keys: true }))}>
            Add a TMDB API key to scan
          </button>
        ) : !settings.root_folder && (
          <p className="settings-warn">Choose a library folder to scan</p>
        )}
      </div>

      {cleaningOrphans && (
        <OrphanCleanup root={settings.root_folder} onClose={() => setCleaningOrphans(false)} />
      )}
      {cleaningJunk && (
        <JunkCleanup root={settings.root_folder} onClose={() => setCleaningJunk(false)} />
      )}
    </div>
  )
}
