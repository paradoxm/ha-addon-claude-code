"use strict";

// Every URL here is relative on purpose: Home Assistant serves the add-on under
// /api/hassio_ingress/<token>/ and strips that prefix before it reaches nginx.
// An absolute path would escape the ingress session.

const $ = (id) => document.getElementById(id);

const els = {
    version: $("r-version"),
    login: $("r-login"),
    skills: $("r-skills"),
    queue: $("r-queue"),
    loginBanner: $("login-banner"),
    errorBanner: $("error-banner"),
    list: $("skills"),
    listEmpty: $("skills-empty"),
    refresh: $("refresh"),
    drop: $("drop"),
    dropText: $("drop-text"),
    file: $("file"),
    name: $("name"),
    upload: $("upload"),
    prompt: $("prompt"),
    model: $("model"),
    run: $("run"),
    job: $("job"),
    jobStatus: $("job-status"),
    jobId: $("job-id"),
    jobOut: $("job-out"),
    jobFiles: $("job-files"),
};

let picked = null;
let jobTimer = null;

// --------------------------------------------------------------------------- //

function showError(message) {
    els.errorBanner.textContent = message;
    els.errorBanner.hidden = !message;
}

async function api(path, options = {}) {
    const response = await fetch(`api/${path}`, options);
    const type = response.headers.get("Content-Type") || "";
    const payload = type.includes("json") ? await response.json() : await response.text();
    if (!response.ok) {
        throw new Error((payload && payload.error) || `HTTP ${response.status}`);
    }
    return payload;
}

function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} b`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} kb`;
    return `${(bytes / 1024 / 1024).toFixed(1)} mb`;
}

function formatDate(iso) {
    if (!iso) return "—";
    return iso.replace("T", " ").replace("+00:00", "").replace("Z", "");
}

// ------------------------------------------------------------------- health //

async function loadHealth() {
    try {
        const health = await api("health");
        els.version.textContent = (health.claude_version || "unavailable").replace(
            / \(Claude Code\)$/,
            "",
        );
        els.login.textContent = health.logged_in ? "signed in" : "not signed in";
        els.login.className = health.logged_in ? "is-ok" : "is-bad";
        els.skills.textContent = health.skills;
        els.queue.textContent = health.queued;
        els.loginBanner.hidden = health.logged_in;
        if (!els.model.value && !els.model.placeholder) {
            els.model.placeholder = health.default_model;
        }
        showError("");
    } catch (error) {
        showError(`Cannot reach the add-on API: ${error.message}`);
    }
}

// ------------------------------------------------------------------- skills //

function skillRow(skill) {
    const li = document.createElement("li");
    li.className = "skill";

    const name = document.createElement("div");
    name.className = "skill__name";
    name.textContent = skill.name;
    li.append(name);

    if (skill.description) {
        const desc = document.createElement("p");
        desc.className = "skill__desc";
        desc.textContent = skill.description;
        li.append(desc);
    }

    const meta = document.createElement("div");
    meta.className = "skill__meta";
    const bits = [`${skill.files} files`, formatBytes(skill.bytes), formatDate(skill.updated_at)];
    for (const bit of bits) {
        const span = document.createElement("span");
        span.textContent = bit;
        meta.append(span);
    }
    if (!skill.has_skill_md) {
        const warn = document.createElement("span");
        warn.className = "skill__warn";
        warn.textContent = "no SKILL.md";
        meta.append(warn);
    }
    li.append(meta);

    const actions = document.createElement("div");
    actions.className = "skill__actions";

    const download = document.createElement("a");
    download.className = "ghost";
    download.href = `api/skills/${encodeURIComponent(skill.name)}/archive`;
    download.textContent = "download";
    actions.append(download);

    const reuse = document.createElement("button");
    reuse.className = "ghost";
    reuse.textContent = "replace";
    reuse.addEventListener("click", () => {
        els.name.value = skill.name;
        els.file.click();
    });
    actions.append(reuse);

    // Two-step instead of confirm(): a modal dialog would block the page.
    const remove = document.createElement("button");
    remove.className = "ghost ghost--danger";
    remove.textContent = "delete";
    let armed = false;
    remove.addEventListener("click", async () => {
        if (!armed) {
            armed = true;
            remove.textContent = "really delete?";
            remove.classList.add("ghost--armed");
            setTimeout(() => {
                if (!armed) return;
                armed = false;
                remove.textContent = "delete";
                remove.classList.remove("ghost--armed");
            }, 4000);
            return;
        }
        remove.disabled = true;
        try {
            await api(`skills/${encodeURIComponent(skill.name)}`, { method: "DELETE" });
            await Promise.all([loadSkills(), loadHealth()]);
        } catch (error) {
            showError(`Could not delete ${skill.name}: ${error.message}`);
            remove.disabled = false;
        }
    });
    actions.append(remove);

    li.append(actions);
    return li;
}

