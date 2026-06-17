console.log('App.js Version: 3.0 - Auth + Admin');

// ── Auth State ────────────────────────────────────────────────────────────────
let authToken = localStorage.getItem('imgflow_token') || null;
let currentUserRole = localStorage.getItem('imgflow_role') || null;
let currentUsername = localStorage.getItem('imgflow_username') || null;

function authFetch(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    return fetch(url, { ...options, headers }).then(res => {
        if (res.status === 401) {
            handleLogout();
            throw new Error('Session expired. Please log in again.');
        }
        return res;
    });
}

function saveAuth(token, role, username) {
    authToken = token;
    currentUserRole = role;
    currentUsername = username;
    localStorage.setItem('imgflow_token', token);
    localStorage.setItem('imgflow_role', role);
    localStorage.setItem('imgflow_username', username);
}

function clearAuth() {
    authToken = null;
    currentUserRole = null;
    currentUsername = null;
    localStorage.removeItem('imgflow_token');
    localStorage.removeItem('imgflow_role');
    localStorage.removeItem('imgflow_username');
}

let heartbeatInterval = null;

function startHeartbeat() {
    if (heartbeatInterval) return;
    // Fire immediately so time starts accumulating right away
    if (authToken) authFetch('/api/v1/heartbeat', { method: 'POST' }).catch(() => {});
    heartbeatInterval = setInterval(() => {
        if (authToken) authFetch('/api/v1/heartbeat', { method: 'POST' }).catch(() => {});
    }, 30000);
}

function stopHeartbeat() {
    if (heartbeatInterval) { clearInterval(heartbeatInterval); heartbeatInterval = null; }
}

function handleLogout() {
    stopHeartbeat();
    clearAuth();
    showLoginOverlay();
}

function showLoginOverlay() {
    document.getElementById('login-overlay').classList.remove('hidden');
    document.getElementById('user-menu').classList.add('hidden');
    document.getElementById('admin-panel-btn').classList.add('hidden');
}

function hideLoginOverlay() {
    document.getElementById('login-overlay').classList.add('hidden');
    document.getElementById('header-username').textContent = currentUsername || '';
    document.getElementById('user-menu').classList.remove('hidden');
    if (currentUserRole === 'admin') {
        document.getElementById('admin-panel-btn').classList.remove('hidden');
    }
}

function toggleUserMenu(e) {
    e.stopPropagation();
    document.getElementById('user-menu').classList.toggle('open');
}

function closeUserMenu() {
    document.getElementById('user-menu').classList.remove('open');
}

document.addEventListener('click', function(e) {
    const menu = document.getElementById('user-menu');
    if (menu && !menu.contains(e.target)) closeUserMenu();
});

function openChangePasswordModal() {
    closeUserMenu();
    document.getElementById('cp-current').value = '';
    document.getElementById('cp-new').value = '';
    document.getElementById('cp-confirm').value = '';
    const err = document.getElementById('cp-error');
    err.textContent = '';
    err.classList.add('hidden');
    document.getElementById('change-password-modal').classList.remove('hidden');
}

function closeChangePasswordModal(e) {
    if (e && !e.target.classList.contains('login-overlay')) return;
    document.getElementById('change-password-modal').classList.add('hidden');
}

async function submitChangePassword(e) {
    if (e) e.stopPropagation();
    const current = document.getElementById('cp-current').value;
    const newPw = document.getElementById('cp-new').value;
    const confirm = document.getElementById('cp-confirm').value;
    const errEl = document.getElementById('cp-error');

    const showErr = (msg) => { errEl.textContent = msg; errEl.classList.remove('hidden'); };

    if (!current || !newPw || !confirm) return showErr('All fields are required.');
    if (newPw !== confirm) return showErr('New passwords do not match.');
    if (newPw.length < 6) return showErr('New password must be at least 6 characters.');

    try {
        const res = await authFetch('/api/v1/users/me/change-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ current_password: current, new_password: newPw }),
        });
        if (!res.ok) {
            const data = await res.json();
            return showErr(data.detail || 'Failed to change password.');
        }
        document.getElementById('change-password-modal').classList.add('hidden');
    } catch (err) {
        showErr('Network error. Please try again.');
    }
}

// ── Global State ──────────────────────────────────────────────────────────────
let currentSessionId = null;
let currentCourseId = null;
let currentCourseData = null;
let statusPollingIntervals = [];

let lightboxImageId = null;
let lightboxPlanId = null;
let lightboxImages = [];
let lightboxIndex = 0;

const planTimestamps = {};
const planSessionTags = {};  // planId → session tag
let currentEditorPlan = null;

const DEFAULT_RESTRICTIONS = `No logos or branding\nNo watermarks\nNo foreign subjects\nNo foreign currency\nUse Indian currency (₹) when money is shown\nABSOLUTELY NO TEXT, letters, numbers, or words anywhere in the image\nNo political or sensitive content`;


// ── DOM Elements ──────────────────────────────────────────────────────────────
const sectionCourseList = document.getElementById('course-list-section');
const sectionCourseDetail = document.getElementById('course-detail-section');
const sectionUpload = document.getElementById('upload-section');
const sectionEditor = document.getElementById('editor-section');
const sectionTimeline = document.getElementById('timeline-section');
const sectionAdmin = document.getElementById('admin-section');

const coursesContainer = document.getElementById('courses-container');
const createCourseFormContainer = document.getElementById('create-course-form-container');
const createCourseForm = document.getElementById('create-course-form');

const courseDetailsDisplay = document.getElementById('course-details-display');
const editCourseFormContainer = document.getElementById('edit-course-form-container');
const editCourseForm = document.getElementById('edit-course-form');

const courseVideosContainer = document.getElementById('course-videos-container');

const uploadForm = document.getElementById('upload-form');
const uploadBtn = document.getElementById('upload-btn');
const uploadSpinner = document.getElementById('upload-spinner');
const statusMsg = document.getElementById('upload-status');

const segmentsContainer = document.getElementById('segments-container');
const finalAssetsContainer = document.getElementById('final-assets-container');
const lightbox = document.getElementById('lightbox');
const lightboxImg = document.getElementById('lightbox-img');

// ── Init ──────────────────────────────────────────────────────────────────────
function showLoginPanel() {
    document.getElementById('login-panel').classList.remove('hidden');
    document.getElementById('register-panel').classList.add('hidden');
    document.getElementById('register-form').reset();
    document.getElementById('register-error').classList.add('hidden');
    document.getElementById('register-success').classList.add('hidden');
}

function showRegisterPanel() {
    document.getElementById('register-panel').classList.remove('hidden');
    document.getElementById('login-panel').classList.add('hidden');
    document.getElementById('login-error').classList.add('hidden');
}

function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  const btn = document.getElementById('theme-toggle-btn');
  if (btn) btn.textContent = next === 'dark' ? '☀️' : '🌙';
}

function initTheme() {
  const saved = localStorage.getItem('theme') || 'light';
  const btn = document.getElementById('theme-toggle-btn');
  if (btn) btn.textContent = saved === 'dark' ? '☀️' : '🌙';
}

document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    // Register form
    document.getElementById('register-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = document.getElementById('reg-username').value.trim();
        const password = document.getElementById('reg-password').value;
        const confirm = document.getElementById('reg-confirm').value;
        const btn = document.getElementById('register-btn');
        const errEl = document.getElementById('register-error');
        const successEl = document.getElementById('register-success');

        errEl.classList.add('hidden');
        successEl.classList.add('hidden');

        if (password !== confirm) {
            errEl.textContent = 'Passwords do not match.';
            errEl.classList.remove('hidden');
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Creating account…';
        try {
            const res = await fetch('/api/v1/auth/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            });
            const data = await res.json();
            if (res.ok) {
                document.getElementById('register-form').reset();
                successEl.textContent = data.detail;
                successEl.classList.remove('hidden');
            } else {
                errEl.textContent = data.detail || 'Registration failed.';
                errEl.classList.remove('hidden');
            }
        } catch {
            errEl.textContent = 'Network error. Please try again.';
            errEl.classList.remove('hidden');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Create Account';
        }
    });

    // Login form
    document.getElementById('login-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = document.getElementById('login-username').value.trim();
        const password = document.getElementById('login-password').value;
        const btn = document.getElementById('login-btn');
        const errEl = document.getElementById('login-error');
        btn.disabled = true;
        btn.textContent = 'Signing in…';
        errEl.classList.add('hidden');
        try {
            const res = await fetch('/api/v1/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            });
            const data = await res.json();
            if (res.ok) {
                saveAuth(data.access_token, data.role, data.username);
                hideLoginOverlay();
                startHeartbeat();
                history.replaceState({ view: 'course-list' }, '', '#courses');
                loadCourses();
            } else {
                errEl.textContent = data.detail || 'Login failed';
                errEl.classList.remove('hidden');
            }
        } catch (err) {
            errEl.textContent = 'Network error. Please try again.';
            errEl.classList.remove('hidden');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Sign In';
        }
    });

    // If we have a token, verify it then restore last view; else show login
    if (authToken) {
        authFetch('/api/v1/auth/me')
            .then(async res => {
                if (res.ok) {
                    hideLoginOverlay();
                    startHeartbeat();
                    await restoreFromHash();
                } else {
                    clearAuth();
                    showLoginOverlay();
                }
            })
            .catch(() => {
                clearAuth();
                showLoginOverlay();
            });
    } else {
        showLoginOverlay();
    }

    // Download button
    const dlBtn = document.getElementById('download-lightbox-btn');
    if (dlBtn) {
        dlBtn.addEventListener('click', () => downloadLightboxImage());
    }

    // Admin panel
    document.getElementById('add-user-btn').addEventListener('click', () => {
        document.getElementById('add-user-form-container').classList.remove('hidden');
    });
    document.getElementById('cancel-add-user-btn').addEventListener('click', () => {
        document.getElementById('add-user-form-container').classList.add('hidden');
        document.getElementById('add-user-form').reset();
    });
    document.getElementById('add-user-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = document.getElementById('new-username').value.trim();
        const password = document.getElementById('new-password').value;
        const role = document.getElementById('new-role').value;
        try {
            const res = await authFetch('/api/v1/admin/users', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password, role })
            });
            if (res.ok) {
                document.getElementById('add-user-form-container').classList.add('hidden');
                document.getElementById('add-user-form').reset();
                loadAdminUsers();
            } else {
                const d = await res.json();
                alert(d.detail || 'Failed to create user');
            }
        } catch (err) {
            alert('Network error');
        }
    });
});

