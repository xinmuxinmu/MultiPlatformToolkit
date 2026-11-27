const { createApp, ref, reactive, computed, onMounted, onUnmounted, watch } = Vue;
const onActivated = Vue.onActivated || function(fn) {};
const { createRouter, createWebHistory } = VueRouter;
const { ElMessage, ElMessageBox } = ElementPlus;

// 账号管理统一放在多平台工具的抖音页，火花内不再重复一套
const EMBEDDED = (() => {
  try { return window.self !== window.top || new URLSearchParams(location.search).get('embed') === '1'; }
  catch (e) { return true; }
})();
// 由消息查看器内嵌时隐藏火花自己的侧栏/顶栏，只保留页面内容
const CHROME_HIDDEN = (() => {
  try { return new URLSearchParams(location.search).get('chrome') === '0'; }
  catch (e) { return false; }
})();
function openAccountCenter() {
  var url = '/douyin/#accounts';
  try {
    if (window.self !== window.top) { window.top.location.href = url; return; }
  } catch (e) {}
  window.location.href = url;
}

// ===== API =====
const api = axios.create({ baseURL: (window.__SPARK_BASE__ || '') + '/api', timeout: 10000 });
api.interceptors.request.use(config => {
  const token = localStorage.getItem('token') || localStorage.getItem('douyin_token');
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
}, error => Promise.reject(error));
api.interceptors.response.use(response => {
  const ct = response.headers['content-type'];
  if (ct && ct.includes('application/json')) {
    const res = response.data;
    if (res && res.code !== undefined && res.code == 401) {
      const hadToken = !!response.config?.headers?.Authorization;
      localStorage.removeItem('token'); localStorage.removeItem('douyin_token');
      if (hadToken) { ElMessage.error('登录已过期，请重新登录'); setTimeout(() => window.location.replace((window.__SPARK_BASE__ || '') + '/login'), 1500); }
      return Promise.reject(new Error(res.data || '未授权'));
    }
    if (res && res.code !== undefined && res.code != 200 && res.code != '200') {
      ElMessage.error(res.data || res.msg || res.message || '请求失败');
      return Promise.reject(new Error(res.data || '请求失败'));
    }
    return res;
  }
  return response;
}, error => {
  if (error.response) {
    const status = error.response.status, data = error.response.data;
    if (status === 401) {
      const hadToken = !!error.config?.headers?.Authorization;
      localStorage.removeItem('token'); localStorage.removeItem('douyin_token');
      if (hadToken) { ElMessage.error('登录已过期，请重新登录'); setTimeout(() => window.location.replace((window.__SPARK_BASE__ || '') + '/login'), 1500); }
      return;
    } else if (status === 500) ElMessage.error('服务器内部错误');
    else if (status === 404) ElMessage.error('请求的资源不存在');
    else { const msg = data?.data || data?.msg || data?.message || error.message || `请求失败 (${status})`; ElMessage.error(msg); }
  } else if (error.request) ElMessage.error('无法连接到服务器，请检查后端服务是否启动');
  else ElMessage.error(error.message || '网络错误');
  return Promise.reject(error);
});

const API = {
  initBrowser: () => api.get('/Api/Init', { timeout: 60000 }),
  getInitStatus: () => api.get('/Api/GetInit'),
  getLoginStatus: () => api.get('/Api/GetLogin'),
  pnglogin: () => api.get('/Api/Pnglogin'),
  getScrlk: () => api.get('/Api/GetScrlk'),
  login: async (cookie) => {
    const jsonStr = JSON.stringify(cookie);
    const bytes = new TextEncoder().encode(jsonStr);
    const cs = new CompressionStream('gzip');
    const writer = cs.writable.getWriter(); writer.write(bytes); writer.close();
    const output = await new Response(cs.readable).arrayBuffer();
    const base64 = btoa(String.fromCharCode(...new Uint8Array(output)));
    return api.post('/Api/login', { cooke: base64, gzip_flag: true });
  },
  getLoginPng: () => api.get('/Api/login/Init/GetLoginPng'),
  dieLogin: () => api.get('/Api/DieLogin'),
  getCooker: (password) => api.get('/Api/login/Init/GetCooker', { params: { password } }),
  sendVerifyCode: (areacode, phone) => api.get('/Api/LoginPhone', { params: { areacode, phone } }),
  submitVerifyCode: (code) => api.get('/Api/LoginPhoneInput', { params: { code } }),
  logout: () => api.get('/Api/logout'),
  getFriendsList: (force = 0) => api.get('/Api/GetFriendsList', { params: { force }, timeout: 180000 }),
  repairFriendNames: () => api.get('/Api/RepairFriendNames', { timeout: 180000 }),
  sendMessage: (name, text) => api.get('/Api/Send', { params: { name, text } }),
  addTask: (time, name, text, send_type, sticker_src) => api.get('/Time/add', { params: { time, name, text, send_type: send_type || 'text', sticker_src: sticker_src || '' } }),
  delTask: (task_id) => api.get('/Time/del', { params: { task_id } }),
  editTask: (name, new_time) => api.get('/Time/edit', { params: { name, new_time } }),
  runTask: (task_id) => api.get('/Time/run', { params: { task_id }, timeout: 120000 }),
  runBatch: (task_ids) => api.post('/Time/runBatch', { task_ids }, { timeout: 30000 }),
  getProgress: () => api.get('/Time/progress'),
  cancelBatch: () => api.post('/Time/batchCancel'),
  getTaskList: () => api.get('/Time/getlist'),
  // 分组
  getGroups: () => api.get('/Group/getlist'),
  addGroup: (name, time, text, send_type, sticker_src) => api.get('/Group/add', { params: { name, time, text, send_type: send_type || 'text', sticker_src: sticker_src || '' } }),
  editGroup: (group_id, name, time, text, send_type, sticker_src) => api.get('/Group/edit', { params: { group_id, name, time, text, send_type, sticker_src } }),
  delGroup: (group_id) => api.get('/Group/del', { params: { group_id } }),
  setGroupMembers: (group_id, names) => api.post('/Group/setmembers', { group_id, names }),
  addGroupMembers: (group_id, names) => api.post('/Group/addmembers', { group_id, names }),
  removeGroupMember: (group_id, name) => api.get('/Group/removeMember', { params: { group_id, name } }),
  applyGroup: (group_id) => api.get('/Group/apply', { params: { group_id }, timeout: 300000 }),
  getUsername: () => api.get('/Api/GetUsername'),
  changePassword: (old_password, new_password) => api.get('/Api/ChangePassword', { params: { old_password, new_password } }),
  getLastLoginIP: () => api.get('/Api/GetLastLoginIP'),
  forceLogin: (password) => api.get('/Api/LoginDebug', { params: { password } }),
  getPort: () => api.get('/Api/GetPort'),
  setPort: (port) => api.get('/Api/SetPort', { params: { port } }),
  getBrowserMode: () => api.get('/Api/GetBrowserMode'),
  setBrowserMode: (show) => api.get('/Api/SetBrowserMode', { params: { show } }),
  getBrowserConfig: () => api.get('/Api/GetBrowserConfig'),
  setBrowserConfig: (browser_name, browser_path) => api.get('/Api/SetBrowserConfig', { params: { browser_name, browser_path } }),
  getRiskConfig: () => api.get('/Api/GetRiskConfig'),
  getDefaults: () => api.get('/Api/GetDefaults'),
  setDefaults: (time, message) => api.get('/Api/SetDefaults', { params: { time, message } }),
  setRiskConfig: (payload) => api.post('/Api/SetRiskConfig', payload),
  getRiskStatus: () => api.get('/Api/GetRiskStatus'),
  resumeRisk: () => api.post('/Api/ResumeRisk'),
  getCalendar: (month) => api.get('/Api/SparkCalendar', { params: { month } }),
  getStickerList: (name, sec_uid) => api.get('/Api/GetStickerList', { params: { name, sec_uid }, timeout: 120000 }),
  sendSticker: (name, sticker_index, sticker_src) => api.get('/Api/SendSticker', { params: { name, sticker_index, sticker_src }, timeout: 30000 }),
  getChatHistory: (name) => api.get('/Api/GetChatHistory', { params: { name }, timeout: 30000 }),
  getHome: () => api.get('/Home'),
  // 多账号（cookie 管理器）
  listAccounts: () => api.get('/Api/Account/List'),
  addAccount: (name) => api.post('/Api/Account/Add', { name }),
  renameAccount: (account_id, name) => api.get('/Api/Account/Rename', { params: { account_id, name } }),
  removeAccount: (account_id) => api.get('/Api/Account/Remove', { params: { account_id } }),
  switchAccount: (account_id) => api.get('/Api/Account/Switch', { params: { account_id }, timeout: 180000 }),
  browserLogin: (payload) => api.post('/Api/Account/BrowserLogin', payload, { timeout: 120000 }),
  accountLoginState: () => api.get('/Api/Account/LoginState', { timeout: 30000 }),
  cancelLogin: () => api.post('/Api/Account/CancelLogin'),
};

// ===== Global State =====
const browserStatus = ref(false);
const loginStatus = ref(false);
const friendsList = ref([]);
const hasLoaded = ref(false);
const homeLoaded = ref(false);
// 本人头像已不再抓取（后端不再访问我的主页），统一用默认图标，避免显示过期/错误的头像
const douyinAvatar = ref('');
let _savedNick = localStorage.getItem('douyin_username');
if (_savedNick === '未知用户' || _savedNick === '抖音') { localStorage.removeItem('douyin_username'); _savedNick = ''; }
const douyinNickname = ref(_savedNick || '');
const setBrowserStatus = (s) => browserStatus.value = s;
const setLoginStatus = (s) => loginStatus.value = s;
const setFriendsList = (l) => { friendsList.value = l; hasLoaded.value = true; };
const setHomeLoaded = () => homeLoaded.value = true;
const setDouyinUser = (nickname, avatar) => {
  douyinNickname.value = nickname || '';
  douyinAvatar.value = '';
  localStorage.removeItem('douyin_avatar');
  if (nickname) localStorage.setItem('douyin_username', nickname); else localStorage.removeItem('douyin_username');
};

// ===== 多账号（cookie 管理器）全局状态 =====
const accounts = ref([]);
const activeAccountId = ref('');
const setAccounts = (list, activeId) => { accounts.value = list || []; activeAccountId.value = activeId || ''; };
const activeAccount = computed(() => accounts.value.find(a => a.id === activeAccountId.value) || null);
const activeAccountName = computed(() => activeAccount.value ? activeAccount.value.name : '');
const switchingAccount = ref(false);

// 顶栏与设置页共用的账号刷新
const fetchAccounts = async () => {
  try {
    const res = await API.listAccounts();
    if (res.code === 200 && res.data) setAccounts(res.data.accounts, res.data.active_id);
  } catch (e) { /* 忽略，下次再刷 */ }
};

// 切换抖音账号：成功后整页刷新，让好友/任务/分组等模块按新账号重新加载
const switchDouyinAccount = async (accountId) => {
  if (switchingAccount.value) return false;
  switchingAccount.value = true;
  try {
    const res = await API.switchAccount(accountId);
    if (res.code === 200) {
      ElMessage.success(res.data || '切换成功');
      setTimeout(() => window.location.reload(), 800);
      return true;
    }
    ElMessage.error(res.data || '切换失败');
  } catch (e) { ElMessage.error('切换失败，请重试'); }
  finally { switchingAccount.value = false; }
  return false;
};

// ===== User Store =====
const userStore = reactive({
  token: localStorage.getItem('token') || '',
  userInfo: JSON.parse(localStorage.getItem('userInfo') || '{}'),
  get isLoggedIn() { return !!this.token; },
  async login(username, password) {
    try {
      const res = await axios.get('/api/Api/Login/Admin', { params: { username, password } });
      if (res.data.code == 200 || res.data.code == '200') {
        this.token = res.data.data;
        this.userInfo = { username, loginTime: new Date().toISOString() };
        localStorage.setItem('token', this.token);
        localStorage.setItem('userInfo', JSON.stringify(this.userInfo));
        axios.defaults.headers.common['Authorization'] = `Bearer ${this.token}`;
        return { success: true, message: '登录成功' };
      }
      return { success: false, message: res.data.data || '登录失败' };
    } catch (error) {
      return { success: false, message: error.response?.data?.data || error.message || '登录失败' };
    }
  },
  logout() {
    this.token = ''; this.userInfo = {};
    localStorage.removeItem('token'); localStorage.removeItem('userInfo');
    delete axios.defaults.headers.common['Authorization'];
  },
  restoreSession() {
    if (this.token) axios.defaults.headers.common['Authorization'] = `Bearer ${this.token}`;
  }
});

// ===== Login Component =====
const Login = {
  template: `
  <div class="login-page">
    <div class="login-orb login-orb-1"></div>
    <div class="login-orb login-orb-2"></div>
    <div class="login-orb login-orb-3"></div>
    <div class="login-shell">
      <div class="login-card">
        <div class="login-header">
          <div class="login-logo">
            <div class="login-logo-ring"></div>
            <span class="login-logo-emoji">🔥</span>
          </div>
          <h1 class="login-title">抖音火花助手</h1>
          <p class="login-subtitle">SPARK ASSISTANT</p>
        </div>
        <el-form ref="loginFormRef" :model="loginForm" :rules="loginRules" @keyup.enter="handleLogin">
          <el-form-item prop="username" class="login-form-item">
            <el-input v-model="loginForm.username" placeholder="请输入用户名" size="large" :prefix-icon="UserIcon" />
          </el-form-item>
          <el-form-item prop="password" class="login-form-item">
            <el-input v-model="loginForm.password" type="password" placeholder="请输入密码" size="large" :prefix-icon="LockIcon" show-password />
          </el-form-item>
          <el-form-item class="login-btn-wrap">
            <el-button type="primary" size="large" :loading="loading" class="login-btn" @click="handleLogin">
              {{ loading ? '登录中...' : '登 录' }}
            </el-button>
          </el-form-item>
        </el-form>
      </div>
    </div>
  </div>`,
  setup() {
    const router = VueRouter.useRouter();
    const loginFormRef = ref(null);
    const loading = ref(false);
    const loginForm = reactive({ username: '', password: '' });
    const loginRules = {
      username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
      password: [{ required: true, message: '请输入密码', trigger: 'blur' }, { min: 1, message: '密码不能为空', trigger: 'blur' }]
    };
    const handleLogin = async () => {
      if (!loginFormRef.value) return;
      await loginFormRef.value.validate(async (valid) => {
        if (valid) {
          loading.value = true;
          try {
            const result = await userStore.login(loginForm.username, loginForm.password);
            if (result.success) { ElMessage.success(result.message); router.push('/'); }
            else ElMessage.error(result.message);
          } catch (e) { ElMessage.error('登录失败'); }
          finally { loading.value = false; }
        }
      });
    };
    return { loginFormRef, loading, loginForm, loginRules, handleLogin, UserIcon: ElementPlusIconsVue.User, LockIcon: ElementPlusIconsVue.Lock };
  }
};

