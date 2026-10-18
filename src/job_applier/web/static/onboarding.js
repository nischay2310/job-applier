// First-run popup. It is loaded by both dashboards and does nothing unless
// the server says this user still needs to be set up.
//
// Flow: upload a resume (optional) -> check the details -> Save.
// The answers go to POST /api/onboarding and are stored in the database.

(function () {
  const FIELDS = [
    { key: "name", label: "Full name", required: true },
    { key: "email", label: "Email", type: "email" },
    { key: "phone", label: "Phone" },
    { key: "location", label: "City, Country", placeholder: "Berlin, Germany" },
    { key: "linkedin", label: "LinkedIn URL (optional)" },
    { key: "years_of_experience", label: "Years of experience" },
    { key: "job_title", label: "Current or latest job title" },
    { key: "company", label: "Current or latest company" },
    { key: "skills", label: "Main skills (comma separated)" },
    { key: "target_roles", label: "Job titles to search for (comma separated)" },
    { key: "target_locations", label: "Cities or countries to search in (comma separated)" },
  ];

  // What the resume reader found, kept so it can be saved with the answers.
  let resumeFile = null;
  let resumeDetails = {};

  const css = `
    #onb-backdrop { position: fixed; inset: 0; z-index: 100000; background: rgba(2,6,23,.8);
      display: flex; align-items: center; justify-content: center; padding: 16px; }
    #onb-box { background: #0f172a; color: #e2e8f0; border: 1px solid #334155; border-radius: 16px;
      width: 100%; max-width: 560px; max-height: 92vh; overflow-y: auto; padding: 24px;
      font: 14px/1.4 system-ui, sans-serif; }
    #onb-box h2 { margin: 0 0 4px; font-size: 20px; color: #fff; }
    #onb-box p { margin: 0 0 16px; color: #94a3b8; }
    #onb-box label { display: block; margin: 12px 0 4px; font-size: 12px; color: #cbd5e1; }
    #onb-box input[type=text], #onb-box input[type=email], #onb-box input[type=password] {
      width: 100%; box-sizing: border-box; padding: 9px 10px; border-radius: 8px;
      border: 1px solid #334155; background: #020617; color: #fff; }
    #onb-box .onb-card { border: 1px dashed #475569; border-radius: 12px; padding: 12px; margin-bottom: 8px; }
    #onb-box button { padding: 9px 16px; border-radius: 8px; border: 0; cursor: pointer;
      font-weight: 600; background: #10b981; color: #022c22; }
    #onb-box button.secondary { background: #334155; color: #e2e8f0; margin-top: 8px; }
    #onb-box button:disabled { opacity: .5; cursor: wait; }
    #onb-msg { min-height: 18px; margin-top: 8px; font-size: 12px; color: #fbbf24; }
    #onb-box .onb-check { display: flex; gap: 8px; align-items: center; margin-top: 16px; }
  `;

  function el(tag, props, ...children) {
    const node = Object.assign(document.createElement(tag), props || {});
    node.append(...children);
    return node;
  }

  function setMessage(text) {
    document.getElementById("onb-msg").textContent = text;
  }

  function fieldValue(key) {
    return document.getElementById("onb-" + key).value.trim();
  }

  // The resume reader returns lists for some fields; the form shows text.
  function toText(value) {
    return Array.isArray(value) ? value.join(", ") : value || "";
  }

  function fillFromResume(details) {
    resumeDetails = details;
    const mapped = {
      ...details,
      job_title: (details.experience || [{}])[0].role,
      company: (details.experience || [{}])[0].company,
    };
    for (const field of FIELDS) {
      const input = document.getElementById("onb-" + field.key);
      const value = toText(mapped[field.key]);
      if (value) input.value = value;
    }
  }

  async function readResume() {
    const file = document.getElementById("onb-file").files[0];
    if (!file) return setMessage("Choose your resume PDF first.");
    resumeFile = file;

    const body = new FormData();
    body.append("file", file);
    const button = document.getElementById("onb-read");
    button.disabled = true;
    setMessage("Reading your resume on this computer (can take up to 2 minutes)...");
    try {
      const response = await fetch("/api/onboarding/resume", { method: "POST", body });
      const data = await response.json();
      if (!response.ok) return setMessage(data.detail || "Could not read the resume.");
      fillFromResume(data.details);
      setMessage("Done - please check the details below.");
    } catch (err) {
      setMessage("Could not reach the server: " + err);
    } finally {
      button.disabled = false;
    }
  }

  async function save() {
    if (!fieldValue("name")) return setMessage("Please enter your name.");

    const details = { ...resumeDetails };
    for (const field of FIELDS) details[field.key] = fieldValue(field.key);
    // Typed job title/company only apply when the resume gave no experience.
    if (resumeDetails.experience && resumeDetails.experience.length) {
      resumeDetails.experience[0].role = details.job_title;
      resumeDetails.experience[0].company = details.company;
    }

    const body = new FormData();
    body.append("details", JSON.stringify(details));
    if (resumeFile) body.append("resume", resumeFile);

    const button = document.getElementById("onb-save");
    button.disabled = true;
    setMessage("Saving...");
    try {
      const response = await fetch("/api/onboarding", { method: "POST", body });
      if (!response.ok) {
        const data = await response.json();
        button.disabled = false;
        return setMessage(data.detail || "Could not save.");
      }
      if (document.getElementById("onb-search").checked) await startSearch(details);
      window.location.reload();
    } catch (err) {
      button.disabled = false;
      setMessage("Could not reach the server: " + err);
    }
  }

  async function startSearch(details) {
    const split = (text) => text.split(",").map((t) => t.trim()).filter(Boolean);
    const terms = split(details.target_roles);
    const locations = split(details.target_locations);
    if (!terms.length || !locations.length) return;
    await fetch("/api/pipeline/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ terms, locations }),
    });
  }

  function buildPopup() {
    document.head.append(el("style", { textContent: css }));

    const box = el("div", { id: "onb-box" });
    box.append(
      el("h2", {}, "Welcome! Let's set you up"),
      el("p", {}, "Upload your resume to fill this in for you (read privately on this computer), or just type your details."),
    );

    const card = el("div", { className: "onb-card" });
    card.append(
      el("label", { textContent: "Resume (PDF)" }),
      el("input", { id: "onb-file", type: "file", accept: "application/pdf" }),
    );
    const readButton = el("button", { id: "onb-read", className: "secondary", textContent: "Read my resume" });
    readButton.onclick = readResume;
    card.append(readButton);
    box.append(card);

    for (const field of FIELDS) {
      box.append(
        el("label", { textContent: field.label + (field.required ? " *" : "") }),
        el("input", { id: "onb-" + field.key, type: field.type || "text", placeholder: field.placeholder || "" }),
      );
    }

    const search = el("input", { id: "onb-search", type: "checkbox", checked: true });
    box.append(el("label", { className: "onb-check" }, search, "Start searching for jobs when I save"));
    box.append(el("div", { id: "onb-msg" }));

    const saveButton = el("button", { id: "onb-save", textContent: "Save and continue" });
    saveButton.onclick = save;
    box.append(saveButton);

    document.body.append(el("div", { id: "onb-backdrop" }, box));
  }

  async function start() {
    try {
      const response = await fetch("/api/onboarding");
      const status = await response.json();
      if (status.needed) buildPopup();
    } catch (err) {
      // The dashboard still works without the popup.
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
