const $ = (selector) => document.querySelector(selector);
let jobs = [];
let messages = [];
let aiBusy = false;
let showSocial = true;
const TRACK_LABELS = {
  wishlist: '想投递', applied: '已投递', written: '笔试',
  interview1: '一面', interview2: '二面', interview3: '三面',
  hr: 'HR面', offer: 'Offer', rejected: '已拒绝', closed: '已终止'
};
const TRACK_ORDER = ['wishlist', 'applied', 'written', 'interview1', 'interview2', 'interview3', 'hr', 'offer', 'rejected', 'closed'];
const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;
const MAX_REQUEST_BYTES = 14 * 1024 * 1024;

function trackOptionsHtml(selected) {
  return ['', ...TRACK_ORDER].map((value) =>
    `<option value="${value}"${value === selected ? ' selected' : ''}>${value ? TRACK_LABELS[value] : '未追踪'}</option>`
  ).join('');
}

function trackTimeText(value) {
  if (!value) return '';
  const numeric = Number(value);
  const date = new Date(Number.isFinite(numeric) && numeric < 1e12 ? numeric * 1000 : numeric);
  if (isNaN(date.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function loadJobs() {
  try {
    const response = await fetch('/api/jobs');
    if (!response.ok) throw new Error(`岗位接口 ${response.status}`);
    const data = await response.json();
    if (!Array.isArray(data)) throw new Error('岗位数据格式错误');
    jobs = data;
    populateCityFilter();
    populateDirectionFilter();
    renderJobs();
  } catch (error) {
    $('#jobs').innerHTML = `<div class="job empty">暂时无法加载岗位：${escapeHtml(error.message)}</div>`;
    $('#count').textContent = '0';
  }
}

function populateCityFilter() {
  const select = $('#city');
  const current = select.value;
  const counts = {};
  jobs.forEach((job) => {
    const city = String(job.city || '').trim();
    if (city) counts[city] = (counts[city] || 0) + 1;
  });
  const cities = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b, 'zh-CN'));
  select.innerHTML = [
    '<option value="">全部城市</option>',
    ...cities.map((city) => `<option value="${escapeHtml(city)}"${city === current ? ' selected' : ''}>${escapeHtml(city)}</option>`),
  ].join('');
}

function populateDirectionFilter() {
  const select = $('#direction');
  const current = select.value;
  const counts = {};
  jobs.forEach((job) => {
    const direction = String(job.direction || '').trim();
    if (direction) counts[direction] = (counts[direction] || 0) + 1;
  });
  const directions = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b, 'zh-CN'));
  select.innerHTML = [
    '<option value="">全部方向</option>',
    ...directions.map((direction) => `<option value="${escapeHtml(direction)}"${direction === current ? ' selected' : ''}>${escapeHtml(direction)}</option>`),
  ].join('');
}

async function loadHealth() {
  try {
    const response = await fetch('/api/health', { cache: 'no-store' });
    if (!response.ok) throw new Error(`后端状态 ${response.status}`);
    const data = await response.json();
    const refreshed = data.refresh_in_progress ? '正在同步公开来源' : (data.last_refresh_iso ? `已同步 ${data.last_refresh_iso}` : '首次同步中');
    const seconds = Number(data.refresh_interval_seconds);
    let cadence = '自动更新';
    if (Number.isFinite(seconds) && seconds > 0) {
      if (seconds >= 3600 && seconds % 3600 === 0) {
        cadence = `每 ${seconds / 3600} 小时自动更新`;
      } else if (seconds >= 60 && seconds % 60 === 0) {
        cadence = `每 ${seconds / 60} 分钟自动更新`;
      } else {
        cadence = `每 ${seconds} 秒自动更新`;
      }
    }
    $('#syncStatus').textContent = `${refreshed} · ${cadence}`;
  } catch (_) {
    $('#syncStatus').textContent = '正在连接本机后端';
  }
}