async function loadSkills() {
    try {
        const { skills } = await api("skills");
        els.list.replaceChildren(...skills.map(skillRow));
        els.listEmpty.hidden = skills.length > 0;
    } catch (error) {
        showError(`Could not list skills: ${error.message}`);
    }
}

// ------------------------------------------------------------------- upload //

function nameFromFile(filename) {
    return filename
        .replace(/\.tar\.gz$/i, "")
        .replace(/\.tgz$/i, "")
        .replace(/[^A-Za-z0-9._-]+/g, "-")
        .replace(/^-+|-+$/g, "");
}

function pickFile(file) {
    picked = file;
    els.dropText.innerHTML = "";
    els.dropText.append(document.createTextNode(`${file.name} · ${formatBytes(file.size)}`));
    els.drop.classList.add("is-loaded");
    if (!els.name.value) els.name.value = nameFromFile(file.name);
    syncUploadButton();
}

function syncUploadButton() {
    els.upload.disabled = !(picked && els.name.value.trim());
}

els.file.addEventListener("change", () => {
    if (els.file.files.length) pickFile(els.file.files[0]);
});

els.name.addEventListener("input", syncUploadButton);

for (const event of ["dragenter", "dragover"]) {
    els.drop.addEventListener(event, (e) => {
        e.preventDefault();
        els.drop.classList.add("is-over");
    });
}

for (const event of ["dragleave", "drop"]) {
    els.drop.addEventListener(event, () => els.drop.classList.remove("is-over"));
}

els.drop.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer.files.length) pickFile(e.dataTransfer.files[0]);
});

els.upload.addEventListener("click", async () => {
    const name = els.name.value.trim();
    els.upload.disabled = true;
    els.upload.textContent = "Installing…";
    try {
        await api(`skills?name=${encodeURIComponent(name)}`, {
            method: "POST",
            headers: { "Content-Type": "application/gzip" },
            body: picked,
        });
        picked = null;
        els.file.value = "";
        els.name.value = "";
        els.drop.classList.remove("is-loaded");
        els.dropText.innerHTML = "Drop a <b>.tar.gz</b> here, or click to choose";
        await Promise.all([loadSkills(), loadHealth()]);
    } catch (error) {
        showError(`Upload failed: ${error.message}`);
    } finally {
        els.upload.textContent = "Install skill";
        syncUploadButton();
    }
});

// ---------------------------------------------------------------------- job //

function renderJob(job) {
    els.job.hidden = false;
    els.jobId.textContent = job.id;
    els.jobStatus.textContent = job.status;
    els.jobStatus.className = `pill is-${job.status}`;
    els.jobOut.textContent = job.error || job.result || "";

    els.jobFiles.replaceChildren();
    for (const file of job.files || []) {
        const li = document.createElement("li");
        const link = document.createElement("a");
        link.href = `api/jobs/${job.id}/files/${file.path
            .split("/")
            .map(encodeURIComponent)
            .join("/")}`;
        link.textContent = file.path;
        const size = document.createElement("span");
        size.textContent = formatBytes(file.size);
        li.append(link, size);
        els.jobFiles.append(li);
    }
}

async function pollJob(id) {
    try {
        const job = await api(`jobs/${id}`);
        renderJob(job);
        if (job.status === "queued" || job.status === "running") return;
    } catch (error) {
        showError(`Lost track of the job: ${error.message}`);
    }
    clearInterval(jobTimer);
    jobTimer = null;
    els.run.disabled = false;
    els.run.textContent = "Queue job";
    loadHealth();
}

els.run.addEventListener("click", async () => {
    const prompt = els.prompt.value.trim();
    if (!prompt) {
        showError("A prompt is required.");
        return;
    }
    els.run.disabled = true;
    els.run.textContent = "Queued…";
    try {
        const job = await api("jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                prompt,
                model: els.model.value.trim() || undefined,
                start: true,
            }),
        });
        renderJob(job);
        if (jobTimer) clearInterval(jobTimer);
        jobTimer = setInterval(() => pollJob(job.id), 4000);
        pollJob(job.id);
    } catch (error) {
        showError(`Could not queue the job: ${error.message}`);
        els.run.disabled = false;
        els.run.textContent = "Queue job";
    }
});

// -------------------------------------------------------------------- start //

els.refresh.addEventListener("click", () => {
    loadSkills();
    loadHealth();
});

loadHealth();
loadSkills();
setInterval(loadHealth, 20000);
