/* ==========================================================
   FillPac AI
   Dashboard Frontend
   ========================================================== */

"use strict";


/* ==========================================================
   CONFIGURATION
   ========================================================== */

const API_BASE = window.location.origin;

const REFRESH_INTERVAL_MS = 5000;

const CAMERA_COUNT = 4;

/*
Number of selectable packers shown in the "Packer Running
Status" selector and the notification packer filter. Only
Packer 1 is currently installed/configured, so this is set to
1 - Packers 2-5 have been removed from the UI. If more packers
are commissioned later, raise this back up and re-add their
buttons to the #packerSelectorButtons and
#notificationPackerFilter markup in index.html.
*/
const PACKER_COUNT = 1;


/* ==========================================================
   APPLICATION STATE
   ========================================================== */

const appState = {

    currentPage: "dashboard",

    dashboardState: null,

    cameras: {},

    socket: null,

    socketConnected: false,

    lastStateUpdate: null,

    refreshTimer: null,

    events: [],

    analytics: null,

    production: null,

    cameraConfig: null,

    notifications: [],

    /*
    Session history of generated report PDFs, newest first, so
    the "Recent Reports" list on the Reports page can offer a
    re-download without regenerating from scratch. Not persisted
    across page reloads - just like the rest of appState.
    */
    reports: [],

    /*
    Restructured to track each packer (1..PACKER_COUNT)
    independently under `packers`, rather than a single global
    plc/pms/dcs/packer state. This is what lets notifications
    say "Packer 2 PLC Offline" instead of always assuming there
    is only one packer.
    */
    lastNotificationStates: {
        cameras: {},
        packers: {}
    },

    currentUser: null,

    userRole: "operator",

    cameraViewIndex: 1,

    // Which packer the System Status page and notification
    // panel are currently focused on (1..PACKER_COUNT).
    selectedPacker: 1,

    // "all" or a packer index as a string ("1".."5"). Filters
    // which notifications are shown in the notification panel.
    notificationPackerFilter: "all",

    productionTrendRangeHours: 8,

    /*
    0 = "LIVE" (no filter). Tracked here (not just as a CSS
    "active" class on the button) so the periodic refresh loop
    can re-apply the selected KPI range after every data refresh
    - otherwise the live /state poll every 5s was silently
    overwriting the filtered numbers back to live totals, which
    is why the range buttons looked like they "weren't working".
    */
    kpiRangeHours: 0
};


/* ==========================================================
   PAGE INFORMATION
   ========================================================== */

const PAGE_INFO = {

    dashboard: {
        title: "Dashboard",
        subtitle: "Real-time FillPac AI production overview"
    },

    "system-status": {
        title: "System Status",
        subtitle: "Real-time system health and component status"
    },

    reports: {
        title: "Reports",
        subtitle: "Production and performance reports"
    },

    about: {
        title: "About",
        subtitle: "FillPac Vision Intelligence Platform information"
    },

    settings: {
        title: "Settings",
        subtitle: "FillPac AI dashboard settings",
        adminOnly: true
    },

    "create-user": {
        title: "Create User",
        subtitle: "Create dashboard login accounts",
        adminOnly: true
    },

    // Legacy pages (kept for backward compatibility, but hidden from UI)
    "live-monitor": {
        title: "Live Monitor",
        subtitle: "Real-time camera streams and AI inspection"
    },

    production: {
        title: "Production",
        subtitle: "Production counts and print inspection performance"
    },

    analytics: {
        title: "Analytics",
        subtitle: "Production analytics and camera performance"
    },

    events: {
        title: "Events",
        subtitle: "Search and export persisted production events"
    },

    "jam-monitor": {
        title: "Jam Monitoring",
        subtitle: "Real-time conveyor motion and bag jam detection"
    },

    cameras: {
        title: "Cameras",
        subtitle: "Camera configuration and runtime status"
    }
};


/* ==========================================================
   REPORT TYPE LABELS

   Human-readable titles for each <option value="..."> in the
   #reportType select on the Reports page. Used both for the
   generated PDF's heading and the "Recent Reports" list.
   ========================================================== */

const REPORT_TYPE_LABELS = {
    production: "Production Report",
    print_detection: "Print Detection Report",
    camera_performance: "Camera Performance Report",
    system_status: "System Status Report",
    exception: "Exception / Alert Report"
};


/* ==========================================================
   JAM STATES
   ========================================================== */

const JAM_STATES = [
    "normal",
    "slow",
    "warning",
    "jam",
    "recovering",
    "disabled",
    "unknown"
];


/* ==========================================================
   DOM HELPERS
   ========================================================== */

function byId(id) {

    return document.getElementById(id);
}


function setText(id, value) {

    const element = byId(id);

    if (!element) {
        return;
    }

    element.textContent =
        value ?? "--";
}


function safeNumber(
    value,
    fallback = 0
) {

    const number = Number(value);

    return Number.isFinite(number)
        ? number
        : fallback;
}


function formatInteger(value) {

    return Math.round(
        safeNumber(value, 0)
    ).toLocaleString();
}


function formatDecimal(
    value,
    digits = 1
) {

    return safeNumber(
        value,
        0
    ).toFixed(digits);
}


function formatPercent(value) {

    return `${formatDecimal(value, 1)}%`;
}


function formatRoiBagDetail(bags) {

    const list =
        Array.isArray(bags)
            ? bags
            : [];

    if (list.length === 0) {
        return "--";
    }

    return list
        .map(
            bag => {

                const trackId =
                    bag?.track_id
                    ??
                    "?";

                const center =
                    Array.isArray(bag?.center)
                        ? bag.center
                        : null;

                if (!center) {
                    return `${trackId}`;
                }

                const x =
                    Math.round(
                        safeNumber(center[0], 0)
                    );

                const y =
                    Math.round(
                        safeNumber(center[1], 0)
                    );

                return `${trackId} @ (${x}, ${y})`;
            }
        )
        .join(", ");
}


function formatUptime(totalSeconds) {

    const seconds =
        Math.max(
            0,
            Math.floor(
                safeNumber(totalSeconds, 0)
            )
        );

    const hours =
        Math.floor(seconds / 3600);

    const minutes =
        Math.floor((seconds % 3600) / 60);

    const secs =
        seconds % 60;

    const pad =
        value => String(value).padStart(2, "0");

    return `${pad(hours)}:${pad(minutes)}:${pad(secs)}`;
}


