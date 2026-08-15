"use strict";

// Everything below is wrapped so that nothing this file declares lands on
// `window`. A classic script's top-level declarations are global properties,
// and this page has no reason to publish any of them.
(() => {

    // Every URL here is relative on purpose: Home Assistant serves the add-on under
    // /api/hassio_ingress/<token>/ and strips that prefix before it reaches nginx.
    // An absolute path would escape the ingress session.

    const $ = (id) => document.getElementById(id);

    const els = {
        version: $("r-version"),
        login: $("r-login"),
        skills: $("r-skills"),
        queueReading: $("r-queue"),
        updates: $("r-updates"),
        menu: $("menu"),
        drawer: $("drawer"),
        tools: $("tools"),
        actions: $("actions"),
        newChat: $("new-chat"),
        history: $("history"),
        loginBanner: $("login-banner"),
        errorBanner: $("error-banner"),
        limitsBanner: $("limits-banner"),
        updateBanner: $("update-banner"),
        updateText: $("update-text"),
        updateDismiss: $("update-dismiss"),
        chatTitle: $("chat-title"),
        transcript: $("transcript"),
        transcriptEmpty: $("transcript-empty"),
        toLatest: $("to-latest"),
        queue: $("chat-queue"),
        contextbar: $("contextbar"),
        context: $("context"),
        compact: $("compact"),
        controls: $("controls"),
        controlsChip: $("controls-chip"),
        controlsSummary: $("controls-summary"),
        notices: $("notices"),
        noticesCount: $("notices-count"),
        noticesOverlay: $("notices-overlay"),
        noticesClose: $("notices-close"),
        noticesList: $("notices-list"),
        composer: $("composer"),
        prompt: $("prompt"),
        send: $("send"),
        model: $("model"),
        effort: $("effort"),
        mode: $("mode"),
        list: $("skills"),
        listEmpty: $("skills-empty"),
        refresh: $("refresh"),
        drop: $("drop"),
        dropText: $("drop-text"),
        file: $("file"),
        upload: $("upload"),
        settingsOpen: $("settings-open"),
        settings: $("settings-overlay"),
        settingsClose: $("settings-close"),
        tabSkills: $("tab-skills"),
        tabPermissions: $("tab-permissions"),
        tabMcp: $("tab-mcp"),
        tabMemory: $("tab-memory"),
        tabConfig: $("tab-config"),
        tabUsage: $("tab-usage"),
        tabUpdates: $("tab-updates"),
        paneSkills: $("pane-skills"),
        panePermissions: $("pane-permissions"),
        paneMcp: $("pane-mcp"),
        paneMemory: $("pane-memory"),
        paneConfig: $("pane-config"),
        mcpList: $("mcp-list"),
        mcpEmpty: $("mcp-empty"),
        paneUsage: $("pane-usage"),
        paneUpdates: $("pane-updates"),
        usageSessionFigure: $("usage-session-figure"),
        usageSessionBar: $("usage-session-bar"),
        usageSessionMark: $("usage-session-mark"),
        usageSessionWhen: $("usage-session-when"),
        usageWeekFigure: $("usage-week-figure"),
        usageWeekBar: $("usage-week-bar"),
        usageWeekMark: $("usage-week-mark"),
        usageWeekWhen: $("usage-week-when"),
        usageNote: $("usage-note"),
        usageChecked: $("usage-checked"),
        usageRefresh: $("usage-refresh"),
        usageRule: $("usage-rule"),
        uInstalled: $("u-installed"),
        uAvailable: $("u-available"),
        uChannel: $("u-channel"),
        uAuto: $("u-auto"),
        uProgress: $("u-progress"),
        uOutput: $("u-output"),
        uBinary: $("u-binary"),
        uCheck: $("u-check"),
        uInstall: $("u-install"),
        settingsPath: $("settings-path"),
        settingsJson: $("settings-json"),
        settingsStatus: $("settings-status"),
        settingsFormat: $("settings-format"),
        settingsReload: $("settings-reload"),
        settingsSave: $("settings-save"),
        overlay: $("history-overlay"),
        historyClose: $("history-close"),
        historySearch: $("history-search"),
        historyList: $("history-list"),
        historyEmpty: $("history-empty"),
    };

    const MAX_UPLOAD = 256 * 1024 * 1024;
    const POLL_FAILURES_ALLOWED = 5;
    const DROP_DEFAULT = els.dropText.innerHTML;

    let picked = null;
    let healthTimer = null;
    let healthPeriod = 0;
    let chatTimer = null;
    let chatFailures = 0;
    let apiDown = false;
    let updateRequested = false;
    let dismissKey = null;
    let controlsBuilt = false;
    let sessions = [];
    let runningTurn = null;
    let titleEditing = false;
    let currentSession = null;
    let notices = [];
    // Which of them were new when the sheet was last opened, so the list can say so.
    let unreadNotices = new Set();
    // True while the reader is at the end of the transcript, which is the only time
    // new text should pull the view along.
    let followTail = true;

    // --------------------------------------------------------------------------- //

    const TAIL_SLACK_PX = 80;

    function atTail() {
        const view = els.transcript;
        return view.scrollHeight - view.scrollTop - view.clientHeight < TAIL_SLACK_PX;
    }

    function scrollToTail() {
        els.transcript.scrollTop = els.transcript.scrollHeight;
        followTail = true;
        els.toLatest.hidden = true;
    }

    function showError(message) {
        els.errorBanner.textContent = message;
        els.errorBanner.hidden = !message;
    }

    async function api(path, options = {}) {
        const response = await fetch(`api/${path}`, options);
        const type = response.headers.get("Content-Type") || "";

        if (!type.includes("json")) {
            // An HTML 200 means something in front of us answered instead of the
            // add-on — an expired ingress session, or a reverse proxy. Reading
            // properties off that string would render a plausible, invented status.
            const text = await response.text();
            throw new Error(
                response.ok
                    ? `unexpected ${type || "empty"} response`
                    : `HTTP ${response.status}: ${text.slice(0, 200)}`,
            );
        }

        let payload;
        try {
            payload = await response.json();
        } catch (error) {
            throw new Error(`malformed response: ${error.message}`, { cause: error });
        }
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
        const when = new Date(iso);
        // The stamps are UTC, and carry microseconds so jobs order deterministically.
        // Before, the offset was simply deleted along with them — which showed every time
        // in the console five hours out here, silently and without a label. They are shown
        // on Home Assistant's clock now, in the same layout as before.
        if (Number.isNaN(when.getTime())) return String(iso);
        const parts = new Intl.DateTimeFormat("en-GB", {
            timeZone: houseZone,
            // h23 rather than hour12: false, which turns midnight into 24:00 on some
            // engines.
            hourCycle: "h23",
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
        }).formatToParts(when).reduce((all, part) => ({ ...all, [part.type]: part.value }), {});
        return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
    }

    function formatCount(n) {
        return n.toLocaleString("en-US");
    }

    // ---------------------------------------------------------------- controls //

    // Built from what /health reports rather than hardcoded, so the choices cannot
    // drift from what the server will accept.
    function buildControls(health) {
        if (controlsBuilt) return;
        controlsBuilt = true;

        for (const [select, values, fallback] of [
            [els.model, health.models, health.default_model],
            [els.effort, health.efforts, health.default_effort],
            [els.mode, health.permission_modes, health.default_permission_mode],
        ]) {
            select.replaceChildren();
            for (const value of values || []) {
                const option = document.createElement("option");
                option.value = value;
                option.textContent = value;
                select.append(option);
            }
            if (fallback && !values?.includes(fallback)) {
                const extra = document.createElement("option");
                extra.value = fallback;
                extra.textContent = fallback;
                select.prepend(extra);
            }
            select.value = fallback;
        }
        renderControlsSummary();
    }

    // What the next message will be sent with, in one line. On a phone the three
    // selects fold behind this; on a wide screen they are all visible and it is not
    // shown at all.
    function renderControlsSummary() {
        els.controlsSummary.textContent = [els.model.value, els.effort.value, els.mode.value]
            .filter(Boolean)
            .join(" · ");
    }

    // A scroll of their own decides it: away from the end stops the following, back
    // at the end resumes it.
    els.transcript.addEventListener("scroll", () => {
        followTail = atTail();
        els.toLatest.hidden = followTail;
    });

    els.toLatest.addEventListener("click", scrollToTail);

    els.controlsChip.addEventListener("click", () => {
        const open = els.controls.classList.toggle("is-open");
        els.controlsChip.setAttribute("aria-expanded", String(open));
    });

    for (const select of [els.model, els.effort, els.mode]) {
        select.addEventListener("change", renderControlsSummary);
    }

    // ------------------------------------------------------------------- health //

    function pluginSummary(plugins) {
        if (!plugins) return "";
        const failed = Object.entries(plugins.plugins || {})
            .filter(([, outcome]) => outcome !== "ok")
            .map(([name]) => name);
        return failed.length
            ? ` Plugins needing attention: ${failed.join(", ")}.`
            : " Marketplaces and plugins refreshed.";
    }

    // One sentence about the last or current install, used by both the banner and the
    // updates tab so the two cannot tell different stories.
    function updateStory(state) {
        if (state.status === "running") {
            const from = state.previous ? ` from ${state.previous}` : "";
            return `Installing ${state.target}${from}. Started ${formatDate(state.started_at)} — ` +
                "this keeps running if you close the page.";
        }
        if (state.status === "failed" || state.status === "interrupted") {
            return `Last attempt ${state.status}: ${state.error || "no detail given"}`;
        }
        if (state.status === "done" && state.changed) {
            return `Updated ${state.previous} → ${state.installed} at ` +
                `${formatDate(state.finished_at)}.${pluginSummary(state.plugins)}`;
        }
        if (state.status === "done") {
            return `Checked at ${formatDate(state.finished_at)} — ${state.installed} was ` +
                "already the newest on this channel.";
        }
        return "Nothing has been installed from here yet.";
    }

    function renderUpdateBanner(health, state) {
        const banner = els.updateBanner;
        banner.classList.remove("banner--error");

        if (state.status === "running") {
            els.updateText.textContent = updateStory(state);
            banner.hidden = false;
            return 3000;
        }

        // A check that changed nothing is not news, so it gets no banner — it used to
        // sit there permanently with no way to close it. The time still shows in the
        // updates tab for anyone who wants it.
        let message = null;
        let key = state.finished_at;
        if (state.status === "failed" || state.status === "interrupted") {
            banner.classList.add("banner--error");
            message = updateStory(state);
        } else if (state.status === "done" && state.changed) {
            message = updateStory(state);
        } else if (health.update_available) {
            // The one prompt left in the masthead now that the button lives in
            // Settings: without it a waiting update would be invisible until someone
            // went looking for it.
            message = `Claude Code ${health.available_version} is available. ` +
                "Settings → Updates installs it.";
            key = `available:${health.available_version}`;
        }

        dismissKey = key || null;
        if (message && dismissed() !== key) {
            els.updateText.textContent = message;
            banner.hidden = false;
        } else {
            banner.hidden = true;
        }
        return 20000;
    }

    function renderUpdatePane(health, state) {
        const running = state.status === "running";
        els.uInstalled.textContent = health.claude_version || "unavailable";
        els.uAvailable.textContent = health.available_version || "not checked";
        els.uAvailable.classList.toggle("is-ok", Boolean(health.update_available));
        els.uChannel.textContent = health.update_channel;
        els.uAuto.textContent = health.auto_update ? "on" : "off";

        els.uProgress.textContent = updateStory(state);
        els.uProgress.classList.toggle("progress--busy", running);
        els.uProgress.classList.toggle(
            "progress--bad",
            state.status === "failed" || state.status === "interrupted",
        );

        els.uOutput.hidden = !state.output;
        els.uOutput.textContent = state.output || "";

        els.uInstall.disabled = Boolean(running || health.job_running || updateRequested);
        els.uInstall.textContent = running || updateRequested
            ? "Installing…"
            : health.update_available
                ? `Update → ${health.available_version}`
                : `Reinstall ${health.update_channel}`;
        els.uInstall.title = health.job_running
            ? "A job is running; updating is blocked until it finishes"
            : `Runs claude install ${health.update_channel}`;
        els.uCheck.disabled = running;
    }

    function renderUpdate(health) {
        const state = health.update || {};
        if (state.status === "running") updateRequested = false;
        renderUpdatePane(health, state);
        return renderUpdateBanner(health, state);
    }

    function dismissed() {
        try {
            return localStorage.getItem("dismissedUpdate");
        } catch {
            return null;
        }
    }

    els.updateDismiss.addEventListener("click", () => {
        els.updateBanner.hidden = true;
        try {
            localStorage.setItem("dismissedUpdate", dismissKey || "");
        } catch {
            // Private mode or storage disabled: it stays hidden until the next poll.
        }
    });

    function scheduleHealth(period) {
        if (period === healthPeriod) return;
        healthPeriod = period;
        if (healthTimer) clearInterval(healthTimer);
        healthTimer = setInterval(loadHealth, period);
    }

    // Why nothing is happening, on the page you are already looking at. Settings has the
    // detail; this is the one line that explains a console that answers nothing.
    //
    // Asked for sparingly: every reading is a request to Anthropic, and a page left open on
    // a dashboard would otherwise ask for ever. A page nobody is looking at asks nothing at
    // all, and a page in front of somebody asks every three minutes — the figure does not
    // move fast enough for more, and the add-on caches its own answer for a minute besides.
    // Home Assistant's timezone, learned from /health. Until it answers, the browser's own
    // is the best guess there is.
    let houseZone = undefined;
    let limitsEveryMs = 180_000;
    let limitsAskedAt = 0;

    async function refreshLimits({ force = false } = {}) {
        // "hidden" precisely: a page still starting up reports prerender, and that is a
        // page somebody is about to look at.
        const unwatched = document.visibilityState === "hidden";
        if (!force && (unwatched || Date.now() - limitsAskedAt < limitsEveryMs)) return;
        limitsAskedAt = Date.now();
        try {
            const usage = await api("usage");
            // How often the add-on is willing to be asked; it is a setting there, not here.
            if (usage.check_every > 0) limitsEveryMs = usage.check_every * 1000;
            const worst = usage.available ? usage.worst : null;
            const held = Boolean(worst) && usage.acting && !usage.enough;
            els.limitsBanner.hidden = !held;
            if (held) {
                const window = worst.kind === "week" ? "weekly" : "five-hour";
                els.limitsBanner.textContent =
                    `Work is held: the ${window} allowance is ${worst.percent}% used and this`
                    + ` add-on stops at ${worst.threshold}%.`
                    + (worst.resets_at ? ` It resets ${untilWhen(worst.resets_at)}.` : "")
                    + " A new turn is refused, and one already going is frozen where it stands.";
            }
        } catch {
            // The reading is best-effort by design; a page that cannot get it says nothing
            // rather than crying wolf.
            els.limitsBanner.hidden = true;
        }
    }

    async function loadHealth() {
        try {
            const health = await api("health");
            refreshLimits();
            buildControls(health);
            els.version.textContent = health.claude_version || "unavailable";
            els.login.textContent = health.logged_in ? "signed in" : "not signed in";
            els.login.className = health.logged_in ? "is-ok" : "is-bad";
            els.skills.textContent = health.skills;
            // The running job has already been taken off the queue, so qsize alone
            // reads 0 for the whole run — the one moment you want a number.
            els.queueReading.textContent = health.queued + (health.job_running ? 1 : 0);
            houseZone = health.timezone || undefined;
            els.updates.textContent = health.auto_update
                ? `auto · ${health.update_channel}`
                : "manual";
            const checked = (health.update || {}).finished_at;
            els.updates.title = checked
                ? `Last checked ${formatDate(checked)}`
                : "Not checked yet this run";
            els.loginBanner.hidden = health.logged_in;

            scheduleHealth(renderUpdate(health));

            if (health.skills !== els.list.children.length) loadSkills();

            if (apiDown) {
                apiDown = false;
                showError("");
            }
        } catch (error) {
            apiDown = true;
            showError(`Cannot reach the add-on API: ${error.message}`);
        }
    }

    // --------------------------------------------------------------------- chat //

    // Line icons rather than words: two actions on one row, the same size and weight, and
    // no wall of uppercase where a title should be read.
    const ICONS = {
        rename: "M2.5 13.5V11l7.4-7.4 2.5 2.5L5 13.5H2.5zM10.9 2.1l1.6-1.6 2.5 2.5-1.6 1.6z",
        delete: "M2.5 4.5h11M6 4.5V2.5h4v2M4.3 4.5l.7 9.2h6l.7-9.2M6.6 7v4M9.4 7v4",
        sure: "M2.8 8.6l3.4 3.4L13.2 5",
    };

    function iconSvg(kind) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 16 16");
        svg.setAttribute("aria-hidden", "true");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", ICONS[kind]);
        svg.append(path);
        return svg;
    }

    function iconButton(kind, label, extra = "") {
        const button = document.createElement("button");
        button.className = `icon ${extra}`.trim();
        button.type = "button";
        button.title = label;
        button.setAttribute("aria-label", label);
        button.append(iconSvg(kind));
        return button;
    }

    function setIcon(button, kind) {
        button.replaceChildren(iconSvg(kind));
    }

    function turnElement(role, text, at, pending = false, failed = false) {
        const turn = document.createElement("article");
        turn.className = `turn turn--${role === "user" ? "user" : "claude"}`;
        if (pending) turn.classList.add("turn--pending");
        if (failed) turn.classList.add("turn--failed");

        const head = document.createElement("header");
        head.className = "turn__head";

        const who = document.createElement("span");
        who.className = "turn__who";
        who.textContent = role === "user" ? "you" : "claude";
        head.append(who);

        if (pending) {
            const pill = document.createElement("span");
            pill.className = "pill is-running";
            pill.textContent = "working";
            head.append(pill);
        } else if (at) {
            const when = document.createElement("span");
            when.className = "turn__when";
            when.textContent = formatDate(at);
            head.append(when);
        }

        const body = document.createElement("div");
        body.className = "turn__body";
        body.textContent = text;

        turn.append(head, body);
        return turn;
    }

    function renderContext(context, busy) {
        els.context.hidden = !context;
        els.compact.hidden = !context || context.left_percent >= 50;
        els.compact.disabled = Boolean(busy);
        if (!context) return;

        const { used, window: limit, left_percent: left } = context;
        els.context.textContent =
            `context ${left}% free — ${formatCount(used)} of ${formatCount(limit)} tokens used`;
        // The short form a narrow screen shows instead, and only once the window is
        // more than half used — the same point at which compact turns up beside it.
        els.context.dataset.short = `${left}% free`;
        els.context.classList.toggle("context--tight", left < 15);
        els.context.classList.toggle("context--roomy", left >= 50);
        // Offered once the conversation is using more than half the window; below
        // that there is nothing worth summarising.
    }

    // Warnings from the CLI — a refused request, a failed tool call — belong beside the
    // conversation, not in it. A count in the context bar, the detail behind a click,
    // and nothing at all once they have been read.
    const SEEN_NOTICES_KEY = "seenNotices";
    const SEEN_NOTICES_KEPT = 200;

    function noticeKey(notice) {
        return `${notice.at || ""}|${notice.kind}|${(notice.text || "").slice(0, 60)}`;
    }

    function seenNotices() {
        try {
            return new Set(JSON.parse(localStorage.getItem(SEEN_NOTICES_KEY)) || []);
        } catch {
            // Private mode, or something else wrote nonsense there: everything is new.
            return new Set();
        }
    }

    function rememberSeenNotices(keys) {
        try {
            const kept = [...keys].slice(-SEEN_NOTICES_KEPT);
            localStorage.setItem(SEEN_NOTICES_KEY, JSON.stringify(kept));
        } catch {
            // Nothing to do about it; they will read as new again next time.
        }
    }

    function renderNotices(incoming) {
        notices = incoming;
        const seen = seenNotices();
        const unread = notices.filter((notice) => !seen.has(noticeKey(notice)));

        // Hidden once they have all been read: a warning is worth one look, not a
        // permanent mark on the page.
        els.notices.hidden = unread.length === 0;
        els.noticesCount.textContent =
            `${unread.length} new notice${unread.length === 1 ? "" : "s"}`;
        if (!els.noticesOverlay.hidden) renderNoticesList();
    }

    function noticeElement(notice) {
        const row = document.createElement("div");
        row.className = unreadNotices.has(noticeKey(notice))
            ? "notice-row notice-row--new"
            : "notice-row";

        const head = document.createElement("p");
        head.className = "notice-row__head";
        head.textContent = [notice.kind.replace(/_/g, " "), notice.at && formatDate(notice.at)]
            .filter(Boolean)
            .join(" · ");

        const body = document.createElement("p");
        body.className = "notice-row__text";
        body.textContent = notice.text;

        row.append(head, body);
        return row;
    }

    function renderNoticesList() {
        // Newest first: the one that just happened is the one being looked for.
        els.noticesList.replaceChildren(...[...notices].reverse().map(noticeElement));
    }

    function closeNotices() {
        els.noticesOverlay.hidden = true;
        els.notices.focus();
    }

    function renderChat(chat) {
        const turns = chat.turns || [];
        const pending = chat.pending || [];
        const failed = chat.failed;
        const running = pending.find((job) => job.status === "running");
        runningTurn = running ? running.id : null;
        const queued = pending.filter((job) => job !== running);

        renderTitle(chat);
        els.transcriptEmpty.hidden = turns.length > 0 || pending.length > 0 || Boolean(failed);

        const nodes = turns.map((t) => turnElement(t.role, t.text, t.at));

        // Claude Code writes the user message into its transcript as soon as the turn
        // starts, so rendering the running job's prompt as well showed it twice.
        const lastUser = [...turns].reverse().find((t) => t.role === "user");
        if (running && !running.command) {
            if (!lastUser || lastUser.text.trim() !== (running.prompt || "").trim()) {
                nodes.push(turnElement("user", running.prompt, running.created_at));
            }
            nodes.push(workingElement(running));
        } else if (running) {
            nodes.push(commandNotice(running, true));
        }

        if (failed && !running) {
            if (failed.command) {
                nodes.push(commandNotice(failed, false));
            } else {
                const alreadyThere = lastUser && lastUser.text.trim() === (failed.prompt || "").trim();
                if (!alreadyThere) nodes.push(turnElement("user", failed.prompt, failed.created_at));
                nodes.push(
                    turnElement("claude", failed.error || "The run failed.", failed.finished_at, false, true),
                );
            }
        }

        // Whether to follow the text as it is written is the reader's call, not
        // ours: scrolling up to re-read something used to be undone a moment later
        // by the next poll, which forced the view down for as long as the turn ran.
        const following = followTail || atTail();

        els.transcript.replaceChildren(els.transcriptEmpty, ...nodes);
        if (following) scrollToTail();
        els.toLatest.hidden = following || (turns.length === 0 && !running);

        renderQueue(queued);
        renderContext(chat.context, Boolean(running));
        renderNotices(chat.notices || []);
        // The bar carries both, and either one on its own is reason to show it.
        els.contextbar.hidden = !chat.context && notices.length === 0;

        // Sending stays available: another message simply joins the queue, the way the
        // terminal accepts one while Claude is still working.
        els.send.textContent = running ? "Queue" : "Send";
        return pending.length > 0;
    }

    // The conversation's own name, which is Claude's generated title unless one was
    // set with /rename. Editable in place: the same command the history list uses.
    function renderTitle(chat) {
        if (titleEditing) return;
        currentSession = chat.session || null;
        els.chatTitle.textContent = chat.title || (chat.session ? "Untitled" : "New conversation");
        els.chatTitle.disabled = !chat.session;
    }

    function startTitleEdit() {
        if (!currentSession || titleEditing) return;
        titleEditing = true;

        const form = document.createElement("form");
        form.className = "title__form";

        const input = document.createElement("input");
        input.type = "text";
        input.value = els.chatTitle.textContent.trim();
        input.maxLength = 120;
        input.setAttribute("aria-label", "Conversation name");

        form.append(input);
        els.chatTitle.replaceWith(form);
        input.focus();
        input.select();

        const finish = () => {
            titleEditing = false;
            form.replaceWith(els.chatTitle);
            loadChat();
        };

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const title = input.value.trim();
            if (!title || title === els.chatTitle.textContent.trim()) return finish();
            input.disabled = true;
            try {
                await api("chat/rename", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ session: currentSession, title }),
                });
                els.chatTitle.textContent = title;
            } catch (error) {
                showError(`Could not rename: ${error.message}`);
            }
            finish();
        });

        input.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.stopPropagation();
                finish();
            }
        });
        input.addEventListener("blur", () => {
            if (titleEditing) form.requestSubmit();
        });
    }

    els.chatTitle.addEventListener("click", startTitleEdit);

    // The assistant side of a turn in progress: the text as it arrives, with a stop
    // control, and pulsing dots until the first words come through.
    function workingElement(job) {
        const turn = document.createElement("article");
        turn.className = "turn turn--claude turn--working";

        const head = document.createElement("header");
        head.className = "turn__head";

        const who = document.createElement("span");
        who.className = "turn__who";
        who.textContent = "claude";

        const pill = document.createElement("span");
        // A frozen turn is not a working one, and saying "working" about something that
        // will not move for hours leaves the only exit — cancel — as the obvious action,
        // which discards exactly what the freeze was protecting.
        pill.className = job.paused ? "pill is-paused" : "pill is-running";
        pill.textContent = job.paused ? "paused" : "working";

        const cancel = document.createElement("button");
        cancel.className = "ghost ghost--danger";
        cancel.type = "button";
        cancel.textContent = "cancel";
        cancel.title = "Stops this turn — the same as pressing Esc in the terminal";
        cancel.addEventListener("click", () => cancelTurn(job.id, cancel));

        head.append(who, pill);
        if (job.paused) {
            const letGo = document.createElement("button");
            letGo.className = "ghost";
            letGo.type = "button";
            letGo.textContent = "let it go";
            letGo.title = "Carries on from where it stopped, whatever the allowance says";
            letGo.addEventListener("click", async () => {
                letGo.disabled = true;
                try {
                    await api(`jobs/${encodeURIComponent(job.id)}/resume`, { method: "POST" });
                } catch (error) {
                    showError(`Could not let it go: ${error.message}`);
                    letGo.disabled = false;
                }
                loadChat();
            });
            head.append(letGo);
        }
        head.append(cancel);

        const body = document.createElement("div");
        if (job.partial) {
            body.className = "turn__body";
            body.textContent = job.partial;
            const caret = document.createElement("span");
            caret.className = "caret";
            body.append(caret);
        } else {
            body.className = "turn__body turn__body--working";
            body.append(
                document.createElement("span"),
                document.createElement("span"),
                document.createElement("span"),
            );
        }

        turn.append(head, body);
        return turn;
    }

    // Shared by the button and by Esc, which is what the terminal uses.
    async function cancelTurn(id, button) {
        if (button) {
            button.disabled = true;
            button.textContent = "cancelling…";
        }
        try {
            await api(`jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
        } catch (error) {
            showError(`Could not cancel: ${error.message}`);
            if (button) {
                button.disabled = false;
                button.textContent = "cancel";
            }
        }
        loadChat();
    }

    function commandNotice(job, busy) {
        const note = document.createElement("p");
        note.className = "command-note";
        note.textContent = busy
            ? `${job.command === "compact" ? "Compacting" : "Renaming"}…`
            : `${job.command} failed: ${job.error || "no detail given"}`;
        if (!busy) note.classList.add("command-note--error");
        return note;
    }

    // Queued messages sit below the transcript, visibly not yet Claude's: he has not
    // seen them, and will pick them up in order when the current turn finishes.
    function renderQueue(queued) {
        if (!queued.length) {
            els.queue.hidden = true;
            els.queue.replaceChildren();
            return;
        }
        const heading = document.createElement("p");
        heading.className = "rule-label";
        heading.textContent = `${queued.length} waiting — not seen by Claude yet`;

        const items = queued.map((job) => {
            const item = document.createElement("div");
            item.className = "queued";

            const text = document.createElement("span");
            text.className = "queued__text";
            text.textContent = job.prompt;

            const drop = document.createElement("button");
            drop.className = "ghost ghost--danger";
            drop.type = "button";
            drop.textContent = "remove";
            drop.addEventListener("click", async () => {
                drop.disabled = true;
                try {
                    await api(`jobs/${encodeURIComponent(job.id)}`, { method: "DELETE" });
                    loadChat();
                } catch (error) {
                    showError(`Could not remove: ${error.message}`);
                    drop.disabled = false;
                }
            });

            item.append(text, drop);
            return item;
        });

        els.queue.replaceChildren(heading, ...items);
        els.queue.hidden = false;
    }

    async function loadChat() {
        try {
            const chat = await api("chat");
            chatFailures = 0;
            const busy = renderChat(chat);
            scheduleChat(busy ? 1200 : 0);
        } catch (error) {
            // A blip must not stop tracking a reply that is still coming.
            if (++chatFailures >= POLL_FAILURES_ALLOWED) {
                showError(`Lost track of the conversation: ${error.message}`);
                scheduleChat(0);
            }
        }
    }

    function scheduleChat(period) {
        if (chatTimer) clearTimeout(chatTimer);
        chatTimer = period ? setTimeout(loadChat, period) : null;
    }

    els.composer.addEventListener("submit", async (event) => {
        event.preventDefault();
        const prompt = els.prompt.value.trim();
        if (!prompt) return;

        els.send.disabled = true;
        try {
            await api("chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    prompt,
                    model: els.model.value.trim() || undefined,
                    effort: els.effort.value || undefined,
                    permission_mode: els.mode.value || undefined,
                }),
            });
            els.prompt.value = "";
            scrollToTail();
            loadChat();
            loadHealth();
        } catch (error) {
            showError(`Could not send: ${error.message}`);
        } finally {
            els.send.disabled = false;
        }
    });

    // Enter sends, Shift+Enter breaks the line. Deliberately not bound while a
    // composition is in progress, or an IME's Enter would submit half a word.
    els.prompt.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
        event.preventDefault();
        els.composer.requestSubmit();
    });

    els.compact.addEventListener("click", async () => {
        els.compact.disabled = true;
        try {
            await api("chat/compact", { method: "POST" });
            loadChat();
        } catch (error) {
            showError(`Could not compact: ${error.message}`);
            els.compact.disabled = false;
        }
    });

    els.newChat.addEventListener("click", async () => {
        try {
            await api("chat/new", { method: "POST" });
            els.prompt.focus();
            loadChat();
        } catch (error) {
            showError(`Could not start a new conversation: ${error.message}`);
        }
    });

    // ------------------------------------------------------------------ history //

    function renderHistory() {
        const query = els.historySearch.value.trim().toLowerCase();
        const matches = query
            ? sessions.filter((s) =>
                  `${s.title} ${s.preview}`.toLowerCase().includes(query),
              )
            : sessions;

        els.historyEmpty.hidden = matches.length > 0;
        els.historyList.replaceChildren(
            ...matches.map((session) => {
                const item = document.createElement("button");
                item.className = "session";
                item.type = "button";

                const title = document.createElement("span");
                title.className = "session__title";
                title.textContent = session.title;

                const meta = document.createElement("span");
                meta.className = "session__meta";
                meta.textContent = `${session.messages} messages · ${formatDate(session.updated_at)}`;

                item.append(title, meta);
                item.addEventListener("click", async () => {
                    try {
                        await api("chat/resume", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ session: session.id }),
                        });
                        closeHistory();
                        loadChat();
                    } catch (error) {
                        showError(`Could not resume: ${error.message}`);
                    }
                });

                const row = document.createElement("div");
                row.className = "session__row";

                // Renaming sits outside the row's button: a control inside a button is
                // not valid markup, and clicking it must not also resume.
                const rename = iconButton("rename", "Rename this conversation",
                                          "session__act");
                rename.addEventListener("click", () => startRename(row, session));

                // Deleting takes two presses, and the button says which press it is on:
                // there is no undo, and the transcript is the whole conversation.
                const drop = iconButton("delete", "Delete this conversation",
                                        "session__act icon--danger");
                let armed = null;
                drop.addEventListener("click", async () => {
                    if (!armed) {
                        setIcon(drop, "sure");
                        drop.title = "Press again to delete — there is no undo";
                        drop.classList.add("is-armed");
                        // Disarmed on its own: a button left saying "sure?" is a trap for
                        // whoever comes back to the page later.
                        armed = window.setTimeout(() => {
                            armed = null;
                            setIcon(drop, "delete");
                            drop.title = "Delete this conversation";
                            drop.classList.remove("is-armed");
                        }, 5000);
                        return;
                    }
                    window.clearTimeout(armed);
                    armed = null;
                    drop.disabled = true;
                    try {
                        await api(`chat/sessions/${encodeURIComponent(session.id)}`,
                                  { method: "DELETE" });
                        await refreshSessions();
                        loadChat();
                    } catch (error) {
                        showError(`Could not delete: ${error.message}`);
                        drop.disabled = false;
                        setIcon(drop, "delete");
                        drop.title = "Delete this conversation";
                        drop.classList.remove("is-armed");
                    }
                });

                row.append(item, rename, drop);
                return row;
            }),
        );
    }

    // Renaming asks the CLI to do it, with /rename, so the new title lands in Claude
    // Code's own transcript and is what every client shows from then on.
    function startRename(row, session) {
        const form = document.createElement("form");
        form.className = "session__renameform";

        const input = document.createElement("input");
        input.type = "text";
        input.value = session.title;
        input.maxLength = 120;
        input.setAttribute("aria-label", "New title");

        const save = document.createElement("button");
        save.className = "ghost";
        save.type = "submit";
        save.textContent = "save";

        form.append(input, save);
        row.replaceChildren(form);
        input.focus();
        input.select();

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const title = input.value.trim();
            if (!title || title === session.title) return renderHistory();
            save.disabled = true;
            save.textContent = "saving…";
            try {
                await api("chat/rename", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ session: session.id, title }),
                });
                // The rename runs as a turn, so the transcript needs a moment before
                // the new title can be read back.
                session.title = title;
                renderHistory();
                setTimeout(refreshSessions, 4000);
            } catch (error) {
                showError(`Could not rename: ${error.message}`);
                renderHistory();
            }
        });

        input.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.stopPropagation();
                renderHistory();
            }
        });
    }

    async function refreshSessions() {
        try {
            const data = await api("chat/sessions");
            sessions = data.sessions || [];
            if (!els.overlay.hidden) renderHistory();
        } catch {
            // The overlay may well be closed by now; nothing to report.
        }
    }

    function closeHistory() {
        els.overlay.hidden = true;
        els.history.focus();
    }

    els.history.addEventListener("click", async () => {
        els.overlay.hidden = false;
        els.historySearch.value = "";
        els.historyList.replaceChildren();
        els.historySearch.focus();
        try {
            const data = await api("chat/sessions");
            sessions = data.sessions || [];
            renderHistory();
        } catch (error) {
            showError(`Could not list conversations: ${error.message}`);
            closeHistory();
        }
    });

    els.historySearch.addEventListener("input", renderHistory);
    els.historyClose.addEventListener("click", closeHistory);
    els.overlay.addEventListener("click", (event) => {
        if (event.target === els.overlay) closeHistory();
    });

    els.notices.addEventListener("click", () => {
        // What is new is worked out before they are marked as read, so the list that
        // opens can still show which ones you had not seen.
        const seen = seenNotices();
        unreadNotices = new Set(
            notices.map(noticeKey).filter((key) => !seen.has(key)),
        );

        els.noticesOverlay.hidden = false;
        renderNoticesList();
        els.noticesClose.focus();

        for (const notice of notices) seen.add(noticeKey(notice));
        rememberSeenNotices(seen);
        // The conversation is only polled while a turn is running, so the count is
        // brought up to date here rather than waiting for a redraw that may not come.
        renderNotices(notices);
    });
    els.noticesClose.addEventListener("click", closeNotices);
    els.noticesOverlay.addEventListener("click", (event) => {
        if (event.target === els.noticesOverlay) closeNotices();
    });

    // ---------------------------------------------------------------------- menu //

    // Below the breakpoint the actions live behind one button. The panel is the same
    // element either way — the stylesheet decides whether it is a row or a drop-down,
    // so there is no second copy of the buttons to keep in step.
    function closeMenu() {
        els.drawer.classList.remove("is-open");
        els.menu.setAttribute("aria-expanded", "false");
    }

    els.menu.addEventListener("click", (event) => {
        event.stopPropagation();
        const open = els.drawer.classList.toggle("is-open");
        els.menu.setAttribute("aria-expanded", String(open));
    });

    // Anything chosen inside it, or any click outside, puts it away again.
    els.tools.addEventListener("click", (event) => {
        if (event.target.closest("button, a")) closeMenu();
    });
    document.addEventListener("click", (event) => {
        // The backdrop is the drawer itself, so a tap anywhere but the card or the
        // button that opened it puts the drawer away.
        if (!els.tools.contains(event.target) && event.target !== els.menu) closeMenu();
    });

    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        // A sheet first, then the running turn — the same thing Esc does in the
        // terminal, and the only way to stop a long stream from the keyboard.
        if (els.drawer.classList.contains("is-open")) closeMenu();
        else if (!els.overlay.hidden) closeHistory();
        else if (!els.noticesOverlay.hidden) closeNotices();
        else if (!els.settings.hidden) closeSettings();
        else if (runningTurn) cancelTurn(runningTurn, null);
    });

    // ----------------------------------------------------------------- settings //

    // A table rather than a pair of booleans: the third tab is exactly the change the
    // boolean version would have had to be rewritten for.
    const TABS = {
        usage: { tab: els.tabUsage, pane: els.paneUsage },
        skills: { tab: els.tabSkills, pane: els.paneSkills },
        permissions: { tab: els.tabPermissions, pane: els.panePermissions },
        mcp: { tab: els.tabMcp, pane: els.paneMcp },
        memory: { tab: els.tabMemory, pane: els.paneMemory },
        config: { tab: els.tabConfig, pane: els.paneConfig },
        updates: { tab: els.tabUpdates, pane: els.paneUpdates },
    };

    function showTab(which) {
        for (const [name, { tab, pane }] of Object.entries(TABS)) {
            const active = name === which;
            pane.hidden = !active;
            tab.classList.toggle("is-active", active);
            tab.setAttribute("aria-selected", String(active));
        }
        if (which === "permissions") els.settingsJson.focus();
        if (which === "mcp") loadMcp();
        if (which === "memory") EDITORS.memory.load();
        if (which === "config") EDITORS.config.load();
        if (which === "usage") loadUsage();
        if (which === "updates") {
            // The binary's resolved path is the one fact /health does not carry.
            loadVersionDetail().catch(() => {
                els.uBinary.textContent = "";
            });
        }
    }

    // Validated as you type: Save stays out of reach while the JSON is broken, which
    // is better than accepting it and failing at the server.
    function validateSettings() {
        const text = els.settingsJson.value.trim();
        if (!text) {
            els.settingsStatus.textContent = "Empty — saving would write {}.";
            els.settingsStatus.className = "hint";
            els.settingsSave.disabled = false;
            return;
        }
        try {
            const parsed = JSON.parse(text);
            if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
                throw new Error("the top level must be an object");
            }
            els.settingsStatus.textContent = "Valid JSON.";
            els.settingsStatus.className = "hint hint--ok";
            els.settingsSave.disabled = false;
        } catch (error) {
            els.settingsStatus.textContent = error.message;
            els.settingsStatus.className = "hint hint--bad";
            els.settingsSave.disabled = true;
        }
    }

    async function loadSettings() {
        els.settingsStatus.textContent = "Loading…";
        els.settingsStatus.className = "hint";
        try {
            const data = await api("settings");
            els.settingsJson.value = JSON.stringify(data.settings, null, 2);
            const enforced = Object.keys(data.enforced_env || {}).join(", ");
            els.settingsPath.textContent =
                `${data.path} — Claude Code's own user settings. ` +
                (enforced ? `${enforced} is reapplied on save; the bundled ripgrep does not run here.` : "");
            validateSettings();
        } catch (error) {
            els.settingsStatus.textContent = `Could not load: ${error.message}`;
            els.settingsStatus.className = "hint hint--bad";
        }
    }

    els.settingsFormat.addEventListener("click", () => {
        try {
            els.settingsJson.value = JSON.stringify(JSON.parse(els.settingsJson.value), null, 2);
            validateSettings();
        } catch (error) {
            els.settingsStatus.textContent = `Cannot format: ${error.message}`;
            els.settingsStatus.className = "hint hint--bad";
        }
    });

    els.settingsReload.addEventListener("click", loadSettings);
    els.settingsJson.addEventListener("input", validateSettings);

    els.settingsSave.addEventListener("click", async () => {
        els.settingsSave.disabled = true;
        els.settingsSave.textContent = "Saving…";
        try {
            const data = await api("settings", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: els.settingsJson.value,
            });
            els.settingsJson.value = JSON.stringify(data.settings, null, 2);
            els.settingsStatus.textContent = "Saved. It applies to the next message.";
            els.settingsStatus.className = "hint hint--ok";
        } catch (error) {
            els.settingsStatus.textContent = `Not saved: ${error.message}`;
            els.settingsStatus.className = "hint hint--bad";
        } finally {
            els.settingsSave.textContent = "Save";
            validateSettings();
        }
    });

    function closeSettings() {
        els.settings.hidden = true;
        els.settingsOpen.focus();
    }

    els.settingsOpen.addEventListener("click", () => {
        els.settings.hidden = false;
        showTab("usage");
        loadSkills();
    });

    els.settingsClose.addEventListener("click", closeSettings);
    els.tabSkills.addEventListener("click", () => showTab("skills"));
    els.tabPermissions.addEventListener("click", () => {
        showTab("permissions");
        if (!els.settingsJson.value) loadSettings();
    });
    els.tabMcp.addEventListener("click", () => showTab("mcp"));
    els.tabMemory.addEventListener("click", () => showTab("memory"));
    els.tabConfig.addEventListener("click", () => showTab("config"));
    els.tabUsage.addEventListener("click", () => showTab("usage"));
    els.tabUpdates.addEventListener("click", () => showTab("updates"));

    els.settings.addEventListener("click", (event) => {
        if (event.target === els.settings) closeSettings();
    });

    // ------------------------------------------------------------------- skills //

    function tickIcon() {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 16 16");
        svg.setAttribute("width", "12");
        svg.setAttribute("height", "12");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("focusable", "false");
        const path = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
        path.setAttribute("points", "2.5,8.5 6,12 13.5,4");
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "currentColor");
        path.setAttribute("stroke-width", "2");
        path.setAttribute("stroke-linecap", "round");
        path.setAttribute("stroke-linejoin", "round");
        svg.append(path);
        return svg;
    }

    function skillRow(skill) {
        // <details> rather than a hand-rolled accordion: keyboard and screen-reader
        // behaviour come free, and a shared `name` makes the group exclusive in
        // browsers that support it while degrading to independent toggles elsewhere.
        const item = document.createElement("details");
        item.className = "skill";
        item.setAttribute("name", "skill");

        const head = document.createElement("summary");
        head.className = "skill__head";

        const name = document.createElement("span");
        name.className = "skill__name";
        name.textContent = skill.name;

        const meta = document.createElement("span");
        meta.className = "skill__meta";
        meta.textContent = [
            `${skill.files} files`,
            formatBytes(skill.bytes),
            formatDate(skill.updated_at),
        ].join(" · ");

        head.append(name, meta);

        if (!skill.has_skill_md) {
            const warn = document.createElement("span");
            warn.className = "skill__warn";
            warn.textContent = "no SKILL.md";
            head.append(warn);
        }
        item.append(head);

        const body = document.createElement("div");
        body.className = "skill__body";

        const desc = document.createElement("p");
        desc.className = "skill__desc";
        desc.textContent = skill.description || "No description in this skill's frontmatter.";
        body.append(desc);

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
            // An upload replaces whatever its SKILL.md names, so this is a shortcut to
            // the picker rather than a mode. Say which one it is meant for.
            els.dropText.textContent = "Choose a .tar.gz — it replaces whatever its SKILL.md names";
            els.file.click();
        });
        actions.append(reuse);

        // Two steps instead of confirm(): a modal dialog blocks the page. The first
        // click arms the button and it shows a tick; the second deletes.
        const remove = document.createElement("button");
        remove.className = "ghost ghost--danger";
        remove.textContent = "delete";
        let armed = false;
        const disarm = () => {
            armed = false;
            remove.replaceChildren(document.createTextNode("delete"));
            remove.classList.remove("ghost--armed");
        };
        remove.addEventListener("click", async () => {
            if (!armed) {
                armed = true;
                remove.replaceChildren(tickIcon(), document.createTextNode("confirm"));
                remove.classList.add("ghost--armed");
                setTimeout(() => {
                    if (armed) disarm();
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

        body.append(actions);
        item.append(body);
        return item;
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

    function clearPick() {
        picked = null;
        els.file.value = "";
        els.drop.classList.remove("is-loaded");
        els.dropText.innerHTML = DROP_DEFAULT;
        syncUploadButton();
    }

    function pickFile(file) {
        // `accept` on the input only filters the picker dialog; a file dropped on the
        // area went through whatever it was, to be refused by the server after the
        // whole upload had been sent.
        if (!/\.(tar\.gz|tgz|gz)$/i.test(file.name)) {
            showError(`${file.name} is not a .tar.gz. A skill is uploaded as a gzipped tar.`);
            return;
        }
        if (file.size > MAX_UPLOAD) {
            showError(`${file.name} is ${formatBytes(file.size)}; the limit is 256 mb.`);
            return;
        }
        picked = file;
        els.dropText.replaceChildren(
            document.createTextNode(`${file.name} · ${formatBytes(file.size)}`),
        );
        els.drop.classList.add("is-loaded");
        syncUploadButton();
    }

    function syncUploadButton() {
        els.upload.disabled = !picked;
    }

    els.file.addEventListener("change", () => {
        if (els.file.files.length) pickFile(els.file.files[0]);
    });

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
        els.upload.disabled = true;
        els.upload.textContent = "Installing…";
        try {
            // No name: the server reads it from the archive's SKILL.md.
            await api("skills", {
                method: "POST",
                headers: { "Content-Type": "application/gzip" },
                body: picked,
            });
            clearPick();
            await Promise.all([loadSkills(), loadHealth()]);
        } catch (error) {
            showError(`Upload failed: ${error.message}`);
        } finally {
            els.upload.textContent = "Install skill";
            syncUploadButton();
        }
    });

    // ------------------------------------------------------------ file editors //

    // Two files, one behaviour: read it, say what state it is in, save it back. The
    // permissions tab predates this and keeps its own validation, which is about the
    // shape of what is inside rather than about the file.
    function fileEditor({ key, text, status, path, saveButton, reload, format = null }) {
        const setStatus = (message, tone = "") => {
            status.textContent = message;
            status.className = tone ? `hint hint--${tone}` : "hint";
        };

        const editor = {
            load: async () => {
                try {
                    const file = await api(`files/${key}`);
                    text.value = file.text;
                    path.textContent = file.exists
                        ? file.path
                        : `${file.path} — not written yet`;
                    editor.validate();
                } catch (error) {
                    setStatus(error.message, "bad");
                    saveButton.disabled = true;
                }
            },

            // Markdown has nothing to validate, so the status is a character count;
            // JSON is checked as it is typed and Save stays out of reach until it
            // parses, rather than being accepted and failing at the server.
            validate: () => {
                if (format === null) {
                    setStatus(`${text.value.length} characters`);
                    saveButton.disabled = false;
                    return;
                }
                if (!text.value.trim()) {
                    setStatus("Empty — saving would write nothing.");
                    saveButton.disabled = false;
                    return;
                }
                try {
                    JSON.parse(text.value);
                    setStatus("Valid JSON.", "ok");
                    saveButton.disabled = false;
                } catch (error) {
                    setStatus(error.message, "bad");
                    saveButton.disabled = true;
                }
            },

            save: async () => {
                const label = saveButton.textContent;
                saveButton.disabled = true;
                saveButton.textContent = "Saving…";
                try {
                    await api(`files/${key}`, { method: "PUT", body: text.value });
                    setStatus("Saved.", "ok");
                } catch (error) {
                    setStatus(error.message, "bad");
                }
                saveButton.textContent = label;
                saveButton.disabled = false;
            },
        };

        text.addEventListener("input", editor.validate);
        saveButton.addEventListener("click", editor.save);
        reload.addEventListener("click", editor.load);
        if (format) {
            format.addEventListener("click", () => {
                try {
                    text.value = JSON.stringify(JSON.parse(text.value), null, 2);
                } catch {
                    // Nothing to reindent; validate has already said why.
                }
                editor.validate();
            });
        }
        return editor;
    }

    const EDITORS = {
        memory: fileEditor({
            key: "memory",
            text: $("memory-text"),
            status: $("memory-status"),
            path: $("memory-path"),
            saveButton: $("memory-save"),
            reload: $("memory-reload"),
        }),
        config: fileEditor({
            key: "config",
            text: $("config-text"),
            status: $("config-status"),
            path: $("config-path"),
            saveButton: $("config-save"),
            reload: $("config-reload"),
            format: $("config-format"),
        }),
    };

    // ---------------------------------------------------------------------- mcp //

    // Two groups, because where a server applies is the thing worth knowing about it:
    // one is available in every conversation, the other only in this working folder.
    const MCP_GROUPS = [
        { key: "user", heading: "Everywhere", note: "available in every conversation" },
        {
            key: "folder",
            heading: "This folder only",
            note: "configured for /data/chat, where the conversation runs",
        },
    ];

    function mcpRow(server) {
        const row = document.createElement("div");
        row.className = "mcp";

        const control = document.createElement("label");
        control.className = "switch";

        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = server.enabled;
        input.setAttribute("aria-label", `${server.name} is ${server.enabled ? "on" : "off"}`);
        input.addEventListener("change", () => toggleMcp(server.name, input));

        const track = document.createElement("span");
        track.className = "switch__track";
        track.setAttribute("aria-hidden", "true");

        control.append(input, track);

        const text = document.createElement("div");
        text.className = "mcp__text";

        const name = document.createElement("span");
        name.className = "mcp__name";
        name.textContent = server.name;

        const transport = document.createElement("span");
        transport.className = "pill";
        transport.textContent = server.transport;

        const summary = document.createElement("span");
        summary.className = "mcp__summary";
        summary.textContent = server.summary || "no command recorded";

        text.append(name, transport, summary);
        row.append(control, text);
        if (!server.enabled) row.classList.add("mcp--off");
        return row;
    }

    function renderMcp(servers) {
        els.mcpEmpty.hidden = servers.length > 0;
        const blocks = [];

        for (const group of MCP_GROUPS) {
            const inGroup = servers.filter((server) =>
                group.key === "user" ? server.scope === "user" : server.scope !== "user",
            );
            if (!inGroup.length) continue;

            const heading = document.createElement("h3");
            heading.className = "rule-label pane__sub";
            heading.textContent = `${group.heading} — ${group.note}`;
            blocks.push(heading, ...inGroup.map(mcpRow));
        }

        els.mcpList.replaceChildren(...blocks);
    }

    async function loadMcp() {
        try {
            renderMcp((await api("mcp")).servers || []);
        } catch (error) {
            showError(`Could not read the MCP servers: ${error.message}`);
        }
    }

    async function toggleMcp(name, input) {
        const wanted = input.checked;
        input.disabled = true;
        try {
            await api(`mcp/${encodeURIComponent(name)}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: wanted }),
            });
        } catch (error) {
            showError(`Could not switch ${name} ${wanted ? "on" : "off"}: ${error.message}`);
        }
        input.disabled = false;
        loadMcp();
    }

    // ------------------------------------------------------------------- update //

    els.uInstall.addEventListener("click", async () => {
        updateRequested = true;
        els.uInstall.disabled = true;
        els.uInstall.textContent = "Installing…";
        try {
            // Returns as soon as the install has been started; progress is polled.
            await api("update", { method: "POST" });
        } catch (error) {
            updateRequested = false;
            showError(`Could not start the update: ${error.message}`);
        }
        loadHealth();
    });

    // The only call that reaches the release channel, so it is on a button rather than
    // on the poll: nothing here should touch the network once a second.
    els.uCheck.addEventListener("click", async () => {
        els.uCheck.disabled = true;
        els.uCheck.textContent = "checking…";
        try {
            await loadVersionDetail({ refresh: true });
            await loadHealth();
        } catch (error) {
            showError(`Could not check for updates: ${error.message}`);
        }
        els.uCheck.textContent = "check now";
        els.uCheck.disabled = false;
    });

    // ------------------------------------------------------------------- usage //

    /** "in 2 hr 16 min", or "in 3 days" — how long until a window comes back. */
    function untilWhen(value) {
        const when = new Date(value);
        if (Number.isNaN(when.getTime())) return "";
        const minutes = Math.round((when.getTime() - Date.now()) / 60000);
        if (minutes <= 0) return "any moment now";
        if (minutes < 60) return `in ${minutes} min`;
        if (minutes < 60 * 24) {
            const hours = Math.floor(minutes / 60);
            const rest = minutes % 60;
            return `in ${hours} hr${rest ? ` ${rest} min` : ""}`;
        }
        const days = Math.round(minutes / (60 * 24));
        return `in ${days} day${days === 1 ? "" : "s"}`;
    }

    // How full is worth noticing, whatever the add-on is set to stop at: two thirds gone
    // is worth a glance, nine tenths is worth a plan.
    const AMBER_FROM = 70;
    const RED_FROM = 90;

    function showWindow(window, figure, bar, when, threshold, mark) {
        if (!window) {
            figure.textContent = "—";
            bar.style.width = "0";
            when.textContent = "not reported";
            mark.hidden = true;
            return;
        }
        const percent = Number(window.percent) || 0;
        // Red for either reason: nearly nothing left, or past the figure the add-on stops
        // at — which is why nothing is running, whatever the figure happens to be.
        const stopped = threshold > 0 && percent >= threshold;
        const spent = percent >= RED_FROM || stopped;
        figure.textContent = `${percent}% used`;
        bar.style.width = `${Math.min(100, percent)}%`;
        bar.classList.toggle("meter__fill--close", !spent && percent >= AMBER_FROM);
        bar.classList.toggle("meter__fill--spent", spent);
        // Where the guard stands, on the same line as what is used.
        mark.hidden = !(threshold > 0 && threshold < 100);
        mark.style.left = `${Math.min(100, Math.max(0, threshold))}%`;
        mark.title = `Work stops at ${threshold}%`;
        const local = new Date(window.resets_at);
        // The day and the hour, on Home Assistant's clock: nobody plans around a second,
        // and nobody reads a reset time in another country's zone.
        const stamp = Number.isNaN(local.getTime()) ? "" : local.toLocaleString("en-GB", {
            day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
            timeZone: houseZone,
        });
        const resets = stamp
            ? `Resets ${untilWhen(window.resets_at)} · ${stamp}`
            : "reset time not reported";
        when.textContent = stopped ? `Work is held until this resets · ${resets}` : resets;
        when.classList.toggle("hint--bad", stopped);
    }

    async function loadUsage({ refresh = false } = {}) {
        els.usageRefresh.disabled = true;
        try {
            const usage = await api(`usage${refresh ? "?refresh" : ""}`);
            const stops = usage.thresholds || {};
            if (!usage.available) {
                // A refusal for asking too often is not a sign-in problem, and saying so
                // sends somebody to the terminal for nothing.
                const waiting = usage.retry_at
                    ? ` The add-on will try again ${untilWhen(usage.retry_at)}.`
                    : " Signing in again in the terminal usually fixes it.";
                els.usageNote.textContent =
                    `The plan's allowance could not be read: ${usage.reason || "no reason given"}.`
                    + waiting + " Work is never held back on a reading that cannot be had.";
            } else {
                els.usageNote.textContent = usage.enough
                    ? ""
                    : "Over the figure this add-on stops at: a new turn is refused, and a running"
                      + " one is frozen where it stands until the window resets.";
            }
            els.usageNote.classList.toggle("hint--bad", usage.available && !usage.enough);
            showWindow(usage.session, els.usageSessionFigure, els.usageSessionBar,
                       els.usageSessionWhen, Number(stops.session) || 100, els.usageSessionMark);
            showWindow(usage.week, els.usageWeekFigure, els.usageWeekBar,
                       els.usageWeekWhen, Number(stops.week) || 100, els.usageWeekMark);
            const checked = new Date(usage.checked_at);
            els.usageChecked.textContent = Number.isNaN(checked.getTime())
                ? "" : `Last read ${checked.toLocaleTimeString("en-GB", { timeZone: houseZone })}`;
            els.usageRule.textContent = usage.available
                ? `Work stops at ${Number(stops.session) || 100}% of the five-hour window or`
                  + ` ${Number(stops.week) || 100}% of the week — whichever comes first.`
                  + " Both figures are the add-on's own settings."
                : "";
        } catch (error) {
            els.usageNote.textContent = String(error.message || error);
            els.usageNote.classList.add("hint--bad");
        } finally {
            els.usageRefresh.disabled = false;
        }
    }

    els.usageRefresh.addEventListener("click", () => loadUsage({ refresh: true }));


    // Coming back to a page that was left open: the figure may have moved a great deal.
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") refreshLimits({ force: true });
    });

    async function loadVersionDetail({ refresh = false } = {}) {
        const version = await api(`version${refresh ? "?refresh" : ""}`);
        els.uBinary.textContent = version.binary
            ? `Running ${version.binary}`
            : "The claude binary is not on PATH.";
        els.uAvailable.textContent = version.available || "not checked";
        els.uAvailable.classList.toggle("is-ok", Boolean(version.update_available));
    }

    // -------------------------------------------------------------------- start //

    els.refresh.addEventListener("click", () => {
        loadSkills();
        loadHealth();
    });

    loadHealth();
    loadSkills();
    loadChat();
    scheduleHealth(20000);
})();