// ===== Layout Component =====
const Layout = {
  template: `
  <div class="app-shell" :class="{ embedded: embedded, 'no-chrome': chromeHidden }">
    <div v-if="isMobile && sidebarVisible" class="sidebar-overlay" @click="closeSidebar"></div>
    <aside class="sidebar" :class="{ collapsed: isCollapsed && !isMobile, 'sidebar-mobile': isMobile, 'sidebar-mobile-visible': sidebarVisible }">
      <div class="sidebar-header">
        <span class="sidebar-logo">🔥</span>
        <span v-if="!isCollapsed || (isMobile && sidebarVisible)" class="sidebar-title">抖音火花助手</span>
      </div>
      <div class="sidebar-menu">
        <div v-for="(item, idx) in menuList" :key="item.path" class="sidebar-item anim-fade-up" :class="{ active: activeMenu === item.path, ['stagger-' + (idx+1)]: true }" @click="handleMenuSelect(item.path)">
          <el-icon><component :is="item.icon" /></el-icon>
          <span class="sidebar-item-text">{{ item.title }}</span>
        </div>
      </div>
      <div class="sidebar-footer">
        <a href="https://github.com/Kounva/TikTokcn-AutoSpark" target="_blank" style="color:var(--text-muted);text-decoration:none;font-size:12px;display:flex;align-items:center;gap:6px">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
          <span v-if="!isCollapsed || isMobile" class="sidebar-footer-text">GitHub</span>
        </a>
      </div>
    </aside>
    <div class="main-area">
      <header class="topbar">
        <div class="topbar-left">
          <el-icon v-if="isMobile" class="topbar-menu-btn" @click="toggleSidebar"><component is="Expand" v-if="!sidebarVisible" /><component is="Fold" v-else /></el-icon>
          <div class="topbar-title">{{ currentMenuTitle }}</div>
        </div>
        <div class="topbar-right">
          <el-dropdown v-if="!embedded" trigger="click" @command="handleAccountCommand">
            <div class="topbar-user" style="cursor:pointer" title="点击切换抖音账号">
              <div class="topbar-avatar" :class="{ 'has-img': douyinAvatar }">
                <img v-if="douyinAvatar" :src="douyinAvatar" alt="头像" @error="douyinAvatar = ''" />
                <span v-else>{{ (douyinNickname || activeAccountName || userStore.userInfo.username || 'A')[0] }}</span>
              </div>
              <span class="topbar-username">{{ douyinNickname || activeAccountName || userStore.userInfo.username || 'Admin' }}</span>
              <el-icon style="font-size:14px;color:var(--text-secondary)"><component is="ArrowDown" /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <div style="padding:6px 16px 4px;font-size:12px;color:var(--text-muted);font-weight:600;letter-spacing:0.5px">切换抖音账号</div>
                <el-dropdown-item v-for="acc in accounts" :key="acc.id" :command="'switch:' + acc.id" :disabled="acc.is_active || switchingAccount">
                  <div style="display:flex;align-items:center;gap:8px;min-width:220px;padding:2px 0">
                    <el-icon v-if="acc.is_active" style="color:var(--success);font-size:15px"><component is="Check" /></el-icon>
                    <span v-else style="display:inline-block;width:15px;flex-shrink:0"></span>
                    <div style="flex:1;min-width:0">
                      <div style="font-weight:600;font-size:13px">{{ acc.name }}<el-tag v-if="acc.is_default" size="small" type="info" effect="plain" style="margin-left:6px;transform:scale(0.8)">默认</el-tag></div>
                      <div style="font-size:12px;color:var(--text-muted);margin-top:1px">
                        <template v-if="acc.nickname">{{ acc.nickname }}</template>
                        <template v-else-if="acc.is_active && loginStatus">已登录</template>
                        <template v-else>未登录</template>
                        <span v-if="acc.has_cookies" style="margin-left:4px">· Cookie已存档</span>
                      </div>
                    </div>
                    <el-tag v-if="acc.is_active" type="success" size="small" effect="plain">当前</el-tag>
                  </div>
                </el-dropdown-item>
                <el-dropdown-item divided command="logout"><el-icon style="margin-right:6px"><component is="SwitchButton" /></el-icon>退出登录（管理后台）</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>
      <div class="page-body">
        <router-view v-slot="{ Component }">
          <keep-alive :include="cachedViews">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </div>
  </div>`,
  setup() {
    const router = VueRouter.useRouter();
    const route = VueRouter.useRoute();
    const isCollapsed = ref(false);
    const isMobile = ref(false);
    const sidebarVisible = ref(false);
    const checkMobile = () => {
      isMobile.value = window.innerWidth < 768;
      if (isMobile.value) { isCollapsed.value = true; sidebarVisible.value = false; }
      else sidebarVisible.value = true;
    };
    const toggleSidebar = () => { if (isMobile.value) sidebarVisible.value = !sidebarVisible.value; else isCollapsed.value = !isCollapsed.value; };
    const closeSidebar = () => { if (isMobile.value) sidebarVisible.value = false; };
    const fetchDouyinUser = () => { API.getUsername().then(res => { if (res.code == 200 && res.data && (res.data.nickname || res.data.avatar)) setDouyinUser(res.data.nickname || '', res.data.avatar || ''); }).catch(() => {}); };
    onMounted(() => { checkMobile(); window.addEventListener('resize', checkMobile);
      // 已登录就刷新一次头像，避免本地缓存的旧头像一直生效
      if (loginStatus.value) fetchDouyinUser();
      // 顶栏账号切换器：拉取多账号列表
      fetchAccounts();
    });
    let _userRetryTimer = null;
    Vue.watch(loginStatus, (val) => { if (val) { clearTimeout(_userRetryTimer); _userRetryTimer = setTimeout(fetchDouyinUser, 1500); } fetchAccounts(); });
    const menuList = [
      { path: '/home', title: '首页', icon: 'House' },
      { path: '/friends', title: '好友列表', icon: 'User' },
      { path: '/groups', title: '好友分组', icon: 'FolderOpened' },
      { path: '/tasks', title: '定时任务', icon: 'Clock' },
      { path: '/calendar', title: '火花日历', icon: 'Calendar' },
      { path: '/settings', title: '设置', icon: 'Setting' }
    ];
    const cachedViews = ['Friends', 'Chat', 'Tasks', 'SparkCalendar'];
    const activeMenu = computed(() => route.path);
    const currentMenuTitle = computed(() => { const m = menuList.find(i => i.path === activeMenu.value); return m ? m.title : ''; });
    const handleMenuSelect = (path) => { router.push(path); if (isMobile.value) sidebarVisible.value = false; };
    const handleCommand = (cmd) => {
      if (cmd === 'logout') {
        ElMessageBox.confirm('确定要退出登录吗？', '提示', { type: 'warning' }).then(async () => {
          try { await API.logout(); } catch (e) {}
          userStore.logout(); setDouyinUser('', ''); localStorage.removeItem('douyin_username_loaded'); router.push('/login');
        }).catch(() => {});
      }
    };
    // 顶栏账号切换器
    const handleAccountCommand = (cmd) => {
      if (cmd === 'logout') { handleCommand('logout'); return; }
      if (typeof cmd === 'string' && cmd.startsWith('switch:')) {
        const acc = accounts.value.find(a => a.id === cmd.slice(7));
        if (!acc || acc.is_active) return;
        ElMessageBox.confirm(
          `确定切换到账号「${acc.name}」？切换会重启浏览器，并加载该账号的好友、定时任务与分组。`,
          '切换抖音账号', { confirmButtonText: '切换', cancelButtonText: '取消', type: 'info' }
        ).then(async () => { await switchDouyinAccount(acc.id); }).catch(() => {});
      }
    };
    return { embedded: EMBEDDED, chromeHidden: CHROME_HIDDEN, isCollapsed, isMobile, sidebarVisible, toggleSidebar, closeSidebar, menuList, cachedViews, activeMenu, currentMenuTitle, handleMenuSelect, handleCommand, handleAccountCommand, userStore, browserStatus, douyinAvatar, douyinNickname, accounts, activeAccountName, switchingAccount, loginStatus };
  }
};

// ===== Home Component =====
const Home = {
  template: `
  <div>
    <div class="home-hero anim-fade-up">
      <h1 class="home-hero-title">👋 欢迎使用抖音火花助手</h1>
      <p class="home-hero-sub">自动化管理你的抖音好友火花，保持联系不间断</p>
    </div>
    <div class="stats-row">
      <div class="stat-tile anim-fade-up stagger-1" :style="{ cursor: initLoading ? 'wait' : 'pointer', opacity: initLoading ? 0.6 : 1 }" @click="initBrowser">
        <div class="stat-tile-icon accent"><el-icon><component is="Monitor" /></el-icon></div>
        <div class="stat-tile-label">浏览器状态</div>
        <div class="stat-tile-value">{{ initLoading ? '初始化中...' : (browserStatus ? '已初始化' : '未初始化') }}<span class="status-pill" :class="browserStatus ? 'online' : 'offline'"></span></div>
      </div>
      <div class="stat-tile anim-fade-up stagger-2">
        <div class="stat-tile-icon purple"><el-icon><component is="Key" /></el-icon></div>
        <div class="stat-tile-label">登录状态</div>
        <div class="stat-tile-value">{{ loginStatus ? '已登录' : '未登录' }}<span class="status-pill" :class="loginStatus ? 'online' : 'offline'"></span></div>
        <div v-if="activeAccountName" style="margin-top:6px;font-size:12px;color:var(--text-muted)">当前账号：{{ activeAccountName }}</div>
      </div>
      <div class="stat-tile anim-fade-up stagger-3">
        <div class="stat-tile-icon mint"><el-icon><component is="User" /></el-icon></div>
        <div class="stat-tile-label">好友数量</div>
        <div class="stat-tile-value">{{ friendsCount }} <span style="font-size:14px;color:var(--text-muted)">人</span></div>
      </div>
      <div class="stat-tile anim-fade-up stagger-4" style="cursor:pointer" @click="$router.push('/tasks')">
        <div class="stat-tile-icon cyan"><el-icon><component is="Clock" /></el-icon></div>
        <div class="stat-tile-label">定时任务</div>
        <div class="stat-tile-value">{{ taskCount }} <span style="font-size:14px;color:var(--text-muted)">个</span></div>
      </div>
    </div>
    <el-row :gutter="20" style="margin-bottom:24px">
      <el-col :xs="24" :lg="14">
        <div class="glass-shell anim-fade-up stagger-2">
          <div class="glass-core">
            <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Operation" /></el-icon></div>快速操作</div>
            <div class="action-grid">
              <div class="action-tile" :style="{ opacity: initLoading ? 0.6 : 1, cursor: initLoading ? 'wait' : 'pointer' }" @click="initBrowser">
                <div class="action-tile-icon accent"><el-icon><component is="Monitor" /></el-icon></div>
                <div class="action-tile-label">{{ initLoading ? '初始化中...' : '初始化浏览器' }}</div>
                <div class="action-tile-hint">启动自动化环境</div>
              </div>
              <div class="action-tile" @click="refreshFriends">
                <div class="action-tile-icon purple"><el-icon><component is="Refresh" /></el-icon></div>
                <div class="action-tile-label">刷新好友列表</div>
                <div class="action-tile-hint">更新好友数据</div>
              </div>
              <div class="action-tile" @click="$router.push('/tasks')">
                <div class="action-tile-icon mint"><el-icon><component is="Clock" /></el-icon></div>
                <div class="action-tile-label">管理任务</div>
                <div class="action-tile-hint">添加或修改</div>
              </div>
              <div class="action-tile" @click="$router.push('/settings')">
                <div class="action-tile-icon cyan"><el-icon><component is="Setting" /></el-icon></div>
                <div class="action-tile-label">系统设置</div>
                <div class="action-tile-hint">配置账户</div>
              </div>
            </div>
          </div>
        </div>
      </el-col>
      <el-col :xs="24" :lg="10">
        <div class="glass-shell anim-fade-up stagger-3">
          <div class="glass-core">
            <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="InfoFilled" /></el-icon></div>系统信息</div>
            <div class="info-row"><span class="info-row-label">系统版本</span><span class="info-row-value">v1.0.0</span></div>
            <div class="info-row"><span class="info-row-label">运行时间</span><span class="info-row-value" style="color:var(--success)">{{ uptime }}</span></div>
            <div class="info-row"><span class="info-row-label">后端地址</span><span class="info-row-value">{{ backendPort }}</span></div>
            <div class="info-row"><span class="info-row-label">运行状态</span><el-tag :type="browserStatus && loginStatus ? 'success' : 'warning'" size="small">{{ browserStatus && loginStatus ? '正常' : '待初始化' }}</el-tag></div>
            <div class="info-row"><span class="info-row-label">自动任务</span><el-tag :type="riskStatus.paused ? 'danger' : 'success'" size="small">{{ riskStatus.paused ? '已暂停' : '运行中' }}</el-tag></div>
            <div class="info-row"><span class="info-row-label">今日发送</span><span class="info-row-value">{{ riskStatus.today_send_count || 0 }} 次</span></div>
          </div>
        </div>
      </el-col>
    </el-row>
    <div class="glass-shell anim-fade-up stagger-4" style="margin-bottom:24px">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Warning" /></el-icon></div>风险状态</div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px">
          <el-tag :type="riskStatus.paused ? 'danger' : 'success'" effect="dark">{{ riskStatus.paused ? '自动任务已暂停' : '自动任务正常' }}</el-tag>
          <span style="color:var(--text-secondary);font-size:13px">连续失败：{{ riskStatus.consecutive_failures || 0 }} / {{ riskStatus.max_consecutive_failures || 0 }}</span>
          <span style="color:var(--text-secondary);font-size:13px">随机延迟：{{ riskStatus.task_jitter_minutes || 0 }} 分钟</span>
        </div>
        <div v-if="riskStatus.pause_reason" style="color:var(--danger);font-size:13px;line-height:1.6">{{ riskStatus.pause_reason }}</div>
        <div v-else-if="riskStatus.last_error" style="color:var(--warning);font-size:13px;line-height:1.6">{{ riskStatus.last_error }}</div>
        <div v-else style="color:var(--text-muted);font-size:13px">暂未检测到异常，系统会自动记录发送结果并在连续失败时暂停任务。</div>
      </div>
    </div>
    <div class="glass-shell anim-fade-up stagger-4">
      <div class="glass-core">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <div class="panel-title" style="margin-bottom:0"><div class="panel-title-icon"><el-icon><component is="Calendar" /></el-icon></div>最近定时任务</div>
          <el-button type="primary" link @click="$router.push('/tasks')">查看全部 <el-icon><component is="ArrowRight" /></el-icon></el-button>
        </div>
        <el-empty v-if="recentTasks.length === 0" description="暂无定时任务">
          <el-button type="primary" @click="$router.push('/tasks')">添加任务</el-button>
        </el-empty>
        <el-table v-else :data="recentTasks" stripe style="width:100%">
          <el-table-column type="index" label="#" width="60" align="center" />
          <el-table-column prop="name" label="好友" min-width="120" />
          <el-table-column prop="time" label="执行时间" width="120" align="center">
            <template #default="{ row }"><el-tag type="info" effect="plain">{{ row.time }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="next_run" label="下次执行" min-width="180">
            <template #default="{ row }"><span style="color:var(--text-secondary);font-size:13px">{{ row.next_run || '未设置' }}</span></template>
          </el-table-column>
          <el-table-column prop="last_result" label="最近结果" min-width="140">
            <template #default="{ row }"><span :style="{ color: row.last_result && row.last_result.includes('成功') ? 'var(--success)' : 'var(--text-secondary)', fontSize: '13px' }">{{ row.last_result || '未执行' }}</span></template>
          </el-table-column>
        </el-table>
      </div>
    </div>
  </div>`,
  setup() {
    const friendsCount = ref(0), taskCount = ref(0), recentTasks = ref([]), initLoading = ref(false), uptime = ref('--');
    const riskStatus = reactive({ paused: false, pause_reason: '', last_error: '', consecutive_failures: 0, max_consecutive_failures: 0, today_send_count: 0, task_jitter_minutes: 0 });
    const backendPort = 'http://127.0.0.1:' + (window.location.port || '8088');
    const formatUptime = (st) => {
      const s = new Date(st), n = new Date(), d = Math.floor((n - s) / 1000);
      const days = Math.floor(d / 86400), h = Math.floor((d % 86400) / 3600), m = Math.floor((d % 3600) / 60), sec = d % 60;
      const p = []; if (days > 0) p.push(`${days}天`); if (h > 0 || days > 0) p.push(`${h}小时`); if (m > 0 || h > 0 || days > 0) p.push(`${m}分`); p.push(`${sec}秒`); return p.join(' ');
    };
    const checkBrowserStatus = async () => {
      try { const res = await API.getInitStatus(); browserStatus.value = res.data === 'Yes'; setBrowserStatus(browserStatus.value); }
      catch (e) { browserStatus.value = false; setBrowserStatus(false); }
    };
    let _lastLoginCheck = 0;
    const checkLoginStatus = async (force = false) => { const now = Date.now(); if (!force && now - _lastLoginCheck < 30000) return; _lastLoginCheck = now; try { const res = await API.getLoginStatus(); loginStatus.value = res.data === 'Yes'; setLoginStatus(loginStatus.value); if (loginStatus.value && !douyinNickname.value) { try { const u = await API.getUsername(); if (u.code == 200 && u.data) setDouyinUser(u.data.nickname || '', u.data.avatar || ''); } catch (e) {} } else if (!loginStatus.value) { setDouyinUser('', ''); } } catch (e) { loginStatus.value = false; setLoginStatus(false); } };
    const refreshFriends = async () => {
      try { const res = await API.getFriendsList(); const list = res.data.list || {}; const fl = Object.entries(list).map(([name, [avatar, fire]]) => ({ name, avatar, fire })); setFriendsList(fl); friendsCount.value = res.data.count || 0; ElMessage.success('刷新成功');
      } catch (e) {
        const unauth = e.response?.status === 401 || e.message?.includes('未授权') || e.message?.includes('登录已过期');
        if (unauth) ElMessageBox.confirm('您还未登录抖音账号，是否前往登录？', '提示', { confirmButtonText: '前往登录', cancelButtonText: '取消', type: 'warning' }).then(() => openAccountCenter()).catch(() => {});
        else ElMessage.error('刷新失败');
      }
    };
    const loadTaskList = async () => { try { const res = await API.getTaskList(); taskCount.value = res.data.count || 0; recentTasks.value = res.data.tasks?.slice(0, 5) || []; localStorage.setItem('douyin_tasks', JSON.stringify(res.data.tasks || [])); } catch (e) {} };
    const loadRiskStatus = async () => { try { const res = await API.getRiskStatus(); if (res.code === 200 && res.data) Object.assign(riskStatus, res.data); } catch (e) {} };
    const initBrowser = async () => {
      if (initLoading.value) return;  // 防止重复点击
      initLoading.value = true;
      try { const res = await API.initBrowser(); if (res.code === 200) { browserStatus.value = true; setBrowserStatus(true); setDouyinUser('', ''); localStorage.removeItem('douyin_username_loaded'); ElMessage.success('浏览器初始化成功'); await checkLoginStatus(true); if (!loginStatus.value) ElMessageBox.confirm('浏览器初始化成功，但您还未登录抖音账号，是否前往登录？', '提示', { confirmButtonText: '前往登录', cancelButtonText: '稍后', type: 'warning' }).then(() => openAccountCenter()).catch(() => {}); } }
      catch (e) { ElMessage.error('浏览器初始化失败'); } finally { initLoading.value = false; }
    };
    let _uptimeTimer = null;
    const startUptime = (startTime) => { if (_uptimeTimer) clearInterval(_uptimeTimer); uptime.value = formatUptime(startTime); _uptimeTimer = setInterval(() => uptime.value = formatUptime(startTime), 1000); };
    onMounted(async () => {
      if (!accounts.value.length) await fetchAccounts();
      if (!homeLoaded.value) {
        await checkBrowserStatus(); await checkLoginStatus();
        if (browserStatus.value && loginStatus.value) { await refreshFriends(); await loadTaskList(); await loadRiskStatus(); }
        try { const res = await API.getHome(); if (res.time) { localStorage.setItem('douyin_start_time', res.time); startUptime(res.time); } } catch (e) { uptime.value = '获取失败'; }
        setHomeLoaded();
      } else { friendsCount.value = friendsList.value.length; const t = JSON.parse(localStorage.getItem('douyin_tasks') || '[]'); taskCount.value = t.length; recentTasks.value = t.slice(0, 5); const st = localStorage.getItem('douyin_start_time'); if (st) startUptime(st); await loadRiskStatus(); }
    });
    onUnmounted(() => { if (_uptimeTimer) clearInterval(_uptimeTimer); });
    return { friendsCount, taskCount, recentTasks, initLoading, uptime, backendPort, browserStatus, loginStatus, riskStatus, initBrowser, refreshFriends, activeAccountName };
  }
};