function escapeHtml(value) {

    return String(
        value ?? ""
    )
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


/* ==========================================================
   DATE / TIME HELPERS
   ========================================================== */

function parseDate(value) {

    if (!value) {
        return null;
    }

    const date = new Date(value);

    if (
        Number.isNaN(
            date.getTime()
        )
    ) {
        return null;
    }

    return date;
}


function formatDateTime(value) {

    const date = parseDate(value);

    if (!date) {
        return "--";
    }

    return date.toLocaleString();
}


function formatTime(value) {

    const date = parseDate(value);

    if (!date) {
        return "--";
    }

    return date.toLocaleTimeString();
}


/* ==========================================================
   CHART REGISTRY

   Chart.js is loaded globally (see index.html). Every chart on
   the dashboard is created once, lazily, the first time it has
   data to show, then updated in place on every subsequent
   refresh instead of being destroyed/recreated.
   ========================================================== */

const chartInstances = {};

const CHART_COLORS = {
    blue: "#1976d2",
    blueSoft: "rgba(25, 118, 210, 0.15)",
    success: "#16a34a",
    successSoft: "rgba(22, 163, 74, 0.15)",
    danger: "#dc2626",
    dangerSoft: "rgba(220, 38, 38, 0.15)",
    warning: "#f59e0b",
    grid: "rgba(0, 63, 70, 0.08)",
    text: "#005864"
};


function getOrCreateChart(
    canvasId,
    buildConfig
) {

    const canvas =
        byId(canvasId);

    if (!canvas) {
        return null;
    }

    if (
        typeof Chart === "undefined"
    ) {
        return null;
    }

    if (chartInstances[canvasId]) {
        return chartInstances[canvasId];
    }

    const config =
        buildConfig();

    chartInstances[canvasId] =
        new Chart(
            canvas,
            config
        );

    return chartInstances[canvasId];
}


function baseChartOptions(extra = {}) {

    return {

        responsive: true,

        maintainAspectRatio: false,

        plugins: {

            legend: {
                labels: {
                    color: CHART_COLORS.text
                }
            }
        },

        scales: {

            x: {
                ticks: { color: CHART_COLORS.text },
                grid: { color: CHART_COLORS.grid }
            },

            y: {
                beginAtZero: true,
                ticks: { color: CHART_COLORS.text },
                grid: { color: CHART_COLORS.grid }
            }
        },

        ...extra
    };
}


/* ----------------------------------------------------------
   DASHBOARD: PRODUCTION TREND (line chart, hourly totals)
   ---------------------------------------------------------- */

function updateProductionTrendChart(hourly) {

    const fullRows =
        Array.isArray(hourly)
            ? hourly
            : [];

    /*
    The backend's /analytics endpoint returns hourly buckets for
    its own default window. Rather than requiring a new backend
    query parameter (which would need backend changes outside
    this frontend), the 1/4/8/16/24 HR filter slices the most
    recent N hourly buckets already returned by the existing
    pipeline. This keeps full backward compatibility with the
    existing /analytics response shape.
    */

    const rangeHours =
        Math.max(
            1,
            safeNumber(
                appState.productionTrendRangeHours,
                8
            )
        );

    const rows =
        fullRows.slice(
            -rangeHours
        );

    const labels =
        rows.map(row => row.hour);

    const totals =
        rows.map(
            row => safeNumber(row.total, 0)
        );

    const printed =
        rows.map(
            row => safeNumber(row.printed, 0)
        );

    /*
    Changed from a single-series line chart to a grouped column
    (bar) chart with two series - "Count" (total bags) and
    "Print" (printed bags) - shown side by side for each hour so
    both can be compared together at a glance.
    */

    const chart =
        getOrCreateChart(
            "productionChart",
            () => ({
                type: "bar",
                data: {
                    labels,
                    datasets: [
                        {
                            label: "Count",
                            data: totals,
                            backgroundColor: CHART_COLORS.blue,
                            borderRadius: 4,
                            maxBarThickness: 28
                        },
                        {
                            label: "Print",
                            data: printed,
                            backgroundColor: CHART_COLORS.success,
                            borderRadius: 4,
                            maxBarThickness: 28
                        }
                    ]
                },
                options: baseChartOptions({
                    scales: {
                        x: {
                            ticks: { color: CHART_COLORS.text },
                            grid: { display: false }
                        },
                        y: {
                            beginAtZero: true,
                            ticks: { color: CHART_COLORS.text },
                            grid: { color: CHART_COLORS.grid }
                        }
                    }
                })
            })
        );

    if (!chart) {
        return;
    }

    /*
    If an existing chart instance was created before this change
    (e.g. hot-reload during development) it may still only have
    one dataset - guard against that instead of assuming index 1
    always exists.
    */

    chart.config.type = "bar";
    chart.data.labels = labels;
    chart.data.datasets[0].label = "Count";
    chart.data.datasets[0].data = totals;
    chart.data.datasets[0].backgroundColor = CHART_COLORS.blue;
    chart.data.datasets[0].borderColor = undefined;
    chart.data.datasets[0].fill = undefined;
    chart.data.datasets[0].tension = undefined;
    chart.data.datasets[0].pointRadius = undefined;

    if (chart.data.datasets[1]) {
        chart.data.datasets[1].label = "Print";
        chart.data.datasets[1].data = printed;
        chart.data.datasets[1].backgroundColor = CHART_COLORS.success;
    } else {
        chart.data.datasets.push({
            label: "Print",
            data: printed,
            backgroundColor: CHART_COLORS.success,
            borderRadius: 4,
            maxBarThickness: 28
        });
    }

    chart.update();
}


/* ----------------------------------------------------------
   DASHBOARD: PRINT INSPECTION (donut, printed vs missing)
   ---------------------------------------------------------- */

function updatePrintInspectionChart(printed, missing) {

    const chart =
        getOrCreateChart(
            "printChart",
            () => ({
                type: "doughnut",
                data: {
                    labels: ["Printed", "Not Printed"],
                    datasets: [
                        {
                            data: [printed, missing],
                            backgroundColor: [
                                CHART_COLORS.success,
                                CHART_COLORS.danger
                            ],
                            borderWidth: 0
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: "65%",
                    plugins: {
                        legend: {
                            position: "bottom",
                            labels: { color: CHART_COLORS.text }
                        }
                    }
                }
            })
        );

    if (!chart) {
        return;
    }

    chart.data.datasets[0].data = [printed, missing];
    chart.update();
}


/* ----------------------------------------------------------
   ANALYTICS: HOURLY PRODUCTION (bar)
   ---------------------------------------------------------- */

function updateAnalyticsHourlyChart(hourly) {

    const rows =
        Array.isArray(hourly)
            ? hourly
            : [];

    const labels =
        rows.map(row => row.hour);

    const totals =
        rows.map(
            row => safeNumber(row.total, 0)
        );

    const printed =
        rows.map(
            row => safeNumber(row.printed, 0)
        );

    const missing =
        rows.map(
            row => safeNumber(row.missing, 0)
        );

    const chart =
        getOrCreateChart(
            "analyticsHourlyChart",
            () => ({
                type: "bar",
                data: {
                    labels,
                    datasets: [
                        {
                            label: "Printed",
                            data: printed,
                            backgroundColor: CHART_COLORS.success,
                            stack: "stack0"
                        },
                        {
                            label: "Missing",
                            data: missing,
                            backgroundColor: CHART_COLORS.danger,
                            stack: "stack0"
                        }
                    ]
                },
                options: baseChartOptions({
                    scales: {
                        x: {
                            stacked: true,
                            ticks: { color: CHART_COLORS.text },
                            grid: { display: false }
                        },
                        y: {
                            stacked: true,
                            beginAtZero: true,
                            ticks: { color: CHART_COLORS.text },
                            grid: { color: CHART_COLORS.grid }
                        }
                    }
                })
            })
        );

    if (!chart) {
        return;
    }

    chart.data.labels = labels;
    chart.data.datasets[0].data = printed;
    chart.data.datasets[1].data = missing;
    chart.update();

    void totals;
}


/* ----------------------------------------------------------
   ANALYTICS: CAMERA COMPARISON (bar)
   ---------------------------------------------------------- */

function updateAnalyticsCameraChart(byCamera) {

    const rows =
        Array.isArray(byCamera)
            ? byCamera
            : [];

    const labels =
        rows.map(row => row.camera);

    const totals =
        rows.map(
            row => safeNumber(row.total, 0)
        );

    const chart =
        getOrCreateChart(
            "analyticsCameraChart",
            () => ({
                type: "bar",
                data: {
                    labels,
                    datasets: [
                        {
                            label: "Events",
                            data: totals,
                            backgroundColor: CHART_COLORS.blue
                        }
                    ]
                },
                options: baseChartOptions({
                    plugins: {
                        legend: { display: false }
                    }
                })
            })
        );

    if (!chart) {
        return;
    }

    chart.data.labels = labels;
    chart.data.datasets[0].data = totals;
    chart.update();
}


/* ----------------------------------------------------------
   ANALYTICS: PRINT INSPECTION ANALYTICS (donut, overall)
   ---------------------------------------------------------- */

function updateAnalyticsPrintChart(printed, missing) {

    const chart =
        getOrCreateChart(
            "analyticsPrintChart",
            () => ({
                type: "doughnut",
                data: {
                    labels: ["Printed", "Missing"],
                    datasets: [
                        {
                            data: [printed, missing],
                            backgroundColor: [
                                CHART_COLORS.success,
                                CHART_COLORS.danger
                            ],
                            borderWidth: 0
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: "65%",
                    plugins: {
                        legend: {
                            position: "bottom",
                            labels: { color: CHART_COLORS.text }
                        }
                    }
                }
            })
        );

    if (!chart) {
        return;
    }

    chart.data.datasets[0].data = [printed, missing];
    chart.update();
}


/* ----------------------------------------------------------
   ANALYTICS: SHIFT PRODUCTION (bar)
   ---------------------------------------------------------- */

function updateAnalyticsShiftChart(byShift) {

    const rows =
        Array.isArray(byShift)
            ? byShift
            : [];

    const labelMap = {
        "shift-a": "Shift A",
        "shift-b": "Shift B",
        "shift-c": "Shift C",
        "unknown": "Unknown"
    };

    const labels =
        rows.map(
            row => labelMap[row.shift] || row.shift
        );

    const printed =
        rows.map(
            row => safeNumber(row.printed, 0)
        );

    const missing =
        rows.map(
            row => safeNumber(row.missing, 0)
        );

    const chart =
        getOrCreateChart(
            "analyticsShiftChart",
            () => ({
                type: "bar",
                data: {
                    labels,
                    datasets: [
                        {
                            label: "Printed",
                            data: printed,
                            backgroundColor: CHART_COLORS.success,
                            stack: "stack0"
                        },
                        {
                            label: "Missing",
                            data: missing,
                            backgroundColor: CHART_COLORS.danger,
                            stack: "stack0"
                        }
                    ]
                },
                options: baseChartOptions({
                    scales: {
                        x: {
                            stacked: true,
                            ticks: { color: CHART_COLORS.text },
                            grid: { display: false }
                        },
                        y: {
                            stacked: true,
                            beginAtZero: true,
                            ticks: { color: CHART_COLORS.text },
                            grid: { color: CHART_COLORS.grid }
                        }
                    }
                })
            })
        );

    if (!chart) {
        return;
    }

    chart.data.labels = labels;
    chart.data.datasets[0].data = printed;
    chart.data.datasets[1].data = missing;
    chart.update();
}


/* ==========================================================
   API
   ========================================================== */

// Set the moment we get a 401 and decide to redirect. Every
// in-flight interval/handler (state refresh, production refresh,
// visibility-change refresh, etc.) checks this before hitting the
// network again, so a rejected session logs ONE 401 and then waits
// for navigation instead of hammering the API (and the console)
// every REFRESH_INTERVAL_MS until the browser finally unloads the
// page -- which is what was happening before.
let authRedirectInProgress = false;

async function apiFetch(
    path,
    options = {}
) {

    if (authRedirectInProgress) {
        throw new Error(`Skipped ${path}: session invalid, redirecting to login`);
    }

    const token =
        localStorage.getItem("fillpac_auth_token") ||
        sessionStorage.getItem("fillpac_auth_token");

    const headers = {
        ...(options.headers || {}),
        ...(token ? { "Authorization": `Bearer ${token}` } : {}),
    };

    const response =
        await fetch(
            `${API_BASE}${path}`,
            {
                cache: "no-store",
                ...options,
                headers,
            }
        );

    if (response.status === 401) {

        // Session expired/invalid -- clear it and send the
        // user back to the login page instead of surfacing a
        // confusing 401 error in the dashboard UI.
        authRedirectInProgress = true;

        localStorage.removeItem("fillpac_auth_token");
        localStorage.removeItem("fillpac_user");
        sessionStorage.removeItem("fillpac_auth_token");
        sessionStorage.removeItem("fillpac_user");

        console.warn(
            `Session rejected by server on ${path} -- ` +
            "redirecting to login instead of retrying."
        );

        window.location.href =
            "/login?redirect=" + encodeURIComponent(window.location.href);

        throw new Error(`HTTP 401: ${path}`);
    }

    if (!response.ok) {

        throw new Error(
            `HTTP ${response.status}: ${path}`
        );
    }

    const contentType =
        response.headers.get(
            "content-type"
        ) || "";

    if (
        contentType.includes(
            "application/json"
        )
    ) {

        return await response.json();
    }

    return await response.text();
}


/* ==========================================================
   NAVIGATION
   ========================================================== */

function initializeNavigation() {

    const items =
        document.querySelectorAll(
            ".sidebar-item[data-page]"
        );

    items.forEach(
        item => {

            item.addEventListener(
                "click",
                async () => {

                    const page =
                        item.dataset.page;

                    if (!page) {
                        return;
                    }

                    await showPage(page);
                }
            );
        }
    );

    // ------------------------------------------------------
    // Overview jam banner "View Jam Monitor" link
    // ------------------------------------------------------

    document
        .querySelectorAll(
            "[data-page-link]"
        )
        .forEach(
            link => {

                link.addEventListener(
                    "click",
                    async () => {

                        const page =
                            link.dataset.pageLink;

                        if (!page) {
                            return;
                        }

                        await showPage(page);
                    }
                );
            }
        );
}


function initializeNotificationSystem() {

    // Load user role to determine feature visibility
    loadUserRole();
    bindCreateUserForm();

    const notificationBell = byId("notificationBell");
    const notificationPanel = byId("notificationPanel");
    const closeNotificationPanel = byId("closeNotificationPanel");
    const markAllAsReadCheckbox = byId("markAllAsReadCheckbox");

    if (notificationBell && notificationPanel) {
        notificationBell.addEventListener("click", (e) => {
            e.stopPropagation();
            const isVisible = notificationPanel.style.display !== "none";
            /*
            Must match the CSS's "display: flex" for .notification-panel
            (a header/status/filter/list/footer column). Setting this to
            "block" instead broke that flex layout every time the panel
            opened, which is why the list's flex:1 + min-height:0 sizing
            (and therefore its scrollbar) never actually took effect.
            */
            notificationPanel.style.display = isVisible ? "none" : "flex";

            // Opening the panel can change the list's available
            // height (e.g. if the page scrolled since it was last
            // open), so re-check whether the "more below" hint
            // should show.
            if (!isVisible) {
                refreshNotificationScrollHint();
            }
        });
    }

    // Hide the "more below" hint once the person has scrolled far
    // enough to see the bottom of the list; show it again if they
    // scroll back up and there's still more content past the
    // visible area.
    const notificationList = byId("notificationList");

    if (notificationList) {

        notificationList.addEventListener(
            "scroll",
            refreshNotificationScrollHint
        );
    }

    if (closeNotificationPanel) {
        closeNotificationPanel.addEventListener("click", () => {
            if (notificationPanel) {
                notificationPanel.style.display = "none";
            }
        });
    }

    /*
    The "Mark all as read" control is a toggle switch rather than
    a one-shot button: switching it ON marks every notification as
    read, switching it back OFF marks them all unread again. Its
    checked state is also kept in sync (see updateNotificationUI)
    with the actual data, so if a new unread notification arrives
    the switch automatically flips back off.
    */

    if (markAllAsReadCheckbox) {

        markAllAsReadCheckbox.addEventListener("change", () => {

            if (markAllAsReadCheckbox.checked) {
                markAllNotificationsAsRead();
            } else {
                markAllNotificationsAsUnread();
            }
        });

        markAllAsReadCheckbox.addEventListener(
            "click",
            (e) => e.stopPropagation()
        );
    }

    // Close notification panel when clicking outside
    document.addEventListener("click", (e) => {
        if (notificationPanel && notificationPanel.style.display !== "none") {
            if (!notificationPanel.contains(e.target) && notificationBell && !notificationBell.contains(e.target)) {
                notificationPanel.style.display = "none";
            }
        }
    });

    initializeNotificationPackerFilter();
}


/* ==========================================================
   NOTIFICATION PANEL — PACKER FILTER (ALL / P1-P5)
   ========================================================== */

function initializeNotificationPackerFilter() {

    const container =
        byId("notificationPackerFilter");

    if (!container) {
        return;
    }

    const buttons =
        container.querySelectorAll(
            ".time-range-button"
        );

    buttons.forEach(
        button => {

            button.addEventListener(
                "click",
                event => {

                    // Prevent this click from bubbling up to the
                    // document-level "click outside closes panel"
                    // listener registered above.
                    event.stopPropagation();

                    appState.notificationPackerFilter =
                        button.dataset.packer;

                    buttons.forEach(
                        other => {

                            other.classList.toggle(
                                "active",
                                other === button
                            );
                        }
                    );

                    updateNotificationUI();
                }
            );
        }
    );
}


/* ==========================================================
   SYSTEM STATUS PAGE — PACKER SELECTOR (PACKER 1-5)
   ========================================================== */

function initializePackerSelector() {

    const container =
        byId("packerSelectorButtons");

    if (!container) {
        return;
    }

    const buttons =
        container.querySelectorAll(
            ".time-range-button"
        );

    buttons.forEach(
        button => {

            button.addEventListener(
                "click",
                () => {

                    const index =
                        safeNumber(
                            button.dataset.packer,
                            1
                        );

                    appState.selectedPacker =
                        index;

                    buttons.forEach(
                        other => {

                            other.classList.toggle(
                                "active",
                                other === button
                            );
                        }
                    );

                    renderSystemStatus(
                        appState.dashboardState || {}
                    );
                }
            );
        }
    );
}


async function showPage(page) {

    // ------------------------------------------------------
    // Validate page
    // ------------------------------------------------------

    if (!PAGE_INFO[page]) {

        console.warn(
            "Unknown dashboard page:",
            page
        );

        return;
    }


    // ------------------------------------------------------
    // Check access permissions
    // ------------------------------------------------------

    if (!canAccessPage(page)) {

        console.warn(
            `Access denied to page: ${page}`
        );

        addNotification(
            "Access Denied",
            `You do not have permission to access this page.`,
            "danger"
        );

        return;
    }


    // ------------------------------------------------------
    // Find requested page
    // ------------------------------------------------------

    const target =
        byId(`page-${page}`);


    if (!target) {

        console.error(
            `Dashboard page not found: page-${page}`
        );

        return;
    }


    // ------------------------------------------------------
    // Hide all pages
    // ------------------------------------------------------

    document
        .querySelectorAll(
            ".dashboard-page"
        )
        .forEach(
            element => {

                element.classList.remove(
                    "active-page"
                );
            }
        );


    // ------------------------------------------------------
    // Show selected page
    // ------------------------------------------------------

    target.classList.add(
        "active-page"
    );


    // ------------------------------------------------------
    // Update sidebar active button
    // ------------------------------------------------------

    document
        .querySelectorAll(
            ".sidebar-item[data-page]"
        )
        .forEach(
            item => {

                item.classList.toggle(
                    "active",
                    item.dataset.page === page
                );
            }
        );


    // ------------------------------------------------------
    // Store selected page
    // ------------------------------------------------------

    appState.currentPage =
        page;


    // ------------------------------------------------------
    // Update page heading
    // ------------------------------------------------------

    updatePageHeader(
        page
    );


    // ------------------------------------------------------
    // Load page data
    // ------------------------------------------------------

    try {

        await loadCurrentPage();

    }

    catch (error) {

        console.error(
            `Failed loading page "${page}":`,
            error
        );
    }
}


function updatePageHeader(page) {

    const info =
        PAGE_INFO[page]
        ||
        PAGE_INFO.dashboard;


    setText(
        "pageTitle",
        info.title
    );


    setText(
        "pageSubtitle",
        info.subtitle
    );

    /*
    The global "Dashboard / Real-time FillPac AI production
    overview" title+subtitle is removed only on the Dashboard
    page itself (per request); it still shows normally on every
    other page (System Status, Reports, About, Settings, etc.).
    */

    const headerText =
        byId("pageHeaderText");

    if (headerText) {

        headerText.classList.toggle(
            "page-header-hidden",
            page === "dashboard"
        );
    }
}


/* ==========================================================
   NOTIFICATION SYSTEM
   ========================================================== */

function addNotification(
    title,
    message,
    severity = "info",
    source = "system"
) {

    const notification = {
        id: `notif-${Date.now()}-${Math.random()}`,
        title,
        message,
        severity,
        source,
        timestamp: new Date(),
        read: false
    };

    appState.notifications.unshift(notification);

    // Keep only last 50 notifications
    if (appState.notifications.length > 50) {
        appState.notifications.pop();
    }

    updateNotificationUI();

    return notification;
}


function updateNotificationUI() {

    const badge = byId("notificationBadge");
    const list = byId("notificationList");
    const markAllAsReadCheckbox = byId("markAllAsReadCheckbox");

    if (!badge || !list) {
        return;
    }

    const unreadCount = appState.notifications.filter(n => !n.read).length;

    if (unreadCount > 0) {
        badge.textContent = unreadCount;
        badge.style.display = "flex";
    } else {
        badge.style.display = "none";
    }

    /*
    Keep the toggle switch reflecting reality: checked only when
    there's at least one notification and none of them are
    unread. If a fresh notification comes in, the switch flips
    back off on its own.
    */

    if (markAllAsReadCheckbox) {
        markAllAsReadCheckbox.checked =
            appState.notifications.length > 0
            && unreadCount === 0;
    }

    if (appState.notifications.length === 0) {
        list.innerHTML = "<p class=\"no-notifications\">No notifications</p>";
        refreshNotificationScrollHint();
        return;
    }

    /*
    Packer filter (ALL / P1 / P2 / P3 / P4 / P5): when a specific
    packer is selected, only show notifications whose source
    starts with "packer-{N}-" or "camera" notifications tied to
    that context are hidden, since they aren't specific to any
    one packer. "all" (the default) shows everything, unchanged
    from before.
    */

    const filter =
        appState.notificationPackerFilter
        || "all";

    const filteredNotifications =
        filter === "all"
            ? appState.notifications
            : appState.notifications.filter(
                notif =>
                    String(notif.source || "")
                        .startsWith(`packer-${filter}-`)
            );

    if (filteredNotifications.length === 0) {

        list.innerHTML =
            `<p class="no-notifications">No notifications for Packer ${filter}</p>`;

        refreshNotificationScrollHint();

        return;
    }

    list.innerHTML = filteredNotifications.map(notif => {
        const severityClass = `notif-${notif.severity}`;
        const readClass = notif.read ? "notif-read" : "notif-unread";
        const timeStr = formatTime(notif.timestamp);

        return `
            <div class="notification-item ${severityClass} ${readClass}" data-notif-id="${notif.id}">
                <div class="notif-dot"></div>
                <div class="notif-content">
                    <strong>${escapeHtml(notif.title)}</strong>
                    <p>${escapeHtml(notif.message)}</p>
                </div>
                <span class="notif-time">${timeStr}</span>
            </div>
        `;
    }).join("");

    refreshNotificationScrollHint();
}


/* ==========================================================
   NOTIFICATION LIST — "MORE BELOW" SCROLL HINT

   Makes it unmistakable that the notification list scrolls:
   shows a small bouncing-chevron hint pinned to the bottom of
   the list whenever there is more content below the currently
   visible area, and hides it once scrolled within a few pixels
   of the bottom. Re-run after every re-render (new/removed
   notifications, filter changes) since that can change whether
   there's overflow at all.
   ========================================================== */

function refreshNotificationScrollHint() {

    const list = byId("notificationList");
    const hint = byId("notificationScrollHint");

    if (!list || !hint) {
        return;
    }

    /*
    Wait a frame so the browser has finished laying out the
    freshly-injected innerHTML before we measure scrollHeight -
    reading it in the same tick as the innerHTML write can
    occasionally see stale (pre-update) dimensions.
    */

    requestAnimationFrame(() => {

        const hasOverflow =
            list.scrollHeight - list.clientHeight > 4;

        const nearBottom =
            list.scrollHeight - list.scrollTop - list.clientHeight < 4;

        hint.style.display =
            hasOverflow && !nearBottom
                ? "flex"
                : "none";
    });
}


function markAllNotificationsAsRead() {

    appState.notifications.forEach(n => {
        n.read = true;
    });

    updateNotificationUI();
}


function markAllNotificationsAsUnread() {

    appState.notifications.forEach(n => {
        n.read = false;
    });

    updateNotificationUI();
}


/* ==========================================================
   ROLE-BASED ACCESS CONTROL
   ========================================================== */

function loadUserRole() {

    const userStr =
        localStorage.getItem("fillpac_user") ||
        sessionStorage.getItem("fillpac_user");

    // Default to the least-privileged role if user info is missing.
    appState.currentUser = null;
    appState.userRole = "operator";

    if (userStr) {
        try {
            const user = JSON.parse(userStr);
            appState.currentUser = user;
            appState.userRole =
                String(user.role || "operator").toLowerCase();
        } catch (e) {
            console.warn("Failed to parse user info:", e);
        }
    }

    updateSettingsVisibility();
}


function updateSettingsVisibility() {

    // Settings and Create User are admin-only. This only hides
    // the UI -- the real enforcement is server-side (require_admin
    // on /api/settings* and POST /api/users in server.py).
    const isAdmin = appState.userRole === "admin";

    ["settingsNavButton", "createUserNavButton"].forEach(id => {
        const button = byId(id);
        if (button) {
            button.style.display = isAdmin ? "flex" : "none";
        }
    });

    // If a non-admin somehow ends up on an admin-only page,
    // send them back to the Dashboard.
    if (
        !isAdmin &&
        ["settings", "create-user"].includes(appState.currentPage)
    ) {
        showPage("dashboard");
    }
}


function canAccessPage(page) {

    const pageInfo = PAGE_INFO[page];

    if (!pageInfo) {
        return false;
    }

    if (pageInfo.adminOnly && appState.userRole !== "admin") {
        return false;
    }

    return true;
}


/* ==========================================================
   CURRENT PAGE LOADER
   ========================================================== */

async function loadCurrentPage() {

    try {

        switch (
            appState.currentPage
        ) {

            case "dashboard":

                await loadDashboardState();

                await loadAnalytics();

                /*
                Production KPI cards + Camera Production table
                now live on the Dashboard tab (moved from the
                former standalone "Production" tab), so load
                that data here too.
                */

                await loadProduction();

                reapplyActiveKpiRangeFilter();

                break;


            case "live-monitor":

                await loadDashboardState();

                break;


            case "production":

                await loadProduction();

                break;


            case "analytics":

                await loadAnalytics();

                break;


            case "events":

                await loadEvents();

                break;


            case "jam-monitor":

                /*
                Fetch fresh state before rendering the
                dedicated jam monitoring page.
                */

                await loadDashboardState();

                renderJamMonitoring(
                    appState
                        .dashboardState
                        ?.cameras
                    || {}
                );

                break;


            case "cameras":

                await loadDashboardState();

                await loadCameraConfig();

                break;


            case "system-status":

                await loadDashboardState();

                renderSystemStatus(appState.dashboardState);

                break;


            case "reports":

                renderReportsPage();

                break;


            case "about":

                renderAboutPage();

                break;


            case "create-user":

                resetCreateUserForm();

                break;


            case "settings":

                // loadDashboardState() re-throws on failure (see its
                // catch block), which used to abort this entire case
                // before loadSettings() got a chance to run -- a
                // transient /state error meant the Settings page
                // stayed stuck on "Loading configuration..." forever
                // with no error shown. Settings data doesn't depend
                // on dashboard state, so a /state failure here is
                // logged and swallowed instead of blocking the page.
                try {
                    await loadDashboardState();
                } catch (error) {
                    console.error(
                        "Settings page: /state failed, continuing to load settings anyway:",
                        error
                    );
                }

                await loadSettings();

                break;


            default:

                await loadDashboardState();

                break;
        }

    }

    catch (error) {

        console.error(
            "Page load failed:",
            error
        );
    }
}


/* ==========================================================
   DASHBOARD STATE
   ========================================================== */

async function loadDashboardState() {

    try {

        const state =
            await apiFetch(
                "/state"
            );


        renderDashboardState(
            state
        );


        return state;

    }

    catch (error) {

        console.error(
            "Failed loading dashboard state:",
            error
        );


        updateSystemStatus(
            "offline"
        );


        throw error;
    }
}


/* ==========================================================
   RENDER DASHBOARD STATE
   ========================================================== */

function renderDashboardState(state) {

    if (
        !state
        ||
        typeof state !== "object"
    ) {
        return;
    }


    appState.dashboardState =
        state;


    appState.cameras =
        state.cameras || {};


    appState.lastStateUpdate =
        new Date();


    /* ------------------------------------------------------
       SYSTEM
       ------------------------------------------------------ */

    updateSystemStatus(
        state.system_status
        || "offline"
    );


    setText(
        "systemUptime",
        formatUptime(
            state.uptime_seconds
        )
    );


    /* ------------------------------------------------------
       MAIN COUNTS
       ------------------------------------------------------ */

    setText(
        "totalBags",
        formatInteger(
            state.total_count
        )
    );


    setText(
        "printedBags",
        formatInteger(
            state.total_printed_count
            ??
            state.total_printed_bags_count
            ??
            0
        )
    );


    setText(
        "missingBags",
        formatInteger(
            state.total_missing_count
            ??
            state.total_not_printed_bags_count
            ??
            0
        )
    );


    const printedForQuality =
        safeNumber(
            state.total_printed_count
            ??
            state.total_printed_bags_count
            ??
            0
        );

    const missingForQuality =
        safeNumber(
            state.total_missing_count
            ??
            state.total_not_printed_bags_count
            ??
            0
        );

    updatePrintInspectionChart(
        printedForQuality,
        missingForQuality
    );


    /*
    Print Quality = Printed / (Printed + Missing) * 100.

    This is the same underlying camera counts already shown in
    the Printed / Not Printed KPI cards above, so the percentage
    can never disagree with those numbers (see data-consistency
    requirement). Division by zero (no classified bags yet) is
    shown as "--" rather than NaN/Infinity.
    */

    const classifiedTotal =
        printedForQuality + missingForQuality;

    setText(
        "printQuality",
        classifiedTotal > 0
            ? formatPercent(
                (printedForQuality / classifiedTotal) * 100
            )
            : "--"
    );


    /* ------------------------------------------------------
       CAMERA CARDS
       ------------------------------------------------------ */

    renderCameraCards(
        state.cameras || {}
    );


    /* ------------------------------------------------------
       JAM MONITORING
       ------------------------------------------------------ */

    renderJamMonitoring(
        state.cameras || {}
    );


    /* ------------------------------------------------------
       DASHBOARD HEALTH
       ------------------------------------------------------ */

    renderDashboardHealthSummary(
        state
    );


    /* ------------------------------------------------------
       SERVICE HEALTH
       ------------------------------------------------------ */

    renderServiceHealth(
        state.service_status || {}
    );


    /* ------------------------------------------------------
       SYSTEM CHANGE DETECTION
       Detects state transitions and generates notifications
       ------------------------------------------------------ */

    detectAndNotifySystemChanges(state);


    /* ------------------------------------------------------
       KPI RANGE FILTER

       Everything above just overwrote the KPI cards with LIVE
       totals from `state`. renderDashboardState() runs very
       frequently - on every Socket.IO "state" / "state_update" /
       "dashboard_state" push (much more often than the 5s REST
       poll) - so a selected 1/4/8/16/24 HR filter was being wiped
       out almost instantly after being clicked. Re-applying it
       here, at the end of the one function every live-update path
       funnels through, guarantees the filter sticks no matter
       which path triggered the update.
       ------------------------------------------------------ */

    reapplyActiveKpiRangeFilter();
}


/* ==========================================================
   SYSTEM STATUS
   ========================================================== */

function updateSystemStatus(status) {

    const normalized =
        String(
            status || "offline"
        )
            .trim()
            .toLowerCase();


    const statusElement =
        byId("systemStatus");


    if (statusElement) {

        statusElement.className =
            `system-status ${normalized}`;
    }


    const statusText =
        byId("systemStatusText");


    if (statusText) {

        statusText.textContent =
            normalized.toUpperCase();
    }


    const dot =
        byId("systemStatusDot");


    if (dot) {

        dot.className =
            `status-dot ${normalized}`;
    }
}


/* ==========================================================
   SERVICE HEALTH
   ========================================================== */

function renderServiceHealth(
    serviceStatus
) {

    const modelLoaded =
        Boolean(
            serviceStatus
                ?.model_loaded
        );


    const inferenceRunning =
        Boolean(
            serviceStatus
                ?.inference_manager_running
        );


    const elasticsearch =
        Boolean(
            serviceStatus
                ?.elasticsearch_connected
        );


    setText(
        "healthModel",
        modelLoaded
            ? "Loaded"
            : "Not Loaded"
    );


    setText(
        "healthInference",
        inferenceRunning
            ? "Running"
            : "Stopped"
    );


    setText(
        "healthElasticsearch",
        elasticsearch
            ? "Connected"
            : "Disconnected"
    );
}


/* ==========================================================
   STATUS VALUE NORMALIZATION

   Backends are inconsistent about how they report an "online" /
   "running" state - sometimes a boolean, sometimes a number,
   sometimes a string in any casing ("online", "ONLINE",
   "Online", "running", "true", "1", "connected", "ok"). A
   strict `=== "online"` check (the old behaviour) silently
   treated every one of those other formats as offline, which is
   what caused PLC/PMS/DCS to show "Offline" and fire spurious
   notifications even while the system was healthy. Every status
   comparison in this file should go through this function.
   ========================================================== */

function normalizeBooleanStatus(value) {

    if (typeof value === "boolean") {
        return value;
    }

    if (typeof value === "number") {
        return value !== 0;
    }

    const text =
        String(value ?? "")
            .trim()
            .toLowerCase();

    return [
        "online",
        "running",
        "active",
        "true",
        "1",
        "connected",
        "ok",
        "healthy"
    ].includes(text);
}


/* ==========================================================
   PACKER LOOKUP

   Mirrors findCameraByIndex(): looks for a `state.packers` map
   keyed by packer (packer_1, "Packer 1", etc.) and matches by
   index. If the backend hasn't been upgraded to send a
   `packers` map yet, Packer 1 is synthesized from the existing
   top-level packer_running/plc_status/pms_status/dcs_status
   fields so the dashboard keeps working unchanged for
   single-packer installs. Packers 2-5 return null (shown in the
   UI as "NOT CONFIGURED") until the backend actually reports
   them.
   ========================================================== */

function findPackerByIndex(state, index) {

    const packersMap =
        state?.packers
        ??
        state?.packer_list
        ??
        null;

    if (
        packersMap
        &&
        typeof packersMap === "object"
    ) {

        const entries =
            Object.entries(packersMap);

        const wanted =
            `packer${index}`
                .replaceAll(" ", "")
                .replaceAll("_", "")
                .toLowerCase();

        const direct =
            entries.find(
                ([key, packer]) => {

                    const values = [
                        key,
                        packer?.packer_id,
                        packer?.name,
                        packer?.packer_name
                    ];

                    return values.some(
                        value =>
                            String(value || "")
                                .replaceAll(" ", "")
                                .replaceAll("_", "")
                                .toLowerCase()
                            === wanted
                    );
                }
            );

        if (direct) {
            return direct[1];
        }

        return entries[index - 1]?.[1] || null;
    }

    // No multi-packer data from the backend yet - synthesize
    // Packer 1 from the existing single-packer top-level fields.
    if (index === 1) {

        return {
            packer_id: "packer_1",
            name: "Packer 1",
            running: state?.packer_running,
            plc_status: state?.plc_status ?? state?.plc_online,
            pms_status: state?.pms_status ?? state?.pms_online,
            dcs_status: state?.dcs_status ?? state?.dcs_online
        };
    }

    return null;
}


function packerIsRunning(packer) {

    if (!packer) {
        return false;
    }

    return normalizeBooleanStatus(
        packer.running
        ??
        packer.packer_running
        ??
        packer.status
    );
}


/* ==========================================================
   NOTIFICATION PANEL — LIVE PACKER STATUS CHIP

   Keeps a persistent, always-current "Packer N: RUNNING /
   STOPPED" chip at the top of the notification panel, so
   opening it immediately shows the live state rather than
   requiring the person to interpret a list of historical alert
   entries to figure out whether the packer is running right
   now.
   ========================================================== */

function updateNotificationLiveStatus(
    index,
    running
) {

    const dot =
        byId("notificationLiveStatusDot");

    const text =
        byId("notificationLiveStatusText");

    if (!text) {
        return;
    }

    if (running === null) {

        text.textContent =
            `Packer ${index}: No data`;

        if (dot) {
            dot.className =
                "live-status-dot unknown";
        }

        return;
    }

    text.textContent =
        `Packer ${index}: ${running ? "RUNNING" : "STOPPED"}`;

    if (dot) {

        dot.className =
            `live-status-dot ${running ? "running" : "stopped"}`;
    }
}


/* ==========================================================
   SYSTEM STATE CHANGE DETECTION
   ========================================================== */

function detectAndNotifySystemChanges(state) {

    if (!state || typeof state !== "object") {
        return;
    }

    // Check camera status changes
    const cameras = state.cameras || {};
    Object.entries(cameras).forEach(([cameraId, cameraData]) => {
        const cameraNum = parseInt(cameraId.replace("camera_", "")) + 1;
        const isOnline = Boolean(cameraData.online) || cameraIsOnline(cameraData);
        const wasOnline = appState.lastNotificationStates.cameras[cameraId];

        if (wasOnline === undefined) {
            // First time checking this camera
            appState.lastNotificationStates.cameras[cameraId] = isOnline;
        } else if (wasOnline !== isOnline) {
            // Status changed
            appState.lastNotificationStates.cameras[cameraId] = isOnline;

            if (isOnline) {
                addNotification(
                    `Camera ${cameraNum} Online`,
                    `Camera ${cameraNum} connection restored`,
                    "success",
                    `camera-${cameraNum}`
                );
            } else {
                addNotification(
                    `Camera ${cameraNum} Offline`,
                    `Camera ${cameraNum} connection lost`,
                    "danger",
                    `camera-${cameraNum}`
                );
            }
        }
    });

    // ------------------------------------------------------
    // PACKERS (1..PACKER_COUNT)
    //
    // Each packer's running/PLC/PMS/DCS state is tracked
    // independently and compared with normalizeBooleanStatus(),
    // which accepts booleans, numbers, or strings in any casing
    // ("online"/"ONLINE"/"running"/true/1/etc). This replaces
    // the old strict `=== "online"` check, which treated any
    // other casing/format as offline - the cause of stale
    // "Offline"/"Stopped" notifications piling up even while the
    // system was actually healthy and running.
    // ------------------------------------------------------

    if (!appState.lastNotificationStates.packers) {
        appState.lastNotificationStates.packers = {};
    }

    for (
        let index = 1;
        index <= PACKER_COUNT;
        index += 1
    ) {

        const packer =
            findPackerByIndex(state, index);

        if (!packer) {
            continue;
        }

        const key =
            `packer-${index}`;

        if (!appState.lastNotificationStates.packers[key]) {
            appState.lastNotificationStates.packers[key] = {};
        }

        const previous =
            appState.lastNotificationStates.packers[key];

        const checks = [
            {
                field: "running",
                value: packerIsRunning(packer),
                onLabel: "Running",
                offLabel: "Stopped",
                onMessage: "Production resumed",
                offMessage: "Production line is stopped",
                onSeverity: "success",
                offSeverity: "warning"
            },
            {
                field: "plc",
                value: normalizeBooleanStatus(packer.plc_status),
                onLabel: "PLC Online",
                offLabel: "PLC Offline",
                onMessage: "Packer PLC communication restored",
                offMessage: "Packer PLC communication unavailable",
                onSeverity: "success",
                offSeverity: "danger"
            },
            {
                field: "pms",
                value: normalizeBooleanStatus(packer.pms_status),
                onLabel: "PMS Online",
                offLabel: "PMS Offline",
                onMessage: "PMS PC connection restored",
                offMessage: "PMS PC connection unavailable",
                onSeverity: "success",
                offSeverity: "danger"
            },
            {
                field: "dcs",
                value: normalizeBooleanStatus(packer.dcs_status),
                onLabel: "DCS Online",
                offLabel: "DCS Offline",
                onMessage: "DCS connection restored",
                offMessage: "DCS connection unavailable",
                onSeverity: "success",
                offSeverity: "danger"
            }
        ];

        checks.forEach(
            check => {

                const prior =
                    previous[check.field];

                if (prior === undefined) {

                    previous[check.field] =
                        check.value;

                    return;
                }

                if (prior === check.value) {
                    return;
                }

                previous[check.field] =
                    check.value;

                addNotification(
                    `Packer ${index} ${check.value ? check.onLabel : check.offLabel}`,
                    check.value ? check.onMessage : check.offMessage,
                    check.value ? check.onSeverity : check.offSeverity,
                    `${key}-${check.field}`
                );
            }
        );

        // Keep the live status chip in the notification panel in
        // sync with whichever packer the System Status page is
        // currently focused on.
        if (index === (appState.selectedPacker || 1)) {

            updateNotificationLiveStatus(
                index,
                packerIsRunning(packer)
            );
        }
    }
}




function findCameraByIndex(
    cameras,
    index
) {

    const entries =
        Object.entries(
            cameras || {}
        );


    const wanted =
        `camera${index}`
            .replaceAll(" ", "")
            .replaceAll("_", "")
            .toLowerCase();


    const direct =
        entries.find(
            ([key, camera]) => {

                const values = [
                    key,
                    camera?.camera_id,
                    camera?.camera_name,
                    camera?.name
                ];


                return values.some(
                    value => {

                        const normalized =
                            String(
                                value || ""
                            )
                                .replaceAll(
                                    " ",
                                    ""
                                )
                                .replaceAll(
                                    "_",
                                    ""
                                )
                                .toLowerCase();


                        return (
                            normalized
                            ===
                            wanted
                        );
                    }
                );
            }
        );


    if (direct) {

        return direct[1];
    }


    /*
    Fallback to insertion order if backend camera
    keys do not exactly match Camera1/Camera2/etc.
    */

    return (
        entries[
            index - 1
        ]?.[1]
        ||
        null
    );
}


/* ==========================================================
   CAMERA CARDS
   ========================================================== */

function renderCameraCards(cameras) {

    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const camera =
            findCameraByIndex(
                cameras,
                index
            );


        renderCameraCard(
            index,
            camera
        );
    }
}


function renderCameraCard(
    index,
    camera
) {

    if (!camera) {

        setText(
            `camera${index}Count`,
            "0"
        );


        setText(
            `camera${index}Printed`,
            "0"
        );


        setText(
            `camera${index}Missing`,
            "0"
        );


        setText(
            `camera${index}RoiCount`,
            "0"
        );


        setText(
            `camera${index}RoiActive`,
            "0"
        );


        setText(
            `camera${index}RoiTracks`,
            "--"
        );


        setText(
            `camera${index}Fps`,
            "0.0"
        );


        setText(
            `camera${index}Status`,
            "OFFLINE"
        );


        return;
    }


    const count =
        camera.count
        ??
        camera.total_count
        ??
        0;


    const entryRoiCount =
        camera.entry_roi_count
        ??
        0;


    const entryRoiActiveCount =
        camera.entry_roi_active_count
        ??
        0;


    const entryRoiActiveBags =
        camera.entry_roi_active_bags
        ??
        [];


    const printed =
        camera.printed_count
        ??
        camera.printed_bags_count
        ??
        0;


    const missing =
        camera.missing_count
        ??
        camera.not_printed_bags_count
        ??
        0;


    setText(
        `camera${index}Count`,
        formatInteger(count)
    );


    setText(
        `camera${index}RoiCount`,
        formatInteger(entryRoiCount)
    );


    setText(
        `camera${index}RoiActive`,
        formatInteger(entryRoiActiveCount)
    );


    setText(
        `camera${index}RoiTracks`,
        formatRoiBagDetail(entryRoiActiveBags)
    );


    setText(
        `camera${index}Printed`,
        formatInteger(printed)
    );


    setText(
        `camera${index}Missing`,
        formatInteger(missing)
    );


    setText(
        `camera${index}Fps`,
        formatDecimal(
            camera.fps,
            1
        )
    );


    const statusRaw =
        String(
            camera.status
            || "offline"
        ).toLowerCase();

    const isOnline =
        Boolean(
            camera.online
            ??
            (statusRaw === "online")
        );

    setText(
        `camera${index}Status`,
        isOnline
            ? "ONLINE"
            : "OFFLINE"
    );

    /*
    Bug fix: the status badge's CSS class was never updated to
    match the live status, so it always kept its initial
    "offline" (red) class from the HTML template even when the
    camera was actually online. Toggling the class here makes
    the badge render green when online and red when offline.
    */

    const statusBadge =
        byId(`camera${index}Status`);

    if (statusBadge) {

        statusBadge.classList.toggle(
            "online",
            isOnline
        );

        statusBadge.classList.toggle(
            "offline",
            !isOnline
        );
    }

    const cameraCard =
        byId(`camera-card-${index}`);

    if (cameraCard) {

        cameraCard.classList.toggle(
            "online",
            isOnline
        );

        cameraCard.classList.toggle(
            "offline",
            !isOnline
        );
    }
}


/* ==========================================================
   JAM STATUS NORMALIZATION
   ========================================================== */

function normalizeJamStatus(camera) {

    if (
        !camera
        ||
        typeof camera
        !== "object"
    ) {

        return "unknown";
    }


    const enabled =
        camera.jam_detection_enabled;


    if (
        enabled === false
        ||
        enabled === 0
        ||
        enabled === "false"
    ) {

        return "disabled";
    }


    /*
    Explicit confirmed jam takes highest priority.
    */

    if (
        camera.jam_detected
        === true
    ) {

        return "jam";
    }


    /*
    Explicit warning takes priority over textual status.
    */

    if (
        camera.jam_warning
        === true
    ) {

        return "warning";
    }


    const raw =
        String(
            camera.jam_status
            || "normal"
        )
            .trim()
            .toLowerCase()
            .replaceAll(
                "_",
                "-"
            );


    if (
        raw === "jam"
        ||
        raw === "jammed"
    ) {

        return "jam";
    }


    if (
        raw === "warning"
    ) {

        return "warning";
    }


    if (
        raw === "slow"
    ) {

        return "slow";
    }


    if (
        raw === "recovering"
        ||
        raw === "recovery"
    ) {

        return "recovering";
    }


    if (
        raw === "disabled"
    ) {

        return "disabled";
    }


    if (
        raw === "normal"
    ) {

        return "normal";
    }


    return "unknown";
}


/* ==========================================================
   JAM BADGE
   ========================================================== */

function setJamBadge(
    element,
    status
) {

    if (!element) {
        return;
    }


    const normalized =
        JAM_STATES.includes(
            status
        )
            ? status
            : "unknown";


    JAM_STATES.forEach(
        state => {

            element.classList.remove(
                state
            );
        }
    );


    element.classList.add(
        normalized
    );


    element.textContent =
        normalized.toUpperCase();
}


/* ==========================================================
   JAM MONITORING
   ========================================================== */

function renderJamMonitoring(cameras) {

    let normalCount = 0;

    let slowCount = 0;

    let warningCount = 0;

    let activeJamCount = 0;


    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const camera =
            findCameraByIndex(
                cameras,
                index
            );


        const enabled =
            Boolean(
                camera
                    ?.jam_detection_enabled
            );


        const status =
            camera
                ? normalizeJamStatus(
                    camera
                )
                : "disabled";


        const cameraJamCount =
            Math.max(
                0,
                Math.round(
                    safeNumber(
                        camera
                            ?.active_jam_count,
                        0
                    )
                )
            );


        const trackIds =
            Array.isArray(
                camera
                    ?.condition_c_track_ids
            )
                ? camera
                    .condition_c_track_ids
                : (
                    Array.isArray(
                        camera
                            ?.active_jam_track_ids
                    )
                        ? camera
                            .active_jam_track_ids
                        : []
                );


        /* --------------------------------------------------
           CONDITION C (ROI OCCUPANCY) FIELDS
           -------------------------------------------------- */

        const roiBagCount =
            safeNumber(
                camera
                    ?.condition_c_bag_count,
                0
            );

        const roiGap =
            camera
                ?.condition_c_minimum_gap_mm;

        const roiDistances =
            camera
                ?.condition_c_distances
            || [];

        const roiImage =
            camera
                ?.condition_c_image_url
            ||
            camera
                ?.condition_c_image_path;

        const roiStatus =
            camera
                ?.condition_c_status
            ||
            (
                camera
                    ?.condition_c_detected
                    ? "jam"
                    : "normal"
            );


        /* --------------------------------------------------
           SYSTEM JAM COUNTERS
           -------------------------------------------------- */

        if (enabled) {

            if (
                status === "normal"
            ) {

                normalCount += 1;
            }


            else if (
                status === "slow"
            ) {

                slowCount += 1;
            }


            else if (
                status === "warning"
            ) {

                warningCount += 1;
            }


            else if (
                status === "jam"
            ) {

                activeJamCount +=
                    cameraJamCount > 0
                        ? cameraJamCount
                        : 1;
            }
        }


        /* --------------------------------------------------
           DASHBOARD CAMERA JAM BADGE
           -------------------------------------------------- */

        const summaryBadge =
            byId(
                `camera${index}JamStatus`
            );


        setJamBadge(
            summaryBadge,
            status
        );


        const dashboardCard =
            byId(
                `camera-card-${index}`
            )
            ||
            summaryBadge
                ?.closest(
                    ".camera-card"
                );


        if (dashboardCard) {

            dashboardCard
                .classList
                .toggle(
                    "jam-active",
                    status === "jam"
                );
        }


        /* --------------------------------------------------
           DEDICATED JAM PAGE
           -------------------------------------------------- */

        const jamBadge =
            byId(
                `jamCamera${index}Status`
            );


        setJamBadge(
            jamBadge,
            status
        );


        setText(
            `jamCamera${index}Count`,
            formatInteger(
                cameraJamCount
            )
        );


        setText(
            `jamCamera${index}RoiBags`,
            formatInteger(
                roiBagCount
            )
        );


        setText(
            `jamCamera${index}MinimumGap`,
            roiGap == null
                ? "--"
                : `${formatDecimal(roiGap, 1)} mm`
        );


        const distanceElement =
            byId(
                `jamCamera${index}Distances`
            );

        if (distanceElement) {

            distanceElement.innerHTML =
                roiDistances.length
                    ? roiDistances
                        .map(
                            d => `${formatDecimal(d?.distance_mm, 1)} mm`
                        )
                        .join("<br>")
                    : "--";
        }


        const roiImageElement =
            byId(
                `jamCamera${index}Image`
            );

        if (roiImageElement) {

            roiImageElement.src =
                roiImage
                    ? `${roiImage}?t=${Date.now()}`
                    : "";
        }


        setText(
            `jamCamera${index}ConditionC`,
            roiStatus.toUpperCase()
        );


        setText(
            `camera${index}ConditionC`,
            roiStatus.toUpperCase()
        );


        setText(
            `jamCamera${index}Tracks`,
            trackIds.length > 0
                ? trackIds.join(", ")
                : "--"
        );


        setText(
            `jamCamera${index}Enabled`,
            enabled
                ? "Enabled"
                : "Disabled"
        );


        const jamCard =
            byId(
                `jam-camera-card-${index}`
            );


        if (jamCard) {

            JAM_STATES.forEach(
                state => {

                    jamCard
                        .classList
                        .remove(
                            state
                        );
                }
            );


            jamCard
                .classList
                .add(
                    status
                );
        }
    }


    /* ------------------------------------------------------
       JAM KPI CARDS
       ------------------------------------------------------ */

    setText(
        "jamNormalCameras",
        formatInteger(
            normalCount
        )
    );


    setText(
        "jamSlowCameras",
        formatInteger(
            slowCount
        )
    );


    setText(
        "jamWarningCameras",
        formatInteger(
            warningCount
        )
    );


    setText(
        "jamActiveCount",
        formatInteger(
            activeJamCount
        )
    );


    /* ------------------------------------------------------
       OVERVIEW JAM BANNER (first/dashboard page)

       Same numbers computed above, just rendered a second
       place. This mirrors the Jam Monitor KPI cards rather
       than re-deriving them, so the two views can never show
       different counts for the same underlying camera data.
       ------------------------------------------------------ */

    setText(
        "overviewJamActiveCount",
        formatInteger(activeJamCount)
    );

    setText(
        "overviewJamWarningCameras",
        formatInteger(warningCount)
    );

    setText(
        "overviewJamSlowCameras",
        formatInteger(slowCount)
    );

    setText(
        "overviewJamNormalCameras",
        formatInteger(normalCount)
    );

    const banner = byId("overviewJamBanner");

    if (banner) {

        const bannerTitle = byId("overviewJamBannerTitle");
        const bannerSubtitle = byId("overviewJamBannerSubtitle");

        banner.classList.remove(
            "status-normal",
            "status-warning",
            "status-jam"
        );

        if (activeJamCount > 0) {

            banner.classList.add("status-jam");

            if (bannerTitle) {
                bannerTitle.textContent =
                    activeJamCount === 1
                        ? "1 Active Jam"
                        : `${activeJamCount} Active Jams`;
            }

            if (bannerSubtitle) {
                bannerSubtitle.textContent =
                    "Immediate attention required";
            }

        } else if (warningCount > 0 || slowCount > 0) {

            banner.classList.add("status-warning");

            if (bannerTitle) {
                bannerTitle.textContent = "Reduced Flow Detected";
            }

            if (bannerSubtitle) {
                bannerSubtitle.textContent =
                    "No confirmed jam yet -- monitor closely";
            }

        } else {

            banner.classList.add("status-normal");

            if (bannerTitle) {
                bannerTitle.textContent = "No Active Jams";
            }

            if (bannerSubtitle) {
                bannerSubtitle.textContent =
                    "All conveyors running normally";
            }
        }
    }
}/* ==========================================================
   PRODUCTION
   ========================================================== */

async function loadProduction() {

    try {

        const data =
            await apiFetch(
                "/production"
            );

        appState.production =
            data;

        renderProduction(
            data
        );

    }

    catch (error) {

        console.error(
            "Failed loading production:",
            error
        );
    }
}


function renderProduction(data) {

    if (
        !data
        ||
        typeof data !== "object"
    ) {
        return;
    }


    const total =
        safeNumber(
            data.total_bags
            ??
            0
        );


    const printed =
        safeNumber(
            data.printed_bags
            ??
            0
        );


    const missing =
        safeNumber(
            data.not_printed_bags
            ??
            0
        );


    const quality =
        safeNumber(
            data.print_quality
            ??
            0
        );


    /*
    Production Rate must never be invented. Only render a number
    when the backend actually provides one; otherwise show "--"
    rather than a fabricated 0.
    */

    const rateAvailable =
        data.production_rate_per_hour !== null
        &&
        data.production_rate_per_hour !== undefined
        &&
        Number.isFinite(
            Number(data.production_rate_per_hour)
        );

    setText(
        "productionTotal",
        formatInteger(total)
    );


    setText(
        "productionPrinted",
        formatInteger(printed)
    );


    setText(
        "productionMissing",
        formatInteger(missing)
    );


    setText(
        "productionQuality",
        formatPercent(quality)
    );


    setText(
        "productionRate",
        rateAvailable
            ? formatInteger(data.production_rate_per_hour)
            : "--"
    );


    renderProductionCameras(
        data.cameras
        ||
        appState
            .dashboardState
            ?.cameras
        ||
        {}
    );
}


/* ==========================================================
   PRODUCTION CAMERA TABLE
   ========================================================== */

function renderProductionCameras(cameras) {

    const body =
        byId(
            "productionCameraTable"
        );


    if (!body) {
        return;
    }


    const rows = [];


    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const camera =
            findCameraByIndex(
                cameras,
                index
            );


        if (!camera) {
            continue;
        }


        const count =
            safeNumber(
                camera.count
                ??
                camera.total_count
                ??
                0
            );


        const printed =
            safeNumber(
                camera.printed_count
                ??
                camera.printed_bags_count
                ??
                0
            );


        const missing =
            safeNumber(
                camera.missing_count
                ??
                camera.not_printed_bags_count
                ??
                0
            );


        const printRate =
            count > 0
                ? (printed / count) * 100
                : 0;


        const jamStatus =
            normalizeJamStatus(
                camera
            );


        rows.push(
            `
            <tr>

                <td>
                    Camera ${index}
                </td>

                <td>
                    ${formatInteger(count)}
                </td>

                <td>
                    ${formatInteger(printed)}
                </td>

                <td>
                    ${formatInteger(missing)}
                </td>

                <td>
                    ${formatPercent(printRate)}
                </td>

                <td>
                    <span class="jam-status ${jamStatus}">
                        ${jamStatus.toUpperCase()}
                    </span>
                </td>

            </tr>
            `
        );
    }


    body.innerHTML =
        rows.length > 0
            ? rows.join("")
            : `
                <tr>
                    <td colspan="6">
                        No production data available.
                    </td>
                </tr>
              `;
}


/* ==========================================================
   ANALYTICS
   ========================================================== */

async function loadAnalytics() {

    try {

        const data =
            await apiFetch(
                "/analytics"
            );


        appState.analytics =
            data;


        renderAnalytics(
            data
        );

    }

    catch (error) {

        console.error(
            "Failed loading analytics:",
            error
        );
    }
}


function renderAnalytics(data) {

    if (
        !data
        ||
        typeof data !== "object"
    ) {
        return;
    }


    const total =
        safeNumber(
            data.total_events
            ??
            0
        );


    const printed =
        safeNumber(
            data.printed_events
            ??
            0
        );


    const missing =
        safeNumber(
            data.missing_events
            ??
            0
        );


    const quality =
        safeNumber(
            data.print_quality
            ??
            0
        );


    setText(
        "analyticsTotalEvents",
        formatInteger(total)
    );


    setText(
        "analyticsPrinted",
        formatInteger(printed)
    );


    setText(
        "analyticsMissing",
        formatInteger(missing)
    );


    setText(
        "analyticsQuality",
        formatPercent(quality)
    );


    renderAnalyticsCameraTable(
        data.by_camera
        ||
        []
    );


    /* ------------------------------------------------------
       CHARTS

       Shared between the Dashboard page (Production Trend)
       and the Analytics page (Hourly / Camera / Print / Shift).
       byId() guards inside each updater mean this is a no-op
       for canvases that aren't on the current page.
       ------------------------------------------------------ */

    updateProductionTrendChart(
        data.hourly || []
    );

    updateAnalyticsHourlyChart(
        data.hourly || []
    );

    updateAnalyticsCameraChart(
        data.by_camera || []
    );

    updateAnalyticsPrintChart(
        printed,
        missing
    );

    updateAnalyticsShiftChart(
        data.by_shift || []
    );
}


/* ==========================================================
   ANALYTICS CAMERA TABLE
   ========================================================== */

function renderAnalyticsCameraTable(cameras) {

    const body =
        byId(
            "analyticsCameraTableBody"
        );


    if (!body) {
        return;
    }


    const rows = [];


    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const camera =
            findCameraByIndex(
                cameras,
                index
            );


        if (!camera) {
            continue;
        }


        const count =
            safeNumber(
                camera.count
                ??
                camera.total_count
                ??
                0
            );


        const printed =
            safeNumber(
                camera.printed_count
                ??
                camera.printed_bags_count
                ??
                0
            );


        const missing =
            safeNumber(
                camera.missing_count
                ??
                camera.not_printed_bags_count
                ??
                0
            );


        const printRate =
            count > 0
                ? (printed / count) * 100
                : 0;


        const jamStatus =
            normalizeJamStatus(
                camera
            );


        rows.push(
            `
            <tr>

                <td>
                    Camera ${index}
                </td>

                <td>
                    ${formatInteger(count)}
                </td>

                <td>
                    ${formatInteger(printed)}
                </td>

                <td>
                    ${formatInteger(missing)}
                </td>

                <td>
                    ${formatPercent(printRate)}
                </td>

                <td>
                    ${formatDecimal(
                        camera.fps,
                        1
                    )}
                </td>

                <td>
                    <span class="jam-status ${jamStatus}">
                        ${jamStatus.toUpperCase()}
                    </span>
                </td>

            </tr>
            `
        );
    }


    body.innerHTML =
        rows.length > 0
            ? rows.join("")
            : `
                <tr>
                    <td colspan="7">
                        No analytics data available.
                    </td>
                </tr>
              `;
}


/* ==========================================================
   EVENTS
   ========================================================== */

async function loadEvents() {

    try {

        const query =
            buildEventQuery();


        const path =
            query
                ? `/events?${query}`
                : "/events";


        const data =
            await apiFetch(
                path
            );


        const events =
            Array.isArray(data)
                ? data
                : (
                    data?.events
                    ??
                    data?.items
                    ??
                    []
                );


        appState.events =
            events;


        renderEvents(
            events
        );

    }

    catch (error) {

        console.error(
            "Failed loading events:",
            error
        );
    }
}


/* ==========================================================
   EVENT QUERY
   ========================================================== */

function isWildcardFilterValue(value) {

    return (
        !value
        ||
        value.trim() === ""
        ||
        value.trim().toLowerCase() === "all"
    );
}


function buildEventQuery() {

    const params =
        new URLSearchParams();


    const camera =
        byId(
            "cameraFilter"
        )?.value;


    const eventType =
        byId(
            "printFilter"
        )?.value;


    const shift =
        byId(
            "shiftFilter"
        )?.value;


    const dateFrom =
        byId(
            "startDate"
        )?.value;


    const dateTo =
        byId(
            "endDate"
        )?.value;


    const limit =
        byId(
            "recordLimit"
        )?.value;


    if (!isWildcardFilterValue(camera)) {

        params.set(
            "camera",
            camera
        );
    }


    if (!isWildcardFilterValue(eventType)) {

        params.set(
            "print_status",
            eventType
        );
    }


    if (!isWildcardFilterValue(shift)) {

        params.set(
            "shift",
            shift
        );
    }


    if (dateFrom) {

        params.set(
            "start",
            dateFrom
        );
    }


    if (dateTo) {

        params.set(
            "end",
            dateTo
        );
    }


    if (limit) {

        params.set(
            "limit",
            limit
        );
    }


    return params.toString();
}


/* ==========================================================
   EVENT TABLE
   ========================================================== */

function renderEvents(events) {

    const body =
        byId(
            "eventsTableBody"
        );


    if (!body) {
        return;
    }


    setText(
        "eventRecordCount",
        formatInteger(
            Array.isArray(events)
                ? events.length
                : 0
        )
    );


    if (
        !Array.isArray(events)
        ||
        events.length === 0
    ) {

        body.innerHTML =
            `
            <tr>
                <td colspan="8">
                    No events found.
                </td>
            </tr>
            `;

        return;
    }


    body.innerHTML =
        events
            .map(
                event => {

                    const camera =
                        event.camera_name
                        ??
                        event.camera
                        ??
                        event.camera_id
                        ??
                        "--";


                    const eventType =
                        event.event_type
                        ??
                        event.type
                        ??
                        event.event
                        ??
                        "--";


                    const totalCount =
                        event.total_count
                        ??
                        event.count
                        ??
                        "--";


                    const printed =
                        event.printed
                        ??
                        event.print_present;


                    const printStatus =
                        printed === true
                            ? "PRINTED"
                            : (
                                printed === false
                                    ? "MISSING"
                                    : "--"
                            );


                    const timestamp =
                        event.timestamp
                        ??
                        event.created_at
                        ??
                        event["@timestamp"]
                        ??
                        null;


                    const trackId =
                        event.track_id
                        ??
                        event.bag_track_id
                        ??
                        "--";


                    return `
                    <tr>

                        <td>
                            ${escapeHtml(
                                formatDateTime(
                                    timestamp
                                )
                            )}
                        </td>

                        <td>
                            ${escapeHtml(camera)}
                        </td>

                        <td>
                            ${escapeHtml(eventType)}
                        </td>

                        <td>
                            ${escapeHtml(totalCount)}
                        </td>

                        <td>
                            ${escapeHtml(printStatus)}
                        </td>

                        <td>
                            ${escapeHtml(trackId)}
                        </td>

                        <td>
                            ${escapeHtml(
                                event.event_id
                                ??
                                event.id
                                ??
                                "--"
                            )}
                        </td>

                        <td>
                            ${escapeHtml(
                                event.status
                                ??
                                "OK"
                            )}
                        </td>

                    </tr>
                    `;
                }
            )
            .join("");
}


/* ==========================================================
   EVENT FILTER CONTROLS
   ========================================================== */

function initializeEventControls() {

    const searchButton =
        byId(
            "applyFilters"
        );


    if (searchButton) {

        searchButton.addEventListener(
            "click",
            async () => {

                await loadEvents();
            }
        );
    }


    const resetButton =
        byId(
            "resetFilters"
        );


    if (resetButton) {

        resetButton.addEventListener(
            "click",
            async () => {

                [
                    "cameraFilter",
                    "printFilter",
                    "shiftFilter"
                ]
                    .forEach(
                        id => {

                            const element =
                                byId(id);


                            if (element) {

                                element.value =
                                    "all";
                            }
                        }
                    );


                [
                    "startDate",
                    "endDate"
                ]
                    .forEach(
                        id => {

                            const element =
                                byId(id);


                            if (element) {

                                element.value =
                                    "";
                            }
                        }
                    );


                const recordLimitElement =
                    byId("recordLimit");


                if (recordLimitElement) {

                    recordLimitElement.value =
                        "100";
                }


                await loadEvents();
            }
        );
    }


    const exportButton =
        byId(
            "exportButton"
        );


    if (exportButton) {

        exportButton.addEventListener(
            "click",
            () => {

                exportEventsCsv(
                    appState.events
                );
            }
        );
    }
}


/* ==========================================================
   EVENT CSV EXPORT
   ========================================================== */

function exportEventsCsv(events) {

    if (
        !Array.isArray(events)
        ||
        events.length === 0
    ) {

        alert(
            "No events available to export."
        );

        return;
    }


    const headers = [
        "Timestamp",
        "Camera",
        "Event Type",
        "Total Count",
        "Printed",
        "Track ID",
        "Event ID"
    ];


    const rows =
        events.map(
            event => [

                event.timestamp
                ??
                event.created_at
                ??
                event["@timestamp"]
                ??
                "",

                event.camera_name
                ??
                event.camera
                ??
                event.camera_id
                ??
                "",

                event.event_type
                ??
                event.type
                ??
                event.event
                ??
                "",

                event.total_count
                ??
                event.count
                ??
                "",

                event.printed
                ??
                event.print_present
                ??
                "",

                event.track_id
                ??
                "",

                event.event_id
                ??
                event.id
                ??
                ""
            ]
        );


    const csv =
        [
            headers,
            ...rows
        ]
            .map(
                row =>

                    row
                        .map(
                            value => {

                                const text =
                                    String(
                                        value ?? ""
                                    )
                                        .replaceAll(
                                            '"',
                                            '""'
                                        );


                                return `"${text}"`;
                            }
                        )
                        .join(",")
            )
            .join("\n");


    const blob =
        new Blob(
            [csv],
            {
                type:
                    "text/csv;charset=utf-8"
            }
        );


    const url =
        URL.createObjectURL(
            blob
        );


    const anchor =
        document.createElement(
            "a"
        );


    anchor.href =
        url;


    anchor.download =
        `fillpac-events-${Date.now()}.csv`;


    document.body.appendChild(
        anchor
    );


    anchor.click();


    anchor.remove();


    URL.revokeObjectURL(
        url
    );
}


/* ==========================================================
   LIVE CAMERA STREAMS
   ========================================================== */

function initializeLiveStreams() {

    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const feedContainer =
            byId(
                `liveCamera${index}Feed`
            );


        if (!feedContainer) {
            continue;
        }


        const cameraName =
            `Camera ${index}`;


        const image =
            document.createElement(
                "img"
            );


        image.className =
            "camera-feed-stream";


        image.alt =
            `${cameraName} live feed`;


        image.addEventListener(
            "error",
            () => {

                feedContainer.classList
                    .remove(
                        "has-stream"
                    );

                feedContainer.innerHTML =
                    `
                    <svg class="icon-svg" aria-hidden="true"><use href="#icon-video-slash" xlink:href="#icon-video-slash"></use></svg>
                    <span>
                        Live video stream not connected
                    </span>
                    `;
            }
        );


        image.addEventListener(
            "load",
            () => {

                feedContainer.classList.add(
                    "has-stream"
                );
            }
        );


        image.src =
            `${API_BASE}/live/${encodeURIComponent(
                cameraName
            )}`;


        feedContainer.innerHTML = "";

        feedContainer.appendChild(
            image
        );
    }
}