async function loadSources() {
  try {
    const response = await fetch('/api/sources', { cache: 'no-store' });
    if (!response.ok) throw new Error('source status');
    const sources = await response.json();
    const stateOf = (source) => {
      if (source.state) return source.state;
      if (source.last_error && source.last_error !== '页面未解析到匹配岗位') return 'error';
      if (source.last_success && Number(source.jobs_found) > 0) return 'ok';
      if (source.last_checked) return 'empty';
      return 'unknown';
    };
    const healthy = sources.filter((source) => stateOf(source) === 'ok').length;
    const empty = sources.filter((source) => stateOf(source) === 'empty').length;
    const failed = sources.filter((source) => stateOf(source) === 'error').length;
    const pieces = [`${sources.length} 个公开源`, `${healthy} 个有岗位`];
    if (empty) pieces.push(`${empty} 个已检查无匹配`);
    if (failed) pieces.push(`${failed} 个暂时不可用`);
    $('#sourceStatus').textContent = pieces.join(' · ');
    const links = [...sources].sort((a, b) => (b.priority || 0) - (a.priority || 0) || String(a.name).localeCompare(String(b.name)))
      .map((source) => {
        const url = safeUrl(source.url);
        return url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.name)}</a>` : '';
      }).filter(Boolean);
    const shown = links.slice(0, 200);
    let sourceHtml = shown.join(' · ');
    if (links.length > shown.length) sourceHtml += ` · 等共 ${links.length} 个信源（扩展池分批刷新）`;
    if (links.length) $('#sourceLinks').innerHTML = sourceHtml;
  } catch (_) {
    $('#sourceStatus').textContent = '信息源状态暂不可用';
  }
}

async function loadProfile() {
  try {
    const response = await fetch('/api/profile', { cache: 'no-store' });
    if (!response.ok) throw new Error('profile');
    const profile = await response.json();
    const roles = Array.isArray(profile.targetRoles) ? profile.targetRoles : [];
    const skills = Array.isArray(profile.skills) ? profile.skills : [];
    const summary = [...roles, ...skills].slice(0, 3);
    if (summary.length) $('#profileSummary').textContent = summary.join(' · ');
    const chips = [profile.major, ...skills, ...(Array.isArray(profile.keywords) ? profile.keywords : [])]
      .filter(Boolean).slice(0, 14);
    if (chips.length) $('#profileChips').innerHTML = chips.map((chip) => `<span>${escapeHtml(chip)}</span>`).join('');
  } catch (_) {
    // The server-side fallback profile remains visible if the request fails.
  }
}

function renderJobs() {
  const query = $('#search').value.trim().toLowerCase();
  const city = $('#city').value;
  const direction = $('#direction').value;
  const salaryValue = Number($('#salary').value || 0);
  const minimum = Number.isFinite(salaryValue) ? salaryValue : 0;
  const trackFilter = $('#trackFilter').value;
  const filtered = jobs.filter((job) => {
    const audience = job.audience || 'general';
    if (!showSocial && audience !== 'campus') return false;
    const trackStatus = (job.tracking && job.tracking.status) || '';
    if (trackFilter === 'tracked' && !trackStatus) return false;
    if (trackFilter && trackFilter !== 'tracked' && trackStatus !== trackFilter) return false;
    const tags = Array.isArray(job.tags) ? job.tags : [];
    const haystack = `${job.title || ''}${job.company || ''}${job.city || ''}${job.direction || ''}${tags.join('')}`.toLowerCase();
    return (!query || haystack.includes(query)) && (!city || job.city === city) &&
      (!direction || job.direction === direction) && (!minimum || job.minSalary >= minimum);
  });
  const trackedCount = jobs.filter((job) => job.tracking && job.tracking.status).length;
  $('#trackCount').textContent = trackedCount;
  if ($('#sort').value === 'salary') filtered.sort((a, b) => b.maxSalary - a.maxSalary);
  else filtered.sort((a, b) => b.match - a.match || b.priority - a.priority);
  $('#count').textContent = filtered.length;
  $('#jobs').innerHTML = filtered.map((job) => {
    const rawFirstSeen = Number(job.firstSeen);
    const firstSeenMs = Number.isFinite(rawFirstSeen) && rawFirstSeen < 1e12 ? rawFirstSeen * 1000 : rawFirstSeen;
    const isNew = Number.isFinite(firstSeenMs) && firstSeenMs > Date.now() - 24 * 60 * 60 * 1000;
    const trackStatus = (job.tracking && job.tracking.status) || '';
    return `
    <article class="job${trackStatus ? ' tracked' : ''}">
      <div class="job-main"><h3>${escapeHtml(job.title)}<span class="audience ${job.audience === 'campus' ? 'campus' : 'social'}">${job.audience === 'campus' ? '校招' : '社招'}</span>${isNew ? '<span class="new-badge">新</span>' : ''}</h3>
        <p>${escapeHtml(job.company)} · ${escapeHtml(job.city || '地点待确认')} · ${escapeHtml(job.salary)}</p>
        <div class="tags">${(Array.isArray(job.tags) ? job.tags : []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join('')}</div>
        <small class="source-label">来源：${escapeHtml(job.source || '公开招聘页')}</small>
        <div class="track-bar">
          <select class="track-status" data-id="${Number(job.id)}" aria-label="投递状态">${trackOptionsHtml(trackStatus)}</select>
          ${trackStatus ? `<span class="track-time">${TRACK_LABELS[trackStatus] || ''}${job.tracking.updatedAt ? ' · ' + trackTimeText(job.tracking.updatedAt) : ''}</span>` : ''}
        </div>
      </div>
      <div class="match"><div class="score">${job.match}<small>%</small></div>
        ${detailLink(job.url)}
      </div>
    </article>`;
  }).join('') || '<div class="job empty">没有符合条件的岗位，试试放宽筛选条件。</div>';
}

function detailLink(value) {
  const url = safeUrl(value);
  return url
    ? `<a class="apply" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">查看详情</a>`
    : '<span class="apply" aria-disabled="true">暂无链接</span>';
}

function safeUrl(value) {
  try {
    const url = new URL(String(value || ''), window.location.href);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return '';
    return url.href;
  } catch (_) { return ''; }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[character]));
}