// ── View Switching ────────────────────────────────────────────────────────────
document.getElementById('show-create-course-btn').addEventListener('click', () => {
    document.getElementById('course-restrictions').value = DEFAULT_RESTRICTIONS;
    createCourseFormContainer.classList.remove('hidden');
});
document.getElementById('cancel-course-btn').addEventListener('click', () => {
    createCourseFormContainer.classList.add('hidden');
});
document.getElementById('back-to-courses-btn').addEventListener('click', () => {
    history.back();
});
document.getElementById('edit-course-btn').addEventListener('click', () => {
    document.getElementById('edit-course-title').value = currentCourseData.title;
    document.getElementById('edit-course-scenario').value = currentCourseData.scenario_description;
    document.getElementById('edit-course-about').value = currentCourseData.about_course;
    document.getElementById('edit-course-restrictions').value = currentCourseData.custom_restrictions || DEFAULT_RESTRICTIONS;
    courseDetailsDisplay.classList.add('hidden');
    editCourseFormContainer.classList.remove('hidden');
});
document.getElementById('cancel-edit-course-btn').addEventListener('click', () => {
    editCourseFormContainer.classList.add('hidden');
    courseDetailsDisplay.classList.remove('hidden');
});
document.getElementById('add-video-btn').addEventListener('click', () => {
    document.getElementById('upload-course-name').textContent = currentCourseData.title;
    document.getElementById('upload-course-id').value = currentCourseId;
    navigateTo('upload', { courseId: currentCourseId });
});
document.getElementById('cancel-upload-btn').addEventListener('click', () => {
    history.back();
});
document.getElementById('back-to-detail-btn').addEventListener('click', () => {
    history.back();
});

// ── Create Course ─────────────────────────────────────────────────────────────
createCourseForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const title = document.getElementById('course-title').value;
    const scenario = document.getElementById('course-scenario').value;
    const about = document.getElementById('course-about').value;
    const restrictions = document.getElementById('course-restrictions').value;
    const btn = document.getElementById('submit-course-btn');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
        const res = await authFetch('/api/v1/courses', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title, scenario_description: scenario, about_course: about, custom_restrictions: restrictions })
        });
        if (res.ok) {
            createCourseForm.reset();
            createCourseFormContainer.classList.add('hidden');
            await loadCourses();
        } else {
            alert("Failed to create course.");
        }
    } catch (e) {
        console.error(e);
        alert("Error creating course.");
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Course';
    }
});

// ── Edit Course ───────────────────────────────────────────────────────────────
editCourseForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const title = document.getElementById('edit-course-title').value;
    const scenario = document.getElementById('edit-course-scenario').value;
    const about = document.getElementById('edit-course-about').value;
    const restrictions = document.getElementById('edit-course-restrictions').value;
    const btn = document.getElementById('save-edit-course-btn');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
        const payload = { title, scenario_description: scenario, about_course: about, custom_restrictions: restrictions };
        
        const res = await authFetch(`/api/v1/courses/${currentCourseId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (res.ok) {
            // Auto-refresh prompts if restrictions changed
            if (currentCourseData && currentCourseData.custom_restrictions !== restrictions) {
                await authFetch(`/api/v1/courses/${currentCourseId}/prompts`, { method: 'DELETE' });
            }
            
            editCourseFormContainer.classList.add('hidden');
            courseDetailsDisplay.classList.remove('hidden');
            await loadCourseDetail(currentCourseId);
        } else {
            alert("Failed to update course.");
        }
    } catch (e) {
        console.error(e);
        alert("Error updating course.");
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Changes';
    }
});

// ── Load Courses ──────────────────────────────────────────────────────────────
async function loadCourses() {
    try {
        const res = await authFetch('/api/v1/courses');
        const courses = await res.json();

        if (courses.length === 0) {
            coursesContainer.innerHTML = `
                <div class="empty-state" style="grid-column:1/-1;">
                    <div class="empty-state-icon">📚</div>
                    <div class="empty-state-title">No courses yet</div>
                    <p>Create your first course to start generating visual assets.</p>
                </div>`;
            return;
        }

        coursesContainer.innerHTML = courses.map(c => {
            const sessions = c.media_sessions || [];
            const assetCount = sessions.length;
            const assetBadge = assetCount > 0
                ? `<span class="badge badge-blue">📦 ${assetCount} asset${assetCount !== 1 ? 's' : ''}</span>`
                : `<span class="badge" style="background:rgba(255,255,255,.07);color:var(--text-muted);">No assets</span>`;
            const footerRight = currentUserRole === 'admin' && c.owner_username
                ? `<span style="font-size:.75rem;color:var(--muted);display:flex;align-items:center;gap:4px;"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>${c.owner_username}</span>`
                : '';
            return `
            <div class="course-card fade-in" onclick="navigateTo('course-detail', { courseId: '${c.id}' }); loadCourseDetail('${c.id}')">
                <div class="course-card-icon">🎓</div>
                <div>
                    <div class="course-card-title">${c.title}</div>
                    <div class="course-card-meta">${c.scenario_description}</div>
                </div>
                <div class="course-card-footer">
                    <div style="display:flex;gap:6px;flex-wrap:wrap;">${assetBadge}</div>
                    ${footerRight}
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        coursesContainer.innerHTML = '<p style="color:red">Failed to load courses.</p>';
    }
}

