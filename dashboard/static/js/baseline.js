// Baseline classification — adjudicate disagreements between Klodo's
// current predictions and the current SSD layout.
//
// Verdicts (data-action):
//   klodo_right   → predicted folder is correct, current is wrong
//   actual_right  → current folder is correct, predicted is wrong
//   neither_right → both wrong, opens folder picker for ground truth
//   skip          → cannot tell (illegible / out of scope)
//
// Keyboard shortcuts:
//   K = klodo_right · A = actual_right · N = neither_right (open picker)
//   S = skip · ↓/↑ navigate suggestions · Enter confirm · Esc cancel
(function () {
    "use strict";

    const cfg = window._baselineConfig || {};
    const folders = JSON.parse(
        document.getElementById("baseline-folders-data").textContent || "[]"
    );

    const state = {
        profile: cfg.profile,
        runId: cfg.runId,
        currentFileId: cfg.initialFileId,
        suggestionIndex: -1,
        currentSuggestions: [],
        pickerOpen: false,
    };

    // ─── DOM refs ────────────────────────────────────────────────────────
    const $thumb = document.getElementById("baseline-thumb");
    const $filename = document.getElementById("baseline-filename");
    const $relpath = document.getElementById("baseline-relpath");
    const $predicted = document.getElementById("baseline-predicted");
    const $actual = document.getElementById("baseline-actual");
    const $picker = document.getElementById("baseline-folder-picker");
    const $input = document.getElementById("baseline-folder-input");
    const $suggestions = document.getElementById("baseline-folder-suggestions");
    const $confirm = document.getElementById("baseline-folder-confirm");
    const $cancel = document.getElementById("baseline-folder-cancel");
    const $progressFill = document.querySelector(".baseline-progress-fill");
    const $vCount = document.getElementById("baseline-validated");
    const $tCount = document.getElementById("baseline-total");
    const $kr = document.getElementById("baseline-kr");
    const $ar = document.getElementById("baseline-ar");
    const $nr = document.getElementById("baseline-nr");
    const $sk = document.getElementById("baseline-sk");

    // ─── Verdict submission ──────────────────────────────────────────────
    async function submitVerdict(verdict, groundTruth) {
        if (!state.currentFileId) return;
        try {
            const resp = await fetch("/api/baseline/verdict", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    profile: state.profile,
                    run_id: state.runId,
                    file_id: state.currentFileId,
                    verdict: verdict,
                    ground_truth: groundTruth || null,
                }),
            });
            const data = await resp.json();
            if (!resp.ok) {
                console.error("Verdict failed:", data);
                return;
            }
            updateStats(data.stats);
            if (data.done) {
                location.reload();
                return;
            }
            loadRecord(data.next);
        } catch (e) {
            console.error(e);
        }
    }

    // ─── Update visible stats ────────────────────────────────────────────
    function updateStats(stats) {
        if (!stats) return;
        if ($vCount) $vCount.textContent = stats.validated;
        if ($tCount) $tCount.textContent = stats.n_disagreements;
        if ($kr) $kr.textContent = stats.klodo_right;
        if ($ar) $ar.textContent = stats.actual_right;
        if ($nr) $nr.textContent = stats.neither_right;
        if ($sk) $sk.textContent = stats.skip;
        if ($progressFill && stats.n_disagreements) {
            $progressFill.style.width = (100 * stats.validated / stats.n_disagreements) + "%";
        }
    }

    // ─── Load a new record ───────────────────────────────────────────────
    const $visionMeta = document.getElementById("baseline-vision-meta");
    const $titleRow = document.getElementById("baseline-title-row");
    const $title = document.getElementById("baseline-title");
    const $themeRow = document.getElementById("baseline-theme-row");
    const $theme = document.getElementById("baseline-theme");
    const $confWrap = document.getElementById("baseline-conf-wrap");
    const $conf = document.getElementById("baseline-conf");

    function setRow(row, show) {
        if (row) row.style.display = show ? "" : "none";
    }

    function loadRecord(record) {
        if (!record) return;
        state.currentFileId = record.file_id;
        $filename.textContent = record.filename;
        $relpath.textContent = record.rel_path;
        if ($predicted) $predicted.textContent = record.predicted_folder;
        if ($actual) $actual.textContent = record.current_folder || "(racine)";
        $thumb.src = `/api/baseline/thumbnail/${record.file_id}?profile=${state.profile}&run_id=${state.runId}`;
        // Update vision metadata
        const title = record.title || "";
        const theme = record.theme || "";
        const conf = record.confidence || 0;
        if ($title) $title.textContent = title;
        if ($theme) $theme.textContent = theme;
        if ($conf) $conf.textContent = Math.round(conf * 100);
        setRow($titleRow, Boolean(title));
        setRow($themeRow, Boolean(theme));
        setRow($confWrap, Boolean(conf));
        if ($visionMeta) {
            $visionMeta.classList.toggle("hidden", !title && !theme);
        }
        closeFolderPicker();
    }

    // ─── Folder picker (autocomplete) ────────────────────────────────────
    function openFolderPicker() {
        state.pickerOpen = true;
        $picker.classList.remove("hidden");
        $input.value = "";
        renderSuggestions("");
        setTimeout(() => $input.focus(), 50);
    }

    function closeFolderPicker() {
        state.pickerOpen = false;
        $picker.classList.add("hidden");
        state.currentSuggestions = [];
        state.suggestionIndex = -1;
        $confirm.disabled = true;
    }

    function renderSuggestions(query) {
        const q = (query || "").toLowerCase().trim();
        // Empty query → no suggestions (avoid misleading "first N" alphabetical slice)
        if (!q) {
            state.currentSuggestions = [];
            state.suggestionIndex = -1;
            $suggestions.innerHTML =
                '<div class="baseline-suggestion-hint">' +
                'Tape au moins 1 caractère pour filtrer parmi les ' + folders.length +
                ' dossiers (ex. "info", "physique", "ml", "musique"…)</div>';
            $confirm.disabled = true;
            return;
        }
        const matches = folders.filter(f => f.toLowerCase().includes(q));
        // Cap at 50 to keep DOM light, sort by length so most-specific (shortest) match first
        state.currentSuggestions = matches.slice(0, 50);
        state.suggestionIndex = state.currentSuggestions.length > 0 ? 0 : -1;
        $suggestions.innerHTML = "";
        if (state.currentSuggestions.length === 0) {
            $suggestions.innerHTML =
                '<div class="baseline-suggestion-hint">Aucun dossier ne contient "' +
                q.replace(/[<>]/g, "") + '"</div>';
            return;
        }
        state.currentSuggestions.forEach((f, i) => {
            const div = document.createElement("div");
            div.className = "baseline-suggestion-item" + (i === 0 ? " selected" : "");
            div.textContent = f;
            div.dataset.index = i;
            div.addEventListener("click", () => {
                state.suggestionIndex = i;
                confirmSelection();
            });
            div.addEventListener("mouseenter", () => {
                state.suggestionIndex = i;
                refreshSelection();
            });
            $suggestions.appendChild(div);
        });
        if (matches.length > 50) {
            const more = document.createElement("div");
            more.className = "baseline-suggestion-hint";
            more.textContent = "… et " + (matches.length - 50) + " autres. Affine la recherche.";
            $suggestions.appendChild(more);
        }
        $confirm.disabled = state.suggestionIndex < 0;
    }

    function refreshSelection() {
        const items = $suggestions.querySelectorAll(".baseline-suggestion-item");
        items.forEach((el, i) => {
            el.classList.toggle("selected", i === state.suggestionIndex);
            if (i === state.suggestionIndex) el.scrollIntoView({ block: "nearest" });
        });
        $confirm.disabled = state.suggestionIndex < 0;
    }

    function moveSuggestion(delta) {
        if (state.currentSuggestions.length === 0) return;
        let i = state.suggestionIndex + delta;
        if (i < 0) i = 0;
        if (i >= state.currentSuggestions.length) i = state.currentSuggestions.length - 1;
        state.suggestionIndex = i;
        refreshSelection();
    }

    function confirmSelection() {
        if (state.suggestionIndex < 0) return;
        const folder = state.currentSuggestions[state.suggestionIndex];
        if (!folder) return;
        submitVerdict("neither_right", folder);
    }

    // ─── Wire events ─────────────────────────────────────────────────────
    document.querySelectorAll("[data-action]").forEach(btn => {
        btn.addEventListener("click", () => {
            const action = btn.dataset.action;
            if (action === "neither_right") {
                openFolderPicker();
            } else {
                submitVerdict(action);
            }
        });
    });

    if ($input) {
        $input.addEventListener("input", () => renderSuggestions($input.value));
    }
    if ($confirm) {
        $confirm.addEventListener("click", () => confirmSelection());
    }
    if ($cancel) {
        $cancel.addEventListener("click", () => closeFolderPicker());
    }

    // ─── Keyboard shortcuts ──────────────────────────────────────────────
    document.addEventListener("keydown", (e) => {
        if (state.pickerOpen) {
            if (e.key === "ArrowDown") { e.preventDefault(); moveSuggestion(1); return; }
            if (e.key === "ArrowUp") { e.preventDefault(); moveSuggestion(-1); return; }
            if (e.key === "Enter") { e.preventDefault(); confirmSelection(); return; }
            if (e.key === "Escape") { e.preventDefault(); closeFolderPicker(); return; }
            return;
        }
        const key = e.key.toLowerCase();
        if (key === "k") { e.preventDefault(); submitVerdict("klodo_right"); }
        else if (key === "a") { e.preventDefault(); submitVerdict("actual_right"); }
        else if (key === "n") { e.preventDefault(); openFolderPicker(); }
        else if (key === "s") { e.preventDefault(); submitVerdict("skip"); }
    });
})();