['search', 'city', 'direction', 'salary', 'sort', 'trackFilter'].forEach((id) => {
  $(`#${id}`).addEventListener('input', renderJobs);
  $(`#${id}`).addEventListener('change', renderJobs);
});
$('#showSocial').addEventListener('change', () => {
  showSocial = $('#showSocial').checked;
  renderJobs();
});
$('#resetBtn').onclick = () => {
  ['search', 'city', 'direction', 'salary', 'trackFilter'].forEach((id) => { $(`#${id}`).value = ''; });
  $('#sort').value = 'match';
  $('#showSocial').checked = true;
  showSocial = true;
  renderJobs();
};
$('#editProfile').onclick = () => $('#profilePanel').classList.add('open');
$('#closeProfile').onclick = () => $('#profilePanel').classList.remove('open');

// “用 AI 重建画像”：上传简历 → AI 提取 → 预览确认 → 保存
const aiProfileButton = document.createElement('button');
aiProfileButton.className = 'text-btn';
aiProfileButton.id = 'aiProfileBtn';
aiProfileButton.textContent = '用 AI 重建画像 →';
aiProfileButton.style.marginLeft = '14px';
$('#editProfile').parentElement.appendChild(aiProfileButton);
const aiProfileInput = document.createElement('input');
aiProfileInput.type = 'file';
aiProfileInput.accept = '.pdf,.doc,.docx,.png,.jpg,.jpeg,.webp';
aiProfileInput.style.display = 'none';
aiProfileButton.parentElement.appendChild(aiProfileInput);
aiProfileButton.onclick = () => aiProfileInput.click();
aiProfileInput.onchange = async () => {
  const file = aiProfileInput.files && aiProfileInput.files[0];
  if (!file) return;
  aiProfileButton.textContent = 'AI 解析中…';
  try {
    const attachment = await readAttachment(file);
    const response = await fetch('/api/profile/ai', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ attachments: [attachment] })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `解析失败 ${response.status}`);
    renderProfilePreview(data);
  } catch (error) {
    alert(`AI 解析失败：${error.message}`);
  } finally {
    aiProfileButton.textContent = '用 AI 重建画像 →';
    aiProfileInput.value = '';
  }
};

