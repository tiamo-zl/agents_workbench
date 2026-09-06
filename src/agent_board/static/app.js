/* Agent 看板 — Vue 3 应用逻辑（全局构建，无构建步骤） */
/* global Vue */
const { createApp } = Vue;

const TASK_AGENTS = ['claude', 'codex', 'gemini', 'dsh'];
const TASK_AGENT_NAMES = {
  claude: 'Claude Code',
  codex: 'Codex CLI',
  gemini: 'Gemini CLI',
  dsh: 'DeepSeek Harness',
};

function loadCollapsed() {
  try {
    const v = JSON.parse(localStorage.getItem('ab_collapse') || '{}');
    return { running: !!v.running, offline: !!v.offline };
  } catch {
    return { running: false, offline: false };
  }
}

/* 卡片与弹窗共享的格式化方法 */
const helpers = {
  stateText(s) {
    return { waiting: '等待审批', busy: '忙碌', running: '运行中', idle: '空闲', offline: '未运行' }[s] || s;
  },
  fmtK(n) {
    return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1000 ? Math.round(n / 1000) + 'k' : String(n);
  },
  agoText(ts) {
    if (!ts) return '—';
    const d = (Date.now() / 1000) - ts;
    if (d < 60) return '刚刚';
    if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
    if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
    return `${Math.floor(d / 86400)} 天前`;
  },
  fmtAgo(ts) {
    if (!ts) return '最后活动 —';
    return `最后活动 ${this.agoText(ts)}`;
  },
  fmtMem(mb) {
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)}GB` : `${Math.round(mb)}MB`;
  },
  shortTitle(t) {
    const s = t || '';
    return s.length > 20 ? s.slice(0, 20) + '...' : s;
  },
};

const app = createApp({
  data() {
    return {
      agents: [],
      tasks: [],
      connected: false,
      lastFrame: null,
      showTaskPanel: false,
      form: { goal: '', steps: [{ agent: 'claude', prompt: '' }] },
      taskAgents: TASK_AGENTS,
      taskAgentNames: TASK_AGENT_NAMES,
      submitting: false,
      notifyEnabled: localStorage.getItem('ab_notify') === '1',
      collapsed: loadCollapsed(),   // 分区折叠状态（记住偏好）
      // agent 管理
      manageOpen: false,
      manageList: [],
      appCatalog: [],
      addCfg: { type: 'app', name: '', match: '', host: '127.0.0.1', port: '' },
      editId: null,        // 非空 = 表单处于编辑模式
      editSource: null,    // 'builtin' | 'custom'
      editDefaultName: '',
      // 会话弹窗
      sessOpen: false,
      sessAgent: null,
      sessList: [],
      sessHidden: [],
      sessView: 'all',   // 'all' | 'hidden'
      editingSessId: null,
      editingName: '',
      helpOpen: false,
      toast: { text: '', type: 'ok' },
      _toastTimer: null,
      _tick: 0,
    };
  },

  computed: {
    activeAgents() {   // 上层：等待审批（最需关注）在前，忙碌在后
      const rank = { waiting: 0, busy: 1 };
      return this.agents
        .filter((a) => a.state === 'waiting' || a.state === 'busy')
        .sort((x, y) => (rank[x.state] ?? 9) - (rank[y.state] ?? 9));
    },
    runningAgents() {   // 中层：运行中 + 空闲（空闲 = 进程活着只是没任务）
      return this.agents.filter((a) => a.state === 'running' || a.state === 'idle');
    },
    offlineAgents() {
      return this.agents.filter((a) => a.state === 'offline');
    },
    runningCount() {
      return this.agents.filter((a) => a.state === 'busy' || a.state === 'running').length;
    },
    editingBuiltin() {
      return !!this.editId && this.editSource === 'builtin';
    },
    editTitle() {
      if (!this.editId) return '添加自定义 agent';
      return this.editSource === 'custom' ? '编辑自定义 agent' : '编辑显示名称（内置 agent）';
    },
  },

  mounted() {
    this._prevWaiting = new Set();   // 上一帧处于等待审批的 agent id，用于通知去重
    this.connect();
    setInterval(() => { this._tick++; }, 1000); // 让「已运行 X 秒」走起来
  },

  methods: {
    ...helpers,

    connect() {
      const es = new EventSource('/api/status');
      es.onmessage = (e) => {
        try {
          const frame = JSON.parse(e.data);
          this.agents = frame.agents || [];
          this.tasks = frame.tasks || [];
          this.lastFrame = frame.ts;
          this.connected = true;
          this.syncSessionWaiting();   // 弹窗打开时，徽标跟随 SSE 实时增减
          this.checkWaitingNotify();   // 进入等待审批时弹系统通知（看板被遮挡时）
        } catch { /* 半包帧忽略 */ }
      };
      es.onerror = () => { this.connected = false; };  // EventSource 自动重连
    },

    kindLabel(k) {
      return { cli: 'CLI', app: '桌面应用', web: 'Web 服务' }[k] || k;
    },

    pipelineOf(a) {   // 该 agent 正在参与的流水线任务（第一条）
      const t = this.tasks.find((t) => t.status === 'running' &&
        t.steps.some((s) => s.agent === a.id && (s.status === 'running' || s.status === 'pending')));
      if (!t) return '';
      const i = t.steps.findIndex((s) => s.agent === a.id && (s.status === 'running' || s.status === 'pending'));
      const st = t.steps[i].status === 'running' ? '运行中' : '等待';
      return `▶ ${t.goal || t.id} · 步骤 ${i + 1}/${t.steps.length} ${st}`;
    },

    async jumpSession(agentId, s) {   // 卡片内会话预览行点击直达
      try {
        const r = await fetch(`/api/open/${agentId}/session`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: s.id, cwd: s.cwd || '' }),
        });
        const body = await r.json().catch(() => ({}));
        if (r.ok) this.showToast(body.message || '已在 iTerm2 打开该会话', 'ok');
        else this.showToast(body.detail || '跳转失败', 'error');
      } catch (e) {
        this.showToast(`跳转失败：${e}`, 'error');
      }
    },

    toggleCollapse(key) {
      this.collapsed[key] = !this.collapsed[key];
      localStorage.setItem('ab_collapse', JSON.stringify(this.collapsed));
    },

    async toggleNotify() {
      if (this.notifyEnabled) {
        this.notifyEnabled = false;
        localStorage.setItem('ab_notify', '0');
        this.showToast('已关闭系统通知', 'ok');
        return;
      }
      try {
        let perm = Notification.permission;
        if (perm === 'default') perm = await Notification.requestPermission();
        if (perm !== 'granted') {
          this.showToast('浏览器通知权限未授予，无法开启', 'error');
          return;
        }
        this.notifyEnabled = true;
        localStorage.setItem('ab_notify', '1');
        this.showToast('已开启：进入等待审批且看板不在前台时弹系统通知', 'ok');
      } catch (e) {
        this.showToast(`开启失败：${e}`, 'error');
      }
    },

    checkWaitingNotify() {
      const cur = new Set(this.agents.filter((a) => a.state === 'waiting').map((a) => a.id));
      // 只在看板被遮挡/切走时弹，正盯着看板时卡片动效已足够
      if (this.notifyEnabled && document.hidden) {
        for (const a of this.agents) {
          if (a.state === 'waiting' && !this._prevWaiting.has(a.id)) {
            try {
              new Notification(`${a.name} 等待审批`, { tag: a.id, body: a.activity || '需要你的确认' });
            } catch { /* 通知失败不影响看板 */ }
          }
        }
      }
      this._prevWaiting = cur;
    },

    async openManage() {
      this.manageOpen = true;
      this.helpOpen = false;
      try {
        const r = await fetch('/api/manage');
        const body = await r.json();
        this.manageList = body.agents || [];
        this.appCatalog = body.apps || [];
      } catch (e) {
        this.showToast(`加载管理数据失败：${e}`, 'error');
      }
    },

    openHelp() {
      clearTimeout(this._helpTimer);
      this.helpOpen = true;
    },
    scheduleCloseHelp() {
      clearTimeout(this._helpTimer);
      this._helpTimer = setTimeout(() => { this.helpOpen = false; }, 180);
    },

    async toggleHidden(a) {
      const hide = !a.hidden;
      a.hidden = hide; // 乐观更新，SSE 2 秒内同步卡片
      try {
        const r = await fetch('/api/manage/hidden', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: a.id, hidden: hide }),
        });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
        this.showToast(hide ? `已隐藏 ${a.name}` : `已显示 ${a.name}`, 'ok');
      } catch (e) {
        a.hidden = !hide; // 失败回滚
        this.showToast(`操作失败：${e}`, 'error');
      }
    },

    editAgent(a) {
      this.editId = a.id;
      this.editSource = a.source;
      this.editDefaultName = a.default_name || a.name;
      if (a.source === 'custom' && a.cfg) {
        this.addCfg = { type: a.cfg.type, name: a.cfg.name, match: a.cfg.match || '',
                        host: a.cfg.host || '127.0.0.1', port: a.cfg.port || '' };
      } else {
        this.addCfg = { type: 'app', name: a.name, match: '', host: '127.0.0.1', port: '' };
      }
      this.$nextTick(() => {
        const el = document.querySelector('.add-form');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });
    },

    cancelEdit() {
      this.editId = null;
      this.editSource = null;
      this.editDefaultName = '';
      this.addCfg = { type: this.addCfg.type, name: '', match: '', host: '127.0.0.1', port: '' };
    },

    async submitAgentForm() {
      const wasEdit = !!this.editId;
      const name = this.addCfg.name.trim();
      if (!name) { this.showToast('请填写显示名称', 'error'); return; }
      try {
        let r;
        if (wasEdit && this.editSource === 'builtin') {
          // 内置 agent：只改显示名（与默认相同视为恢复默认）
          r = await fetch('/api/manage/name', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: this.editId, name }),
          });
        } else {
          const payload = { name, type: this.addCfg.type, match: this.addCfg.match.trim(),
                            host: this.addCfg.host.trim() || '127.0.0.1', url: '' };
          if (payload.type === 'web') {
            payload.port = parseInt(this.addCfg.port, 10);
            if (!payload.port) { this.showToast('请填写 1-65535 的端口', 'error'); return; }
          } else if (!payload.match) {
            this.showToast('请填写识别串', 'error'); return;
          }
          r = await fetch(wasEdit ? `/api/manage/custom/${encodeURIComponent(this.editId)}` : '/api/manage/custom', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
        }
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || r.status);
        this.showToast(wasEdit ? '已保存修改，看板稍后同步' : `已添加 ${name}，看板稍后自动出现`, 'ok');
        this.cancelEdit();
        await this.openManage();
      } catch (e) {
        this.showToast(`${wasEdit ? '保存' : '添加'}失败：${e}`, 'error');
      }
    },

    async delCustom(a) {
      if (!confirm(`删除自定义 agent「${a.name}」？其配置将从 config.json 移除。`)) return;
      try {
        const r = await fetch(`/api/manage/custom/${encodeURIComponent(a.id)}`, { method: 'DELETE' });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status);
        this.showToast(`已删除 ${a.name}`, 'ok');
        this.manageList = this.manageList.filter((x) => x.id !== a.id);
      } catch (e) {
        this.showToast(`删除失败：${e}`, 'error');
      }
    },
    taskStatusText(s) {
      return { running: '运行中', done: '已完成', error: '出错', cancelled: '已取消' }[s] || s;
    },
    stepStatusText(s) {
      return { pending: '等待', running: '运行中', done: '完成', error: '出错', cancelled: '取消' }[s] || s;
    },

    fmtClock(ts) {
      if (!ts) return '—';
      return new Date(ts * 1000).toTimeString().slice(0, 8);
    },
    async openSessions(a) {
      this.sessAgent = a;
      this.sessOpen = true;
      this.sessList = [];
      this.sessHidden = [];
      this.sessView = 'all';
      try {
        const r = await fetch(`/api/sessions/${a.id}`);
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || r.status);
        this.sessList = (body.sessions || [])
          .sort((x, y) => (y.waiting ? 1 : 0) - (x.waiting ? 1 : 0));   // 等待审批置顶
        this.sessHidden = body.hidden || [];
      } catch (e) {
        this.showToast(`读取会话失败：${e}`, 'error');
      }
    },

    syncSessionWaiting() {
      // 弹窗打开期间，用 SSE 帧里的 waiting_sessions 实时刷新会话徽标
      if (!this.sessOpen || !this.sessAgent) return;
      const card = this.agents.find((a) => a.id === this.sessAgent.id);
      if (!card) return;
      const ws = new Set(card.waiting_sessions || []);
      const mark = (s) => { s.waiting = ws.has(s.id); };
      this.sessList.forEach(mark);
      this.sessHidden.forEach(mark);
    },

    toggleSessView() {
      this.sessView = this.sessView === 'all' ? 'hidden' : 'all';
    },

    startRename(s) {
      this.editingSessId = s.id;
      this.editingName = s.title || '';
      this.$nextTick(() => {
        const el = document.querySelector('.sess-edit-input');
        if (el) { el.focus(); el.select(); }
      });
    },

    cancelRename() {
      this.editingSessId = null;
      this.editingName = '';
    },

    async saveRename(s) {
      if (this.editingSessId !== s.id) return;   // Enter 后触发的 blur 不重复保存
      const name = this.editingName.trim();
      try {
        const r = await fetch(`/api/sessions/${this.sessAgent.id}/rename`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: s.id, name }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || r.status);
        if (name) {
          s.title = name;
          s.renamed = true;
          this.showToast('已保存自定义名称', 'ok');
        } else {
          s.title = s.orig_title;
          s.renamed = false;
          this.showToast('已恢复默认名称', 'ok');
        }
      } catch (e) {
        this.showToast(`改名失败：${e}`, 'error');
      } finally {
        this.cancelRename();
      }
    },

    async hideSession(s, hidden) {
      if (!this.sessAgent) return;
      try {
        const r = await fetch(`/api/sessions/${this.sessAgent.id}/hide`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: s.id, hidden }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || r.status);
        if (hidden) {
          this.sessList = this.sessList.filter((x) => x.id !== s.id);
          this.sessHidden.unshift(s);
          this.showToast('已隐藏，可在「隐藏中的会话」里恢复', 'ok');
        } else {
          this.sessHidden = this.sessHidden.filter((x) => x.id !== s.id);
          this.sessList.unshift(s);
          this.showToast('已恢复显示', 'ok');
        }
      } catch (e) {
        this.showToast(`操作失败：${e}`, 'error');
      }
    },

    async openSession(s) {
      if (!this.sessAgent) return;
      if (this.editingSessId) return;   // 正在改名时，行内任何点击都不跳转
      try {
        const r = await fetch(`/api/open/${this.sessAgent.id}/session`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: s.id, cwd: s.cwd || '' }),
        });
        const body = await r.json().catch(() => ({}));
        if (r.ok) {
          this.showToast(body.message || '已在 iTerm2 打开该会话', 'ok');
          this.sessOpen = false;
        } else {
          this.showToast(body.detail || '打开会话失败', 'error');
        }
      } catch (e) {
        this.showToast(`打开会话失败：${e}`, 'error');
      }
    },
    fmtDur(t) {
      this._tick; // 依赖 tick 触发重算
      const end = t.status === 'running' ? Date.now() / 1000 : (t.steps.at(-1)?.ended_at || t.created_at);
      const d = Math.max(0, end - t.created_at);
      if (d < 60) return `${Math.floor(d)} 秒`;
      return `${Math.floor(d / 60)} 分 ${Math.floor(d % 60)} 秒`;
    },
    async enter(a) {
      try {
        const r = await fetch(`/api/open/${a.id}`, { method: 'POST' });
        const body = await r.json().catch(() => ({}));
        if (r.ok) this.showToast(body.message || `已进入 ${a.name}`, 'ok');
        else this.showToast(body.detail || `进入 ${a.name} 失败`, 'error');
      } catch (e) {
        this.showToast(`进入 ${a.name} 失败：${e}`, 'error');
      }
    },

    addStep() {
      this.form.steps.push({ agent: 'claude', prompt: '' });
    },

    async submitTask() {
      if (this.submitting) return;
      const steps = this.form.steps
        .map((s) => ({ agent: s.agent, prompt: s.prompt.trim() }))
        .filter((s) => s.prompt);
      if (!steps.length) {
        this.showToast('至少填写一个步骤的提示词', 'error');
        return;
      }
      this.submitting = true;
      try {
        const r = await fetch('/api/tasks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ goal: this.form.goal.trim(), steps }),
        });
        const body = await r.json().catch(() => ({}));
        if (r.ok) {
          this.showToast(`任务已发起（${steps.length} 步）`, 'ok');
          this.form = { goal: '', steps: [{ agent: 'claude', prompt: '' }] };
        } else {
          this.showToast(body.detail || '任务发起失败', 'error');
        }
      } catch (e) {
        this.showToast(`任务发起失败：${e}`, 'error');
      } finally {
        this.submitting = false;
      }
    },

    async cancelTask(id) {
      try {
        const r = await fetch(`/api/tasks/${id}/cancel`, { method: 'POST' });
        if (!r.ok) this.showToast('取消失败', 'error');
      } catch (e) {
        this.showToast(`取消失败：${e}`, 'error');
      }
    },

    showToast(text, type) {
      this.toast = { text, type };
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.toast.text = ''; }, 3500);
    },
  },
});

/* 卡片组件：三分区共用，enter/sessions 事件交由根实例处理 */
/* 卡片组件：三分区共用，事件交由根实例处理 */
app.component('agent-card', {
  props: {
    a: { type: Object, required: true },
    pline: { type: String, default: '' },   // 流水线状态条（根实例计算）
  },
  emits: ['enter', 'sessions', 'jump'],
  methods: {
    ...helpers,
    ident(a) {   // 身份行：模型 · 版本 · 来源
      const s = a.stats || {};
      return [s.model, s.version ? 'v' + s.version : '', s.originator].filter(Boolean).join(' · ');
    },
    statsText(a) {   // 统计行：今日会话 / 消息数 / token / 上下文 / 代码增删 / 并行
      const s = a.stats || {};
      const parts = [];
      if (s.today_sessions != null) parts.push(`今日 ${s.today_sessions} 会话`);
      if (s.total_sessions != null) parts.push(`共 ${s.total_sessions}`);
      if (s.message_count != null) parts.push(`${s.message_count} 条消息`);
      if (s.total_tokens != null) parts.push(`tok ${this.fmtK(s.total_tokens)}`);
      if (s.ctx_used != null) parts.push(`上下文 ${s.ctx_used}%`);
      if (s.parallel_sessions > 1) parts.push(`并行 ${s.parallel_sessions} 会话`);
      if (s.add != null) parts.push(`+${s.add} -${s.del ?? 0}`);
      return parts.join(' · ');
    },
    pendingText(a) {   // 待批准内容（hook 写入的审批消息）
      const msgs = Object.values(a.waiting_msgs || {}).filter(Boolean);
      if (!msgs.length) return '';
      const m = msgs[0].match(/permission to use (.+)$/i);
      return m ? `待批准：${m[1]}` : `待批准：${msgs[0].slice(0, 40)}`;
    },
  },
  computed: {
    preview() { return (this.a.sessions_preview || []).slice(0, 3); },
    waitingCount() { return (this.a.waiting_sessions || []).length; },
  },
  template: `
