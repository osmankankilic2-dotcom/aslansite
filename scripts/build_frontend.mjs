#!/usr/bin/env node
/**
 * ASLAN Technologies - Production Frontend Build Pipeline
 * Combines modular src/js/* into a closed IIFE, minifies JS/CSS/HTML/SW,
 * injects content hash, and validates syntax via `node --check`.
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const SRC_DIR = path.join(ROOT, 'src');
const PUBLIC_DIR = path.join(ROOT, 'public');
const ASSETS_DIR = path.join(PUBLIC_DIR, 'assets');

const JS_ORDER = [
    'state.js',
    'telemetry.js',
    'guard.js',
    'consent.js',
    'pwa.js',
    'ui.js',
    'auth.js',
    'tts.js',
    'ai.js',
    'main.js',
];

const REGEX_PREV_KEYWORDS = new Set([
    'return', 'throw', 'case', 'delete', 'typeof', 'void',
    'in', 'instanceof', 'new', 'else', 'yield', 'await', 'do', 'of'
]);

/**
 * Strips JS comments safely while preserving string, template, and regex literals,
 * then compacts whitespace without breaking Automatic Semicolon Insertion (ASI).
 */
function minifyJsSafe(source) {
    const src = String(source || '');
    const len = src.length;
    let out = '';
    let i = 0;
    let lastNonWsChar = '';
    let lastWord = '';

    function canPrecedeRegex() {
        if (!lastNonWsChar) return true;
        if ('=(,[!&|?:;{}+-*/%<>^~'.includes(lastNonWsChar)) return true;
        if (lastWord && REGEX_PREV_KEYWORDS.has(lastWord)) return true;
        return false;
    }

    function readIdentifierBack(endIdx) {
        let k = endIdx;
        while (k >= 0 && /[A-Za-z0-9_$]/.test(src[k])) k--;
        return src.slice(k + 1, endIdx + 1);
    }

    while (i < len) {
        const ch = src[i];
        const next = src[i + 1] || '';

        // Single-line comment
        if (ch === '/' && next === '/') {
            i += 2;
            while (i < len && src[i] !== '\n') i++;
            continue;
        }

        // Block comment
        if (ch === '/' && next === '*') {
            i += 2;
            while (i < len && !(src[i] === '*' && src[i + 1] === '/')) i++;
            i += 2;
            out += ' ';
            continue;
        }

        // String literal (' or ")
        if (ch === '\'' || ch === '"') {
            const quote = ch;
            let lit = ch;
            i++;
            while (i < len) {
                const c = src[i];
                lit += c;
                if (c === '\\' && i + 1 < len) {
                    lit += src[i + 1];
                    i += 2;
                    continue;
                }
                if (c === quote) {
                    i++;
                    break;
                }
                i++;
            }
            out += lit;
            lastNonWsChar = quote;
            lastWord = '';
            continue;
        }

        // Template literal
        if (ch === '`') {
            let lit = '`';
            i++;
            let depth = 0;
            while (i < len) {
                const c = src[i];
                lit += c;
                if (c === '\\' && i + 1 < len) {
                    lit += src[i + 1];
                    i += 2;
                    continue;
                }
                if (c === '$' && src[i + 1] === '{') {
                    lit += '{';
                    depth++;
                    i += 2;
                    continue;
                }
                if (c === '}' && depth > 0) {
                    depth--;
                    i++;
                    continue;
                }
                if (c === '`' && depth === 0) {
                    i++;
                    break;
                }
                i++;
            }
            out += lit;
            lastNonWsChar = '`';
            lastWord = '';
            continue;
        }

        // Regex literal
        if (ch === '/' && canPrecedeRegex()) {
            let lit = '/';
            i++;
            let inClass = false;
            while (i < len) {
                const c = src[i];
                lit += c;
                if (c === '\\' && i + 1 < len) {
                    lit += src[i + 1];
                    i += 2;
                    continue;
                }
                if (c === '[') inClass = true;
                else if (c === ']' && inClass) inClass = false;
                else if (c === '/' && !inClass) {
                    i++;
                    while (i < len && /[a-z]/i.test(src[i])) {
                        lit += src[i];
                        i++;
                    }
                    break;
                } else if (c === '\n') {
                    break;
                }
                i++;
            }
            out += lit;
            lastNonWsChar = '/';
            lastWord = '';
            continue;
        }

        if (ch === '\r') {
            i++;
            continue;
        }

        out += ch;
        if (!/\s/.test(ch)) {
            lastNonWsChar = ch;
            if (/[A-Za-z0-9_$]/.test(ch)) {
                lastWord = readIdentifierBack(i);
            } else {
                lastWord = '';
            }
        }
        i++;
    }

    // Compact lines safely without collapsing ASI boundaries
    const lines = out.split('\n').map(l => l.replace(/[ \t]+/g, ' ').trim()).filter(Boolean);
    let compact = '';
    for (let idx = 0; idx < lines.length; idx++) {
        const cur = lines[idx];
        if (!compact) {
            compact = cur;
            continue;
        }
        const prevChar = compact[compact.length - 1];
        const nextChar = cur[0];
        if (';{,(['.includes(prevChar) || (prevChar === '}' && '}),;'.includes(nextChar))) {
            compact += cur;
        } else {
            compact += '\n' + cur;
        }
    }
    return compact;
}

