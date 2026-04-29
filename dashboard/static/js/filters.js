// filters.js — Helpers partagés de filtre/tri/pagination
// Chaque page doit définir `state` et `buildUrl` avant de charger ce fichier.

let searchTimer;

function debounceSearch(val) {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
        window.location.href = buildUrl({ search: val, page: 1 });
    }, 400);
}

function sortBy(col) {
    let newOrder = 'asc';
    if (state.sort === col && state.order === 'asc') {
        newOrder = 'desc';
    }
    window.location.href = buildUrl({ sort: col, order: newOrder, page: 1 });
}

function goToPage(p) {
    window.location.href = buildUrl({ page: p });
}

function applyFilters() {
    const status = document.getElementById('status-filter').value;
    const sectionEl = document.getElementById('section-filter');
    const section = sectionEl ? sectionEl.value : '';
    window.location.href = buildUrl({ status: status, section: section, page: 1 });
}