// ── Load Course Detail ────────────────────────────────────────────────────────
async function loadCourseDetail(courseId) {
    currentCourseId = courseId;
    try {
        const res = await authFetch(`/api/v1/courses/${courseId}`);
        const course = await res.json();
        currentCourseData = course;

        document.getElementById('detail-course-title').textContent = course.title;
        document.getElementById('detail-course-scenario').textContent = course.scenario_description;
        document.getElementById('detail-course-about').textContent = course.about_course;
        
        const restrictionsContainer = document.getElementById('detail-course-restrictions-container');
        if (course.custom_restrictions && course.custom_restrictions.trim()) {
            document.getElementById('detail-course-restrictions').textContent = course.custom_restrictions;
            restrictionsContainer.classList.remove('hidden');
        } else {
            restrictionsContainer.classList.add('hidden');
        }

        courseDetailsDisplay.classList.remove('hidden');
        editCourseFormContainer.classList.add('hidden');

        statusPollingIntervals.forEach(id => clearInterval(id));
        statusPollingIntervals = [];

        if (course.media_sessions.length === 0) {
            courseVideosContainer.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">🎬</div>
                    <div class="empty-state-title">No videos yet</div>
                    <p>Upload a video or transcript to begin generating visual assets.</p>
                </div>`;
        } else {
            courseVideosContainer.innerHTML = [...course.media_sessions]
                .sort((a, b) => new Date(b.upload_timestamp) - new Date(a.upload_timestamp))
                .map(s => {
                const status = s.processing_status || 'completed';
                const isProcessing = status === 'processing' || status === 'pending';
                const isFailed = status === 'failed';
                const isCompleted = status === 'completed';
                let statusBadge = '';
                if (isProcessing) statusBadge = '<span class="badge badge-amber" style="gap:6px;"><span class="pulse-ring" style="background:var(--warning);"></span>Processing…</span>';
                if (isFailed) statusBadge = '<span class="badge badge-red">Failed</span>';
                if (isCompleted) statusBadge = '<span class="badge badge-green">Ready</span>';
                const clickAction = isCompleted ? `onclick="loadEditorForSession('${s.id}')"` : '';
                const opacity = isCompleted ? '' : 'opacity:.65;';
                const isTranscript = s.source_type === 'transcript';
                const hasSourceFile = !!s.s3_key;
                const resourceIcon = isTranscript
                    ? `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="width:26px;height:26px;color:var(--primary)"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>`
                    : '🎬';
                const regenerateBtn = hasSourceFile
                    ? `<button class="btn btn-sm btn-secondary" title="Regenerate" onclick="regenerateSession(event, '${s.id}', '${s.title}')">🔄</button>`
                    : '';
                const downloadSourceBtn = hasSourceFile
                    ? `<button class="btn btn-sm btn-secondary" title="Download original file" onclick="downloadSourceFile(event, '${s.id}')">⬇️</button>`
                    : '';
                return `
                <div class="video-item fade-in" style="${opacity}" id="session-card-${s.id}">
                    <div class="video-item-icon">${resourceIcon}</div>
                    <div class="video-item-info" ${clickAction} style="${isCompleted ? 'cursor:pointer;' : ''}">
                        <div class="video-item-name">${s.title}</div>
                        <div class="video-item-sub">Uploaded ${new Date(s.upload_timestamp).toLocaleString()}${s.duration_seconds ? ` • ${_formatDuration(s.duration_seconds)}` : ''}</div>
                    </div>
                    <div class="video-item-tag" onclick="event.stopPropagation()">
                        <input type="text"
                            id="tag-input-${s.id}"
                            placeholder="tag"
                            value="${s.tag || ''}"
                            title="Tag used in downloaded image filenames: tag_timestamp.jpeg"
                            style="width:80px;padding:3px 7px;font-size:.75rem;border:1.5px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);"
                            onblur="saveSessionTag('${s.id}', this.value)"
                            onkeydown="if(event.key==='Enter'){this.blur();}">
                    </div>
                    ${statusBadge}
                    <div class="video-item-actions">
                        ${downloadSourceBtn}
                        ${regenerateBtn}
                        <button class="btn btn-sm btn-danger" onclick="deleteSession(event, '${s.id}')">🗑️</button>
                    </div>
                </div>`;
            }).join('');

            course.media_sessions
                .filter(s => (s.processing_status || 'completed') === 'processing' || (s.processing_status || 'completed') === 'pending')
                .forEach(s => pollSessionStatus(s.id));
        }
        switchView('course-detail');
    } catch (e) {
        alert("Failed to load course details.");
    }
}

async function loadEditorForSession(sessionId) {
    currentSessionId = sessionId;
    navigateTo('editor', { sessionId, courseId: currentCourseId });
    await fetchAndRenderPlan();
}

async function downloadSourceFile(event, sessionId) {
    event.stopPropagation();
    try {
        const res = await authFetch(`/api/v1/sessions/${sessionId}/download-source`);
        if (!res.ok) { alert('Could not get download link.'); return; }
        const data = await res.json();
        const a = document.createElement('a');
        a.href = data.url;
        a.download = data.filename || 'source';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    } catch (e) {
        alert('Download failed: ' + e.message);
    }
}

async function regenerateSession(event, sessionId, title) {
    event.stopPropagation();
    if (!confirm(`Regenerate "${title}"?\n\nThis will delete all existing segments and visual plans for this session and re-process the file from scratch. This cannot be undone.`)) return;
    const card = document.getElementById(`session-card-${sessionId}`);
    if (card) card.style.opacity = '0.5';
    try {
        const res = await authFetch(`/api/v1/sessions/${sessionId}/regenerate`, { method: 'POST' });
        if (res.ok) {
            await loadCourseDetail(currentCourseId);
            pollSessionStatus(sessionId);
        } else {
            const err = await res.json().catch(() => ({}));
            alert(`Regeneration failed: ${err.detail || res.statusText}`);
            if (card) card.style.opacity = '';
        }
    } catch (e) {
        alert('Regeneration request failed. Please try again.');
        if (card) card.style.opacity = '';
    }
}

async function deleteSession(event, sessionId) {
    event.stopPropagation();
    if (confirm("Are you sure you want to delete this video and all associated visual plans? This cannot be undone.")) {
        try {
            const res = await authFetch(`/api/v1/sessions/${sessionId}`, { method: 'DELETE' });
            if (res.ok) {
                await loadCourseDetail(currentCourseId);
            } else {
                alert("Failed to delete video.");
            }
        } catch (e) {
            console.error(e);
            alert("Error deleting video.");
        }
    }
}

// ── View Map ──────────────────────────────────────────────────────────────────
const viewSectionMap = {
    'course-list': sectionCourseList,
    'course-detail': sectionCourseDetail,
    'upload': sectionUpload,
    'editor': sectionEditor,
    'timeline': sectionTimeline,
    'admin': sectionAdmin,
};

function showSection(sectionId) {
    const allSections = Object.values(viewSectionMap);
    allSections.forEach(sec => { if (sec) sec.classList.add('hidden'); });
    const target = document.getElementById(sectionId);
    if (target) {
        target.classList.remove('hidden');
        target.classList.add('fade-in');
        setTimeout(() => target.classList.remove('fade-in'), 400);
    }
    if (sectionId === 'admin-section') loadAdminUsers();
    if (sectionId === 'course-list-section') loadCourses();
    if (typeof updateBreadcrumb === 'function') updateBreadcrumb([]);
}

function switchView(viewName) {
    const activeSection = viewSectionMap[viewName];
    if (!activeSection) return;
    Object.values(viewSectionMap).forEach(sec => { if (sec) sec.classList.add('hidden'); });
    activeSection.classList.remove('hidden');
    activeSection.classList.add('fade-in');
    setTimeout(() => activeSection.classList.remove('fade-in'), 400);

    const courseTitle = currentCourseData ? currentCourseData.title : null;
    const crumbs = {
        'course-list': [],
        'course-detail': courseTitle ? ['Courses', courseTitle] : ['Courses'],
        'upload': courseTitle ? ['Courses', courseTitle, 'Upload'] : ['Upload'],
        'editor': courseTitle ? ['Courses', courseTitle, 'Editor'] : ['Editor'],
        'timeline': courseTitle ? ['Courses', courseTitle, 'Timeline'] : ['Timeline'],
        'admin': [],
    };
    if (typeof updateBreadcrumb === 'function') updateBreadcrumb(crumbs[viewName] || []);
}

function navigateTo(viewName, context = {}) {
    switchView(viewName);
    const state = { view: viewName, ...context };
    const hash = viewName === 'course-list' ? '#courses'
        : viewName === 'admin'       ? '#admin'
        : viewName === 'course-detail' ? `#course/${context.courseId || currentCourseId}`
        : viewName === 'upload' ? `#upload/${context.courseId || currentCourseId}`
        : viewName === 'editor' ? `#editor/${context.sessionId || currentSessionId}`
        : viewName === 'timeline' ? `#timeline/${context.sessionId || currentSessionId}`
        : '#';
    history.pushState(state, '', hash);
    if (viewName === 'admin') loadAdminUsers();
}

window.addEventListener('popstate', async (e) => {
    const state = e.state;
    if (!state || !state.view) { switchView('course-list'); loadCourses(); return; }
    switchView(state.view);
    switch (state.view) {
        case 'course-list': await loadCourses(); break;
        case 'admin': loadAdminUsers(); break;
        case 'course-detail':
            if (state.courseId) { currentCourseId = state.courseId; await loadCourseDetail(state.courseId); } break;
        case 'upload': if (state.courseId) currentCourseId = state.courseId; break;
        case 'editor':
            if (state.sessionId) { currentSessionId = state.sessionId; await fetchAndRenderPlan(); } break;
        case 'timeline':
            if (state.sessionId) { currentSessionId = state.sessionId; await renderFinalTimeline(); } break;
    }
});

async function restoreFromHash() {
    const hash = window.location.hash;
    if (hash === '#admin' && currentUserRole === 'admin') {
        switchView('admin');
        history.replaceState({ view: 'admin' }, '', '#admin');
        loadAdminUsers();
    } else if (hash.startsWith('#course/')) {
        const courseId = hash.split('/')[1];
        currentCourseId = courseId;
        switchView('course-detail');
        history.replaceState({ view: 'course-detail', courseId }, '', hash);
        await loadCourseDetail(courseId);
    } else if (hash.startsWith('#upload/')) {
        const courseId = hash.split('/')[1];
        currentCourseId = courseId;
        switchView('upload');
        history.replaceState({ view: 'upload', courseId }, '', hash);
    } else if (hash.startsWith('#editor/')) {
        const sessionId = hash.split('/')[1];
        currentSessionId = sessionId;
        switchView('editor');
        history.replaceState({ view: 'editor', sessionId }, '', hash);
        await fetchAndRenderPlan();
    } else if (hash.startsWith('#timeline/')) {
        const sessionId = hash.split('/')[1];
        currentSessionId = sessionId;
        switchView('timeline');
        history.replaceState({ view: 'timeline', sessionId }, '', hash);
        await renderFinalTimeline();
    } else {
        history.replaceState({ view: 'course-list' }, '', '#courses');
        loadCourses();
    }
}

// ── Upload ────────────────────────────────────────────────────────────────────
uploadForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const mediaFile = document.getElementById('media-file').files[0];
    const transcriptFile = document.getElementById('transcript-file').files[0];
    if (!mediaFile && !transcriptFile) { alert("Please select a file to upload."); return; }
    const courseId = document.getElementById('upload-course-id').value;
    const formData = new FormData();
    formData.append('course_id', courseId);
    if (mediaFile)     formData.append('file',       mediaFile);
    if (transcriptFile) formData.append('transcript', transcriptFile);
    try {
        setLoading(true);
        statusMsg.textContent = "Uploading file...";
        const response = await authFetch('/api/v1/sessions/upload', { method: 'POST', body: formData });
        const data = await response.json();
        if (response.ok) {
            statusMsg.textContent = "Upload successful! Processing started in background.";
            // Reset unified drop zone
            ['unified-file', 'media-file', 'transcript-file'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.value = '';
            });
            const zone = document.getElementById('unified-drop-zone');
            const icon = document.getElementById('unified-drop-icon');
            const text = document.getElementById('unified-drop-label');
            if (zone) zone.style.borderColor = '';
            if (icon) icon.textContent = '📁';
            if (text) { text.textContent = 'Drop your file here or click to browse'; text.style.color = ''; text.style.fontWeight = ''; }
            setTimeout(async () => {
                setLoading(false);
                history.back();
                setTimeout(() => loadCourseDetail(currentCourseId), 300);
            }, 800);
            return;
        } else {
            throw new Error(data.detail || "Upload failed");
        }
    } catch (error) {
        console.error(error);
        statusMsg.textContent = "Error: " + error.message;
        statusMsg.style.color = "var(--danger)";
    } finally {
        setLoading(false);
    }
});

function pollSessionStatus(sessionId) {
    const intervalId = setInterval(async () => {
        try {
            const res = await authFetch(`/api/v1/sessions/${sessionId}/status`);
            if (!res.ok) return;
            const data = await res.json();
            if (data.processing_status === 'completed' || data.processing_status === 'failed') {
                clearInterval(intervalId);
                statusPollingIntervals = statusPollingIntervals.filter(id => id !== intervalId);
                if (currentCourseId) await loadCourseDetail(currentCourseId);
            }
        } catch (e) { console.warn("Polling error for session", sessionId, e); }
    }, 5000);
    statusPollingIntervals.push(intervalId);
}

document.getElementById('back-to-editor-btn').addEventListener('click', () => { history.back(); });

// ── Lightbox ──────────────────────────────────────────────────────────────────
lightbox.addEventListener('click', (e) => {
    if (e.target === lightbox || (e.target.classList && e.target.classList.contains('lightbox-content'))) {
        closeLightbox();
    }
});

function closeLightbox() {
    lightbox.classList.remove('active');
    document.getElementById('edit-comment-input').value = '';
    lightboxImageId = null;
    lightboxPlanId = null;
    lightboxImages = [];
    lightboxIndex = 0;
}

async function saveSessionTag(sessionId, value) {
    const tag = value.trim();
    const input = document.getElementById(`tag-input-${sessionId}`);
    try {
        const res = await authFetch(`/api/v1/sessions/${sessionId}/tag`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tag: tag || null })
        });
        if (!res.ok) throw new Error('Failed to save tag');
        if (input) {
            input.style.borderColor = '#22c55e';
            setTimeout(() => { if (input) input.style.borderColor = ''; }, 1500);
        }
    } catch (err) {
        console.error('Save tag error:', err);
        if (input) input.style.borderColor = 'var(--danger)';
    }
}

async function downloadLightboxImage() {
    if (!lightboxImageId) return;

    let filename = 'image.jpeg';
    const ts = lightboxPlanId != null ? planTimestamps[lightboxPlanId] : null;
    if (ts != null) {
        const totalSec = Math.floor(ts);
        const h = Math.floor(totalSec / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const s = totalSec % 60;
        const pad = n => String(n).padStart(2, '0');
        const timeStr = `${pad(h)}h${pad(m)}m${pad(s)}s`;
        const tag = lightboxPlanId != null ? planSessionTags[lightboxPlanId] : null;
        filename = tag ? `${tag}_${timeStr}.jpeg` : `${timeStr}.jpeg`;
    }

    try {
        const response = await authFetch(`/api/v1/images/${lightboxImageId}/download`);
        if (!response.ok) throw new Error(`Server returned ${response.status}`);
        const blob = await response.blob();
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(blobUrl), 100);
    } catch (err) {
        console.error("Download failed:", err);
        alert(`Download failed: ${err.message}`);
    }
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && lightbox.classList.contains('active')) {
        e.preventDefault(); e.stopPropagation(); closeLightbox();
    }
    if (lightbox.classList.contains('active') && document.activeElement?.id !== 'edit-comment-input') {
        if (e.key === 'ArrowLeft') { e.preventDefault(); lightboxNav(-1); }
        if (e.key === 'ArrowRight') { e.preventDefault(); lightboxNav(1); }
    }
});

document.getElementById('edit-comment-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); applyImageEdit(); }
});

// ── Helpers ───────────────────────────────────────────────────────────────────
function setLoading(isLoading) {
    if (isLoading) { uploadSpinner.classList.remove('hidden'); uploadBtn.disabled = true; }
    else { uploadSpinner.classList.add('hidden'); uploadBtn.disabled = false; }
}

let curateFilter = 'all'; // 'all' | 'selected'