// ===== Friends Component =====
const Friends = {
  name: 'Friends',
  template: `
  <div class="glass-shell anim-fade-up">
    <div class="glass-core">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
        <div class="panel-title" style="margin-bottom:0"><div class="panel-title-icon"><el-icon><component is="User" /></el-icon></div>好友列表</div>
        <div style="display:flex;gap:10px">
          <el-button v-if="!selectionMode" type="primary" :icon="ElementPlusIconsVue.Refresh" @click="loadFriends(1)" :loading="loading">刷新</el-button>
          <el-button v-if="!selectionMode" type="success" :icon="ElementPlusIconsVue.Tickets" @click="selectionMode = true">多选</el-button>
          <template v-if="selectionMode">
            <el-button :icon="ElementPlusIconsVue.Select" @click="selectAllFiltered">全选筛选 ({{ filteredFriends.length }})</el-button>
            <el-button type="success" :icon="ElementPlusIconsVue.Check" @click="openBatchTaskDialog">创建任务 ({{ selectedFriends.length }})</el-button>
            <el-button type="warning" :icon="ElementPlusIconsVue.FolderAdd" @click="openAddToGroupDialog">加入分组 ({{ selectedFriends.length }})</el-button>
            <el-button :icon="ElementPlusIconsVue.Close" @click="cancelSelection">取消</el-button>
          </template>
        </div>
      </div>
      <div v-if="friendsCachedAt" style="font-size:12px;color:var(--text-muted);margin-bottom:10px">
        好友数据时间：{{ friendsCachedAt }}
        <span v-if="friendsStale">（来自本地缓存，浏览器未初始化时也能查看；点「刷新」可更新）</span>
      </div>
      <el-alert v-if="missingNames > 0" type="warning" :closable="false" show-icon style="margin-bottom:12px">
        <template #title>
          有 {{ missingNames }} 位好友没取到昵称（显示为「未命名好友（尾号…）」，仍可按 ID 收发消息）
          <el-button type="primary" link @click="repairNames" :loading="repairing" style="margin-left:6px">重新获取昵称</el-button>
        </template>
      </el-alert>
      <div class="toolbar-row">
        <el-input v-model="searchKeyword" placeholder="搜索好友..." :prefix-icon="ElementPlusIconsVue.Search" clearable style="max-width:240px" />
        <el-select v-model="fireFilter" placeholder="火花筛选" clearable style="width:140px">
          <el-option label="全部" value="all" /><el-option label="有火花" value="has" /><el-option label="无火花" value="none" />
        </el-select>
        <el-select v-model="groupFilter" placeholder="分组筛选" clearable style="width:150px">
          <el-option label="未分组" value="__none__" />
          <el-option v-for="g in groupsData" :key="g.id" :label="g.name" :value="g.name" />
        </el-select>
      </div>
      <el-table ref="friendsTable" v-loading="loading" :data="filteredFriends" stripe style="width:100%" @selection-change="handleSelectionChange">
        <el-table-column v-if="selectionMode" type="selection" width="50" />
        <el-table-column type="index" label="序号" :width="selectionMode ? 80 : 60" />
        <el-table-column label="头像" width="80"><template #default="{ row }"><el-avatar :size="40" :src="row.avatar"><el-icon><component is="User" /></el-icon></el-avatar></template></el-table-column>
        <el-table-column label="昵称" min-width="120">
          <template #default="{ row }">
            <span>{{ row.name }}</span>
            <el-tag v-if="row.name.startsWith('未命名好友')" size="small" type="warning" style="margin-left:6px">名称未获取</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分组" width="120">
          <template #default="{ row }">
            <el-tag v-if="groupMap[row.name]" size="small" type="success">{{ groupMap[row.name] }}</el-tag>
            <span v-else style="color:var(--text-dim);font-size:12px">未分组</span>
          </template>
        </el-table-column>
        <el-table-column prop="fire" label="火花天数" width="100"><template #default="{ row }"><el-tag v-if="isFireActive(row.fire)" type="warning">{{ row.fire }}🔥</el-tag><el-tag v-else type="info">无火花</el-tag></template></el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-dropdown @command="(cmd) => handleCommand(cmd, row)" trigger="click">
              <el-button type="primary" size="small">操作<el-icon class="el-icon--right"><component is="ArrowDown" /></el-icon></el-button>
              <template #dropdown><el-dropdown-menu><el-dropdown-item command="send">发起聊天</el-dropdown-item><el-dropdown-item command="create">创建任务</el-dropdown-item></el-dropdown-menu></template>
            </el-dropdown>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && filteredFriends.length === 0" description="暂无好友数据" />
      <el-dialog v-model="sendDialogVisible" title="发送消息" width="500px" destroy-on-close append-to-body>
        <el-form :model="sendForm" label-width="80px"><el-form-item label="好友"><el-input v-model="sendForm.name" disabled /></el-form-item><el-form-item label="消息内容"><el-input v-model="sendForm.text" type="textarea" :rows="4" placeholder="请输入消息内容" /></el-form-item></el-form>
        <template #footer><el-button @click="sendDialogVisible = false">取消</el-button><el-button type="primary" @click="handleSend" :loading="sendLoading">发送</el-button></template>
      </el-dialog>
      <el-dialog v-model="taskDialogVisible" title="创建定时任务" width="500px" destroy-on-close append-to-body>
        <el-form :model="taskForm" label-width="80px"><el-form-item label="好友"><el-input v-model="taskForm.name" disabled /></el-form-item><el-form-item label="执行时间"><el-time-picker v-model="taskForm.time" format="HH:mm" value-format="HH:mm" placeholder="选择时间" style="width:100%" /></el-form-item><el-form-item label="消息内容"><el-input v-model="taskForm.text" type="textarea" :rows="3" placeholder="留空使用全局模板池；多条模板可用 || 或换行分隔" /></el-form-item></el-form>
        <template #footer><el-button @click="taskDialogVisible = false">取消</el-button><el-button type="primary" @click="handleCreateTask" :loading="taskLoading">创建</el-button></template>
      </el-dialog>
      <el-dialog v-model="batchTaskDialogVisible" title="批量创建定时任务" width="600px" destroy-on-close append-to-body>
        <div style="display:flex;flex-wrap:wrap;align-items:center;max-height:120px;overflow-y:auto">
          <span style="font-weight:500;margin-right:8px">已选 ({{ selectedFriends.length }})：</span>
          <el-tag v-for="f in selectedFriends" :key="f.name" style="margin:4px">{{ f.name }}</el-tag>
        </div><el-divider />
        <el-form :model="batchTaskForm" label-width="86px">
          <el-form-item label="执行时间"><el-time-picker v-model="batchTaskForm.time" format="HH:mm" value-format="HH:mm" placeholder="选择时间" style="width:100%" /></el-form-item>
          <el-form-item label="发送内容">
            <el-radio-group v-model="batchTaskForm.send_type">
              <el-radio-button label="text">文字</el-radio-button>
              <el-radio-button label="sticker">表情包</el-radio-button>
              <el-radio-button label="both">文字+表情</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="batchTaskForm.send_type !== 'sticker'" label="消息内容"><el-input v-model="batchTaskForm.text" type="textarea" :rows="3" placeholder="留空使用全局模板池；多条模板可用 || 或换行分隔" /></el-form-item>
          <el-form-item v-if="batchTaskForm.send_type !== 'text'" label="选择表情">
            <div style="display:flex;align-items:center;gap:10px">
              <img v-if="batchTaskForm.sticker_src" :src="batchTaskForm.sticker_src" style="width:56px;height:56px;object-fit:contain;border:1px solid var(--glass-border-soft);border-radius:8px" />
              <span v-else style="font-size:12px;color:var(--text-muted)">还没有选择表情（所有选中好友发同一个表情）</span>
              <el-button size="small" @click="batchStickerVisible = true">选择表情</el-button>
            </div>
          </el-form-item>
        </el-form>
        <sticker-picker v-model="batchStickerVisible"
                        :friend="selectedFriends.length ? selectedFriends[0].name : ''"
                        @pick="src => (batchTaskForm.sticker_src = src)" />
        <template #footer><el-button @click="batchTaskDialogVisible = false">取消</el-button><el-button type="primary" @click="handleBatchCreateTask" :loading="batchTaskLoading">批量创建 ({{ selectedFriends.length }})</el-button></template>
      </el-dialog>
      <el-dialog v-model="groupDialogVisible" title="加入分组" width="440px" destroy-on-close append-to-body>
        <el-form label-width="80px">
          <el-form-item label="目标分组">
            <el-select v-model="targetGroupId" placeholder="选择分组" style="width:100%">
              <el-option v-for="g in groupsData" :key="g.id" :label="g.name + '（' + g.member_count + ' 人）'" :value="g.id" />
            </el-select>
          </el-form-item>
        </el-form>
        <div style="font-size:12px;color:var(--text-muted);line-height:1.7">
          已选 {{ selectedFriends.length }} 位好友。好友若已在其他分组，会自动移到新分组（一个好友只归属一个组）。
          加入后到「好友分组」页点「应用到任务」即可按组的时间与消息批量创建任务。
        </div>
        <template #footer><el-button @click="groupDialogVisible = false">取消</el-button><el-button type="primary" :loading="groupSaving" @click="handleAddToGroup">加入</el-button></template>
      </el-dialog>
    </div>
  </div>`,
  setup() {
    const router = VueRouter.useRouter();
    const loading = ref(false), searchKeyword = ref(''), fireFilter = ref('');
    const friendsCachedAt = ref(''), friendsStale = ref(false);
    const groupsData = ref([]), groupMap = ref({}), groupFilter = ref('');
    const groupDialogVisible = ref(false), targetGroupId = ref(''), groupSaving = ref(false);
    const isFireActive = (fire) => { if (!fire) return false; const n = Number(fire); return !isNaN(n) ? n > 0 : true; };
    const sendDialogVisible = ref(false), sendLoading = ref(false), sendForm = ref({ name: '', text: '' });
    const taskDialogVisible = ref(false), taskLoading = ref(false), taskForm = ref({ name: '', time: '', text: '' });
    const selectionMode = ref(false), selectedFriends = ref([]), batchTaskDialogVisible = ref(false), batchTaskLoading = ref(false), batchStickerVisible = ref(false), batchTaskForm = ref({ time: '', text: '', send_type: 'text', sticker_src: '' });
    const loadGroups = async () => {
      try {
        const res = await API.getGroups();
        if (res.code === 200) { groupsData.value = res.data.groups || []; groupMap.value = res.data.member_map || {}; }
      } catch (e) { /* 分组加载失败不影响好友列表 */ }
    };
    const filteredFriends = computed(() => {
      let list = friendsList.value;
      if (searchKeyword.value) { const k = searchKeyword.value.toLowerCase(); list = list.filter(f => f.name.toLowerCase().includes(k)); }
      if (fireFilter.value && fireFilter.value !== 'all') list = list.filter(f => fireFilter.value === 'has' ? isFireActive(f.fire) : !isFireActive(f.fire));
      if (groupFilter.value === '__none__') list = list.filter(f => !groupMap.value[f.name]);
      else if (groupFilter.value) list = list.filter(f => groupMap.value[f.name] === groupFilter.value);
      return list;
    });
    const missingNames = ref(0), repairing = ref(false);
    const repairNames = async () => {
      repairing.value = true;
      const tip = ElMessage({ message: '正在重新抓取并补全昵称，约需 20 秒…', duration: 0, type: 'info' });
      try {
        const res = await API.repairFriendNames();
        if (res.code === 200) {
          ElMessage.success(res.data || '已重新获取');
          await loadFriends(0);
        } else {
          ElMessage.warning(res.data || '重新获取失败');
        }
      } catch (e) {
        ElMessage.error('重新获取昵称失败');
      } finally {
        tip.close();
        repairing.value = false;
      }
    };
    const loadFriends = async (force = 0) => {
      loading.value = true;
      // 好友多时需要逐屏滚动加载，给个明确提示，避免用户以为卡死
      const tip = ElMessage({ message: force ? '正在重新抓取全部好友，约需 20 秒…' : '正在加载好友…', duration: 0, type: 'info' });
      try { const res = await API.getFriendsList(force); if (res.code === 200) { const list = res.data.list || {}; const fl = Object.entries(list).map(([name, [avatar, fire]]) => ({ name, avatar, fire })); setFriendsList(fl); friendsCachedAt.value = res.data.cached_at || ''; friendsStale.value = !!res.data.stale; missingNames.value = res.data.missing_names || 0; } }
      catch (e) { ElMessage.error('加载好友列表失败'); } finally { tip.close(); loading.value = false; loadGroups(); }
    };
    const handleSend = async () => { if (!sendForm.value.text.trim()) { ElMessage.warning('请输入消息内容'); return; } sendLoading.value = true; try { await API.sendMessage(sendForm.value.name, sendForm.value.text); ElMessage.success('发送成功'); sendDialogVisible.value = false; } catch (e) { ElMessage.error('发送失败'); } finally { sendLoading.value = false; } };
    const handleCommand = async (cmd, row) => {
      if (cmd === 'send') { router.push({ path: '/chat', query: { name: row.name } }); }
      else if (cmd === 'create') {
        taskForm.value = { name: row.name, time: '', text: '' };
        try {
          const r = await API.getDefaults();
          if (r.code === 200) taskForm.value = { name: row.name, time: r.data.time || '', text: r.data.message || '' };
        } catch (e) { /* 忽略 */ }
        taskDialogVisible.value = true;
      }
    };
    const handleCreateTask = async () => { if (!taskForm.value.time) { ElMessage.warning('请选择时间'); return; } taskLoading.value = true; try { await API.addTask(taskForm.value.time, taskForm.value.name, taskForm.value.text || null); ElMessage.success('创建成功'); taskDialogVisible.value = false; } catch (e) { ElMessage.error('创建失败'); } finally { taskLoading.value = false; } };
    const handleSelectionChange = (rows) => selectedFriends.value = rows;
    const friendsTable = ref(null);
    const selectAllFiltered = () => {
      const t = friendsTable.value;
      if (!t) return;
      t.clearSelection();
      // 只选中当前筛选结果，不误选被搜索/火花/分组条件过滤掉的人
      filteredFriends.value.forEach(row => t.toggleRowSelection(row, true));
      ElMessage.success(`已选中当前筛选出的 ${filteredFriends.value.length} 位好友`);
    };
    const cancelSelection = () => { selectionMode.value = false; selectedFriends.value = []; };
    const openBatchTaskDialog = async () => {
      if (selectedFriends.value.length === 0) { ElMessage.warning('请先选择好友'); return; }
      batchTaskForm.value = { time: '', text: '', send_type: 'text', sticker_src: '' };
      try {
        const r = await API.getDefaults();
        if (r.code === 200) batchTaskForm.value = { time: r.data.time || '', text: r.data.message || '' };
      } catch (e) { /* 忽略 */ }
      batchTaskDialogVisible.value = true;
    };
    const handleBatchCreateTask = async () => { if (!batchTaskForm.value.time) { ElMessage.warning('请选择时间'); return; } if (batchTaskForm.value.send_type !== 'text' && !batchTaskForm.value.sticker_src) { ElMessage.warning('请先选择一个表情'); return; } batchTaskLoading.value = true; let s = 0, f = 0; try { for (const fr of selectedFriends.value) { try { await API.addTask(batchTaskForm.value.time, fr.name, batchTaskForm.value.text || null, batchTaskForm.value.send_type, batchTaskForm.value.sticker_src); s++; } catch { f++; } } if (f === 0) ElMessage.success(`批量创建成功，共 ${s} 个任务`); else ElMessage.warning(`完成：成功 ${s} 个，失败 ${f} 个`); batchTaskDialogVisible.value = false; cancelSelection(); } finally { batchTaskLoading.value = false; } };
    const openAddToGroupDialog = () => {
      if (selectedFriends.value.length === 0) { ElMessage.warning('请先选择好友'); return; }
      if (groupsData.value.length === 0) { ElMessage.warning('还没有分组，请先到「好友分组」页新建'); return; }
      targetGroupId.value = ''; groupDialogVisible.value = true;
    };
    const handleAddToGroup = async () => {
      if (!targetGroupId.value) { ElMessage.warning('请选择目标分组'); return; }
      groupSaving.value = true;
      try {
        const names = selectedFriends.value.map(f => f.name);
        await API.addGroupMembers(targetGroupId.value, names);
        ElMessage.success(`已把 ${names.length} 位好友加入分组`);
        groupDialogVisible.value = false;
        await loadGroups();
        cancelSelection();
      } catch (e) { /* 拦截器已提示 */ } finally { groupSaving.value = false; }
    };
    onMounted(() => { loadGroups(); if (friendsList.value.length === 0) loadFriends(0); });
    return { loading, searchKeyword, fireFilter, friendsCachedAt, friendsStale, missingNames, repairing, repairNames, isFireActive, filteredFriends, loadFriends, sendDialogVisible, sendLoading, sendForm, handleSend, handleCommand, taskDialogVisible, taskLoading, taskForm, handleCreateTask, selectionMode, selectedFriends, batchTaskDialogVisible, batchTaskLoading, batchTaskForm, batchStickerVisible, handleSelectionChange, friendsTable, selectAllFiltered, cancelSelection, openBatchTaskDialog, handleBatchCreateTask, groupsData, groupMap, groupFilter, groupDialogVisible, targetGroupId, groupSaving, openAddToGroupDialog, handleAddToGroup, ElementPlusIconsVue };
  }
};

