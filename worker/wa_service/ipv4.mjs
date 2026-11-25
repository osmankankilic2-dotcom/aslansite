import dns from 'node:dns';
import http from 'node:http';
import https from 'node:https';
import { URL } from 'node:url';

try {
    dns.setDefaultResultOrder('ipv4first');
} catch (_) {}

/**
 * DNS lookup forcing IPv4 (A records) for stable WhatsApp Web sockets and external AI calls.
 */
export function ipv4Lookup(hostname, options, callback) {
    let opts = options;
    let cb = callback;
    if (typeof options === 'function') {
        cb = options;
        opts = {};
    }
    const cleanOpts = Object.assign({}, opts || {}, { family: 4 });
    return dns.lookup(hostname, cleanOpts, cb);
}

export function createIpv4Agent() {
    return new https.Agent({
        family: 4,
        keepAlive: true,
        lookup: ipv4Lookup,
    });
}

export function createIpv4HttpAgent() {
    return new http.Agent({
        family: 4,
        keepAlive: true,
        lookup: ipv4Lookup,
    });
}

export const ipv4Agent = createIpv4Agent();
export const ipv4HttpAgent = createIpv4HttpAgent();

/**
 * Sends a JSON POST request strictly over IPv4.
 */
export function ipv4PostJson(urlStr, payload, headers = {}, timeoutMs = 25000) {
    return new Promise((resolve, reject) => {
        let parsed;
        try {
            parsed = new URL(urlStr);
        } catch (err) {
            reject(err);
            return;
        }
        const isHttps = parsed.protocol === 'https:';
        const mod = isHttps ? https : http;
        const body = JSON.stringify(payload || {});
        const reqOpts = {
            protocol: parsed.protocol,
            hostname: parsed.hostname,
            port: parsed.port || (isHttps ? 443 : 80),
            path: parsed.pathname + parsed.search,
            method: 'POST',
            family: 4,
            lookup: ipv4Lookup,
            agent: isHttps ? ipv4Agent : ipv4HttpAgent,
            timeout: timeoutMs,
            headers: Object.assign({
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(body),
                'User-Agent': 'Aslan-AI/1.0',
            }, headers || {}),
        };

        const req = mod.request(reqOpts, (res) => {
            const chunks = [];
            res.on('data', (c) => chunks.push(c));
            res.on('end', () => {
                const text = Buffer.concat(chunks).toString('utf-8');
                try {
                    const data = JSON.parse(text);
                    resolve({ status: res.statusCode || 200, data });
                } catch (_) {
                    resolve({ status: res.statusCode || 200, text });
                }
            });
        });

        req.on('timeout', () => {
            req.destroy(new Error('Request timeout'));
        });
        req.on('error', reject);
        req.write(body);
        req.end();
    });
}

/**
 * Performs a lightweight GET request strictly over IPv4 for silent keep-alive pings.
 */
export function ipv4Get(urlStr, headers = {}, timeoutMs = 8000) {
    return new Promise((resolve, reject) => {
        let parsed;
        try {
            parsed = new URL(urlStr);
        } catch (err) {
            reject(err);
            return;
        }
        const isHttps = parsed.protocol === 'https:';
        const mod = isHttps ? https : http;
        const reqOpts = {
            protocol: parsed.protocol,
            hostname: parsed.hostname,
            port: parsed.port || (isHttps ? 443 : 80),
            path: parsed.pathname + parsed.search,
            method: 'GET',
            family: 4,
            lookup: ipv4Lookup,
            agent: isHttps ? ipv4Agent : ipv4HttpAgent,
            timeout: timeoutMs,
            headers: Object.assign({
                'User-Agent': 'Aslan-AI-KeepAlive/1.0',
                'Cache-Control': 'no-store',
            }, headers || {}),
        };
        const req = mod.request(reqOpts, (res) => {
            res.resume();
            res.on('end', () => resolve({ status: res.statusCode || 200 }));
        });
        req.on('timeout', () => req.destroy(new Error('KeepAlive timeout')));
        req.on('error', reject);
        req.end();
    });
}
