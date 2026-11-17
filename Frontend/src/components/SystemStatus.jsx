import { useCallback, useEffect, useState } from "react";
import { fmtStamp } from "../utils/format";
import { versionLabel } from "../version";

const REFRESH_MS = 30 * 1000;
const LABEL = { ok: "OK", warn: "Warning", fail: "Failed", off: "Off" };

// Admins: Settings → Status. One "LED" per part of the system, from GET /health/details
// (Backend/health.py). Codes and what to do are explained in docs/DIAGNOSTICS.md.
export default function SystemStatus({ api, version }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setData(await api.healthDetails());
      setError("");
    } catch (e) {
      // The backend itself can't be reached: that's a status too
      setData(null);
      setError(
        e.status === 0
          ? "E-NET-01: the server can't be reached (backend container stopped, server off, or no network)."
          : e.status === 404
            ? "The server doesn't have the status check yet. Deploy the latest version."
            : e.message
      );
    } finally {
      setBusy(false);
    }
  }, [api]);

  useEffect(() => {
    const first = setTimeout(load, 0); // right away, then every 30 s
    const t = setInterval(load, REFRESH_MS);
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [load, version]);

  function report() {
    const lines = [
      `CAIC system status, ${new Date().toLocaleString("en-PH")}`,
      `App: ${versionLabel()} · page: ${window.location.href}`,
    ];
    if (error) lines.push(`ERROR ${error}`);
    if (data) {
      lines.push(`Overall: ${LABEL[data.status]} · backend up ${Math.round(data.uptime_seconds / 60)} min (process ${data.process})`);
      for (const c of data.components)
        lines.push(`- ${c.label}: ${LABEL[c.status]}${c.code ? ` [${c.code}]` : ""} ${c.detail}`);
    }
    return lines.join("\n");
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(report());
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  const overall = error ? "fail" : data?.status;

  return (
    <div className="system-status">
      <div className={`status-overall st-${overall || "unknown"}`}>
        <span className="status-led" aria-hidden="true" />
        <strong>
          {error
            ? "The server can't be checked"
            : !data
              ? "Checking…"
              : overall === "ok"
                ? "Everything is working"
                : overall === "warn"
                  ? "Working, with warnings"
                  : "Something isn't working"}
        </strong>
        <span className="muted small">
          {data && `Checked ${fmtStamp(data.checked_at)} · refreshes every 30 s`}
        </span>
        <span className="status-actions">
          <button type="button" className="pill sm" onClick={load} disabled={busy}>
            {busy ? "Checking…" : "Check now"}
          </button>
          <button type="button" className="pill sm" onClick={copy} title="Copy a text report to paste in a message">
            {copied ? "Copied" : "Copy report"}
          </button>
        </span>
      </div>

      {error && <p className="error" role="alert">{error}</p>}

      {data && (
        <table className="review-table status-table">
          <thead>
            <tr>
              <th>Part</th>
              <th>Status</th>
              <th>Details</th>
              <th>Code / what to do</th>
            </tr>
          </thead>
          <tbody>
            {data.components.map((c) => (
              <tr key={c.key}>
                <td className="nowrap">
                  <strong>{c.label}</strong>
                </td>
                <td className="nowrap">
                  <span className={`status-pill st-${c.status}`}>
                    <span className="status-led" aria-hidden="true" />
                    {LABEL[c.status]}
                  </span>
                </td>
                <td className="reason">{c.detail}</td>
                <td className="reason">
                  {c.code ? (
                    <>
                      <code>{c.code}</code> {c.meaning}
                      <br />
                      <span className="muted small">{c.action}</span>
                    </>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <p className="hint">
        Green: working. Yellow: working, but needs a look. Red: not working. Grey: turned off. Server errors are listed
        in the Activity Log (action <em>Server error</em>) with an error ID such as <code>E-1A2B3C4D</code>, the same ID
        a user sees and the one to search for in <code>docker logs caic-api</code>. The full guide is in{" "}
        <code>docs/DIAGNOSTICS.md</code>.
      </p>
    </div>
  );
}
