/**
 * FillPac Dashboard Authentication Integration - Theme Aware
 * 
 * Matches the FillPac dashboard design (dark teal theme, card-based layout)
 * Add to your existing dashboard.js
 */

// ==========================================================
// AUTHENTICATION HELPERS
// ==========================================================

/**
 * Get authentication token from storage
 */
function getAuthToken() {
    return localStorage.getItem('fillpac_auth_token') ||
           sessionStorage.getItem('fillpac_auth_token');
}

/**
 * Get current user from storage
 */
function getCurrentUser() {
    try {
        const userJson = localStorage.getItem('fillpac_user') ||
                        sessionStorage.getItem('fillpac_user');
        return userJson ? JSON.parse(userJson) : null;
    } catch (e) {
        return null;
    }
}

/**
 * Check if user is authenticated
 */
function isAuthenticated() {
    const token = getAuthToken();
    return !!token;
}

/**
 * Check authentication on page load
 */
function checkAuthentication() {
    if (!isAuthenticated()) {
        window.location.href = '/login';
        return false;
    }
    return true;
}

/**
 * Handle logout
 */
function handleLogout() {
    showAuthAlert('Logging out...', 'info');

    // Clear auth tokens
    localStorage.removeItem('fillpac_auth_token');
    localStorage.removeItem('fillpac_user');
    sessionStorage.removeItem('fillpac_auth_token');
    sessionStorage.removeItem('fillpac_user');

    // Redirect to login
    setTimeout(() => {
        window.location.href = '/login';
    }, 800);
}

/**
 * Handle 401 Unauthorized responses
 */
function handleUnauthorized() {
    console.warn('Session expired or invalid. Redirecting to login...');

    // Clear tokens
    localStorage.removeItem('fillpac_auth_token');
    localStorage.removeItem('fillpac_user');
    sessionStorage.removeItem('fillpac_auth_token');
    sessionStorage.removeItem('fillpac_user');

    // Show warning
    showAuthAlert('Session expired. Redirecting to login...', 'warning');

    // Redirect to login
    setTimeout(() => {
        window.location.href = '/login?redirect=' + encodeURIComponent(window.location.href);
    }, 2000);
}

// ==========================================================
// USER MENU COMPONENT
// ==========================================================
//
// NOTE: General authenticated API requests go through dashboard.js's
// apiFetch(), which already attaches the bearer token, handles 401s,
// and points at API_BASE. There's no separate authenticatedFetch()
// here anymore to avoid two different request helpers drifting out
// of sync.

/**
 * Create user menu HTML element
 */
function createUserMenu(user) {
    const userInitial = (user.username || 'U').charAt(0).toUpperCase();

    return `
        <div class="user-menu">
            <div class="user-avatar">
                <i class="fas fa-user"></i>
            </div>
            <div class="user-details">
                <span class="username">
                    ${escapeHtml(user.username)}
                </span>
                <span class="user-role">${escapeHtml(String(user.role || 'operator'))}</span>
            </div>
            <button class="btn-logout" onclick="handleLogout()" title="Logout">
                <i class="fas fa-sign-out-alt"></i>
                <span>Logout</span>
            </button>
        </div>
    `;
}

/**
 * Update header with user info and logout button
 */
function updateHeaderWithAuth() {
    const user = getCurrentUser();

    if (!user) {
        return;
    }

    // Find header right element
    const headerRight = document.querySelector('.header-right');

    if (!headerRight) {
        console.warn('Could not find .header-right element for user menu');
        return;
    }

    // Check if user menu already exists
    let userMenu = document.querySelector('.user-menu');

    if (!userMenu) {
        // Create and append user menu
        const userMenuHtml = createUserMenu(user);
        headerRight.insertAdjacentHTML('beforeend', userMenuHtml);
    } else {
        // Update existing user menu
        userMenu.innerHTML = createUserMenu(user);
    }

    // Ensure CSS is loaded
    ensureAuthStylesLoaded();
}

/**
 * Ensure authentication CSS styles are loaded
 */