/* ==========================================================
   CAMERA CONFIGURATION
   ========================================================== */

async function loadCameraConfig() {

    try {

        const data =
            await apiFetch(
                "/cameras"
            );


        appState.cameraConfig =
            data;


        renderCameraManagement(
            data
        );


        return data;

    }

    catch (error) {

        console.error(
            "Failed loading camera configuration:",
            error
        );

        return null;
    }
}


/* ==========================================================
   SETTINGS PAGE (editable, schema-driven from config.yaml)

   Every field on this page is generated from whatever
   GET /api/settings returns -- nothing here hard-codes the
   shape of config.yaml, so it stays correct if fields are
   added/removed on the backend later.

   Read access: any logged-in user (operator or admin).
   Write access (Save / Validate / Reset): admin role only --
   the backend enforces this too (403 on write routes for a
   non-admin token), this is just UI-level convenience so an
   operator sees a clearly read-only page instead of a
   confusing 403 after editing.
   ========================================================== */

// Clearer labels / hints for fields whose bare key name is
// ambiguous (e.g. database.name). Keyed by dotted config path.
const SETTINGS_FIELD_LABELS = {
    "database.name": "Database Name",
    "database.server": "Server / Instance",
    "database.driver": "ODBC Driver",
    "database.connect_timeout": "Connect Timeout (s)",
    "database.authentication.trusted_connection": "Windows Authentication",
    "database.authentication.username": "SQL Username"
};