// ===== Chat Component =====
const Chat = {
  name: 'Chat',
  template: `
  <div class="glass-shell anim-fade-up chat-shell">
    <div class="glass-core chat-core">
      <div class="chat-container">
        <!-- 左侧好友列表 -->
        <div class="chat-sidebar">
          <div class="chat-sidebar-header">
            <el-input v-model="searchKeyword" placeholder="搜索好友..." :prefix-icon="ElementPlusIconsVue.Search" clearable size="small" />
            <el-button :icon="ElementPlusIconsVue.Refresh" @click="loadFriends" :loading="friendsLoading" size="small" circle style="margin-left:8px;flex-shrink:0" />
          </div>
          <div class="chat-friend-list">
            <div v-for="friend in filteredFriends" :key="friend.name"
                 class="chat-friend-item" :class="{ active: currentChat === friend.name }"
                 @click="selectFriend(friend)">
              <el-avatar :size="40" :src="friend.avatar"><el-icon><component is="User" /></el-icon></el-avatar>
              <div class="chat-friend-info">
                <div class="chat-friend-name">{{ friend.name }}</div>
                <div class="chat-friend-fire" v-if="isFireActive(friend.fire)">{{ friend.fire }} 🔥</div>
              </div>
            </div>
            <el-empty v-if="!friendsLoading && filteredFriends.length === 0" description="暂无好友" :image-size="60" />
          </div>
        </div>

        <!-- 右侧聊天区域 -->
        <div class="chat-main">
          <template v-if="currentChat">
            <div class="chat-header">
              <div class="chat-header-info">
                <span class="chat-header-name">{{ currentChat }}</span>
              </div>
              <el-button :icon="ElementPlusIconsVue.Refresh" @click="loadChatHistory" :loading="historyLoading" size="small" text>刷新记录</el-button>
            </div>

            <div class="chat-messages" ref="messagesContainer">
              <div v-if="historyLoading" class="chat-loading">
                <el-icon class="is-loading" style="font-size:28px;color:var(--text-muted)"><component is="Loading" /></el-icon>
                <span style="margin-left:8px;color:var(--text-muted)">加载消息中...</span>
              </div>
              <template v-else>
                <div v-for="(msg, i) in chatMessages" :key="i"
                     class="chat-msg-row" :class="msg.is_self ? 'self' : 'other'">
                  <div class="chat-avatar">
                    <el-avatar :size="32" :src="msg.is_self ? (douyinAvatar || undefined) : currentFriendAvatar">
                      <el-icon><component is="User" /></el-icon>
                    </el-avatar>
                  </div>
                  <div class="chat-bubble" :class="msg.is_self ? 'bubble-self' : 'bubble-other'">{{ msg.text }}</div>
                </div>
                <div v-if="chatMessages.length === 0" class="chat-empty">
                  <el-icon style="font-size:40px;color:var(--text-muted)"><component is="ChatLineRound" /></el-icon>
                  <p style="color:var(--text-muted);margin-top:8px">暂无消息记录，发送一条消息开始对话</p>
                </div>
              </template>
            </div>

            <!-- 表情面板 -->
            <transition name="sticker-slide">
              <div v-if="stickerPanelVisible" class="sticker-panel">
                <div v-if="stickerLoading" class="sticker-loading">
                  <el-icon class="is-loading" style="font-size:24px"><component is="Loading" /></el-icon>
                  <span style="margin-left:8px">加载表情包中...</span>
                </div>
                <template v-else-if="stickerCategories.length > 0">
                  <div class="sticker-tabs">
                    <div v-for="(cat, i) in stickerCategories" :key="i"
                         class="sticker-tab" :class="{active: activeStickerTab === i}"
                         :title="cat.label + '（' + (cat.stickers || []).length + ' 个）'"
                         @click="activeStickerTab = i">
                      <span v-if="cat.icon_html" class="sticker-tab-icon" v-html="cat.icon_html"></span>
                      <span class="sticker-tab-label">{{ cat.label }}</span>
                    </div>
                  </div>
                  <div class="sticker-grid">
                    <div v-for="(src, j) in stickerCategories[activeStickerTab]?.stickers || []" :key="j"
                         class="sticker-item" @click="sendSticker(src)">
                      <img :src="src" :alt="'表情' + (j+1)" loading="lazy" />
                    </div>
                  </div>
                </template>
                <el-empty v-else description="未获取到表情包" :image-size="50" />
              </div>
            </transition>

            <!-- 发送阶段提示：发送要定位会话+输入+确认，耗时几秒，显示进度避免以为卡死 -->
            <div v-if="sendStage" style="padding:0 6px 6px;font-size:12px;color:var(--text-muted);display:flex;align-items:center;gap:6px">
              <el-icon class="is-loading" style="font-size:13px"><component is="Loading" /></el-icon>
              <span>{{ sendStage }}</span>
            </div>
            <!-- 输入区域 -->
            <div class="chat-input-area">
              <button class="sticker-btn" @click="toggleStickerPanel" :class="{ active: stickerPanelVisible }" :disabled="stickerLoading" title="表情包">😀</button>
              <el-input v-model="messageText" placeholder="输入消息，回车发送..."
                        @keyup.enter="sendMessage" :disabled="sendLoading"
                        type="textarea" :autosize="{ minRows: 1, maxRows: 4 }"
                        resize="none" style="flex:1" />
              <el-button type="primary" @click="sendMessage" :loading="sendLoading"
                         :disabled="!messageText.trim()" :icon="ElementPlusIconsVue.Promotion">发送</el-button>
            </div>
          </template>

          <!-- 未选择好友时的占位 -->
          <div v-else class="chat-placeholder">
            <el-icon style="font-size:64px;color:var(--text-muted);opacity:0.5"><component is="ChatDotRound" /></el-icon>
            <p style="color:var(--text-muted);margin-top:16px;font-size:15px">选择好友开始聊天</p>
            <p style="color:var(--text-muted);margin-top:4px;font-size:12px;opacity:0.7">支持发送文字和抖音内置表情包</p>
          </div>
        </div>
      </div>
    </div>
  </div>`,
  setup() {
    const route = VueRouter.useRoute();
    const searchKeyword = ref('');
    const currentChat = ref('');
    const currentFriendAvatar = ref('');
    const chatMessages = ref([]);
    const messageText = ref('');
    const stickerPanelVisible = ref(false);
    const stickerCategories = ref([]);
    const activeStickerTab = ref(0);
    const stickerLoading = ref(false);
    const stickerLoaded = ref(false);
    const sendLoading = ref(false);
    const sendStage = ref('');
    let sendStageTimer = null;
    // 发送期间轮询后端进度，显示「正在打开会话 / 正在输入并发送 / 等待发送确认」
    const startSendStage = () => {
      sendStage.value = '正在准备发送…';
      if (sendStageTimer) clearInterval(sendStageTimer);
      sendStageTimer = setInterval(async () => {
        try {
          const r = await API.getProgress();
          if (r.code === 200 && r.data && r.data.running) sendStage.value = r.data.stage || '正在发送…';
        } catch (e) { /* 忽略轮询失败 */ }
      }, 700);
    };
    const stopSendStage = () => {
      if (sendStageTimer) { clearInterval(sendStageTimer); sendStageTimer = null; }
      sendStage.value = '';
    };
    onUnmounted(stopSendStage);
    const historyLoading = ref(false);
    const friendsLoading = ref(false);
    const messagesContainer = ref(null);

    const isFireActive = (fire) => { if (!fire) return false; const n = Number(fire); return !isNaN(n) ? n > 0 : true; };

    const filteredFriends = computed(() => {
      let list = friendsList.value;
      if (searchKeyword.value) { const k = searchKeyword.value.toLowerCase(); list = list.filter(f => f.name.toLowerCase().includes(k)); }
      return list;
    });

    const loadFriends = async () => {
      friendsLoading.value = true;
      try { const res = await API.getFriendsList(); if (res.code === 200) { const list = res.data.list || {}; setFriendsList(Object.entries(list).map(([name, [avatar, fire]]) => ({ name, avatar, fire }))); } }
      catch (e) { ElMessage.error('加载好友列表失败'); } finally { friendsLoading.value = false; }
    };

    const scrollToBottom = () => {
      setTimeout(() => {
        if (messagesContainer.value) messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight;
      }, 50);
    };

    const selectFriend = async (friend, forceReload = false) => {
      if (!friend) return;
      if (!forceReload && currentChat.value === friend.name) return;
      currentChat.value = friend.name;
      currentFriendAvatar.value = friend.avatar || '';
      chatMessages.value = [];
      stickerPanelVisible.value = false;
      await loadChatHistory();
    };

    const loadChatHistory = async () => {
      if (!currentChat.value) return;
      historyLoading.value = true;
      try {
        const res = await API.getChatHistory(currentChat.value);
        if (res.code === 200 && Array.isArray(res.data)) {
          chatMessages.value = res.data;
          scrollToBottom();
        } else {
          chatMessages.value = [];
        }
      } catch (e) {
        chatMessages.value = [];
      } finally {
        historyLoading.value = false;
      }
    };

    const sendMessage = async () => {
      const text = messageText.value.trim();
      if (!text || !currentChat.value || sendLoading.value) return;
      sendLoading.value = true;
      startSendStage();
      try {
        await API.sendMessage(currentChat.value, text);
        chatMessages.value.push({ text, is_self: true });
        messageText.value = '';
        scrollToBottom();
        ElMessage.success('发送成功');
      } catch (e) {
        ElMessage.error('发送失败');
      } finally {
        stopSendStage();
        sendLoading.value = false;
      }
    };

    const loadStickers = async () => {
      stickerLoading.value = true;
      try {
        const res = await API.getStickerList(currentChat.value);
        if (res.code == 200) {
          stickerCategories.value = res.data || [];
          activeStickerTab.value = 0;
          stickerLoaded.value = true;
        } else {
          stickerCategories.value = [];
          ElMessage.warning(res.data || '未获取到表情包');
        }
      } catch (e) {
        ElMessage.error('获取表情包失败');
      } finally {
        stickerLoading.value = false;
      }
    };

    const toggleStickerPanel = () => {
      stickerPanelVisible.value = !stickerPanelVisible.value;
      if (stickerPanelVisible.value && !stickerLoaded.value) {
        loadStickers();
      }
    };

    const sendSticker = async (src) => {
      if (!currentChat.value) return;
      stickerPanelVisible.value = false;
      sendLoading.value = true;
      startSendStage();
      try {
        await API.sendSticker(currentChat.value, 0, src || '');
        ElMessage.success('表情包已发送');
        await loadChatHistory();
      } catch (e) {
        if (!e || !e.message) ElMessage.error('表情包发送失败');
      } finally {
        stopSendStage();
        sendLoading.value = false;
      }
    };

    const syncRouteTarget = async () => {
      const targetName = typeof route.query.name === 'string' ? route.query.name : '';
      if (!targetName) return;
      if (friendsList.value.length === 0 && !friendsLoading.value) await loadFriends();
      const friend = friendsList.value.find(f => f.name === targetName);
      if (friend) await selectFriend(friend);
      else ElMessage.warning('未找到该好友');
    };

    onMounted(async () => {
      if (friendsList.value.length === 0) await loadFriends();
      await syncRouteTarget();
    });
    onActivated(async () => { await syncRouteTarget(); });
    watch(() => route.query.name, async () => { await syncRouteTarget(); });

    return {
      searchKeyword, currentChat, currentFriendAvatar, chatMessages, messageText,
      stickerPanelVisible, stickerCategories, activeStickerTab, stickerLoading, sendLoading, sendStage, historyLoading,
      friendsLoading, messagesContainer, filteredFriends, isFireActive,
      loadFriends, selectFriend, loadChatHistory, sendMessage,
      toggleStickerPanel, sendSticker, douyinAvatar, ElementPlusIconsVue
    };
  }
};

// ===== Tasks Component =====
const Tasks = {
  name: 'Tasks',
  template: `
  <div class="glass-shell anim-fade-up">
    <div class="glass-core">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
        <div class="panel-title" style="margin-bottom:0"><div class="panel-title-icon"><el-icon><component is="Clock" /></el-icon></div>定时任务管理</div>
        <div style="display:flex;gap:10px">
          <el-button v-if="!selectionMode" type="primary" :icon="ElementPlusIconsVue.Refresh" @click="refreshAll" :loading="loading">刷新</el-button>
          <el-button v-if="!selectionMode" type="success" :icon="ElementPlusIconsVue.Plus" @click="openAddDialog">添加任务</el-button>
          <template v-if="selectionMode">
            <el-button type="success" :icon="ElementPlusIconsVue.VideoPlay" @click="handleBatchRun" :loading="batchRunLoading">立即执行 ({{ selectedTasks.length }})</el-button>
            <el-button type="danger" :icon="ElementPlusIconsVue.Delete" @click="handleBatchDelete" :loading="batchDeleteLoading">删除 ({{ selectedTasks.length }})</el-button>
            <el-button :icon="ElementPlusIconsVue.Close" @click="cancelSelection">取消</el-button>
          </template>
          <el-button v-if="!selectionMode && taskList.length > 0" :icon="ElementPlusIconsVue.Tickets" @click="selectionMode = true">多选</el-button>
        </div>
      </div>
      <el-table v-loading="loading" :data="taskList" stripe style="width:100%" @selection-change="handleSelectionChange">
        <el-table-column v-if="selectionMode" type="selection" width="50" />
        <el-table-column type="index" label="序号" :width="selectionMode ? 80 : 60" />
        <el-table-column prop="name" label="好友" min-width="120" />
        <el-table-column label="类型" width="124">
          <template #default="{ row }">
            <span v-if="row.send_type !== 'text'" style="display:inline-flex;align-items:center;gap:6px">
              <img v-if="row.sticker_src" :src="row.sticker_src" style="width:26px;height:26px;object-fit:contain" />
              <el-tag v-else size="small" type="warning">表情包</el-tag>
              <el-tag v-if="row.send_type === 'both'" size="small" type="success">+文字</el-tag>
            </span>
            <el-tag v-else size="small">文字</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分组" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.group_name" size="small" type="success">{{ row.group_name }}</el-tag>
            <span v-else style="color:var(--text-dim);font-size:12px">未分组</span>
          </template>
        </el-table-column>
        <el-table-column prop="time" label="执行时间" width="100" />
        <el-table-column prop="next_run" label="下次执行" min-width="150" />
        <el-table-column prop="last_run_at" label="最近执行" min-width="160" />
        <el-table-column prop="last_result" label="结果" min-width="150" />
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button type="success" size="small" @click="handleRunNow(row)" :loading="runningId === row.task_id">立即执行</el-button>
            <el-button type="warning" size="small" @click="openEditDialog(row)">改时间</el-button>
            <el-button type="danger" size="small" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && taskList.length === 0" description="暂无定时任务"><el-button type="primary" @click="openAddDialog">添加第一个任务</el-button></el-empty>
      <!-- 发送进度：批量/单个发送时显示当前进度，避免只转圈不知道进行到哪 -->
      <el-dialog v-model="progressVisible" :title="progressTitle" width="470px"
                 :close-on-click-modal="false" :close-on-press-escape="false" :show-close="!progressData.running" append-to-body>
        <div style="padding:2px 4px">
          <el-progress :percentage="progressData.percent || 0" :status="progressStatus" :stroke-width="14" />
          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;font-size:13px;color:var(--text-secondary)">
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:290px">
              {{ progressData.running ? (progressData.stage || '准备中') : (progressData.message || '已完成') }}
              <b v-if="progressData.current && progressData.running" style="color:var(--text-primary)"> · {{ progressData.current }}</b>
            </span>
            <span>{{ progressData.done || 0 }}/{{ progressData.total || 0 }}</span>
          </div>
          <div style="display:flex;gap:16px;margin-top:8px;font-size:12px;color:var(--text-muted)">
            <span>成功 <b style="color:#67c23a">{{ progressData.ok || 0 }}</b></span>
            <span>失败 <b style="color:#f56c6c">{{ progressData.fail || 0 }}</b></span>
            <span v-if="progressData.running && progressData.total > 1">每人间隔 1.5~3 秒，可随时停止</span>
          </div>
        </div>
        <template #footer>
          <el-button v-if="progressData.running && progressData.total > 1" type="danger" plain @click="handleStopBatch">停止发送</el-button>
          <el-button v-else type="primary" :disabled="progressData.running" @click="progressVisible = false">关闭</el-button>
        </template>
      </el-dialog>
      <el-dialog v-model="dialogVisible" :title="dialogMode === 'add' ? '添加定时任务' : '修改执行时间'" width="500px" destroy-on-close append-to-body>
        <el-form :model="taskForm" label-width="86px">
          <el-form-item label="好友"><el-select v-model="taskForm.name" placeholder="选择好友" filterable :disabled="dialogMode === 'edit'" style="width:100%"><el-option v-for="f in availableFriends" :key="f.name" :label="f.name" :value="f.name" /></el-select></el-form-item>
          <el-form-item label="执行时间"><el-time-picker v-model="taskForm.time" format="HH:mm" value-format="HH:mm" placeholder="选择时间" style="width:100%" /></el-form-item>
          <el-form-item v-if="dialogMode === 'add'" label="发送内容">
            <el-radio-group v-model="taskForm.send_type">
              <el-radio-button label="text">文字</el-radio-button>
              <el-radio-button label="sticker">表情包</el-radio-button>
              <el-radio-button label="both">文字+表情</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="dialogMode === 'add' && taskForm.send_type !== 'sticker'" label="消息内容"><el-input v-model="taskForm.text" type="textarea" :rows="3" placeholder="留空使用全局模板池；多条模板可用 || 或换行分隔" /></el-form-item>
          <el-form-item v-if="dialogMode === 'add' && taskForm.send_type !== 'text'" label="选择表情">
            <div style="display:flex;align-items:center;gap:10px">
              <img v-if="taskForm.sticker_src" :src="taskForm.sticker_src" style="width:56px;height:56px;object-fit:contain;border:1px solid var(--glass-border-soft);border-radius:8px" />
              <span v-else style="font-size:12px;color:var(--text-muted)">还没有选择表情</span>
              <el-button size="small" @click="stickerDialogVisible = true">选择表情</el-button>
            </div>
          </el-form-item>
        </el-form>
        <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" @click="handleSubmit" :loading="submitLoading">{{ dialogMode === 'add' ? '添加' : '修改' }}</el-button></template>
      </el-dialog>
      <sticker-picker v-model="stickerDialogVisible" :friend="taskForm.name"
                      @pick="src => (taskForm.sticker_src = src)" />
    </div>
  </div>`,
  setup() {
    const loading = ref(false), taskList = ref([]), isFirstLoad = ref(true);
    const selectionMode = ref(false), selectedTasks = ref([]), batchDeleteLoading = ref(false);
    const dialogVisible = ref(false), dialogMode = ref('add'), submitLoading = ref(false);
    const taskForm = ref({ name: '', time: '', text: '', send_type: 'text', sticker_src: '' });
    // 表情选择（弹窗本身是全局组件 sticker-picker）
    const stickerDialogVisible = ref(false);
    const availableFriends = computed(() => { const ex = taskList.value.map(t => t.name); return friendsList.value.filter(f => !ex.includes(f.name)); });
    const refreshAll = async () => {
      loading.value = true;
      // 两个请求分开处理：好友列表失败（如浏览器未初始化）不应该连累任务列表展示
      try {
        const tr = await API.getTaskList();
        if (tr.code === 200) { taskList.value = tr.data.tasks || []; localStorage.setItem('douyin_tasks', JSON.stringify(tr.data.tasks || [])); }
      } catch (e) { ElMessage.error('加载任务列表失败'); }
      try {
        const fr = await API.getFriendsList();
        if (fr.code === 200) { const list = fr.data.list || {}; setFriendsList(Object.entries(list).map(([name, [avatar, fire]]) => ({ name, avatar, fire }))); }
      } catch (e) { /* 浏览器未初始化时忽略，任务列表照常显示 */ }
      loading.value = false;
    };
    const openAddDialog = async () => {
      dialogMode.value = 'add';
      taskForm.value = { name: '', time: '', text: '', send_type: 'text', sticker_src: '' };
      try {
        const r = await API.getDefaults();
        if (r.code === 200) taskForm.value = { name: '', time: r.data.time || '', text: r.data.message || '', send_type: 'text', sticker_src: '' };
      } catch (e) { /* 忽略，仍可手动填写 */ }
      dialogVisible.value = true;
    };
    const openEditDialog = (t) => { dialogMode.value = 'edit'; taskForm.value = { name: t.name, time: t.time, text: '', send_type: t.send_type || 'text', sticker_src: t.sticker_src || '' }; dialogVisible.value = true; };
    const handleSubmit = async () => { if (!taskForm.value.name && dialogMode.value === 'add') { ElMessage.warning('请选择好友'); return; } if (!taskForm.value.time) { ElMessage.warning('请选择时间'); return; } if (dialogMode.value === 'add' && taskForm.value.send_type !== 'text' && !taskForm.value.sticker_src) { ElMessage.warning('请先选择一个表情'); return; } submitLoading.value = true; try { if (dialogMode.value === 'add') { await API.addTask(taskForm.value.time, taskForm.value.name, taskForm.value.text || null, taskForm.value.send_type, taskForm.value.sticker_src); ElMessage.success({ text: '添加成功', sticker: '添加成功（每天发送表情包）', both: '添加成功（每天发送文字 + 表情包）' }[taskForm.value.send_type] || '添加成功'); } else { await API.editTask(taskForm.value.name, taskForm.value.time); ElMessage.success('修改成功'); } dialogVisible.value = false; await refreshAll(); } catch (e) { ElMessage.error(dialogMode.value === 'add' ? '添加失败' : '修改失败'); } finally { submitLoading.value = false; } };
    const handleDelete = async (t) => { try { await ElMessageBox.confirm(`确定要删除 ${t.name} 的定时任务吗？`, '提示', { type: 'warning' }); await API.delTask(t.task_id); ElMessage.success('删除成功'); await refreshAll(); } catch (e) { if (e !== 'cancel') ElMessage.error('删除失败'); } };
    const runningId = ref('');
    const batchRunLoading = ref(false);

    // ===== 发送进度 =====
    const progressVisible = ref(false);
    const progressData = ref({ running: false, percent: 0, done: 0, total: 0, ok: 0, fail: 0, current: '', stage: '', message: '', detail: [], kind: '' });
    let progressTimer = null;
    const progressTitle = computed(() => (progressData.value.kind === 'batch' ? '批量发送进度' : '发送进度'));
    const progressStatus = computed(() => {
      const d = progressData.value;
      if (d.running) return '';
      return (d.fail || 0) > 0 ? 'warning' : 'success';
    });
    const emptyProgress = (total, name, kind) => ({ running: true, percent: 0, done: 0, total: total || 0, ok: 0, fail: 0, current: name || '', stage: '准备中', message: '', detail: [], kind: kind });
    const pollProgress = async () => {
      try {
        const r = await API.getProgress();
        if (r.code === 200 && r.data) progressData.value = r.data;
      } catch (e) { /* 轮询失败不打扰用户，下一轮再试 */ }
    };
    const startProgressPoll = () => {
      stopProgressPoll();
      pollProgress();
      progressTimer = setInterval(pollProgress, 700);
    };
    const stopProgressPoll = () => { if (progressTimer) { clearInterval(progressTimer); progressTimer = null; } };
    // 轮询到 running=false 为止（批量发送是后台任务，接口先返回）
    const waitProgressDone = async () => {
      const deadline = Date.now() + 30 * 60 * 1000;
      await pollProgress();
      while (progressData.value.running && Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 700));
        await pollProgress();
      }
      return progressData.value;
    };
    onUnmounted(stopProgressPoll);

    const handleBatchRun = async () => {
      if (selectedTasks.value.length === 0) { ElMessage.warning('请先选择要执行的任务'); return; }
      const n = selectedTasks.value.length;
      try {
        await ElMessageBox.confirm(
          `将立即向选中的 ${n} 位好友依次发送消息，每人间隔 1.5~3 秒，好友多时整体耗时较长。确定继续？`,
          '批量立即执行',
          { type: 'warning', confirmButtonText: '开始发送', cancelButtonText: '取消' }
        );
      } catch (e) { return; }
      batchRunLoading.value = true;
      progressData.value = emptyProgress(n, '', 'batch');
      progressVisible.value = true;
      try {
        await API.runBatch(selectedTasks.value.map(t => t.task_id));
        const final = await waitProgressDone();
        progressVisible.value = false;
        const bad = (final.detail || []).filter(d => !d.ok);
        if (bad.length) {
          const html = bad.slice(-20).map(d => `${d.name}：${d.msg}`).join('<br>');
          ElMessageBox.alert(html, final.message || '批量执行完成', { type: 'warning', dangerouslyUseHTMLString: true });
        } else {
          ElMessage.success(final.message || '批量执行完成');
        }
        cancelSelection();
        await refreshAll();
      } catch (e) {
        progressVisible.value = false;
      } finally { batchRunLoading.value = false; }
    };
    const handleStopBatch = async () => {
      try {
        const r = await API.cancelBatch();
        ElMessage.info(r.data || '已请求停止');
      } catch (e) { /* 拦截器已提示 */ }
    };
    const handleRunNow = async (row) => {
      runningId.value = row.task_id;
      progressData.value = emptyProgress(1, row.name, 'single');
      progressVisible.value = true;
      startProgressPoll();
      try {
        const res = await API.runTask(row.task_id);
        ElMessage.success(res.data || '已执行');
        await refreshAll();
      } catch (e) { /* 拦截器已提示 */ } finally {
        stopProgressPoll();
        await pollProgress();
        progressVisible.value = false;
        runningId.value = '';
      }
    };
    const handleSelectionChange = (rows) => selectedTasks.value = rows;
    const cancelSelection = () => { selectionMode.value = false; selectedTasks.value = []; };
    const handleBatchDelete = async () => { if (selectedTasks.value.length === 0) { ElMessage.warning('请先选择要删除的任务'); return; } try { await ElMessageBox.confirm(`确定要删除选中的 ${selectedTasks.value.length} 个任务吗？`, '批量删除', { type: 'warning' }); batchDeleteLoading.value = true; let s = 0, f = 0; for (const t of selectedTasks.value) { try { await API.delTask(t.task_id); s++; } catch { f++; } } if (f === 0) ElMessage.success(`批量删除成功，共 ${s} 个`); else ElMessage.warning(`完成：成功 ${s} 个，失败 ${f} 个`); cancelSelection(); await refreshAll(); } catch (e) { if (e !== 'cancel') ElMessage.error('批量删除失败'); } finally { batchDeleteLoading.value = false; } };
    onMounted(async () => { if (isFirstLoad.value) { await refreshAll(); isFirstLoad.value = false; } else { const c = localStorage.getItem('douyin_tasks'); if (c) taskList.value = JSON.parse(c); } });
    return { loading, taskList, selectionMode, selectedTasks, batchDeleteLoading, dialogVisible, dialogMode, submitLoading, taskForm, availableFriends, refreshAll, openAddDialog, openEditDialog, handleSubmit, handleDelete, runningId, handleRunNow, batchRunLoading, handleBatchRun, handleSelectionChange, cancelSelection, handleBatchDelete, progressVisible, progressData, progressTitle, progressStatus, handleStopBatch, stickerDialogVisible, ElementPlusIconsVue };
  }
};