function renderProfilePreview(profile) {
  $('#listTitle').textContent = 'AI 画像预览';
  const roles = (profile.targetRoles || []).map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join('');
  const skills = (profile.skills || []).map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join('');
  const cities = (profile.preferredCities || []).map((item) => `<span class="tag">${escapeHtml(item)}</span>`).join('');
  $('#listBody').innerHTML = `<p class="apply-ok">AI 已从简历提取以下画像，请核对后保存：</p>
    <div class="profile-form">
      <p><b>方向</b><br>${roles || '（空）'}</p>
      <p><b>专业</b><br>${escapeHtml(profile.major || '（空）')}</p>
      <p><b>技能</b><br>${skills || '（空）'}</p>
      <p><b>关键词</b><br>${escapeHtml((profile.keywords || []).join('、') || '（空）')}</p>
      <p><b>意向城市</b><br>${cities || '（空）'}</p>
      <p><b>定位</b><br>${escapeHtml(profile.summary || '（空）')}</p>
      <div class="pf-buttons">
        <button id="profileAiSave" class="apply apply-submit">保存为新画像</button>
        <button id="profileAiCancel" class="apply">取消</button>
      </div>
    </div>`;
  $('#listModal').classList.add('open');
  $('#profileAiSave').onclick = async () => {
    try {
      const response = await fetch('/api/profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `保存失败 ${response.status}`);
      $('#listBody').innerHTML = '<p class="apply-ok">✅ 简历画像已更新，岗位匹配分已重新计算。</p><button class="apply" id="profileAiDone">完成</button>';
      $('#profileAiDone').onclick = () => $('#listModal').classList.remove('open');
      await loadProfile();
      await loadJobs();
    } catch (error) {
      alert(`保存失败：${error.message}`);
    }
  };
  $('#profileAiCancel').onclick = () => $('#listModal').classList.remove('open');
}

$('#listBtn').onclick = () => {
  $('#listTitle').textContent = '本周投递清单';
  const top = [...jobs].sort((a, b) => b.match - a.match).slice(0, 5);
  $('#listBody').innerHTML = top.length ? top.map((job) => `<div class="list-row"><span>${escapeHtml(job.title)}</span><b>${job.match}%</b></div>`).join('') : '<p>暂无高匹配岗位</p>';
  $('#listModal').classList.add('open');
};
$('#trackBtn').onclick = openTrackingView;
$('#closeList').onclick = () => $('#listModal').classList.remove('open');

async function openTrackingView() {
  $('#listTitle').textContent = '投递追踪';
  try {
    const response = await fetch('/api/tracking', { cache: 'no-store' });
    if (!response.ok) throw new Error(`追踪接口 ${response.status}`);
    const tracked = await response.json();
    tracked.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0) || b.match - a.match);
    $('#listBody').innerHTML = tracked.length
      ? tracked.map((job) => {
          const url = safeUrl(job.url);
          return `<div class="track-row" data-id="${Number(job.jobId)}">
            <div class="track-main">
              <b>${escapeHtml(job.title)}${job.active ? '' : '<span class="audience social">已下线</span>'}</b>
              <span>${escapeHtml(job.company || '')} · ${escapeHtml(job.city || '地点待确认')} · ${escapeHtml(job.direction || '')} · ${job.match}%</span>
              <small>${job.audience === 'campus' ? '校招' : '社招'} · ${job.updatedAt ? '更新 ' + trackTimeText(job.updatedAt) : ''} · 来源：${escapeHtml(job.source || '公开招聘页')}</small>
            </div>
            <div class="track-ops">
              <select class="track-status" data-id="${Number(job.jobId)}" aria-label="投递状态">${trackOptionsHtml(job.status)}</select>
              <a class="apply" href="${escapeHtml(url || '#')}" target="_blank" rel="noopener noreferrer">打开</a>
              <button class="track-remove" data-id="${Number(job.jobId)}" title="取消追踪">×</button>
            </div>
          </div>`;
        }).join('')
      : '<p>还没有追踪任何岗位。在岗位卡片上把状态设为“想投递”或“已投递”即可开始。</p>';
  } catch (error) {
    $('#listBody').innerHTML = `<p>追踪数据加载失败：${escapeHtml(error.message)}</p>`;
  }
  $('#listModal').classList.add('open');
}

async function setTracking(jobId, status) {
  try {
    const response = await fetch('/api/tracking', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jobId, status })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `保存失败 ${response.status}`);
    const job = jobs.find((item) => Number(item.id) === Number(jobId));
    if (job) {
      if (data.status) job.tracking = { status: data.status, note: data.note || '', updatedAt: data.updatedAt };
      else job.tracking = null;
    }
    renderJobs();
    if ($('#listModal').classList.contains('open') && $('#listTitle').textContent === '投递追踪') openTrackingView();
  } catch (error) {
    alert(`追踪保存失败：${error.message}`);
    renderJobs();
  }
}

