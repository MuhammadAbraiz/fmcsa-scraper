// Shared helpers loaded on every page (see base.html).

// Carrier data (legal_name, address, cargo_carried, etc.) comes from FMCSA's
// public registry — external, uncontrolled input. Call log notes are agent
// free-text. Anything from either source that gets inserted via innerHTML
// must be escaped, or a carrier/agent could plant HTML/script that runs in
// another user's browser (stored XSS).
function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
window.escapeHtml = escapeHtml;

// When deployed behind a reverse proxy that mounts the app under a sub-path
// and strips that prefix before forwarding (see URL_PREFIX in
// app/__init__.py), a hardcoded '/search' etc. in a fetch() call resolves
// relative to the domain root, not the mounted path — bypassing the proxy's
// routing entirely and hitting whatever else is mounted at '/'. Every
// same-origin fetch() in this app must go through this helper instead of a
// bare string literal.
function apiUrl(path) {
    return (window.URL_PREFIX || '') + path;
}
window.apiUrl = apiUrl;
