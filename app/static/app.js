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