async function runCuration() {
    const btn = document.getElementById('curate-btn');
    btn.disabled = true;
    btn.innerHTML = `<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24" style="animation:spin 1s linear infinite"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg> Picking…`;
    try {
        const res = await authFetch(`/api/v1/sessions/${currentSessionId}/curate`, { method: 'POST' });
        if (!res.ok) throw new Error('Curation failed');
        const data = await res.json();
        // Normalize UUIDs to no-hyphens lowercase for reliable matching
        const normalize = id => String(id).replace(/-/g, '').toLowerCase();
        const curatedSet = new Set(data.curated_ids.map(normalize));

        // Update card classes in place — no full re-render
        document.querySelectorAll('.alt-plan-card').forEach(card => {
            const planId = normalize(card.id.replace('card-', ''));
            const isCurated = curatedSet.has(planId);
            card.classList.toggle('curated', isCurated);
            card.dataset.curated = isCurated ? '1' : '0';
            // Update/add the curated star badge in the header
            const titleArea = card.querySelector('.card-title-area');
            const existing = titleArea.querySelector('.curated-star');
            if (isCurated && !existing) {
                titleArea.insertAdjacentHTML('beforeend',
                    `<span class="curated-star">★ AI Pick</span><button class="reject-curated-btn" onclick="rejectCurated(event,'${planId}')" title="Replace with another suggestion">✕</button>`);
            } else if (!isCurated && existing) {
                existing.remove();
                titleArea.querySelector('.reject-curated-btn')?.remove();
            }
        });

        // Show the toggle and clear button, default to showing only selected
        document.getElementById('curate-toggle').classList.remove('hidden');
        document.getElementById('clear-curate-btn').classList.remove('hidden');
        setCurateFilter('selected');
    } catch (e) {
        alert('Curation failed. Please try again.');
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg> Re-pick`;
    }
}

async function clearCuration() {
    const btn = document.getElementById('clear-curate-btn');
    btn.disabled = true;
    btn.textContent = 'Clearing…';
    try {
        await authFetch(`/api/v1/sessions/${currentSessionId}/curate`, { method: 'DELETE' });
        document.querySelectorAll('.alt-plan-card').forEach(card => {
            card.classList.remove('curated');
            card.dataset.curated = '0';
            const titleArea = card.querySelector('.card-title-area');
            titleArea.querySelector('.curated-star')?.remove();
            titleArea.querySelector('.reject-curated-btn')?.remove();
            card.style.display = '';
        });
        document.querySelectorAll('.segment-block').forEach(b => b.style.display = '');
        document.getElementById('curate-toggle').classList.add('hidden');
        document.getElementById('clear-curate-btn').classList.add('hidden');
        curateFilter = 'all';
        const curateBtn = document.getElementById('curate-btn');
        curateBtn.innerHTML = `<svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg> AI Pick ~10/min`;
    } catch {
        alert('Failed to clear picks.');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Clear Picks';
    }
}

function copyPrompt(e, btn, planId) {
    e.stopPropagation();
    const card = document.getElementById(`card-${planId}`);
    const prompt = card?.querySelector(`[data-field="final_prompt"]`)?.value || '';
    if (!prompt) return;
    navigator.clipboard.writeText(prompt).then(() => {
        const orig = btn.innerHTML;
        btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> Copied!`;
        btn.classList.add('copied');
        setTimeout(() => { btn.innerHTML = orig; btn.classList.remove('copied'); }, 1800);
    });
}

async function rejectCurated(e, planId) {
    e.stopPropagation();
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = '…';

    try {
        const res = await authFetch(`/api/v1/visual-plans/${planId}/reject-curated`, { method: 'POST' });
        if (!res.ok) throw new Error();
        const data = await res.json();

        // Un-curate the rejected card
        const rejectedCard = document.getElementById(`card-${planId}`);
        if (rejectedCard) {
            rejectedCard.classList.remove('curated');
            rejectedCard.dataset.curated = '0';
            const titleArea = rejectedCard.querySelector('.card-title-area');
            titleArea.querySelector('.curated-star')?.remove();
            titleArea.querySelector('.reject-curated-btn')?.remove();
            if (curateFilter === 'selected') rejectedCard.style.display = 'none';
        }

        // Curate the replacement card
        if (data.replacement_id) {
            const normalize = id => String(id).replace(/-/g, '').toLowerCase();
            const replacementCard = [...document.querySelectorAll('.alt-plan-card')]
                .find(c => normalize(c.id.replace('card-', '')) === normalize(data.replacement_id));
            if (replacementCard) {
                replacementCard.classList.add('curated');
                replacementCard.dataset.curated = '1';
                const titleArea = replacementCard.querySelector('.card-title-area');
                if (!titleArea.querySelector('.curated-star')) {
                    titleArea.insertAdjacentHTML('beforeend',
                        `<span class="curated-star">★ AI Pick</span><button class="reject-curated-btn" onclick="rejectCurated(event,'${data.replacement_id}')" title="Replace with another suggestion">✕</button>`);
                }
                if (curateFilter === 'selected') replacementCard.style.display = '';
            }
        }

        // Re-apply segment visibility
        if (curateFilter === 'selected') setCurateFilter('selected');
    } catch {
        btn.disabled = false;
        btn.textContent = '✕';
    }
}

function setCurateFilter(mode) {
    curateFilter = mode;
    document.getElementById('toggle-selected-btn').classList.toggle('active', mode === 'selected');
    document.getElementById('toggle-all-btn').classList.toggle('active', mode === 'all');

    document.querySelectorAll('.alt-plan-card').forEach(card => {
        if (mode === 'all') {
            card.style.display = '';
        } else {
            card.style.display = card.dataset.curated === '1' ? '' : 'none';
        }
    });

    // Hide segment blocks that have no visible cards
    document.querySelectorAll('.segment-block').forEach(block => {
        const grid = block.querySelector('.alternatives-grid');
        if (!grid) return;
        const anyVisible = [...grid.querySelectorAll('.alt-plan-card')].some(c => c.style.display !== 'none');
        block.style.display = anyVisible ? '' : 'none';
    });
}

async function fetchAndRenderPlan() {
    try {
        const response = await authFetch(`/api/v1/plans/${currentSessionId}`);
        const plan = await response.json();
        switchView('editor');
        if (currentCourseData) {
            const scenarioEl = document.getElementById('editor-course-scenario');
            const aboutEl = document.getElementById('editor-course-about');
            if (scenarioEl) scenarioEl.textContent = currentCourseData.scenario_description || '';
            if (aboutEl) aboutEl.textContent = currentCourseData.about_course || '';
        }
        await renderSegments(plan);
    } catch (err) {
        console.error("Failed to fetch plan:", err);
        alert("Failed to load visual plan");
    }
}

function buildPromptForCard(planId) {
    const card = document.getElementById(`card-${planId}`);
    if (!card) return;
    const vt = card.querySelector(`[data-field="visual_type"]`).value;
    const concept = card.querySelector(`[data-field="concept"]`).value;
    const desc = card.querySelector(`[data-field="description"]`).value;
    const tags = card.querySelector(`[data-field="tags"]`).value;
    const promptField = card.querySelector(`[data-field="final_prompt"]`);
    if (!promptField) return;
    if (promptField.dataset.userEdited === 'true') return;
    const scenario = currentCourseData?.scenario_description || '';
    const about = currentCourseData?.about_course || '';
    let visualStyle = "";
    let generatedType = vt;
    switch (vt) {
        case 'unicolour_icon':
            generatedType = "minimal unicolour vector icon";
            visualStyle = "Simple symbolic icon, single colour, simple geometric shapes, bold lines, centered composition, flat vector design.\n\nThe icon should be minimal, abstract, and clearly communicate the concept of " + concept + ".\n\nRestrictions:\nNo text\nNo logos\nNo branding\nNo background scene";
            break;
        case 'tricolour_icon':
            generatedType = "flat 3-colour vector icon";
            visualStyle = "Simple recognizable icon, maximum 3 colours, minimal details, centered composition.\n\nThe icon should be simple, recognizable, and clearly communicate the concept of " + concept + ".\n\nRestrictions:\nNo text\nNo logos\nNo branding\nNo complicated background scene";
            break;
        case 'unicolour_illustration':
            generatedType = "minimal unicolour flat digital illustration";
            visualStyle = "Flat digital illustration style, NOT a photograph. Single colour palette with tints and shades. Clean vector-like shapes, minimal detail, bold outlines, simple storytelling composition. Think editorial infographic art or textbook diagram illustration. No photographic textures, no gradients that look like lighting, no realistic shadows.";
            break;
        case 'multicolour_illustration':
            generatedType = "multicolour flat digital illustration";
            visualStyle = "Colourful flat digital illustration style, NOT a photograph. Think modern explainer video art or editorial infographic — clean vector shapes, bold colour fills, soft cel-shading at most. Human figures should be stylised, not realistic. Contextual scene with narrative clarity. No photographic textures, no realistic lighting, no cinematic depth-of-field.";
            break;
        case 'image':
        default:
            generatedType = "image";
            visualStyle = "Documentary style image, natural lighting, cinematic composition, realistic environments, educational B-roll quality.";
            break;
    }
    let basePrompt = `Create a ${generatedType}.`;
    if (desc && desc.trim()) basePrompt += `\n\n${desc}`;
    if (scenario && scenario.trim()) basePrompt += `\n\nVisual Scene Setup\n${scenario}`;
    if (about && about.trim()) basePrompt += `\n\nAbout This Course\n${about}`;
    basePrompt += `\n\nVisual Style\n${visualStyle}`;
    basePrompt += `\n\nQuality Requirements\nHigh resolution\nClear composition\nSuitable for educational course videos\n16:9 aspect ratio\n\nRestrictions`;
    
    if (currentCourseData && currentCourseData.custom_restrictions && currentCourseData.custom_restrictions.trim()) {
        basePrompt += `\n${currentCourseData.custom_restrictions}`;
    } else {
        basePrompt += `\n${DEFAULT_RESTRICTIONS}`;
    }
    
    promptField.value = basePrompt;
}

async function resetAllPrompts() {
    if (!currentSessionId) return;
    if (!confirm('This will clear all stored prompts for this session and regenerate them fresh with the new format. Continue?')) return;
    try {
        const res = await authFetch(`/api/v1/sessions/${currentSessionId}/prompts`, { method: 'DELETE' });
        if (!res.ok) { alert('Failed to clear stored prompts.'); return; }
        // Re-mark all loaded prompt fields as not user-edited so buildPromptForCard runs
        document.querySelectorAll('[data-field="final_prompt"]').forEach(field => {
            field.dataset.userEdited = 'false';
            field.dataset.dbPrompt = '';
        });
        // Rebuild all visible prompts
        document.querySelectorAll('[data-field="final_prompt"]').forEach(field => {
            buildPromptForCard(field.dataset.id);
            field.style.minHeight = 'auto';
            field.style.minHeight = field.scrollHeight + 'px';
        });
        alert('All prompts have been regenerated with the new format.');
    } catch (err) {
        console.error('Reset prompts error:', err);
        alert('Network error while resetting prompts.');
    }
}

async function adminResetAllPrompts() {
    if (!confirm('⚠️ This will clear ALL stored prompts for EVERY user and session in the system.\n\nEveryone will get fresh prompts regenerated on their next load.\n\nContinue?')) return;
    try {
        const res = await authFetch('/api/v1/admin/reset-all-prompts', { method: 'DELETE' });
        if (!res.ok) { alert('Failed to reset prompts globally.'); return; }
        const data = await res.json();
        alert(`✅ Done! ${data.count} stored prompt${data.count !== 1 ? 's' : ''} cleared across all users and sessions.`);
    } catch (err) {
        console.error('Admin reset prompts error:', err);
        alert('Network error while resetting prompts.');
    }
}