function ensureAuthStylesLoaded() {
    // Check if auth styles already loaded
    if (document.querySelector('link[data-auth-styles]')) {
        return;
    }

    // Create style element with auth styles
    const style = document.createElement('style');
    style.setAttribute('data-auth-styles', 'true');
    style.textContent = `
        .user-menu {
            display: flex;
            align-items: center;
            gap: 16px;
            padding: 0 20px;
            height: 60px;
            border-left: 1px solid rgba(255, 255, 255, 0.15);
            margin-left: auto;
        }

        .user-info {
            display: flex;
            align-items: center;
            gap: 12px;
            color: white;
        }

        .user-avatar {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: rgba(255, 255, 255, 0.15);
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-size: 16px;
            border: 2px solid rgba(255, 255, 255, 0.2);
        }

        .user-details {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .username {
            font-size: 13px;
            font-weight: 600;
            color: white;
        }

        .user-role {
            font-size: 11px;
            color: rgba(255, 255, 255, 0.7);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .btn-logout {
            background: rgba(255, 255, 255, 0.1);
            color: white;
            border: 1px solid rgba(255, 255, 255, 0.2);
            padding: 6px 14px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 600;
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 6px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            white-space: nowrap;
        }

        .btn-logout:hover {
            background: rgba(255, 255, 255, 0.2);
            border-color: rgba(255, 255, 255, 0.3);
            transform: translateY(-1px);
        }

        .btn-logout:active {
            background: rgba(255, 255, 255, 0.15);
            transform: translateY(0);
        }

        @media (max-width: 768px) {
            .user-menu {
                gap: 8px;
                padding: 0 12px;
            }

            .user-details {
                display: none;
            }

            .btn-logout {
                padding: 4px 8px;
                font-size: 11px;
            }

            .btn-logout span {
                display: none;
            }
        }
    `;

    document.head.appendChild(style);
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}

// ==========================================================
// NOTIFICATIONS
// ==========================================================

/**
 * Show authentication alert notification
 */
function showAuthAlert(message, type = 'info') {
    // Create alert container if it doesn't exist
    let alertContainer = document.querySelector('#authAlertContainer');

    if (!alertContainer) {
        alertContainer = document.createElement('div');
        alertContainer.id = 'authAlertContainer';
        document.body.appendChild(alertContainer);
    }

    // Create alert element
    const alertId = 'auth-alert-' + Date.now();
    const alertDiv = document.createElement('div');
    alertDiv.id = alertId;
    alertDiv.className = `auth-alert ${type}`;

    const iconMap = {
        'error': 'fa-exclamation-circle',
        'success': 'fa-check-circle',
        'warning': 'fa-exclamation-triangle',
        'info': 'fa-info-circle'
    };

    const titleMap = {
        'error': 'Error',
        'success': 'Success',
        'warning': 'Warning',
        'info': 'Information'
    };

    const icon = iconMap[type] || 'fa-info-circle';
    const title = titleMap[type] || 'Notification';

    alertDiv.innerHTML = `
        <div class="alert-content">
            <div class="alert-icon">
                <i class="fas ${icon}"></i>
            </div>
            <div class="alert-text">
                <div class="alert-title">${title}</div>
                <div class="alert-message">${escapeHtml(message)}</div>
            </div>
        </div>
    `;

    alertContainer.appendChild(alertDiv);

    // Add alert styles if not present
    ensureAlertStylesLoaded();

    // Auto-remove after 5 seconds
    setTimeout(() => {
        if (alertDiv.parentElement) {
            alertDiv.style.animation = 'slideOutRight 0.3s ease-out';
            setTimeout(() => {
                if (alertDiv.parentElement) {
                    alertDiv.remove();
                }
            }, 300);
        }
    }, 5000);
}

/**
 * Ensure alert notification styles are loaded
 */