const SETTINGS_FIELD_HINTS = {
    "database.enabled": "When off, every database connection attempt fails on purpose.",
    "database.server": "e.g. WS-7YHHYD3\\SQLEXPRESS or localhost,1433",
    "database.driver": "e.g. SQL Server, or ODBC Driver 17/18 for SQL Server",
    "database.authentication.trusted_connection": "On: uses the Windows account running the service. Off: uses the username below plus the FILLPAC_DB_PASSWORD environment variable (the password is never stored here).",
    "database.encrypt": "Only applied by ODBC Driver 17/18 (ignored by the legacy 'SQL Server' driver)."
};

const settingsState = {
    original: null,     // last-known-saved config object (deep clone)
    working: null,       // in-progress edited copy
    activeTab: "global",
    advanced: false,
    controlsBound: false,
    loading: false
};

// Top-level (and per-camera) config keys that are deep/internal
// tuning rather than everyday operator settings. Hidden unless
// "Show Advanced Settings" is on.
const SETTINGS_ADVANCED_GROUPS = new Set([
    "tracker",
    "model",
    "elasticsearch",
    "logging",
    "output"
]);

// Friendly titles/descriptions for known config sections. Any
// key not listed here still renders fine -- it just falls back
// to a prettified version of the raw key name.
const SETTINGS_GROUP_META = {
    project: { title: "Project", description: "Application identity" },
    model: { title: "AI Model", description: "Detection model configuration (advanced)" },
    tracker: { title: "Tracker", description: "ByteTrack tuning (advanced)" },
    counting: { title: "Counting", description: "Bag counting parameters" },
    print_detection: { title: "Print Detection", description: "Print inspection thresholds" },
    jam_detection: { title: "Jam Detection", description: "Conveyor jam thresholds" },
    bag_spacing: { title: "Bag Spacing", description: "Spacing / gap jam detection" },
    condition_c: { title: "Condition C (ROI Occupancy)", description: "ROI bag-occupancy monitoring" },
    display: { title: "Display Overlays", description: "On-screen overlay toggles" },
    output: { title: "Output", description: "Video / image output (advanced)" },
    elasticsearch: { title: "Elasticsearch", description: "Optional search indexing (advanced)" },
    dashboard: { title: "Dashboard Service", description: "Dashboard host / port settings" },
    database: { title: "Database (SQL Server)", description: "SQL Server connection. Use Test Database before saving; a wrong value blocks login after the next restart." },
    authentication: { title: "Authentication", description: "Windows (trusted) login, or SQL login with the FILLPAC_DB_PASSWORD environment variable" },
    logging: { title: "Logging", description: "Log level and file (advanced)" },
    entry_roi_counting: { title: "Entry ROI Counting", description: "ROI-based entry counting" },
    roi: { title: "Counting Line (yellow)", description: "Line a bag's centre crosses to be counted. Horizontal line: y1 = y2. Vertical line: x1 = x2. Coordinates are pixels of this camera's video frame." },
    detection_roi: { title: "Detection ROI", description: "Detections whose centre is outside this box are ignored. Pixels of the video frame. Does not make inference faster." },
    entry_roi: { title: "Entry ROI", description: "Region of interest coordinates" },
    calibration: { title: "Calibration", description: "Pixel-to-world calibration points (advanced)" },
    indices: { title: "Indices", description: "Elasticsearch index names" },

    // Path-specific titles (relative to a camera, or global). These win
    // over the plain-key entries above so each "roi" says what it is.
    "roi.entry_roi": { title: "Counting Line - Entry Direction", description: "Entry zone and the direction bags move through it" },
    "entry_roi_counting.roi": { title: "Entry ROI Zone (pink)", description: "A bag is counted when its centre enters this box. Pixels of the video frame." },
    "jam_detection.roi": { title: "Movement Jam Zone (green)", description: "Bags stopping or slowing inside this box raise a movement jam. Pixels of the video frame." },
    "bag_spacing.roi": { title: "Bag Spacing Zone", description: "Gap between bags is measured inside this box. Pixels of the video frame." },
    "condition_c.roi": { title: "ROI Occupancy Zone", description: "Too many bags inside this box raise an occupancy jam. Pixels of the video frame." },
    "model.detection_roi": { title: "Detection ROI", description: "Detections whose centre is outside this box are ignored. Pixels of the video frame. Does not make inference faster." }
};

// Sections a camera may override. When a camera does not define one,
// the camera tab offers "Override for this camera" instead of hiding it.
const SETTINGS_CAMERA_OVERRIDABLE = [
    "counting",
    "print_detection",
    "jam_detection",
    "bag_spacing",
    "condition_c"
];

// Hints/labels by plain key (apply on every tab).
Object.assign(SETTINGS_FIELD_LABELS, {
    "realtime_playback": "Real-time Playback (video files)",
    "frame_skip": "Frame Skip (1 = every frame)",
    "fps_window_seconds": "FPS Averaging Window (s)",
    "performance_monitoring": "Log Performance Timings",
    "performance_log_interval": "Performance Log Interval (s)",
    "dashboard_publish_interval": "Dashboard Publish Interval (s)",
    "inference_timeout": "Inference Timeout (s)",
    "half": "FP16 (half precision)",
    "min_positive_votes": "Min Positive Print Frames",
    "decision_grace_frames": "Print Decision Delay (frames)",
    "decision_max_frames": "Print Decision Max Wait (frames)",
    "vote_threshold": "Print Vote Ratio",
    "min_observations": "Min Print Observations",
    "history_size": "Print Votes Kept (frames)",
    "entry_direction": "Entry Direction"
});

Object.assign(SETTINGS_FIELD_HINTS, {
    "source": "Video file path, or an rtsp:// URL. Any username/password in the URL is stored in plain text in config.yaml.",
    "mode": "video = play a file, rtsp = live camera stream.",
    "realtime_playback": "Video files only: play at the file's own FPS instead of as fast as the PC can. Ignored for RTSP.",
    "frame_skip": "1 = run detection on every frame. 2 = every 2nd frame, and so on. Only raise it if the GPU cannot keep up.",
    "fps_window_seconds": "On-screen FPS is averaged over this many seconds.",
    "half": "FP16 inference: faster on a GPU. The detector falls back to FP32 automatically if it is not supported.",
    "confidence": "Minimum YOLO confidence. Lower values add more boxes to post-process and track.",
    "max_detections": "Most boxes kept per frame.",
    "min_positive_votes": "A bag counts as PRINTED when at least this many frames saw a print during its whole pass (0 = use the vote ratio only).",
    "vote_threshold": "Share (0-1) of frames that must see a print for the bag to count as printed.",
    "min_observations": "Bags with fewer print observations than this are not added to Printed / Not Printed.",
    "history_size": "How many print observations are kept per bag. Keep it at least as large as the number of frames a bag is visible.",
    "decision_grace_frames": "The print result is decided once the bag's track has been gone for this many frames.",
    "decision_max_frames": "Safety limit: decide anyway after this many frames, even if the track is still visible.",
    "direction": "Direction bags travel across the counting line (down, up, left, right or both).",
    "entry_direction": "Direction bags move through the entry zone.",
    "publish_interval": "Seconds between dashboard updates. Lower = smoother but more work.",
    "dashboard_publish_interval": "Seconds between dashboard updates for this camera."
});

/* ----------------------------------------------------------
   SETTINGS UI ADDITIONS (new config: production switches,
   per-camera ai_enabled, count type, dropdowns, live badges)
   ---------------------------------------------------------- */

Object.assign(SETTINGS_FIELD_LABELS, {
    "ai_enabled": "AI Detection",
    "count_enabled": "Count Bags",
    "include_bags_in_total": "Add Bags to Total",
    "include_prints_in_total": "Add Prints to Total",
    "default_count_type": "Default Count Type",
    "stale_timeout": "Stale Data Timeout (s)",
    "buffer_size": "Frame Buffer Size",
    "bag_confidence": "Bag Confidence",
    "print_confidence": "Print Confidence",
    "min_bbox_area": "Min Box Area (px\u00b2)",
    "allowed_classes": "Allowed Class IDs",
    "minimum_safe_gap_mm": "Minimum Safe Gap (mm)",
    "jam_threshold_mm": "Jam Gap Threshold (mm)",
    "measurement_margin_mm": "Measurement Margin (mm)",
    "max_allowed_bags": "Max Bags in Zone",
    "save_roi_image": "Save Zone Image",
    "save_full_frame": "Save Full Frame",
    "save_distances": "Save Gap Distances",
    "capture_cooldown_seconds": "Capture Cooldown (s)",
    "image_directory": "Image Folder"
});

Object.assign(SETTINGS_FIELD_HINTS, {
    "ai_enabled": "Off = video only: the model is never called, so there is no counting, print or jam detection for this camera. Needs a service restart.",
    "count_enabled": "Off = no bag counting for this camera and no count events saved.",
    "include_bags_in_total": "On = this camera's bag count is added to the total bags. Off = not added (the camera still shows its own bag count).",
    "include_prints_in_total": "On = this camera's printed / not-printed counts are added to the dashboard data and totals. Off = hidden and left out of the totals.",
    "default_count_type": "Counter the dashboard shows at startup: roi = entry ROI zone counter, line = counting line counter.",
    "stale_timeout": "Seconds without an update before a camera's data is treated as stale.",
    "buffer_size": "Frames kept in the capture buffer. 1 = always work on the newest frame (lowest delay).",
    "bag_confidence": "Minimum confidence for a detection to be accepted as a bag.",
    "print_confidence": "Minimum confidence for a detection to be accepted as a print.",
    "min_bbox_area": "Boxes smaller than this many pixels\u00b2 are ignored.",
    "allowed_classes": "Model class IDs that are kept, comma-separated. Everything else is dropped.",
    "minimum_safe_gap_mm": "Smallest acceptable gap between two bags, in millimetres.",
    "jam_threshold_mm": "Gap (mm) at which a spacing jam is flagged.",
    "measurement_margin_mm": "Tolerance (mm) added around the measured gap.",
    "max_allowed_bags": "More bags than this inside the zone raises an occupancy jam.",
    "save_roi_image": "Save a picture of the zone when an occupancy jam is captured.",
    "capture_cooldown_seconds": "Minimum seconds between two saved captures."
});

Object.assign(SETTINGS_GROUP_META, {
    production: {
        title: "Production Switches",
        description: "Counting and totals. On the Global tab these are the defaults; a camera's own switches win."
    },
    entry_roi_counting: {
        title: "Entry ROI Counting",
        description: "Counts a bag when its centre enters the Entry ROI zone."
    }
});

// Dropdown fields. Looked up like labels: full path, camera-relative
// path, then plain key. A saved value that is not listed is appended
// so it is never silently changed.
const SETTINGS_SELECT_OPTIONS = {
    "mode": ["video", "rtsp"],
    "direction": ["down", "up", "left", "right", "both"],
    "entry_direction": ["down", "up", "left", "right"],
    "default_count_type": ["roi", "line"],
    "logging.level": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
};

// Fields the server applies to the running process on save
// (see apply_live_settings in server.py). Everything else needs
// a restart of the FillPac AI service.
function settingsIsLivePath(pathParts) {

    const parts = pathParts.map(String);
    const relative = (parts[0] === "cameras" && parts.length > 2)
        ? parts.slice(2)
        : parts;

    const dotted = relative.join(".");

    return (
        dotted === "dashboard.default_count_type"
        || /^production\.(count_enabled|include_bags_in_total|include_prints_in_total)$/.test(dotted)
    );
}

// Order of the section cards on a tab. Unlisted sections keep
// their config order after these.
const SETTINGS_SECTION_ORDER = [
    "production", "project", "dashboard", "database",
    "counting", "roi", "entry_roi_counting",
    "print_detection", "jam_detection", "bag_spacing", "condition_c",
    "display", "model", "tracker"
];

// Camera sections that stop mattering when a switch is off.
const SETTINGS_AI_SECTIONS = new Set([
    "model", "counting", "entry_roi_counting", "roi",
    "print_detection", "jam_detection", "bag_spacing", "condition_c"
]);

const SETTINGS_COUNT_SECTIONS = new Set([
    "counting", "entry_roi_counting", "roi"
]);

SETTINGS_CAMERA_OVERRIDABLE.push("production");


/* ----------------------------------------------------------
   SUMMARY CARD + DEPENDENCY DIMMING
   ---------------------------------------------------------- */

function settingsActiveCamera() {

    if (!settingsState.working || settingsState.activeTab === "global") {
        return null;
    }

    const index = parseInt(settingsState.activeTab.replace("camera-", ""), 10);

    return (settingsState.working.cameras || [])[index] || null;
}

function settingsEffectiveProduction(camera) {

    const globalFlags = (settingsState.working && settingsState.working.production) || {};
    const cameraFlags = (camera && camera.production) || {};

    const pick = key => {

        if (cameraFlags[key] !== undefined && cameraFlags[key] !== null) {
            return Boolean(cameraFlags[key]);
        }

        if (globalFlags[key] !== undefined && globalFlags[key] !== null) {
            return Boolean(globalFlags[key]);
        }

        return true;
    };

    return {
        count: pick("count_enabled"),
        bags: pick("include_bags_in_total"),
        prints: pick("include_prints_in_total")
    };
}

function settingsBuildSummary() {

    const chips = [];
    const notes = [];
    const working = settingsState.working || {};
    const camera = settingsActiveCamera();

    const chip = (label, value, state) => chips.push({ label, value, state });

    if (camera) {

        const flags = settingsEffectiveProduction(camera);
        const enabled = camera.enabled !== false;
        const aiOn = camera.ai_enabled !== false;
        const counting = aiOn && flags.count;

        chip("Camera", enabled ? "Enabled" : "Disabled", enabled ? "ok" : "off");

        chip(
            "Source",
            camera.mode === "rtsp" ? "RTSP live stream"
                : camera.mode === "video" ? "Video file"
                : "Not set",
            camera.mode ? "neutral" : (enabled ? "warn" : "off")
        );

        chip("AI detection", aiOn ? "On" : "Video only", aiOn ? "ok" : "off");
        chip("Bag counting", counting ? "On" : "Off", counting ? "ok" : "off");
        chip("Bags in total", flags.bags ? "Added" : "Not added", flags.bags ? "ok" : "off");
        chip("Prints in total", flags.prints ? "Added" : "Hidden", flags.prints ? "ok" : "off");

        if (!enabled) {
            notes.push({ type: "warn", text: "This camera is disabled and will not start." });
        }

        if (enabled && !camera.mode) {
            notes.push({ type: "warn", text: "No source mode is set. Choose video or rtsp, and fill in the source." });
        }

        if (enabled && !aiOn) {
            notes.push({ type: "info", text: "AI is off: this camera only streams video. Model, counting, print and jam settings below are not used." });
        }

        if (enabled && aiOn && !flags.count) {
            notes.push({ type: "info", text: "Counting is off: no bags are counted and no count events are saved for this camera." });
        }

        if (enabled && counting && !flags.bags) {
            notes.push({ type: "info", text: "Bags are counted and shown for this camera but are not added to the total bags." });
        }

    } else {

        const cameras = Array.isArray(working.cameras) ? working.cameras : [];
        const enabledCameras = cameras.filter(item => item && item.enabled !== false);
        const aiCameras = enabledCameras.filter(item => item.ai_enabled !== false);
        const flags = settingsEffectiveProduction(null);
        const countType = (working.dashboard && working.dashboard.default_count_type) || "roi";

        chip("Count type", countType === "line" ? "Counting line" : "Entry ROI zone", "neutral");
        chip("Cameras enabled", `${enabledCameras.length} of ${cameras.length}`, enabledCameras.length > 0 ? "ok" : "warn");
        chip("AI running on", `${aiCameras.length} camera${aiCameras.length === 1 ? "" : "s"}`, aiCameras.length > 0 ? "ok" : "off");
        chip("Default: counting", flags.count ? "On" : "Off", flags.count ? "ok" : "off");
        chip("Default: bags in total", flags.bags ? "Added" : "Not added", flags.bags ? "ok" : "off");
        chip("Default: prints in total", flags.prints ? "Added" : "Hidden", flags.prints ? "ok" : "off");

        const noTotals = enabledCameras.length > 0
            && enabledCameras.every(item => !settingsEffectiveProduction(item).bags);

        if (noTotals) {
            notes.push({ type: "warn", text: "No enabled camera adds its bags to the total, so the total bag count will stay at 0." });
        }
    }

    return { chips, notes };
}

function settingsRenderSummary() {

    const card = byId("settingsSummaryCard");

    if (!card || !settingsState.working) {
        return;
    }

    const { chips, notes } = settingsBuildSummary();

    card.innerHTML = `
        <div class="settings-chip-row">
            ${chips.map(item => `
                <div class="settings-chip ${escapeHtml(item.state)}">
                    <span>${escapeHtml(item.label)}</span>
                    <strong>${escapeHtml(item.value)}</strong>
                </div>
            `).join("")}
        </div>
        ${notes.map(note => `
            <div class="settings-summary-note settings-summary-note-${escapeHtml(note.type)}">${escapeHtml(note.text)}</div>
        `).join("")}
        <div class="settings-summary-legend">
            <span class="settings-badge settings-badge-live">LIVE</span>
            applies as soon as you save. Everything else needs a restart of the FillPac AI service.
        </div>
    `;
}

function settingsApplyDependencies() {

    const root = byId("settingsFormRoot");

    if (!root) {
        return;
    }

    const camera = settingsActiveCamera();

    root.querySelectorAll("[data-section]").forEach(card => {

        const section = card.dataset.section;
        const heading = card.querySelector(".settings-group-header h3");

        let reason = "";

        if (camera) {

            const flags = settingsEffectiveProduction(camera);

            if (SETTINGS_AI_SECTIONS.has(section)) {

                if (camera.enabled === false) {
                    reason = "Camera disabled";
                } else if (camera.ai_enabled === false) {
                    reason = "AI off";
                } else if (SETTINGS_COUNT_SECTIONS.has(section) && !flags.count) {
                    reason = "Counting off";
                }
            }
        }

        card.classList.toggle("settings-group-inactive", reason !== "");

        if (heading) {

            if (reason) {
                heading.dataset.reason = reason;
            } else {
                delete heading.dataset.reason;
            }
        }
    });
}

function settingsRefreshDerivedUi() {

    settingsRenderSummary();
    settingsApplyDependencies();
}

// Friendly name for a dotted diff path, used by the confirm dialog.
function settingsDescribePath(dottedPath) {

    const parts = String(dottedPath).split(".");
    const key = parts[parts.length - 1];
    const label = settingsLookup(SETTINGS_FIELD_LABELS, parts, key) || settingsPrettify(key);

    let scope = "Global";

    if (parts[0] === "cameras" && parts.length > 2) {

        const camera = (settingsState.working.cameras || [])[parseInt(parts[1], 10)];
        scope = (camera && camera.name) || `Camera ${parseInt(parts[1], 10) + 1}`;
    }

    return { label, scope };
}

// Looks `key` up in `table` using, in order: the full dotted path, the
// path relative to a camera (cameras.N. removed), then the plain key.
function settingsLookup(table, pathParts, key) {

    const parts = pathParts.map(String);
    const relative = (parts[0] === "cameras" && parts.length > 2)
        ? parts.slice(2)
        : parts;

    const candidates = [parts.join("."), relative.join("."), String(key)];

    for (const candidate of candidates) {

        if (Object.prototype.hasOwnProperty.call(table, candidate)) {
            return table[candidate];
        }
    }

    return undefined;
}

function settingsGroupMeta(key, pathParts) {

    return settingsLookup(SETTINGS_GROUP_META, pathParts, key) || {
        title: settingsPrettify(key),
        description: ""
    };
}


/* ----------------------------------------------------------
   SMALL HELPERS
   ---------------------------------------------------------- */

function settingsPrettify(key) {

    return String(key)
        .replaceAll("_", " ")
        .replace(/\b\w/g, character => character.toUpperCase());
}


function settingsIsPlainObject(value) {

    return (
        value !== null
        && typeof value === "object"
        && !Array.isArray(value)
    );
}


function settingsIsPrimitiveArray(value) {

    return (
        Array.isArray(value)
        && value.every(
            item =>
                item === null
                || typeof item === "string"
                || typeof item === "number"
                || typeof item === "boolean"
        )
    );
}


function settingsDeepClone(value) {

    return JSON.parse(JSON.stringify(value));
}


function settingsGetIn(root, pathParts) {

    let node = root;

    for (const part of pathParts) {

        if (node === null || node === undefined) {
            return undefined;
        }

        node = node[part];
    }

    return node;
}


function settingsSetIn(root, pathParts, value) {

    let node = root;

    for (let i = 0; i < pathParts.length - 1; i++) {

        const part = pathParts[i];

        if (node[part] === null || typeof node[part] !== "object") {
            node[part] = {};
        }

        node = node[part];
    }

    node[pathParts[pathParts.length - 1]] = value;
}


// Leaf-level diff between two config trees, mirroring the
// server's own ConfigManager.diff() so the confirmation modal
// shows exactly what /api/settings will report as changed.
function settingsComputeDiff(oldData, newData) {

    const changes = [];

    function walk(oldNode, newNode, path) {

        const oldIsDict = settingsIsPlainObject(oldNode);
        const newIsDict = settingsIsPlainObject(newNode);

        if (oldIsDict || newIsDict) {

            const oldMap = oldIsDict ? oldNode : {};
            const newMap = newIsDict ? newNode : {};

            const keys = Array.from(
                new Set([
                    ...Object.keys(oldMap),
                    ...Object.keys(newMap)
                ])
            ).sort();

            for (const key of keys) {
                walk(oldMap[key], newMap[key], [...path, key]);
            }

            return;
        }

        const oldIsList = Array.isArray(oldNode);
        const newIsList = Array.isArray(newNode);

        if (oldIsList || newIsList) {

            const oldList = oldIsList ? oldNode : [];
            const newList = newIsList ? newNode : [];
            const length = Math.max(oldList.length, newList.length);

            for (let i = 0; i < length; i++) {
                walk(oldList[i], newList[i], [...path, String(i)]);
            }

            return;
        }

        if (oldNode !== newNode) {
            changes.push({
                path: path.join("."),
                old: oldNode,
                new: newNode
            });
        }
    }

    walk(oldData || {}, newData || {}, []);

    return changes;
}