async function adminBackfillDurations() {
    if (!confirm('📺 Calculate video durations for all existing uploads?\n\nThis will scan all media sessions and populate missing duration data based on audio segments.\n\nContinue?')) return;
    const btn = document.getElementById('backfill-btn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Backfilling...';
    try {
        const res = await authFetch('/api/v1/admin/backfill-durations', { method: 'POST' });
        if (!res.ok) { alert('Failed to backfill durations.'); return; }
        const data = await res.json();
        alert(`✅ Done! Updated ${data.updated} media session${data.updated !== 1 ? 's' : ''} with duration data.`);
        loadAdminUsers();
    } catch (err) {
        console.error('Admin backfill durations error:', err);
        alert('Network error while backfilling durations.');
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
}

async function renderSegments(plan) {
    currentEditorPlan = plan;
    segmentsContainer.innerHTML = '';
    curateFilter = 'all';
    const toggle = document.getElementById('curate-toggle');
    const hasCurated = plan.segments.some(s => s.alternative_plans.some(a => a.is_curated));
    toggle.classList.toggle('hidden', !hasCurated);
    const segments = plan.segments;
    const formatTime = (seconds) => {
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${m}:${s.toString().padStart(2, '0')}`;
    };

    // Pre-register timestamps and session tags so they're always available at download time
    segments.forEach(seg => {
        seg.alternative_plans.forEach(alt => {
            planTimestamps[alt.id] = seg.timestamp_start;
            planSessionTags[alt.id] = plan.session_tag || null;
        });
    });

    // Single observer: renders cards + loads images only when segment scrolls into view
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting && !entry.target.dataset.rendered) {
                entry.target.dataset.rendered = 'true';
                const segIndex = parseInt(entry.target.dataset.segIndex, 10);
                renderSegmentCards(entry.target, segments[segIndex]);
                loadSegmentImages(segments[segIndex]);
                observer.unobserve(entry.target);
            }
        });
    }, { rootMargin: '300px' });

    segments.forEach((seg, index) => {
        const segBlock = document.createElement('div');
        segBlock.className = 'segment-block fade-in';
        segBlock.dataset.segIndex = index;
        segBlock.innerHTML = `
            <div class="segment-header">
                <div class="seg-time">[${formatTime(seg.timestamp_start)} - ${formatTime(seg.timestamp_end)}]</div>
                <div class="seg-transcript">"${seg.transcript_text}"</div>
            </div>
            <div class="alternatives-grid">
                <div style="height:80px; display:flex; align-items:center; justify-content:center; opacity:0.35; font-size:0.82rem; color:var(--text-muted);">⏳ Loading…</div>
            </div>`;
        segmentsContainer.appendChild(segBlock);
        observer.observe(segBlock);
    });
}

function renderSegmentCards(segBlock, seg) {
    const grid = segBlock.querySelector('.alternatives-grid');
    grid.innerHTML = seg.alternative_plans.map(alt => createAlternativeCard(alt)).join('');

    // Apply current curate filter to newly rendered cards immediately
    if (curateFilter === 'selected') {
        segBlock.querySelectorAll('.alt-plan-card').forEach(card => {
            card.style.display = card.dataset.curated === '1' ? '' : 'none';
        });
    }

    segBlock.querySelectorAll('.sync-prompt').forEach(input => {
        input.addEventListener('input', (e) => { buildPromptForCard(e.target.dataset.id); });
    });

    segBlock.querySelectorAll('[data-field="visual_type"]').forEach(select => {
        select.addEventListener('change', (e) => {
            const card = document.getElementById(`card-${e.target.dataset.id}`);
            const promptField = card?.querySelector('[data-field="final_prompt"]');
            if (promptField) promptField.dataset.userEdited = 'false';
            buildPromptForCard(e.target.dataset.id);
        });
    });

    segBlock.querySelectorAll('[data-field="final_prompt"]').forEach(field => {
        const dbPrompt = field.dataset.dbPrompt;
        if (dbPrompt && dbPrompt.trim()) {
            field.value = dbPrompt;
            field.dataset.userEdited = 'true';
        } else {
            buildPromptForCard(field.dataset.id);
        }
        field.style.minHeight = Math.max(140, field.scrollHeight) + 'px';
        field.addEventListener('input', () => {
            field.dataset.userEdited = 'true';
            field.style.minHeight = 'auto';
            field.style.minHeight = field.scrollHeight + 'px';
        });
    });

    segBlock.querySelectorAll('.btn-reset-prompt').forEach(btn => {
        btn.addEventListener('click', () => {
            const planId = btn.dataset.id;
            const card = document.getElementById(`card-${planId}`);
            const promptField = card.querySelector('[data-field="final_prompt"]');
            promptField.dataset.userEdited = 'false';
            buildPromptForCard(planId);
            promptField.style.minHeight = 'auto';
            promptField.style.minHeight = promptField.scrollHeight + 'px';
        });
    });

    segBlock.querySelectorAll('.alt-input').forEach(input => {
        input.addEventListener('change', handlePlanUpdate);
    });

    segBlock.querySelectorAll('.generate-alt-btn').forEach(btn => {
        btn.addEventListener('click', handleGenerateClick);
    });

    segBlock.querySelectorAll('.accordion-header').forEach(header => {
        header.addEventListener('click', () => {
            const toggleIcon = header.querySelector('.accordion-toggle');
            const content = header.nextElementSibling;
            const isOpen = toggleIcon.classList.contains('expanded');
            toggleIcon.classList.toggle('expanded');
            if (isOpen) {
                content.style.maxHeight = content.scrollHeight + 'px';
                requestAnimationFrame(() => {
                    content.style.maxHeight = '0';
                    content.style.opacity = '0';
                    content.style.marginTop = '0';
                    content.style.marginBottom = '0';
                });
                content.classList.remove('expanded');
            } else {
                content.classList.add('expanded');
                content.style.maxHeight = content.scrollHeight + 'px';
                content.style.opacity = '1';
                content.style.marginTop = '1rem';
                content.style.marginBottom = '1.25rem';
                content.addEventListener('transitionend', () => {
                    if (content.classList.contains('expanded')) content.style.maxHeight = 'none';
                }, { once: true });
            }
        });
    });
}

async function loadSegmentImages(seg) {
    await Promise.all(seg.alternative_plans.map(async alt => {
        let allImages = [];
        const tagQ = encodeURIComponent(alt.tags || "");
        const typeQ = encodeURIComponent(alt.visual_type || "");
        let sugUrl = `/api/v1/images/suggest?visual_type=${typeQ}&tags=${tagQ}&limit=5&course_id=${currentCourseId}&exclude_plan_id=${alt.id}`;
        if (alt.selected_image_id) sugUrl += `&except_image_id=${alt.selected_image_id}`;
        const [histRes, sugRes] = await Promise.allSettled([
            authFetch(`/api/v1/plans/${alt.id}/images`),
            authFetch(sugUrl)
        ]);
        try {
            if (histRes.status === 'fulfilled' && histRes.value.ok) {
                const historyImages = await histRes.value.json();
                historyImages.forEach(img => {
                    const isEdited = img.is_edited === true;
                    allImages.push({
                        id: img.id, src: `/images/${img.local_path.split('/').pop()}`,
                        label: isEdited ? 'Edited' : 'Generated',
                        badgeColor: isEdited ? 'rgba(147,51,234,0.85)' : 'rgba(34,139,34,0.85)',
                        isSelected: String(img.id) === String(alt.selected_image_id), planId: alt.id
                    });
                });
            }
        } catch (e) { console.warn("Failed to load history for", alt.id, e); }
        try {
            if (sugRes.status === 'fulfilled' && sugRes.value.ok) {
                const suggestions = await sugRes.value.json();
                suggestions.forEach(s => {
                    if (!allImages.find(i => String(i.id) === String(s.id))) {
                        allImages.push({
                            id: s.id, src: `/images/${s.local_path.split('/').pop()}`,
                            label: 'Suggestion', badgeColor: 'rgba(0,0,0,0.7)', isSelected: false, planId: alt.id
                        });
                    }
                });
            }
        } catch (e) { console.warn("Failed to load suggestions for", alt.id, e); }
        if (allImages.length > 0) {
            const carousel = document.getElementById(`carousel-${alt.id}`);
            const track = document.getElementById(`track-${alt.id}`);
            carousel.classList.remove('hidden');
            let trackHtml = '';
            allImages.forEach(img => {
                const borderStyle = img.isSelected ? '3px solid var(--primary)' : '2px solid transparent';
                trackHtml += `
                    <div class="carousel-item" id="citem-${img.id}" style="position:relative; min-width:110px; width:110px; cursor:pointer; flex-shrink:0;"
                         onclick="selectAndEdit('${img.planId}', '${img.id}', '${img.src}')">
                        <div style="position:absolute; top:3px; left:3px; background:${img.badgeColor}; color:white; font-size:9px; padding:2px 5px; border-radius:3px; font-weight:bold; z-index:1;">${img.label}</div>
                        <img src="${img.src}" style="width:100%; border-radius:6px; border:${borderStyle};"
                             onmouseover="this.style.borderColor='var(--primary)'"
                             onmouseout="this.style.borderColor='${img.isSelected ? 'var(--primary)' : 'transparent'}'">
                    </div>`;
            });
            track.innerHTML = trackHtml;
            const btn = document.querySelector(`.generate-alt-btn[data-id="${alt.id}"]`);
            if (btn && allImages.some(i => i.label === 'Generated')) {
                btn.innerHTML = "🔄 Regenerate another";
                btn.disabled = false;
            }
        }
    }));
}

function createAlternativeCard(alt) {
    const types = [
        { val: 'unicolour_icon', label: 'Unicolour Icon (Theme A)' },
        { val: 'tricolour_icon', label: 'Tricolour Icon (Theme B)' },
        { val: 'unicolour_illustration', label: 'Unicolour Illustration' },
        { val: 'multicolour_illustration', label: 'Multicolour Illustration' },
        { val: 'image', label: 'Image' }
    ];
    let currentVt = alt.visual_type;
    if (currentVt === 'icon') currentVt = 'unicolour_icon';
    if (currentVt === 'illustration') currentVt = 'multicolour_illustration';
    const typeOptions = types.map(t => `<option value="${t.val}" ${currentVt === t.val ? 'selected' : ''}>${t.label}</option>`).join('');
    const categories = ['action', 'object', 'process', 'emotion', 'situation'];
    const currentCategory = alt.category || 'action';
    const catOptions = categories.map(c => `<option value="${c}" ${currentCategory === c ? 'selected' : ''}>${c}</option>`).join('');
    let imageDisplayHtml = `<div class="img-preview hidden" id="img-${alt.id}"></div>`;
    let generateBtnHtml = `<button class="btn btn-primary generate-alt-btn" data-id="${alt.id}" style="margin-top: 0.75rem; width: 100%;">✨ Generate Image</button>`;
    let carouselHtml = `
        <div class="image-carousel hidden" id="carousel-${alt.id}" style="margin-top:1rem; position:relative;">
            <button class="carousel-arrow carousel-left" onclick="scrollCarousel('${alt.id}', -1)" style="position:absolute; left:-12px; top:50%; transform:translateY(-50%); z-index:2; background:rgba(0,0,0,0.6); color:white; border:none; border-radius:50%; width:28px; height:28px; cursor:pointer; font-size:16px; display:flex; align-items:center; justify-content:center;">❮</button>
            <div class="carousel-track" id="track-${alt.id}" style="display:flex; gap:10px; overflow-x:hidden; scroll-behavior:smooth; padding:4px 20px;"></div>
            <button class="carousel-arrow carousel-right" onclick="scrollCarousel('${alt.id}', 1)" style="position:absolute; right:-12px; top:50%; transform:translateY(-50%); z-index:2; background:rgba(0,0,0,0.6); color:white; border:none; border-radius:50%; width:28px; height:28px; cursor:pointer; font-size:16px; display:flex; align-items:center; justify-content:center;">❯</button>
        </div>`;
    const curatedClass = alt.is_curated ? ' curated' : '';
    const curatedStar = alt.is_curated
        ? `<span class="curated-star">★ AI Pick</span><button class="reject-curated-btn" onclick="rejectCurated(event,'${alt.id}')" title="Replace with another suggestion">✕</button>`
        : '';
    const typeLabel = types.find(t => t.val === currentVt)?.label || alt.visual_type || 'Image';
    const shortLabel = typeLabel.replace(/\s*\(.*?\)/g, '').trim();
    return `
        <div class="alt-plan-card${curatedClass}" id="card-${alt.id}" data-curated="${alt.is_curated ? '1' : '0'}">
            <div class="accordion-header">
                <div class="card-title-area">
                    <div class="card-header-main">
                        <div class="card-header-top">
                            <span class="alt-badge">${shortLabel}</span>
                            <span class="card-title-text">${alt.concept || alt.theme || 'Image Plan'}</span>
                            ${curatedStar}
                        </div>
                        ${alt.description ? `<div class="card-header-desc">${alt.description}</div>` : ''}
                    </div>
                </div>
                <div style="display:flex;align-items:center;gap:.4rem;flex-shrink:0;">
                    <button class="copy-prompt-btn" onclick="copyPrompt(event,this,'${alt.id}')" title="Copy prompt">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                        Copy
                    </button>
                    <div class="accordion-toggle">▼</div>
                </div>
            </div>
            <div class="accordion-content" style="max-height:0; opacity:0; overflow:hidden; margin-top:0; margin-bottom:0; transition: max-height 0.3s ease, opacity 0.3s ease, margin-top 0.3s ease, margin-bottom 0.3s ease;">
                <div class="input-row">
                    <label>Image Type:</label>
                    <select class="input-sm alt-input sync-prompt" data-id="${alt.id}" data-field="visual_type">${typeOptions}</select>
                </div>
                <div class="input-row">
                    <label>Category:</label>
                    <select class="input-sm alt-input sync-prompt" data-id="${alt.id}" data-field="category">${catOptions}</select>
                </div>
                <div class="input-row">
                    <label>Content / Topic:</label>
                    <input type="text" class="input-sm alt-input sync-prompt" data-id="${alt.id}" data-field="concept" value="${alt.concept}">
                </div>
                <div class="input-row">
                    <label>Local Context:</label>
                    <textarea class="input-sm alt-input sync-prompt" data-id="${alt.id}" data-field="description" style="min-height:60px">${alt.description}</textarea>
                </div>
                <div class="input-row" style="display:none;">
                    <label>Global Context:</label>
                    <textarea class="input-sm alt-input sync-prompt" data-id="${alt.id}" data-field="global_context" style="min-height:60px">${alt.global_context || ''}</textarea>
                </div>
                <div class="input-row">
                    <label>Tags:</label>
                    <input type="text" class="input-sm alt-input sync-prompt" data-id="${alt.id}" data-field="tags" value="${alt.tags || ''}">
                </div>
                <div class="input-row">
                    <label style="display: flex; align-items: center; justify-content: space-between; gap: 6px; flex-wrap: wrap;">
                        <span style="display:flex; align-items:center; gap:6px;">
                            Composite Prompt:
                            <span style="font-size: 0.7rem; color: var(--text-muted); font-weight: normal;">(editable — this is sent to Gemini)</span>
                        </span>
                        <button type="button" class="btn-reset-prompt" data-id="${alt.id}" style="font-size:0.7rem; padding:2px 8px; cursor:pointer; background:transparent; border:1px solid var(--border-color); border-radius:4px; color:var(--text-muted);">↺ Reset to auto</button>
                    </label>
                    <textarea class="input-sm alt-input final-prompt-field" data-id="${alt.id}" data-field="final_prompt" data-db-prompt="${(alt.final_prompt || '').replace(/"/g, '&quot;')}" data-user-edited="false" style="min-height:140px; font-size: 0.82rem; line-height: 1.5; resize: vertical; width: 100%; box-sizing: border-box; transition: min-height 0.2s ease;"></textarea>
                </div>
            </div>
            ${carouselHtml}
            ${generateBtnHtml}
        </div>`;
}

async function handlePlanUpdate(e) {
    const planId = e.target.dataset.id;
    const card = document.getElementById(`card-${planId}`);
    const payload = {
        concept: card.querySelector(`[data-field="concept"]`).value,
        category: card.querySelector(`[data-field="category"]`).value,
        description: card.querySelector(`[data-field="description"]`).value,
        visual_type: card.querySelector(`[data-field="visual_type"]`).value,
        global_context: "",
        tags: card.querySelector(`[data-field="tags"]`).value,
        final_prompt: card.querySelector(`[data-field="final_prompt"]`).value,
        is_disabled: false
    };
    try {
        await authFetch(`/api/v1/visual-plans/${planId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
    } catch (err) { console.error("Save error: ", err); }
}

async function handleGenerateClick(e) {
    e.stopPropagation();
    const btn = e.currentTarget;
    const planId = btn.dataset.id;
    btn.disabled = true;
    btn.innerHTML = `Generating... <div class="spinner"></div>`;
    try {
        await handlePlanUpdate({ target: { dataset: { id: planId } } });
    } catch (saveErr) { console.warn('Pre-generate save failed (non-fatal):', saveErr); }
    try {
        const response = await authFetch(`/api/v1/visual-plans/${planId}/generate`, { method: 'POST' });
        const data = await response.json();
        if (response.ok) {
            try {
                await authFetch(`/api/v1/plans/${planId}/select_image`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ image_id: data.asset_id })
                });
            } catch (e) { console.error("Failed to bind generated image", e); }
            const carousel = document.getElementById(`carousel-${planId}`);
            const track = document.getElementById(`track-${planId}`);
            carousel.classList.remove('hidden');
            track.querySelectorAll('.carousel-item img').forEach(img => { img.style.border = '2px solid transparent'; });
            const newItem = document.createElement('div');
            newItem.className = 'carousel-item';
            newItem.style.cssText = 'position:relative; min-width:110px; width:110px; cursor:pointer; flex-shrink:0;';
            newItem.onclick = () => selectSuggestedImage(planId, data.asset_id, data.image_path);
            newItem.innerHTML = `
                <div style="position:absolute; top:3px; left:3px; background:rgba(34,139,34,0.85); color:white; font-size:9px; padding:2px 5px; border-radius:3px; font-weight:bold; z-index:1;">Generated</div>
                <img src="${data.image_path}" style="width:100%; border-radius:6px; border:3px solid var(--primary);"
                     onmouseover="this.style.borderColor='var(--primary)'"
                     onmouseout="this.style.borderColor='var(--primary)'">`;
            track.prepend(newItem);
            track.scrollLeft = 0;
            btn.innerHTML = "🔄 Regenerate another";
        } else {
            alert("Generation failed: " + data.detail);
            btn.innerHTML = "✨ Generate Image";
        }
    } catch (err) {
        alert("Network error.");
        btn.innerHTML = "✨ Generate Image";
    } finally {
        btn.disabled = false;
    }
}

