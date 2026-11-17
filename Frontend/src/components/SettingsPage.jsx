import { useEffect, useMemo, useRef, useState } from "react";
import NameRequests from "./NameRequests";
import { UsersContent } from "./UsersDialog";
import { CountBadge } from "./AdminMenu";
import BackupStatus from "./BackupStatus";
import SystemStatus from "./SystemStatus";
import Logo from "./Logo";
import { setAppSettings } from "../hooks/useAppSettings";
import { daysLabel } from "../hooks/useRetentionDays";

// Admins: Settings page (user menu → Settings). Manage Users, then the app-wide settings,
// one tab per group. The fields come from the server's descriptions (Backend/app_settings.py),
// so a new setting there shows up here without changes.
const TABS = [
  { key: "users", label: "Manage Users" },
  { key: "status", label: "Status" },
  { key: "security", label: "Security" },
  { key: "requests", label: "Requests" },
  { key: "system", label: "System" },
  { key: "company", label: "Company" },
];

// Sections inside a tab: [title, setting keys]
const SECTIONS = {
  security: [
    ["Sign-in", ["login_max_failures", "login_lock_minutes", "session_idle_minutes"]],
    ["Devices", ["single_session", "single_session_exempt"]],
    ["Sign-up and password resets", ["allow_registration", "allow_password_reset", "register_per_ip_per_hour",
      "register_total_per_hour", "max_waiting_accounts"]],
  ],
  requests: [["Staff edits", ["staff_edits_need_approval", "edit_reason_required"]]],
  system: [
    ["Archived items", ["archive_retention_days"]],
    ["Activity Log", ["activity_log_keep_days"]],
    ["Assistant", ["chat_access"]],
    ["Backups", ["backup_hour", "backup_keep_daily", "backup_keep_weekly", "backup_keep_monthly"]],
  ],
  company: [["Company", ["company_name", "company_short_name", "currency"]]],
};

export default function SettingsPage({ api, user, version, pending, initialSection, onChanged }) {
  const waitingAccounts = (pending?.accounts || 0) + (pending?.names || 0);
  const [section, setSection] = useState(TABS.some((t) => t.key === initialSection) ? initialSection : "users");
  const [data, setData] = useState(null); // { values, schema, backup_requested }
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    let live = true;
    api
      .getSettings()
      .then((d) => {
        if (!live) return;
        setData(d);
        setAppSettings(d.values);
        setLoadError("");
      })
      .catch(
        (e) =>
          live &&
          setLoadError(
            e.status === 404
              ? "The server doesn't have Settings yet. Restart or redeploy the backend with the latest code, then try again."
              : e.message
          )
      );
    return () => {
      live = false;
    };
  }, [api, version]);

  const saved = (d, msg) => {
    setData(d);
    setAppSettings(d.values);
    if (msg) onChanged?.(msg);
  };

  const title = TABS.find((t) => t.key === section)?.label || "";

  return (
    <>
      <div className="page-head">
        <h1>Settings</h1>
      </div>

      <div className="folder-tabs" role="tablist" aria-label="Settings sections">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={section === t.key}
            className="folder-tab"
            onClick={() => setSection(t.key)}
          >
            {t.label}
            {t.key === "users" && <CountBadge count={waitingAccounts} label="waiting" />}
          </button>
        ))}
      </div>

      <section className="panel folder-body" aria-label={title}>
        <h2 className="panel-title">{title}</h2>
        {section === "users" ? (
          <>
            <NameRequests api={api} version={version} onChanged={onChanged} />
            <UsersContent api={api} user={user} version={version} onChanged={onChanged} />
          </>
        ) : section === "status" ? (
          <SystemStatus api={api} version={version} />
        ) : loadError ? (
          <p role="alert" className="error">{loadError}</p>
        ) : !data ? (
          <p className="muted">Loading…</p>
        ) : !data.schema ? (
          <p className="muted">Settings can't be changed here.</p>
        ) : (
          <SettingsForm
            key={section}
            api={api}
            group={section}
            data={data}
            version={version}
            onSaved={saved}
          />
        )}
      </section>
    </>
  );
}