function settingsFormatValue(value) {

    if (value === null || value === undefined) {
        return "--";
    }

    if (typeof value === "object") {
        return JSON.stringify(value);
    }

    return String(value);
}


// Talks directly to the API (reusing the same auth token
// apiFetch() uses) but always returns the parsed JSON body
// alongside the response status, since the Settings page needs
// to show the server's actual "detail" / "error" message on a
// 400/403 -- apiFetch() only throws a generic Error on failure.
async function settingsApiFetch(path, options = {}) {

    const token =
        localStorage.getItem("fillpac_auth_token") ||
        sessionStorage.getItem("fillpac_auth_token");

    const headers = {
        "Content-Type": "application/json",
        ...(options.headers || {}),
        ...(token ? { "Authorization": `Bearer ${token}` } : {})
    };

    const response = await fetch(
        `${API_BASE}${path}`,
        {
            cache: "no-store",
            ...options,
            headers
        }
    );

    let body = null;

    try {
        body = await response.json();
    } catch (error) {
        body = null;
    }

    if (response.status === 401) {

        authRedirectInProgress = true;

        localStorage.removeItem("fillpac_auth_token");
        localStorage.removeItem("fillpac_user");
        sessionStorage.removeItem("fillpac_auth_token");
        sessionStorage.removeItem("fillpac_user");

        window.location.href =
            "/login?redirect=" + encodeURIComponent(window.location.href);
    }

    return {
        ok: response.ok,
        status: response.status,
        data: body
    };
}


function isSettingsAdmin() {

    return appState.userRole === "admin";
}


/* ==========================================================
   CREATE USER PAGE (admin only)
   ========================================================== */

function resetCreateUserForm() {

    const form = byId("createUserForm");
    const message = byId("createUserMessage");

    if (form) {
        form.reset();
    }

    if (message) {
        message.textContent = "";
        message.className = "create-user-message";
    }
}


function setCreateUserMessage(text, isError) {

    const message = byId("createUserMessage");

    if (!message) {
        return;
    }

    message.textContent = text;
    message.className =
        "create-user-message " +
        (isError ? "create-user-error" : "create-user-success");
}


function bindCreateUserForm() {

    const form = byId("createUserForm");

    if (!form || form.dataset.bound === "1") {
        return;
    }

    form.dataset.bound = "1";

    form.addEventListener("submit", async (event) => {

        event.preventDefault();

        if (!isSettingsAdmin()) {
            setCreateUserMessage("Administrator access required.", true);
            return;
        }

        const username = byId("newUsername").value.trim();
        const password = byId("newPassword").value;
        const role = byId("newUserRole").value;
        const submit = byId("createUserSubmit");

        if (submit) {
            submit.disabled = true;
        }

        try {

            const response = await settingsApiFetch("/api/users", {
                method: "POST",
                body: JSON.stringify({ username, password, role })
            });

            if (!response.ok) {
                setCreateUserMessage(
                    (response.data && response.data.detail) ||
                        "Could not create user.",
                    true
                );
                return;
            }

            form.reset();

            setCreateUserMessage(
                `User "${response.data.username}" (${response.data.role}) created successfully.`,
                false
            );

        } catch (error) {

            console.error("Create user failed:", error);
            setCreateUserMessage("Request failed. Please try again.", true);

        } finally {

            if (submit) {
                submit.disabled = false;
            }
        }
    });
}


function showSettingsBanner(type, message) {

    const banner = byId("settingsBanner");

    if (!banner) {
        return;
    }

    banner.className = `settings-banner settings-banner-${type}`;
    banner.textContent = message;
    banner.style.display = "block";
}


function hideSettingsBanner() {

    const banner = byId("settingsBanner");

    if (banner) {
        banner.style.display = "none";
    }
}


function updateSettingsStatusText() {

    const statusEl = byId("settingsStatusText");

    if (!statusEl) {
        return;
    }

    if (!isSettingsAdmin()) {
        statusEl.textContent = "View only -- an administrator account is required to change settings.";
        return;
    }

    const changeCount =
        settingsComputeDiff(settingsState.original, settingsState.working).length;

    statusEl.textContent =
        changeCount === 0
            ? "No unsaved changes"
            : `${changeCount} unsaved change${changeCount === 1 ? "" : "s"}`;
}


/* ----------------------------------------------------------
   LOAD
   ---------------------------------------------------------- */

async function loadSettings() {

    settingsState.loading = true;

    try {

        const response = await settingsApiFetch("/api/settings");

        if (!response.ok) {

            const message =
                (response.data && (response.data.detail || response.data.error))
                || `Failed to load settings (HTTP ${response.status}).`;

            renderSettingsError(message);

            return null;
        }

        const config = (response.data && response.data.config) || {};

        settingsState.original = settingsDeepClone(config);
        settingsState.working = settingsDeepClone(config);

        if (
            settingsState.activeTab !== "global"
            && !settingsState.activeTab.startsWith("camera-")
        ) {
            settingsState.activeTab = "global";
        }

        bindSettingsControls();
        renderSettingsTabs();
        renderSettingsActiveTab();
        applySettingsRoleGating();
        updateSettingsStatusText();
        hideSettingsBanner();

        return config;
    }

    catch (error) {

        console.error("Failed loading settings:", error);

        renderSettingsError("Failed to load settings: " + error.message);

        return null;
    }

    finally {

        settingsState.loading = false;
    }
}


function renderSettingsError(message) {

    const root = byId("settingsFormRoot");

    if (root) {
        root.innerHTML = `
            <div class="setting-row">
                <span>Status</span>
                <strong>${escapeHtml(message)}</strong>
            </div>
        `;
    }

    const tabs = byId("settingsTabs");
    if (tabs) {
        tabs.innerHTML = "";
    }

    showSettingsBanner("error", message);
}


/* ----------------------------------------------------------
   TABS
   ---------------------------------------------------------- */

function renderSettingsTabs() {

    const tabsContainer = byId("settingsTabs");

    if (!tabsContainer || !settingsState.working) {
        return;
    }

    const cameras = Array.isArray(settingsState.working.cameras)
        ? settingsState.working.cameras
        : [];

    const tabs = [{ id: "global", label: "Global" }];

    cameras.forEach((camera, index) => {
        tabs.push({
            id: `camera-${index}`,
            label: camera && camera.name ? camera.name : `Camera ${index + 1}`
        });
    });

    tabsContainer.innerHTML = tabs
        .map(
            tab => `
            <button
                type="button"
                class="settings-tab${tab.id === settingsState.activeTab ? " active" : ""}"
                data-tab-id="${escapeHtml(tab.id)}"
            >${escapeHtml(tab.label)}</button>
            `
        )
        .join("");

    tabsContainer.querySelectorAll(".settings-tab").forEach(button => {

        button.addEventListener("click", () => {

            settingsState.activeTab = button.dataset.tabId;

            renderSettingsTabs();
            renderSettingsActiveTab();
            applySettingsRoleGating();
        });
    });
}


/* ----------------------------------------------------------
   FORM RENDERING
   ---------------------------------------------------------- */

function renderSettingsActiveTab() {

    const root = byId("settingsFormRoot");

    if (!root || !settingsState.working) {
        return;
    }

    root.innerHTML = "";
    root.classList.toggle("settings-basic-mode", !settingsState.advanced);

    let dataObj;
    let pathPrefix;

    if (settingsState.activeTab === "global") {

        dataObj = settingsState.working;
        pathPrefix = [];

    } else {

        const index = parseInt(settingsState.activeTab.replace("camera-", ""), 10);
        dataObj = (settingsState.working.cameras || [])[index];
        pathPrefix = ["cameras", String(index)];
    }

    if (!dataObj) {

        root.innerHTML = `
            <div class="setting-row">
                <span>Status</span>
                <strong>No configuration found for this tab.</strong>
            </div>
        `;

        return;
    }

    const summaryCard = document.createElement("section");
    summaryCard.id = "settingsSummaryCard";
    summaryCard.className = "settings-group settings-summary";
    root.appendChild(summaryCard);

    const leafEntries = [];
    const dictEntries = [];

    Object.entries(dataObj).forEach(([key, value]) => {

        if (key === "cameras" && pathPrefix.length === 0) {
            return;
        }

        if (settingsIsPlainObject(value)) {
            dictEntries.push([key, value]);
        } else {
            leafEntries.push([key, value]);
        }
    });

    // "General" card: primitive fields living directly on this
    // tab's root object (e.g. camera.name, camera.enabled).
    if (leafEntries.length > 0) {

        const card = document.createElement("section");
        card.className = "settings-group";

        card.innerHTML = `
            <div class="settings-group-header">
                <h3>General</h3>
                <p>Top-level settings for this section</p>
            </div>
        `;

        const grid = document.createElement("div");
        grid.className = "settings-field-grid";

        leafEntries.forEach(([key, value]) => {
            grid.appendChild(
                buildSettingsFieldRow(
                    key,
                    value,
                    [...pathPrefix, key],
                    false,
                    key === "id"
                )
            );
        });

        card.appendChild(grid);
        root.appendChild(card);
    }

    const sectionRank = key => {

        const rank = SETTINGS_SECTION_ORDER.indexOf(key);
        return rank === -1 ? SETTINGS_SECTION_ORDER.length : rank;
    };

    dictEntries.sort((a, b) => sectionRank(a[0]) - sectionRank(b[0]));

    dictEntries.forEach(([key, value]) => {

        const advancedGroup = SETTINGS_ADVANCED_GROUPS.has(key);
        const meta = settingsGroupMeta(key, [...pathPrefix, key]);

        const card = document.createElement("section");
        card.className = "settings-group";
        card.dataset.section = key;

        if (advancedGroup) {
            card.classList.add("advanced-only-group");
        }

        card.innerHTML = `
            <div class="settings-group-header">
                <h3>${escapeHtml(meta.title)}</h3>
                ${meta.description ? `<p>${escapeHtml(meta.description)}</p>` : ""}
            </div>
        `;

        renderSettingsDictBody(
            card,
            value,
            [...pathPrefix, key],
            advancedGroup
        );

        root.appendChild(card);
    });

    // Camera tabs: sections this camera does not define are inherited
    // from the Global tab. Offer an explicit override instead of
    // silently hiding them (e.g. a camera's own spacing / occupancy ROI).
    if (pathPrefix.length > 0) {

        SETTINGS_CAMERA_OVERRIDABLE.forEach(key => {

            const globalSection = settingsState.working[key];

            if (
                settingsIsPlainObject(dataObj[key])
                || !settingsIsPlainObject(globalSection)
            ) {
                return;
            }

            const meta = settingsGroupMeta(key, [...pathPrefix, key]);

            const card = document.createElement("section");
            card.className = "settings-group settings-group-inherited";
            card.dataset.section = key;

            card.innerHTML = `
                <div class="settings-group-header">
                    <h3>${escapeHtml(meta.title)}</h3>
                    <p>Using the Global values. Add an override to give this camera its own values (for example its own ROI).</p>
                </div>
            `;

            const button = document.createElement("button");
            button.type = "button";
            button.className = "secondary-button";
            button.textContent = "Override for this camera";
            button.disabled = !isSettingsAdmin();

            button.addEventListener("click", () => {

                settingsSetIn(
                    settingsState.working,
                    [...pathPrefix, key],
                    settingsDeepClone(globalSection)
                );

                renderSettingsActiveTab();
                applySettingsRoleGating();
                updateSettingsStatusText();
            });

            card.appendChild(button);
            root.appendChild(card);
        });
    }

    settingsRefreshDerivedUi();
}


// Renders one dict's leaf fields into a field-grid, then any
// nested dicts as indented "subgroup" boxes (recursively), so
// arbitrarily nested config (camera.model.detection_roi, etc.)
// renders correctly without special-casing each shape.
function renderSettingsDictBody(container, dictObj, pathPrefix, advancedGroup) {

    const grid = document.createElement("div");
    grid.className = "settings-field-grid";

    const nestedDicts = [];

    Object.entries(dictObj).forEach(([key, value]) => {

        if (settingsIsPlainObject(value)) {
            nestedDicts.push([key, value]);
            return;
        }

        grid.appendChild(
            buildSettingsFieldRow(
                key,
                value,
                [...pathPrefix, key],
                advancedGroup,
                false
            )
        );
    });

    container.appendChild(grid);

    nestedDicts.forEach(([key, value]) => {

        const meta = settingsGroupMeta(key, [...pathPrefix, key]);

        const subgroup = document.createElement("div");
        subgroup.className = "settings-subgroup";

        if (advancedGroup || key === "calibration") {
            subgroup.classList.add("advanced-only");
        }

        subgroup.innerHTML = `<div class="settings-subgroup-title">${escapeHtml(meta.title)}</div>`
            + (meta.description
                ? `<div class="settings-subgroup-desc">${escapeHtml(meta.description)}</div>`
                : "");

        renderSettingsDictBody(
            subgroup,
            value,
            [...pathPrefix, key],
            advancedGroup || key === "calibration"
        );

        container.appendChild(subgroup);
    });
}


// Builds one <div class="settings-field"> row for a single leaf
// value (primitive, primitive-array, or complex-array/JSON).
function buildSettingsFieldRow(key, value, pathParts, advancedGroup, readOnly) {

    const row = document.createElement("div");
    row.className = "settings-field";
    row.dataset.path = JSON.stringify(pathParts);

    let forceAdvanced = false;
    let controlHtml = "";
    let kind = "string";

    if (typeof value === "boolean") {

        kind = "boolean";
        controlHtml = `
            <label class="settings-toggle-switch">
                <input
                    type="checkbox"
                    class="settings-checkbox"
                    ${value ? "checked" : ""}
                    ${readOnly ? "disabled" : ""}
                >
                <span class="settings-toggle-slider"></span>
            </label>
        `;

    } else if (typeof value === "number") {

        kind = "number";
        controlHtml = `
            <input
                type="number"
                step="any"
                class="settings-input"
                value="${escapeHtml(String(value))}"
                ${readOnly ? "readonly" : ""}
            >
        `;

    } else if (settingsIsPrimitiveArray(value)) {

        kind = "array-primitive";
        controlHtml = `
            <input
                type="text"
                class="settings-input"
                value="${escapeHtml(value.join(", "))}"
                placeholder="comma-separated"
                ${readOnly ? "readonly" : ""}
            >
        `;

    } else if (Array.isArray(value) || value === null) {

        kind = "json";
        forceAdvanced = true;
        row.classList.add("settings-field-json");
        controlHtml = `
            <textarea
                class="settings-input"
                spellcheck="false"
                ${readOnly ? "readonly" : ""}
            >${escapeHtml(JSON.stringify(value, null, 2))}</textarea>
        `;

    } else {

        const selectOptions = settingsLookup(SETTINGS_SELECT_OPTIONS, pathParts, key);

        if (Array.isArray(selectOptions)) {

            kind = "select";

            const options = selectOptions.includes(String(value))
                ? selectOptions
                : [...selectOptions, String(value)];

            controlHtml = `
                <select class="settings-select" ${readOnly ? "disabled" : ""}>
                    ${options.map(option => `
                        <option value="${escapeHtml(option)}"${option === String(value) ? " selected" : ""}>${escapeHtml(option)}</option>
                    `).join("")}
                </select>
            `;

        } else {

            kind = "string";
            controlHtml = `
                <input
                    type="text"
                    class="settings-input"
                    value="${escapeHtml(String(value))}"
                    ${readOnly ? "readonly" : ""}
                >
            `;
        }
    }

    row.dataset.kind = kind;

    if (advancedGroup || forceAdvanced) {
        row.classList.add("advanced-only");
    }

    const dottedPath = pathParts.join(".");
    const fieldLabel = settingsLookup(SETTINGS_FIELD_LABELS, pathParts, key) || settingsPrettify(key);
    const fieldHint = settingsLookup(SETTINGS_FIELD_HINTS, pathParts, key);

    const liveBadge = settingsIsLivePath(pathParts)
        ? `<span class="settings-badge settings-badge-live" title="Applied to the running service as soon as you save">LIVE</span>`
        : "";

    row.innerHTML = `
        <div class="settings-field-main">
            <span class="settings-field-label">${escapeHtml(fieldLabel)}${liveBadge}</span>
            ${controlHtml}
        </div>
        ${fieldHint ? `<small class="settings-field-hint">${escapeHtml(fieldHint)}</small>` : ""}
    `;

    if (!readOnly) {

        const control = row.querySelector("input, textarea, select");

        if (control) {

            const eventName = (kind === "boolean" || kind === "select") ? "change" : "input";

            control.addEventListener(eventName, () => {
                handleSettingsFieldChange(row, control, kind, pathParts);
            });
        }
    }

    return row;
}


function handleSettingsFieldChange(row, control, kind, pathParts) {

    let parsedValue;

    if (kind === "boolean") {

        parsedValue = control.checked;

    } else if (kind === "number") {

        const numeric = Number(control.value);

        if (control.value.trim() === "" || Number.isNaN(numeric)) {
            row.classList.add("settings-field-invalid");
            return;
        }

        parsedValue = numeric;

    } else if (kind === "array-primitive") {

        const originalValue = settingsGetIn(settingsState.original, pathParts);
        const sampleItem = Array.isArray(originalValue) ? originalValue[0] : undefined;

        parsedValue = control.value
            .split(",")
            .map(item => item.trim())
            .filter(item => item.length > 0)
            .map(item => {

                if (typeof sampleItem === "number") {
                    return Number(item);
                }

                if (typeof sampleItem === "boolean") {
                    return item.toLowerCase() === "true";
                }

                return item;
            });

    } else if (kind === "json") {

        try {
            parsedValue = control.value.trim() === "" ? null : JSON.parse(control.value);
        } catch (error) {
            row.classList.add("settings-field-invalid");
            return;
        }

    } else {

        parsedValue = control.value;
    }

    row.classList.remove("settings-field-invalid");

    settingsSetIn(settingsState.working, pathParts, parsedValue);

    const originalValue = settingsGetIn(settingsState.original, pathParts);
    const changed = JSON.stringify(originalValue) !== JSON.stringify(parsedValue);

    row.classList.toggle("settings-field-changed", changed);

    updateSettingsStatusText();
    settingsRefreshDerivedUi();
}


/* ----------------------------------------------------------
   ROLE GATING
   ---------------------------------------------------------- */

function applySettingsRoleGating() {

    const admin = isSettingsAdmin();

    const root = byId("settingsFormRoot");

    if (root && !admin) {

        // Operators can view every field, but none are editable
        // -- writes are enforced server-side too (require_admin
        // on PUT/validate/reload/reset), this just avoids a
        // confusing 403 after typing a change.
        root.querySelectorAll("input, textarea, select").forEach(control => {
            control.disabled = true;
        });
    }

    ["settingsSaveBtn", "settingsValidateBtn", "settingsResetBtn", "settingsTestDbBtn"].forEach(id => {

        const button = byId(id);

        if (!button) {
            return;
        }

        button.disabled = !admin;
        button.title = admin ? "" : "Administrator account required";
    });

    const advancedToggle = byId("settingsAdvancedToggle");
    if (advancedToggle) {
        advancedToggle.disabled = false; // viewing basic/advanced is fine for everyone
    }
}


/* ----------------------------------------------------------
   CONTROLS
   ---------------------------------------------------------- */

function bindSettingsControls() {

    if (settingsState.controlsBound) {
        return;
    }

    settingsState.controlsBound = true;

    const advancedToggle = byId("settingsAdvancedToggle");
    if (advancedToggle) {

        advancedToggle.addEventListener("change", () => {

            settingsState.advanced = advancedToggle.checked;

            const root = byId("settingsFormRoot");
            if (root) {
                root.classList.toggle("settings-basic-mode", !settingsState.advanced);
            }
        });
    }

    const testDbBtn = byId("settingsTestDbBtn");
    if (testDbBtn) {
        testDbBtn.addEventListener("click", handleSettingsTestDatabase);
    }

    const validateBtn = byId("settingsValidateBtn");
    if (validateBtn) {
        validateBtn.addEventListener("click", handleSettingsValidate);
    }

    const saveBtn = byId("settingsSaveBtn");
    if (saveBtn) {
        saveBtn.addEventListener("click", handleSettingsSaveClick);
    }

    const resetBtn = byId("settingsResetBtn");
    if (resetBtn) {
        resetBtn.addEventListener("click", handleSettingsResetClick);
    }

    const modalClose = byId("settingsConfirmClose");
    const modalCancel = byId("settingsConfirmCancel");
    const modalApply = byId("settingsConfirmApply");

    if (modalClose) {
        modalClose.addEventListener("click", closeSettingsConfirmModal);
    }

    if (modalCancel) {
        modalCancel.addEventListener("click", closeSettingsConfirmModal);
    }

    if (modalApply) {
        modalApply.addEventListener("click", handleSettingsConfirmApply);
    }
}


async function handleSettingsTestDatabase() {

    if (!isSettingsAdmin()) {
        return;
    }

    const database = settingsState.working && settingsState.working.database;

    if (!database) {
        showSettingsBanner("error", "config.yaml has no database section to test.");
        return;
    }

    const button = byId("settingsTestDbBtn");

    if (button) {
        button.disabled = true;
    }

    showSettingsBanner("info", "Testing database connection...");

    try {

        // Tests the values currently in the form, saved or not.
        const response = await settingsApiFetch(
            "/api/settings/database/test",
            {
                method: "POST",
                body: JSON.stringify(database)
            }
        );

        if (!response.ok) {

            showSettingsBanner(
                "error",
                (response.data && response.data.detail)
                || `Database test failed (HTTP ${response.status}).`
            );
            return;
        }

        showSettingsBanner(
            response.data && response.data.ok ? "success" : "error",
            (response.data && response.data.message) || "No response from server."
        );
    }

    catch (error) {
        showSettingsBanner("error", "Database test failed: " + error.message);
    }

    finally {

        if (button) {
            button.disabled = !isSettingsAdmin();
        }
    }
}


async function handleSettingsValidate() {

    if (!isSettingsAdmin()) {
        return;
    }

    hideSettingsBanner();
    showSettingsBanner("info", "Validating configuration...");

    try {

        const response = await settingsApiFetch(
            "/api/settings/validate",
            {
                method: "POST",
                body: JSON.stringify(settingsState.working)
            }
        );

        if (!response.ok) {

            const message =
                (response.data && (response.data.detail || response.data.error))
                || `Validation request failed (HTTP ${response.status}).`;

            showSettingsBanner("error", message);
            return;
        }

        if (response.data && response.data.valid) {
            showSettingsBanner("success", "Configuration is valid.");
        } else {
            showSettingsBanner(
                "error",
                (response.data && response.data.error) || "Configuration is invalid."
            );
        }
    }

    catch (error) {
        showSettingsBanner("error", "Validation failed: " + error.message);
    }
}


function handleSettingsSaveClick() {

    if (!isSettingsAdmin()) {
        return;
    }

    const changes = settingsComputeDiff(settingsState.original, settingsState.working);

    if (changes.length === 0) {
        showSettingsBanner("info", "No changes to save.");
        return;
    }

    openSettingsConfirmModal(changes);
}


function openSettingsConfirmModal(changes) {

    const modal = byId("settingsConfirmModal");
    const list = byId("settingsConfirmList");

    if (!modal || !list) {
        return;
    }

    if (changes.length === 0) {

        list.innerHTML = `<div class="settings-confirm-empty">No changes</div>`;

    } else {

        list.innerHTML = changes
            .map(
                change => `
                <div class="settings-confirm-row">
                    <div class="settings-confirm-path">
                        <strong>${escapeHtml(settingsDescribePath(change.path).label)}</strong>
                        <span class="settings-confirm-scope">${escapeHtml(settingsDescribePath(change.path).scope)}</span>
                        ${settingsIsLivePath(change.path.split("."))
                            ? `<span class="settings-badge settings-badge-live">LIVE</span>`
                            : `<span class="settings-badge settings-badge-muted">RESTART</span>`}
                        <small>${escapeHtml(change.path)}</small>
                    </div>
                    <div class="settings-confirm-values">
                        <span class="settings-confirm-old">${escapeHtml(settingsFormatValue(change.old))}</span>
                        <span>&rarr;</span>
                        <span class="settings-confirm-new">${escapeHtml(settingsFormatValue(change.new))}</span>
                    </div>
                </div>
                `
            )
            .join("");
    }

    modal.style.display = "flex";
}


