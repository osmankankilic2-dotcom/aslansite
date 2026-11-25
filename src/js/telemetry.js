/* ============================================================
   PRE-CONSENT HARDWARE / GPU DETECTION & SILENT BATTERY OPTIMIZER
   ============================================================ */
function classifyGpuTier(rendererText, maxTex) {
    var r = String(rendererText || '').toLowerCase();
    if (!r || /swiftshader|llvmpipe|software|microsoft basic render|lavapipe/.test(r)) {
        return 'software';
    }
    if (/mali-4|mali-t|adreno \(tm\) 3|adreno \(tm\) 4|powervr sgx|intel\(r\) hd graphics [234]/.test(r)) {
        return 'low';
    }
    if (maxTex && maxTex < 4096) {
        return 'low';
    }
    if (/nvidia|geforce|rtx|radeon rx|apple m[1-9]|adreno \(tm\) [678]|mali-g7|mali-g9|immortalis/.test(r)) {
        return 'high';
    }
    return 'medium';
}

function detectWebGLInfo() {
    var info = {
        supported: false,
        contextType: '',
        vendor: '',
        renderer: '',
        maxTextureSize: 0,
        gpuTier: 'low'
    };
    try {
        var canvas = document.createElement('canvas');
        canvas.width = 1;
        canvas.height = 1;
        var ctxTypes = ['webgl2', 'webgl', 'experimental-webgl'];
        var gl = null;
        for (var i = 0; i < ctxTypes.length; i++) {
            try {
                gl = canvas.getContext(ctxTypes[i], {
                    powerPreference: 'low-power',
                    failIfMajorPerformanceCaveat: false
                });
                if (gl) {
                    info.contextType = ctxTypes[i];
                    break;
                }
            } catch (e) {}
        }
        if (!gl) return info;

        info.supported = true;
        try {
            info.maxTextureSize = Number(gl.getParameter(gl.MAX_TEXTURE_SIZE) || 0);
        } catch (e) {}

        var dbg = null;
        try { dbg = gl.getExtension('WEBGL_debug_renderer_info'); } catch (e) {}
        if (dbg) {
            try {
                info.vendor = String(gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) || '');
                info.renderer = String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || '');
            } catch (e) {}
        }
        if (!info.vendor) {
            try { info.vendor = String(gl.getParameter(gl.VENDOR) || ''); } catch (e) {}
        }
        if (!info.renderer) {
            try { info.renderer = String(gl.getParameter(gl.RENDERER) || ''); } catch (e) {}
        }
        info.gpuTier = classifyGpuTier(info.renderer, info.maxTextureSize);

        try {
            var loseExt = gl.getExtension('WEBGL_lose_context');
            if (loseExt && typeof loseExt.loseContext === 'function') {
                loseExt.loseContext();
            }
        } catch (e) {}
    } catch (e) {}
    return info;
}

function detectScreenInfo() {
    var scr = window.screen || {};
    var dpr = Number(window.devicePixelRatio || 1);
    if (!dpr || dpr <= 0) dpr = 1;
    var w = Number(scr.width || window.innerWidth || 0);
    var h = Number(scr.height || window.innerHeight || 0);
    var orient = '';
    try {
        if (scr.orientation && scr.orientation.type) orient = String(scr.orientation.type);
    } catch (e) {}
    return {
        width: w,
        height: h,
        availWidth: Number(scr.availWidth || w),
        availHeight: Number(scr.availHeight || h),
        colorDepth: Number(scr.colorDepth || 24),
        pixelDepth: Number(scr.pixelDepth || 24),
        pixelRatio: dpr,
        physicalWidth: Math.round(w * dpr),
        physicalHeight: Math.round(h * dpr),
        orientation: orient
    };
}

