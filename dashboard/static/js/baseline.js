// Baseline classification validation — keyboard-driven UI
// Modes:
//   - classified: confirm/correct Klodo's predictions
//   - atrier:     provide ground-truth folder for unclassified files
(function () {
    "use strict";

    const cfg = window._baselineConfig || {};
    const folders = JSON.parse(
        document.getElementById("baseline-folders-data").textContent || "[]"
    );

    const state = {
        currentFileId: cfg.initialFileId,
        mode: cfg.mode,
        profile: cfg.profile,
        suggestionIndex: -1,
        currentSuggestions: [],
        pickerOpen: false,
    };

    // ─── DOM refs ────────────────────────────────────────────────────────
    const $thumb = document.getElementById("baseline-thumb");
    const $filename = document.getElementById("baseline-filename");
    const $relpath = document.getElementById("baseline-relpath");
    const $predicted = document.getElementById("baseline-predicted");
    const $picker = document.getElementById("baseline-folder-picker");
    const $input = document.getElementById("baseline-folder-input");
    const $suggestions = document.getElementById("baseline-folder-suggestions");
    const $confirm = document.getElementById("baseline-folder-confirm");
    const $cancel = document.getElementById("baseline-folder-cancel");
    const $progressFill = document.querySelector(".baseline-progress-fill");
    const $vCount = document.getElementById("baseline-validated");
    const $tCount = document.getElementById("baseline-total");
    const $cCount = document.getElementById("baseline-correct");
    const $wCount = document.getElementById("baseline-wrong");
    const $sCount = document.getElementById("baseline-skip");

    // ─── Verdict submission ──────────────────────────────────────────────
    async function submitVerdict(verdict, groundTruth) {
        if (!state.currentFileId) return;
        try {
            const resp = await fetch("/api/baseline/verdict", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    profile: state.profile,
                    mode: state.mode,
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
        if ($tCount) $tCount.textContent = stats.total;
        const correct = state.mode === "classified" ? stats.correct : stats.classified;
        if ($cCount) $cCount.textContent = correct;
        if ($wCount && stats.wrong !== undefined) $wCount.textContent = stats.wrong;
        if ($sCount) $sCount.textContent = stats.skip;
        if ($progressFill && stats.total) {
            $progressFill.style.width = (100 * stats.validated / stats.total) + "%";
        }
    }

    // ─── Load a new record into the card ─────────────────────────────────
    function loadRecord(record) {
        if (!record) return;
        state.currentFileId = record.file_id;
        $filename.textContent = record.filename;
        $relpath.textContent = record.rel_path;
        if ($predicted) $predicted.textContent = record.predicted_folder;
        $thumb.src = `/api/baseline/thumbnail/${record.file_id}?profile=${state.profile}&mode=${state.mode}`;
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
        const matches = q
            ? folders.filter(f => f.toLowerCase().includes(q))
            : folders.slice(0, 30);
        state.currentSuggestions = matches.slice(0, 30);
        state.suggestionIndex = state.currentSuggestions.length > 0 ? 0 : -1;
        $suggestions.innerHTML = "";
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
        const verdict = state.mode === "classified" ? "wrong" : "classified";
        submitVerdict(verdict, folder);
    }

    // ─── Wire events ─────────────────────────────────────────────────────
    document.querySelectorAll("[data-action]").forEach(btn => {
        btn.addEventListener("click", () => {
            const action = btn.dataset.action;
            if (action === "correct") {
                submitVerdict("correct");
            } else if (action === "wrong") {
                openFolderPicker();
            } else if (action === "skip") {
                submitVerdict("skip");
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
        // Don't intercept when typing in the input field
        if (state.pickerOpen) {
            if (e.key === "ArrowDown") { e.preventDefault(); moveSuggestion(1); return; }
            if (e.key === "ArrowUp") { e.preventDefault(); moveSuggestion(-1); return; }
            if (e.key === "Enter") { e.preventDefault(); confirmSelection(); return; }
            if (e.key === "Escape") { e.preventDefault(); closeFolderPicker(); return; }
            return; // let the input handle other keys (chars, backspace, etc.)
        }
        // Card-level shortcuts
        if (e.key === "y" || e.key === "Y") {
            if (state.mode === "classified") {
                e.preventDefault();
                submitVerdict("correct");
            }
        } else if (e.key === "n" || e.key === "N") {
            if (state.mode === "classified") {
                e.preventDefault();
                openFolderPicker();
            } else {
                // In atrier mode, N opens the picker too (it's the main action)
                e.preventDefault();
                openFolderPicker();
            }
        } else if (e.key === "s" || e.key === "S") {
            e.preventDefault();
            submitVerdict("skip");
        } else if (e.key === "f" || e.key === "F") {
            // F = Fill folder (alias for opening picker in atrier mode)
            if (state.mode === "atrier") {
                e.preventDefault();
                openFolderPicker();
            }
        }
    });

    // In atrier mode, auto-open the picker on load (it's the only useful action)
    if (state.mode === "atrier") {
        setTimeout(() => openFolderPicker(), 100);
    }
})();