function closeSettingsConfirmModal() {

    const modal = byId("settingsConfirmModal");

    if (modal) {
        modal.style.display = "none";
    }
}


async function handleSettingsConfirmApply() {

    if (!isSettingsAdmin()) {
        closeSettingsConfirmModal();
        return;
    }

    const applyBtn = byId("settingsConfirmApply");

    if (applyBtn) {
        applyBtn.disabled = true;
    }

    try {

        const response = await settingsApiFetch(
            "/api/settings",
            {
                method: "PUT",
                body: JSON.stringify(settingsState.working)
            }
        );

        if (!response.ok) {

            const message =
                (response.data && response.data.detail)
                || `Save failed (HTTP ${response.status}).`;

            closeSettingsConfirmModal();
            showSettingsBanner("error", message);
            return;
        }

        settingsState.original = settingsDeepClone(settingsState.working);

        closeSettingsConfirmModal();
        renderSettingsTabs();
        renderSettingsActiveTab();
        applySettingsRoleGating();
        updateSettingsStatusText();

        let message =
            (response.data && response.data.message)
            || "Settings saved.";

        if (response.data && response.data.comments_preserved === false) {
            message += " Note: comments in config.yaml were not kept. "
                + "Install ruamel.yaml on the server (pip install ruamel.yaml) to keep them.";
        }

        showSettingsBanner("success", message);
    }

    catch (error) {

        closeSettingsConfirmModal();
        showSettingsBanner("error", "Save failed: " + error.message);
    }

    finally {

        if (applyBtn) {
            applyBtn.disabled = false;
        }
    }
}


async function handleSettingsResetClick() {

    if (!isSettingsAdmin()) {
        return;
    }

    const confirmed = window.confirm(
        "This restores config.yaml from the most recent backup, " +
        "discarding any changes made since then. Continue?"
    );

    if (!confirmed) {
        return;
    }

    hideSettingsBanner();
    showSettingsBanner("info", "Restoring configuration from backup...");

    try {

        const response = await settingsApiFetch(
            "/api/settings/reset",
            { method: "POST" }
        );

        if (!response.ok) {

            const message =
                (response.data && response.data.detail)
                || `Reset failed (HTTP ${response.status}).`;

            showSettingsBanner("error", message);
            return;
        }

        await loadSettings();

        showSettingsBanner("success", "Configuration restored from the most recent backup.");
    }

    catch (error) {
        showSettingsBanner("error", "Reset failed: " + error.message);
    }
}


/* ==========================================================
   CAMERA MANAGEMENT
   ========================================================== */

function renderCameraManagement(data) {

    const grid =
        byId(
            "cameraManagementGrid"
        );


    if (!grid) {
        return;
    }


    const cameras =
        data?.cameras
        ??
        data
        ??
        {};


    const cards = [];


    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const camera =
            findCameraByIndex(
                cameras,
                index
            );


        const status =
            String(
                camera?.status
                ??
                "offline"
            ).toLowerCase();


        const mode =
            camera?.mode
            ??
            camera?.source_type
            ??
            "--";


        const fps =
            formatDecimal(
                camera?.fps,
                1
            );


        const jamStatus =
            normalizeJamStatus(
                camera ?? {}
            );


        const printDetection =
            camera?.print_detection_enabled
                ? "Enabled"
                : "Disabled";


        cards.push(
            `
            <article class="management-camera-card ${status}">

                <div class="management-camera-header">

                    <div class="management-camera-title">

                        <div class="camera-management-icon">
                            <svg class="icon-svg" aria-hidden="true"><use href="#icon-camera" xlink:href="#icon-camera"></use></svg>
                        </div>

                        <div>
                            <h3>${escapeHtml(
                                camera?.name
                                ??
                                `Camera ${index}`
                            )}</h3>
                            <p>${escapeHtml(
                                status.toUpperCase()
                            )}</p>
                        </div>

                    </div>

                </div>

                <div class="management-camera-details">

                    <div class="management-detail">
                        <span>Mode</span>
                        <strong>${escapeHtml(
                            String(mode)
                        )}</strong>
                    </div>

                    <div class="management-detail">
                        <span>FPS</span>
                        <strong>${fps}</strong>
                    </div>

                    <div class="management-detail">
                        <span>Jam Status</span>
                        <strong>${escapeHtml(
                            jamStatus.toUpperCase()
                        )}</strong>
                    </div>

                    <div class="management-detail">
                        <span>Print Detection</span>
                        <strong>${printDetection}</strong>
                    </div>

                </div>

            </article>
            `
        );
    }


    grid.innerHTML =
        cards.length > 0
            ? cards.join("")
            : `
                <article class="management-camera-card offline">
                    <div>
                        <h3>No cameras configured</h3>
                        <p>Waiting for backend</p>
                    </div>
                </article>
              `;
}


/* ==========================================================
   CAMERA CONTROLS
   ========================================================== */

function initializeCameraControls() {

    const refreshButton =
        byId(
            "refreshCamerasButton"
        );


    if (!refreshButton) {
        return;
    }


    refreshButton.addEventListener(
        "click",
        async () => {

            refreshButton.disabled =
                true;


            try {

                await loadCameraConfig();

                await loadDashboardState();

            }

            catch (error) {

                console.error(
                    "Camera refresh failed:",
                    error
                );

            }

            finally {

                refreshButton.disabled =
                    false;
            }
        }
    );
}


/* ==========================================================
   JAM TRACK HELPERS
   ========================================================== */

function getJamTracks(camera) {

    if (
        !camera
        ||
        typeof camera !== "object"
    ) {

        return [];
    }


    const tracks =
        camera.jam_tracks
        ??
        camera.tracks
        ??
        [];


    return Array.isArray(tracks)
        ? tracks
        : [];
}


function getJamTrackSpeed(track) {

    if (!track) {
        return 0;
    }


    return safeNumber(
        track.speed_px_s
        ??
        track.speed
        ??
        track.motion_speed
        ??
        0,
        0
    );
}


function getJamTrackDistance(track) {

    if (!track) {
        return 0;
    }


    return safeNumber(
        track.distance_px
        ??
        track.movement_distance
        ??
        track.distance
        ??
        0,
        0
    );
}


function getJamTrackStationaryTime(track) {

    if (!track) {
        return 0;
    }


    return safeNumber(
        track.stationary_seconds
        ??
        track.stationary_time
        ??
        track.time_stationary
        ??
        0,
        0
    );
}


function getJamTrackState(track) {

    if (!track) {
        return "unknown";
    }


    return String(
        track.status
        ??
        track.state
        ??
        "unknown"
    )
        .trim()
        .toLowerCase();
}


function getJamTrackId(track) {

    if (!track) {
        return "--";
    }


    return (
        track.track_id
        ??
        track.id
        ??
        "--"
    );
}


/* ==========================================================
   JAM CAMERA SUMMARY
   ========================================================== */

function getJamCameraSummary(camera) {

    if (!camera) {

        return {

            enabled: false,

            status: "disabled",

            activeCount: 0,

            trackIds: [],

            tracks: []
        };
    }


    const enabled =
        Boolean(
            camera.jam_detection_enabled
        );


    const status =
        normalizeJamStatus(
            camera
        );


    const trackIds =
        Array.isArray(
            camera.active_jam_track_ids
        )
            ? camera.active_jam_track_ids
            : [];


    const activeCount =
        Math.max(
            0,
            Math.round(
                safeNumber(
                    camera.active_jam_count,
                    0
                )
            )
        );


    return {

        enabled,

        status,

        activeCount,

        trackIds,

        tracks:
            getJamTracks(
                camera
            )
    };
}


/* ==========================================================
   JAM SYSTEM SUMMARY
   ========================================================== */

function getJamSystemSummary(cameras) {

    const summary = {

        enabledCameras: 0,

        normal: 0,

        slow: 0,

        warning: 0,

        jam: 0,

        recovering: 0,

        disabled: 0,

        activeJams: 0
    };


    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const camera =
            findCameraByIndex(
                cameras,
                index
            );


        const cameraSummary =
            getJamCameraSummary(
                camera
            );


        if (!cameraSummary.enabled) {

            summary.disabled += 1;

            continue;
        }


        summary.enabledCameras += 1;


        if (
            Object.prototype
                .hasOwnProperty
                .call(
                    summary,
                    cameraSummary.status
                )
        ) {

            summary[
                cameraSummary.status
            ] += 1;
        }


        if (
            cameraSummary.status
            === "jam"
        ) {

            summary.activeJams +=
                cameraSummary.activeCount > 0
                    ? cameraSummary.activeCount
                    : 1;
        }
    }


    return summary;
}


/* ==========================================================
   SYSTEM STATUS PAGE
   ========================================================== */

function renderSystemStatus(state) {

    if (!state || typeof state !== "object") {
        return;
    }

    // Update camera status
    renderCameraStatusList(state.cameras || {});

    // Update system health indicators (scoped to the selected packer)
    updateSystemHealthDisplay(state);


    /* ------------------------------------------------------
       PACKER RUNNING STATUS (selected packer, 1-5)
       ------------------------------------------------------ */

    const selectedIndex =
        appState.selectedPacker || 1;

    const subtitleElement =
        byId("packerSelectorSubtitle");

    if (subtitleElement) {

        subtitleElement.textContent =
            `Showing: Packer ${selectedIndex}`;
    }

    const packer =
        findPackerByIndex(
            state,
            selectedIndex
        );

    const packerStatusElement =
        byId("packerRunningStatus");

    if (!packer) {

        // Packer has no data yet (backend hasn't reported it) -
        // show clearly rather than defaulting to a misleading
        // RUNNING/STOPPED value.

        setText(
            "packerRunningStatus",
            "NOT CONFIGURED"
        );

        if (packerStatusElement) {

            packerStatusElement.classList.remove(
                "status-value-running",
                "status-value-stopped"
            );
        }

        setText("packerPlcStatus", "--");
        setText("packerPmsStatus", "--");
        setText("packerDcsStatus", "--");

        updateNotificationLiveStatus(
            selectedIndex,
            null
        );

        return;
    }

    const packerRunning =
        packerIsRunning(packer);

    setText(
        "packerRunningStatus",
        packerRunning ? "RUNNING" : "STOPPED"
    );

    if (packerStatusElement) {

        packerStatusElement.classList.toggle(
            "status-value-running",
            packerRunning
        );

        packerStatusElement.classList.toggle(
            "status-value-stopped",
            !packerRunning
        );
    }

    setText(
        "packerPlcStatus",
        normalizeBooleanStatus(packer.plc_status)
            ? "Online"
            : "Offline"
    );

    setText(
        "packerPmsStatus",
        normalizeBooleanStatus(packer.pms_status)
            ? "Online"
            : "Offline"
    );

    setText(
        "packerDcsStatus",
        normalizeBooleanStatus(packer.dcs_status)
            ? "Online"
            : "Offline"
    );

    updateNotificationLiveStatus(
        selectedIndex,
        packerRunning
    );
}


function renderCameraStatusList(cameras) {

    const container = byId("cameraStatusList");

    if (!container) {
        return;
    }

    if (!cameras || Object.keys(cameras).length === 0) {
        container.innerHTML = "<p>No cameras found</p>";
        return;
    }

    const entries = [];

    for (let index = 1; index <= CAMERA_COUNT; index += 1) {

        const cameraData = findCameraByIndex(cameras, index);

        if (!cameraData) {
            continue;
        }

        entries.push([`camera_${index - 1}`, cameraData, index]);
    }

    const items = entries.map(([cameraId, cameraData, cameraNum]) => {
        const online = Boolean(cameraData.online ?? cameraIsOnline(cameraData));
        const status = online ? "ONLINE" : "OFFLINE";
        const statusClass = online ? "status-online" : "status-offline";
        const fps = safeNumber(cameraData.fps, 0).toFixed(1);

        /*
        Jam status is preserved here (rather than dropped along
        with the old standalone Jam Monitoring tab) since it is
        real operator-relevant camera health information.
        */
        const jamStatus = normalizeJamStatus(cameraData);

        return `
            <div class="status-item ${statusClass}">
                <div class="status-header">
                    <span class="status-name">Camera ${cameraNum}</span>
                    <span class="status-badge">${status}</span>
                </div>
                <div class="status-details">
                    <span class="detail">Printed: <strong>${formatInteger(cameraData.printed_count ?? cameraData.printed_bags_count ?? 0)}</strong></span>
                    <span class="detail">Missing: <strong>${formatInteger(cameraData.missing_count ?? cameraData.not_printed_bags_count ?? 0)}</strong></span>
                    <span class="detail">FPS: <strong>${fps}</strong></span>
                    <span class="detail">Jam: <strong class="jam-status ${jamStatus}">${jamStatus.toUpperCase()}</strong></span>
                </div>
                <button class="camera-view-btn" onclick="showCameraView('${cameraId}')" type="button">
                    View Camera
                </button>
            </div>
        `;
    }).join("");

    container.innerHTML = items;
}


function updateSystemHealthDisplay(state) {

    /*
    Camera System Health

    Bug fix: this used to count active cameras via
    `Object.values(state.cameras).filter(c => c.online)`, which
    only ever checked the raw `online` boolean and could miss
    cameras whose backend payload instead reports status via a
    `status` string ("online"/"running"/"active"). That silently
    undercounted active cameras here even though the same camera
    showed correctly as ONLINE on its Camera Production Overview
    card elsewhere on the dashboard.

    Now this walks the same 4 camera slots via findCameraByIndex()
    and checks each one with the shared cameraIsOnline() helper,
    so the count shown here can never disagree with what the
    camera cards show.
    */

    const cameras =
        state.cameras || {};

    let camerasOnline = 0;

    let totalCameras = 0;

    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const camera =
            findCameraByIndex(cameras, index);

        if (!camera) {
            continue;
        }

        totalCameras += 1;

        if (
            cameraIsOnline(camera)
            ||
            Boolean(camera.online)
        ) {

            camerasOnline += 1;
        }
    }

    const cameraHealth =
        totalCameras === 0 || camerasOnline === totalCameras
            ? "Healthy"
            : camerasOnline > 0
                ? "Warning"
                : "Critical";

    const cameraIndicator = byId("cameraStatusIndicator");
    if (cameraIndicator) {
        cameraIndicator.className = `status-indicator ${cameraHealth.toLowerCase()}`;
        cameraIndicator.textContent = cameraHealth === "Healthy" ? "●" : cameraHealth === "Warning" ? "⚠" : "●";
    }
    setText("cameraStatusDetail", `${camerasOnline} of ${totalCameras} cameras online`);


    /*
    Packer PLC / PMS / DCS Health

    Scoped to whichever packer is currently selected on the
    System Status page (see the Packer 1-5 selector), falling
    back to Packer 1 by default. Status comparisons go through
    normalizeBooleanStatus() so "online"/"ONLINE"/"running"/
    true/1/etc are all recognised, instead of only the exact
    lowercase string "online".
    */

    const packer =
        findPackerByIndex(
            state,
            appState.selectedPacker || 1
        );

    const plcOnline =
        normalizeBooleanStatus(packer?.plc_status);

    const plcIndicator = byId("plcStatusIndicator");
    if (plcIndicator) {
        plcIndicator.className = `status-indicator ${plcOnline ? "healthy" : "critical"}`;
    }
    setText("plcStatusDetail", plcOnline ? "Online" : "Offline");

    const pmsOnline =
        normalizeBooleanStatus(packer?.pms_status);

    const pmsIndicator = byId("pmsStatusIndicator");
    if (pmsIndicator) {
        pmsIndicator.className = `status-indicator ${pmsOnline ? "healthy" : "critical"}`;
    }
    setText("pmsStatusDetail", pmsOnline ? "Online" : "Offline");

    const dcsOnline =
        normalizeBooleanStatus(packer?.dcs_status);

    const dcsIndicator = byId("dcsStatusIndicator");
    if (dcsIndicator) {
        dcsIndicator.className = `status-indicator ${dcsOnline ? "healthy" : "critical"}`;
    }
    setText("dcsStatusDetail", dcsOnline ? "Online" : "Offline");
}


/* ==========================================================
   REPORTS PAGE

   FIXED: reports are generated entirely client-side using
   jsPDF + jsPDF-AutoTable (both already loaded in index.html)
   from data already sitting in appState, instead of calling a
   nonexistent POST /reports/generate backend endpoint. That
   endpoint never existed, so every click was guaranteed to hit
   the catch block and fire a "Report Failed" notification.

   Also fixed: the Generate button's click listener is now only
   attached once (see dataset.listenerAttached guard below).
   Previously renderReportsPage() called addEventListener() on
   every visit to the Reports page with nothing to prevent
   duplicates, so visiting the page N times meant N stacked
   listeners on the same button - a single click then fired
   handleGenerateReport() N times, producing exactly the
   multiple near-simultaneous "Report Failed" toasts seen in
   the screenshot.

   Also fixed: the "Recent Reports" list is no longer hard-coded
   to "No reports generated yet" - it now reflects
   appState.reports so successfully generated reports actually
   show up and can be identified later.
   ========================================================== */

function renderReportsPage() {

    renderRecentReportsList();

    const generateBtn = byId("generateReportBtn");

    if (generateBtn && !generateBtn.dataset.listenerAttached) {

        generateBtn.addEventListener("click", handleGenerateReport);

        generateBtn.dataset.listenerAttached = "true";
    }

    // Set today's date as default (only if not already set, so
    // navigating away and back doesn't clobber a date the
    // operator already picked).
    const dateInput = byId("reportDate");
    if (dateInput && !dateInput.value) {
        const today = new Date().toISOString().split('T')[0];
        dateInput.value = today;
    }

    // Set default times
    const startTimeInput = byId("reportStartTime");
    const endTimeInput = byId("reportEndTime");
    if (startTimeInput && !startTimeInput.value) {
        startTimeInput.value = "00:00";
    }
    if (endTimeInput && !endTimeInput.value) {
        endTimeInput.value = "23:59";
    }
}


function renderRecentReportsList() {

    const container = byId("recentReportsList");

    if (!container) {
        return;
    }

    if (!appState.reports || appState.reports.length === 0) {

        container.innerHTML =
            "<p class=\"no-reports\">No reports generated yet</p>";

        return;
    }

    container.innerHTML =
        appState.reports
            .map(
                report => `
                <div class="report-list-item">
                    <strong>${escapeHtml(report.title)}</strong>
                    <p>${escapeHtml(report.date)} · ${
                        report.camera === "all"
                            ? "All Cameras"
                            : escapeHtml(report.camera)
                    } · ${escapeHtml(formatDateTime(report.generatedAt))}</p>
                </div>
                `
            )
            .join("");
}


async function handleGenerateReport() {

    const reportType = byId("reportType")?.value || "production";
    const reportDate = byId("reportDate")?.value || new Date().toISOString().split('T')[0];
    const startTime = byId("reportStartTime")?.value || "00:00";
    const endTime = byId("reportEndTime")?.value || "23:59";
    const camera = byId("reportCamera")?.value || "all";

    const generateBtn = byId("generateReportBtn");

    if (generateBtn) {
        generateBtn.disabled = true;
        generateBtn.textContent = "Generating...";
    }

    try {

        // Query SQL Server for the chosen period (local date/time ->
        // UTC ISO) instead of printing whatever is in live state.
        const startIso =
            new Date(`${reportDate}T${startTime}:00`).toISOString();

        const endIso =
            new Date(`${reportDate}T${endTime}:59`).toISOString();

        const reportData =
            await apiFetch(
                `/api/report-data?start=${encodeURIComponent(startIso)}`
                + `&end=${encodeURIComponent(endIso)}`
            );

        const doc =
            buildReportPdf({
                reportType,
                reportDate,
                startTime,
                endTime,
                camera,
                reportData
            });

        const label =
            REPORT_TYPE_LABELS[reportType]
            ||
            "Report";

        const filename =
            `fillpac-${reportType}-${reportDate}-${Date.now()}.pdf`;

        doc.save(filename);

        appState.reports.unshift({
            id: `report-${Date.now()}`,
            title: label,
            date: reportDate,
            camera,
            generatedAt: new Date(),
            filename
        });

        // Keep only the last 20 reports in the session list.
        if (appState.reports.length > 20) {
            appState.reports.pop();
        }

        renderRecentReportsList();

        addNotification(
            "Report Generated",
            `${label} created successfully`,
            "success"
        );

    }

    catch (error) {

        console.error("Failed to generate report:", error);

        addNotification(
            "Report Failed",
            "Failed to generate report. Please check your inputs.",
            "danger"
        );

    }

    finally {

        if (generateBtn) {
            generateBtn.disabled = false;
            generateBtn.textContent = "Generate Report";
        }
    }
}


/* ==========================================================
   REPORT PDF BUILDER (client-side)

   Builds a jsPDF document for the requested report type using
   data already loaded into appState (production, dashboard
   state/cameras, packer status, notifications). Never invents
   numbers: each section reads the same fields the rest of the
   dashboard already renders and trusts.
   ========================================================== */

function buildReportPdf({
    reportType,
    reportDate,
    startTime,
    endTime,
    camera,
    reportData
}) {

    const jsPDFCtor =
        window.jspdf
        &&
        window.jspdf.jsPDF;

    if (!jsPDFCtor) {

        throw new Error(
            "jsPDF library not loaded"
        );
    }

    const doc =
        new jsPDFCtor({
            unit: "pt",
            format: "a4"
        });

    const label =
        REPORT_TYPE_LABELS[reportType]
        ||
        "Report";

    doc.setFontSize(18);
    doc.text("FillPac Vision Intelligence Platform", 40, 50);

    doc.setFontSize(13);
    doc.setTextColor(80);
    doc.text(label, 40, 72);

    doc.setFontSize(10);
    doc.setTextColor(120);

    doc.text(
        `Date: ${reportDate}   Time: ${startTime} - ${endTime}   Camera: ${
            camera === "all" ? "All Cameras" : camera
        }`,
        40,
        90
    );

    doc.text(
        `Generated: ${new Date().toLocaleString()}`,
        40,
        104
    );

    let cursorY = 130;

    switch (reportType) {

        case "production":
            cursorY = addProductionReportSection(doc, cursorY, reportData, camera);
            break;

        case "print_detection":
            cursorY = addPrintDetectionReportSection(doc, cursorY, reportData, camera);
            break;

        case "camera_performance":
            cursorY = addCameraPerformanceReportSection(doc, cursorY, camera);
            break;

        case "system_status":
            cursorY = addSystemStatusReportSection(doc, cursorY);
            break;

        case "exception":
            cursorY = addExceptionReportSection(doc, cursorY);
            break;

        default:
            doc.text(
                "No data available for this report type.",
                40,
                cursorY
            );
    }

    void cursorY;

    return doc;
}


function reportCameraRows(reportData, cameraFilter) {

    const norm =
        value =>
            String(value || "")
                .replace(/[\s_]/g, "")
                .toLowerCase();

    return Object.entries(reportData?.cameras || {})
        .filter(
            ([name]) =>
                cameraFilter === "all"
                ||
                norm(name) === norm(cameraFilter)
        )
        .sort(([a], [b]) => a.localeCompare(b));
}


function addProductionReportSection(doc, startY, reportData, cameraFilter) {

    const rows = reportCameraRows(reportData, cameraFilter);

    const sum =
        key =>
            rows.reduce(
                (acc, [, values]) => acc + (values[key] || 0),
                0
            );

    const total = sum("total");
    const printed = sum("printed");
    const missing = sum("missing");

    doc.autoTable({
        startY,
        head: [["Metric", "Value"]],
        body: [
            ["Total Bags", formatInteger(total)],
            ["Printed Bags", formatInteger(printed)],
            ["Not Printed", formatInteger(missing)],
            [
                "Print Quality",
                printed + missing > 0
                    ? formatPercent((printed / (printed + missing)) * 100)
                    : "--"
            ]
        ],
        theme: "grid",
        headStyles: { fillColor: [0, 63, 70] }
    });

    return doc.lastAutoTable.finalY + 24;
}


function addPrintDetectionReportSection(doc, startY, reportData, cameraFilter) {

    const body =
        reportCameraRows(reportData, cameraFilter)
            .map(([name, values]) => {

                const inspected =
                    (values.printed || 0) + (values.missing || 0);

                return [
                    name,
                    formatInteger(values.printed || 0),
                    formatInteger(values.missing || 0),
                    inspected > 0
                        ? formatPercent((values.printed / inspected) * 100)
                        : "--"
                ];
            });

    doc.autoTable({
        startY,
        head: [["Camera", "Printed", "Missing", "Print Rate"]],
        body:
            body.length
                ? body
                : [["No data in this period", "", "", ""]],
        theme: "grid",
        headStyles: { fillColor: [0, 63, 70] }
    });

    return doc.lastAutoTable.finalY + 24;
}


