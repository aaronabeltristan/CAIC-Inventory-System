import { useEffect, useRef, useState } from "react";
import { currency, money } from "../utils/format";

const MAX_PO_FILE = 10 * 1024 * 1024; // 10 MB, as the server allows

// Create a project (title, PO number, PO amount, PO as a PDF: all required), or, with
// `project`, let an admin add or correct an existing project's PO. When the project already
// has a PO file, choosing a new one is optional.
export default function ProjectDialog({ project, onSave, onClose }) {
  const editing = !!project;
  const ref = useRef(null);
  const fileInput = useRef(null);
  const [title, setTitle] = useState(project?.project_title ?? "");
  const [poNumber, setPoNumber] = useState(project?.po_number ?? "");
  const [amount, setAmount] = useState(project?.po_amount != null ? String(Number(project.po_amount)) : "");
  const [file, setFile] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const needsFile = !editing || !project.has_po_file;

  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);

  function pickFile(e) {
    const f = e.target.files?.[0] || null;
    setError("");
    if (f && !(f.type === "application/pdf" || /\.pdf$/i.test(f.name))) {
      e.target.value = "";
      setFile(null);
      return setError("The PO file must be a PDF.");
    }
    if (f && f.size > MAX_PO_FILE) {
      e.target.value = "";
      setFile(null);
      return setError("The PO file is too large (10 MB at most).");
    }
    setFile(f);
  }

  async function submit(e) {
    e.preventDefault();
    setError("");
    const cleanTitle = title.trim().replace(/\s+/g, " ");
    const cleanPo = poNumber.trim();
    const n = Number(String(amount).replace(/,/g, ""));
    if (!editing && !cleanTitle) return setError("Enter a project title.");
    if (!cleanPo) return setError("Enter the PO number.");
    if (!/^\d{1,30}$/.test(cleanPo)) return setError("The PO number must be numbers only (0-9).");
    if (!(n > 0)) return setError("Enter a PO amount greater than zero.");
    if (Math.round(n * 100) !== n * 100) return setError("The PO amount can have at most two decimals.");
    if (needsFile && !file) return setError("Attach the PO as a PDF.");
    setBusy(true);
    try {
      await onSave({ title: cleanTitle, po_number: cleanPo, amount: n.toFixed(2), file });
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby="newproj-title"
      onClose={onClose}
      onClick={(e) => e.target === ref.current && !busy && ref.current.close()}
    >
      <form onSubmit={submit}>
        <h2 id="newproj-title">{editing ? (project.po_number ? "Edit purchase order" : "Add purchase order") : "Create new project"}</h2>
        {editing ? (
          <p className="muted small">
            Project: <strong>{project.project_title}</strong>
          </p>
        ) : (
          <label>
            Project title
            <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={255} required autoFocus />
          </label>
        )}
        <div className="row">
          <label>
            PO number
            <input
              value={poNumber}
              onChange={(e) => setPoNumber(e.target.value.replace(/\D/g, ""))}
              inputMode="numeric"
              pattern="[0-9]*"
              maxLength={30}
              title="Numbers only (0-9)"
              placeholder="e.g. 20260153"
              required
              autoFocus={editing}
            />
          </label>
          <label>
            PO amount ({currency()})
            <input
              type="number"
              inputMode="decimal"
              step="0.01"
              min="0.01"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              required
            />
          </label>
        </div>
        <label>
          {needsFile ? "PO file (PDF)" : "Replace the PO file (PDF, optional)"}
          <input
            ref={fileInput}
            type="file"
            accept="application/pdf,.pdf"
            onChange={pickFile}
            required={needsFile}
          />
        </label>
        <p className="hint">
          The PO number is numbers only and identifies the project: no two projects can have the same one. PDF
          up to 10 MB.
          {editing && project.has_po_file && !file && " The current PO file is kept unless you choose a new one."}
          {amount && Number(amount) > 0 && ` PO amount: ${money(amount)}.`}
        </p>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="dialog-actions">
          <button type="button" className="pill" disabled={busy} onClick={() => ref.current.close()}>
            Cancel
          </button>
          <button className="pill primary" disabled={busy}>
            {busy ? (editing ? "Saving…" : "Creating…") : editing ? "Save PO" : "Create project"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
