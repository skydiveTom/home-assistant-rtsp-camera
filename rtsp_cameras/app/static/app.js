/* RTSP Camera Manager — web interface (vanilla JS, no build step) */
(() => {
  'use strict';

  const STORAGE_KEY = 'rtsp-camera-manager.language';
  const bootstrap = JSON.parse(document.getElementById('bootstrap').textContent || '{}');
  const BASE = String(bootstrap.base_path || '').replace(/\/+$/, '');

  const state = {
    cameras: bootstrap.cameras || [],
    settings: bootstrap.settings || {},
    integration: bootstrap.integration || {},
    i18n: bootstrap.i18n || {},
    languages: bootstrap.languages || ['en'],
    language: 'en',
    editing: null,
    preview: { cameraId: null, mode: 'mjpeg', hls: null, loaded: {} },
  };

  /* ------------------------------------------------------------------ i18n */
  function normalise(code) {
    if (!code) return null;
    const base = String(code).toLowerCase().replace('_', '-').split('-')[0];
    return state.languages.indexOf(base) >= 0 ? base : null;
  }

  function pickLanguage() {
    let stored = null;
    try {
      stored = window.localStorage.getItem(STORAGE_KEY);
    } catch (err) {
      stored = null;
    }
    return (
      normalise(stored) || normalise(bootstrap.language) || normalise(navigator.language) || 'en'
    );
  }

  function lookup(bundle, key) {
    return key.split('.').reduce((node, part) => {
      return node && typeof node === 'object' ? node[part] : undefined;
    }, bundle);
  }

  function format(text, vars) {
    if (!vars) return text;
    return String(text).replace(/\{(\w+)\}/g, (match, name) => {
      return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match;
    });
  }

  function t(key, vars) {
    const bundle = state.i18n[state.language] || {};
    const english = state.i18n.en || {};
    const text = lookup(bundle, key) || lookup(english, key);
    return text ? format(text, vars) : key;
  }

  function errorText(code, vars) {
    const bundle = state.i18n[state.language] || {};
    const english = state.i18n.en || {};
    const text =
      lookup(bundle, 'errors.' + code) || lookup(english, 'errors.' + code);
    return text ? format(text, vars) : t('errors.generic');
  }

  function applyTranslations(root = document) {
    document.documentElement.lang = state.language;
    root.querySelectorAll('[data-i18n]').forEach((node) => {
      node.textContent = t(node.dataset.i18n);
    });
    root.querySelectorAll('[data-i18n-title]').forEach((node) => {
      node.setAttribute('title', t(node.dataset.i18nTitle));
    });
    root.querySelectorAll('[data-i18n-placeholder]').forEach((node) => {
      node.setAttribute('placeholder', t(node.dataset.i18nPlaceholder));
    });
  }

  /* ------------------------------------------------------------------- util */
  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    Object.entries(props).forEach(([key, value]) => {
      if (value === null || value === undefined || value === false) return;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, value);
    });
    [].concat(children).forEach((child) => {
      if (child === null || child === undefined || child === false) return;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return node;
  }

  const ICONS = {
    play: '<path d="M8 5.5v13l11-6.5z"></path>',
    pulse: '<path d="M4 12h4l2-5 4 10 2-5h4"></path>',
    pencil: '<path d="M4 20h4L20 8l-4-4L4 16z"></path>',
    trash: '<path d="M5 7h14M9 7V5h6v2M7 7l1 13h8l1-13"></path>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"></path>',
    camera:
      '<rect x="4" y="8" width="12" height="9" rx="2"></rect><path d="M16 11.5 21 9v7l-5-2.5z"></path>',
  };

  function icon(name) {
    return el('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true', html: ICONS[name] || '' });
  }

  function apiUrl(path) {
    return (BASE ? BASE + '/' : '/') + String(path).replace(/^\/+/, '');
  }

  class ApiError extends Error {
    constructor(code, message, data) {
      super(message || code);
      this.code = code;
      this.data = data || {};
    }
  }

  async function api(path, options = {}) {
    const init = { method: options.method || 'GET', headers: {} };
    if (options.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    let response;
    try {
      response = await fetch(apiUrl(path), init);
    } catch (err) {
      throw new ApiError('network', String(err));
    }
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok || (data && data.ok === false)) {
      const code = (data && data.error) || (response.status === 404 ? 'not_found' : 'generic');
      throw new ApiError(code, data && data.message, data);
    }
    return data || {};
  }

  /* ----------------------------------------------------------------- toasts */
  function toast(message, kind) {
    const node = el('div', { class: 'toast' + (kind ? ' toast--' + kind : ''), text: message });
    const region = document.getElementById('toasts');
    region.append(node);
    window.setTimeout(() => node.remove(), 5200);
  }

  function fail(err) {
    const code = err instanceof ApiError ? err.code : 'generic';
    toast(errorText(code), 'err');
  }

  /* ---------------------------------------------------------------- render */
  function cameraById(cameraId) {
    return state.cameras.find((camera) => camera.id === cameraId) || null;
  }

  function led(kind, live) {
    return el('span', {
      class: 'led' + (kind ? ' led--' + kind : '') + (live ? ' led--live' : ''),
    });
  }

  function button(label, handler, kind) {
    return el('button', {
      type: 'button',
      class: 'btn' + (kind ? ' btn--' + kind : ' btn--ghost'),
      text: label,
      onclick: handler,
    });
  }

  function banner(kind, title, text, actions) {
    return el('div', { class: 'banner' + (kind ? ' banner--' + kind : '') }, [
      el('div', { class: 'banner__text' }, [
        el('h3', { text: title }),
        text ? el('p', { text: text }) : null,
      ]),
      actions.length ? el('div', { class: 'banner__actions' }, actions) : null,
    ]);
  }

  function renderStatus() {
    const strip = document.getElementById('status-strip');
    const cameras = state.cameras;
    const enabled = cameras.filter((camera) => camera.enabled);
    const online = enabled.filter((camera) => camera.status === 'online').length;
    const settings = state.settings;

    strip.replaceChildren(
      el('li', { title: t('camera.status') }, [
        led(enabled.length > 0 && online === enabled.length ? 'ok' : online > 0 ? 'warn' : ''),
        el('span', { text: t('nav.cameras') }),
        el('b', { text: online + ' / ' + enabled.length }),
      ]),
      el('li', {
        title: settings.ffmpeg_available ? t('settings.ffmpeg_ok') : t('settings.ffmpeg_missing'),
      }, [
        led(settings.ffmpeg_available ? 'ok' : 'err'),
        el('span', { text: 'ffmpeg' }),
        el('b', { text: settings.ffmpeg_available ? '✓' : '✕' }),
      ]),
      el('li', { title: settings.supervisor_api ? t('common.yes') : t('common.no') }, [
        led(settings.supervisor_api ? 'ok' : 'warn'),
        el('span', { text: 'Supervisor API' }),
        el('b', { text: settings.supervisor_api ? '✓' : '✕' }),
      ]),
      el('li', { title: t('settings.preview_mode') }, [
        el('span', { text: t('settings.preview_mode') }),
        el('b', { text: settings.preview_mode }),
      ]),
    );
  }

  function renderBanner() {
    const region = document.getElementById('banner-region');
    region.replaceChildren();
    const settings = state.settings;
    const integration = state.integration;

    if (settings.publish_error) {
      region.append(
        banner('err', t('settings.title'), t('settings.cameras_file_error', { path: settings.publish_error }), []),
      );
    }
    if (!settings.install_integration) {
      region.append(banner('warn', t('integration.title'), t('integration.disabled'), []));
      return;
    }
    if (integration.needs_restart) {
      region.append(
        banner('warn', t('integration.title'), t('integration.restart_required'), [
          button(t('integration.restart_now'), () => restartHomeAssistant(), 'primary'),
        ]),
      );
      return;
    }
    if (!integration.installed) {
      region.append(
        banner('', t('integration.title'), t('integration.description'), [
          button(t('integration.install'), (event) => installIntegration(event.currentTarget), 'primary'),
        ]),
      );
      return;
    }
    if (integration.update_available) {
      region.append(
        banner('', t('integration.title'), t('integration.outdated', { version: integration.source_version }), [
          button(t('integration.update'), (event) => installIntegration(event.currentTarget), 'primary'),
        ]),
      );
    }
  }

  function statusKind(camera) {
    if (!camera.enabled) return '';
    if (camera.status === 'online') return 'ok';
    if (camera.status === 'offline') return 'err';
    return 'warn';
  }

  function statusLabel(camera) {
    if (!camera.enabled) return t('camera.disabled_label');
    if (camera.status === 'online') return t('camera.status_online');
    if (camera.status === 'offline') return t('camera.status_offline');
    return t('camera.status_unknown');
  }

  function probeDetails(camera) {
    return (camera.last_probe && camera.last_probe.details) || {};
  }

  function tile(camera, index) {
    const classes = ['tile'];
    if (camera.enabled && camera.status === 'offline') classes.push('tile--offline');
    if (!camera.enabled) classes.push('tile--disabled');

    const details = probeDetails(camera);
    const thumb = el('div', { class: 'thumb', dataset: { cameraId: camera.id } }, [
      el('span', {
        class: 'thumb__empty',
        text: state.settings.ffmpeg_available ? '—' : t('settings.ffmpeg_missing'),
      }),
      el('span', { class: 'chip thumb__badge' }, [
        led(statusKind(camera)),
        el('span', { text: statusLabel(camera) }),
      ]),
      el('button', {
        type: 'button',
        class: 'icon-btn thumb__refresh',
        title: t('common.refresh'),
        onclick: () => loadSnapshot(camera.id, true),
      }, [icon('refresh')]),
    ]);

    const head = el('div', { class: 'tile__head' }, [
      led(statusKind(camera), camera.enabled && camera.status === 'online'),
      el('div', { class: 'tile__headline' }, [
        el('div', { class: 'tile__name', title: camera.name, text: camera.name }),
        el('div', { class: 'tile__entity' }, [
          el('span', { text: camera.entity_id }),
          el('button', {
            type: 'button',
            text: t('camera.copy'),
            onclick: () => copyText(camera.entity_id),
          }),
        ]),
      ]),
    ]);

    const pairs = [
      [t('camera.resolution'), details.resolution || '—'],
      [t('camera.codec'), details.codec || '—'],
      [t('camera.fps'), details.fps !== null && details.fps !== undefined ? String(details.fps) : '—'],
      [t('camera.transport_value'), camera.rtsp_transport],
    ];
    const meta = el(
      'dl',
      { class: 'tile__meta' },
      pairs.flatMap(([label, value]) => [
        el('div', {}, [el('dt', { text: label }), el('dd', { text: value, title: value })]),
      ]),
    );

    const foot = el('div', { class: 'tile__foot' }, [
      el('button', {
        type: 'button',
        class: 'btn btn--primary btn--tiny',
        onclick: () => openPreview(camera.id),
      }, [icon('play'), el('span', { text: t('camera.preview') })]),
      el('button', {
        type: 'button',
        class: 'btn btn--ghost btn--tiny',
        onclick: (event) => testCamera(camera.id, event.currentTarget),
      }, [icon('pulse'), el('span', { text: t('camera.test') })]),
      el('button', {
        type: 'button',
        class: 'btn btn--ghost btn--tiny',
        onclick: () => openCameraModal(camera.id),
      }, [icon('pencil'), el('span', { text: t('camera.edit') })]),
      el('button', {
        type: 'button',
        class: 'btn btn--ghost btn--tiny',
        onclick: () => removeCamera(camera),
      }, [icon('trash'), el('span', { text: t('camera.delete') })]),
    ]);

    const node = el('article', { class: classes.join(' '), dataset: { cameraId: camera.id } }, [
      head,
      thumb,
      meta,
      foot,
    ]);
    node.style.setProperty('--i', String(index));
    return node;
  }

  function renderCameras() {
    const list = document.getElementById('camera-list');
    const empty = document.getElementById('camera-empty');
    list.replaceChildren();
    state.cameras.forEach((camera, index) => list.append(tile(camera, index)));
    empty.hidden = state.cameras.length > 0;
    document.getElementById('tab-camera-count').textContent = String(state.cameras.length);
    observeThumbnails();
  }

  /* ----------------------------------------------- lazy snapshot thumbnails */
  let observer = null;
  const snapshotQueue = [];
  let snapshotsRunning = 0;

  function observeThumbnails() {
    const thumbs = document.querySelectorAll('.thumb[data-camera-id]');
    if (!('IntersectionObserver' in window)) {
      thumbs.forEach((node) => queueSnapshot(node.dataset.cameraId));
      return;
    }
    if (observer) observer.disconnect();
    observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        observer.unobserve(entry.target);
        queueSnapshot(entry.target.dataset.cameraId);
      });
    }, { rootMargin: '140px' });
    thumbs.forEach((node) => observer.observe(node));
  }

  function queueSnapshot(cameraId) {
    if (!cameraId || snapshotQueue.includes(cameraId)) return;
    snapshotQueue.push(cameraId);
    drainSnapshots();
  }

  function drainSnapshots() {
    if (snapshotsRunning >= 2 || snapshotQueue.length === 0) return;
    const cameraId = snapshotQueue.shift();
    snapshotsRunning += 1;
    loadSnapshot(cameraId).finally(() => {
      snapshotsRunning -= 1;
      drainSnapshots();
    });
  }

  function loadSnapshot(cameraId, force) {
    const camera = cameraById(cameraId);
    const thumb = document.querySelector('.thumb[data-camera-id="' + cameraId + '"]');
    if (!camera || !thumb || !camera.enabled || !state.settings.ffmpeg_available) {
      return Promise.resolve();
    }
    if (force) thumb.querySelector('img')?.remove();
    else if (thumb.querySelector('img')) return Promise.resolve();

    return new Promise((resolve) => {
      const image = new Image();
      image.alt = '';
      image.onload = () => {
        const placeholder = thumb.querySelector('.thumb__empty');
        if (placeholder) placeholder.hidden = true;
        thumb.prepend(image);
        resolve();
      };
      image.onerror = () => resolve();
      image.src = apiUrl(
        'api/cameras/' + cameraId + '/snapshot.jpg?height=360&t=' + Date.now(),
      );
    });
  }

  function renderSettings() {
    const list = document.getElementById('settings-list');
    const settings = state.settings;
    const integration = state.integration;
    const health =
      settings.health_check_interval > 0
        ? t('settings.health_seconds', { seconds: settings.health_check_interval })
        : t('settings.health_disabled');
    const version = integration.installed
      ? t('integration.installed_version', { version: integration.version })
      : t('integration.not_installed');

    const pairs = [
      [t('app.title'), settings.version],
      [t('settings.data_file'), settings.cameras_file],
      [t('settings.published_file'), settings.published_file],
      [t('integration.title'), version],
      [t('settings.preview_mode'), t('preview.mode_' + settings.preview_mode)],
      [t('settings.health_interval'), health],
      ['ffmpeg', settings.ffmpeg_available ? t('settings.ffmpeg_ok') : t('settings.ffmpeg_missing')],
      ['Supervisor API', settings.supervisor_api ? t('common.yes') : t('common.no')],
    ];
    list.replaceChildren(
      ...pairs.flatMap(([label, value]) => [el('dt', { text: label }), el('dd', { text: value })]),
    );
  }

  function renderAll() {
    applyTranslations();
    renderStatus();
    renderBanner();
    renderCameras();
    renderSettings();
  }

  /* ------------------------------------------------------------------ modal */
  function showModal(id) {
    document.getElementById(id).hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function hideModal(id) {
    const modal = document.getElementById(id);
    if (!modal) return;
    modal.hidden = true;
    if (id === 'preview-modal') stopPreview();
    const open = Array.from(document.querySelectorAll('.modal')).some((node) => !node.hidden);
    if (!open) document.body.style.overflow = '';
  }

  function renderProbe(container, probe) {
    container.hidden = false;
    container.className = 'probe ' + (probe.ok ? 'probe--ok' : 'probe--err');
    const children = [
      el('div', { class: 'probe__head' }, [
        led(probe.ok ? 'ok' : 'err'),
        el('span', { text: probe.ok ? t('camera.test_ok') : t('camera.test_failed') }),
      ]),
    ];

    if (probe.ok) {
      const details = probe.details || {};
      const pairs = [
        [t('camera.resolution'), details.resolution || '—'],
        [t('camera.codec'), details.codec || '—'],
        [t('camera.fps'), details.fps !== null && details.fps !== undefined ? String(details.fps) : '—'],
        [
          t('camera.bitrate'),
          details.bit_rate_kbps ? t('camera.bitrate_value', { value: details.bit_rate_kbps }) : '—',
        ],
        [t('camera.format'), details.format || '—'],
        [t('camera.transport_value'), details.transport || '—'],
      ];
      children.push(
        el(
          'dl',
          { class: 'probe__grid' },
          pairs.flatMap(([label, value]) => [el('dt', { text: label }), el('dd', { text: value })]),
        ),
      );
    } else if (probe.error) {
      children.push(el('p', { class: 'probe__error', text: probe.error }));
    }
    container.replaceChildren(...children);
  }

  async function reload() {
    const data = await api('api/meta');
    if (data.cameras) state.cameras = data.cameras;
    if (data.settings) state.settings = data.settings;
    if (data.integration) state.integration = data.integration;
    renderAll();
  }

  function openCameraModal(cameraId) {
    const camera = cameraId ? cameraById(cameraId) : null;
    state.editing = camera ? camera.id : null;
    document.getElementById('camera-modal-title').textContent = camera ? t('camera.edit') : t('camera.add');
    document.getElementById('field-name').value = camera ? camera.name : '';
    document.getElementById('field-url').value = camera ? camera.url : '';
    document.getElementById('field-transport').value = camera
      ? camera.rtsp_transport
      : state.settings.default_rtsp_transport || 'tcp';
    document.getElementById('field-enabled').checked = camera ? camera.enabled : true;
    document.getElementById('btn-save').textContent = camera ? t('camera.save_changes') : t('camera.save');
    document.getElementById('btn-preview-form').hidden = !camera;
    const probeBox = document.getElementById('probe-result');
    probeBox.hidden = true;
    probeBox.replaceChildren();
    showModal('camera-modal');
    document.getElementById('field-name').focus();
  }

  async function submitCamera(event) {
    event.preventDefault();
    const name = document.getElementById('field-name').value.trim();
    const url = document.getElementById('field-url').value.trim();
    const rtsp_transport = document.getElementById('field-transport').value;
    const enabled = document.getElementById('field-enabled').checked;

    if (!name) {
      toast(errorText('name_required'), 'err');
      return;
    }
    if (!url) {
      toast(errorText('url_required'), 'err');
      return;
    }

    const saveButton = document.getElementById('btn-save');
    saveButton.disabled = true;
    try {
      const body = { name, url, rtsp_transport, enabled };
      const data = state.editing
        ? await api('api/cameras/' + state.editing, { method: 'PUT', body })
        : await api('api/cameras', { method: 'POST', body });
      if (data.cameras) state.cameras = data.cameras;
      else await reload();
      hideModal('camera-modal');
      renderAll();
      toast(t('common.saved'), 'ok');
      if (data.publish_error) {
        toast(t('settings.cameras_file_error', { path: data.publish_error }), 'err');
      }
    } catch (err) {
      fail(err);
    } finally {
      saveButton.disabled = false;
    }
  }

  async function testForm(showPreview) {
    const url = document.getElementById('field-url').value.trim();
    const transport = document.getElementById('field-transport').value;
    const button = document.getElementById('btn-test-form');
    const probeBox = document.getElementById('probe-result');

    if (!url) {
      toast(errorText('url_required'), 'err');
      return;
    }
    button.disabled = true;
    probeBox.hidden = false;
    probeBox.className = 'probe';
    probeBox.replaceChildren(el('p', { class: 'probe__head', text: t('camera.testing') }));

    try {
      const camera = state.editing ? cameraById(state.editing) : null;
      const useSaved = Boolean(camera) && camera.url === url && camera.rtsp_transport === transport;
      const data = useSaved
        ? await api('api/cameras/' + camera.id + '/test', { method: 'POST' })
        : await api('api/probe', { method: 'POST', body: { url, rtsp_transport: transport } });

      renderProbe(probeBox, data.probe || {});
      if (data.camera && camera) {
        camera.status = data.camera.status;
        camera.last_probe = data.camera.last_probe;
        camera.last_checked = data.camera.last_checked;
        camera.last_error = data.camera.last_error;
        renderStatus();
        renderCameras();
      }
      if (showPreview && data.probe && data.probe.ok && useSaved) {
        openPreview(camera.id);
      }
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'generic';
      renderProbe(probeBox, { ok: false, error: errorText(code) });
    } finally {
      button.disabled = false;
    }
  }

  async function testCamera(cameraId, node) {
    const camera = cameraById(cameraId);
    if (!camera) return;
    if (node) node.disabled = true;
    try {
      const data = await api('api/cameras/' + cameraId + '/test', { method: 'POST' });
      if (data.camera) {
        camera.status = data.camera.status;
        camera.last_probe = data.camera.last_probe;
        camera.last_checked = data.camera.last_checked;
        camera.last_error = data.camera.last_error;
      }
      renderStatus();
      renderCameras();
      const probe = data.probe || {};
      toast(probe.ok ? t('camera.test_ok') : t('camera.test_failed'), probe.ok ? 'ok' : 'err');
      if (!probe.ok && probe.error) toast(probe.error, 'err');
    } catch (err) {
      fail(err);
    } finally {
      if (node) node.disabled = false;
    }
  }

  async function removeCamera(camera) {
    if (!window.confirm(t('camera.delete_confirm', { name: camera.name }))) return;
    try {
      const data = await api('api/cameras/' + camera.id, { method: 'DELETE' });
      if (data.cameras) state.cameras = data.cameras;
      else await reload();
      renderAll();
      toast(t('common.deleted'), 'ok');
    } catch (err) {
      fail(err);
    }
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast(t('camera.copied'), 'ok');
    } catch (err) {
      window.prompt(t('camera.copy'), text);
    }
  }

  /* ---------------------------------------------------------------- preview */
  function openPreview(cameraId) {
    const camera = cameraById(cameraId);
    if (!camera) return;
    state.preview.cameraId = cameraId;
    document.getElementById('preview-title').textContent = t('preview.title', { name: camera.name });
    document.getElementById('preview-mode-select').value =
      state.preview.mode || state.settings.preview_mode || 'mjpeg';
    showModal('preview-modal');
    startPreview();
  }

  function resetStage() {
    if (state.preview.hls) {
      try {
        state.preview.hls.destroy();
      } catch (err) {
        /* ignore */
      }
      state.preview.hls = null;
    }
    const image = document.getElementById('preview-image');
    image.hidden = true;
    image.removeAttribute('src');
    const video = document.getElementById('preview-video');
    try {
      video.pause();
    } catch (err) {
      /* ignore */
    }
    video.hidden = true;
    video.removeAttribute('src');
    document.getElementById('preview-placeholder').hidden = false;
    document.getElementById('preview-error').hidden = true;
  }

  function stopPreview() {
    state.preview.cameraId = null;
    resetStage();
  }

  function previewError(message) {
    document.getElementById('preview-placeholder').hidden = true;
    const box = document.getElementById('preview-error');
    box.hidden = false;
    box.textContent = message;
  }

  function startPreview() {
    const cameraId = state.preview.cameraId;
    if (!cameraId) return;
    const mode = document.getElementById('preview-mode-select').value || 'mjpeg';
    state.preview.mode = mode;
    document.getElementById('preview-chip').textContent = mode;
    resetStage();

    const image = document.getElementById('preview-image');
    if (mode === 'hls') {
      startHlsPreview(cameraId);
      return;
    }
    image.onload = () => {
      document.getElementById('preview-placeholder').hidden = true;
      image.hidden = false;
    };
    image.onerror = () => previewError(errorText('stream_failed'));
    image.src = apiUrl('api/cameras/' + cameraId + '/mjpeg?t=' + Date.now());
  }

  function loadHlsLibrary() {
    if (window.Hls) return Promise.resolve(true);
    if (state.preview.libraryFailed) return Promise.resolve(false);
    return new Promise((resolve) => {
      const script = document.createElement('script');
      script.src = apiUrl('static/hls.min.js');
      script.onload = () => resolve(true);
      script.onerror = () => {
        state.preview.libraryFailed = true;
        resolve(false);
      };
      document.head.append(script);
    });
  }

  async function startHlsPreview(cameraId) {
    const video = document.getElementById('preview-video');
    try {
      const data = await api('api/cameras/' + cameraId + '/hls/start', { method: 'POST' });
      const url = apiUrl(data.playlist || 'api/cameras/' + cameraId + '/hls/index.m3u8');
      const hasLibrary = await loadHlsLibrary();

      if (hasLibrary && window.Hls && window.Hls.isSupported()) {
        const hls = new window.Hls({ liveDurationInfinity: true, lowLatencyMode: true });
        state.preview.hls = hls;
        hls.on(window.Hls.Events.ERROR, (event, payload) => {
          if (payload && payload.fatal) previewError(errorText('stream_failed'));
        });
        hls.loadSource(url);
        hls.attachMedia(video);
        hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
          document.getElementById('preview-placeholder').hidden = true;
          video.hidden = false;
          video.play().catch(() => {});
        });
        return;
      }
      if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = url;
        video.hidden = false;
        video.onloadeddata = () => {
          document.getElementById('preview-placeholder').hidden = true;
        };
        video.play().catch(() => {});
        return;
      }
      previewError(errorText('stream_failed'));
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'generic';
      const detail = err instanceof ApiError && err.data && err.data.detail ? ' — ' + err.data.detail : '';
      previewError(errorText(code) + detail);
    }
  }

  /* ----------------------------------------------------- HA integration ops */
  async function installIntegration(node) {
    if (node) node.disabled = true;
    try {
      const data = await api('api/integration/install', { method: 'POST', body: {} });
      if (data.integration) state.integration = data.integration;
      renderBanner();
      renderSettings();
      toast(t('integration.install'), 'ok');
      if (data.restart_requested) toast(t('integration.restarted'), 'ok');
    } catch (err) {
      fail(err);
    } finally {
      if (node) node.disabled = false;
    }
  }

  async function restartHomeAssistant(node) {
    if (node) node.disabled = true;
    try {
      await api('api/ha/restart', { method: 'POST' });
      state.integration = Object.assign({}, state.integration, { needs_restart: false });
      renderBanner();
      toast(t('integration.restarted'), 'ok');
    } catch (err) {
      fail(err);
    } finally {
      if (node) node.disabled = false;
    }
  }

  /* ------------------------------------------------------------------ chrome */
  function selectPanel(name) {
    document.querySelectorAll('.tab').forEach((tab) => {
      const active = tab.dataset.panel === name;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    document.querySelectorAll('.panel').forEach((panel) => {
      const active = panel.id === 'panel-' + name;
      panel.classList.toggle('is-active', active);
      panel.hidden = !active;
    });
  }

  function buildLanguageSelect() {
    const select = document.getElementById('language-select');
    const labels = { en: 'English', de: 'Deutsch', es: 'Español', pl: 'Polski' };
    select.replaceChildren(
      ...state.languages.map((code) =>
        el('option', { value: code, text: labels[code] || code.toUpperCase() }),
      ),
    );
    select.value = state.language;
    select.addEventListener('change', () => {
      state.language = normalise(select.value) || 'en';
      try {
        window.localStorage.setItem(STORAGE_KEY, state.language);
      } catch (err) {
        /* ignore */
      }
      renderAll();
      syncRevealButton();
    });
  }

  function syncRevealButton() {
    const input = document.getElementById('field-url');
    const button = document.getElementById('btn-reveal');
    const hidden = input.type === 'password';
    button.title = hidden ? t('camera.reveal') : t('camera.hide');
    const label = button.querySelector('span');
    if (label) label.textContent = hidden ? t('camera.reveal') : t('camera.hide');
  }

  /* ------------------------------------------------------- quiet status poll */
  function structuralKey(camera) {
    return [camera.id, camera.name, camera.url, camera.rtsp_transport, camera.enabled].join('|');
  }

  function updateTileStatus(camera) {
    const node = document.querySelector('.tile[data-camera-id="' + camera.id + '"]');
    if (!node) return;

    const kind = statusKind(camera);
    node.className =
      'tile' +
      (camera.enabled && camera.status === 'offline' ? ' tile--offline' : '') +
      (camera.enabled ? '' : ' tile--disabled');

    const indicator = node.querySelector('.tile__head .led');
    if (indicator) {
      indicator.className =
        'led' + (kind ? ' led--' + kind : '') + (camera.enabled && camera.status === 'online' ? ' led--live' : '');
    }

    const badge = node.querySelector('.thumb__badge');
    if (badge) badge.replaceChildren(led(kind), el('span', { text: statusLabel(camera) }));

    const details = probeDetails(camera);
    const values = [
      details.resolution || '—',
      details.codec || '—',
      details.fps !== null && details.fps !== undefined ? String(details.fps) : '—',
      camera.rtsp_transport,
    ];
    node.querySelectorAll('.tile__meta dd').forEach((cell, index) => {
      const value = values[index];
      if (value === undefined || cell.textContent === value) return;
      cell.textContent = value;
      cell.title = value;
    });
  }

  async function refreshStatuses() {
    try {
      const data = await api('api/meta');
      const incoming = data.cameras || [];
      const changed =
        incoming.map(structuralKey).join(',') !== state.cameras.map(structuralKey).join(',');
      state.cameras = incoming;
      if (data.settings) state.settings = data.settings;
      if (data.integration) state.integration = data.integration;

      if (changed) {
        renderAll();
        return;
      }
      renderStatus();
      renderBanner();
      state.cameras.forEach(updateTileStatus);
    } catch (err) {
      /* stay quiet, the next tick tries again */
    }
  }

  /* -------------------------------------------------------------------- init */
  function wireEvents() {
    document.getElementById('btn-add-camera').addEventListener('click', () => openCameraModal(null));
    document.getElementById('btn-reload').addEventListener('click', () => {
      reload().catch(fail);
    });
    document.getElementById('camera-form').addEventListener('submit', submitCamera);
    document.getElementById('btn-test-form').addEventListener('click', () => testForm(false));
    document.getElementById('btn-preview-form').addEventListener('click', () => {
      const camera = state.editing ? cameraById(state.editing) : null;
      if (!camera) {
        toast(errorText('url_required'), 'err');
        return;
      }
      hideModal('camera-modal');
      openPreview(camera.id);
    });
    document.getElementById('btn-reveal').addEventListener('click', () => {
      const input = document.getElementById('field-url');
      input.type = input.type === 'password' ? 'text' : 'password';
      syncRevealButton();
    });
    document.getElementById('preview-mode-select').addEventListener('change', startPreview);
    document.querySelectorAll('.tab').forEach((tab) => {
      tab.addEventListener('click', () => selectPanel(tab.dataset.panel));
    });
    document.addEventListener('click', (event) => {
      const closer = event.target.closest('[data-close]');
      if (closer) hideModal(closer.dataset.close);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      Array.from(document.querySelectorAll('.modal')).forEach((modal) => {
        if (!modal.hidden) hideModal(modal.id);
      });
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refreshStatuses();
    });
  }

  function init() {
    state.language = pickLanguage();
    buildLanguageSelect();
    wireEvents();
    applyTranslations();
    renderAll();
    selectPanel('cameras');
    syncRevealButton();
    window.setInterval(() => {
      if (document.hidden) return;
      refreshStatuses();
    }, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