function addCameraPerformanceReportSection(doc, startY, cameraFilter) {

    const cameras =
        appState.dashboardState?.cameras
        ||
        {};

    const rows = [];

    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        if (
            cameraFilter !== "all"
            &&
            cameraFilter !== `Camera ${index}`
        ) {
            continue;
        }

        const cam =
            findCameraByIndex(cameras, index);

        if (!cam) {
            continue;
        }

        rows.push([
            `Camera ${index}`,
            String(cam.status ?? "offline").toUpperCase(),
            formatDecimal(cam.fps, 1),
            formatInteger(cam.count ?? cam.total_count ?? 0),
            normalizeJamStatus(cam).toUpperCase()
        ]);
    }

    doc.autoTable({
        startY,
        head: [["Camera", "Status", "FPS", "Count", "Jam Status"]],
        body:
            rows.length
                ? rows
                : [["No camera data available", "", "", "", ""]],
        theme: "grid",
        headStyles: { fillColor: [0, 63, 70] }
    });

    return doc.lastAutoTable.finalY + 24;
}


function addSystemStatusReportSection(doc, startY) {

    const state =
        appState.dashboardState
        ||
        {};

    const packer =
        findPackerByIndex(state, 1);

    const rows = [
        ["System Status", String(state.system_status ?? "offline").toUpperCase()],
        ["System Uptime", formatUptime(state.uptime_seconds)],
        ["Packer 1 Running", packerIsRunning(packer) ? "RUNNING" : "STOPPED"],
        ["Packer 1 PLC", normalizeBooleanStatus(packer?.plc_status) ? "Online" : "Offline"],
        ["Packer 1 PMS", normalizeBooleanStatus(packer?.pms_status) ? "Online" : "Offline"],
        ["Packer 1 DCS", normalizeBooleanStatus(packer?.dcs_status) ? "Online" : "Offline"]
    ];

    doc.autoTable({
        startY,
        head: [["Component", "Status"]],
        body: rows,
        theme: "grid",
        headStyles: { fillColor: [0, 63, 70] }
    });

    return doc.lastAutoTable.finalY + 24;
}


function addExceptionReportSection(doc, startY) {

    const notifications =
        (appState.notifications || [])
            .filter(
                n =>
                    n.severity === "danger"
                    ||
                    n.severity === "warning"
            );

    const rows =
        notifications.map(
            n => [
                formatDateTime(n.timestamp),
                n.severity.toUpperCase(),
                n.title,
                n.message
            ]
        );

    doc.autoTable({
        startY,
        head: [["Time", "Severity", "Title", "Message"]],
        body:
            rows.length
                ? rows
                : [["--", "--", "No exceptions recorded", "--"]],
        theme: "grid",
        headStyles: { fillColor: [0, 63, 70] },
        columnStyles: { 3: { cellWidth: 220 } }
    });

    return doc.lastAutoTable.finalY + 24;
}


/* ==========================================================
   ABOUT PAGE
   ================================================== */

function renderAboutPage() {

    // Version info
    setText("aboutVersion", "BGCNT_01_01_01");
    setText("aboutFrontend", "BGCNT_01_01_01");
    setText("currentVersion", "BGCNT_01_01_01");
    setText("updateStatus", "Up to date");

    // Try to fetch additional version info
    try {
        apiFetch("/version").then(data => {
            if (data) {
                if (data.backend) setText("aboutBackend", data.backend);
                if (data.ai_model) setText("aboutAIModel", data.ai_model);
                if (data.build) setText("aboutBuild", data.build);
            }
        }).catch(e => {
            console.log("Version endpoint not available");
        });
    } catch (e) {
        // Version endpoint may not exist
    }
}


/* ==========================================================
   PRODUCTION STATISTICS TIME RANGE FILTER
   ========================================================== */

function initializeProductionTrendRangeButtons() {

    const container =
        byId("productionTrendRangeButtons");

    if (!container) {
        return;
    }

    const buttons =
        container.querySelectorAll(
            ".time-range-button"
        );

    buttons.forEach(
        button => {

            button.addEventListener(
                "click",
                () => {

                    const hours =
                        safeNumber(
                            button.dataset.hours,
                            8
                        );

                    appState.productionTrendRangeHours =
                        hours;

                    buttons.forEach(
                        other => {

                            other.classList.toggle(
                                "active",
                                other === button
                            );
                        }
                    );

                    /*
                    Re-render immediately from already-loaded
                    analytics data rather than re-fetching, so
                    the chart updates without a page reload.
                    */

                    updateProductionTrendChart(
                        appState.analytics?.hourly || []
                    );
                }
            );
        }
    );
}


/* ==========================================================
   DASHBOARD KPI TIME RANGE FILTER (LIVE / 1 / 4 / 8 / 16 / 24 HR)

   Reuses the same hourly buckets already returned by /analytics
   (data.hourly, each row shaped { hour, total, printed, missing })
   that power the Production Statistics chart, so no backend
   changes are needed. "LIVE" restores the all-time cumulative
   totals from the live dashboard state; the other buttons sum
   the most recent N hourly buckets instead.
   ========================================================== */

function initializeKpiRangeButtons() {

    const container =
        byId("kpiRangeButtons");

    if (!container) {
        return;
    }

    const buttons =
        container.querySelectorAll(
            ".time-range-button"
        );

    buttons.forEach(
        button => {

            button.addEventListener(
                "click",
                () => {

                    const hours =
                        safeNumber(
                            button.dataset.hours,
                            0
                        );

                    appState.kpiRangeHours =
                        hours;

                    buttons.forEach(
                        other => {

                            other.classList.toggle(
                                "active",
                                other === button
                            );
                        }
                    );

                    applyKpiRangeFilter(hours);
                }
            );
        }
    );
}


/*
Re-applies the currently active KPI range filter (if any) after
a live data refresh. The periodic refresh loop calls this right
after loadDashboardState()/loadProduction()/loadAnalytics(),
since those calls re-render the KPI cards from live totals and
would otherwise silently wipe out a selected 1/4/8/16/24 HR
filter within one refresh cycle (5s).
*/

function reapplyActiveKpiRangeFilter() {

    if (appState.kpiRangeHours) {

        applyKpiRangeFilter(
            appState.kpiRangeHours
        );
    }
}


function applyKpiRangeFilter(hours) {

    const label =
        byId("kpiRangeLabel");

    /*
    hours === 0 means "LIVE": restore the cumulative totals from
    the live dashboard state, exactly as they were before any
    range filter existed.
    */

    if (!hours) {

        if (label) {
            label.textContent = "Live totals";
        }

        renderDashboardState(
            appState.dashboardState || {}
        );

        return;
    }

    if (label) {
        label.textContent = `Last ${hours} hr${hours === 1 ? "" : "s"}`;
    }

    const hourly =
        Array.isArray(appState.analytics?.hourly)
            ? appState.analytics.hourly
            : [];

    const rows =
        hourly.slice(-hours);

    const total =
        rows.reduce(
            (sum, row) => sum + safeNumber(row.total, 0),
            0
        );

    const printed =
        rows.reduce(
            (sum, row) => sum + safeNumber(row.printed, 0),
            0
        );

    const missing =
        rows.reduce(
            (sum, row) => sum + safeNumber(row.missing, 0),
            0
        );

    const classifiedTotal =
        printed + missing;

    setText(
        "totalBags",
        formatInteger(total)
    );

    setText(
        "printedBags",
        formatInteger(printed)
    );

    setText(
        "missingBags",
        formatInteger(missing)
    );

    setText(
        "printQuality",
        classifiedTotal > 0
            ? formatPercent((printed / classifiedTotal) * 100)
            : "--"
    );

    setText(
        "productionRate",
        formatInteger(
            Math.round(total / hours)
        )
    );

    updatePrintInspectionChart(
        printed,
        missing
    );
}


/* ==========================================================
   CAMERA VIEW MODAL

   Reuses the same /live/{camera name} streaming endpoint that
   initializeLiveStreams() already uses, rather than introducing
   a second streaming mechanism.
   ========================================================== */

function renderCameraViewModal(index) {

    const clampedIndex =
        Math.min(
            CAMERA_COUNT,
            Math.max(1, index)
        );

    appState.cameraViewIndex =
        clampedIndex;

    const camera =
        findCameraByIndex(
            appState.dashboardState?.cameras || {},
            clampedIndex
        );

    const online =
        Boolean(
            camera?.online
            ??
            cameraIsOnline(camera)
        );

    const cameraDisplayName =
        `Camera ${clampedIndex}`;

    setText(
        "cameraViewTitle",
        cameraDisplayName
    );

    const statusElement =
        byId("cameraViewStatus");

    if (statusElement) {

        statusElement.textContent =
            online ? "ONLINE" : "OFFLINE";

        statusElement.classList.toggle("online", online);
        statusElement.classList.toggle("offline", !online);
    }

    const feed =
        byId("cameraViewFeed");

    if (!feed) {
        return;
    }

    if (!online) {

        feed.innerHTML =
            `
            <svg class="icon-svg" aria-hidden="true"><use href="#icon-video-slash" xlink:href="#icon-video-slash"></use></svg>
            <span>Camera Offline</span>
            `;

        return;
    }

    feed.innerHTML = "";

    const image =
        document.createElement("img");

    image.alt =
        `${cameraDisplayName} live feed`;

    image.addEventListener(
        "error",
        () => {

            feed.innerHTML =
                `
                <svg class="icon-svg" aria-hidden="true"><use href="#icon-video-slash" xlink:href="#icon-video-slash"></use></svg>
                <span>Live video stream not connected</span>
                `;
        }
    );

    image.src =
        `${API_BASE}/live/${encodeURIComponent(cameraDisplayName)}?t=${Date.now()}`;

    feed.appendChild(image);
}


function showCameraView(cameraId) {

    const cameraNum =
        parseInt(
            String(cameraId).replace("camera_", ""),
            10
        )
        + 1;

    const modal =
        byId("cameraViewModal");

    if (!modal) {
        return;
    }

    modal.style.display =
        "flex";

    renderCameraViewModal(
        Number.isFinite(cameraNum)
            ? cameraNum
            : 1
    );
}


function closeCameraView() {

    const modal =
        byId("cameraViewModal");

    if (!modal) {
        return;
    }

    modal.style.display =
        "none";
}


function initializeCameraViewModal() {

    const closeButton =
        byId("cameraViewClose");

    if (closeButton) {

        closeButton.addEventListener(
            "click",
            closeCameraView
        );
    }

    const modal =
        byId("cameraViewModal");

    if (modal) {

        modal.addEventListener(
            "click",
            event => {

                if (event.target === modal) {

                    closeCameraView();
                }
            }
        );
    }

    const prevButton =
        byId("cameraViewPrev");

    if (prevButton) {

        prevButton.addEventListener(
            "click",
            () => {

                const current =
                    appState.cameraViewIndex || 1;

                const next =
                    current <= 1
                        ? CAMERA_COUNT
                        : current - 1;

                renderCameraViewModal(next);
            }
        );
    }

    const nextButton =
        byId("cameraViewNext");

    if (nextButton) {

        nextButton.addEventListener(
            "click",
            () => {

                const current =
                    appState.cameraViewIndex || 1;

                const next =
                    current >= CAMERA_COUNT
                        ? 1
                        : current + 1;

                renderCameraViewModal(next);
            }
        );
    }

    document.addEventListener(
        "keydown",
        event => {

            if (event.key === "Escape") {

                closeCameraView();
            }
        }
    );
}


/* ==========================================================
   SOCKET.IO
   ========================================================== */

function initializeSocket() {

    if (
        typeof io !== "function"
    ) {

        console.warn(
            "Socket.IO client is not available. REST refresh will remain active."
        );

        return;
    }


    try {

        const token =
            localStorage.getItem("fillpac_auth_token") ||
            sessionStorage.getItem("fillpac_auth_token");

        if (!token) {
            console.warn(
                "No auth token available; skipping Socket.IO connection."
            );
            return;
        }

appState.socket =
    io(
        API_BASE,
        {
            auth: {
                token: token,
            },

            transports: [
                "websocket",
                "polling"
            ],

            reconnection: true,

            reconnectionAttempts:
                Infinity,

            reconnectionDelay:
                1000,

            reconnectionDelayMax:
                5000,

            timeout:
                10000
        }
    );


        /* --------------------------------------------------
           CONNECT ERROR (e.g. auth rejected)
           -------------------------------------------------- */

        appState.socket.on(
            "connect_error",
            (error) => {
                console.error(
                    "Socket connection error:",
                    error && error.message
                );
            }
        );


        /* --------------------------------------------------
           CONNECT
           -------------------------------------------------- */

        appState.socket.on(
            "connect",
            () => {

                appState.socketConnected =
                    true;


                console.log(
                    "FillPac Dashboard Socket.IO connected."
                );


                updateSocketIndicator(
                    true
                );
            }
        );


        /* --------------------------------------------------
           DISCONNECT
           -------------------------------------------------- */

        appState.socket.on(
            "disconnect",
            () => {

                appState.socketConnected =
                    false;


                console.warn(
                    "FillPac Dashboard Socket.IO disconnected."
                );


                updateSocketIndicator(
                    false
                );
            }
        );


        /* --------------------------------------------------
           FULL STATE
           -------------------------------------------------- */

        appState.socket.on(
            "state",
            state => {

                renderDashboardState(
                    state
                );
            }
        );


        /* --------------------------------------------------
           STATE UPDATE
           -------------------------------------------------- */

        appState.socket.on(
            "state_update",
            state => {

                renderDashboardState(
                    state
                );
            }
        );


        /* --------------------------------------------------
           DASHBOARD STATE
           -------------------------------------------------- */

        appState.socket.on(
            "dashboard_state",
            state => {

                renderDashboardState(
                    state
                );
            }
        );


        /* --------------------------------------------------
           CAMERA UPDATE
           -------------------------------------------------- */

        appState.socket.on(
            "camera_update",
            payload => {

                handleCameraSocketUpdate(
                    payload
                );
            }
        );


        /* --------------------------------------------------
           JAM UPDATE
           -------------------------------------------------- */

        appState.socket.on(
            "jam_update",
            payload => {

                handleJamSocketUpdate(
                    payload
                );
            }
        );


        /* --------------------------------------------------
           COUNT EVENT
           -------------------------------------------------- */

        appState.socket.on(
            "count_event",
            payload => {

                handleCountEvent(
                    payload
                );
            }
        );


        /* --------------------------------------------------
           PRODUCTION EVENT
           -------------------------------------------------- */

        appState.socket.on(
            "production_event",
            payload => {

                handleProductionEvent(
                    payload
                );
            }
        );

    }

    catch (error) {

        console.error(
            "Socket.IO initialization failed:",
            error
        );
    }
}


/* ==========================================================
   SOCKET INDICATOR
   ========================================================== */

function updateSocketIndicator(
    connected
) {

    const indicator =
        byId(
            "socketStatus"
        );


    if (!indicator) {
        return;
    }


    indicator.textContent =
        connected
            ? "LIVE"
            : "OFFLINE";


    indicator.classList.toggle(
        "online",
        connected
    );


    indicator.classList.toggle(
        "offline",
        !connected
    );
}


/* ==========================================================
   ENSURE DASHBOARD STATE
   ========================================================== */

function ensureDashboardState() {

    if (
        !appState.dashboardState
        ||
        typeof appState.dashboardState
        !== "object"
    ) {

        appState.dashboardState = {
            cameras: {}
        };
    }


    if (
        !appState.dashboardState.cameras
        ||
        typeof appState.dashboardState.cameras
        !== "object"
    ) {

        appState.dashboardState.cameras =
            {};
    }


    return appState.dashboardState;
}


/* ==========================================================
   CAMERA SOCKET UPDATE
   ========================================================== */

function handleCameraSocketUpdate(
    payload
) {

    if (
        !payload
        ||
        typeof payload !== "object"
    ) {
        return;
    }


    const cameraName =
        payload.camera_name
        ??
        payload.name
        ??
        payload.camera
        ??
        payload.camera_id;


    if (!cameraName) {

        console.warn(
            "Camera socket update without camera identifier:",
            payload
        );

        return;
    }


    const state =
        ensureDashboardState();


    const existing =
        state.cameras[
            cameraName
        ]
        || {};


    state.cameras[
        cameraName
    ] = {

        ...existing,

        ...payload
    };


    renderDashboardState(
        state
    );
}


/* ==========================================================
   JAM SOCKET UPDATE
   ========================================================== */

function handleJamSocketUpdate(
    payload
) {

    if (
        !payload
        ||
        typeof payload !== "object"
    ) {
        return;
    }


    const cameraName =
        payload.camera_name
        ??
        payload.name
        ??
        payload.camera
        ??
        payload.camera_id;


    if (!cameraName) {

        console.warn(
            "Jam update without camera identifier:",
            payload
        );

        return;
    }


    const state =
        ensureDashboardState();


    const existing =
        state.cameras[
            cameraName
        ]
        || {};


    state.cameras[
        cameraName
    ] = {

        ...existing,

        jam_detection_enabled:
            payload.jam_detection_enabled
            ??
            existing.jam_detection_enabled,

        jam_status:
            payload.jam_status
            ??
            payload.status
            ??
            existing.jam_status,

        jam_detected:
            payload.jam_detected
            ??
            existing.jam_detected,

        jam_warning:
            payload.jam_warning
            ??
            payload.warning
            ??
            existing.jam_warning,

        active_jam_count:
            payload.active_jam_count
            ??
            existing.active_jam_count
            ??
            0,

        active_jam_track_ids:
            payload.active_jam_track_ids
            ??
            existing.active_jam_track_ids
            ??
            [],

        jam_tracks:
            payload.jam_tracks
            ??
            payload.tracks
            ??
            existing.jam_tracks
            ??
            [],

        condition_c_detected:
            payload.condition_c_detected
            ??
            existing.condition_c_detected,

        condition_c_status:
            payload.condition_c_status
            ??
            existing.condition_c_status,

        condition_c_bag_count:
            payload.condition_c_bag_count
            ??
            existing.condition_c_bag_count,

        condition_c_track_ids:
            payload.condition_c_track_ids
            ??
            existing.condition_c_track_ids,

        condition_c_minimum_gap_mm:
            payload.condition_c_minimum_gap_mm
            ??
            existing.condition_c_minimum_gap_mm,

        condition_c_distances:
            payload.condition_c_distances
            ??
            existing.condition_c_distances,

        condition_c_image_url:
            payload.condition_c_image_url
            ??
            existing.condition_c_image_url,

        condition_c_image_path:
            payload.condition_c_image_path
            ??
            existing.condition_c_image_path
    };


    /*
    Update only the jam UI immediately.

    The normal periodic /state refresh remains the
    authoritative source for the complete dashboard.
    */

    renderJamMonitoring(
        state.cameras
    );


    renderDashboardHealthSummary(
        state
    );
}


/* ==========================================================
   COUNT EVENT
   ========================================================== */

function handleCountEvent(
    payload
) {

    if (
        !payload
        ||
        typeof payload !== "object"
    ) {
        return;
    }


    /*
    Backend remains the source of truth for counts.

    Do not increment counters in JavaScript because
    doing so could create duplicate counts when REST
    synchronization occurs.
    */

    loadDashboardState()
        .catch(
            error => {

                console.error(
                    "Count-event state refresh failed:",
                    error
                );
            }
        );


    if (
        appState.currentPage
        === "events"
    ) {

        loadEvents()
            .catch(
                error => {

                    console.error(
                        "Count-event event refresh failed:",
                        error
                    );
                }
            );
    }
}


/* ==========================================================
   PRODUCTION EVENT
   ========================================================== */

function handleProductionEvent(
    payload
) {

    if (
        !payload
        ||
        typeof payload !== "object"
    ) {
        return;
    }


    if (
        appState.currentPage
        === "events"
    ) {

        loadEvents()
            .catch(
                error => {

                    console.error(
                        "Production event refresh failed:",
                        error
                    );
                }
            );
    }


    if (
        appState.currentPage
        === "production"
        ||
        appState.currentPage
        === "dashboard"
    ) {

        /*
        Production KPI cards + Camera Production table now live
        on the Dashboard tab (moved from the former standalone
        "Production" tab), so refresh them here too on live
        socket push, not just on the REST poll interval.
        */

        loadProduction()
            .then(
                () => {

                    reapplyActiveKpiRangeFilter();
                }
            )
            .catch(
                error => {

                    console.error(
                        "Production page refresh failed:",
                        error
                    );
                }
            );
    }
}


/* ==========================================================
   JAM DIAGNOSTICS
   ========================================================== */

function logJamDiagnostics() {

    const cameras =
        appState
            .dashboardState
            ?.cameras
        || {};


    const summary =
        getJamSystemSummary(
            cameras
        );


    console.group(
        "FillPac AI - Jam Detection"
    );


    console.log(
        "System Summary:",
        summary
    );


    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const camera =
            findCameraByIndex(
                cameras,
                index
            );


        const cameraSummary =
            getJamCameraSummary(
                camera
            );


        console.log(
            `Camera ${index}:`,
            cameraSummary
        );


        if (
            cameraSummary.tracks.length > 0
        ) {

            console.table(
                cameraSummary.tracks.map(
                    track => ({

                        trackId:
                            getJamTrackId(
                                track
                            ),

                        state:
                            getJamTrackState(
                                track
                            ),

                        speedPxS:
                            getJamTrackSpeed(
                                track
                            ),

                        distancePx:
                            getJamTrackDistance(
                                track
                            ),

                        stationarySeconds:
                            getJamTrackStationaryTime(
                                track
                            )
                    })
                )
            );
        }
    }


    console.groupEnd();
}


/* ==========================================================
   REFRESH LOOP
   ========================================================== */

function startRefreshLoop() {

    if (
        appState.refreshTimer
    ) {

        clearInterval(
            appState.refreshTimer
        );
    }


    appState.refreshTimer =
        setInterval(
            async () => {

                try {

                    /*
                    --------------------------------------------------
                    Always refresh /state.

                    Socket.IO gives low-latency updates while this
                    REST poll protects against missed socket events.
                    --------------------------------------------------
                    */

                    await loadDashboardState();


                    /* ----------------------------------------------
                       PRODUCTION

                       Also runs on the Dashboard page, since the
                       Production KPI cards + Camera Production
                       table were moved there from the former
                       standalone "Production" tab.
                       ---------------------------------------------- */

                    if (
                        appState.currentPage
                        === "production"
                        ||
                        appState.currentPage
                        === "dashboard"
                    ) {

                        await loadProduction();
                    }


                    /* ----------------------------------------------
                       ANALYTICS

                       Also runs on the Dashboard page, since the
                       Production Trend chart there is fed by the
                       same /analytics payload.
                       ---------------------------------------------- */

                    if (
                        appState.currentPage
                        === "analytics"
                        ||
                        appState.currentPage
                        === "dashboard"
                    ) {

                        await loadAnalytics();
                    }


                    /* ----------------------------------------------
                       KPI RANGE FILTER

                       loadDashboardState()/loadProduction()/
                       loadAnalytics() above just re-rendered the KPI
                       cards from live totals - if a 1/4/8/16/24 HR
                       filter is active, re-apply it now so it isn't
                       silently reverted to LIVE every refresh cycle.
                       ---------------------------------------------- */

                    if (
                        appState.currentPage
                        === "dashboard"
                    ) {

                        reapplyActiveKpiRangeFilter();
                    }


                    /* ----------------------------------------------
                       EVENTS
                       ---------------------------------------------- */

                    if (
                        appState.currentPage
                        === "events"
                    ) {

                        await loadEvents();
                    }


                    /* ----------------------------------------------
                       CAMERAS
                       ---------------------------------------------- */

                    if (
                        appState.currentPage
                        === "cameras"
                    ) {

                        await loadCameraConfig();
                    }


                    /* ----------------------------------------------
                       JAM MONITOR

                       /state already refreshed the values.
                       Re-render explicitly for clarity.
                       ---------------------------------------------- */

                    if (
                        appState.currentPage
                        === "jam-monitor"
                    ) {

                        renderJamMonitoring(
                            appState
                                .dashboardState
                                ?.cameras
                            || {}
                        );
                    }

                }

                catch (error) {

                    console.error(
                        "Dashboard refresh cycle failed:",
                        error
                    );
                }

            },
            REFRESH_INTERVAL_MS
        );
}


/* ==========================================================
   STOP REFRESH LOOP
   ========================================================== */

function stopRefreshLoop() {

    if (
        !appState.refreshTimer
    ) {
        return;
    }


    clearInterval(
        appState.refreshTimer
    );


    appState.refreshTimer =
        null;
}


/* ==========================================================
   MANUAL REFRESH
   ========================================================== */