function minifyCssSafe(css) {
    return String(css || '')
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/\s+/g, ' ')
        .replace(/\s*([{}:;,>+~])\s*/g, '$1')
        .replace(/;}/g, '}')
        .trim();
}

function minifyHtmlSafe(html) {
    return String(html || '')
        .replace(/<!--[\s\S]*?-->/g, '')
        .replace(/>\s+</g, '><')
        .replace(/\s{2,}/g, ' ')
        .trim() + '\n';
}

function main() {
    fs.mkdirSync(ASSETS_DIR, { recursive: true });

    // Remove any source map files in public/
    for (const dir of [PUBLIC_DIR, ASSETS_DIR]) {
        if (!fs.existsSync(dir)) continue;
        for (const f of fs.readdirSync(dir)) {
            if (f.endsWith('.map')) {
                fs.unlinkSync(path.join(dir, f));
            }
        }
    }

    // 1) CSS
    const rawCss = fs.readFileSync(path.join(SRC_DIR, 'css', 'main.css'), 'utf8');
    const minCss = minifyCssSafe(rawCss);

    // 2) JS bundle (closed IIFE)
    const jsParts = JS_ORDER.map(name => {
        const full = path.join(SRC_DIR, 'js', name);
        return fs.readFileSync(full, 'utf8');
    });
    const wrappedJs = '(function(){\n"use strict";\n' + jsParts.join('\n') + '\n})();';
    const minJs = minifyJsSafe(wrappedJs);

    // 3) Build hash from CSS + JS + HTML + SW templates
    const rawHtml = fs.readFileSync(path.join(SRC_DIR, 'index.html'), 'utf8');
    const rawSw = fs.readFileSync(path.join(SRC_DIR, 'sw.js'), 'utf8');
    const hash = crypto
        .createHash('sha256')
        .update(minCss)
        .update(minJs)
        .update(rawHtml)
        .update(rawSw)
        .digest('hex')
        .slice(0, 8);

    const outCssPath = path.join(ASSETS_DIR, 'app.min.css');
    const outJsPath = path.join(ASSETS_DIR, 'app.min.js');
    const outHtmlPath = path.join(PUBLIC_DIR, 'index.html');
    const outSwPath = path.join(PUBLIC_DIR, 'sw.js');

    const finalHtml = minifyHtmlSafe(rawHtml.replaceAll('__BUILD_HASH__', hash));
    const finalSw = minifyJsSafe(rawSw.replaceAll('__BUILD_HASH__', hash));

    fs.writeFileSync(outCssPath, minCss, 'utf8');
    fs.writeFileSync(outJsPath, minJs, 'utf8');
    fs.writeFileSync(outHtmlPath, finalHtml, 'utf8');
    fs.writeFileSync(outSwPath, finalSw, 'utf8');

    // Validate generated JS syntax
    execFileSync(process.execPath, ['--check', outJsPath], { stdio: 'inherit' });
    execFileSync(process.execPath, ['--check', outSwPath], { stdio: 'inherit' });

    console.log(
        `Built frontend (hash=${hash}): ` +
        `index.html=${Buffer.byteLength(finalHtml)}B, ` +
        `app.min.css=${Buffer.byteLength(minCss)}B, ` +
        `app.min.js=${Buffer.byteLength(minJs)}B, ` +
        `sw.js=${Buffer.byteLength(finalSw)}B`
    );
}

main();
