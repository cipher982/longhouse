/**
 * Swarmlet Funnel Tracking
 *
 * Tracks the complete user journey:
 * 1. Page view / JS loaded
 * 2. Human detection (mouse, scroll, keyboard)
 * 3. CTA clicks
 * 4. Auth funnel (modal, submit, complete)
 * 5. Engagement (scroll depth)
 */

(function() {
    'use strict';

    // Configuration
    // Use API_BASE_URL for split deployment (e.g., api.swarmlet.com)
    const apiBase = (window.API_BASE_URL && typeof window.API_BASE_URL === 'string')
        ? window.API_BASE_URL.replace(/\/api\/?$/, '')  // Remove /api suffix if present
        : '';
    const CONFIG = {
        batchEndpoint: apiBase + '/api/funnel/batch',
        batchSize: 10,
        batchInterval: 5000, // 5 seconds
        debug: false, // Set to true for console logging
    };

    // State
    let visitorId = null;
    let eventQueue = [];
    let humanDetected = false;
    let scrollMilestones = { 25: false, 50: false, 75: false, 100: false };
    let pageStartTime = Date.now();

    // ==================== VISITOR ID ====================

    function getCookie(name) {
        const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
        return match ? match[2] : null;
    }

    function setCookie(name, value, days) {
        const expires = new Date();
        expires.setTime(expires.getTime() + days * 24 * 60 * 60 * 1000);
        document.cookie = name + '=' + value + ';expires=' + expires.toUTCString() + ';path=/;SameSite=Lax';
    }

    function getVisitorId() {
        if (visitorId) return visitorId;

        // Check localStorage first
        try {
            visitorId = localStorage.getItem('swarmlet_visitor_id');
            if (visitorId) {
                // Sync to cookie for server-side access
                setCookie('swarmlet_visitor_id', visitorId, 365);
                return visitorId;
            }
        } catch (e) { /* ignore */ }

        // Check cookie as fallback
        visitorId = getCookie('swarmlet_visitor_id');
        if (visitorId) {
            // Sync to localStorage
            try {
                localStorage.setItem('swarmlet_visitor_id', visitorId);
            } catch (e) { /* ignore */ }
            return visitorId;
        }

        // Generate new ID (client-generated, prefixed with 'v_')
        visitorId = 'v_' + crypto.randomUUID();
        try {
            localStorage.setItem('swarmlet_visitor_id', visitorId);
        } catch (e) { /* ignore */ }
        setCookie('swarmlet_visitor_id', visitorId, 365);
        return visitorId;
    }

    // ==================== ATTRIBUTION TRACKING ====================

    const ATTRIBUTION_KEY = 'swarmlet_attribution';

    function captureAttribution() {
        const params = new URLSearchParams(window.location.search);

        const currentTouch = {
            utm_source: params.get('utm_source'),
            utm_medium: params.get('utm_medium'),
            utm_campaign: params.get('utm_campaign'),
            utm_term: params.get('utm_term'),
            utm_content: params.get('utm_content'),
            gclid: params.get('gclid'),
            referrer: document.referrer || null,
            landing_page: window.location.pathname,
            ts: Date.now(),
        };

        // Check if this touch has any attribution data
        const hasAttribution = currentTouch.utm_source || currentTouch.gclid;

        let stored = {};
        try {
            stored = JSON.parse(localStorage.getItem(ATTRIBUTION_KEY) || '{}');
        } catch (e) {
            stored = {};
        }

        // First-touch: only set once, never overwrite
        if (!stored.first_touch && hasAttribution) {
            stored.first_touch = { ...currentTouch };
        }

        // Last-touch: always update if we have new attribution data
        if (hasAttribution) {
            stored.last_touch = { ...currentTouch };
        }

        // Always store (even if empty, so we know we checked)
        try {
            localStorage.setItem(ATTRIBUTION_KEY, JSON.stringify(stored));
        } catch (e) {
            // localStorage blocked
        }

        return stored;
    }

    function getAttribution() {
        try {
            return JSON.parse(localStorage.getItem(ATTRIBUTION_KEY) || '{}');
        } catch (e) {
            return {};
        }
    }

    // ==================== EVENT TRACKING ====================

    function track(eventType, metadata = {}) {
        const attribution = getAttribution();

        const event = {
            event: eventType,
            page: window.location.pathname,
            metadata: {
                ...metadata,
                referrer: document.referrer || null,
                viewport: `${window.innerWidth}x${window.innerHeight}`,
                timestamp: Date.now(),
                // Add attribution data
                first_touch: attribution.first_touch || null,
                last_touch: attribution.last_touch || null,
            }
        };

        if (CONFIG.debug) {
            console.log('[Funnel]', eventType, event.metadata);
        }

        eventQueue.push(event);

        // Send immediately for important events
        const immediateEvents = ['signup_submitted', 'signup_completed', 'cta_clicked'];
        if (immediateEvents.includes(eventType)) {
            flushEvents();
        }
    }

    function flushEvents() {
        if (eventQueue.length === 0) return;

        const events = eventQueue.splice(0, CONFIG.batchSize);
        const payload = {
            visitor_id: getVisitorId(),
            events: events
        };

        // Use sendBeacon for reliability (works even on page unload)
        // Wrap in Blob to ensure proper Content-Type: application/json
        if (navigator.sendBeacon) {
            const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
            navigator.sendBeacon(CONFIG.batchEndpoint, blob);
        } else {
            fetch(CONFIG.batchEndpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
                keepalive: true
            }).catch(() => {});
        }
    }

    // Batch flush interval
    setInterval(flushEvents, CONFIG.batchInterval);

    // Flush on page unload
    window.addEventListener('beforeunload', flushEvents);
    window.addEventListener('pagehide', flushEvents);

    // ==================== AUTOMATIC TRACKING ====================

    // 1. Page view (immediate)
    function trackPageView() {
        track('page_view', {
            title: document.title,
            url: window.location.href,
        });
    }

    // 2. JS loaded (proves JavaScript executed)
    function trackJsLoaded() {
        track('js_loaded', {
            loadTime: performance.now(),
        });
    }

    // 3. Human detection (not a bot)
    function detectHuman(eventType) {
        if (humanDetected) return;
        humanDetected = true;
        track('human_detected', { trigger: eventType });
    }

    // Mouse movement (with throttling)
    let lastMouseMove = 0;
    document.addEventListener('mousemove', (e) => {
        const now = Date.now();
        if (now - lastMouseMove < 1000) return; // Throttle to once per second
        lastMouseMove = now;
        detectHuman('mousemove');
    }, { passive: true });

    // Scroll
    let hasScrolled = false;
    document.addEventListener('scroll', () => {
        if (!hasScrolled) {
            hasScrolled = true;
            detectHuman('scroll');
        }
        trackScrollDepth();
    }, { passive: true });

    // Keyboard
    document.addEventListener('keydown', () => {
        detectHuman('keydown');
    }, { passive: true, once: true });

    // Touch (mobile)
    document.addEventListener('touchstart', () => {
        detectHuman('touch');
    }, { passive: true, once: true });

    // 4. Scroll depth tracking
    function trackScrollDepth() {
        const scrollTop = window.scrollY;
        const docHeight = document.documentElement.scrollHeight - window.innerHeight;
        const scrollPercent = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;

        if (scrollPercent >= 25 && !scrollMilestones[25]) {
            scrollMilestones[25] = true;
            track('scroll_25');
        }
        if (scrollPercent >= 50 && !scrollMilestones[50]) {
            scrollMilestones[50] = true;
            track('scroll_50');
        }
        if (scrollPercent >= 75 && !scrollMilestones[75]) {
            scrollMilestones[75] = true;
            track('scroll_75');
        }
        if (scrollPercent >= 95 && !scrollMilestones[100]) {
            scrollMilestones[100] = true;
            track('scroll_100');
        }
    }

    // ==================== PRICING PAGE TRACKING ====================

    function initPricingTracking() {
        if (window.location.pathname === '/pricing') {
            track('pricing_viewed');
        }
    }

    // ==================== INITIALIZATION ====================

    function init() {
        captureAttribution();  // Must be before getVisitorId to capture UTM params
        getVisitorId();
        trackPageView();
        trackJsLoaded();

        // Wait for DOM
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initTrackers);
        } else {
            initTrackers();
        }
    }

    function initTrackers() {
        initPricingTracking();
    }

    // Expose for manual tracking
    window.SwarmletFunnel = {
        track: track,
        getVisitorId: getVisitorId,
        flush: flushEvents,
    };

    // Start tracking
    init();

})();