function initializeRefreshButton() {

    const button =
        byId(
            "refreshButton"
        )
        ||
        byId(
            "dashboardRefreshButton"
        );


    if (!button) {
        return;
    }


    button.addEventListener(
        "click",
        async () => {

            button.disabled =
                true;


            try {

                await loadDashboardState();


                /*
                Load page-specific information only when
                necessary. Dashboard state has already been
                refreshed above.
                */

                if (
                    appState.currentPage
                    === "production"
                ) {

                    await loadProduction();
                }


                else if (
                    appState.currentPage
                    === "analytics"
                ) {

                    await loadAnalytics();
                }


                else if (
                    appState.currentPage
                    === "events"
                ) {

                    await loadEvents();
                }


                else if (
                    appState.currentPage
                    === "cameras"
                ) {

                    await loadCameraConfig();
                }


                else if (
                    appState.currentPage
                    === "dashboard"
                ) {

                    /*
                    Production KPI cards + Camera Production
                    table, and the Analytics-fed data, now live
                    on the Dashboard tab (moved from the former
                    standalone "Production"/"Analytics" tabs).
                    */

                    await loadAnalytics();

                    await loadProduction();
                }

            }

            catch (error) {

                console.error(
                    "Manual dashboard refresh failed:",
                    error
                );

            }

            finally {

                button.disabled =
                    false;
            }
        }
    );
}


/* ==========================================================
   NETWORK STATUS
   ========================================================== */

function initializeNetworkListeners() {

    window.addEventListener(
        "online",
        () => {

            console.log(
                "Browser network connection restored."
            );


            loadDashboardState()
                .catch(
                    error => {

                        console.error(
                            "Network recovery refresh failed:",
                            error
                        );
                    }
                );
        }
    );


    window.addEventListener(
        "offline",
        () => {

            console.warn(
                "Browser network connection lost."
            );


            updateSystemStatus(
                "offline"
            );
        }
    );
}


/* ==========================================================
   PAGE VISIBILITY
   ========================================================== */

function initializeVisibilityListener() {

    document.addEventListener(
        "visibilitychange",
        () => {

            if (
                document.visibilityState
                !== "visible"
            ) {
                return;
            }


            /*
            Browser tabs may throttle timers while hidden.
            Refresh immediately when the operator returns.
            */

            loadDashboardState()
                .catch(
                    error => {

                        console.error(
                            "Visibility refresh failed:",
                            error
                        );
                    }
                );
        }
    );
}


/* ==========================================================
   KEYBOARD SHORTCUT
   ========================================================== */

function initializeKeyboardShortcuts() {

    document.addEventListener(
        "keydown",
        async event => {

            const target =
                event.target;


            const typing =
                target
                &&
                (
                    target.tagName === "INPUT"
                    ||
                    target.tagName === "TEXTAREA"
                    ||
                    target.tagName === "SELECT"
                );


            if (typing) {
                return;
            }


            /*
            Press R to manually refresh dashboard data.
            */

            if (
                event.key
                    .toLowerCase()
                === "r"
            ) {

                event.preventDefault();


                try {

                    await loadDashboardState();


                    if (
                        appState.currentPage
                        === "production"
                    ) {

                        await loadProduction();
                    }


                    else if (
                        appState.currentPage
                        === "analytics"
                    ) {

                        await loadAnalytics();
                    }


                    else if (
                        appState.currentPage
                        === "events"
                    ) {

                        await loadEvents();
                    }


                    else if (
                        appState.currentPage
                        === "cameras"
                    ) {

                        await loadCameraConfig();
                    }


                    else if (
                        appState.currentPage
                        === "dashboard"
                    ) {

                        /*
                        Production KPI cards + Camera Production
                        table, and the Analytics-fed data, now
                        live on the Dashboard tab (moved from the
                        former standalone "Production"/"Analytics"
                        tabs).
                        */

                        await loadAnalytics();

                        await loadProduction();
                    }

                }

                catch (error) {

                    console.error(
                        "Keyboard refresh failed:",
                        error
                    );
                }
            }
        }
    );
}


/* ==========================================================
   CLOCK
   ========================================================== */

function updateClock() {

    const now =
        new Date();


    setText(
        "currentTime",
        now.toLocaleTimeString()
    );


    setText(
        "currentDate",
        now.toLocaleDateString()
    );
}


function initializeClock() {

    updateClock();


    setInterval(
        updateClock,
        1000
    );
}


/* ==========================================================
   LAST STATE UPDATE
   ========================================================== */

function updateLastRefreshIndicator() {

    const element =
        byId(
            "lastUpdated"
        );


    if (!element) {
        return;
    }


    if (
        !appState.lastStateUpdate
    ) {

        element.textContent =
            "Waiting for data";

        return;
    }


    const elapsed =
        Math.max(
            0,
            (
                Date.now()
                -
                appState
                    .lastStateUpdate
                    .getTime()
            )
            /
            1000
        );


    if (
        elapsed < 2
    ) {

        element.textContent =
            "Updated now";
    }


    else if (
        elapsed < 60
    ) {

        element.textContent =
            `Updated ${Math.round(
                elapsed
            )}s ago`;
    }


    else {

        element.textContent =
            `Updated ${Math.round(
                elapsed / 60
            )}m ago`;
    }
}


function initializeLastUpdateIndicator() {

    updateLastRefreshIndicator();


    setInterval(
        updateLastRefreshIndicator,
        1000
    );
}


/* ==========================================================
   STATE AGE
   ========================================================== */

function getStateAgeSeconds(state) {

    if (!state) {
        return null;
    }


    const timestamp =
        state.updated_at
        ??
        state.timestamp
        ??
        state.last_update;


    if (!timestamp) {
        return null;
    }


    const updated =
        parseDate(
            timestamp
        );


    if (!updated) {
        return null;
    }


    return Math.max(
        0,
        (
            Date.now()
            -
            updated.getTime()
        )
        /
        1000
    );
}


/* ==========================================================
   STATE FRESHNESS
   ========================================================== */

function monitorStateFreshness() {

    const state =
        appState.dashboardState;


    if (!state) {
        return;
    }


    const age =
        getStateAgeSeconds(
            state
        );


    const stale =
        state.state_stale === true
        ||
        (
            age !== null
            &&
            age > 15
        );


    const element =
        byId(
            "dataFreshness"
        );


    if (!element) {
        return;
    }


    element.textContent =
        stale
            ? "STALE"
            : "LIVE";


    element.classList.toggle(
        "stale",
        stale
    );


    element.classList.toggle(
        "live",
        !stale
    );
}


function initializeFreshnessMonitor() {

    monitorStateFreshness();


    setInterval(
        monitorStateFreshness,
        2000
    );
}/* ==========================================================
   DASHBOARD HEALTH SUMMARY
   ========================================================== */

function cameraIsOnline(camera) {

    if (!camera) {
        return false;
    }


    const status =
        String(
            camera.status
            ??
            camera.camera_status
            ??
            ""
        )
            .trim()
            .toLowerCase();


    return (
        status === "online"
        ||
        status === "running"
        ||
        status === "active"
    );
}


function countOnlineCameras(cameras) {

    let count = 0;


    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const camera =
            findCameraByIndex(
                cameras,
                index
            );


        if (
            cameraIsOnline(
                camera
            )
        ) {

            count += 1;
        }
    }


    return count;
}


function renderDashboardHealthSummary(state) {

    if (!state) {
        return;
    }


    const cameras =
        state.cameras
        || {};


    const onlineCameras =
        countOnlineCameras(
            cameras
        );


    const jamSummary =
        getJamSystemSummary(
            cameras
        );


    setText(
        "onlineCameras",
        formatInteger(
            onlineCameras
        )
    );


    setText(
        "dashboardActiveJamCount",
        formatInteger(
            jamSummary.activeJams
        )
    );
}


/* ==========================================================
   SETTINGS
   ========================================================== */

function initializeSettings() {

    const autoRefreshToggle =
        byId(
            "autoRefreshToggle"
        );


    if (autoRefreshToggle) {

        autoRefreshToggle.addEventListener(
            "change",
            event => {

                if (
                    event.target.checked
                ) {

                    startRefreshLoop();
                }

                else {

                    stopRefreshLoop();
                }
            }
        );
    }


    const diagnosticsButton =
        byId(
            "jamDiagnosticsButton"
        );


    if (diagnosticsButton) {

        diagnosticsButton.addEventListener(
            "click",
            () => {

                logJamDiagnostics();
            }
        );
    }
}


/* ==========================================================
   CAMERA STREAM REFRESH
   ========================================================== */

function refreshCameraStreams() {

    const timestamp =
        Date.now();


    for (
        let index = 1;
        index <= CAMERA_COUNT;
        index += 1
    ) {

        const feedContainer =
            byId(
                `liveCamera${index}Feed`
            );


        if (!feedContainer) {
            continue;
        }


        const image =
            feedContainer.querySelector(
                "img"
            );


        if (!image) {
            continue;
        }


        const cameraName =
            `Camera ${index}`;


        image.src =
            `${API_BASE}/live/${encodeURIComponent(
                cameraName
            )}?t=${timestamp}`;
    }
}


/* ==========================================================
   LIVE MONITOR CONTROLS
   ========================================================== */

function initializeLiveMonitorControls() {

    const refreshButton =
        byId(
            "refreshStreamsButton"
        );


    if (!refreshButton) {
        return;
    }


    refreshButton.addEventListener(
        "click",
        () => {

            refreshCameraStreams();
        }
    );
}


/* ==========================================================
   JAM PAGE REFRESH
   ========================================================== */

function refreshJamPage() {

    const cameras =
        appState
            .dashboardState
            ?.cameras
        || {};


    renderJamMonitoring(
        cameras
    );


    return getJamSystemSummary(
        cameras
    );
}


/* ==========================================================
   INITIAL LOAD
   ========================================================== */

async function initialLoad() {

    try {

        /*
        ------------------------------------------------------
        /state is the primary dashboard source.

        It contains camera runtime state including:
        - counts
        - FPS
        - camera status
        - jam detection enabled
        - jam status
        - jam warning
        - jam detected
        - active jam count
        - active jam track IDs
        - jam track information
        ------------------------------------------------------
        */

        await loadDashboardState();

    }

    catch (error) {

        console.error(
            "Initial dashboard state load failed:",
            error
        );
    }


    /*
    ----------------------------------------------------------
    PAGE-SPECIFIC DATA

    These calls are intentionally isolated so failure of one
    endpoint does not prevent the dashboard from starting.
    ----------------------------------------------------------
    */

    try {

        if (
            appState.currentPage
            === "production"
        ) {

            await loadProduction();
        }


        else if (
            appState.currentPage
            === "analytics"
            ||
            appState.currentPage
            === "dashboard"
        ) {

            await loadAnalytics();

            /*
            Production KPI cards + Camera Production table now
            live on the Dashboard tab (moved from the former
            standalone "Production" tab), so fetch that data
            on initial load too, not just on the refresh loop.
            */

            if (
                appState.currentPage
                === "dashboard"
            ) {

                await loadProduction();
            }
        }


        else if (
            appState.currentPage
            === "events"
        ) {

            await loadEvents();
        }


        else if (
            appState.currentPage
            === "cameras"
        ) {

            await loadCameraConfig();
        }


        else if (
            appState.currentPage
            === "jam-monitor"
        ) {

            refreshJamPage();
        }

    }

    catch (error) {

        console.error(
            "Initial page-specific load failed:",
            error
        );
    }
}


/* ==========================================================
   APPLICATION INITIALIZATION
   ========================================================== */

async function initializeDashboard() {

    console.log(
        "=================================================="
    );

    console.log(
        "FillPac AI Dashboard"
    );

    console.log(
        "Initializing frontend..."
    );

    console.log(
        "=================================================="
    );


    /* ------------------------------------------------------
       NAVIGATION
       ------------------------------------------------------ */

    initializeNavigation();


    /* ------------------------------------------------------
       NOTIFICATION SYSTEM
       ------------------------------------------------------ */

    initializeNotificationSystem();


    /* ------------------------------------------------------
       CLOCK
       ------------------------------------------------------ */

    initializeClock();


    /* ------------------------------------------------------
       LAST UPDATE
       ------------------------------------------------------ */

    initializeLastUpdateIndicator();


    /* ------------------------------------------------------
       NETWORK LISTENERS
       ------------------------------------------------------ */

    initializeNetworkListeners();


    /* ------------------------------------------------------
       PAGE VISIBILITY
       ------------------------------------------------------ */

    initializeVisibilityListener();


    /* ------------------------------------------------------
       KEYBOARD SHORTCUTS
       ------------------------------------------------------ */

    initializeKeyboardShortcuts();


    /* ------------------------------------------------------
       REFRESH BUTTON
       ------------------------------------------------------ */

    initializeRefreshButton();


    /* ------------------------------------------------------
       EVENT CONTROLS
       ------------------------------------------------------ */

    initializeEventControls();


    /* ------------------------------------------------------
       CAMERA CONTROLS
       ------------------------------------------------------ */

    initializeCameraControls();


    /* ------------------------------------------------------
       LIVE STREAMS
       ------------------------------------------------------ */

    initializeLiveStreams();


    initializeLiveMonitorControls();


    /* ------------------------------------------------------
       CAMERA VIEW MODAL
       ------------------------------------------------------ */

    initializeCameraViewModal();


    /* ------------------------------------------------------
       PRODUCTION STATISTICS TIME RANGE FILTER
       ------------------------------------------------------ */

    initializeProductionTrendRangeButtons();

    initializeKpiRangeButtons();

    initializePackerSelector();


    /* ------------------------------------------------------
       SETTINGS
       ------------------------------------------------------ */

    initializeSettings();


    /* ------------------------------------------------------
       SOCKET.IO
       ------------------------------------------------------ */

    initializeSocket();


    /* ------------------------------------------------------
       STATE FRESHNESS
       ------------------------------------------------------ */

    initializeFreshnessMonitor();


    /* ------------------------------------------------------
       DETERMINE INITIAL PAGE
       ------------------------------------------------------ */

    const initialActiveItem =
        document.querySelector(
            ".sidebar-item.active[data-page]"
        );


    if (
        initialActiveItem
        &&
        initialActiveItem.dataset.page
    ) {

        appState.currentPage =
            initialActiveItem.dataset.page;
    }


    updatePageHeader(
        appState.currentPage
    );


    /* ------------------------------------------------------
       INITIAL DATA
       ------------------------------------------------------ */

    await initialLoad();


    /* ------------------------------------------------------
       PERIODIC REST REFRESH
       ------------------------------------------------------ */

    startRefreshLoop();


    console.log(
        "=================================================="
    );

    console.log(
        "FillPac AI Dashboard initialized successfully."
    );

    console.log(
        "Current page:",
        appState.currentPage
    );

    console.log(
        "Refresh interval:",
        `${REFRESH_INTERVAL_MS} ms`
    );

    console.log(
        "=================================================="
    );
}


/* ==========================================================
   GLOBAL JAVASCRIPT ERROR HANDLER
   ========================================================== */

window.addEventListener(
    "error",
    event => {

        console.error(
            "Dashboard JavaScript error:",
            event.error
            ??
            event.message
        );
    }
);


/* ==========================================================
   UNHANDLED PROMISE ERROR
   ========================================================== */

window.addEventListener(
    "unhandledrejection",
    event => {

        console.error(
            "Unhandled dashboard promise rejection:",
            event.reason
        );
    }
);


/* ==========================================================
   APPLICATION CLEANUP
   ========================================================== */

window.addEventListener(
    "beforeunload",
    () => {

        /* --------------------------------------------------
           STOP REST REFRESH
           -------------------------------------------------- */

        stopRefreshLoop();


        /* --------------------------------------------------
           DISCONNECT SOCKET
           -------------------------------------------------- */

        if (
            appState.socket
        ) {

            try {

                appState.socket.disconnect();

            }

            catch (error) {

                console.warn(
                    "Socket cleanup failed:",
                    error
                );
            }
        }
    }
);


/* ==========================================================
   DOM READY
   ========================================================== */

document.addEventListener(
    "DOMContentLoaded",
    () => {

        initializeDashboard()
            .catch(
                error => {

                    console.error(
                        "FillPac dashboard initialization failed:",
                        error
                    );


                    updateSystemStatus(
                        "offline"
                    );
                }
            );
    }
);


/* ==========================================================
   DEBUG / DEVELOPMENT INTERFACE
   ========================================================== */

/*
============================================================
Browser Console Commands
============================================================

Open:

    F12
    → Console


1. Complete dashboard state

    FillPacDashboard.state()


2. All camera jam states

    FillPacDashboard.jams()


3. Camera 1 jam tracks

    FillPacDashboard.jamTracks(1)


4. Camera 2 jam tracks

    FillPacDashboard.jamTracks(2)


5. Camera 3 jam tracks

    FillPacDashboard.jamTracks(3)


6. Camera 4 jam tracks

    FillPacDashboard.jamTracks(4)


7. Force state refresh

    FillPacDashboard.refresh()


8. Refresh jam UI

    FillPacDashboard.refreshJams()


9. Detailed jam diagnostics

    FillPacDashboard.diagnostics()

============================================================
*/


window.FillPacDashboard = {

    /* ------------------------------------------------------
       COMPLETE STATE
       ------------------------------------------------------ */

    state() {

        return (
            appState.dashboardState
            ??
            null
        );
    },


    /* ------------------------------------------------------
       JAM SUMMARY
       ------------------------------------------------------ */

    jams() {

        const cameras =
            appState
                .dashboardState
                ?.cameras
            || {};


        const result = {};


        for (
            let index = 1;
            index <= CAMERA_COUNT;
            index += 1
        ) {

            const camera =
                findCameraByIndex(
                    cameras,
                    index
                );


            result[
                `Camera ${index}`
            ] =
                getJamCameraSummary(
                    camera
                );
        }


        console.table(
            Object.entries(
                result
            )
                .map(
                    (
                        [
                            camera,
                            value
                        ]
                    ) => ({

                        camera,

                        enabled:
                            value.enabled,

                        status:
                            value.status,

                        activeJams:
                            value.activeCount,

                        trackIds:
                            value.trackIds.join(
                                ", "
                            )
                    })
                )
        );


        return result;
    },


    /* ------------------------------------------------------
       INDIVIDUAL CAMERA JAM TRACKS
       ------------------------------------------------------ */

    jamTracks(
        cameraIndex = 1
    ) {

        const index =
            Math.min(
                CAMERA_COUNT,
                Math.max(
                    1,
                    Math.round(
                        safeNumber(
                            cameraIndex,
                            1
                        )
                    )
                )
            );


        const camera =
            findCameraByIndex(

                appState
                    .dashboardState
                    ?.cameras
                || {},

                index
            );


        const tracks =
            getJamTracks(
                camera
            );


        console.table(
            tracks.map(
                track => ({

                    trackId:
                        getJamTrackId(
                            track
                        ),

                    status:
                        getJamTrackState(
                            track
                        ),

                    speedPxS:
                        getJamTrackSpeed(
                            track
                        ),

                    distancePx:
                        getJamTrackDistance(
                            track
                        ),

                    stationarySeconds:
                        getJamTrackStationaryTime(
                            track
                        )
                })
            )
        );


        return tracks;
    },


    /* ------------------------------------------------------
       FORCE BACKEND REFRESH
       ------------------------------------------------------ */

    async refresh() {

        return await loadDashboardState();
    },


    /* ------------------------------------------------------
       FORCE JAM UI REFRESH
       ------------------------------------------------------ */

    refreshJams() {

        return refreshJamPage();
    },


    /* ------------------------------------------------------
       JAM DIAGNOSTICS
       ------------------------------------------------------ */

    diagnostics() {

        logJamDiagnostics();
    },


    /* ------------------------------------------------------
       JAM SYSTEM SUMMARY
       ------------------------------------------------------ */

    jamSummary() {

        return getJamSystemSummary(

            appState
                .dashboardState
                ?.cameras
            || {}
        );
    }
};


/* ==========================================================
   END OF FILE
   ========================================================== */

/* ==========================================================
   BATCH COUNTER (dashboard first page)

   The operator feeds a count limit; the card shows how many
   bags were counted since Start, separate from the all-time
   totals. Data comes from /api/batch-counter (production_events
   since the start time), so it survives restarts.
   ========================================================== */

const BATCH_REFRESH_MS = 2000;

let batchReachedAnnounced = false;
let batchCamerasLoaded = false;


function batchFormat(value) {

    const number = Number(value);

    return Number.isFinite(number)
        ? number.toLocaleString()
        : "--";
}


function batchShowMessage(text) {

    setText("batchMessage", text || "");
}


function renderBatchCounter(data) {

    const card = byId("batchCounter");
    const badge = byId("batchStatusBadge");
    const fill = byId("batchBarFill");

    if (!card || !badge || !fill) {
        return;
    }

    const active = !!(data && data.active);

    if (!active) {

        batchReachedAnnounced = false;

        badge.textContent = "NOT SET";
        badge.className = "status-badge info";
        card.classList.remove("is-reached");
        fill.style.width = "0%";
        fill.classList.remove("reached");

        setText("batchCounted", "--");
        setText("batchTarget", "--");
        setText("batchRemaining", "--");
        setText("batchPrinted", "--");
        setText("batchMissing", "--");
        setText("batchStarted", "--");

        return;
    }

    setText("batchCounted", batchFormat(data.counted));
    setText("batchTarget", batchFormat(data.target));
    setText("batchPrinted", batchFormat(data.printed));
    setText("batchMissing", batchFormat(data.missing));

    setText(
        "batchRemaining",
        data.reached
            ? (data.over_by > 0
                ? `+${batchFormat(data.over_by)} over`
                : "0")
            : batchFormat(data.remaining)
    );

    const started = new Date(data.started_at);

    setText(
        "batchStarted",
        Number.isNaN(started.getTime())
            ? "--"
            : started.toLocaleString()
    );

    fill.style.width = `${data.progress_percent || 0}%`;
    fill.classList.toggle("reached", !!data.reached);
    card.classList.toggle("is-reached", !!data.reached);

    badge.textContent = data.reached ? "LIMIT REACHED" : "COUNTING";
    badge.className =
        "status-badge " + (data.reached ? "success" : "warning");

    const bar = card.querySelector(".batch-bar");

    if (bar) {
        bar.setAttribute(
            "aria-valuenow",
            String(data.progress_percent || 0)
        );
    }

    if (data.reached && !batchReachedAnnounced) {

        batchReachedAnnounced = true;

        batchShowMessage("");

        console.info(
            `Batch counter reached its limit (${data.target}).`
        );
    }

    if (!data.reached) {
        batchReachedAnnounced = false;
    }
}


async function loadBatchCounter() {

    try {

        const data = await apiFetch("/api/batch-counter");

        renderBatchCounter(data);

    } catch (error) {

        console.error("Failed loading batch counter:", error);
    }
}


async function loadBatchCameras() {

    const select = byId("batchCameraSelect");

    if (!select || batchCamerasLoaded) {
        return;
    }

    try {

        const cameras = await apiFetch("/cameras");

        Object.keys(cameras || {}).sort().forEach(
            name => {

                const option = document.createElement("option");

                option.value = name;
                option.textContent = name;

                select.appendChild(option);
            }
        );

        batchCamerasLoaded = true;

    } catch (error) {

        console.error("Failed loading camera list for batch counter:", error);
    }
}


async function startBatchCounter() {

    const input = byId("batchTargetInput");
    const select = byId("batchCameraSelect");

    const target = Number(input ? input.value : 0);

    if (!Number.isInteger(target) || target < 1) {

        batchShowMessage("Enter a whole number of bags, 1 or more.");

        return;
    }

    batchShowMessage("");

    try {

        const data = await apiFetch(
            "/api/batch-counter",
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    target: target,
                    camera: select ? select.value : "all",
                }),
            }
        );

        batchReachedAnnounced = false;

        renderBatchCounter(data);

    } catch (error) {

        console.error("Could not start batch counter:", error);

        batchShowMessage("Could not start the counter. Try again.");
    }
}


async function stopBatchCounter() {

    batchShowMessage("");

    try {

        await apiFetch(
            "/api/batch-counter/stop",
            { method: "POST" }
        );

        renderBatchCounter({ active: false });

    } catch (error) {

        console.error("Could not clear batch counter:", error);

        batchShowMessage("Could not clear the counter. Try again.");
    }
}


function initializeBatchCounter() {

    const startButton = byId("batchStartButton");
    const stopButton = byId("batchStopButton");
    const input = byId("batchTargetInput");

    if (!startButton || !stopButton) {
        return;
    }

    startButton.addEventListener("click", startBatchCounter);
    stopButton.addEventListener("click", stopBatchCounter);

    if (input) {

        input.addEventListener(
            "keydown",
            event => {

                if (event.key === "Enter") {
                    startBatchCounter();
                }
            }
        );
    }

    loadBatchCameras();
    loadBatchCounter();

    setInterval(
        () => {

            if (!document.hidden) {

                loadBatchCameras();
                loadBatchCounter();
            }
        },
        BATCH_REFRESH_MS
    );
}


document.addEventListener(
    "DOMContentLoaded",
    initializeBatchCounter
);