// ===== Groups Component =====
const Groups = {
  name: 'Groups',
  template: `
  <div class="glass-shell anim-fade-up">
    <div class="glass-core">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
        <div class="panel-title" style="margin-bottom:0">
          <div class="panel-title-icon"><el-icon><component is="FolderOpened" /></el-icon></div>好友分组
        </div>
        <div style="display:flex;gap:10px">
          <el-button :icon="ElementPlusIconsVue.Refresh" @click="loadAll" :loading="loading">刷新</el-button>
          <el-button type="primary" :icon="ElementPlusIconsVue.Plus" @click="openCreate">新建分组</el-button>
        </div>
      </div>

      <el-alert type="info" :closable="false" show-icon style="margin-bottom:18px"
        title="给分组设好发布时间和消息，点「应用到任务」即可为组内所有好友批量创建或更新定时任务；之后改组的时间或消息，再点一次就能同步。一个好友只归属一个分组。" />

      <el-empty v-if="!loading && groups.length === 0" description="还没有分组">
        <el-button type="primary" @click="openCreate">新建第一个分组</el-button>
      </el-empty>

      <div class="group-grid">
        <div class="group-card" v-for="g in groups" :key="g.id">
          <div class="group-card-head">
            <div class="group-name">{{ g.name }}</div>
            <el-tag size="small" :type="g.member_count > 0 && g.synced_count === g.member_count ? 'success' : 'info'">
              {{ g.synced_count }}/{{ g.member_count }} 已同步
            </el-tag>
          </div>
          <div class="group-meta">
            <span><el-icon><component is="Clock" /></el-icon>{{ g.time }}</span>
            <span v-if="g.send_type !== 'text'" class="group-msg" style="display:inline-flex;align-items:center;gap:6px" :title="g.text">
              <img v-if="g.sticker_src" :src="g.sticker_src" style="width:24px;height:24px;object-fit:contain" />
              <span>{{ g.send_type === 'both' ? ('文字 + 表情' + (g.text ? '：' + g.text : '')) : '每天发同一个表情' }}</span>
            </span>
            <span v-else class="group-msg" :title="g.text">{{ g.text || '未设置消息，将用模板池' }}</span>
          </div>
          <div class="group-members">
            <el-tag v-for="m in g.members.slice(0, 8)" :key="m.name" size="small"
              :type="m.synced ? 'success' : 'info'" style="margin:2px">{{ m.name }}</el-tag>
            <span v-if="g.members.length > 8" class="more-hint">+{{ g.members.length - 8 }}</span>
            <span v-if="g.members.length === 0" class="more-hint">暂无成员，点「勾选成员」添加</span>
          </div>
          <div class="group-actions">
            <el-button size="small" @click="openMembers(g)">勾选成员</el-button>
            <el-button size="small" type="primary" :loading="applyingId === g.id" @click="apply(g)">应用到任务</el-button>
            <el-button size="small" @click="openEdit(g)">编辑</el-button>
            <el-button size="small" type="danger" @click="removeGroup(g)">删除</el-button>
          </div>
        </div>
      </div>

      <el-dialog v-model="formVisible" :title="formMode === 'create' ? '新建分组' : '编辑分组'" width="480px" destroy-on-close append-to-body>
        <el-form :model="form" label-width="90px">
          <el-form-item label="分组名称"><el-input v-model="form.name" maxlength="40" placeholder="例如：家人 / 同事 / 老同学" /></el-form-item>
          <el-form-item label="发布时间"><el-time-picker v-model="form.time" format="HH:mm" value-format="HH:mm" placeholder="选择时间" style="width:100%" /></el-form-item>
          <el-form-item label="发送内容">
            <el-radio-group v-model="form.send_type">
              <el-radio-button label="text">文字</el-radio-button>
              <el-radio-button label="sticker">表情包</el-radio-button>
              <el-radio-button label="both">文字+表情</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="form.send_type !== 'sticker'" label="默认消息"><el-input v-model="form.text" type="textarea" :rows="4" placeholder="留空则使用全局模板池；多条可用 || 或换行分隔" /></el-form-item>
          <el-form-item v-if="form.send_type !== 'text'" label="选择表情">
            <div style="display:flex;align-items:center;gap:10px">
              <img v-if="form.sticker_src" :src="form.sticker_src" style="width:56px;height:56px;object-fit:contain;border:1px solid var(--glass-border-soft);border-radius:8px" />
              <span v-else style="font-size:12px;color:var(--text-muted)">组内好友每天都会收到这个表情</span>
              <el-button size="small" @click="openGroupSticker">选择表情</el-button>
            </div>
          </el-form-item>
        </el-form>
        <sticker-picker v-model="stickerVisible" :friend="stickerFriend" @pick="src => (form.sticker_src = src)" />
        <template #footer>
          <el-button @click="formVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="submitForm">保存</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="memberVisible" :title="'勾选成员 · ' + (currentGroup ? currentGroup.name : '')" width="620px" destroy-on-close append-to-body>
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap">
          <el-input v-model="memberKeyword" placeholder="搜索好友..." :prefix-icon="ElementPlusIconsVue.Search" clearable style="max-width:165px" />
          <el-select v-model="memberFireFilter" placeholder="火花筛选" clearable style="width:120px">
            <el-option label="有火花" value="has" />
            <el-option label="无火花" value="none" />
          </el-select>
          <el-button size="small" :icon="ElementPlusIconsVue.Refresh" @click="loadFriendsForPicker" :loading="friendsLoading">加载全部好友</el-button>
          <el-button size="small" @click="selectAllVisible">全选当前结果</el-button>
          <el-button size="small" @click="memberSelected = []">清空</el-button>
        </div>
        <div style="font-size:13px;color:var(--text-muted);margin-bottom:10px">
          已选 <b style="color:var(--accent)">{{ memberSelected.length }}</b> 位 ·
          当前筛选出 {{ filteredPickerFriends.length }} 位 · 共 {{ pickerFriends.length }} 位
        </div>
        <el-alert v-if="pickerFriends.length === 0 && !friendsLoading" type="warning" :closable="false" show-icon
          title="还没有好友数据。请先在首页初始化浏览器并登录抖音，再点「加载全部好友」。" style="margin-bottom:12px" />
        <el-checkbox-group v-model="memberSelected">
          <div class="member-picker">
            <el-checkbox v-for="f in filteredPickerFriends" :key="f.name" :value="f.name" :disabled="isOtherGroupMember(f.name)">
              <span>{{ f.name }}</span>
              <span v-if="isOtherGroupMember(f.name)" style="font-size:12px;color:var(--text-dim);margin-left:8px">（已属于 {{ memberMap[f.name] }}）</span>
              <span v-else-if="f.fire" style="font-size:12px;color:var(--text-muted);margin-left:8px">{{ f.fire }}🔥</span>
            </el-checkbox>
            <div v-if="filteredPickerFriends.length === 0 && pickerFriends.length > 0" style="padding:12px;color:var(--text-muted);font-size:13px">没有匹配的好友</div>
          </div>
        </el-checkbox-group>
        <template #footer>
          <el-button @click="memberVisible = false">取消</el-button>
          <el-button type="primary" :loading="savingMembers" @click="saveMembers">保存成员（{{ memberSelected.length }}）</el-button>
        </template>
      </el-dialog>
    </div>
  </div>`,
  setup() {
    const loading = ref(false), groups = ref([]), memberMap = ref({});
    const defaults = reactive({ time: '22:00', message: '' });
    const formVisible = ref(false), formMode = ref('create'), saving = ref(false);
    const form = ref({ id: '', name: '', time: '', text: '', send_type: 'text', sticker_src: '' });
    const stickerVisible = ref(false);
    // 表情面板需要打开一个会话才能读到，这里借用组内第一个成员（没有就用好友列表第一个）
    const stickerFriend = computed(() => {
      const g = groups.value.find(x => x.id === form.value.id);
      const member = g && g.members && g.members.length ? g.members[0].name : '';
      return member || (friendsList.value[0] ? friendsList.value[0].name : '');
    });
    const openGroupSticker = async () => {
      if (!stickerFriend.value) await loadFriendsForPicker();
      if (!stickerFriend.value) { ElMessage.warning('需要先加载好友列表，才能读取表情'); return; }
      stickerVisible.value = true;
    };
    const applyingId = ref('');
    const memberVisible = ref(false), currentGroup = ref(null), memberSelected = ref([]);
    const memberKeyword = ref(''), savingMembers = ref(false), friendsLoading = ref(false);
    const memberFireFilter = ref('');

    const isFireActive = (fire) => {
      if (!fire) return false;
      const n = Number(fire);
      return !isNaN(n) ? n > 0 : true;
    };

    const pickerFriends = computed(() => friendsList.value);
    const filteredPickerFriends = computed(() => {
      let list = friendsList.value;
      const k = (memberKeyword.value || '').trim().toLowerCase();
      if (k) list = list.filter(f => (f.name || '').toLowerCase().includes(k));
      if (memberFireFilter.value === 'has') list = list.filter(f => isFireActive(f.fire));
      else if (memberFireFilter.value === 'none') list = list.filter(f => !isFireActive(f.fire));
      return list;
    });

    const isOtherGroupMember = (name) => {
      const gname = memberMap.value[name];
      if (!gname) return false;
      return !currentGroup.value || gname !== currentGroup.value.name;
    };

    const loadAll = async () => {
      loading.value = true;
      try {
        const res = await API.getGroups();
        if (res.code === 200) {
          groups.value = res.data.groups || [];
          memberMap.value = res.data.member_map || {};
          defaults.time = res.data.default_time || '22:00';
          defaults.message = res.data.default_message || '';
        }
      } catch (e) { ElMessage.error('加载分组失败'); } finally { loading.value = false; }
    };

    const openCreate = () => {
      formMode.value = 'create';
      form.value = { id: '', name: '', time: defaults.time, text: defaults.message, send_type: 'text', sticker_src: '' };
      formVisible.value = true;
    };
    const openEdit = (g) => {
      formMode.value = 'edit';
      form.value = { id: g.id, name: g.name, time: g.time, text: g.text, send_type: g.send_type || 'text', sticker_src: g.sticker_src || '' };
      formVisible.value = true;
    };

    const submitForm = async () => {
      if (!form.value.name.trim()) { ElMessage.warning('请输入分组名称'); return; }
      if (!form.value.time) { ElMessage.warning('请选择发布时间'); return; }
      if (form.value.send_type !== 'text' && !form.value.sticker_src) { ElMessage.warning('请先选择一个表情'); return; }
      saving.value = true;
      try {
        if (formMode.value === 'create') {
          await API.addGroup(form.value.name.trim(), form.value.time, form.value.text || '', form.value.send_type, form.value.sticker_src);
          ElMessage.success('分组已创建');
        } else {
          await API.editGroup(form.value.id, form.value.name.trim(), form.value.time, form.value.text || '', form.value.send_type, form.value.sticker_src);
          ElMessage.success('已保存');
        }
        formVisible.value = false;
        await loadAll();
      } catch (e) { /* 拦截器已提示 */ } finally { saving.value = false; }
    };

    const removeGroup = async (g) => {
      try {
        await ElMessageBox.confirm(`删除分组「${g.name}」？组内好友的定时任务会保留，只解除分组归属。`, '删除分组', { type: 'warning' });
        await API.delGroup(g.id);
        ElMessage.success('已删除');
        await loadAll();
      } catch (e) { /* 取消或已提示 */ }
    };

    const openMembers = async (g) => {
      currentGroup.value = g;
      memberSelected.value = g.members.map(m => m.name);
      memberKeyword.value = '';
      memberFireFilter.value = '';
      memberVisible.value = true;
      if (friendsList.value.length === 0) await loadFriendsForPicker();
    };

    const loadFriendsForPicker = async () => {
      friendsLoading.value = true;
      const tip = ElMessage({ message: '正在加载全部好友，好友较多时需要 10-20 秒…', duration: 0, type: 'info' });
      try {
        const res = await API.getFriendsList();
        if (res.code === 200) {
          const list = res.data.list || {};
          setFriendsList(Object.entries(list).map(([name, [avatar, fire]]) => ({ name, avatar, fire })));
          ElMessage.success(`已加载 ${Object.keys(list).length} 位好友`);
        }
      } catch (e) { ElMessage.error('加载好友失败，请确认浏览器已初始化并登录'); }
      finally { tip.close(); friendsLoading.value = false; }
    };

    const selectAllVisible = () => {
      const names = filteredPickerFriends.value.filter(f => !isOtherGroupMember(f.name)).map(f => f.name);
      memberSelected.value = Array.from(new Set([...memberSelected.value, ...names]));
    };

    const saveMembers = async () => {
      if (!currentGroup.value) return;
      savingMembers.value = true;
      try {
        await API.setGroupMembers(currentGroup.value.id, memberSelected.value);
        ElMessage.success('成员已保存');
        memberVisible.value = false;
        await loadAll();
      } catch (e) { /* 已提示 */ } finally { savingMembers.value = false; }
    };

    const apply = async (g) => {
      applyingId.value = g.id;
      try {
        const res = await API.applyGroup(g.id);
        const d = res.detail || {};
        if (d.fail) {
          const bad = (d.details || []).filter(x => !x.ok).map(x => `${x.name}：${x.msg}`).join('<br>');
          ElMessageBox.alert(bad || '部分好友应用失败', res.data || '应用完成', { type: 'warning', dangerouslyUseHTMLString: true });
        } else {
          ElMessage.success(res.data || '已应用');
        }
        await loadAll();
      } catch (e) { /* 已提示 */ } finally { applyingId.value = ''; }
    };

    onMounted(loadAll);

    return {
      loading, groups, memberMap, defaults, formVisible, formMode, saving, form, applyingId,
      memberVisible, currentGroup, memberSelected, memberKeyword, memberFireFilter, savingMembers, friendsLoading,
      pickerFriends, filteredPickerFriends, isOtherGroupMember,
      stickerVisible, stickerFriend, openGroupSticker,
      loadAll, openCreate, openEdit, submitForm, removeGroup, openMembers,
      loadFriendsForPicker, selectAllVisible, saveMembers, apply,
      ElementPlusIconsVue
    };
  }
};