async function selectSuggestedImage(planId, imageId, localPath) {
    try {
        const response = await authFetch(`/api/v1/plans/${planId}/select_image`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ image_id: imageId })
        });
        if (response.ok) {
            const track = document.getElementById(`track-${planId}`);
            if (track) {
                track.querySelectorAll('.carousel-item img').forEach(img => { img.style.border = '2px solid transparent'; });
                track.querySelectorAll('.carousel-item').forEach(item => {
                    const onclickStr = item.getAttribute('onclick') || '';
                    if (onclickStr.includes(imageId)) {
                        const img = item.querySelector('img');
                        if (img) img.style.border = '3px solid var(--primary)';
                    }
                });
            }
            const fullUrl = localPath.startsWith('/images/') ? localPath : `/images/${localPath.split('/').pop()}`;
            openLightbox(fullUrl, imageId, planId);
        }
    } catch (e) { console.error("Failed to select image", e); alert("Failed to bind suggested image."); }
}

async function selectAndEdit(planId, imageId, imgSrc) {
    await selectSuggestedImage(planId, imageId, imgSrc);
}

async function deleteImage(imageId, planId) {
    if (!confirm('Delete this image? This cannot be undone.')) return;
    try {
        const res = await authFetch(`/api/v1/images/${imageId}`, { method: 'DELETE' });
        if (res.ok) {
            const item = document.getElementById(`citem-${imageId}`);
            if (item) item.remove();
            const track = document.getElementById(`track-${planId}`);
            if (track && track.children.length === 0) {
                const carousel = document.getElementById(`carousel-${planId}`);
                if (carousel) carousel.classList.add('hidden');
                const btn = document.querySelector(`.generate-alt-btn[data-id="${planId}"]`);
                if (btn) btn.innerHTML = '✨ Generate Image';
            }
        } else { alert('Failed to delete image.'); }
    } catch (e) { console.error('Delete error:', e); alert('Network error during delete.'); }
}

function scrollCarousel(planId, direction) {
    const track = document.getElementById(`track-${planId}`);
    if (track) track.scrollBy({ left: direction * 130, behavior: 'smooth' });
}

function openLightbox(imgSrc, imageId = null, planId = null) {
    lightboxImageId = imageId;
    lightboxPlanId = planId;
    lightboxImages = [];
    lightboxIndex = 0;
    document.getElementById('edit-comment-input').value = '';
    if (planId) {
        const track = document.getElementById(`track-${planId}`);
        if (track) {
            track.querySelectorAll('.carousel-item').forEach(item => {
                const img = item.querySelector('img');
                const itemId = item.id ? item.id.replace('citem-', '') : null;
                if (img && itemId) lightboxImages.push({ id: itemId, src: img.src.split('?')[0] });
            });
        }
    }
    if (lightboxImages.length > 0 && imageId) {
        const idx = lightboxImages.findIndex(i => String(i.id) === String(imageId));
        if (idx >= 0) lightboxIndex = idx;
    }
    lightboxImg.src = imgSrc;
    const prevBtn = document.getElementById('lightbox-prev');
    const nextBtn = document.getElementById('lightbox-next');
    if (prevBtn && nextBtn) {
        const showArrows = lightboxImages.length > 1;
        prevBtn.style.display = showArrows ? 'flex' : 'none';
        nextBtn.style.display = showArrows ? 'flex' : 'none';
    }
    const editBar = document.getElementById('lightbox-edit-bar');
    if (imageId) editBar.style.display = 'flex';
    else editBar.style.display = 'none';
    lightbox.classList.add('active');
}

