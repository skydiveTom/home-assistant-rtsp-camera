/* RTSP Camera Manager — web interface (vanilla JS, no build step) */
(() => {
  'use strict';

  const STORAGE_KEY = 'rtsp-camera-manager.language';
  const MJPEG_FLAG_KEY = 'rtsp-camera-manager.mjpeg-blocked';
  /* The note "MJPEG does not arrive here" is a hint for the next try, not a fact:
     one slow first frame (a camera whose stream has to be decoded, a busy Wi-Fi
     link) must not push this browser to HLS forever, so the note expires. */
  const MJPEG_FLAG_TTL_MS = 10 * 60 * 1000;
  const bootstrap = JSON.parse(document.getElementById('bootstrap').textContent || '{}');
  const BASE = String(bootstrap.base_path || '').replace(/\/+$/, '');
  /* How long the browser waits for the first MJPEG frame before it assumes the
     frames never make it through the reverse proxy and switches to HLS. The
     add-on itself waits for that first frame before it answers and gives the
     stream its own timeout, so the browser has to wait at least as long -
     otherwise a slow camera is declared blocked while its frame is on the way. */
  const previewTimeoutSeconds =
    Number(bootstrap.settings && bootstrap.settings.test_timeout) || 15;
  const PREVIEW_FIRST_FRAME_MS = Math.max(8000, previewTimeoutSeconds * 1000 + 2000);

  const state = {
    cameras: bootstrap.cameras || [],
    settings: bootstrap.settings || {},
    integration: bootstrap.integration || {},
    i18n: bootstrap.i18n || {},
    languages: bootstrap.languages || ['en'],
    language: 'en',
    editing: null,
    addonUpdate: { available: false, update_available: false, busy: false },
    preview: {
      cameraId: null,
      mode: 'auto',
      hls: null,
      loaded: {},
      resolved: {},
      mjpegBlockedAt: 0,
      timer: null,
    },
    ptzProfiles: [],
    ptzActions: ['up', 'down', 'left', 'right', 'zoom_in', 'zoom_out', 'home', 'stop', 'preset', 'store'],
    ptzDirection: null,
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

  /* The integration Home Assistant loads has its own version - showing it next to
     the add-on version makes an outdated copy visible at a glance. */
  function integrationOutdated() {
    const integration = state.integration || {};
    if (!state.settings.install_integration) return false;
    if (!integration.installed) return true;
    return Boolean(integration.update_available || integration.needs_restart);
  }

  function integrationVersionLabel() {
    const integration = state.integration || {};
    if (!state.settings.install_integration) return String(integration.version || '—');
    if (!integration.installed) return t('integration.missing');
    const installed = integration.version || '—';
    if (integration.update_available && integration.source_version) {
      return installed + ' → ' + integration.source_version;
    }
    if (integration.needs_restart) return t('integration.restart_short', { version: installed });
    return installed;
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
      el('li', {
        title: t('addon_update.check'),
        class: 'statusstrip__action' + (state.addonUpdate.update_available ? ' is-alert' : ''),
        onclick: (event) => checkAddonUpdate(event.currentTarget),
      }, [
        led(state.addonUpdate.update_available ? 'warn' : 'ok'),
        el('span', { text: t('addon_update.short') }),
        el('b', {
          text: state.addonUpdate.update_available
            ? t('addon_update.new_version', { version: state.addonUpdate.version_latest || '?' })
            : String(settings.version || '?'),
        }),
      ]),
      /* The integration inside Home Assistant has its own version: showing both next
         to each other makes an outdated copy visible without opening the settings. */
      el('li', {
        title: t('integration.version'),
        class: 'statusstrip__action' + (integrationOutdated() ? ' is-alert' : ''),
        onclick: () => selectPanel('settings'),
      }, [
        led(integrationOutdated() ? 'warn' : 'ok'),
        el('span', { text: t('integration.short') }),
        el('b', { text: integrationVersionLabel() }),
      ]),
    );
  }

  function renderBanner() {
    const region = document.getElementById('banner-region');
    region.replaceChildren();
    const settings = state.settings;
    const integration = state.integration;
    const addon = state.addonUpdate;

    if (addon.update_available && !addon.busy && addon.can_install) {
      region.append(
        banner(
          '',
          t('addon_update.title'),
          t('addon_update.available', { version: addon.version_latest || '?' }),
          [button(t('addon_update.update'), (event) => installAddonUpdate(event.currentTarget), 'primary')],
        ),
      );
    }
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

  /* Home Assistant camera cards play H.265 in very few browsers, so it is worth
     pointing that out next to the codec. */
  function isH265(codec) {
    const value = String(codec || '').toLowerCase();
    return value === 'hevc' || value === 'h265';
  }

  function applyCamera(updated) {
    if (!updated || !updated.id) return;
    const camera = cameraById(updated.id);
    if (!camera) return;
    Object.assign(camera, updated);
    renderCameras();
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
    if (camera.preview_mode) {
      pairs.push([t('camera.preview_mode'), camera.preview_mode]);
    }
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

    const notes = [];
    if (camera.enabled && isH265(details.codec)) {
      notes.push(el('p', {
        class: 'note note--tight note--warn',
        text: t('camera.ha_codec_warning', { codec: details.codec }),
      }));
    }

    const node = el('article', { class: classes.join(' '), dataset: { cameraId: camera.id } }, [
      head,
      thumb,
      meta,
      ...notes,
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
    renderCameras();
    renderSettings();
    renderAddonState();
  }

  /* The version chip, the status strip and the banner all show update state. */
  function renderAddonState() {
    renderAddonUpdate();
    renderStatus();
    renderBanner();
  }

  function renderAddonUpdate() {
    const box = document.getElementById('addon-update-box');
    const chip = document.getElementById('btn-addon-version');
    const info = state.addonUpdate;
    const current = state.settings.version || '?';
    if (chip) {
      chip.textContent = info.update_available ? 'v' + current + ' ↑' : 'v' + current;
      chip.classList.toggle('chip--alert', Boolean(info.update_available));
    }
    if (!box) return;
    const children = [
      el('h3', { text: t('addon_update.title') }),
      el('p', {
        class: 'note note--tight',
        text: t('addon_update.installed', { version: current }),
      }),
    ];

    if (!info.available) {
      children.push(
        el('p', {
          class: 'note note--tight note--warn',
          text: info.hint === 'token_missing'
            ? t('addon_update.token_missing')
            : info.error
              ? t('addon_update.failed', { reason: info.error })
              : t('addon_update.no_supervisor'),
        }),
      );
      if (info.version_latest) {
        children.push(
          el('p', {
            class: 'note note--tight note--warn',
            text: t('addon_update.available', { version: info.version_latest }),
          }),
        );
      }
      if (info.checked_at) {
        children.push(
          el('p', {
            class: 'note note--tight',
            text: t('addon_update.checked_by_ha', { time: info.checked_at }),
          }),
        );
      }
    } else if (info.busy) {
      children.push(el('p', { class: 'note note--tight', text: t('addon_update.updating') }));
    } else if (info.update_available) {
      children.push(
        el('p', {
          class: 'note note--tight note--warn',
          text: t('addon_update.available', { version: info.version_latest || '?' }),
        }),
      );
    } else if (info.checked_at) {
      children.push(el('p', { class: 'note note--tight', text: t('addon_update.up_to_date') }));
    }

    const actions = el('div', { class: 'addon-update__actions' }, [
      button(
        t('addon_update.check'),
        (event) => checkAddonUpdate(event.currentTarget),
        'ghost',
      ),
    ]);
    if (info.available && info.update_available && !info.busy) {
      if (info.can_install || info.via_home_assistant) {
        actions.append(
          button(
            t('addon_update.update'),
            (event) => installAddonUpdate(event.currentTarget),
            'primary',
          ),
        );
      } else {
        actions.append(
          el('p', { class: 'note note--tight', text: t('addon_update.update_in_ha') }),
        );
      }
    }
    children.push(actions);
    box.replaceChildren(...children);
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

  function renderProbe(container, probe, hint) {
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
      /* A stream test can be green while the transport delivers nothing usable
         (UDP without packets): say so, otherwise the black preview looks like a
         bug in the add-on. */
      const note = hint ? t('camera.hint_' + hint) : '';
      if (note && note !== 'camera.hint_' + hint) {
        children.push(el('p', { class: 'probe__hint', text: note }));
      }
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
    document.getElementById('field-ha-stream-url').value = camera && camera.ha_stream_url
      ? camera.ha_stream_url
      : '';
    document.getElementById('field-enabled').checked = camera ? camera.enabled : true;
    document.getElementById('btn-save').textContent = camera ? t('camera.save_changes') : t('camera.save');
    document.getElementById('btn-preview-form').hidden = !camera;
    renderPtzForm(camera);
    resetEncoderBox();
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
    const ha_stream_url = document.getElementById('field-ha-stream-url').value.trim();

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
      const body = { name, url, rtsp_transport, enabled, ha_stream_url, ptz: collectPtz() };
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

      renderProbe(probeBox, data.probe || {}, data.hint);
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
      if (probe.ok && data.hint) toast(t('camera.hint_' + data.hint), 'warn');
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

  /* -------------------------------------------------------------------- PTZ */
  async function loadPtzProfiles() {
    if (state.ptzProfiles.length) return state.ptzProfiles;
    try {
      const data = await api('api/ptz/profiles');
      state.ptzProfiles = data.profiles || [];
    } catch (err) {
      state.ptzProfiles = [];
    }
    return state.ptzProfiles;
  }

  function ptzProfile() {
    const id = document.getElementById('field-ptz-profile').value;
    return state.ptzProfiles.find((profile) => profile.id === id) || null;
  }

  function ptzCommandNodes() {
    return Array.from(document.querySelectorAll('#ptz-commands input'));
  }

  function renderPtzCommands(commands) {
    const box = document.getElementById('ptz-commands');
    const rows = state.ptzActions.map((action) => {
      const input = el('input', {
        type: 'text',
        id: 'field-ptz-' + action,
        autocomplete: 'off',
        spellcheck: 'false',
        placeholder: t('ptz.command_hint'),
      });
      input.dataset.ptzAction = action;
      input.value = (commands && commands[action]) || '';
      return el('label', {}, [el('span', { text: t('ptz.action_' + action) }), input]);
    });
    box.replaceChildren(el('p', { class: 'field__label', text: t('ptz.commands') }), ...rows);
  }

  function fillPtzFromProfile(quiet) {
    const profile = ptzProfile();
    if (!profile) return;
    const templates = profile.commands || {};
    /* A profile that defines no command at all (the custom one) cannot fill anything
       in, so it leaves the fields alone instead of throwing away a command that was
       written by hand. */
    const replacesEverything = Object.keys(templates).length > 0;
    const base = document.getElementById('field-ptz-base').value.trim();
    const values = {
      base: base || 'http://' + t('ptz.your_camera'),
      username: encodeURIComponent(document.getElementById('field-ptz-username').value.trim()),
      password: encodeURIComponent(document.getElementById('field-ptz-password').value),
      channel: document.getElementById('field-ptz-channel').value || '1',
      port: document.getElementById('field-ptz-port').value || '34567',
      token: document.getElementById('field-ptz-token').value.trim(),
    };
    ptzCommandNodes().forEach((input) => {
      const template = templates[input.dataset.ptzAction];
      if (!template) {
        /* An action this profile does not have: it is cleared, so that no command of
           the previously selected vendor survives in the form. */
        if (replacesEverything) input.value = '';
        return;
      }
      input.value = template.replace(/\{(\w+)\}/g, (match, name) =>
        Object.prototype.hasOwnProperty.call(values, name) ? values[name] : match,
      );
    });
    document.getElementById('ptz-profile-hint').textContent = profile.description || '';
    if (!quiet) toast(t('ptz.filled'), 'ok');
  }

  function presetLines(presets) {
    return (presets || []).map((preset) => preset.id + '=' + preset.name).join('\n');
  }

  function parsePresets(text) {
    return String(text || '')
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const index = line.indexOf('=');
        return index < 0
          ? { id: line, name: line }
          : { id: line.slice(0, index).trim(), name: line.slice(index + 1).trim() };
      })
      .filter((preset) => preset.id);
  }
  async function renderPtzForm(camera) {
    await loadPtzProfiles();
    const ptz = (camera && camera.ptz) || null;
    const select = document.getElementById('field-ptz-profile');
    select.replaceChildren(
      ...state.ptzProfiles.map((profile) => el('option', { value: profile.id, text: profile.label })),
    );
    select.value = (ptz && ptz.profile) || 'custom';
    document.getElementById('field-ptz-enabled').checked = Boolean(ptz && ptz.enabled);
    document.getElementById('field-ptz-base').value = (ptz && ptz.base_url) || '';
    document.getElementById('field-ptz-channel').value = (ptz && ptz.channel) || 1;
    document.getElementById('field-ptz-speed').value = (ptz && ptz.speed) || 4;
    document.getElementById('field-ptz-speed-vertical').value =
      (ptz && (ptz.speed_vertical || ptz.speed)) || 4;
    document.getElementById('field-ptz-speed-horizontal').value =
      (ptz && (ptz.speed_horizontal || ptz.speed)) || 4;
    document.getElementById('field-ptz-port').value = (ptz && ptz.port) || 34567;
    document.getElementById('field-ptz-token').value = (ptz && ptz.token) || '';
    document.getElementById('field-ptz-username').value = '';
    document.getElementById('field-ptz-password').value = '';
    document.getElementById('field-ptz-presets').value = presetLines(ptz && ptz.presets);
    renderPtzCommands(ptz && ptz.commands);
    document.getElementById('ptz-profile-hint').textContent = (ptzProfile() || {}).description || '';
    document.getElementById('ptz-state').textContent = ptz && ptz.enabled ? t('ptz.on') : t('ptz.off');
    document.getElementById('btn-ptz-test').hidden = !camera;
    /* The test mode keeps the address of the camera, not the result of the last run,
       so it is ready for the next camera as well. */
    document.getElementById('field-ptz-test-ip').value =
      (ptz && ptz.base_url) || hostFromUrl(camera && camera.url) || '';
    document.getElementById('field-ptz-test-port').value = (ptz && ptz.port) || '';
    const detectResult = document.getElementById('ptz-detect-result');
    detectResult.hidden = true;
    detectResult.replaceChildren();
    const result = document.getElementById('ptz-result');
    result.hidden = true;
    result.replaceChildren();
  }

  function collectPtz() {
    if (!document.getElementById('field-ptz-enabled').checked) return { enabled: false };
    const commands = {};
    ptzCommandNodes().forEach((input) => {
      const value = input.value.trim();
      if (value) commands[input.dataset.ptzAction] = value;
    });
    return {
      enabled: true,
      profile: document.getElementById('field-ptz-profile').value,
      base_url: document.getElementById('field-ptz-base').value.trim(),
      channel: Number(document.getElementById('field-ptz-channel').value) || 1,
      speed: Number(document.getElementById('field-ptz-speed').value) || 4,
      speed_vertical: Number(document.getElementById('field-ptz-speed-vertical').value) || 4,
      speed_horizontal:
        Number(document.getElementById('field-ptz-speed-horizontal').value) || 4,
      port: Number(document.getElementById('field-ptz-port').value) || 34567,
      token: document.getElementById('field-ptz-token').value.trim(),
      username: document.getElementById('field-ptz-username').value.trim(),
      password: document.getElementById('field-ptz-password').value,
      commands,
      presets: parsePresets(document.getElementById('field-ptz-presets').value),
    };
  }

  function errorDetail(err) {
    const detail = err instanceof ApiError && err.data ? err.data.detail : null;
    return detail ? ' — ' + detail : '';
  }

  async function discoverOnvif() {
    const box = document.getElementById('ptz-result');
    const button = document.getElementById('btn-ptz-onvif');
    const base = document.getElementById('field-ptz-base').value.trim();
    if (!base) {
      toast(errorText('ptz_base_url_required'), 'err');
      return;
    }
    button.disabled = true;
    box.hidden = false;
    box.className = 'probe';
    box.replaceChildren(el('p', { class: 'probe__head', text: t('ptz.discovering') }));
    try {
      const data = await api('api/ptz/onvif/discover', {
        method: 'POST',
        body: {
          base_url: base,
          username: document.getElementById('field-ptz-username').value.trim(),
          password: document.getElementById('field-ptz-password').value,
        },
      });
      document.getElementById('field-ptz-token').value = data.token || '';
      box.className = 'probe probe--ok';
      box.replaceChildren(
        el('p', { class: 'probe__head', text: t('ptz.discovered', { token: data.token }) }),
        el('p', { class: 'probe__hint', text: t('ptz.discovered_hint') }),
      );
      /* The commands were rendered without a token, so they are filled again. */
      fillPtzFromProfile();
    } catch (err) {
      box.className = 'probe probe--err';
      const code = err instanceof ApiError ? err.code : 'ptz_onvif_failed';
      box.replaceChildren(el('p', { class: 'probe__error', text: errorText(code) + errorDetail(err) }));
    } finally {
      button.disabled = false;
    }
  }

  async function testPtz() {
    const camera = state.editing ? cameraById(state.editing) : null;
    const box = document.getElementById('ptz-result');
    const button = document.getElementById('btn-ptz-test');
    if (!camera) {
      toast(t('camera.preview_save_first'), 'err');
      return;
    }
    button.disabled = true;
    box.hidden = false;
    box.className = 'probe';
    box.replaceChildren(el('p', { class: 'probe__head', text: t('ptz.testing') }));
    try {
      await api('api/cameras/' + camera.id + '/ptz', {
        method: 'POST',
        body: { action: 'stop', speed: collectPtz().speed },
      });
      box.className = 'probe probe--ok';
      box.replaceChildren(
        el('p', { class: 'probe__head', text: t('ptz.test_ok') }),
        el('p', { class: 'probe__hint', text: t('ptz.test_hint') }),
      );
    } catch (err) {
      box.className = 'probe probe--err';
      const code = err instanceof ApiError ? err.code : 'ptz_failed';
      box.replaceChildren(
        el('p', { class: 'probe__error', text: errorText(code) + errorDetail(err) }),
      );
    } finally {
      button.disabled = false;
    }
  }

  /* ------------------------------------------------- stream codec (ONVIF) */
  /* Home Assistant copies the stream of the camera into HLS without transcoding, so the
     codec inside decides whether a browser plays it. The encoders of the camera are read
     over ONVIF and the codec of one of them is switched - the panel needs no vendor app. */
  const ENCODER_CHOICES = ['H264', 'H265'];

  function resetEncoderBox() {
    const box = document.getElementById('encoder-result');
    box.hidden = true;
    box.replaceChildren();
    document.getElementById('encoder-state').textContent = t('encoder.state_none');
  }

  function encoderBody() {
    /* The HTTP address and the credentials of the PTZ section are the ONVIF account of
       the camera, so what the user typed there is sent along. The add-on falls back to
       the host and the credentials of the stream URL when nothing is typed. */
    const body = {};
    const base = document.getElementById('field-ptz-base').value.trim();
    const username = document.getElementById('field-ptz-username').value.trim();
    const password = document.getElementById('field-ptz-password').value;
    if (base) body.base_url = base;
    if (username) body.username = username;
    if (password) body.password = password;
    return body;
  }

  function encoderStateLabel(configurations) {
    const codecs = [];
    (configurations || []).forEach((item) => {
      if (item.encoding && codecs.indexOf(item.encoding) < 0) codecs.push(item.encoding);
    });
    return codecs.length ? codecs.join(' + ') : t('encoder.state_none');
  }

  function encoderPicture(item) {
    if (!item.width || !item.height) return '—';
    const picture = item.width + '×' + item.height;
    return item.fps ? picture + ' @ ' + item.fps + ' fps' : picture;
  }

  function encoderProfiles(report, item) {
    return (report.profiles || [])
      .filter((profile) => profile.encoder === item.token)
      .map((profile) => profile.name)
      .join(', ');
  }

  function encoderBackend(report) {
    /* Which service of the camera answered: ONVIF, or the vendor protocol of the
       Xiongmai family for a firmware that ships no media service at all. */
    return report.backend === 'xm' ? t('encoder.backend_xm') : t('encoder.backend_onvif');
  }

  function renderEncoders(box, camera, report) {
    const configurations = report.configurations || [];
    document.getElementById('encoder-state').textContent = encoderStateLabel(configurations);
    box.hidden = false;
    box.className = 'probe ' + (configurations.length ? 'probe--ok' : 'probe--err');
    const children = [
      el('p', {
        class: 'probe__head',
        text: configurations.length
          ? t('encoder.found', { count: configurations.length })
          : t('encoder.none'),
      }),
      el('p', {
        class: 'note note--tight',
        text: t('encoder.backend', { backend: encoderBackend(report) }),
      }),
    ];

    configurations.forEach((item) => {
      const pairs = [
        [t('encoder.token'), item.token],
        [t('camera.codec'), item.encoding || '—'],
        [t('camera.resolution'), encoderPicture(item)],
        [
          t('camera.bitrate'),
          item.bitrate ? t('camera.bitrate_value', { value: item.bitrate }) : '—',
        ],
        [t('encoder.gop'), item.gop ? String(item.gop) : '—'],
      ];
      if (item.name && item.name !== item.token) {
        pairs.unshift([t('encoder.name'), item.name]);
      }
      const used = encoderProfiles(report, item);
      const select = el(
        'select',
        { 'aria-label': t('encoder.set') },
        ENCODER_CHOICES.map((codec) => el('option', { value: codec, text: codec })),
      );
      select.value = ENCODER_CHOICES.indexOf(item.encoding) >= 0 ? item.encoding : 'H264';
      const button = el('button', { type: 'button', class: 'btn btn--ghost' }, [
        el('span', { text: t('encoder.set') }),
      ]);
      button.addEventListener('click', () => {
        applyEncoding(camera, item.token, select.value, button, report.backend);
      });

      children.push(
        el('div', { class: 'encoder' }, [
          el(
            'dl',
            { class: 'probe__grid' },
            pairs.flatMap(([label, value]) => [
              el('dt', { text: label }),
              el('dd', { text: value }),
            ]),
          ),
          used ? el('p', { class: 'encoder__used', text: t('encoder.used_by', { profiles: used }) }) : null,
          el('div', { class: 'encoder__actions' }, [select, button]),
        ]),
      );
    });
    box.replaceChildren(...children);
  }

  async function readEncoders() {
    const camera = state.editing ? cameraById(state.editing) : null;
    const box = document.getElementById('encoder-result');
    const button = document.getElementById('btn-encoder-read');
    if (!camera) {
      toast(t('camera.preview_save_first'), 'err');
      return;
    }
    button.disabled = true;
    box.hidden = false;
    box.className = 'probe';
    box.replaceChildren(el('p', { class: 'probe__head', text: t('encoder.reading') }));
    try {
      const data = await api('api/cameras/' + camera.id + '/encoder/inspect', {
        method: 'POST',
        body: encoderBody(),
      });
      renderEncoders(box, camera, data.encoder || {});
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'encoder_fault';
      document.getElementById('encoder-state').textContent = t('encoder.state_none');
      box.className = 'probe probe--err';
      box.replaceChildren(
        el('p', { class: 'probe__error', text: errorText(code) + errorDetail(err) }),
      );
    } finally {
      button.disabled = false;
    }
  }

  async function applyEncoding(camera, token, encoding, button, source) {
    const box = document.getElementById('encoder-result');
    button.disabled = true;
    try {
      const data = await api('api/cameras/' + camera.id + '/encoder', {
        method: 'POST',
        body: Object.assign(encoderBody(), { token, encoding, source }),
      });
      const report = data.encoder || {};
      renderEncoders(box, camera, report);
      toast(
        report.changed
          ? t('encoder.applied', { encoding })
          : t('encoder.unchanged', { encoding }),
        'ok',
      );
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'encoder_fault';
      box.hidden = false;
      box.className = 'probe probe--err';
      box.replaceChildren(
        el('p', { class: 'probe__error', text: errorText(code) + errorDetail(err) }),
      );
      toast(errorText(code), 'err');
    } finally {
      button.disabled = false;
    }
  }

  /* ------------------------------------------------- PTZ test mode (detection) */
  function hostFromUrl(value) {
    try {
      return new URL(String(value || '').trim()).hostname || '';
    } catch (err) {
      return '';
    }
  }

  /* The address the test mode asks: what the user typed, else the HTTP address of the
     camera, else the host of the RTSP URL - so the IP is usually already there. */
  function ptzTestAddress() {
    const typed = document.getElementById('field-ptz-test-ip').value.trim();
    if (typed) return typed;
    const base = document.getElementById('field-ptz-base').value.trim();
    if (base) return base;
    return hostFromUrl(document.getElementById('field-url').value.trim());
  }

  function ptzStatusText(status) {
    const key = 'ptz.status_' + String(status || '').replace(/[^a-z_]/g, '');
    const text = t(key);
    return text === key ? String(status || '') : text;
  }

  function renderPtzProbe(container, report, applied) {
    const winner = report && report.ok ? report : null;
    container.hidden = false;
    container.className = 'probe ' + (winner ? 'probe--ok' : 'probe--err');
    const head = winner
      ? t('ptz.detect_ok', { profile: report.label || report.profile })
      : report && report.profile
        ? t('ptz.detect_partial', { profile: report.label || report.profile })
        : t('ptz.detect_none');
    const children = [
      el('div', { class: 'probe__head' }, [
        led(winner ? 'ok' : 'err'),
        el('span', { text: head }),
      ]),
      el(
        'dl',
        { class: 'probe__grid' },
        (report.results || []).flatMap((item) => [
          el('dt', { text: item.label || item.profile }),
          el('dd', {
            text: ptzStatusText(item.status) + (item.detail ? ' — ' + item.detail : ''),
          }),
        ]),
      ),
    ];
    if (winner) {
      children.push(
        el('p', {
          class: 'probe__hint',
          text: applied ? t('ptz.detect_applied') : t('ptz.detect_save'),
        }),
      );
    } else if (report && report.profile) {
      children.push(el('p', { class: 'probe__hint', text: t('ptz.detect_partial_hint') }));
    }
    container.replaceChildren(...children);
  }

  /* The add-on already stored the winner as the main PTZ handling of the camera; the
     form follows, so a later "Save" cannot overwrite it again. */
  function adoptPtzProfile(report) {
    document.getElementById('field-ptz-enabled').checked = true;
    document.getElementById('field-ptz-profile').value = report.profile || 'custom';
    document.getElementById('field-ptz-base').value = report.base_url || ptzTestAddress();
    if (report.token) document.getElementById('field-ptz-token').value = report.token;
    const profile = ptzProfile();
    document.getElementById('ptz-profile-hint').textContent = (profile && profile.description) || '';
    fillPtzFromProfile(true);
  }

  async function detectPtz() {
    const camera = state.editing ? cameraById(state.editing) : null;
    const box = document.getElementById('ptz-detect-result');
    const button = document.getElementById('btn-ptz-detect');
    const address = ptzTestAddress();
    if (!address) {
      toast(errorText('ptz_base_url_required'), 'err');
      document.getElementById('field-ptz-test-ip').focus();
      return;
    }
    button.disabled = true;
    box.hidden = false;
    box.className = 'probe';
    box.replaceChildren(el('p', { class: 'probe__head', text: t('ptz.detecting') }));
    try {
      const data = await api('api/ptz/probe', {
        method: 'POST',
        body: {
          camera_id: camera ? camera.id : null,
          base_url: address,
          stream_url: document.getElementById('field-url').value.trim(),
          channel: Number(document.getElementById('field-ptz-channel').value) || undefined,
          speed: Number(document.getElementById('field-ptz-speed').value) || undefined,
          port: Number(document.getElementById('field-ptz-test-port').value) || undefined,
          username: document.getElementById('field-ptz-username').value.trim(),
          password: document.getElementById('field-ptz-password').value,
        },
      });
      const report = data.probe || {};
      renderPtzProbe(box, report, Boolean(data.applied));
      if (report.ok && report.profile) {
        adoptPtzProfile(report);
        toast(t('ptz.detect_ok', { profile: report.label || report.profile }), 'ok');
      }
      if (data.cameras) {
        state.cameras = data.cameras;
        renderCameras();
      } else if (data.camera && camera) {
        Object.assign(camera, data.camera);
        renderCameras();
      }
    } catch (err) {
      box.className = 'probe probe--err';
      const code = err instanceof ApiError ? err.code : 'generic';
      box.replaceChildren(
        el('p', { class: 'probe__error', text: errorText(code) + errorDetail(err) }),
      );
    } finally {
      button.disabled = false;
    }
  }

  /* The pad: directions are held (start on pointer down, stop on release), the
     other keys are single clicks - exactly how ONVIF PTZ works in Home Assistant. */
  function renderPtzPad(camera) {
    const pad = document.getElementById('ptz-pad');
    const ptz = (camera && camera.ptz) || null;
    if (!ptz || !ptz.enabled) {
      pad.hidden = true;
      return;
    }
    pad.hidden = false;
    document.getElementById('ptz-speed-vertical').value = ptz.speed_vertical || ptz.speed || 4;
    document.getElementById('ptz-speed-horizontal').value =
      ptz.speed_horizontal || ptz.speed || 4;
    document.getElementById('ptz-error').hidden = true;
    const enabled = ptz.actions || [];
    document.querySelectorAll('#ptz-pad [data-ptz]').forEach((button) => {
      const action = button.dataset.ptz;
      button.disabled = action !== 'stop' && enabled.indexOf(action) < 0;
    });
    const select = document.getElementById('ptz-preset-select');
    const presets = ptz.presets || [];
    select.replaceChildren(
      ...(presets.length
        ? presets.map((preset) => el('option', { value: preset.id, text: preset.name }))
        : [el('option', { value: '', text: t('ptz.no_presets') })]),
    );
    select.disabled = presets.length === 0;
    document.getElementById('btn-ptz-preset').disabled = presets.length === 0;
    renderPtzSteps(ptz);
    renderPtzPosition(camera.ptz_position);
    renderPtzMirror(ptz);
  }

  // Which speed field belongs to the arrows of an axis: the tilt (up/down) and the pan
  // (left/right) can be tuned on their own. Zoom and presets use the general speed of
  // the camera.
  const PTZ_AXIS_SPEED = {
    up: 'vertical',
    down: 'vertical',
    left: 'horizontal',
    right: 'horizontal',
  };

  // The arrows a mirror flag of the camera turns around. A camera on a ceiling bracket, on
  // its side or behind a mirror moves the other way, so the pad offers a button per axis
  // that turns its arrows around; the flag lives in the PTZ block of the camera, so the
  // entity of Home Assistant and every automation follow it as well.
  const PTZ_MIRROR_AXES = {
    horizontal: ['left', 'right'],
    vertical: ['up', 'down'],
  };

  function renderPtzMirror(ptz) {
    const actions = (ptz && ptz.actions) || [];
    Object.keys(PTZ_MIRROR_AXES).forEach((axis) => {
      const button = document.getElementById('btn-ptz-invert-' + axis);
      button.setAttribute('aria-pressed', ptz && ptz['invert_' + axis] ? 'true' : 'false');
      button.disabled = !PTZ_MIRROR_AXES[axis].some(
        (action) => actions.indexOf(action) >= 0,
      );
    });
  }

  async function sendPtz(action, extra) {
    const cameraId = state.preview.cameraId;
    if (!cameraId) return false;
    const axis = PTZ_AXIS_SPEED[action];
    const field = axis ? document.getElementById('ptz-speed-' + axis) : null;
    const speed = field ? Number(field.value) : 0;
    const body = Object.assign({ action }, extra || {});
    if (speed) body['speed_' + axis] = speed;
    try {
      const data = await api('api/cameras/' + cameraId + '/ptz', { method: 'POST', body });
      state.ptzDirection = action === 'stop' ? null : action;
      document.getElementById('ptz-error').hidden = true;
      // The answer of every move carries the coordinates of the camera afterwards: a preset
      // that is a rung keeps them, a continuous move, a stop and the home key clear them.
      if (data && Object.prototype.hasOwnProperty.call(data, 'position')) {
        renderPtzPosition(data.position || null);
      }
      return true;
    } catch (err) {
      showPtzError(err);
      return false;
    }
  }

  function stopPtz() {
    const direction = state.ptzDirection;
    state.ptzDirection = null;
    sendPtz('stop', direction ? { direction } : null);
  }

  /* ----------------------------------------------------------------- steps */
  /* A step by step arrow needs a *position*. This family drives an axis to its limit and
     nothing halts it on the way, so the only bounded move it has is a ``GotoPreset`` of a
     preset slot a position was written into before - a rung of a ladder. The panel offers
     that ladder: "store the position" writes the place the camera stands at into the next
     free slot, a short press of an arrow walks one rung (the number in the step field),
     and a press that stays is the continuous move it always was. */

  //: How long an arrow has to be held to count as a move instead of a step.
  const STEP_TAP_MS = 220;
  //: The pause between two rungs of a held arrow, on top of the time the device needs to
  //: answer the last one (a ``GotoPreset`` is answered when the camera has arrived). It is
  //: only used by an axis whose stop has to drag the camera back (see :func:`walksTheLadder`).
  const STEP_WALK_MS = 120;

  function stepRungs(action, camera) {
    const axis = PTZ_AXIS_SPEED[action];
    if (!axis || !camera || !camera.ptz) return [];
    return ((camera.ptz.steps || {})[axis]) || [];
  }

  function renderPtzSteps(ptz) {
    const steps = (ptz && ptz.steps) || {};
    const vertical = (steps.vertical || []).length;
    const horizontal = (steps.horizontal || []).length;
    const canStore = ((ptz && ptz.actions) || []).indexOf('store') >= 0;
    document.getElementById('btn-ptz-step-vertical').disabled = !canStore;
    document.getElementById('btn-ptz-step-horizontal').disabled = !canStore;
    document.getElementById('btn-ptz-step-clear').disabled = !vertical && !horizontal;
    const note = document.getElementById('ptz-steps-note');
    // A camera whose stop has to drag the axis back walks the ladder during a hold instead of
    // starting a move it cannot end, and the note says so (see ``walksTheLadder``).
    const walkKey =
      ptz && ptz.stops_in_place === false ? 'ptz.steps_note_walk' : 'ptz.steps_note';
    note.textContent = vertical || horizontal
      ? t(walkKey, { vertical, horizontal })
      : t('ptz.steps_empty');
  }

  function stepsPerPress() {
    const value = Number(document.getElementById('ptz-steps-per-press').value);
    return Math.max(1, Math.min(10, Math.round(value) || 1));
  }

  /* The coordinates of the camera on its two ladders - where it stands right now, per axis.
     The add-on keeps them for every move: a step writes the rung it reached *and* the
     crossing rung the other axis is carried to (a rung is a whole position, so it always
     rewrites both), a preset that is a rung does the same, and a continuous move, a stop or
     the home key leaves the camera between two rungs and clears them (see
     ``ptz.rung_position``). Showing them is what makes the model visible: a step of one axis
     that changes the other number as well is exactly the drag the toast reports. */
  function renderPtzPosition(position) {
    const box = document.getElementById('ptz-position');
    const ladders = position || null;
    const axes = ladders ? Object.keys(ladders) : [];
    if (!axes.length) {
      box.hidden = true;
      box.textContent = '';
      return;
    }
    const rung = (axis) => {
      const values = ladders[axis] || {};
      return typeof values.index === 'number'
        ? values.index + 1 + '/' + (values.rungs || 0)
        : '–';
    };
    const known = axes.some((axis) => typeof (ladders[axis] || {}).index === 'number');
    box.hidden = false;
    box.textContent = known
      ? t('ptz.position', { vertical: rung('vertical'), horizontal: rung('horizontal') })
      : t('ptz.position_unknown');
  }

  function showPtzError(err) {
    const box = document.getElementById('ptz-error');
    box.hidden = false;
    const code = err instanceof ApiError ? err.code : 'ptz_failed';
    box.textContent = errorText(code) + errorDetail(err);
  }

  async function sendStep(direction, options) {
    /* Walk one step of the ladder of an axis and return the rung the camera reached (``null``
       when the request failed). ``options.quiet`` keeps the toast of a single step out of a
       *walk*: a held arrow walks many rungs, and a message per rung would cover the picture. */
    const quiet = Boolean(options && options.quiet);
    const cameraId = state.preview.cameraId;
    if (!cameraId) return null;
    try {
      const data = await api('api/cameras/' + cameraId + '/ptz', {
        method: 'POST',
        body: { action: 'step', direction, steps: stepsPerPress() },
      });
      const step = data.step || {};
      document.getElementById('ptz-error').hidden = true;
      // Where the camera stands now, on both ladders: a step rewrites the rung it reached and
      // the other coordinate with it (see ``renderPtzPosition``).
      renderPtzPosition(step.position || null);
      /* A rung is a whole position, and this camera has no bounded *relative* move: a step
         that switches axes therefore carries the other axis back to the crossing of the two
         ladders. The add-on reports that drag instead of refusing the step (see
         ``ptz.step_drag``), so the notice is shown even during a walk - the first rung of a
         walk is the only one that can drag. */
      const drag = step.drag || null;
      if (drag) {
        toast(
          t('ptz.step_dragged', {
            other: t('ptz.axis_' + drag.axis),
            rungs: Math.abs(drag.rungs || 0),
          }),
          'warn',
        );
      }
      if (!quiet) {
        toast(
          t('ptz.step_reached', { index: (step.index || 0) + 1, rungs: step.rungs || 0 }),
          'ok',
        );
      }
      return step;
    } catch (err) {
      showPtzError(err);
      return null;
    }
  }

  /* A hold on an axis whose stop has to *drag* the camera back must not start a move at all.
     This is the Xiongmai DVRIP family: every direction command drives the axis to its limit
     and no payload of the family halts one on the way (``.smoke/dvrip-protocol.md``, and the
     stop of profile ``xiongmai_dvrip`` is a capture for that reason), so a hold there walks
     the rungs of the ladder - one bounded preset move at a time - and a release ends the walk
     at the rung the camera is on. Every other camera keeps the continuous move and its stop. */
  function walksTheLadder(action, camera) {
    if (!camera || !camera.ptz) return false;
    return camera.ptz.stops_in_place === false && stepRungs(action, camera).length > 0;
  }

  async function walkSteps(action, held) {
    let index = null;
    while (held()) {
      const started = Date.now();
      // The promise of the request settles when the device has *arrived*, so a walk paces
      // itself and never queues a rung behind the one the camera is travelling to.
      const step = await sendStep(action, { quiet: true });
      if (!step) return;
      // The end of the ladder: the request is clamped to the last rung and the camera does
      // not move any more, so the walk ends instead of asking for the same slot again.
      if (index !== null && step.index === index) return;
      index = step.index;
      const pause = STEP_WALK_MS - (Date.now() - started);
      if (pause > 0) await new Promise((done) => window.setTimeout(done, pause));
    }
  }

  async function storeStep(axis) {
    const cameraId = state.preview.cameraId;
    if (!cameraId) return;
    try {
      await api('api/cameras/' + cameraId + '/ptz/steps', {
        method: 'POST',
        body: { axis, mode: 'add' },
      });
      await reload();
      renderPtzPad(cameraById(cameraId));
      toast(t('ptz.step_stored'), 'ok');
    } catch (err) {
      showPtzError(err);
    }
  }

  /* Turn the arrows of an axis around: a camera that is mounted the other way round moves
     the other way, and the flag belongs to the camera, so the panel, the entity of Home
     Assistant and every automation read the arrows the way the picture shows them. */
  async function toggleInvert(axis) {
    const cameraId = state.preview.cameraId;
    if (!cameraId) return;
    try {
      const data = await api('api/cameras/' + cameraId + '/ptz/invert', {
        method: 'POST',
        body: { axis },
      });
      const camera = cameraById(cameraId);
      if (camera && data.camera) Object.assign(camera, data.camera);
      renderPtzPad(camera);
      toast(
        t(data.inverted ? 'ptz.invert_on' : 'ptz.invert_off', {
          axis: t('ptz.axis_' + axis),
        }),
        'ok',
      );
    } catch (err) {
      showPtzError(err);
    }
  }

  async function clearSteps() {
    const cameraId = state.preview.cameraId;
    if (!cameraId) return;
    try {
      for (const axis of ['vertical', 'horizontal']) {
        await api('api/cameras/' + cameraId + '/ptz/steps', {
          method: 'POST',
          body: { axis, mode: 'clear' },
        });
      }
      await reload();
      renderPtzPad(cameraById(cameraId));
    } catch (err) {
      showPtzError(err);
    }
  }



  /* ---------------------------------------------------------------- preview */
  function openPreview(cameraId) {
    const camera = cameraById(cameraId);
    if (!camera) return;
    state.preview.cameraId = cameraId;
    document.getElementById('preview-title').textContent = t('preview.title', { name: camera.name });
    document.getElementById('preview-mode-select').value =
      state.preview.mode || state.settings.preview_mode || 'auto';
    renderPtzPad(camera);
    showModal('preview-modal');
    startPreview();
  }

  function mjpegBlocked() {
    if (state.preview.mjpegBlockedAt) {
      if (Date.now() - state.preview.mjpegBlockedAt < MJPEG_FLAG_TTL_MS) return true;
      state.preview.mjpegBlockedAt = 0;
    }
    try {
      const stored = window.localStorage.getItem(MJPEG_FLAG_KEY);
      if (!stored) return false;
      const at = Number.parseInt(stored, 10);
      if (Number.isFinite(at) && Date.now() - at < MJPEG_FLAG_TTL_MS) return true;
      // An expired note (or the older plain "1") must not decide the next preview.
      window.localStorage.removeItem(MJPEG_FLAG_KEY);
      return false;
    } catch (err) {
      return false;
    }
  }

  function rememberMjpegBlocked() {
    state.preview.mjpegBlockedAt = Date.now();
    try {
      window.localStorage.setItem(MJPEG_FLAG_KEY, String(state.preview.mjpegBlockedAt));
    } catch (err) {
      /* the flag is optional */
    }
  }

  function placeholderText(text) {
    const node = document.getElementById('preview-placeholder');
    node.textContent = text;
    node.hidden = false;
  }

  function resetStage() {
    if (state.preview.timer) {
      window.clearTimeout(state.preview.timer);
      state.preview.timer = null;
    }
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
    state.ptzDirection = null;
    document.getElementById('ptz-pad').hidden = true;
    resetStage();
  }

  function previewError(message) {
    document.getElementById('preview-placeholder').hidden = true;
    const box = document.getElementById('preview-error');
    box.hidden = false;
    box.textContent = message;
  }

  async function reportPreviewError(url, fallback) {
    let message = fallback;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(url, {
        signal: controller.signal,
        headers: { Accept: 'application/json' },
      });
      const data = await response.json();
      if (data && data.detail) {
        message = errorText(data.error || 'stream_failed') + ' — ' + data.detail;
      } else if (data && data.error) {
        message = errorText(data.error);
      }
    } catch (err) {
      /* keep the fallback message */
    } finally {
      window.clearTimeout(timer);
    }
    previewError(message);
  }

  function startPreview(force) {
    const cameraId = state.preview.cameraId;
    if (!cameraId) return;
    const mode = document.getElementById('preview-mode-select').value || 'auto';
    state.preview.mode = mode;
    resetStage();
    if (mode === 'auto') {
      startAutoPreview(cameraId, force);
      return;
    }
    playPreview(cameraId, mode);
  }

  /* Automatic mode: ask the add-on which implementation works for this camera
     and use only that one from then on. */
  async function startAutoPreview(cameraId, force) {
    document.getElementById('preview-chip').textContent = t('preview.mode_auto');
    if (mjpegBlocked() && !force) {
      playPreview(cameraId, 'hls', 'auto');
      return;
    }
    let mode = force ? null : state.preview.resolved[cameraId];
    if (!mode) {
      const camera = cameraById(cameraId) || {};
      const codec = String(probeDetails(camera).codec || '').toLowerCase();
      const slow = codec !== '' && ['h264', 'avc1', 'mjpeg'].indexOf(codec) < 0;
      placeholderText(slow ? t('preview.detecting_slow') : t('preview.detecting'));
      try {
        const data = await api(
          'api/cameras/' + cameraId + '/preview/detect' + (force ? '?force=1' : ''),
          { method: 'POST' },
        );
        mode = data.mode || 'mjpeg';
        state.preview.resolved[cameraId] = mode;
        applyCamera(data.camera);
        if (data.transport && camera.rtsp_transport && data.transport !== camera.rtsp_transport) {
          toast(t('preview.transport_fallback', { transport: data.transport }), 'warn');
        }
      } catch (err) {
        const code = err instanceof ApiError ? err.code : 'generic';
        const detail =
          err instanceof ApiError && err.data && err.data.detail ? ' — ' + err.data.detail : '';
        previewError(errorText(code) + detail);
        return;
      }
    }
    playPreview(cameraId, mode, 'auto');
  }

  function playPreview(cameraId, mode, source) {
    const chip = document.getElementById('preview-chip');
    chip.textContent = source === 'auto' ? t('preview.mode_auto_result', { mode }) : mode;
    resetStage();
    placeholderText(t('preview.loading'));

    const image = document.getElementById('preview-image');
    if (mode === 'hls') {
      startHlsPreview(cameraId);
      return;
    }

    let received = false;
    if (source === 'auto') {
      state.preview.timer = window.setTimeout(() => {
        state.preview.timer = null;
        if (received) return;
        // ffmpeg did produce frames, but they never arrived in this browser.
        rememberMjpegBlocked();
        playPreview(cameraId, 'hls', 'auto');
      }, PREVIEW_FIRST_FRAME_MS);
    }
    image.onload = () => {
      received = true;
      if (state.preview.timer) {
        window.clearTimeout(state.preview.timer);
        state.preview.timer = null;
      }
      document.getElementById('preview-placeholder').hidden = true;
      image.hidden = false;
    };
    image.onerror = () => {
      if (state.preview.timer) {
        window.clearTimeout(state.preview.timer);
        state.preview.timer = null;
      }
      if (source === 'auto') {
        rememberMjpegBlocked();
        playPreview(cameraId, 'hls', 'auto');
        return;
      }
      reportPreviewError(
        apiUrl('api/cameras/' + cameraId + '/mjpeg'),
        errorText('stream_failed'),
      );
    };
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

  /* --------------------------------------------------------- add-on updates */
  async function refreshAddonUpdate() {
    try {
      const data = await api('api/addon/update');
      if (data.addon) state.addonUpdate = Object.assign({}, state.addonUpdate, data.addon);
    } catch (err) {
      /* the add-on version is informative only */
    }
    renderAddonState();
  }

  async function checkAddonUpdate(node) {
    if (node) node.disabled = true;
    try {
      const data = await api('api/addon/update/check', { method: 'POST' });
      if (data.addon) state.addonUpdate = Object.assign({}, state.addonUpdate, data.addon);
      state.addonUpdate.checked_at = new Date().toISOString();
      renderAddonState();
      if (!state.addonUpdate.available) {
        toast(
          state.addonUpdate.hint === 'token_missing'
            ? t('addon_update.token_missing')
            : state.addonUpdate.error
              ? t('addon_update.failed', { reason: state.addonUpdate.error })
              : t('addon_update.no_supervisor'),
          'err',
        );
      } else if (state.addonUpdate.update_available) {
        toast(t('addon_update.available', { version: state.addonUpdate.version_latest }), 'warn');
      } else {
        toast(t('addon_update.up_to_date'), 'ok');
      }
    } catch (err) {
      const code = err instanceof ApiError ? err.code : 'generic';
      if (code === 'supervisor_missing' && state.addonUpdate.hint === 'token_missing') {
        toast(t('addon_update.token_missing'), 'err');
        return;
      }
      fail(err);
    } finally {
      if (node) node.disabled = false;
    }
  }

  async function installAddonUpdate(node) {
    if (node) node.disabled = true;
    try {
      const data = await api('api/addon/update/install', { method: 'POST' });
      state.addonUpdate.busy = true;
      state.integration = Object.assign({}, state.integration, { needs_restart: true });
      renderAddonState();
      toast(
        data.via === 'home_assistant'
          ? t('addon_update.queued')
          : t('addon_update.updating'),
        'ok',
      );
      waitForAddonRestart();
    } catch (err) {
      fail(err);
      if (node) node.disabled = false;
    }
  }

  /* The container restarts during the update, so poll until it answers again. */
  function waitForAddonRestart() {
    let attempts = 0;
    const poll = () => {
      attempts += 1;
      window.setTimeout(async () => {
        try {
          const data = await api('api/addon/update');
          if (data.addon) {
            state.addonUpdate = Object.assign({}, state.addonUpdate, data.addon, { busy: false });
            renderAddonState();
            toast(t('addon_update.updated', { version: data.addon.version }), 'ok');
          }
        } catch (err) {
          if (attempts < 40) poll();
        }
      }, attempts === 0 ? 1000 : 5000);
    };
    poll();
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
    document.getElementById('btn-ptz-fill').addEventListener('click', () => fillPtzFromProfile());
    document.getElementById('btn-ptz-onvif').addEventListener('click', discoverOnvif);
  document.getElementById('btn-encoder-read').addEventListener('click', readEncoders);
    document.getElementById('btn-ptz-test').addEventListener('click', testPtz);
    document.getElementById('btn-ptz-detect').addEventListener('click', detectPtz);
    document.getElementById('field-ptz-profile').addEventListener('change', () => {
      /* Picking a vendor preset has to change the commands as well: a user who chooses
         another vendor expects its command set in the form, not only a new hint line. */
      fillPtzFromProfile();
    });
    document.querySelectorAll('#ptz-pad [data-ptz]').forEach((button) => {
      const action = button.dataset.ptz;
      if (button.dataset.ptzHold) {
        /* A short press walks one step of the ladder of this axis - a bounded move to a
           position that was stored before. A press that stays is the continuous move, unless
           the stop of the camera cannot halt an axis: then it walks the ladder rung by rung
           for as long as the arrow is held (see ``walksTheLadder``). The delay is there only
           for an axis that really has rungs. */
        let timer = null;
        let moving = false;
        const release = () => {
          button.classList.remove('is-active');
          if (timer !== null) {
            window.clearTimeout(timer);
            timer = null;
            if (!moving) sendStep(action);
            return;
          }
          if (moving) stopPtz();
        };
        button.addEventListener('pointerdown', (event) => {
          event.preventDefault();
          button.classList.add('is-active');
          moving = false;
          const camera = state.preview.cameraId ? cameraById(state.preview.cameraId) : null;
          if (stepRungs(action, camera).length) {
            timer = window.setTimeout(() => {
              timer = null;
              if (walksTheLadder(action, camera)) {
                /* The walk keeps the button lit while the arrow is down and ends when the
                   class is gone, so a release ends it at the rung the camera is on. */
                walkSteps(action, () => button.classList.contains('is-active')).catch((err) =>
                  showPtzError(err),
                );
                return;
              }
              moving = true;
              sendPtz(action);
            }, STEP_TAP_MS);
          } else {
            moving = true;
            sendPtz(action);
          }
        });
        ['pointerup', 'pointerleave', 'pointercancel'].forEach((name) =>
          button.addEventListener(name, release),
        );
      } else {
        button.addEventListener('click', () => sendPtz(action));
      }
    });
    document.getElementById('btn-ptz-step-vertical').addEventListener('click', () => {
      storeStep('vertical');
    });
    document.getElementById('btn-ptz-step-horizontal').addEventListener('click', () => {
      storeStep('horizontal');
    });
    document.getElementById('btn-ptz-step-clear').addEventListener('click', () => {
      clearSteps();
    });
    Object.keys(PTZ_MIRROR_AXES).forEach((axis) => {
      document.getElementById('btn-ptz-invert-' + axis).addEventListener('click', () => {
        toggleInvert(axis);
      });
    });
    document.getElementById('btn-ptz-preset').addEventListener('click', () => {
      const preset = document.getElementById('ptz-preset-select').value;
      if (preset) sendPtz('preset', { preset });
    });
    document.getElementById('btn-preview-form').addEventListener('click', () => {
      const camera = state.editing ? cameraById(state.editing) : null;
      if (!camera) {
        toast(t('camera.preview_save_first'), 'err');
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
    document.getElementById('preview-mode-select').addEventListener('change', () => startPreview(true));
    document.getElementById('btn-addon-version').addEventListener('click', () => {
      selectPanel('settings');
      checkAddonUpdate();
    });
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
    refreshAddonUpdate();
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
