// ============================================================================
// Smart Bus Tracking System - Core JavaScript
// Theme toggling, auth, notifications, and utilities
// ============================================================================

document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initCurrentUser();
});

// Theme Management
function initTheme() {
    const savedTheme = localStorage.getItem("sb_theme") || "light";
    setTheme(savedTheme);

    const toggleBtn = document.getElementById("theme-toggle-btn");
    if (toggleBtn) {
        toggleBtn.addEventListener("click", () => {
            const currentTheme = document.documentElement.getAttribute("data-theme") || "light";
            const newTheme = currentTheme === "dark" ? "light" : "dark";
            setTheme(newTheme);
            localStorage.setItem("sb_theme", newTheme);
            
            // Dispatch event for charts to redraw in new theme
            window.dispatchEvent(new CustomEvent("themeChanged", { detail: { theme: newTheme } }));
        });
    }
}

function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const icon = document.getElementById("theme-toggle-icon");
    if (icon) {
        if (theme === "dark") {
            icon.className = "bi bi-sun-fill";
        } else {
            icon.className = "bi bi-moon-stars-fill";
        }
    }
}

// Global Toast Notifications
function showToast(message, type = "info") {
    let container = document.getElementById("toast-container");
    if (!container) {
        container = document.createElement("div");
        container.id = "toast-container";
        container.className = "toast-container";
        document.body.appendChild(container);
    }

    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    
    const iconMap = {
        success: "bi-check-circle-fill text-success",
        error: "bi-exclamation-triangle-fill text-danger",
        warning: "bi-exclamation-circle-fill text-warning",
        info: "bi-info-circle-fill text-primary"
    };

    toast.innerHTML = `
        <div style="display:flex; align-items:center; gap:10px;">
            <i class="bi ${iconMap[type] || iconMap.info}"></i>
            <span>${message}</span>
        </div>
        <button style="background:none;border:none;cursor:pointer;color:var(--text-muted);margin-left:12px;" onclick="this.parentElement.remove()">
            &times;
        </button>
    `;

    container.appendChild(toast);
    setTimeout(() => {
        toast.remove();
    }, 4500);
}

// User state & Consent
async function initCurrentUser() {
    try {
        const res = await fetch("/api/auth/me");
        if (res.ok) {
            const data = await res.json();
            if (data.authenticated && data.user) {
                const nameEl = document.getElementById("user-name-display");
                const roleEl = document.getElementById("user-role-display");
                const avatarEl = document.getElementById("user-avatar-display");
                
                if (nameEl) nameEl.textContent = data.user.full_name || data.user.username;
                if (roleEl) roleEl.textContent = data.user.role;
                if (avatarEl) avatarEl.textContent = (data.user.username || "U")[0].toUpperCase();

                // If biometric consent not yet accepted, prompt notice modal
                if (!data.user.consent_accepted && !sessionStorage.getItem("consent_prompted")) {
                    showConsentModal();
                }
            }
        } else if (window.location.pathname !== "/login") {
            window.location.href = "/login";
        }
    } catch (e) {
        console.error("Auth verify error:", e);
    }
}

function showConsentModal() {
    sessionStorage.setItem("consent_prompted", "true");
    const modal = document.getElementById("consent-modal");
    if (modal) {
        modal.classList.add("active");
    }
}

async function acceptBiometricConsent() {
    try {
        const res = await fetch("/api/auth/consent", { method: "POST" });
        if (res.ok) {
            const modal = document.getElementById("consent-modal");
            if (modal) modal.classList.remove("active");
            showToast("Biometric tracking policy acknowledged.", "success");
        }
    } catch (e) {
        console.error(e);
    }
}

async function logoutUser() {
    try {
        await fetch("/api/auth/logout", { method: "POST" });
        window.location.href = "/login";
    } catch (e) {
        window.location.href = "/login";
    }
}

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}