function ensureAlertStylesLoaded() {
    if (document.querySelector('style[data-alert-styles]')) {
        return;
    }

    const style = document.createElement('style');
    style.setAttribute('data-alert-styles', 'true');
    style.textContent = `
        #authAlertContainer {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 9999;
            pointer-events: none;
        }

        .auth-alert {
            max-width: 400px;
            padding: 16px 20px;
            background: white;
            border-left: 4px solid;
            border-radius: 6px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.15);
            animation: slideInRight 0.3s ease-out;
            pointer-events: auto;
            margin-bottom: 12px;
        }

        @keyframes slideInRight {
            from {
                transform: translateX(400px);
                opacity: 0;
            }
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }

        @keyframes slideOutRight {
            from {
                transform: translateX(0);
                opacity: 1;
            }
            to {
                transform: translateX(400px);
                opacity: 0;
            }
        }

        .auth-alert.error {
            border-color: #f56565;
            background: #fff5f5;
        }

        .auth-alert.error .alert-icon {
            color: #f56565;
        }

        .auth-alert.warning {
            border-color: #ed8936;
            background: #fffaf0;
        }

        .auth-alert.warning .alert-icon {
            color: #ed8936;
        }

        .auth-alert.success {
            border-color: #48bb78;
            background: #f0fdf4;
        }

        .auth-alert.success .alert-icon {
            color: #48bb78;
        }

        .auth-alert.info {
            border-color: #63b3ed;
            background: #bee3f8;
        }

        .auth-alert.info .alert-icon {
            color: #63b3ed;
        }

        .alert-content {
            display: flex;
            align-items: flex-start;
            gap: 12px;
        }

        .alert-icon {
            font-size: 18px;
            flex-shrink: 0;
            margin-top: 2px;
        }

        .alert-text {
            flex: 1;
        }

        .alert-title {
            font-weight: 600;
            font-size: 13px;
            margin-bottom: 2px;
            color: #1a202c;
        }

        .alert-message {
            font-size: 12px;
            color: #718096;
        }

        @media (max-width: 480px) {
            #authAlertContainer {
                left: 20px;
                right: 20px;
            }

            .auth-alert {
                max-width: 100%;
            }
        }
    `;

    document.head.appendChild(style);
}

// ==========================================================
// PAGE INITIALIZATION
// ==========================================================
//
// NOTE: Data fetching (apiFetch) and the real Socket.IO connection
// (initializeSocket, wired to API_BASE and appState.socket) live in
// dashboard.js and are the ones actually used by the app. This file
// only owns the auth-aware header/menu and session bookkeeping, so
// there is no second, competing initializeSocket() here anymore --
// that used to shadow dashboard.js's real implementation if this
// script ever loaded after dashboard.js.

/**
 * Initialize dashboard with authentication
 */
function initializeAuthUI() {
    console.log('Initializing authenticated dashboard header...');

    // Update header with user info
    updateHeaderWithAuth();

    // Show welcome message
    const user = getCurrentUser();
    if (user) {
        showAuthAlert(`Welcome back, ${user.username}!`, 'success');
    }

    // Initialize other dashboard components
    // ... your existing initialization code ...
}

/**
 * Main entry point - called on DOMContentLoaded
 */
window.addEventListener('DOMContentLoaded', () => {
    // Check authentication first
    if (!checkAuthentication()) {
        return; // Not authenticated, already redirected to login
    }

    // Initialize the auth-aware header (user menu, welcome toast).
    // The main dashboard data/init logic lives in dashboard.js's own
    // DOMContentLoaded listener and runs independently of this one.
    initializeAuthUI();
});

/**
 * Handle page visibility changes (tab switch)
 */
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        // Verify token is still valid
        const token = getAuthToken();

        if (!token) {
            handleUnauthorized();
            return;
        }

        console.log('Dashboard resumed from background');
    }
});

// ==========================================================
// GLOBAL ERROR HANDLER
// ==========================================================

/**
 * Global error handler for unhandled rejections
 */
window.addEventListener('unhandledrejection', (event) => {
    if (event.reason && event.reason.message === 'Unauthorized - session expired') {
        event.preventDefault();
        console.warn('Handling unauthorized session');
    }
});

// ==========================================================
// SESSION MANAGEMENT
// ==========================================================

/**
 * Check token expiration periodically
 */
function startTokenExpirationCheck(intervalMinutes = 5) {
    setInterval(() => {
        const token = getAuthToken();

        if (!token) {
            return;
        }

        // Simple check: verify token still works
        fetch('/api/auth/verify', {
            headers: {
                'Authorization': `Bearer ${token}`,
            },
        })
        .then(response => {
            if (!response.ok) {
                handleUnauthorized();
            }
        })
        .catch(() => {
            // Network error, don't force logout
            console.warn('Could not verify token');
        });
    }, intervalMinutes * 60 * 1000);
}

// Start token expiration check (every 5 minutes)
startTokenExpirationCheck(5);

// ==========================================================
// EXPORT FOR TESTING
// ==========================================================

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        getAuthToken,
        getCurrentUser,
        isAuthenticated,
        checkAuthentication,
        handleLogout,
        handleUnauthorized,
        updateHeaderWithAuth,
        showAuthAlert,
        initializeAuthUI,
    };
}