function lightboxNav(direction) {
    if (lightboxImages.length <= 1) return;
    lightboxIndex = (lightboxIndex + direction + lightboxImages.length) % lightboxImages.length;
    const img = lightboxImages[lightboxIndex];
    lightboxImg.src = img.src;
    lightboxImageId = img.id;
}

async function applyImageEdit() {
    if (!lightboxImageId) { alert('No image selected for editing.'); return; }
    const commentInput = document.getElementById('edit-comment-input');
    const modification = commentInput.value.trim();
    if (!modification) { alert('Please describe the modification you want.'); commentInput.focus(); return; }
    const applyBtn = document.getElementById('apply-edit-btn');
    const originalBtnText = applyBtn.innerHTML;
    applyBtn.innerHTML = '⏳ Editing...';
    applyBtn.disabled = true;
    try {
        const body = { modification };
        if (lightboxPlanId) body.plan_id = lightboxPlanId;
        const response = await authFetch(`/api/v1/images/${lightboxImageId}/edit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await response.json();
        if (response.ok) {
            const newId = data.asset_id;
            const newSrc = data.image_path + '?t=' + Date.now();
            lightboxImg.src = newSrc;
            lightboxImageId = newId;
            commentInput.value = '';
            commentInput.placeholder = 'Applied! Describe another change or close the lightbox.';
            if (lightboxPlanId) {
                const track = document.getElementById(`track-${lightboxPlanId}`);
                if (track) {
                    track.querySelectorAll('.carousel-item img').forEach(img => { img.style.border = '2px solid transparent'; });
                    const newItem = document.createElement('div');
                    newItem.className = 'carousel-item';
                    newItem.id = `citem-${newId}`;
                    newItem.style.cssText = 'position:relative; min-width:110px; width:110px; cursor:pointer; flex-shrink:0;';
                    newItem.setAttribute('onclick', `selectAndEdit('${lightboxPlanId}', '${newId}', '${data.image_path}')`);
                    newItem.innerHTML = `
                        <div style="position:absolute; top:3px; left:3px; background:rgba(147,51,234,0.85); color:white; font-size:9px; padding:2px 5px; border-radius:3px; font-weight:bold; z-index:1;">Edited</div>
                        <img src="${newSrc}" style="width:100%; border-radius:6px; border:3px solid var(--primary);"
                             onmouseover="this.style.borderColor='var(--primary)'"
                             onmouseout="this.style.borderColor='var(--primary)'">`;
                    track.prepend(newItem);
                    track.scrollLeft = 0;
                }
            }
        } else { alert('Edit failed: ' + (data.detail || 'Unknown error')); }
    } catch (err) { console.error('Edit error:', err); alert('Network error during edit.'); }
    finally { applyBtn.innerHTML = originalBtnText; applyBtn.disabled = false; }
}

function downloadEditorExcel() {
    if (!currentEditorPlan) { alert("No plan loaded yet."); return; }
    const toHMS = (totalSec) => {
        const s = Math.floor(totalSec);
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const sec = s % 60;
        const pad = n => String(n).padStart(2, '0');
        return `${pad(h)}h${pad(m)}m${pad(sec)}s`;
    };
    const rows = [];
    const segments = [...currentEditorPlan.segments].sort((a, b) => a.timestamp_start - b.timestamp_start);
    segments.forEach(seg => {
        (seg.alternative_plans || []).forEach((alt, altIdx) => {
            rows.push({
                "Timestamp Start": toHMS(seg.timestamp_start),
                "Timestamp End": toHMS(seg.timestamp_end),
                "Duration": toHMS(seg.timestamp_end - seg.timestamp_start),
                "Transcript": seg.transcript_text || "",
                "Alt #": altIdx + 1,
                "AI Pick": alt.is_curated ? "Yes" : "",
                "Image Type": alt.visual_type || "",
                "Category": alt.category || "",
                "Content / Topic": alt.concept || "",
                "Local Context": alt.description || "",
                "Theme": alt.theme || "",
                "Tags": alt.tags || "",
                "Final Prompt": alt.final_prompt || "",
                "Disabled": alt.is_disabled ? "Yes" : "No",
            });
        });
    });
    if (rows.length === 0) { alert("No segments to export."); return; }
    const ws = XLSX.utils.json_to_sheet(rows);
    ws["!cols"] = [16, 14, 12, 45, 6, 9, 22, 14, 30, 40, 20, 30, 50, 10].map(w => ({ wch: w }));
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "Editor");
    XLSX.writeFile(wb, "editor-plan.xlsx");
}

async function renderFinalTimeline() {
    finalAssetsContainer.innerHTML = 'Loading timeline...';
    try {
        const response = await authFetch(`/api/v1/sessions/${currentSessionId}/timeline-assets`);
        const data = await response.json();
        if (data.timeline_assets.length === 0) {
            finalAssetsContainer.innerHTML = '<p>No assets generated yet. Go back to Editor to generate some!</p>';
            return;
        }
        finalAssetsContainer.innerHTML = '';
        data.timeline_assets.sort((a, b) => a.timestamp_start - b.timestamp_start).forEach(asset => {
            finalAssetsContainer.innerHTML += `
                <div class="asset-card fade-in">
                    <img src="${asset.asset_path}" class="img-preview" />
                    <div class="asset-info">
                        <strong>${asset.timestamp_start.toFixed(1)}s - ${asset.timestamp_end.toFixed(1)}s</strong><br>
                        <span style="color:var(--text-muted)">Type: ${asset.type}</span>
                    </div>
                </div>`;
        });
    } catch (err) { console.error(err); finalAssetsContainer.innerHTML = '<p>Failed to load timeline.</p>'; }
}

// ── Admin Panel ───────────────────────────────────────────────────────────────
let _adminRefreshTimer = null;
let _dailyChartInstance = null;

function _animateCount(el, target) {
    const start = 0;
    const duration = 600;
    const startTime = performance.now();
    const step = (now) => {
        const progress = Math.min((now - startTime) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        el.textContent = Math.round(start + (target - start) * eased);
        if (progress < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
}


function _formatLastSeen(lastActiveAt) {
    if (!lastActiveAt) return 'Never';
    const diff = Math.floor((Date.now() - new Date(lastActiveAt + 'Z')) / 1000);
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return new Date(lastActiveAt).toLocaleDateString();
}

function _formatActiveTime(seconds) {
    if (!seconds || seconds === 0) return '—';
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function _formatDuration(seconds) {
    if (!seconds || seconds === 0) return '—';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

async function loadAdminCharts() {
    try {
        const res = await authFetch('/api/v1/admin/daily-generations');
        if (!res || !res.ok) return;
        const data = await res.json();

        const labels = data.map(d => {
            const [y, mo, day] = d.date.split('-');
            return new Date(+y, +mo - 1, +day).toLocaleDateString('en-GB', { month: 'short', day: 'numeric' });
        });
        const counts = data.map(d => d.count);
        const total  = counts.reduce((a, b) => a + b, 0);

        const totalEl = document.getElementById('daily-gen-total');
        if (totalEl) totalEl.textContent = `${total} images generated`;

        // Update images stat card
        const statImages = document.getElementById('stat-val-images');
        if (statImages) _animateCount(statImages, total);

        const canvas = document.getElementById('daily-gen-chart');
        if (!canvas) return;
        if (_dailyChartInstance) { _dailyChartInstance.destroy(); _dailyChartInstance = null; }

        const ctx = canvas.getContext('2d');
        const gradient = ctx.createLinearGradient(0, 0, 0, 200);
        gradient.addColorStop(0, 'rgba(99,102,241,0.35)');
        gradient.addColorStop(1, 'rgba(99,102,241,0.0)');

        _dailyChartInstance = new Chart(canvas, {
            type: 'line',
            data: {
                labels,
                datasets: [{
                    label: 'Images',
                    data: counts,
                    borderColor: 'rgba(99,102,241,1)',
                    borderWidth: 2.5,
                    pointBackgroundColor: 'rgba(99,102,241,1)',
                    pointRadius: 3,
                    pointHoverRadius: 6,
                    fill: true,
                    backgroundColor: gradient,
                    tension: 0.4,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: 'rgba(15,15,30,0.92)',
                        titleColor: '#a5b4fc',
                        bodyColor: '#fff',
                        padding: 10,
                        cornerRadius: 8,
                        callbacks: {
                            label: item => ` ${item.raw} image${item.raw !== 1 ? 's' : ''}`
                        }
                    }
                },
                scales: {
                    x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 45, color: 'rgba(120,120,140,0.8)' } },
                    y: { beginAtZero: true, ticks: { stepSize: 1, font: { size: 10 }, color: 'rgba(120,120,140,0.8)' }, grid: { color: 'rgba(120,120,140,0.08)' } }
                }
            }
        });
    } catch (e) {
        console.warn('Daily chart failed:', e);
    }
}

async function loadAdminUsers() {
    const container = document.getElementById('admin-users-container');
    const bannerText = document.getElementById('admin-online-text');

    const [usersRes, onlineRes] = await Promise.all([
        authFetch('/api/v1/admin/users'),
        authFetch('/api/v1/admin/online-count'),
    ]).catch(() => [null, null]);
    loadAdminCharts();

    // Update stat cards from online-count
    if (onlineRes && onlineRes.ok) {
        const { online, total, pending } = await onlineRes.json();
        if (bannerText) bannerText.textContent = `${online} online of ${total}`;
        const pendingBadge = document.getElementById('admin-pending-badge');
        if (pendingBadge) {
            if (pending > 0) { pendingBadge.textContent = `${pending} pending`; pendingBadge.classList.remove('hidden'); }
            else pendingBadge.classList.add('hidden');
        }
        const stTotal   = document.getElementById('stat-val-total');
        const stOnline  = document.getElementById('stat-val-online');
        const stPending = document.getElementById('stat-val-pending');
        if (stTotal)   _animateCount(stTotal, total);
        if (stOnline)  _animateCount(stOnline, online);
        if (stPending) _animateCount(stPending, pending);
    }

    if (!usersRes || !usersRes.ok) {
        container.innerHTML = '<p style="color:red">Failed to load users.</p>';
        return;
    }
    const users = await usersRes.json();
    if (users.length === 0) {
        container.innerHTML = '<p style="color:var(--muted)">No users found.</p>';
        return;
    }

    // Compute total downloads for stat card
    const totalDl = users.reduce((s, u) => s + (u.download_count || 0), 0);
    const stDl = document.getElementById('stat-val-dl');
    if (stDl) _animateCount(stDl, totalDl);

    container.innerHTML = `
        <table class="admin-table">
            <thead>
                <tr>
                    <th></th>
                    <th>User</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Last Seen</th>
                    <th title="Number of courses">Courses</th>
                    <th title="Generated + Edited images">Images</th>
                    <th title="Download rate">DL Rate</th>
                    <th title="Total active time in app">Time</th>
                    <th title="Total video duration uploaded">Duration</th>
                    <th>Joined</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${users.map(u => {
                    const online = u.is_online;
                    const isPending = !u.is_active;
                    const totalImages = u.generated_count + u.edited_count;
                    const dlPct = totalImages > 0 ? Math.round(u.download_count / totalImages * 100) : 0;
                    const dlColor = dlPct >= 75 ? '#22c55e' : dlPct >= 40 ? '#f59e0b' : '#6b7280';
                    const initials = u.username.slice(0,2).toUpperCase();
                    const avatarHue = (u.username.charCodeAt(0) * 37 + u.username.charCodeAt(1||0) * 17) % 360;
                    const onlinePip = online
                        ? `<span style="position:absolute;bottom:0;right:0;width:9px;height:9px;border-radius:50%;background:#22c55e;border:1.5px solid var(--surface);box-shadow:0 0 0 2px rgba(34,197,94,.3);"></span>`
                        : '';
                    return `
                    <tr class="${isPending ? 'pending-user-row' : ''}" style="${online ? 'background:rgba(34,197,94,.025);' : ''}">
                        <td style="padding:10px 8px 10px 12px;">
                            <div style="position:relative;width:34px;height:34px;border-radius:50%;background:hsl(${avatarHue},65%,55%);display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:700;color:#fff;flex-shrink:0;">
                                ${initials}${onlinePip}
                            </div>
                        </td>
                        <td><button class="user-name-btn" onclick="openUserCoursesDrawer('${u.id}', '${u.username}')">${u.username}</button></td>
                        <td><span class="role-badge role-${u.role}">${u.role}</span></td>
                        <td><span class="status-dot ${u.is_active ? 'active' : 'inactive'}">${u.is_active ? 'Active' : '⏳ Pending'}</span></td>
                        <td style="font-size:.82rem;color:var(--muted);">${_formatLastSeen(u.last_active_at)}</td>
                        <td style="font-weight:600;text-align:center;">${u.course_count}</td>
                        <td>
                            <span style="font-weight:700;">${totalImages}</span>
                            ${u.edited_count > 0 ? `<span style="font-size:.72rem;color:var(--muted);margin-left:3px;">(${u.generated_count}+${u.edited_count})</span>` : ''}
                        </td>
                        <td style="min-width:80px;">
                            <div style="display:flex;align-items:center;gap:6px;">
                                <div style="flex:1;height:5px;border-radius:99px;background:var(--border);overflow:hidden;">
                                    <div style="width:${dlPct}%;height:100%;border-radius:99px;background:${dlColor};transition:width .6s ease;"></div>
                                </div>
                                <span style="font-size:.78rem;font-weight:600;color:${dlColor};min-width:28px;text-align:right;">${totalImages > 0 ? dlPct + '%' : '—'}</span>
                            </div>
                        </td>
                        <td style="font-size:.82rem;color:var(--muted);">${_formatActiveTime(u.total_active_seconds)}</td>
                        <td style="font-size:.82rem;color:var(--muted);">${_formatDuration(u.total_video_duration_seconds)}</td>
                        <td style="font-size:.82rem;">${new Date(u.created_at).toLocaleDateString('en-GB', {day:'numeric',month:'short',year:'2-digit'})}</td>
                        <td>
                            ${isPending ? `<button class="btn btn-sm btn-success" onclick="activateUser('${u.id}')" style="margin-right:4px">Activate</button>` : ''}
                            <button class="btn btn-sm btn-outline" onclick="openEditUserModal('${u.id}', '${u.username}', '${u.role}', ${u.is_active})">Edit</button>
                            ${u.role !== 'admin' ? `<button class="btn btn-sm btn-danger" onclick="deleteUser('${u.id}', '${u.username}')" style="margin-left:4px">Delete</button>` : ''}
                        </td>
                    </tr>`;
                }).join('')}
            </tbody>
        </table>`;

    clearTimeout(_adminRefreshTimer);
    _adminRefreshTimer = setTimeout(() => {
        if (!document.getElementById('admin-section').classList.contains('hidden')) {
            loadAdminUsers();
        }
    }, 30000);
}

async function openUserCoursesDrawer(userId, username) {
    currentViewingUserId = userId;
    const drawer = document.getElementById('user-courses-drawer');
    const list   = document.getElementById('drawer-courses-list');
    document.getElementById('drawer-username').textContent = username;
    document.getElementById('drawer-subtitle').textContent = 'Loading courses…';
    list.innerHTML = '<div class="empty-state" style="padding:2rem 0"><div class="empty-state-icon">⏳</div></div>';
    drawer.classList.remove('hidden');
    document.body.style.overflow = 'hidden';

    try {
        const res = await authFetch(`/api/v1/admin/users/${userId}/courses`);
        const courses = await res.json();
        document.getElementById('drawer-subtitle').textContent =
            courses.length === 0 ? 'No courses yet' : `${courses.length} course${courses.length !== 1 ? 's' : ''}`;

        if (courses.length === 0) {
            list.innerHTML = '<div class="empty-state" style="padding:2rem 0"><div class="empty-state-icon">📚</div><div class="empty-state-title">No courses created yet</div></div>';
            return;
        }

        list.innerHTML = courses.map(c => `
            <div class="drawer-course-card" onclick="closeUserCoursesDrawer(); navigateTo('course-detail', { courseId: '${c.id}' }); loadCourseDetail('${c.id}')">
                <span class="drawer-course-title">${c.title}</span>
                <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24" style="flex-shrink:0;opacity:.4"><path d="M9 18l6-6-6-6"/></svg>
            </div>`).join('');
    } catch (e) {
        list.innerHTML = '<p style="color:var(--danger);padding:1rem">Failed to load courses.</p>';
    }
}

function closeUserCoursesDrawer(e) {
    if (e && e.target !== document.getElementById('user-courses-drawer')) return;
    document.getElementById('user-courses-drawer').classList.add('hidden');
    document.body.style.overflow = '';
}

let currentViewingUserId = null;

async function openUserDurationsBreakdown() {
    const username = document.getElementById('drawer-username').textContent;
    if (!currentViewingUserId) return;

    const drawer = document.getElementById('user-durations-drawer');
    const list = document.getElementById('durations-breakdown-list');
    document.getElementById('durations-username').textContent = username;
    document.getElementById('durations-total').textContent = 'Loading…';
    list.innerHTML = '<div class="empty-state" style="padding:2rem 0"><div class="empty-state-icon">⏳</div></div>';
    drawer.classList.remove('hidden');

    try {
        const res = await authFetch(`/api/v1/admin/users/${currentViewingUserId}/durations`);
        const data = await res.json();

        const totalDuration = _formatDuration(data.total_duration_seconds);
        document.getElementById('durations-total').textContent = `Total: ${totalDuration}`;

        if (data.courses.length === 0) {
            list.innerHTML = '<div class="empty-state" style="padding:2rem 0"><div class="empty-state-icon">📚</div><div class="empty-state-title">No uploads yet</div></div>';
            return;
        }

        list.innerHTML = data.courses.map(course => `
            <div style="margin-bottom:1.5rem;border:1.5px solid var(--border);border-radius:12px;padding:1rem;background:var(--surface);">
                <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:0.75rem;">
                    <div>
                        <h4 style="margin:0;font-size:.95rem;font-weight:600;">${course.course_title}</h4>
                        <p style="margin:.25rem 0 0;font-size:.78rem;color:var(--muted);">${course.media_sessions.length} upload${course.media_sessions.length !== 1 ? 's' : ''}</p>
                    </div>
                    <span style="font-weight:700;color:var(--primary);font-size:.9rem;background:rgba(79,70,229,.1);padding:4px 10px;border-radius:8px;">${_formatDuration(course.course_duration_seconds)}</span>
                </div>
                <div style="border-top:1.5px solid var(--border);padding-top:0.75rem;">
                    ${course.media_sessions.map(session => `
                        <div style="display:flex;justify-content:space-between;align-items:center;padding:0.5rem 0;font-size:.82rem;">
                            <div style="flex:1;min-width:0;">
                                <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text);">${session.title}</div>
                                <div style="font-size:.75rem;color:var(--muted);margin-top:.2rem;">${new Date(session.upload_timestamp).toLocaleString()}</div>
                            </div>
                            <div style="display:flex;gap:1rem;align-items:center;margin-left:1rem;flex-shrink:0;">
                                <span style="background:${session.processing_status === 'completed' ? 'rgba(34,197,94,.1)' : 'rgba(241,65,108,.1)'};color:${session.processing_status === 'completed' ? '#22c55e' : '#f14165'};padding:2px 8px;border-radius:4px;font-size:.7rem;font-weight:600;">${session.processing_status}</span>
                                <span style="font-weight:700;min-width:60px;text-align:right;color:var(--primary);">${_formatDuration(session.duration_seconds)}</span>
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
        `).join('');
    } catch (e) {
        console.error('Failed to load durations:', e);
        list.innerHTML = '<p style="color:var(--danger);padding:1rem">Failed to load duration breakdown.</p>';
    }
}

function closeUserDurationsBreakdown(e) {
    if (e && e.target !== document.getElementById('user-durations-drawer')) return;
    document.getElementById('user-durations-drawer').classList.add('hidden');
}

function openEditUserModal(userId, username, role, isActive) {
    document.getElementById('edit-user-id').value = userId;
    document.getElementById('edit-user-username').value = username;
    document.getElementById('edit-user-role').value = role;
    document.getElementById('edit-user-active').checked = isActive;
    document.getElementById('edit-user-password').value = '';
    document.getElementById('edit-user-error').classList.add('hidden');
    document.getElementById('edit-user-modal').classList.remove('hidden');
}

function closeEditUserModal() {
    document.getElementById('edit-user-modal').classList.add('hidden');
}

async function saveUserEdit() {
    const userId = document.getElementById('edit-user-id').value;
    const password = document.getElementById('edit-user-password').value.trim();
    const role = document.getElementById('edit-user-role').value;
    const isActive = document.getElementById('edit-user-active').checked;
    const errEl = document.getElementById('edit-user-error');
    errEl.classList.add('hidden');
    const payload = { role, is_active: isActive };
    if (password) payload.password = password;
    try {
        const res = await authFetch(`/api/v1/admin/users/${userId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (res.ok) {
            closeEditUserModal();
            loadAdminUsers();
        } else {
            const d = await res.json();
            errEl.textContent = d.detail || 'Failed to update user';
            errEl.classList.remove('hidden');
        }
    } catch (e) {
        errEl.textContent = 'Network error';
        errEl.classList.remove('hidden');
    }
}

async function activateUser(userId) {
    try {
        const res = await authFetch(`/api/v1/admin/users/${userId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_active: true })
        });
        if (res.ok) loadAdminUsers();
        else { const d = await res.json(); alert(d.detail || 'Failed to activate user'); }
    } catch { alert('Network error'); }
}

function scrollToPending() {
    document.querySelector('.pending-user-row')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function deleteUser(userId, username) {
    if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return;
    try {
        const res = await authFetch(`/api/v1/admin/users/${userId}`, { method: 'DELETE' });
        if (res.ok) loadAdminUsers();
        else { const d = await res.json(); alert(d.detail || 'Failed to delete user'); }
    } catch (e) { alert('Network error'); }
}