// ===== Settings Component =====
const Settings = {
  template: `
  <div class="settings-stack">
    <div class="glass-shell anim-fade-up stagger-2">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Lock" /></el-icon></div>后台配置</div>
        <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap">
          <div style="display:flex;align-items:center;padding:8px 16px;background:var(--glass-2);border-radius:var(--r-sm);border:1px solid var(--glass-border)"><span style="color:var(--text-secondary);font-size:13px;margin-right:8px">上次登录IP：</span><span style="color:var(--text-primary);font-size:13px;font-weight:500">{{ lastLoginIP }}</span></div>
          <el-button type="primary" :icon="ElementPlusIconsVue.Lock" @click="passwordDialogVisible = true">修改密码</el-button>
        </div>
      </div>
    </div>
    <div class="glass-shell anim-fade-up stagger-3">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Warning" /></el-icon></div>风险状态</div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px">
          <el-tag :type="riskStatus.paused ? 'danger' : 'success'" effect="dark">{{ riskStatus.paused ? '自动任务已暂停' : '自动任务正常' }}</el-tag>
          <span style="color:var(--text-secondary);font-size:13px">连续失败：{{ riskStatus.consecutive_failures || 0 }} / {{ riskStatus.max_consecutive_failures || 0 }}</span>
          <span style="color:var(--text-secondary);font-size:13px">今日发送：{{ riskStatus.today_send_count || 0 }}</span>
          <span style="color:var(--text-secondary);font-size:13px">最后成功：{{ riskStatus.last_success_at || '暂无' }}</span>
        </div>
        <div v-if="riskStatus.pause_reason" style="color:var(--danger);font-size:13px;line-height:1.7;margin-bottom:12px">{{ riskStatus.pause_reason }}</div>
        <div v-else-if="riskStatus.last_error" style="color:var(--warning);font-size:13px;line-height:1.7;margin-bottom:12px">{{ riskStatus.last_error }}</div>
        <div v-else style="color:var(--text-muted);font-size:13px;line-height:1.7;margin-bottom:12px">系统会自动记录最近发送结果；达到连续失败阈值后，会自动暂停所有定时任务。</div>
        <div style="display:flex;gap:12px;flex-wrap:wrap">
          <el-button :icon="ElementPlusIconsVue.Refresh" @click="fetchRiskStatus">刷新风控状态</el-button>
          <el-button type="warning" :disabled="!riskStatus.paused" @click="handleResumeRisk" :loading="riskResumeLoading">恢复自动任务</el-button>
        </div>
      </div>
    </div>
    <div class="glass-shell anim-fade-up stagger-3">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="MagicStick" /></el-icon></div>防封策略</div>
        <el-form :model="riskConfig" label-width="120px">
          <el-form-item label="随机延迟分钟">
            <el-input-number v-model="riskConfig.task_jitter_minutes" :min="0" :max="60" />
            <span style="color:var(--text-muted);font-size:12px;margin-left:10px">定时任务在触发后随机延迟 0~N 分钟执行</span>
          </el-form-item>
          <el-form-item label="熔断阈值">
            <el-input-number v-model="riskConfig.max_consecutive_failures" :min="1" :max="10" />
            <span style="color:var(--text-muted);font-size:12px;margin-left:10px">连续失败达到阈值后自动暂停任务</span>
          </el-form-item>
          <el-form-item label="去重天数">
            <el-input-number v-model="riskConfig.dedupe_window_days" :min="1" :max="30" />
            <span style="color:var(--text-muted);font-size:12px;margin-left:10px">同一好友在该时间窗内尽量避免重复消息</span>
          </el-form-item>
          <el-form-item label="消息模板池">
            <el-input v-model="riskConfig.message_templates_text" type="textarea" :rows="6" placeholder="一行一个模板，或使用 || 分隔。留空会回退到默认模板。" />
          </el-form-item>
        </el-form>
        <div style="display:flex;gap:12px;flex-wrap:wrap">
          <el-button type="primary" :icon="ElementPlusIconsVue.Check" @click="handleSaveRiskConfig" :loading="riskConfigLoading">保存策略</el-button>
          <span style="color:var(--text-muted);font-size:12px">留空消息任务会优先从模板池中随机选取，并结合历史发送记录做去重</span>
        </div>
      </div>
    </div>
    <div class="glass-shell anim-fade-up stagger-3">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Timer" /></el-icon></div>任务默认值</div>
        <el-form :model="taskDefaults" label-width="120px">
          <el-form-item label="默认发布时间">
            <el-time-picker v-model="taskDefaults.time" format="HH:mm" value-format="HH:mm" placeholder="选择时间" style="width:220px" />
            <span style="color:var(--text-muted);font-size:12px;margin-left:10px">新建任务、新建分组时自动带上</span>
          </el-form-item>
          <el-form-item label="默认消息内容">
            <el-input v-model="taskDefaults.message" type="textarea" :rows="3" placeholder="留空则由下方模板池随机选取" />
          </el-form-item>
        </el-form>
        <div style="display:flex;gap:12px;flex-wrap:wrap">
          <el-button type="primary" :icon="ElementPlusIconsVue.Check" @click="handleSaveDefaults" :loading="defaultsLoading">保存默认值</el-button>
          <span style="color:var(--text-muted);font-size:12px">设置之后，手动建任务和新建分组都不用再重复填写时间和内容</span>
        </div>
      </div>
    </div>
    <div class="glass-shell anim-fade-up stagger-3">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Picture" /></el-icon></div>调试功能</div>
        <div style="display:flex;gap:12px;flex-wrap:wrap">
          <el-button type="primary" :icon="ElementPlusIconsVue.Picture" @click="handleGetScreenshot" :loading="screenshotLoading">浏览器截图</el-button>
          <el-button type="warning" :icon="ElementPlusIconsVue.WarnTriangleFilled" @click="handleForceLogin">强制登录状态</el-button>
        </div>
      </div>
    </div>
    <div class="glass-shell anim-fade-up stagger-4">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Setting" /></el-icon></div>服务端口</div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <div style="display:flex;align-items:center;gap:10px;padding:8px 16px;background:var(--glass-2);border-radius:var(--r-sm);border:1px solid var(--glass-border)">
            <span style="color:var(--text-secondary);font-size:13px">当前端口：</span>
            <span style="color:var(--accent);font-size:15px;font-weight:700">{{ currentPort }}</span>
          </div>
          <el-input v-model="portForm.new_port" placeholder="输入新端口" style="width:140px" @keyup.enter="handleSetPort" />
          <el-button type="primary" :icon="ElementPlusIconsVue.Check" @click="handleSetPort" :loading="portLoading">保存</el-button>
          <span style="color:var(--text-muted);font-size:12px">修改后需重启后端生效</span>
        </div>
      </div>
    </div>
    <div class="glass-shell anim-fade-up stagger-4">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Monitor" /></el-icon></div>浏览器显示</div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <div style="display:flex;align-items:center;gap:10px;padding:8px 16px;background:var(--glass-2);border-radius:var(--r-sm);border:1px solid var(--glass-border)">
            <span style="color:var(--text-secondary);font-size:13px">当前模式：</span>
            <span :style="{ color: browserMode ? 'var(--success)' : 'var(--text-muted)', fontSize: '15px', fontWeight: 700 }">{{ browserMode ? '显示窗口' : '隐藏（无头）' }}</span>
          </div>
          <el-switch v-model="browserMode" :loading="browserModeLoading" @change="handleSetBrowserMode" active-text="显示" inactive-text="隐藏" />
          <span style="color:var(--text-muted);font-size:12px">无头模式易崩溃，建议显示窗口</span>
        </div>
      </div>
    </div>
    <div class="glass-shell anim-fade-up stagger-4">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Monitor" /></el-icon></div>浏览器内核</div>
        <div style="display:flex;flex-direction:column;gap:14px">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <div style="display:flex;align-items:center;gap:10px;padding:8px 16px;background:var(--glass-2);border-radius:var(--r-sm);border:1px solid var(--glass-border)">
              <span style="color:var(--text-secondary);font-size:13px">当前浏览器：</span>
              <span style="color:var(--accent);font-size:15px;font-weight:700">{{ browserLabelMap[browserConfig.browser_name] || browserConfig.browser_name }}</span>
            </div>
            <el-select v-model="browserConfig.browser_name" style="width:180px" :disabled="browserConfigLoading">
              <el-option label="Microsoft Edge" value="edge" />
              <el-option label="Google Chrome" value="chrome" />
              <el-option label="Chromium" value="chromium" />
              <el-option label="Brave" value="brave" />
            </el-select>
            <el-button type="primary" :icon="ElementPlusIconsVue.Check" @click="handleSetBrowserConfig" :loading="browserConfigLoading">保存浏览器</el-button>
          </div>
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <el-input v-model="browserConfig.browser_path" placeholder="可选：自定义浏览器路径，例如 /usr/bin/google-chrome 或 C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" />
            <el-button @click="browserConfig.browser_path = ''" :disabled="browserConfigLoading">清空路径</el-button>
          </div>
          <span style="color:var(--text-muted);font-size:12px">Linux 推荐选择 Chrome 或 Chromium；留空时自动查找系统已安装浏览器，修改后需重启后端生效</span>
        </div>
      </div>
    </div>
    <el-dialog v-model="manualDialogVisible" title="手动登录" width="500px" destroy-on-close append-to-body><el-form :model="manualForm" label-width="120px"><el-form-item label="Base64Cookie"><el-input v-model="manualForm.cookie" type="textarea" :rows="6" placeholder="请输入登录Base64Cookie" /></el-form-item></el-form><template #footer><el-button @click="manualDialogVisible = false">取消</el-button><el-button type="primary" @click="handleManualLogin" :loading="manualLoading">验证登录</el-button></template></el-dialog>
    <el-dialog v-model="cookieDialogVisible" title="获取Cookie" width="400px" destroy-on-close append-to-body><el-form :model="cookieForm" label-width="100px"><el-form-item label="确认密码"><el-input v-model="cookieForm.password" type="password" placeholder="请输入密码确认" @keyup.enter="handleGetCookie" /></el-form-item></el-form><template #footer><el-button @click="cookieDialogVisible = false">取消</el-button><el-button type="primary" @click="handleGetCookie" :loading="cookieLoading">获取Cookie</el-button></template></el-dialog>
    <el-dialog v-model="phoneDialogVisible" title="验证码登录" width="400px" destroy-on-close append-to-body><el-form :model="phoneForm" label-width="80px"><el-form-item label="手机号"><div style="display:flex;gap:8px"><el-input v-model="phoneForm.areacode" placeholder="+86" style="width:70px;flex-shrink:0" @keyup.enter="handleSendCode" /><el-input v-model="phoneForm.phone" placeholder="请输入手机号" style="flex:1" @keyup.enter="handleSendCode" /></div></el-form-item><el-form-item label="验证码"><div style="display:flex;gap:10px"><el-input v-model="phoneForm.code" placeholder="请输入验证码" style="flex:1" @keyup.enter="handlePhoneLogin" /><el-button @click="handleSendCode" :disabled="codeCountdown > 0" :loading="codeLoading">{{ codeCountdown > 0 ? codeCountdown + 's' : '发送验证码' }}</el-button></div></el-form-item></el-form><template #footer><el-button @click="phoneDialogVisible = false">取消</el-button><el-button type="primary" @click="handlePhoneLogin" :loading="phoneLoading">登录</el-button></template></el-dialog>
    <el-dialog v-model="qrDialogVisible" title="抖音扫码登录" width="350px" destroy-on-close append-to-body><div style="display:flex;justify-content:center;align-items:center;min-height:300px"><div v-if="qrcodeUrl" style="text-align:center"><img :src="qrcodeUrl" alt="登录二维码" style="width:250px;height:250px;border:1px solid var(--glass-border);border-radius:var(--r-md);padding:8px;background:var(--bg-surface);box-shadow:var(--shadow-glow)" /><p style="margin-top:15px;color:var(--text-secondary);font-size:14px">请使用抖音App扫码登录</p></div><div v-else-if="loading" style="text-align:center;color:var(--text-muted)"><el-icon class="is-loading" style="font-size:48px;margin-bottom:10px"><component is="Loading" /></el-icon><p>正在加载二维码...</p></div></div><div style="display:flex;justify-content:center;gap:12px;margin-top:20px;padding-top:15px;border-top:1px solid var(--glass-border)"><el-button :icon="ElementPlusIconsVue.Refresh" @click="handleRefreshCode" :loading="refreshLoading" size="small">刷新验证码</el-button><el-button :icon="ElementPlusIconsVue.View" @click="handleCheckLogin" :loading="checkLoading" size="small">获取登录状态</el-button></div></el-dialog>
    <el-dialog v-model="passwordDialogVisible" title="修改密码" width="400px" destroy-on-close append-to-body><el-form :model="passwordForm" label-width="90px"><el-form-item label="原密码"><el-input v-model="passwordForm.old_password" type="password" placeholder="请输入原密码" show-password /></el-form-item><el-form-item label="新密码"><el-input v-model="passwordForm.new_password" type="password" placeholder="请输入新密码" show-password /></el-form-item></el-form><template #footer><el-button @click="passwordDialogVisible = false">取消</el-button><el-button type="primary" @click="handleChangePassword" :loading="passwordLoading">确认修改</el-button></template></el-dialog>
    <el-dialog v-model="screenshotPreviewVisible" title="浏览器截图" width="600px" destroy-on-close append-to-body><img :src="screenshotUrl" alt="浏览器截图" style="max-width:100%;max-height:70vh;display:block;margin:0 auto" /></el-dialog>
    <el-dialog v-model="browserLoginDialogVisible" title="浏览器登录" width="460px" destroy-on-close append-to-body>
      <el-form :model="browserLoginForm" label-width="90px">
        <el-form-item label="账号备注"><el-input v-model="browserLoginForm.name" placeholder="给账号起个名字，如：主号 / 小号A" maxlength="40" :disabled="browserLoginForm.mode === 'current'" /></el-form-item>
        <el-form-item label="登录方式">
          <el-radio-group v-model="browserLoginForm.mode">
            <el-radio label="new">添加新账号</el-radio>
            <el-radio label="current">重新登录当前账号</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <div style="color:var(--text-muted);font-size:12px;line-height:1.7;background:var(--glass-2);border:1px solid var(--glass-border);border-radius:var(--r-sm);padding:10px 12px">
        点击开始后将弹出浏览器窗口并打开抖音，在窗口里完成登录（扫码 / 验证码 / 密码均可）。登录成功后系统会自动抓取 Cookie 保存到该账号，无需手动复制粘贴。
      </div>
      <template #footer><el-button @click="browserLoginDialogVisible = false">取消</el-button><el-button type="primary" @click="startBrowserLogin" :loading="browserLoginLoading">开始登录</el-button></template>
    </el-dialog>
    <el-dialog v-model="loginWaitVisible" title="等待登录" width="420px" :close-on-click-modal="false" :close-on-press-escape="false" :show-close="false" append-to-body>
      <div style="text-align:center;padding:10px 0">
        <el-icon class="is-loading" style="font-size:42px;color:var(--accent);margin-bottom:12px"><component is="Loading" /></el-icon>
        <p style="font-size:15px;font-weight:600;margin-bottom:8px">已弹出浏览器窗口</p>
        <p style="color:var(--text-secondary);font-size:13px;line-height:1.8">请在浏览器窗口中完成抖音登录<br>登录成功后系统会自动抓取 Cookie，无需手动操作</p>
        <p style="color:var(--text-muted);font-size:12px;margin-top:10px">已等待 {{ loginWaitSeconds }} 秒（10 分钟未登录将自动取消）</p>
      </div>
      <template #footer><el-button @click="cancelBrowserLogin" :loading="cancelLoginLoading">取消登录</el-button></template>
    </el-dialog>
  </div>`,
  setup() {
    const loginLoading = ref(false), refreshLoading = ref(false), checkLoading = ref(false), refreshStatusLoading = ref(false);
    const qrDialogVisible = ref(false), qrcodeUrl = ref(''), loading = ref(false);
    const manualDialogVisible = ref(false), manualLoading = ref(false), manualForm = ref({ cookie: '' });
    const username = ref(localStorage.getItem('douyin_username') || ''), usernameLoaded = ref(localStorage.getItem('douyin_username_loaded') === '1');
    const passwordDialogVisible = ref(false), passwordLoading = ref(false), passwordForm = ref({ old_password: '', new_password: '' });
    const lastLoginIP = ref(localStorage.getItem('douyin_last_login_ip') || '加载中...'), settingsLoaded = ref(localStorage.getItem('douyin_settings_loaded') === '1');
    const cookieDialogVisible = ref(false), cookieLoading = ref(false), cookieForm = ref({ password: '' });
    const screenshotLoading = ref(false), screenshotUrl = ref(''), screenshotPreviewVisible = ref(false);
    const phoneDialogVisible = ref(false), phoneLoading = ref(false), codeLoading = ref(false), codeCountdown = ref(0);
    const phoneForm = ref({ areacode: '+86', phone: '', code: '' });
    const currentPort = ref(8080), portForm = ref({ new_port: '' }), portLoading = ref(false);
    const browserMode = ref(true), browserModeLoading = ref(false);
    const browserConfigLoading = ref(false);
    const browserConfig = reactive({ browser_name: 'edge', browser_path: '' });
    const riskConfigLoading = ref(false), riskResumeLoading = ref(false);
    const riskConfig = reactive({ task_jitter_minutes: 8, max_consecutive_failures: 3, dedupe_window_days: 7, message_templates_text: '' });
    const taskDefaults = reactive({ time: '22:00', message: '' });
    const defaultsLoading = ref(false);
    const fetchDefaults = async () => {
      try {
        const res = await API.getDefaults();
        if (res.code === 200) {
          taskDefaults.time = res.data.time || '22:00';
          taskDefaults.message = res.data.message || '';
        }
      } catch (e) { /* 忽略 */ }
    };
    const handleSaveDefaults = async () => {
      defaultsLoading.value = true;
      try {
        await API.setDefaults(taskDefaults.time, taskDefaults.message);
        ElMessage.success('默认值已保存');
      } catch (e) { /* 拦截器已提示 */ } finally { defaultsLoading.value = false; }
    };
    const riskStatus = reactive({ paused: false, pause_reason: '', consecutive_failures: 0, max_consecutive_failures: 0, today_send_count: 0, last_error: '', last_success_at: '' });
    const browserLabelMap = {
      edge: 'Microsoft Edge',
      chrome: 'Google Chrome',
      chromium: 'Chromium',
      brave: 'Brave'
    };

    // ===== 多账号（cookie 管理器）=====
    const accountsLoading = ref(false), switchingId = ref('');
    const loadAccounts = async () => { accountsLoading.value = true; try { await fetchAccounts(); } finally { accountsLoading.value = false; } };
    const handleSwitchAccount = async (row) => {
      if (switchingId.value) return;
      switchingId.value = row.id;
      try { await switchDouyinAccount(row.id); } finally { switchingId.value = ''; }
    };
    const openRenameAccount = async (row) => {
      try {
        const { value } = await ElMessageBox.prompt('请输入新的备注名', '重命名账号', { inputValue: row.name, inputPlaceholder: '账号备注名', confirmButtonText: '保存', cancelButtonText: '取消', inputValidator: (v) => (v && v.trim()) ? true : '备注名不能为空' });
        const res = await API.renameAccount(row.id, value.trim());
        if (res.code === 200) { ElMessage.success('已重命名'); await loadAccounts(); } else ElMessage.error(res.data || '重命名失败');
      } catch (e) { if (e !== 'cancel') ElMessage.error('重命名失败'); }
    };
    const handleRemoveAccount = async (row) => {
      try {
        await ElMessageBox.confirm(`确定删除账号「${row.name}」？该账号保存的 Cookie、定时任务、分组、好友缓存会被一并删除，浏览器登录目录也会清除。`, '删除账号', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' });
        const res = await API.removeAccount(row.id);
        if (res.code === 200) { ElMessage.success('账号已删除'); await loadAccounts(); } else ElMessage.error(res.data || '删除失败');
      } catch (e) { if (e !== 'cancel') ElMessage.error('删除失败'); }
    };
    const browserLoginDialogVisible = ref(false), browserLoginLoading = ref(false);
    const browserLoginForm = ref({ name: '', mode: 'new' });
    const loginWaitVisible = ref(false), loginWaitSeconds = ref(0), cancelLoginLoading = ref(false);
    let loginWaitTimer = null, loginWaitSecTimer = null;
    const openBrowserLogin = (row) => {
      if (row) {
        // 重新登录某个已有账号（行内按钮）
        browserLoginForm.value = { name: row.name, mode: 'current' };
      } else {
        browserLoginForm.value = { name: '', mode: 'new' };
      }
      browserLoginDialogVisible.value = true;
    };
    const handleBrowserLogin = () => openBrowserLogin(null);
    const startBrowserLogin = async () => {
      if (browserLoginLoading.value) return;
      const mode = browserLoginForm.value.mode;
      const name = browserLoginForm.value.name.trim();
      if (mode === 'new' && !name) { ElMessage.warning('请填写账号备注名'); return; }
      browserLoginLoading.value = true;
      try {
        const res = await API.browserLogin(mode === 'new' ? { name } : {});
        if (res.code === 200) {
          browserLoginDialogVisible.value = false;
          if (typeof res.data === 'string' && res.data.includes('已是登录状态')) {
            ElMessage.success(res.data);
            await loadAccounts();
            setTimeout(() => window.location.reload(), 600);
            return;
          }
          ElMessage.success(res.data || '已弹出浏览器，请完成登录');
          loginWaitVisible.value = true; loginWaitSeconds.value = 0;
          startLoginWaitPoll();
        } else ElMessage.error(res.data || '启动登录失败');
      } catch (e) { ElMessage.error('启动登录失败，请确保后端服务正常'); }
      finally { browserLoginLoading.value = false; }
    };
    const stopLoginWaitPoll = () => {
      if (loginWaitTimer) { clearInterval(loginWaitTimer); loginWaitTimer = null; }
      if (loginWaitSecTimer) { clearInterval(loginWaitSecTimer); loginWaitSecTimer = null; }
    };
    const startLoginWaitPoll = () => {
      stopLoginWaitPoll();
      loginWaitSecTimer = setInterval(() => { loginWaitSeconds.value += 2; }, 2000);
      loginWaitTimer = setInterval(async () => {
        try {
          const res = await API.accountLoginState();
          const d = res.data || {};
          if (d.status === 'success') {
            stopLoginWaitPoll();
            loginWaitVisible.value = false;
            ElMessage.success('登录成功，Cookie 已保存' + (d.nickname ? '：' + d.nickname : ''));
            await loadAccounts();
            setTimeout(() => window.location.reload(), 800);
          } else if (d.status === 'expired') {
            stopLoginWaitPoll();
            loginWaitVisible.value = false;
            ElMessage.warning(d.message || '登录等待已超时，请重新点击登录');
            await loadAccounts();
          }
        } catch (e) { /* 轮询失败不打断，继续等待 */ }
      }, 2000);
    };
    const cancelBrowserLogin = async () => {
      cancelLoginLoading.value = true;
      try { await API.cancelLogin(); } catch (e) { /* 拦截器已提示 */ }
      finally { cancelLoginLoading.value = false; }
      stopLoginWaitPoll();
      loginWaitVisible.value = false;
      ElMessage.info('已取消登录');
      await loadAccounts();
    };

    const fetchLastLoginIP = async () => { try { const res = await API.getLastLoginIP(); if (res.code == 200) { lastLoginIP.value = res.data || '无'; localStorage.setItem('douyin_last_login_ip', lastLoginIP.value); } } catch (e) { lastLoginIP.value = '获取失败'; } };
    const fetchUsername = async () => { try { const res = await API.getUsername(); if (res.code == 200 && res.data) { const nick = res.data.nickname || ''; const av = res.data.avatar || ''; username.value = nick; usernameLoaded.value = true; setDouyinUser(nick, av); localStorage.setItem('douyin_username_loaded', '1'); } } catch (e) {} };
    let _lastLoginCheckSetting = 0;
    const checkLoginStatus = async (force = false) => { const now = Date.now(); if (!force && now - _lastLoginCheckSetting < 30000) return; _lastLoginCheckSetting = now; try { const res = await API.getLoginStatus(); loginStatus.value = res.data === 'Yes'; setLoginStatus(loginStatus.value); if (loginStatus.value && !usernameLoaded.value) await fetchUsername(); } catch (e) { loginStatus.value = false; setLoginStatus(false); } };
    const handleRefreshStatus = async () => { refreshStatusLoading.value = true; try { usernameLoaded.value = false; setDouyinUser('', ''); localStorage.removeItem('douyin_username_loaded'); await checkLoginStatus(true); await fetchLastLoginIP(); ElMessage.success(loginStatus.value ? '已登录' : '未登录'); } finally { refreshStatusLoading.value = false; } };
    const fetchFriendsList = async () => { try { const res = await API.getFriendsList(); if (res.code === 200) { const list = res.data.list || {}; setFriendsList(Object.entries(list).map(([name, [avatar, fire]]) => ({ name, avatar, fire }))); } } catch (e) {} };
    const handleCheckLogin = async () => { checkLoading.value = true; try { const res = await API.pnglogin(); loginStatus.value = res.code == 200; setLoginStatus(loginStatus.value); if (loginStatus.value) { stopQrPoll(); ElMessage.success('登录成功'); qrDialogVisible.value = false; username.value = ''; usernameLoaded.value = false; setDouyinUser('', ''); localStorage.removeItem('douyin_username_loaded'); await fetchUsername(); await fetchFriendsList(); } else ElMessage.warning('未登录，请继续扫码'); } catch (e) { ElMessage.error('扫码登录失败，请重试'); } finally { checkLoading.value = false; } };
    const handleRefreshCode = async () => { refreshLoading.value = true; try { await API.initBrowser(); const res = await API.getLoginPng(); if (res.data) { qrcodeUrl.value = res.data; qrDialogVisible.value = true; ElMessage.success('刷新成功'); startQrPoll(); } else ElMessage.error('获取二维码失败'); } catch (e) { ElMessage.error('刷新失败，请确保浏览器已初始化'); } finally { refreshLoading.value = false; } };
    const handleManualLogin = async () => { if (!manualForm.value.cookie.trim()) { ElMessage.warning('请输入Base64Cookie'); return; } manualLoading.value = true; try { const res = await API.login(manualForm.value.cookie); if (res.data === 'ok') { ElMessage.success('登录成功'); loginStatus.value = true; setLoginStatus(true); manualDialogVisible.value = false; username.value = ''; usernameLoaded.value = false; setDouyinUser('', ''); localStorage.removeItem('douyin_username_loaded'); await fetchUsername(); await fetchFriendsList(); } else ElMessage.error('登录失败，Cookie无效'); } catch (e) { ElMessage.error('登录失败，请检查Cookie'); } finally { manualLoading.value = false; } };
    const handleChangePassword = async () => { if (!passwordForm.value.old_password) { ElMessage.warning('请输入原密码'); return; } if (!passwordForm.value.new_password) { ElMessage.warning('请输入新密码'); return; } if (passwordForm.value.old_password === passwordForm.value.new_password) { ElMessage.warning('新密码不能与原密码相同'); return; } passwordLoading.value = true; try { const res = await API.changePassword(passwordForm.value.old_password, passwordForm.value.new_password); if (res.code == 200) { ElMessage.success('密码修改成功'); passwordDialogVisible.value = false; passwordForm.value.old_password = ''; passwordForm.value.new_password = ''; } else ElMessage.error(res.data || '修改失败'); } catch (e) { ElMessage.error('修改失败'); } finally { passwordLoading.value = false; } };
    const handleGetCookie = async () => { if (!cookieForm.value.password) { ElMessage.warning('请输入密码'); return; } cookieLoading.value = true; try { const res = await API.getCooker(cookieForm.value.password); if (res.code == 200) { const ta = document.createElement('textarea'); ta.value = res.data.cooke; ta.style.position = 'fixed'; ta.style.left = '-9999px'; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); ElMessage.success('Cookie已复制到剪贴板'); cookieDialogVisible.value = false; cookieForm.value.password = ''; } catch { ElMessage.error('复制失败'); } finally { document.body.removeChild(ta); } } else ElMessage.error(res.data || '获取失败，密码错误'); } catch (e) { ElMessage.error('获取失败'); } finally { cookieLoading.value = false; } };
    const handleDieLogin = async () => { try { await API.dieLogin(); setLoginStatus(false); setDouyinUser('', ''); localStorage.removeItem('douyin_token'); ElMessage.success('已强制退出登录'); } catch (e) { ElMessage.error('强制退出失败'); } };
    const handleSendCode = async () => { if (!phoneForm.value.phone) { ElMessage.warning('请输入手机号'); return; } codeLoading.value = true; try { const res = await API.sendVerifyCode(phoneForm.value.areacode, phoneForm.value.phone); if (res.code == 200) { ElMessage.success('验证码发送成功'); codeCountdown.value = 60; const timer = setInterval(() => { codeCountdown.value--; if (codeCountdown.value <= 0) clearInterval(timer); }, 1000); } else ElMessage.error(res.data || '验证码发送失败'); } catch (e) { ElMessage.error('验证码发送失败，请确保浏览器已初始化'); } finally { codeLoading.value = false; } };
    const handlePhoneLogin = async () => { if (!phoneForm.value.phone) { ElMessage.warning('请输入手机号'); return; } if (!phoneForm.value.code) { ElMessage.warning('请输入验证码'); return; } phoneLoading.value = true; try { const res = await API.submitVerifyCode(phoneForm.value.code); if (res.code == 200) { ElMessage.success('登录成功'); phoneDialogVisible.value = false; setLoginStatus(true); username.value = ''; usernameLoaded.value = false; setDouyinUser('', ''); localStorage.removeItem('douyin_username_loaded'); await fetchUsername(); } else ElMessage.error(res.data || '登录失败'); } catch (e) { ElMessage.error('登录失败，请重试'); } finally { phoneLoading.value = false; } };
    const handleGetScreenshot = async () => { screenshotLoading.value = true; screenshotUrl.value = ''; try { const res = await API.getScrlk(); if (res.code == 200) { screenshotUrl.value = 'data:image/png;base64,' + res.data; screenshotPreviewVisible.value = true; } else ElMessage.error(res.data || '获取截图失败'); } catch (e) { ElMessage.error('获取截图失败，请确保已登录'); } finally { screenshotLoading.value = false; } };
    const handleForceLogin = async () => {
      try {
        const { value } = await ElMessageBox.prompt('请输入密码以确认使用调试功能', '安全确认', { confirmButtonText: '确认', cancelButtonText: '取消', inputType: 'password', inputPlaceholder: '请输入密码' });
        if (!value) { ElMessage.warning('请输入密码'); return; }
        const res = await API.forceLogin(value);
        if (res.code == 200) ElMessage.success(res.data || '强制登录状态成功'); else ElMessage.error(res.data || '强制登录状态失败');
      } catch (e) { if (e !== 'cancel') ElMessage.error('强制登录状态失败'); }
    };
    const fetchPort = async () => { try { const res = await API.getPort(); if (res.code == 200) currentPort.value = res.data; } catch (e) {} };
    const handleSetPort = async () => { const p = parseInt(portForm.value.new_port); if (!p) { ElMessage.warning('请输入端口号'); return; } if (p < 1 || p > 65535) { ElMessage.warning('端口范围 1-65535'); return; } portLoading.value = true; try { const res = await API.setPort(p); if (res.code == 200) { ElMessage.success(res.data || '保存成功，重启后端后生效'); currentPort.value = p; portForm.value.new_port = ''; } else ElMessage.error(res.data || '保存失败'); } catch (e) { ElMessage.error('保存失败'); } finally { portLoading.value = false; } };
    const fetchBrowserMode = async () => { try { const res = await API.getBrowserMode(); if (res.code == 200) browserMode.value = res.data; } catch (e) {} };
    const handleSetBrowserMode = async () => { browserModeLoading.value = true; try { const res = await API.setBrowserMode(browserMode.value); if (res.code == 200) ElMessage.success(res.data || '保存成功，重启后端后生效'); else { ElMessage.error(res.data || '保存失败'); browserMode.value = !browserMode.value; } } catch (e) { ElMessage.error('保存失败'); browserMode.value = !browserMode.value; } finally { browserModeLoading.value = false; } };
    const fetchBrowserConfig = async () => { try { const res = await API.getBrowserConfig(); if (res.code == 200 && res.data) { browserConfig.browser_name = res.data.browser_name || 'edge'; browserConfig.browser_path = res.data.browser_path || ''; } } catch (e) {} };
    const fetchRiskConfig = async () => {
      try {
        const res = await API.getRiskConfig();
        if (res.code == 200 && res.data) {
          riskConfig.task_jitter_minutes = res.data.task_jitter_minutes ?? 8;
          riskConfig.max_consecutive_failures = res.data.max_consecutive_failures ?? 3;
          riskConfig.dedupe_window_days = res.data.dedupe_window_days ?? 7;
          riskConfig.message_templates_text = (res.data.message_templates || []).join('\n');
        }
      } catch (e) {}
    };
    const fetchRiskStatus = async () => { try { const res = await API.getRiskStatus(); if (res.code == 200 && res.data) Object.assign(riskStatus, res.data); } catch (e) {} };
    const handleSetBrowserConfig = async () => {
      browserConfigLoading.value = true;
      try {
        const res = await API.setBrowserConfig(browserConfig.browser_name, browserConfig.browser_path.trim());
        if (res.code == 200) ElMessage.success(res.data || '保存成功，重启后端后生效');
        else ElMessage.error(res.data || '保存失败');
      } catch (e) {
        ElMessage.error('保存失败');
      } finally {
        browserConfigLoading.value = false;
      }
    };
    const handleSaveRiskConfig = async () => {
      riskConfigLoading.value = true;
      try {
        const payload = {
          task_jitter_minutes: riskConfig.task_jitter_minutes,
          max_consecutive_failures: riskConfig.max_consecutive_failures,
          dedupe_window_days: riskConfig.dedupe_window_days,
          message_templates: riskConfig.message_templates_text
        };
        const res = await API.setRiskConfig(payload);
        if (res.code == 200) { ElMessage.success(res.data || '保存成功'); await fetchRiskStatus(); }
        else ElMessage.error(res.data || '保存失败');
      } catch (e) {
        ElMessage.error('保存失败');
      } finally {
        riskConfigLoading.value = false;
      }
    };
    const handleResumeRisk = async () => {
      riskResumeLoading.value = true;
      try {
        const res = await API.resumeRisk();
        if (res.code == 200) { ElMessage.success(res.data || '已恢复'); await fetchRiskStatus(); }
        else ElMessage.error(res.data || '恢复失败');
      } catch (e) {
        ElMessage.error('恢复失败');
      } finally {
        riskResumeLoading.value = false;
      }
    };
    let qrPollTimer = null;
    const stopQrPoll = () => { if (qrPollTimer) { clearInterval(qrPollTimer); qrPollTimer = null; } };
    const startQrPoll = () => {
      stopQrPoll();
      qrPollTimer = setInterval(async () => {
        try {
          const res = await API.pnglogin();
          if (res.code == 200) {
            loginStatus.value = true; setLoginStatus(true);
            qrDialogVisible.value = false; stopQrPoll();
            ElMessage.success('扫码登录成功');
            username.value = ''; usernameLoaded.value = false; setDouyinUser('', '');
            localStorage.removeItem('douyin_username_loaded');
            await fetchUsername(); await fetchFriendsList();
          }
        } catch (e) {}
      }, 3000);
    };
    watch(qrDialogVisible, (v) => { if (!v) stopQrPoll(); });
    const handleLogin = async () => { loginLoading.value = true; loading.value = true; qrcodeUrl.value = ''; qrDialogVisible.value = true; try { await API.initBrowser(); const res = await API.getLoginPng(); if (res.data) { qrcodeUrl.value = res.data; ElMessage.success('请使用抖音App扫码登录'); startQrPoll(); } else { ElMessage.error('获取二维码失败'); qrDialogVisible.value = false; } } catch (e) { ElMessage.error('登录初始化失败，请确保浏览器已启动'); qrDialogVisible.value = false; } finally { loginLoading.value = false; loading.value = false; } };

    onMounted(async () => { if (!settingsLoaded.value) { await checkLoginStatus(); await fetchLastLoginIP(); localStorage.setItem('douyin_settings_loaded', '1'); } await loadAccounts(); await fetchPort(); await fetchBrowserMode(); await fetchBrowserConfig(); await fetchRiskConfig(); await fetchRiskStatus(); await fetchDefaults(); });
    onUnmounted(() => { stopQrPoll(); stopLoginWaitPoll(); });
    onActivated(async () => { lastLoginIP.value = localStorage.getItem('douyin_last_login_ip') || '加载中...'; await fetchRiskStatus(); await loadAccounts(); });

    return { loginLoading, refreshLoading, checkLoading, refreshStatusLoading, qrDialogVisible, qrcodeUrl, loading, manualDialogVisible, manualLoading, manualForm, username, passwordDialogVisible, passwordLoading, passwordForm, lastLoginIP, cookieDialogVisible, cookieLoading, cookieForm, screenshotLoading, screenshotUrl, screenshotPreviewVisible, phoneDialogVisible, phoneLoading, codeLoading, codeCountdown, phoneForm, currentPort, portForm, portLoading, browserMode, browserModeLoading, browserConfigLoading, browserConfig, riskConfigLoading, riskResumeLoading, riskConfig, taskDefaults, defaultsLoading, handleSaveDefaults, riskStatus, browserLabelMap, fetchRiskStatus, handleSaveRiskConfig, handleResumeRisk, handleSetBrowserMode, handleSetBrowserConfig, handleLogin, handleRefreshStatus, handleCheckLogin, handleRefreshCode, handleManualLogin, handleChangePassword, handleGetCookie, handleDieLogin, handleSendCode, handlePhoneLogin, handleGetScreenshot, handleForceLogin, handleSetPort, loginStatus, ElementPlusIconsVue,
      // 多账号
      accounts, accountsLoading, switchingId, loadAccounts, handleSwitchAccount, openRenameAccount, handleRemoveAccount,
      browserLoginDialogVisible, browserLoginLoading, browserLoginForm, openBrowserLogin, handleBrowserLogin, startBrowserLogin,
      loginWaitVisible, loginWaitSeconds, cancelLoginLoading, cancelBrowserLogin };
  }
};

