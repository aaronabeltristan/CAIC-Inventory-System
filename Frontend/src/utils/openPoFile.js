// Opens a project's PO (PDF) in a new browser tab, in the browser's own PDF viewer, where it
// can be read, printed or saved. The tab is opened straight away, while the click still counts,
// so pop-up blockers allow it; it then goes to a link that works for 2 minutes (a tab can't
// send the sign-in, see purchase_orders.py).
export async function openPoFile(api, projectId, title = "Purchase order") {
  const tab = window.open("", "_blank");
  if (tab) {
    tab.document.title = title;
    tab.document.body.style.font = "16px system-ui, sans-serif";
    tab.document.body.textContent = "Opening the PO file…";
  }
  try {
    const url = await api.poFileUrl(projectId);
    if (tab) {
      tab.opener = null; // the PDF tab can't reach back into the app
      tab.location.replace(url);
    } else {
      window.location.assign(url); // pop-ups blocked: open it here instead
    }
  } catch (e) {
    tab?.close();
    throw e;
  }
}