document.addEventListener('change', (event) => {
  const select = event.target.closest('.track-status');
  if (select) setTracking(Number(select.dataset.id), select.value);
});
document.addEventListener('click', (event) => {
  const button = event.target.closest('.track-remove');
  if (button) setTracking(Number(button.dataset.id), '');
});

async function applyFlow(jobId, mode = 'preview') {
  $('#listTitle').textContent = mode === 'submit' ? '正在提交投递…' : '一键投递预演';
  $('#listBody').innerHTML = '<p>正在请求 job-pro…（首次可能要十几秒）</p>';
  $('#listModal').classList.add('open');
  try {
    const response = await fetch('/api/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jobId, mode })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
    renderApplyResult(data, jobId);
  } catch (error) {
    $('#listTitle').textContent = '一键投递';
    $('#listBody').innerHTML = `<p>一键投递请求失败：${escapeHtml(error.message)}</p>`;
  }
}

function renderApplyResult(data, jobId) {
  const submitting = Boolean(data.submit);
  $('#listTitle').textContent = submitting ? '投递提交结果' : '一键投递预演';
  const jobLine = `<p><b>${escapeHtml(data.company || '')}</b> · ${escapeHtml(data.title || '')}</p>`;
  const openButton = data.url
    ? `<p><a class="apply" href="${escapeHtml(safeUrl(data.url) || '#')}" target="_blank" rel="noopener noreferrer">打开官方投递页</a></p>`
    : '';
  if (!data.supported) {
    $('#listBody').innerHTML = `${jobLine}<p>${escapeHtml(data.message || '该公司暂不支持自动投递')}</p>${openButton}`;
    return;
  }
  const result = data.result || {};
  const ok = Boolean(data.ok || result.ok);
  const message = escapeHtml(data.message || result.message || result.hint || '');
  const questions = Array.isArray(result.questions)
    ? result.questions.map((question) => question && question.label).filter(Boolean).join('、')
    : '';
  let extra = '';
  if (ok) {
    extra = `<p class="apply-ok">✅ 预演通过，可以提交。</p>
      <p class="apply-warn">提交成功后会自动把该岗位标记为“已投递”。请先确认简历资料无误。</p>
      <button class="apply apply-submit" data-id="${Number(jobId)}">确认提交投递</button>`;
  } else {
    const cli = data.cliPath ? `node "${data.cliPath}"` : 'job-pro';
    extra = `<p class="apply-warn">⚠ 还不能直接提交：${message || '缺少个人资料或登录会话'}</p>
      ${questions ? `<p>该岗位需要填写：${escapeHtml(questions)}</p>` : ''}
      <p>首次使用需要一次性配置：<br>
      1. 配置个人资料（姓名/邮箱/电话/简历）：<br><code>${escapeHtml(cli)} profile init</code><br>
      2. 为该公司的招聘官网导出登录会话并保存到 <code>~/.jobpro/${escapeHtml(data.companyKey || '')}.session.json</code>（用 job-pro 仓库里的 Chrome 扩展 capture），详见其 <code>docs/auto-apply.md</code>。</p>`;
    extra += `<p><button class="apply apply-setup-profile">用 AI 配置投递资料（推荐）</button></p>`;
  }
  $('#listBody').innerHTML = `${jobLine}${openButton}${extra}
    <details><summary>原始返回（技术细节）</summary><pre>${escapeHtml(JSON.stringify(result, null, 2))}</pre></details>`;
  if (submitting && ok) setTracking(Number(jobId), 'applied');
}

document.addEventListener('click', (event) => {
  const aiSaveButton = event.target.closest('#aiSave');
  if (aiSaveButton) { saveAiConfig(); return; }
});

async function openJobproProfile() {
  applyResumeFile = null;
  parsedResumePath = '';
  existingResumePath = '';
  $('#listTitle').textContent = '投递资料（job-pro）';
  $('#listBody').innerHTML = '<p>正在读取投递资料…</p>';
  $('#listModal').classList.add('open');
  try {
    const response = await fetch('/api/apply/profile/status', { cache: 'no-store' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `读取失败 ${response.status}`);
    renderProfilePanel(data);
  } catch (error) {
    $('#listBody').innerHTML = `<p>读取投递资料失败：${escapeHtml(error.message)}</p>`;
  }
}

function renderProfilePanel(status) {
  const existing = (status.profile && status.exists) ? status.profile : {};
  existingResumePath = existing.resume_path || '';
  let banner = '';
  if (status.exists) {
    banner = status.missing.length
      ? `<p class="apply-warn">配置文件已存在，但还缺：${escapeHtml(status.missing.join('、'))}</p>`
      : `<p class="apply-ok">✅ 配置文件已就绪${status.hasResume ? '' : '，但简历文件路径无效，请重新上传简历'}</p>`;
  } else {
    banner = '<p class="apply-warn">还没有投递资料。上传简历后让 AI 自动拆分，检查无误后保存即可。</p>';
  }
  $('#listBody').innerHTML = `${banner}
    <p>配置文件位置：<code>${escapeHtml(status.path || '')}</code></p>
    <div class="profile-form">
      <label class="pf-file">上传简历（PDF / Word / 图片）<input type="file" id="applyResumeFile" accept=".pdf,.doc,.docx,.png,.jpg,.jpeg,.webp"></label>
      <p class="apply-warn" id="applyResumeHint">上传后点“AI 解析简历”</p>
      <div class="pf-grid">
        <label>名 / 全名<input id="pf_first" value="${escapeHtml(existing.first_name || '')}"></label>
        <label>姓<input id="pf_last" value="${escapeHtml(existing.last_name || '')}"></label>
        <label>邮箱<input id="pf_email" type="email" value="${escapeHtml(existing.email || '')}"></label>
        <label>电话<input id="pf_phone" value="${escapeHtml(existing.phone || '')}"></label>
        <label>学历<select id="pf_degree">
          <option value="">未填</option>
          <option value="bachelor"${existing.degree === 'bachelor' ? ' selected' : ''}>本科</option>
          <option value="master"${existing.degree === 'master' ? ' selected' : ''}>硕士</option>
          <option value="phd"${existing.degree === 'phd' ? ' selected' : ''}>博士</option>
        </select></label>
        <label>毕业年份<input id="pf_grad" type="number" min="2020" max="2035" value="${existing.graduation_year ? escapeHtml(String(existing.graduation_year)) : ''}"></label>
      </div>
      <p class="pf-path" id="pfResumePath">${existing.resume_path ? '简历文件：' + escapeHtml(existing.resume_path) : '尚未保存简历文件'}</p>
      <div class="pf-buttons">
        <button id="pfParse" class="apply">AI 解析简历</button>
        <button id="pfSave" class="apply apply-submit">保存投递资料</button>
      </div>
    </div>`;
}

async function parseProfileResume() {
  if (!applyResumeFile) {
    alert('请先选择简历文件');
    return;
  }
  const parseButton = $('#pfParse');
  if (parseButton) { parseButton.disabled = true; parseButton.textContent = 'AI 解析中…'; }
  try {
    const attachment = await readAttachment(applyResumeFile);
    const response = await fetch('/api/apply/profile/parse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ attachments: [attachment] })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `解析失败 ${response.status}`);
    parsedResumePath = data.resumePath || '';
    $('#pf_first').value = data.profile.first_name || '';
    $('#pf_last').value = data.profile.last_name || '';
    $('#pf_email').value = data.profile.email || '';
    $('#pf_phone').value = data.profile.phone || '';
    $('#pf_degree').value = data.profile.degree || '';
    $('#pf_grad').value = data.profile.graduation_year || '';
    $('#pfResumePath').textContent = `简历已保存：${parsedResumePath}`;
    $('#applyResumeHint').textContent = data.missing && data.missing.length
      ? `AI 没找到：${data.missing.join('、')}，请手动补一下再保存`
      : 'AI 解析完成，请核对后保存';
  } catch (error) {
    alert(`AI 解析失败：${error.message}`);
  } finally {
    if (parseButton) { parseButton.disabled = false; parseButton.textContent = 'AI 解析简历'; }
  }
}

async function saveProfileForm() {
  const profile = {
    first_name: $('#pf_first').value.trim(),
    last_name: $('#pf_last').value.trim(),
    email: $('#pf_email').value.trim(),
    phone: $('#pf_phone').value.trim(),
    degree: $('#pf_degree').value,
    graduation_year: Number($('#pf_grad').value) || null,
    resume_path: parsedResumePath || existingResumePath
  };
  try {
    const response = await fetch('/api/apply/profile/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `保存失败 ${response.status}`);
    $('#listBody').innerHTML = `<p class="apply-ok">✅ 投递资料已保存</p><p>位置：<code>${escapeHtml(data.path || '')}</code></p>
      <p>现在回到岗位卡片，点“一键投递”应该可以直接预演了。</p>
      <button class="apply" id="pfDone">完成</button>`;
    $('#pfDone').onclick = () => $('#listModal').classList.remove('open');
  } catch (error) {
    alert(`保存失败：${error.message}`);
  }
}

async function openAiSettings() {
  $('#listTitle').textContent = 'AI 设置';
  $('#listBody').innerHTML = '<p>正在读取 AI 配置…</p>';
  $('#listModal').classList.add('open');
  try {
    const response = await fetch('/api/ai/config', { cache: 'no-store' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `读取失败 ${response.status}`);
    $('#listBody').innerHTML = `<p class="apply-warn">当前 AI 走 OpenAI 兼容接口（DeepSeek）。文本模型不直接读 PDF/图片——本机已装 OCR，上传图片或扫描版简历会自动转成文字再发给 AI。</p>
      <div class="profile-form">
        <div class="pf-grid">
          <label>Base URL<input id="ai_base" value="${escapeHtml(data.baseUrl || 'https://api.deepseek.com')}"></label>
          <label>模型<input id="ai_model" value="${escapeHtml(data.model || 'deepseek-chat')}" placeholder="deepseek-chat / deepseek-reasoner"></label>
          <label>API Key<input id="ai_key" type="password" placeholder="sk-..."></label>
        </div>
        <p class="pf-path">当前状态：${data.configured ? `已配置（${escapeHtml(data.apiKeyMasked || '')}），填新 Key 可覆盖` : '未配置，请粘贴 DeepSeek API Key'}</p>
        <div class="pf-buttons">
          <button id="aiSave" class="apply apply-submit">保存并启用</button>
        </div>
      </div>`;
  } catch (error) {
    $('#listBody').innerHTML = `<p>读取 AI 配置失败：${escapeHtml(error.message)}</p>`;
  }
}

async function saveAiConfig() {
  const payload = {
    baseUrl: $('#ai_base').value.trim(),
    model: $('#ai_model').value.trim(),
    apiKey: $('#ai_key').value.trim()
  };
  if (!payload.apiKey) {
    alert('请粘贴 DeepSeek API Key');
    return;
  }
  const button = $('#aiSave');
  if (button) { button.disabled = true; button.textContent = '保存中…'; }
  try {
    const response = await fetch('/api/ai/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `保存失败 ${response.status}`);
    $('#ai_key').value = '';
    $('#listBody').innerHTML = `<p class="apply-ok">✅ AI 配置已保存并启用（${escapeHtml(data.model || '')}）</p>
      <p>现在可以去 AI 悬浮球上传简历测试；图片/扫描件会自动 OCR 成文字。</p>
      <button class="apply" id="aiDone">完成</button>`;
    $('#aiDone').onclick = () => $('#listModal').classList.remove('open');
    loadHealth();
  } catch (error) {
    alert(`保存失败：${error.message}`);
  } finally {
    if (button) { button.disabled = false; button.textContent = '保存并启用'; }
  }
}

// Add the “投递资料” entry to the aside actions.
const trackButton = $('#trackBtn');
if (trackButton && !$('#aiSettingsBtn')) {
  const aiButton = document.createElement('button');
  aiButton.id = 'aiSettingsBtn';
  aiButton.textContent = 'AI 设置';
  aiButton.onclick = openAiSettings;
  trackButton.parentElement.appendChild(aiButton);
}

const aiBall = $('#aiBall');
const aiPanel = $('#aiPanel');
aiBall.onclick = () => {
  if (aiBusy) return;
  messages = [];
  $('#resumeFile').value = '';
  $('#fileName').textContent = '未选择文件';
  $('#aiBody').innerHTML = '<div class="bubble bot">你好，我可以分析你的简历、给出修改建议，并帮你判断岗位匹配度。每次打开都是新上下文。</div>';
  aiPanel.classList.add('open');
  $('#aiInput').focus();
};
$('#closeAi').onclick = () => aiPanel.classList.remove('open');
$('#resumeFile').onchange = (event) => {
  $('#fileName').textContent = event.target.files[0]?.name || '未选择文件';
};
$('#sendAi').onclick = sendAi;
$('#aiInput').onkeydown = (event) => { if (event.key === 'Enter' && !event.shiftKey) sendAi(); };

async function sendAi() {
  if (aiBusy) return;
  const input = $('#aiInput');
  const file = $('#resumeFile').files[0];
  const question = input.value.trim() || (file ? '请分析这份简历，提取我的求职画像，并给出针对嵌入式和机器人研发秋招的修改建议。' : '');
  if (!question) return;
  aiBusy = true;
  $('#sendAi').disabled = true;
  const body = $('#aiBody');
  body.insertAdjacentHTML('beforeend', `<div class="bubble user">${escapeHtml(question)}</div>`);
  input.value = '';
  const loading = document.createElement('div');
  loading.className = 'bubble bot';
  loading.textContent = '正在分析…';
  body.append(loading);
  try {
    const attachments = [];
    if (file) attachments.push(await readAttachment(file));
    const userMessage = { role: 'user', content: question };
    const nextMessages = [...messages, userMessage];
    const payload = { messages: nextMessages, attachments };
    const serialized = JSON.stringify(payload);
    const requestBytes = typeof TextEncoder === 'function'
      ? new TextEncoder().encode(serialized).length
      : new Blob([serialized]).size;
    if (requestBytes > MAX_REQUEST_BYTES) throw new Error('附件或对话内容过大，请换用较小文件');
    const response = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: serialized
    });
    const raw = await response.text();
    let data = {};
    try { data = raw ? JSON.parse(raw) : {}; } catch (_) { data = { error: raw }; }
    if (!response.ok) throw new Error(data.detail || data.error || `后端请求 ${response.status}`);
    const answer = data.choices?.[0]?.message?.content || data.output_text || '接口未返回内容。';
    messages.push(userMessage);
    messages.push({ role: 'assistant', content: answer });
    loading.textContent = answer;
  } catch (error) {
    loading.textContent = `AI 暂时不可用：${error.message}`;
  } finally {
    aiBusy = false;
    $('#sendAi').disabled = false;
  }
  body.scrollTop = body.scrollHeight;
}

function readAttachment(file) {
  return new Promise((resolve, reject) => {
    const name = String(file.name || '').toLowerCase();
    const fileType = String(file.type || '');
    const extension = name.includes('.') ? name.slice(name.lastIndexOf('.') + 1) : '';
    const allowedExtension = ['pdf', 'doc', 'docx', 'png', 'jpg', 'jpeg', 'webp'].includes(extension);
    if (!allowedExtension || !(fileType === '' || fileType === 'application/pdf' || fileType === 'application/msword' ||
      fileType === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' || fileType.startsWith('image/'))) {
      reject(new Error('仅支持 PDF、Word 或图片附件')); return;
    }
    if (!file.size) { reject(new Error('附件为空')); return; }
    if (file.size > MAX_ATTACHMENT_BYTES) { reject(new Error('附件不能超过 8MB')); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const data = String(reader.result || '');
      if (!data.startsWith('data:')) { reject(new Error('附件编码失败')); return; }
      const mime = fileType || (extension === 'pdf' ? 'application/pdf' : extension === 'doc' ? 'application/msword' :
        extension === 'docx' ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' : `image/${extension}`);
      resolve({ name: file.name, mime, data });
    };
    reader.onerror = () => reject(new Error('读取附件失败'));
    reader.readAsDataURL(file);
  });
}

async function refreshView() {
  await Promise.all([loadJobs(), loadHealth(), loadSources(), loadProfile()]);
}

refreshView();
// The backend refreshes on its own; this keeps an open tab in sync without
// asking the user to reload the page manually.
window.setInterval(refreshView, 5 * 60 * 1000);
