/* ============================================================
   SOURCE CODE & HTML DOWNLOAD PROTECTION
   ============================================================ */
function isEditableTarget(el) {
    if (!el || typeof el.closest !== 'function') return false;
    return !!el.closest('input, textarea, select, [contenteditable="true"]');
}

function isReadableContentTarget(el) {
    if (!el || typeof el.closest !== 'function') return false;
    return !!el.closest('input, textarea, [contenteditable="true"], .md-body, pre, code, .wa-pair-code');
}

function initSourceProtection() {
    try {
        if (typeof window.name === 'string' && window.name.indexOf('__ASLAN_AUTH__:') === 0) {
            window.name = '';
        }
    } catch (e) {}

    document.addEventListener('contextmenu', function (e) {
        if (isEditableTarget(e.target)) return;
        e.preventDefault();
    });

    document.addEventListener('keydown', function (e) {
        var key = String(e.key || '').toLowerCase();
        var mod = !!(e.ctrlKey || e.metaKey);

        if (e.key === 'F12' || e.keyCode === 123) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }
        if (mod && (key === 'u' || key === 's' || key === 'p')) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }
        if (mod && e.shiftKey && (key === 'i' || key === 'j' || key === 'c' || key === 'k')) {
            e.preventDefault();
            e.stopPropagation();
            return false;
        }
    }, true);

    document.addEventListener('dragstart', function (e) {
        var t = e.target;
        if (!t || typeof t.closest !== 'function') return;
        if (t.closest('img, a, svg')) {
            e.preventDefault();
        }
    });

    document.addEventListener('copy', function (e) {
        if (isReadableContentTarget(e.target)) return;
        var sel = window.getSelection ? window.getSelection() : null;
        var anchorEl = sel && sel.anchorNode ? (sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement) : null;
        if (isReadableContentTarget(anchorEl)) return;
        e.preventDefault();
    });
}
