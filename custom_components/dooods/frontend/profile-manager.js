const PANEL_PATH = "dooods-profiles";
const ARCHIVE_PAGE_SIZE = 12;
const COLORS = ["#00a6a6", "#f07c43", "#5b88e5", "#c052a0", "#8a9b32", "#e0a420"];

class DooodsProfileManager extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._data = null;
    this._cameraOptions = [];
    this._selectedKey = "";
    this._profile = null;
    this._availableLabels = [];
    this._labelTranslationsLoaded = false;
    this._selectedZone = -1;
    this._selectedVertex = -1;
    this._draggingVertex = -1;
    this._addingVertex = false;
    this._dirty = false;
    this._saving = false;
    this._loading = false;
    this._frameWidth = 1280;
    this._frameHeight = 720;
    this._frameReady = false;
    this._frameRect = null;
    this._frameObjectUrl = null;
    this._frameRequestId = 0;
    this._frameImageRequestId = 0;
    this._activeTab = "archive";
    this._archiveFrames = [];
    this._archiveCursor = "";
    this._archiveEnabled = false;
    this._archiveRequestId = 0;
    this._archiveLoading = false;
    this._archiveObjectUrls = new Map();
    this._render();
    this._bindEvents();
  }

  set hass(hass) {
    this._hass = hass;
    if (hass && !this._loaded) {
      this._loaded = true;
      this._refresh();
    }
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; min-height:100%; color:var(--primary-text-color,#202b32); font-family:inherit; }
        * { box-sizing:border-box; }
        main { max-width:1680px; margin:0 auto; padding:24px clamp(16px,3vw,40px) 40px; }
        header { display:flex; align-items:flex-start; justify-content:space-between; gap:20px; margin-bottom:22px; }
        h1 { margin:0 0 7px; font-size:clamp(22px,3vw,30px); line-height:1.2; font-weight:700; }
        .intro { margin:0; color:var(--secondary-text-color,#64727b); font-size:15px; }
        h2 { margin:0 0 16px; font-size:17px; line-height:1.3; }
        h3 { margin:0 0 12px; font-size:15px; }
        button, select, input { font:inherit; }
        button { min-height:38px; border:1px solid var(--divider-color,#d8e0e4); border-radius:9px; padding:8px 12px; color:var(--primary-text-color,#202b32); background:var(--card-background-color,#fff); cursor:pointer; transition:background .15s,border-color .15s,opacity .15s; }
        button:hover:not(:disabled) { border-color:var(--primary-color,#03a9a4); background:var(--secondary-background-color,#f6fafb); }
        button:disabled { cursor:not-allowed; opacity:.45; }
        button.primary { border-color:var(--primary-color,#03a9a4); color:var(--text-primary-color,#fff); background:var(--primary-color,#03a9a4); }
        button.primary:hover:not(:disabled) { filter:brightness(.95); background:var(--primary-color,#03a9a4); }
        button.danger { color:var(--error-color,#c62828); }
        button.icon { min-width:36px; padding:5px 9px; font-size:18px; line-height:1; }
        .refresh { white-space:nowrap; }
        .status { display:none; margin:0 0 16px; border-radius:9px; padding:11px 14px; background:var(--secondary-background-color,#f3f6f7); color:var(--secondary-text-color,#59666d); }
        .status.visible { display:block; }
        .status.error { border:1px solid color-mix(in srgb,var(--error-color,#c62828) 35%,transparent); color:var(--error-color,#b3261e); background:color-mix(in srgb,var(--error-color,#c62828) 7%,var(--card-background-color,#fff)); }
        .status.success { border:1px solid color-mix(in srgb,var(--success-color,#2e7d32) 35%,transparent); color:var(--success-color,#2e7d32); background:color-mix(in srgb,var(--success-color,#2e7d32) 7%,var(--card-background-color,#fff)); }
        .card { border:1px solid var(--divider-color,#d8e0e4); border-radius:14px; background:var(--card-background-color,#fff); box-shadow:0 2px 10px rgba(24,43,54,.04); }
        .camera-list-card { min-width:0; padding:14px; }
        .camera-list-card h2 { margin-bottom:12px; }
        .camera-list { display:grid; gap:8px; max-height:calc(100vh - 250px); overflow:auto; }
        .camera-choice { width:100%; min-width:0; min-height:42px; padding:9px 10px; text-align:left; white-space:normal; overflow-wrap:anywhere; }
        .camera-choice[aria-pressed="true"] { border-color:var(--primary-color,#03a9a4); background:color-mix(in srgb,var(--primary-color,#03a9a4) 9%,var(--card-background-color,#fff)); }
        .camera-choice:focus-visible { outline:2px solid var(--primary-color,#03a9a4); outline-offset:2px; }
        select, input[type="text"], input[type="number"] { width:100%; min-height:40px; border:1px solid var(--divider-color,#d8e0e4); border-radius:8px; padding:8px 10px; background:var(--card-background-color,#fff); color:var(--primary-text-color,#202b32); }
        .editor-grid { display:grid; grid-template-columns:minmax(180px,230px) minmax(0,1fr) minmax(320px,390px); gap:18px; align-items:start; }
        .tab-view { position:relative; grid-column:2/4; min-width:0; }
        .tab-switch { position:absolute; top:0; left:18px; z-index:5; display:flex; gap:7px; transform:translateY(-50%); }
        .tab-switch-button { display:grid; place-items:center; width:40px; min-width:40px; height:40px; min-height:40px; padding:8px; border-radius:50%; color:var(--secondary-text-color,#64727b); background:var(--card-background-color,#fff); box-shadow:0 2px 8px rgba(24,43,54,.08); }
        .tab-switch-button svg { display:block; width:20px; height:20px; }
        .tab-switch-button.active { border-color:var(--primary-color,#03a9a4); color:var(--primary-color,#03a9a4); background:var(--card-background-color,#fff); box-shadow:0 0 0 3px color-mix(in srgb,var(--primary-color,#03a9a4) 10%,transparent); }
        .archive-pane { min-width:0; min-height:360px; border:1px solid var(--divider-color,#d8e0e4); border-radius:14px; padding:32px 18px 18px; background:var(--card-background-color,#fff); box-shadow:0 2px 10px rgba(24,43,54,.04); }
        .archive-title { margin:0 0 16px; font-size:17px; line-height:1.3; }
        .archive-status { margin:0 0 14px; color:var(--secondary-text-color,#64727b); font-size:13px; line-height:1.5; }
        .archive-status:empty { display:none; }
        .archive-status.error { color:var(--error-color,#b3261e); }
        .archive-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; }
        .archive-card { min-width:0; overflow:hidden; border:1px solid var(--divider-color,#d8e0e4); border-radius:10px; background:var(--card-background-color,#fff); }
        .archive-thumb { position:relative; display:block; width:100%; min-height:0; aspect-ratio:16/9; overflow:hidden; border:0; border-radius:0; padding:0; background:var(--secondary-background-color,#edf1f2); }
        .archive-thumb:hover:not(:disabled) { border-color:var(--divider-color,#d8e0e4); background:var(--secondary-background-color,#edf1f2); }
        .archive-thumb:focus-visible { outline:2px solid var(--primary-color,#03a9a4); outline-offset:-2px; }
        .archive-thumb img { display:block; width:100%; height:100%; object-fit:cover; }
        .archive-thumb-placeholder { position:absolute; inset:0; display:grid; place-items:center; color:var(--secondary-text-color,#64727b); font-size:12px; }
        .archive-thumb-placeholder[hidden] { display:none; }
        .archive-card time { display:block; overflow:hidden; padding:7px 8px; color:var(--secondary-text-color,#64727b); font-size:12px; font-variant-numeric:tabular-nums; text-overflow:ellipsis; white-space:nowrap; }
        .archive-load-more { display:block; margin:16px auto 0; }
        .archive-load-more[hidden] { display:none; }
        .archive-modal { position:fixed; inset:0; z-index:20; display:grid; place-items:center; padding:22px; background:rgba(0,0,0,.78); }
        .archive-modal[hidden] { display:none; }
        .archive-modal-card { position:relative; display:grid; justify-items:center; gap:10px; max-width:min(96vw,1600px); max-height:94vh; border:1px solid var(--divider-color,#d8e0e4); border-radius:12px; padding:14px; background:var(--card-background-color,#fff); box-shadow:0 18px 55px rgba(0,0,0,.35); }
        .archive-modal-close { position:absolute; top:8px; right:8px; z-index:1; display:grid; place-items:center; width:36px; min-width:36px; height:36px; min-height:36px; padding:0; border-radius:50%; font-size:24px; line-height:1; }
        .archive-modal-image { display:block; max-width:calc(96vw - 56px); max-height:calc(94vh - 80px); object-fit:contain; }
        .archive-modal-time { color:var(--secondary-text-color,#64727b); font-size:13px; font-variant-numeric:tabular-nums; }
        .settings-pane[hidden] { display:none; }
        .editor-body-grid { display:grid; grid-template-columns:minmax(0,1fr) minmax(320px,390px); gap:18px; align-items:start; }
        .settings-pane .stage-card { padding-top:42px; }
        .stage-card { min-width:0; padding:18px; }
        .stage-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }
        .stage-heading h2 { margin:0; }
        .frame-refresh { min-height:34px; padding:6px 10px; font-size:13px; }
        .stage { position:relative; display:flex; align-items:center; justify-content:center; width:100%; height:clamp(300px,62vh,720px); overflow:hidden; border-radius:10px; background:#10191e; }
        .frame-image { display:block; width:100%; height:100%; object-fit:contain; }
        .frame-image[hidden] { display:none; }
        .frame-placeholder { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; padding:24px; color:#d5e1e5; text-align:center; font-size:15px; }
        .frame-placeholder[hidden] { display:none; }
        .overlay { position:absolute; display:block; overflow:visible; touch-action:none; }
        .overlay polygon { cursor:default; }
        .overlay .vertex { fill:#fff; stroke:#12232a; stroke-width:2.5; cursor:grab; }
        .overlay .vertex:active { cursor:grabbing; }
        .overlay .insert-handle { cursor:pointer; }
        .overlay .insert-handle circle { fill:#28a745; stroke:#fff; stroke-width:2; }
        .overlay .insert-handle line { stroke:#fff; stroke-width:2; stroke-linecap:round; }
        .overlay .vertex-number { fill:#fff; font-family:inherit; font-weight:700; paint-order:stroke; stroke:#12232a; stroke-width:3px; stroke-linejoin:round; }
        .stage-actions { display:flex; flex-wrap:wrap; gap:9px; margin-top:13px; }
        .hint { margin:12px 0 0; color:var(--secondary-text-color,#64727b); font-size:13px; line-height:1.5; }
        aside { display:grid; gap:16px; }
        .zone-card, .settings-card { padding:18px; }
        .zone-list { display:grid; gap:8px; }
        .zone-row { display:grid; grid-template-columns:30px minmax(0,1fr) auto; align-items:center; gap:7px; border:1px solid var(--divider-color,#e0e6e9); border-radius:9px; padding:6px; background:var(--card-background-color,#fff); }
        .zone-row.selected { border-color:var(--primary-color,#03a9a4); background:color-mix(in srgb,var(--primary-color,#03a9a4) 6%,var(--card-background-color,#fff)); }
        .zone-index { color:var(--secondary-text-color,#64727b); text-align:center; font-size:13px; font-weight:650; }
        .zone-choice { min-width:0; min-height:34px; padding:5px 7px; border:0; overflow:hidden; text-align:left; text-overflow:ellipsis; white-space:nowrap; background:transparent; }
        .zone-choice:hover:not(:disabled) { border:0; background:transparent; color:var(--primary-color,#03a9a4); }
        .mini-actions { display:flex; gap:4px; }
        .mini-actions button { min-width:30px; min-height:30px; padding:3px; border-radius:7px; font-size:16px; }
        .zone-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
        .settings-card[hidden] { display:none; }
        .field { display:grid; gap:7px; margin-bottom:14px; }
        .field label, .field-title { font-size:13px; font-weight:650; }
        .label-list { display:grid; gap:8px; }
        .label-row { display:grid; grid-template-columns:minmax(0,1fr) 82px 36px; gap:7px; align-items:center; }
        .label-row select, .label-row input { min-width:0; min-height:38px; padding:6px 8px; }
        .label-row button { min-width:36px; min-height:36px; padding:4px; font-size:19px; }
        .labels-header { display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:8px; }
        .labels-footer { display:flex; justify-content:flex-end; margin-top:8px; }
        .labels-footer button { min-height:32px; padding:5px 9px; font-size:13px; }
        .save-row { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:18px; padding-top:14px; border-top:1px solid var(--divider-color,#e0e6e9); }
        .dirty-note { min-height:16px; color:var(--secondary-text-color,#64727b); font-size:12px; }
        .empty { margin:0; color:var(--secondary-text-color,#64727b); font-size:14px; line-height:1.5; }
        .empty-labels { padding:5px 0 7px; color:var(--secondary-text-color,#64727b); font-size:13px; }
        @media (max-width:1220px) { .editor-grid { grid-template-columns:minmax(180px,220px) minmax(0,1fr); } .tab-view { grid-column:2; } .editor-body-grid { grid-template-columns:1fr; } .profile-sidebar { grid-column:auto; } }
        @media (max-width:820px) { main { padding:18px 14px 28px; } .editor-grid { grid-template-columns:1fr; } .tab-view { grid-column:1; } .editor-body-grid { grid-template-columns:1fr; } .profile-sidebar { grid-column:auto; } .camera-list { grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); max-height:none; } .stage { height:clamp(260px,55vh,560px); } .archive-grid { gap:8px; } }
        @media (max-width:560px) { header { align-items:flex-start; } .stage-card,.camera-list-card,.zone-card,.settings-card { padding:13px; } .settings-pane .stage-card { padding:42px 13px 13px; } .archive-pane { padding-right:12px; padding-left:12px; } .stage { height:48vh; min-height:250px; } .stage-heading { align-items:flex-start; } .label-row { grid-template-columns:minmax(0,1fr) 72px 34px; gap:5px; } }
      </style>
      <main>
        <header>
          <div>
            <h1>Профили камер DOOODS</h1>
            <p class="intro">Выберите камеру, затем редактируйте профиль ниже.</p>
          </div>
          <button id="refresh-button" class="refresh" type="button">Обновить список</button>
        </header>
        <div class="editor-grid">
          <section class="card camera-list-card" aria-label="Выбор камеры">
            <h2>Камеры</h2>
            <div id="camera-list" class="camera-list" role="group" aria-label="Выбрать камеру">
              <p class="empty">Загрузка камер…</p>
            </div>
          </section>
          <section class="tab-view" aria-label="Архив и настройка камеры">
            <nav class="tab-switch" role="tablist" aria-label="Раздел камеры">
              <button id="archive-tab-button" class="tab-switch-button active" type="button" role="tab" data-tab="archive" aria-controls="archive-pane" aria-selected="true" aria-label="Архив" title="Архив">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16v4H4zM5 8v12h14V8M9 12h6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </button>
              <button id="settings-tab-button" class="tab-switch-button" type="button" role="tab" data-tab="settings" aria-controls="settings-pane" aria-selected="false" aria-label="Настройка" title="Настройка">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8.2a3.8 3.8 0 1 0 0 7.6 3.8 3.8 0 0 0 0-7.6Z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="m19.4 13.5 1.1.9-1.8 3.1-1.4-.5a7.5 7.5 0 0 1-1.6.9l-.3 1.5h-3.6l-.3-1.5a7.5 7.5 0 0 1-1.6-.9l-1.4.5-1.8-3.1 1.1-.9a7.4 7.4 0 0 1 0-1.9l-1.1-.9 1.8-3.1 1.4.5a7.5 7.5 0 0 1 1.6-.9l.3-1.5h3.6l.3 1.5a7.5 7.5 0 0 1 1.6.9l1.4-.5 1.8 3.1-1.1.9a7.4 7.4 0 0 1 0 1.9Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>
              </button>
            </nav>
            <section id="archive-pane" class="archive-pane" role="tabpanel" aria-labelledby="archive-tab-button">
              <h2 class="archive-title">Архив изображений</h2>
              <p id="archive-status" class="archive-status" role="status" aria-live="polite">Выберите камеру для просмотра архива.</p>
              <div id="archive-grid" class="archive-grid" aria-live="polite"></div>
              <button id="archive-load-more" class="archive-load-more" type="button" hidden>Показать ещё</button>
            </section>
            <section id="settings-pane" class="settings-pane" role="tabpanel" aria-labelledby="settings-tab-button" hidden>
              <div class="editor-body-grid">
                <section class="card stage-card">
                  <div class="stage-heading">
                    <h2>Предпросмотр и зоны</h2>
                    <button id="refresh-frame-button" class="frame-refresh" type="button">Обновить кадр</button>
                  </div>
                  <div id="stage" class="stage">
                    <div id="frame-placeholder" class="frame-placeholder">Выберите камеру для загрузки кадра.</div>
                    <img id="frame-image" class="frame-image" alt="Кадр выбранной камеры" hidden>
                    <svg id="overlay" class="overlay" role="img" aria-label="Зоны и вершины"></svg>
                  </div>
                  <div class="stage-actions">
                    <button id="add-vertex-button" type="button" disabled>Добавить вершину</button>
                    <button id="remove-vertex-button" type="button" disabled>Удалить выбранную вершину</button>
                  </div>
                  <p class="hint">Перетаскивайте вершины; добавляйте вершину на выбранное ребро; удалить можно выбранную вершину.</p>
                </section>
                <aside class="profile-sidebar">
                  <section class="card zone-card">
                    <h2>Зоны профиля</h2>
                    <div id="zone-list" class="zone-list"><p class="empty">Загрузка профиля…</p></div>
                    <div class="zone-actions">
                      <button id="add-zone-button" type="button" disabled>Добавить зону</button>
                      <button id="remove-zone-button" class="danger" type="button" disabled>Удалить зону</button>
                    </div>
                  </section>
                  <section id="settings-card" class="card settings-card" hidden>
                    <h2>Настройки выбранной зоны</h2>
                    <div class="field">
                      <label for="zone-name">Название зоны</label>
                      <input id="zone-name" type="text" maxlength="128" autocomplete="off">
                    </div>
                    <div class="field">
                      <label for="covers-select">Учитывать объект целиком внутри зоны</label>
                      <select id="covers-select">
                        <option value="false">Нет, достаточно пересечения</option>
                        <option value="true">Да, объект должен помещаться целиком</option>
                      </select>
                    </div>
                    <div class="field">
                      <div class="labels-header">
                        <span class="field-title">Метки и пороги</span>
                      </div>
                      <div id="label-list" class="label-list"></div>
                      <div class="labels-footer">
                        <button id="add-label-button" type="button" disabled>Добавить метку</button>
                      </div>
                    </div>
                    <div class="save-row">
                      <span id="dirty-note" class="dirty-note"></span>
                      <button id="save-zones-button" class="primary" type="button" disabled>Сохранить зоны</button>
                    </div>
                  </section>
                  <div id="status" class="status" role="status" aria-live="polite"></div>
                </aside>
              </div>
            </section>
            <div id="archive-modal" class="archive-modal" hidden>
              <div class="archive-modal-card" role="dialog" aria-modal="true" aria-labelledby="archive-modal-time">
                <button id="archive-modal-close" class="archive-modal-close" type="button" aria-label="Закрыть">×</button>
                <img id="archive-modal-image" class="archive-modal-image" alt="Увеличенный кадр из архива">
                <time id="archive-modal-time" class="archive-modal-time"></time>
              </div>
            </div>
          </section>
        </div>
      </main>`;

    this._stage = this.shadowRoot.getElementById("stage");
    this._image = this.shadowRoot.getElementById("frame-image");
    this._overlay = this.shadowRoot.getElementById("overlay");
    this._placeholder = this.shadowRoot.getElementById("frame-placeholder");
    this._resizeObserver = new ResizeObserver(() => this._updateFrameRect());
    this._resizeObserver.observe(this._stage);
  }

  _bindEvents() {
    this.shadowRoot.getElementById("refresh-button").addEventListener("click", () => this._refresh());
    this.shadowRoot.getElementById("refresh-frame-button").addEventListener("click", () => this._loadCameraFrame());
    this.shadowRoot.querySelectorAll(".tab-switch-button").forEach((button) => {
      button.addEventListener("click", () => this._setTab(button.dataset.tab));
    });
    const archiveGrid = this.shadowRoot.getElementById("archive-grid");
    archiveGrid.addEventListener("click", (event) => {
      const button = event.target instanceof Element
        ? event.target.closest("button[data-frame-id]")
        : null;
      if (!button || !archiveGrid.contains(button)) return;
      this._openArchiveFrame(button.dataset.frameId);
    });
    this.shadowRoot.getElementById("archive-load-more").addEventListener("click", () => this._loadMoreArchiveFrames());
    const archiveModal = this.shadowRoot.getElementById("archive-modal");
    archiveModal.addEventListener("click", (event) => {
      if (event.target === archiveModal) this._closeArchiveModal();
    });
    this.shadowRoot.getElementById("archive-modal-close").addEventListener("click", () => this._closeArchiveModal());
    this.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.shadowRoot.getElementById("archive-modal").hidden) {
        this._closeArchiveModal();
      }
    });
    const cameraList = this.shadowRoot.getElementById("camera-list");
    cameraList.addEventListener("click", (event) => {
      const button = event.target instanceof Element
        ? event.target.closest("button[data-camera-index]")
        : null;
      if (!button || !cameraList.contains(button)) return;
      this._selectCamera(button.dataset.cameraIndex);
    });
    this.shadowRoot.getElementById("add-zone-button").addEventListener("click", () => this._addZone());
    this.shadowRoot.getElementById("remove-zone-button").addEventListener("click", () => this._removeZone());
    this.shadowRoot.getElementById("add-label-button").addEventListener("click", () => this._addLabel());
    this.shadowRoot.getElementById("save-zones-button").addEventListener("click", () => this._saveZones());
    this.shadowRoot.getElementById("add-vertex-button").addEventListener("click", () => this._toggleVertexInsertion());
    this.shadowRoot.getElementById("remove-vertex-button").addEventListener("click", () => this._removeVertex());
    this.shadowRoot.getElementById("zone-name").addEventListener("input", (event) => {
      const zone = this._currentZone();
      if (!zone) return;
      zone.name = event.target.value;
      this._markDirty();
      this._renderZoneList();
    });
    this.shadowRoot.getElementById("covers-select").addEventListener("change", (event) => {
      const zone = this._currentZone();
      if (!zone) return;
      zone.covers = event.target.value === "true";
      this._markDirty();
    });

    this._overlay.addEventListener("pointerdown", (event) => this._onPointerDown(event));
    this._overlay.addEventListener("pointermove", (event) => this._onPointerMove(event));
    this._overlay.addEventListener("pointerup", () => { this._draggingVertex = -1; });
    this._overlay.addEventListener("pointercancel", () => { this._draggingVertex = -1; });
    this._overlay.addEventListener("keydown", (event) => this._onOverlayKeyDown(event));
    this._image.addEventListener("load", () => {
      if (this._frameImageRequestId !== this._frameRequestId) return;
      this._frameWidth = this._image.naturalWidth || 1280;
      this._frameHeight = this._image.naturalHeight || 720;
      this._frameReady = true;
      this._placeholder.hidden = true;
      this._updateFrameRect();
      this._updateButtons();
    });
    this._image.addEventListener("error", () => {
      if (this._frameImageRequestId !== this._frameRequestId) return;
      this._showFrameError("Home Assistant получил ответ, но браузер не смог отобразить кадр.");
    });
  }

  _revokeFrameObjectUrl() {
    if (this._frameObjectUrl) {
      URL.revokeObjectURL(this._frameObjectUrl);
      this._frameObjectUrl = null;
    }
  }

  _showFrameError(message) {
    this._frameRequestId += 1;
    this._frameImageRequestId = 0;
    this._frameReady = false;
    this._frameRect = null;
    this._image.hidden = true;
    this._image.removeAttribute("src");
    this._revokeFrameObjectUrl();
    this._overlay.replaceChildren();
    this._overlay.style.width = "0";
    this._overlay.style.height = "0";
    this._placeholder.hidden = false;
    this._placeholder.textContent = message;
    this._updateButtons();
  }

  disconnectedCallback() {
    this._frameRequestId += 1;
    this._frameImageRequestId = 0;
    this._archiveRequestId += 1;
    this._revokeFrameObjectUrl();
    this._revokeArchiveObjectUrls();
  }

  _revokeArchiveObjectUrls() {
    for (const objectUrl of this._archiveObjectUrls.values()) {
      URL.revokeObjectURL(objectUrl);
    }
    this._archiveObjectUrls.clear();
  }

  _clearArchiveState(message = "") {
    this._archiveRequestId += 1;
    this._archiveFrames = [];
    this._archiveCursor = "";
    this._archiveEnabled = false;
    this._archiveLoading = false;
    this._revokeArchiveObjectUrls();
    this._closeArchiveModal(false);
    this.shadowRoot.getElementById("archive-grid").replaceChildren();
    this.shadowRoot.getElementById("archive-load-more").hidden = true;
    this._setArchiveStatus(message);
  }

  _setArchiveStatus(message = "", kind = "") {
    const status = this.shadowRoot.getElementById("archive-status");
    status.textContent = message;
    status.className = `archive-status${kind ? ` ${kind}` : ""}`;
  }

  _setTab(tab) {
    const selectedTab = tab === "settings" ? "settings" : "archive";
    this._activeTab = selectedTab;
    const archiveActive = selectedTab === "archive";
    this.shadowRoot.getElementById("archive-pane").hidden = !archiveActive;
    this.shadowRoot.getElementById("settings-pane").hidden = archiveActive;
    this.shadowRoot.querySelectorAll(".tab-switch-button").forEach((button) => {
      const selected = button.dataset.tab === selectedTab;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-selected", String(selected));
    });
    if (archiveActive) this._loadArchive(true);
  }

  _formatArchiveTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value || "");
    const locale = this._hass?.locale?.language || "ru-RU";
    const timeZone = this._hass?.config?.time_zone;
    const options = { dateStyle: "short", timeStyle: "medium" };
    if (typeof timeZone === "string" && timeZone) options.timeZone = timeZone;
    try {
      return new Intl.DateTimeFormat(locale, options).format(date);
    } catch (_error) {
      return date.toLocaleString();
    }
  }

  _renderArchiveFrames() {
    const grid = this.shadowRoot.getElementById("archive-grid");
    grid.replaceChildren();
    for (const frame of this._archiveFrames) {
      const card = document.createElement("article");
      card.className = "archive-card";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "archive-thumb";
      button.dataset.frameId = frame.frame_id;
      button.setAttribute("aria-label", `Открыть кадр от ${this._formatArchiveTime(frame.captured_at)}`);
      const image = document.createElement("img");
      image.alt = `Кадр архива от ${this._formatArchiveTime(frame.captured_at)}`;
      image.loading = "lazy";
      image.dataset.frameId = frame.frame_id;
      const placeholder = document.createElement("span");
      placeholder.className = "archive-thumb-placeholder";
      placeholder.textContent = "Загрузка…";
      button.append(image, placeholder);
      const time = document.createElement("time");
      time.dateTime = frame.captured_at;
      time.textContent = this._formatArchiveTime(frame.captured_at);
      card.append(button, time);
      grid.appendChild(card);
      const objectUrl = this._archiveObjectUrls.get(frame.frame_id);
      if (objectUrl) {
        image.src = objectUrl;
        placeholder.hidden = true;
      }
    }
    const loadMore = this.shadowRoot.getElementById("archive-load-more");
    loadMore.hidden = !this._archiveCursor;
    loadMore.disabled = this._archiveLoading;
    loadMore.textContent = this._archiveLoading ? "Загрузка…" : "Показать ещё";
  }

  _setArchiveImageSource(frameId, objectUrl) {
    const image = [...this.shadowRoot.querySelectorAll("img[data-frame-id]")]
      .find((item) => item.dataset.frameId === frameId);
    if (!image) return;
    image.src = objectUrl;
    const placeholder = image.parentElement?.querySelector(".archive-thumb-placeholder");
    if (placeholder) placeholder.hidden = true;
  }

  _archiveImageUrl(option, frameId) {
    const params = new URLSearchParams({
      entry_id: option.entryId,
      camera_entity_id: option.camera.entity_id,
      frame_id: frameId,
    });
    const path = `/api/dooods/profile_manager/archive_image?${params.toString()}`;
    if (this._hass && typeof this._hass.hassUrl === "function") {
      const target = new URL(this._hass.hassUrl(path), window.location.href);
      return `${target.pathname}${target.search}`;
    }
    const pathname = window.location.pathname;
    const panelMarker = `/${PANEL_PATH}`;
    const panelIndex = pathname.indexOf(panelMarker);
    const prefix = panelIndex >= 0 ? pathname.slice(0, panelIndex) : "";
    return `${prefix}${path}`;
  }

  _setStatus(message = "", kind = "") {
    const status = this.shadowRoot.getElementById("status");
    status.textContent = message;
    status.className = `status${message ? " visible" : ""}${kind ? ` ${kind}` : ""}`;
  }

  async _loadArchiveImage(option, frame, requestId) {
    const frameId = frame.frame_id;
    const existingUrl = this._archiveObjectUrls.get(frameId);
    if (existingUrl) {
      this._setArchiveImageSource(frameId, existingUrl);
      return existingUrl;
    }
    if (!this._hass || typeof this._hass.fetchWithAuth !== "function") {
      const placeholder = [...this.shadowRoot.querySelectorAll(".archive-thumb-placeholder")]
        .find((item) => item.parentElement?.dataset.frameId === frameId);
      if (placeholder) placeholder.textContent = "Нет доступа";
      return null;
    }
    try {
      const response = await this._hass.fetchWithAuth(
        this._archiveImageUrl(option, frameId),
        { cache: "no-store" },
      );
      if (
        requestId !== this._archiveRequestId
        || this._selectedOption()?.key !== option.key
      ) return null;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      if (!blob.size) throw new Error("empty image");
      const objectUrl = URL.createObjectURL(blob);
      if (
        requestId !== this._archiveRequestId
        || this._selectedOption()?.key !== option.key
      ) {
        URL.revokeObjectURL(objectUrl);
        return null;
      }
      this._archiveObjectUrls.set(frameId, objectUrl);
      this._setArchiveImageSource(frameId, objectUrl);
      return objectUrl;
    } catch (_error) {
      if (
        requestId !== this._archiveRequestId
        || this._selectedOption()?.key !== option.key
      ) return null;
      const button = [...this.shadowRoot.querySelectorAll("button[data-frame-id]")]
        .find((item) => item.dataset.frameId === frameId);
      const placeholder = button?.querySelector(".archive-thumb-placeholder");
      if (placeholder) placeholder.textContent = "Не удалось загрузить";
      return null;
    }
  }

  async _loadArchive(reset = false) {
    const option = this._selectedOption();
    if (!this._hass || !option) {
      if (!this._cameraOptions.length) this._clearArchiveState("Выберите камеру для просмотра архива.");
      return;
    }
    if (!reset && (!this._archiveCursor || this._archiveLoading)) return;
    if (reset) {
      this._archiveRequestId += 1;
      this._archiveFrames = [];
      this._archiveCursor = "";
      this._archiveEnabled = false;
      this._revokeArchiveObjectUrls();
      this._closeArchiveModal(false);
      this._setArchiveStatus("Загрузка архива…");
      this._renderArchiveFrames();
    }
    const requestId = this._archiveRequestId;
    const cursor = reset ? "" : this._archiveCursor;
    this._archiveLoading = true;
    this._renderArchiveFrames();
    try {
      const payload = await this._hass.callWS({
        type: "dooods/profile_manager/archive_data",
        entry_id: option.entryId,
        camera_entity_id: option.camera.entity_id,
        cursor,
        limit: ARCHIVE_PAGE_SIZE,
      });
      if (
        requestId !== this._archiveRequestId
        || this._selectedOption()?.key !== option.key
      ) return;
      if (!payload || typeof payload !== "object") throw new Error("invalid archive response");
      if (payload.enabled !== true) {
        this._archiveEnabled = false;
        this._archiveFrames = [];
        this._archiveCursor = "";
        this._setArchiveStatus("Архив для этой камеры не настроен.");
        return;
      }
      this._archiveEnabled = true;
      const incoming = Array.isArray(payload.frames)
        ? payload.frames.filter((frame) => (
          frame
          && typeof frame.frame_id === "string"
          && typeof frame.captured_at === "string"
        ))
        : [];
      const knownIds = new Set(this._archiveFrames.map((frame) => frame.frame_id));
      const newFrames = incoming.filter((frame) => !knownIds.has(frame.frame_id));
      this._archiveFrames = reset
        ? newFrames
        : [...this._archiveFrames, ...newFrames];
      this._archiveCursor = typeof payload.next_cursor === "string" ? payload.next_cursor : "";
      this._setArchiveStatus(
        this._archiveFrames.length ? "" : "Архивных изображений пока нет.",
      );
      this._renderArchiveFrames();
      await Promise.all(
        newFrames.map((frame) => this._loadArchiveImage(option, frame, requestId)),
      );
    } catch (_error) {
      if (
        requestId === this._archiveRequestId
        && this._selectedOption()?.key === option.key
      ) {
        this._setArchiveStatus(
          "Не удалось загрузить архив. Проверьте папку архива и права доступа.",
          "error",
        );
      }
    } finally {
      if (
        requestId === this._archiveRequestId
        && this._selectedOption()?.key === option.key
      ) {
        this._archiveLoading = false;
        this._renderArchiveFrames();
      }
    }
  }

  _loadMoreArchiveFrames() {
    if (this._archiveCursor && !this._archiveLoading) this._loadArchive(false);
  }

  async _openArchiveFrame(frameId) {
    const option = this._selectedOption();
    const frame = this._archiveFrames.find((item) => item.frame_id === frameId);
    if (!option || !frame) return;
    const requestId = this._archiveRequestId;
    let objectUrl = this._archiveObjectUrls.get(frameId);
    if (!objectUrl) objectUrl = await this._loadArchiveImage(option, frame, requestId);
    if (
      !objectUrl
      || requestId !== this._archiveRequestId
      || this._selectedOption()?.key !== option.key
    ) return;
    const modal = this.shadowRoot.getElementById("archive-modal");
    this._archiveModalPreviousFocus = this.shadowRoot.activeElement;
    this.shadowRoot.getElementById("archive-modal-image").src = objectUrl;
    const time = this.shadowRoot.getElementById("archive-modal-time");
    time.dateTime = frame.captured_at;
    time.textContent = this._formatArchiveTime(frame.captured_at);
    modal.hidden = false;
    this.shadowRoot.getElementById("archive-modal-close").focus();
  }

  _closeArchiveModal(restoreFocus = true) {
    const modal = this.shadowRoot.getElementById("archive-modal");
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    this.shadowRoot.getElementById("archive-modal-image").removeAttribute("src");
    const previousFocus = this._archiveModalPreviousFocus;
    this._archiveModalPreviousFocus = null;
    if (restoreFocus && previousFocus?.isConnected) previousFocus.focus();
  }

  _errorMessage(error) {
    if (error && typeof error.message === "string" && error.message.trim()) {
      return error.message;
    }
    return "Повторите запрос или проверьте настройки DOOODS.";
  }

  _cameraOptionsFor(data) {
    const options = [];
    for (const entry of (data && Array.isArray(data.entries) ? data.entries : [])) {
      const cameras = Array.isArray(entry.cameras) ? entry.cameras : [];
      if (!cameras.length) continue;
      for (const camera of cameras) {
        options.push({
          entryId: entry.entry_id,
          profilesError: entry.profiles_error || "",
          camera,
          key: `${entry.entry_id}\u0000${camera.entity_id}`,
        });
      }
    }
    return options;
  }

  _cameraDisplayName(camera) {
    const name = typeof camera?.name === "string" ? camera.name.trim() : "";
    return name && name !== camera.entity_id ? name : "Без friendly_name";
  }

  _renderCameraOptions() {
    const list = this.shadowRoot.getElementById("camera-list");
    list.replaceChildren();
    if (!this._cameraOptions.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "В настройках DOOODS не выбраны камеры.";
      list.appendChild(empty);
      return;
    }

    const selectedIndex = this._cameraOptions.findIndex(
      (option) => option.key === this._selectedKey,
    );
    this._cameraOptions.forEach((option, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "camera-choice";
      button.dataset.cameraIndex = String(index);
      button.textContent = this._cameraDisplayName(option.camera);
      button.setAttribute("aria-pressed", String(index === selectedIndex));
      list.appendChild(button);
    });
  }

  async _ensureLabelTranslations() {
    if (this._labelTranslationsLoaded) return;
    this._labelTranslationsLoaded = true;
    try {
      await this._hass?.loadBackendTranslation?.("entity", "dooods");
    } catch (_error) {
      // Keep raw detector labels as a safe fallback if HA translations are unavailable.
      this._labelTranslationsLoaded = false;
    }
  }

  async _refresh() {
    if (!this._hass || this._loading) return;
    if (this._dirty && !window.confirm("Есть несохранённые изменения. Отменить их и обновить список камер?")) return;
    this._loading = true;
    this._setStatus("Загрузка камер…");
    try {
      await this._ensureLabelTranslations();
      const previousKey = this._selectedKey;
      this._data = await this._hass.callWS({ type: "dooods/profile_manager/data" });
      this._cameraOptions = this._cameraOptionsFor(this._data);
      const selected = this._cameraOptions.find((option) => option.key === previousKey)
        || this._cameraOptions.find((option) => option.camera.profile_ready)
        || this._cameraOptions[0];
      this._selectedKey = selected ? selected.key : "";
      this._renderCameraOptions();
      if (!selected) {
        this._clearArchiveState("В настройках DOOODS не выбраны камеры.");
        this._clearEditor("В настройках DOOODS не выбраны камеры.");
        this._setStatus("");
      } else {
        await this._openSelectedCamera();
      }
    } catch (error) {
      this._setStatus(`Не удалось загрузить список камер: ${this._errorMessage(error)}`, "error");
      this._clearEditor("Профиль камеры не загружен.");
    } finally {
      this._loading = false;
      this._updateButtons();
    }
  }

  async _selectCamera(value) {
    const index = Number(value);
    const next = Number.isInteger(index) ? this._cameraOptions[index] : null;
    if (!next || next.key === this._selectedKey) return;
    if (this._dirty && !window.confirm("Есть несохранённые изменения. Отменить их и выбрать другую камеру?")) {
      this._renderCameraOptions();
      return;
    }
    this._selectedKey = next.key;
    this._renderCameraOptions();
    await this._openSelectedCamera();
  }

  _selectedOption() {
    return this._cameraOptions.find((option) => option.key === this._selectedKey) || null;
  }

  async _openSelectedCamera() {
    const option = this._selectedOption();
    this._profile = null;
    this._availableLabels = [];
    this._selectedZone = -1;
    this._selectedVertex = -1;
    this._dirty = false;
    this._addingVertex = false;
    this._frameRequestId += 1;
    this._frameImageRequestId = 0;
    this._revokeFrameObjectUrl();
    this._frameReady = false;
    this._frameRect = null;
    this._image.hidden = true;
    this._image.removeAttribute("src");
    this._overlay.replaceChildren();
    this._overlay.style.width = "0";
    this._overlay.style.height = "0";
    this._placeholder.hidden = false;
    this._placeholder.textContent = "Загрузка кадра выбранной камеры…";
    this._renderZoneList();
    this._renderZoneSettings();
    this._updateButtons();
    if (!option) {
      this._clearArchiveState("Выберите камеру для просмотра архива.");
      this._setStatus("");
      return;
    }
    this._loadArchive(true);
    this._loadCameraFrame();
    if (option.profilesError) {
      const messages = {
        auth_missing: "Профили недоступны: проверьте API-ключ в настройках DOOODS.",
        invalid_auth: "Сервер DOOODS2 отклонил API-ключ. Проверьте настройки подключения.",
        unsupported: "Сервер DOOODS2 не поддерживает редактирование профилей и зон.",
        unavailable: "Не удалось связаться с профильным API DOOODS2.",
      };
      this._clearEditor(messages[option.profilesError] || messages.unavailable);
      this._setStatus(messages[option.profilesError] || messages.unavailable, "error");
      return;
    }
    if (!option.camera.profile_ready) {
      const message = "Профиль этой камеры не найден. Перезагрузите интеграцию DOOODS и повторите попытку.";
      this._clearEditor(message);
      this._setStatus(message, "error");
      return;
    }

    this._setStatus("Загрузка профиля камеры…");
    try {
      const payload = await this._hass.callWS({
        type: "dooods/profile_manager/editor_data",
        entry_id: option.entryId,
        camera_entity_id: option.camera.entity_id,
      });
      if (!payload || !Array.isArray(payload.zones) || payload.zones.length < 1) {
        throw new Error("В профиле камеры нет зон. Сохранение без зоны запрещено.");
      }
      this._profile = {
        zones: payload.zones.map((zone) => this._normalizeZone(zone)),
      };
      this._availableLabels = Array.isArray(payload.available_labels)
        ? payload.available_labels.filter((label) => typeof label === "string" && label.trim())
        : [];
      if (!this._availableLabels.length) {
        throw new Error("Детектор не вернул доступные метки. Проверьте настройки модели DOODS2.");
      }
      this._selectedZone = 0;
      this._dirty = false;
      this._setStatus("");
      this._renderZoneList();
      this._renderZoneSettings();
    } catch (error) {
      const message = `Не удалось загрузить профиль камеры: ${this._errorMessage(error)}`;
      this._clearEditor(message);
      this._setStatus(message, "error");
    }
  }

  _normalizeZone(zone) {
    const points = Array.isArray(zone.points) ? zone.points : [];
    const labels = zone.labels && typeof zone.labels === "object" && !Array.isArray(zone.labels)
      ? { ...zone.labels }
      : {};
    return {
      id: typeof zone.id === "string" ? zone.id : this._newId("zone"),
      name: typeof zone.name === "string" && zone.name.trim() ? zone.name : "Зона",
      points: points.map((point) => ({ left: Number(point.left), top: Number(point.top) })),
      covers: Boolean(zone.covers),
      labels,
    };
  }

  _newId(prefix) {
    const random = window.crypto && typeof window.crypto.randomUUID === "function"
      ? window.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
    return `${prefix}-${random}`;
  }

  _clearEditor(message) {
    this._profile = null;
    this._availableLabels = [];
    this._selectedZone = -1;
    this._selectedVertex = -1;
    this._dirty = false;
    this._addingVertex = false;
    this._renderZoneList(message);
    this._renderZoneSettings();
    this._updateButtons();
  }

  _currentZone() {
    if (!this._profile || this._selectedZone < 0) return null;
    return this._profile.zones[this._selectedZone] || null;
  }

  _renderZoneList(emptyMessage = "Сначала выберите камеру.") {
    const container = this.shadowRoot.getElementById("zone-list");
    container.replaceChildren();
    if (!this._profile || !this._profile.zones.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = emptyMessage;
      container.appendChild(empty);
      this._drawOverlay();
      return;
    }
    this._profile.zones.forEach((zone, index) => {
      const row = document.createElement("div");
      row.className = `zone-row${index === this._selectedZone ? " selected" : ""}`;
      const number = document.createElement("span");
      number.className = "zone-index";
      number.textContent = `${index + 1}.`;
      const select = document.createElement("button");
      select.type = "button";
      select.className = "zone-choice";
      select.textContent = zone.name || `Зона ${index + 1}`;
      select.addEventListener("click", () => this._selectZone(index));
      const actions = document.createElement("div");
      actions.className = "mini-actions";
      const up = document.createElement("button");
      up.type = "button";
      up.className = "icon";
      up.textContent = "↑";
      up.title = "Переместить зону выше";
      up.setAttribute("aria-label", up.title);
      up.disabled = index === 0;
      up.addEventListener("click", () => this._moveZone(index, -1));
      const down = document.createElement("button");
      down.type = "button";
      down.className = "icon";
      down.textContent = "↓";
      down.title = "Переместить зону ниже";
      down.setAttribute("aria-label", down.title);
      down.disabled = index === this._profile.zones.length - 1;
      down.addEventListener("click", () => this._moveZone(index, 1));
      actions.append(up, down);
      row.append(number, select, actions);
      container.appendChild(row);
    });
    this._drawOverlay();
  }

  _renderZoneSettings() {
    const zone = this._currentZone();
    const card = this.shadowRoot.getElementById("settings-card");
    card.hidden = !zone;
    if (!zone) return;
    this.shadowRoot.getElementById("zone-name").value = zone.name;
    this.shadowRoot.getElementById("covers-select").value = String(Boolean(zone.covers));
    this._renderLabels();
    this._updateButtons();
    this._drawOverlay();
  }

  _displayLabel(label) {
    const normalized = String(label)
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "");
    if (!normalized) return label;
    const translationKey = `component.dooods.entity.binary_sensor.label_${normalized}.name`;
    let translated;
    try {
      translated = this._hass?.localize?.(translationKey);
    } catch (_error) {
      return label;
    }
    if (
      typeof translated !== "string"
      || !translated.trim()
      || translated === translationKey
      || translated.startsWith("Translation Error:")
    ) return label;
    return translated;
  }

  _renderLabels() {
    const zone = this._currentZone();
    const container = this.shadowRoot.getElementById("label-list");
    container.replaceChildren();
    if (!zone) return;
    const labels = zone.labels || {};
    const entries = Object.entries(labels);
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "empty-labels";
      empty.textContent = "В этой зоне пока нет меток.";
      container.appendChild(empty);
    }
    for (const [label, threshold] of entries) {
      const displayLabel = this._displayLabel(label);
      const row = document.createElement("div");
      row.className = "label-row";
      const select = document.createElement("select");
      select.setAttribute("aria-label", "Метка зоны");
      const choices = [...new Set([...this._availableLabels, label])];
      choices.forEach((choice) => {
        const displayChoice = this._displayLabel(choice);
        const option = new Option(
          this._availableLabels.includes(choice)
            ? displayChoice
            : `${displayChoice} (нет в детекторе)`,
          choice,
        );
        option.disabled = choice !== label && Object.prototype.hasOwnProperty.call(labels, choice);
        select.add(option);
      });
      select.value = label;
      const input = document.createElement("input");
      input.type = "number";
      input.min = "0";
      input.max = "100";
      input.step = "1";
      input.value = String(threshold);
      input.setAttribute("aria-label", `Порог confidence для ${displayLabel}, процентов`);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "×";
      remove.title = `Удалить метку ${displayLabel}`;
      remove.setAttribute("aria-label", remove.title);
      const apply = () => {
        const nextLabel = select.value;
        const nextThreshold = Number(input.value);
        if (!nextLabel || !Number.isFinite(nextThreshold) || nextThreshold < 0 || nextThreshold > 100) {
          this._setStatus("Проверьте метку и порог от 0 до 100.", "error");
          return;
        }
        if (nextLabel !== label && Object.prototype.hasOwnProperty.call(labels, nextLabel)) {
          this._setStatus("Эта метка уже добавлена в зону.", "error");
          select.value = label;
          return;
        }
        const updated = { ...labels };
        if (nextLabel === label) {
          updated[label] = nextThreshold;
        } else {
          delete updated[label];
          updated[nextLabel] = nextThreshold;
        }
        zone.labels = updated;
        this._markDirty();
        this._renderLabels();
      };
      select.addEventListener("change", apply);
      input.addEventListener("change", apply);
      remove.addEventListener("click", () => {
        const updated = { ...labels };
        delete updated[label];
        zone.labels = updated;
        this._markDirty();
        this._renderLabels();
      });
      row.append(select, input, remove);
      container.appendChild(row);
    }
  }

  _selectZone(index) {
    if (!this._profile || index < 0 || index >= this._profile.zones.length) return;
    this._selectedZone = index;
    this._selectedVertex = -1;
    this._addingVertex = false;
    this._renderZoneList();
    this._renderZoneSettings();
  }

  _moveZone(index, delta) {
    if (!this._profile) return;
    const next = index + delta;
    if (next < 0 || next >= this._profile.zones.length) return;
    const [zone] = this._profile.zones.splice(index, 1);
    this._profile.zones.splice(next, 0, zone);
    this._selectedZone = next;
    this._selectedVertex = -1;
    this._markDirty();
    this._renderZoneList();
    this._renderZoneSettings();
  }

  _addZone() {
    if (!this._profile || !this._frameReady) return;
    const labels = this._availableLabels.includes("person") ? { person: 50 } : {};
    this._profile.zones.push({
      id: this._newId("zone"),
      name: `Зона ${this._profile.zones.length + 1}`,
      points: [
        { left: 0.15, top: 0.15 },
        { left: 0.65, top: 0.15 },
        { left: 0.65, top: 0.65 },
        { left: 0.15, top: 0.65 },
      ],
      covers: false,
      labels,
    });
    this._selectedZone = this._profile.zones.length - 1;
    this._selectedVertex = -1;
    this._markDirty();
    this._renderZoneList();
    this._renderZoneSettings();
  }

  _removeZone() {
    if (!this._profile || !this._frameReady || this._profile.zones.length <= 1 || this._selectedZone < 0) return;
    this._profile.zones.splice(this._selectedZone, 1);
    this._selectedZone = Math.min(this._selectedZone, this._profile.zones.length - 1);
    this._selectedVertex = -1;
    this._addingVertex = false;
    this._markDirty();
    this._renderZoneList();
    this._renderZoneSettings();
  }

  _addLabel() {
    const zone = this._currentZone();
    if (!zone) return;
    const labels = zone.labels || {};
    const label = this._availableLabels.find((candidate) => !Object.prototype.hasOwnProperty.call(labels, candidate));
    if (!label) return;
    zone.labels = { ...labels, [label]: 50 };
    this._markDirty();
    this._renderLabels();
  }

  _toggleVertexInsertion() {
    if (!this._currentZone()) return;
    this._addingVertex = !this._addingVertex;
    const button = this.shadowRoot.getElementById("add-vertex-button");
    button.textContent = this._addingVertex ? "Выберите ребро…" : "Добавить вершину";
    button.setAttribute("aria-pressed", String(this._addingVertex));
    button.classList.toggle("primary", this._addingVertex);
    this._drawOverlay();
  }

  _insertVertex(edgeIndex) {
    const zone = this._currentZone();
    if (!zone || edgeIndex < 0 || edgeIndex >= zone.points.length) return;
    const nextIndex = (edgeIndex + 1) % zone.points.length;
    const start = zone.points[edgeIndex];
    const end = zone.points[nextIndex];
    const insertionIndex = edgeIndex + 1;
    zone.points.splice(insertionIndex, 0, {
      left: (start.left + end.left) / 2,
      top: (start.top + end.top) / 2,
    });
    this._selectedVertex = insertionIndex;
    this._addingVertex = false;
    this._markDirty();
    this._renderZoneSettings();
  }

  _removeVertex() {
    const zone = this._currentZone();
    if (!zone || zone.points.length <= 3 || this._selectedVertex < 0) return;
    zone.points.splice(this._selectedVertex, 1);
    this._selectedVertex = -1;
    this._markDirty();
    this._renderZoneSettings();
  }

  _pointFromEvent(event) {
    const rect = this._overlay.getBoundingClientRect();
    return {
      left: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      top: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
    };
  }

  _onPointerDown(event) {
    const zone = this._currentZone();
    if (!zone || !this._frameReady) return;
    const insertion = event.target.closest("[data-insert-edge]");
    if (this._addingVertex && insertion) {
      this._insertVertex(Number(insertion.getAttribute("data-insert-edge")));
      event.preventDefault();
      return;
    }
    const vertex = event.target.closest("[data-vertex]");
    if (!vertex) return;
    this._selectedVertex = Number(vertex.getAttribute("data-vertex"));
    this._draggingVertex = this._selectedVertex;
    this._overlay.setPointerCapture(event.pointerId);
    this._renderZoneSettings();
    event.preventDefault();
  }

  _onPointerMove(event) {
    const zone = this._currentZone();
    if (!zone || !this._frameReady || this._draggingVertex < 0) return;
    zone.points[this._draggingVertex] = this._pointFromEvent(event);
    this._markDirty();
    this._drawOverlay();
  }

  _onOverlayKeyDown(event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    const insertion = event.target.closest("[data-insert-edge]");
    if (!this._addingVertex || !insertion) return;
    event.preventDefault();
    this._insertVertex(Number(insertion.getAttribute("data-insert-edge")));
  }

  _updateFrameRect() {
    if (!this._frameReady || !this._image.naturalWidth || !this._image.naturalHeight) return;
    const stageRect = this._stage.getBoundingClientRect();
    const scale = Math.min(stageRect.width / this._frameWidth, stageRect.height / this._frameHeight);
    const width = this._frameWidth * scale;
    const height = this._frameHeight * scale;
    const left = (stageRect.width - width) / 2;
    const top = (stageRect.height - height) / 2;
    this._frameRect = { left, top, width, height };
    this._overlay.style.left = `${left}px`;
    this._overlay.style.top = `${top}px`;
    this._overlay.style.width = `${width}px`;
    this._overlay.style.height = `${height}px`;
    this._drawOverlay();
  }

  _drawOverlay() {
    const svg = this._overlay;
    svg.setAttribute("viewBox", `0 0 ${this._frameWidth} ${this._frameHeight}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.replaceChildren();
    if (!this._frameReady || !this._profile || !this._frameRect) return;
    this._profile.zones.forEach((zone, zoneIndex) => {
      if (!Array.isArray(zone.points) || zone.points.length < 3) return;
      const color = COLORS[zoneIndex % COLORS.length];
      const points = zone.points.map((point) => `${point.left * this._frameWidth},${point.top * this._frameHeight}`).join(" ");
      const polygon = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
      polygon.setAttribute("points", points);
      polygon.setAttribute("fill", color);
      polygon.setAttribute("fill-opacity", zoneIndex === this._selectedZone ? "0.24" : "0.10");
      polygon.setAttribute("stroke", color);
      polygon.setAttribute("stroke-width", zoneIndex === this._selectedZone ? "4" : "2.5");
      polygon.setAttribute("vector-effect", "non-scaling-stroke");
      polygon.setAttribute("stroke-linejoin", "round");
      svg.appendChild(polygon);
      if (zoneIndex !== this._selectedZone) return;

      if (this._addingVertex) {
        zone.points.forEach((start, edgeIndex) => {
          const end = zone.points[(edgeIndex + 1) % zone.points.length];
          const x = ((start.left + end.left) / 2) * this._frameWidth;
          const y = ((start.top + end.top) / 2) * this._frameHeight;
          const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
          group.setAttribute("class", "insert-handle");
          group.setAttribute("data-insert-edge", String(edgeIndex));
          group.setAttribute("transform", `translate(${x} ${y})`);
          group.setAttribute("role", "button");
          group.setAttribute("tabindex", "0");
          group.setAttribute("aria-label", `Добавить вершину между ${edgeIndex + 1} и ${(edgeIndex + 1) % zone.points.length + 1}`);
          const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
          circle.setAttribute("r", String(12 * this._frameWidth / this._overlay.clientWidth));
          group.appendChild(circle);
          const horizontal = document.createElementNS("http://www.w3.org/2000/svg", "line");
          horizontal.setAttribute("x1", String(-5 * this._frameWidth / this._overlay.clientWidth));
          horizontal.setAttribute("x2", String(5 * this._frameWidth / this._overlay.clientWidth));
          group.appendChild(horizontal);
          const vertical = document.createElementNS("http://www.w3.org/2000/svg", "line");
          vertical.setAttribute("y1", String(-5 * this._frameHeight / this._overlay.clientHeight));
          vertical.setAttribute("y2", String(5 * this._frameHeight / this._overlay.clientHeight));
          group.appendChild(vertical);
          svg.appendChild(group);
        });
      }
      zone.points.forEach((point, vertexIndex) => {
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("cx", String(point.left * this._frameWidth));
        circle.setAttribute("cy", String(point.top * this._frameHeight));
        circle.setAttribute("r", String((vertexIndex === this._selectedVertex ? 8 : 6) * this._frameWidth / this._overlay.clientWidth));
        circle.setAttribute("class", "vertex");
        circle.setAttribute("data-vertex", String(vertexIndex));
        circle.setAttribute("aria-label", `Вершина ${vertexIndex + 1}`);
        svg.appendChild(circle);
      });
    });
  }

  _markDirty() {
    this._dirty = true;
    this._setStatus("");
    this._updateButtons();
  }

  _updateButtons() {
    const hasProfile = Boolean(this._profile);
    const zone = this._currentZone();
    this.shadowRoot.getElementById("add-zone-button").disabled = !hasProfile || !this._frameReady;
    this.shadowRoot.getElementById("remove-zone-button").disabled = !hasProfile || !this._frameReady || this._profile.zones.length <= 1;
    this.shadowRoot.getElementById("add-vertex-button").disabled = !zone || !this._frameReady;
    this.shadowRoot.getElementById("remove-vertex-button").disabled = !zone || !this._frameReady || this._selectedVertex < 0 || zone.points.length <= 3;
    const zoneLabels = zone ? zone.labels || {} : {};
    const hasUnusedLabel = this._availableLabels.some((label) => !Object.prototype.hasOwnProperty.call(zoneLabels, label));
    this.shadowRoot.getElementById("add-label-button").disabled = !zone || !hasUnusedLabel;
    this.shadowRoot.getElementById("save-zones-button").disabled = !hasProfile || !this._dirty || this._saving;
    this.shadowRoot.getElementById("save-zones-button").textContent = this._saving ? "Сохранение…" : "Сохранить зоны";
    this.shadowRoot.getElementById("dirty-note").textContent = this._dirty ? "Есть несохранённые изменения" : "";
    this.shadowRoot.getElementById("zone-name").disabled = !zone;
    this.shadowRoot.getElementById("covers-select").disabled = !zone;
  }

  async _saveZones() {
    const option = this._selectedOption();
    if (!option || !this._profile || !this._dirty || this._saving) return;
    if (!this._profile.zones.length) {
      this._setStatus("В профиле должна остаться хотя бы одна зона.", "error");
      return;
    }
    this._saving = true;
    this._updateButtons();
    try {
      const result = await this._hass.callWS({
        type: "dooods/profile_manager/save_zones",
        entry_id: option.entryId,
        camera_entity_id: option.camera.entity_id,
        zones: this._profile.zones,
      });
      if (!result || !Array.isArray(result.zones) || result.zones.length < 1) {
        throw new Error("Сервер не подтвердил сохранение зон.");
      }
      this._profile.zones = result.zones.map((zone) => this._normalizeZone(zone));
      this._dirty = false;
      this._selectedVertex = -1;
      this._setStatus("Зоны сохранены.", "success");
      this._renderZoneList();
      this._renderZoneSettings();
    } catch (error) {
      this._setStatus(`Не удалось сохранить зоны: ${this._errorMessage(error)}`, "error");
    } finally {
      this._saving = false;
      this._updateButtons();
    }
  }

  _previewUrl(option) {
    const params = new URLSearchParams({
      entry_id: option.entryId,
      camera_entity_id: option.camera.entity_id,
      refresh: String(Date.now()),
    });
    const path = `/api/dooods/profile_manager/preview?${params.toString()}`;
    if (this._hass && typeof this._hass.hassUrl === "function") {
      const target = new URL(this._hass.hassUrl(path), window.location.href);
      return `${target.pathname}${target.search}`;
    }
    const pathname = window.location.pathname;
    const panelMarker = `/${PANEL_PATH}`;
    const panelIndex = pathname.indexOf(panelMarker);
    const prefix = panelIndex >= 0 ? pathname.slice(0, panelIndex) : "";
    return `${prefix}${path}`;
  }

  async _loadCameraFrame() {
    const option = this._selectedOption();
    if (!option) return;
    const requestId = ++this._frameRequestId;
    this._frameImageRequestId = 0;
    this._revokeFrameObjectUrl();
    this._frameReady = false;
    this._frameRect = null;
    this._updateButtons();
    this._image.hidden = true;
    this._image.removeAttribute("src");
    this._placeholder.hidden = false;
    this._placeholder.textContent = "Загрузка кадра выбранной камеры…";
    this._overlay.replaceChildren();
    this._overlay.style.width = "0";
    this._overlay.style.height = "0";

    try {
      if (!this._hass || typeof this._hass.fetchWithAuth !== "function") {
        this._showFrameError("Home Assistant не предоставил авторизованный запрос для загрузки кадра. Обновите страницу панели.");
        return;
      }
      const response = await this._hass.fetchWithAuth(this._previewUrl(option), {
        cache: "no-store",
      });
      if (requestId !== this._frameRequestId) return;
      if (!response.ok) {
        const messages = {
          401: "Home Assistant не авторизовал запрос кадра (401). Обновите страницу панели и попробуйте снова.",
          403: "Home Assistant запретил доступ к кадру. Проверьте права текущего пользователя.",
          404: "Маршрут кадра или выбранная камера не найдены. Перезагрузите интеграцию DOOODS.",
          415: "Home Assistant получил кадр в неподдерживаемом формате.",
          502: "Home Assistant не смог получить кадр от камеры. Проверьте сущность камеры и повторите запрос.",
        };
        this._showFrameError(
          messages[response.status] || `Не удалось загрузить кадр (HTTP ${response.status}).`,
        );
        return;
      }
      const blob = await response.blob();
      if (requestId !== this._frameRequestId) return;
      if (!blob.size) {
        this._showFrameError("Home Assistant вернул пустой кадр камеры.");
        return;
      }
      const objectUrl = URL.createObjectURL(blob);
      if (requestId !== this._frameRequestId) {
        URL.revokeObjectURL(objectUrl);
        return;
      }
      this._frameObjectUrl = objectUrl;
      this._frameImageRequestId = requestId;
      this._image.hidden = false;
      this._image.src = objectUrl;
    } catch (_error) {
      if (requestId !== this._frameRequestId) return;
      const errorType =
        _error && typeof _error.name === "string"
          ? _error.name
          : typeof _error === "number"
            ? `код ${_error}`
            : "";
      const detail = errorType ? ` Тип ошибки: ${errorType}.` : "";
      this._showFrameError(`Не удалось выполнить авторизованный запрос кадра в Home Assistant. Обновите страницу панели или повторите попытку.${detail}`);
    }
  }
}

if (!customElements.get("dooods-profile-manager")) {
  customElements.define("dooods-profile-manager", DooodsProfileManager);
}