<div class="card" :class="'st-' + a.state" @click="$emit('enter')"
     :title="a.kind === 'app' ? '点击打开该应用' : '点击进入该 agent'">
  <div class="card-top">
    <span class="dot" :class="a.state"></span><b>{{ a.name }}</b>
    <span class="state-label" :class="a.state" v-if="a.state !== 'waiting'">{{ stateText(a.state) }}</span>
    <span class="state-label waiting" v-else><span class="wch" v-for="(ch, i) in '等待审批'" :key="i" :style="{ animationDelay: (i * 0.5) + 's' }">{{ ch }}</span><span class="wcount" v-if="waitingCount"> ({{ waitingCount }})</span></span>
    <span class="enter">
      <button class="linklike" v-if="a.has_sessions" @click.stop="$emit('sessions')">会话</button>
      <button class="linklike" @click.stop="$emit('enter')">{{ a.kind === 'app' ? '打开 →' : '进入 →' }}</button>
    </span>
  </div>
  <div class="line pend" v-if="a.state === 'waiting' && pendingText(a)"><span class="ico">⏸</span><span class="oneline">{{ pendingText(a) }}</span></div>
  <div class="line ident" v-if="ident(a)"><span class="ico">⚙</span><span class="oneline">{{ ident(a) }}</span></div>
  <div class="line" v-if="a.project"><span class="ico">📁</span>{{ a.project }}</div>
  <div class="line" v-if="a.title && !preview.length"><span class="ico">💬</span><span class="oneline">{{ a.title }}</span></div>
  <div class="sess-prev" v-if="preview.length">
    <div class="sp-row" v-for="s in preview" :key="s.id" @click.stop="$emit('jump', s)" title="点击跳转该会话">
      <span class="oneline sp-title">💬 {{ shortTitle(s.title) || '未命名会话' }}</span>
      <span class="sp-time">{{ agoText(s.last_active) }}</span>
    </div>
  </div>
  <div class="line stats" v-if="statsText(a)"><span class="ico">📊</span><span class="oneline">{{ statsText(a) }}</span></div>
  <div class="line pline" v-if="pline">{{ pline }}</div>
  <div class="line muted">
    {{ fmtAgo(a.last_active) }}<template v-if="a.cpu != null"> · CPU {{ a.cpu }}%</template><template v-if="a.mem_mb"> · {{ fmtMem(a.mem_mb) }}</template>
  </div>
</div>`,
});

app.mount('#app');