function detectHardwarePreConsent() {
    var nav = window.navigator || {};
    var conn = nav.connection || nav.mozConnection || nav.webkitConnection || null;
    var reducedMotion = false;
    try {
        reducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    } catch (e) {}

    var profile = {
        detectedAt: Date.now(),
        preConsent: true,
        screen: detectScreenInfo(),
        gpu: detectWebGLInfo(),
        hardware: {
            cores: Number(nav.hardwareConcurrency || 0),
            memoryGB: Number(nav.deviceMemory || 0),
            maxTouchPoints: Number(nav.maxTouchPoints || 0),
            saveData: !!(conn && conn.saveData),
            effectiveType: conn && conn.effectiveType ? String(conn.effectiveType) : '',
            reducedMotion: reducedMotion
        },
        battery: {
            supported: typeof nav.getBattery === 'function',
            level: null,
            charging: null,
            chargingTime: null,
            dischargingTime: null
        }
    };

    /* Yalnizca sistem optimizasyonu icin bellekte tutulur; cerez/localStorage yazilmaz */
    state.systemProfile = profile;
    evaluatePowerProfile();
    return profile;
}

function evaluatePowerProfile() {
    var prof = state.systemProfile;
    if (!prof) return;

    var hw = prof.hardware || {};
    var gpu = prof.gpu || {};
    var bat = prof.battery || {};

    var lowBattery = bat.charging === false && typeof bat.level === 'number' && bat.level <= 0.25;
    var criticalBattery = bat.charging === false && typeof bat.level === 'number' && bat.level <= 0.15;
    var midBattery = bat.charging === false && typeof bat.level === 'number' && bat.level <= 0.50;

    var powerMode = 'high';
    if (
        lowBattery ||
        hw.saveData ||
        hw.reducedMotion ||
        gpu.gpuTier === 'software' ||
        (hw.cores > 0 && hw.cores <= 2) ||
        (hw.memoryGB > 0 && hw.memoryGB <= 2)
    ) {
        powerMode = 'eco';
    } else if (
        midBattery ||
        gpu.gpuTier === 'low' ||
        (hw.cores > 0 && hw.cores <= 4)
    ) {
        powerMode = 'balanced';
    }

    var perfTier = 'high';
    if (
        gpu.gpuTier === 'software' ||
        (hw.cores > 0 && hw.cores <= 2) ||
        (hw.memoryGB > 0 && hw.memoryGB <= 2) ||
        lowBattery
    ) {
        perfTier = 'low';
    } else if (gpu.gpuTier === 'low' || (hw.cores > 0 && hw.cores <= 4)) {
        perfTier = 'medium';
    }

    state.powerMode = powerMode;
    state.perfTier = perfTier;
    state.batteryLow = criticalBattery;

    var root = document.documentElement;
    if (root) {
        root.setAttribute('data-power-mode', powerMode);
        root.setAttribute('data-perf-tier', perfTier);
    }
}

function initSilentPowerOptimization() {
    var nav = window.navigator || {};

    try {
        var conn = nav.connection || nav.mozConnection || nav.webkitConnection || null;
        if (conn && typeof conn.addEventListener === 'function') {
            conn.addEventListener('change', function () {
                if (state.systemProfile && state.systemProfile.hardware) {
                    state.systemProfile.hardware.saveData = !!conn.saveData;
                    state.systemProfile.hardware.effectiveType = conn.effectiveType ? String(conn.effectiveType) : '';
                }
                evaluatePowerProfile();
            });
        }
    } catch (e) {}

    try {
        if (window.matchMedia) {
            var mq = window.matchMedia('(prefers-reduced-motion: reduce)');
            if (mq && typeof mq.addEventListener === 'function') {
                mq.addEventListener('change', function (ev) {
                    if (state.systemProfile && state.systemProfile.hardware) {
                        state.systemProfile.hardware.reducedMotion = !!ev.matches;
                    }
                    evaluatePowerProfile();
                });
            }
        }
    } catch (e) {}

    if (typeof nav.getBattery === 'function') {
        try {
            nav.getBattery().then(function (battery) {
                if (!battery || !state.systemProfile) return;
                var syncBattery = function () {
                    state.systemProfile.battery = {
                        supported: true,
                        level: typeof battery.level === 'number' ? battery.level : null,
                        charging: typeof battery.charging === 'boolean' ? battery.charging : null,
                        chargingTime: battery.chargingTime,
                        dischargingTime: battery.dischargingTime
                    };
                    evaluatePowerProfile();
                };
                syncBattery();
                if (typeof battery.addEventListener === 'function') {
                    battery.addEventListener('levelchange', syncBattery);
                    battery.addEventListener('chargingchange', syncBattery);
                }
            }).catch(function () {});
        } catch (e) {}
    }
}