// One tab's settings, saved together.
function SettingsForm({ api, group, data, version, onSaved }) {
  const byKey = useMemo(() => Object.fromEntries(data.schema.map((s) => [s.key, s])), [data.schema]);
  const sections = SECTIONS[group] || [];
  const keys = sections.flatMap(([, k]) => k).filter((k) => byKey[k]);
  // Anything the server has in this group that isn't placed above goes in "Other"
  const extra = data.schema.filter((s) => s.group === group && !keys.includes(s.key)).map((s) => s.key);
  const allKeys = [...keys, ...extra];

  const [draft, setDraft] = useState(() => Object.fromEntries(allKeys.map((k) => [k, toInput(byKey[k], data.values[k])])));
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  // What changed, converted back to real values; and the first problem, if any
  const { changes, problem } = useMemo(() => {
    const out = {};
    for (const k of allKeys) {
      const s = byKey[k];
      const parsed = fromInput(s, draft[k]);
      if (parsed.error) return { changes: out, problem: parsed.error };
      if (parsed.value !== data.values[k]) out[k] = parsed.value;
    }
    return { changes: out, problem: "" };
  }, [draft, data.values, byKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = Object.keys(changes).length > 0;

  // Archive period getting shorter: how many archived items it would delete
  const retentionPreview = useRetentionPreview(api, changes.archive_retention_days, data.values.archive_retention_days);
  const losing = retentionPreview ? retentionPreview.projects + retentionPreview.entries : 0;

  async function save(e) {
    e.preventDefault();
    setError("");
    if (problem) return setError(problem);
    if (!dirty) return;
    setSaving(true);
    try {
      const d = await api.updateSettings(changes);
      setDraft(Object.fromEntries(allKeys.map((k) => [k, toInput(byKey[k], d.values[k])])));
      const n = Object.keys(changes).length;
      onSaved(d, n === 1 ? `Saved: ${byKey[Object.keys(changes)[0]].label}.` : `Saved ${n} settings.`);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  const reset = () => setDraft(Object.fromEntries(allKeys.map((k) => [k, toInput(byKey[k], data.values[k])])));
  const field = (k) => (
    <Field key={k} s={byKey[k]} value={draft[k]} onChange={(v) => setDraft((d) => ({ ...d, [k]: v }))} />
  );

  return (
    <form className="settings-form" onSubmit={save}>
      {group === "company" && <LogoSettings api={api} data={data} onSaved={onSaved} />}
      {sections.map(([title, sectionKeys]) => {
        const present = sectionKeys.filter((k) => byKey[k]);
        if (!present.length) return null;
        return (
          <fieldset key={title} className="settings-section">
            <legend>{title}</legend>
            {present.map(field)}
            {title === "Archived items" && losing > 0 && (
              <p role="alert" className="error">
                {retentionPreview.projects > 0 &&
                  `${retentionPreview.projects} archived project${retentionPreview.projects === 1 ? "" : "s"}`}
                {retentionPreview.projects > 0 && retentionPreview.entries > 0 && " and "}
                {retentionPreview.entries > 0 &&
                  `${retentionPreview.entries} archived entr${retentionPreview.entries === 1 ? "y" : "ies"}`}{" "}
                {losing === 1 ? "has" : "have"} been archived longer than {daysLabel(changes.archive_retention_days)} and
                will be deleted permanently within the hour after you save. Restore {losing === 1 ? "it" : "them"} first
                if needed.
              </p>
            )}
            {title === "Backups" && <BackupNow api={api} data={data} version={version} onSaved={onSaved} />}
          </fieldset>
        );
      })}
      {extra.length > 0 && (
        <fieldset className="settings-section">
          <legend>Other</legend>
          {extra.map(field)}
        </fieldset>
      )}

      {(error || (dirty && problem)) && (
        <p role="alert" className="error">
          {error || problem}
        </p>
      )}
      <div className="settings-actions">
        <button type="button" className="pill" disabled={!dirty || saving} onClick={reset}>
          Reset
        </button>
        <button className={`pill ${losing > 0 ? "danger" : "primary"}`} disabled={!dirty || saving || !!problem}>
          {saving ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

// ---------- Fields ----------

const toInput = (s, value) => (s.kind === "bool" ? !!value : value == null ? "" : String(value));

function fromInput(s, raw) {
  if (s.kind === "bool") return { value: !!raw };
  if (s.kind === "int") {
    const text = String(raw).trim();
    if (!/^\d+$/.test(text)) return { error: `${s.label}: enter a whole number.` };
    const n = Number(text);
    if ((s.min != null && n < s.min) || (s.max != null && n > s.max))
      return { error: `${s.label}: enter a number from ${s.min} to ${s.max}.` };
    if (s.zero_or_at_least && n > 0 && n < s.zero_or_at_least)
      return { error: `${s.label}: enter 0 (keep everything) or at least ${s.zero_or_at_least}.` };
    return { value: n };
  }
  if (s.kind === "list") {
    return {
      value: [...new Set(String(raw).split(",").map((x) => x.trim()).filter(Boolean))].join(","),
    };
  }
  if (s.kind === "text") {
    const text = String(raw).split(/\s+/).filter(Boolean).join(" ");
    if (!text) return { error: `${s.label} can't be empty.` };
    return { value: text };
  }
  return { value: raw };
}

function Field({ s, value, onChange }) {
  const id = `setting-${s.key}`;
  const help = s.help && <span className="hint">{s.help}</span>;
  if (s.kind === "bool") {
    return (
      <div className="setting-row setting-toggle">
        <label htmlFor={id}>
          <input id={id} type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />
          <span>{s.label}</span>
        </label>
        {help}
      </div>
    );
  }
  return (
    <div className="setting-row">
      <label htmlFor={id}>{s.label}</label>
      {s.kind === "choice" ? (
        <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
          {Object.entries(s.choices).map(([k, label]) => (
            <option key={k} value={k}>
              {label}
            </option>
          ))}
        </select>
      ) : s.kind === "int" ? (
        <input
          id={id}
          type="number"
          inputMode="numeric"
          min={s.min}
          max={s.max}
          step="1"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="setting-number"
        />
      ) : (
        <input
          id={id}
          value={value}
          maxLength={s.max_length}
          onChange={(e) => onChange(e.target.value)}
          placeholder={s.kind === "list" ? "e.g. loadtest, loadtestadmin" : undefined}
          className="setting-text"
        />
      )}
      {help}
    </div>
  );
}

// ---------- Extras ----------

function useRetentionPreview(api, days, current) {
  const [checked, setChecked] = useState(null);
  const shorter = Number.isInteger(days) && days < current;
  useEffect(() => {
    if (!shorter || !api.previewRetention) return;
    let live = true;
    const t = setTimeout(() => api.previewRetention(days).then((p) => live && setChecked(p), () => {}), 300);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [api, days, shorter]);
  // Only the answer for the number currently typed counts
  return shorter && checked?.archive_retention_days === days ? checked : null;
}

function BackupNow({ api, data, version, onSaved }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  async function run() {
    setBusy(true);
    setNote("");
    try {
      const res = await api.backupNow();
      setNote(res?.detail || "Backup requested.");
      const d = await api.getSettings();
      onSaved(d);
    } catch (e) {
      setNote(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="backup-now">
      <BackupStatus api={api} version={version} />
      <button type="button" className="pill" onClick={run} disabled={busy || data.backup_requested}>
        {data.backup_requested ? "Backup requested…" : busy ? "Requesting…" : "Back up now"}
      </button>
      <p className="hint">
        {note ||
          (data.backup_requested
            ? "The backup service picks it up within a minute. The result appears above when it's done."
            : "Makes an extra backup now, besides the daily one. Changes to the schedule apply within a minute.")}
      </p>
    </div>
  );
}

function LogoSettings({ api, data, onSaved }) {
  const input = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const custom = !!data.values.logo_version;

  async function upload(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (file.size > 1024 * 1024) return setError("The logo is too large (1 MB at most).");
    setBusy(true);
    setError("");
    try {
      onSaved(await api.uploadLogo(file), "Logo changed.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setError("");
    try {
      onSaved(await api.removeLogo(), "Went back to the built-in logo.");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <fieldset className="settings-section">
      <legend>Logo</legend>
      <div className="logo-setting">
        <div className="logo-preview">
          <Logo />
        </div>
        <div>
          <input ref={input} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={upload} />
          <button type="button" className="pill" disabled={busy} onClick={() => input.current.click()}>
            {busy ? "Uploading…" : "Upload logo"}
          </button>{" "}
          {custom && (
            <button type="button" className="pill" disabled={busy} onClick={remove}>
              Use the built-in logo
            </button>
          )}
          <p className="hint">PNG, JPEG or WebP, up to 1 MB. Shown on the sign-in page and at the top of every page.</p>
          {error && <p role="alert" className="error">{error}</p>}
        </div>
      </div>
    </fieldset>
  );
}