// ===== Spark Calendar (火花日历) =====
const SparkCalendar = {
  name: 'SparkCalendar',
  template: `
  <div>
    <div class="glass-shell anim-fade-up">
      <div class="glass-core">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:16px">
          <div class="panel-title" style="margin-bottom:0"><div class="panel-title-icon"><el-icon><component is="Calendar" /></el-icon></div>火花日历</div>
          <div style="display:flex;align-items:center;gap:8px">
            <el-button :icon="ElementPlusIconsVue.ArrowLeft" circle size="small" @click="shiftMonth(-1)" />
            <el-select v-model="month" style="width:130px" @change="load">
              <el-option v-for="m in months" :key="m" :label="m" :value="m" />
            </el-select>
            <el-button :icon="ElementPlusIconsVue.ArrowRight" circle size="small" :disabled="isCurrentMonth" @click="shiftMonth(1)" />
            <el-button :icon="ElementPlusIconsVue.Refresh" size="small" @click="load">刷新</el-button>
          </div>
        </div>
        <div class="stats-row" style="margin-bottom:16px">
          <div class="stat-tile">
            <div class="stat-tile-icon mint"><el-icon><component is="CircleCheck" /></el-icon></div>
            <div class="stat-tile-label">本月打卡成功</div>
            <div class="stat-tile-value">{{ monthStats.ok }} <span style="font-size:14px;color:var(--text-muted)">次</span></div>
          </div>
          <div class="stat-tile">
            <div class="stat-tile-icon purple"><el-icon><component is="Calendar" /></el-icon></div>
            <div class="stat-tile-label">有记录天数</div>
            <div class="stat-tile-value">{{ monthStats.days }} <span style="font-size:14px;color:var(--text-muted)">天</span></div>
          </div>
          <div class="stat-tile">
            <div class="stat-tile-icon accent"><el-icon><component is="User" /></el-icon></div>
            <div class="stat-tile-label">覆盖好友</div>
            <div class="stat-tile-value">{{ monthStats.friends }} <span style="font-size:14px;color:var(--text-muted)">人</span></div>
          </div>
          <div class="stat-tile">
            <div class="stat-tile-icon cyan"><el-icon><component is="Warning" /></el-icon></div>
            <div class="stat-tile-label">失败次数</div>
            <div class="stat-tile-value" :style="{ color: monthStats.fail ? 'var(--danger)' : '' }">{{ monthStats.fail }} <span style="font-size:14px;color:var(--text-muted)">次</span></div>
          </div>
        </div>

        <div class="spark-calendar">
          <div class="spark-cal-head">
            <div v-for="w in weekdays" :key="w" class="spark-cal-week">{{ w }}</div>
          </div>
          <div class="spark-cal-grid">
            <div v-for="cell in cells" :key="cell.date" class="spark-cal-cell"
                 :class="{ outside: !cell.inMonth, today: cell.date === today, selected: cell.date === selectedDate, clickable: !!cell.data }"
                 @click="cell.data && (selectedDate = cell.date)">
              <div class="spark-cal-day">{{ cell.day }}</div>
              <div v-if="cell.data" class="spark-cal-marks">
                <span class="spark-cal-dot ok" v-if="cell.data.ok"></span>
                <span class="spark-cal-dot fail" v-if="cell.data.fail"></span>
                <span class="spark-cal-count">{{ cell.data.total }} 次</span>
              </div>
            </div>
          </div>
        </div>
        <div class="spark-cal-legend">
          <span><i class="spark-cal-dot ok"></i>成功</span>
          <span><i class="spark-cal-dot fail"></i>失败</span>
          <span style="color:var(--text-muted)">点击有记录的日子查看明细；记录从升级后开始累积。</span>
        </div>
      </div>
    </div>

    <div class="glass-shell anim-fade-up" style="margin-top:20px" v-if="selectedDate">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="List" /></el-icon></div>{{ selectedDate }} 打卡明细（{{ selectedItems.length }} 条）</div>
        <el-empty v-if="selectedItems.length === 0" description="这一天没有记录" :image-size="70" />
        <el-table v-else :data="selectedItems" stripe style="width:100%">
          <el-table-column prop="time" label="时间" width="100" />
          <el-table-column prop="name" label="好友" min-width="130" />
          <el-table-column label="结果" width="90">
            <template #default="{ row }">
              <el-tag :type="row.ok ? 'success' : 'danger'" size="small" effect="plain">{{ row.ok ? '成功' : '失败' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="detail" label="内容 / 失败原因" min-width="240" show-overflow-tooltip />
          <el-table-column prop="source" label="来源" width="100">
            <template #default="{ row }">
              <span style="color:var(--text-muted);font-size:12px">{{ sourceLabel(row.source) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>

    <div class="glass-shell anim-fade-up" style="margin-top:20px">
      <div class="glass-core">
        <div class="panel-title"><div class="panel-title-icon"><el-icon><component is="Clock" /></el-icon></div>今日计划（{{ planned.length }} 个任务）</div>
        <el-empty v-if="planned.length === 0" description="还没有定时任务" :image-size="70" />
        <div v-else style="display:flex;flex-wrap:wrap;gap:8px">
          <el-tag v-for="p in planned" :key="p.time + p.name" type="info" effect="plain">
            {{ p.time }} · {{ p.name }}
          </el-tag>
        </div>
      </div>
    </div>
  </div>`,
  setup() {
    const now = new Date();
    const today = now.toISOString ? new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10) : '';
    const month = ref(today.slice(0, 7));
    const months = ref([month.value]);
    const days = ref({});
    const planned = ref([]);
    const selectedDate = ref(today);
    const loading = ref(false);
    const weekdays = ['一', '二', '三', '四', '五', '六', '日'];

    const fmtMonth = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;

    const load = async () => {
      loading.value = true;
      try {
        const res = await API.getCalendar(month.value);
        if (res.code === 200 && res.data) {
          days.value = res.data.days || {};
          planned.value = res.data.planned || [];
          const list = res.data.months || [];
          if (!list.includes(month.value)) list.push(month.value);
          months.value = list;
          if (res.data.today) selectedDate.value = selectedDate.value || res.data.today;
        }
      } catch (e) {} finally { loading.value = false; }
    };

    const shiftMonth = (delta) => {
      const [y, m] = month.value.split('-').map(Number);
      const d = new Date(y, m - 1 + delta, 1);
      const next = fmtMonth(d);
      if (next > today.slice(0, 7)) return;
      month.value = next;
      selectedDate.value = '';
      load();
    };

    const isCurrentMonth = computed(() => month.value >= today.slice(0, 7));

    const cells = computed(() => {
      const [y, m] = month.value.split('-').map(Number);
      const first = new Date(y, m - 1, 1);
      const offset = (first.getDay() + 6) % 7; // 周一开头
      const total = new Date(y, m, 0).getDate();
      const out = [];
      for (let i = 0; i < offset; i++) out.push({ date: '', day: '', inMonth: false, data: null });
      for (let d = 1; d <= total; d++) {
        const date = `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
        out.push({ date, day: d, inMonth: true, data: days.value[date] || null });
      }
      while (out.length % 7) out.push({ date: '', day: '', inMonth: false, data: null });
      return out;
    });

    const selectedItems = computed(() => (days.value[selectedDate.value]?.items || []).slice().reverse());

    const monthStats = computed(() => {
      let ok = 0, fail = 0, daysCount = 0;
      const friends = new Set();
      for (const d of Object.values(days.value)) {
        ok += d.ok || 0; fail += d.fail || 0;
        if (d.total) daysCount++;
        for (const it of (d.items || [])) if (it.ok && it.name) friends.add(it.name);
      }
      return { ok, fail, days: daysCount, friends: friends.size };
    });

    const sourceLabel = (s) => ({ scheduled: '定时', manual: '手动', sticker: '表情' }[s] || s || '-');

    onMounted(load);
    onActivated(load);
    return { month, months, days, planned, today, selectedDate, selectedItems, monthStats, cells, weekdays, isCurrentMonth, loading, shiftMonth, load, sourceLabel, ElementPlusIconsVue };
  }
};

// ===== Router =====
const routes = [
  { path: '/login', name: 'Login', component: Login, meta: { requiresAuth: false } },
  { path: '/', component: Layout, meta: { requiresAuth: true }, redirect: '/home', children: [
    { path: 'home', name: 'Home', component: Home },
    { path: 'friends', name: 'Friends', component: Friends },
    { path: 'groups', name: 'Groups', component: Groups },
    { path: 'chat', name: 'Chat', component: Chat },
    { path: 'tasks', name: 'Tasks', component: Tasks },
    { path: 'calendar', name: 'SparkCalendar', component: SparkCalendar },
    { path: 'settings', name: 'Settings', component: Settings }
  ]}
];
const router = createRouter({ history: createWebHistory((window.__SPARK_BASE__ || '') + '/'), routes });

// ===== Boot =====
window.addEventListener('error', (e) => { console.error('Global error:', e.message, e.filename, e.lineno); });
window.addEventListener('unhandledrejection', (e) => { console.error('Unhandled rejection:', e.reason); });

// ===== 通用表情选择弹窗（任务 / 分组 / 批量创建共用） =====
const StickerPickerDialog = {
  name: 'StickerPickerDialog',
  props: {
    modelValue: { type: Boolean, default: false },
    friend: { type: String, default: '' }
  },
  emits: ['update:modelValue', 'pick'],
  template: `
  <el-dialog :model-value="modelValue" @update:model-value="v => $emit('update:modelValue', v)"
             title="选择要发送的表情" width="580px" append-to-body>
    <div v-if="loading" style="text-align:center;padding:30px;color:var(--text-muted)">正在读取表情列表…（首次约 10 秒）</div>
    <template v-else>
      <div class="sticker-tabs">
        <div v-for="(c, i) in cats" :key="i" class="sticker-tab" :class="{active: tab === i}" @click="tab = i">
          <span v-if="c.icon_html" class="sticker-tab-icon" v-html="c.icon_html"></span>
          <span class="sticker-tab-label">{{ c.label }}</span>
        </div>
      </div>
      <div style="max-height:320px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(58px,1fr));gap:8px">
        <div v-for="(src, j) in (cats[tab]?.stickers || [])" :key="j"
             style="cursor:pointer;padding:4px;border:1px solid var(--glass-border-soft);border-radius:8px;display:flex;align-items:center;justify-content:center"
             @click="choose(src)">
          <img :src="src" style="width:48px;height:48px;object-fit:contain" loading="lazy" />
        </div>
      </div>
    </template>
  </el-dialog>`,
  setup(props, { emit }) {
    const loading = ref(false), cats = ref([]), tab = ref(0), loadedFor = ref('');
    const load = async () => {
      if (!props.friend) { ElMessage.warning('请先选择好友，再挑表情'); emit('update:modelValue', false); return; }
      if (cats.value.length && loadedFor.value === props.friend) return;
      loading.value = true;
      try {
        const res = await API.getStickerList(props.friend);
        if (res.code === 200 && Array.isArray(res.data) && res.data.length) {
          cats.value = res.data;
          tab.value = 0;
          loadedFor.value = props.friend;
        } else {
          ElMessage.warning(res.data || '没获取到表情');
          emit('update:modelValue', false);
        }
      } catch (e) {
        ElMessage.error('读取表情失败');
        emit('update:modelValue', false);
      } finally { loading.value = false; }
    };
    Vue.watch(() => props.modelValue, (v) => { if (v) load(); });
    const choose = (src) => { emit('pick', src); emit('update:modelValue', false); };
    return { loading, cats, tab, choose };
  }
};

try {
  var hint = document.getElementById('loading-hint');
  if (hint) hint.remove();

  const app = createApp({ template: '<router-view />' });
  app.config.errorHandler = (err, vm, info) => {
    console.error('Vue渲染错误:', err.message, '\nInfo:', info, '\nStack:', err.stack);
  };

  app.use(router);
  app.use(ElementPlus);

  for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
    app.component(key, component);
  }
  app.component('sticker-picker', StickerPickerDialog);

  userStore.restoreSession();

  router.beforeEach((to, from) => {
    // 本地工具已免登录：访问登录页直接进首页
    if (to.path === '/login') return '/home';
  });

  app.mount('#app');
  console.log('App started');
} catch (e) {
  console.error('App init error:', e);
  document.body.innerHTML = '<div style="color:#ef4444;padding:40px;font-family:monospace;background:#f5f4f1;min-height:100vh"><h2>应用初始化失败</h2><pre style="white-space:pre-wrap">' + e.message + '\n\n' + (e.stack || '') + '</pre></div>';
}
