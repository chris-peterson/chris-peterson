/*!
 * chris-peterson/shared-theme v1.0
 * Dark/light theme engine + header with repo tabs
 *
 * Usage:
 *   <script src="https://chris-peterson.github.io/chris-peterson/shared/theme.js"></script>
 *   <script>
 *     CPTheme.init({
 *       activeRepo: 'pwsh-gitlab',      // highlight this tab
 *       brand:      'Chris Peterson',    // header brand text (optional)
 *       brandUrl:   'https://chris-peterson.github.io/chris-peterson/', // (optional)
 *       repos: [                         // override default tabs (optional)
 *         { name: 'pwsh-gitlab', url: 'https://chris-peterson.github.io/pwsh-gitlab/' },
 *         ...
 *       ]
 *     });
 *   </script>
 */

(function (root) {
  'use strict';

  /* ---------------------------------------------------------------
     Default repo list — edit here to add/remove global tabs
     --------------------------------------------------------------- */
  var DEFAULT_REPOS = [
    { name: 'Home',         url: 'https://chris-peterson.github.io/chris-peterson/' },
    { name: 'pwsh-gitlab',  url: 'https://chris-peterson.github.io/pwsh-gitlab/' }
  ];

  var DEFAULT_BRAND     = 'Chris Peterson';
  var DEFAULT_BRAND_URL = 'https://chris-peterson.github.io/chris-peterson/';

  /* ---------------------------------------------------------------
     Theme engine
     --------------------------------------------------------------- */
  function getPreferredTheme() {
    var saved = localStorage.getItem('cp-theme');
    if (saved === 'dark' || saved === 'light') return saved;
    if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark';
    return 'light';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    // Update all toggle controls on the page
    var checks = document.querySelectorAll('.cp-theme-check input');
    for (var i = 0; i < checks.length; i++) {
      checks[i].checked = (theme === 'dark');
    }
    var pills = document.querySelectorAll('.cp-theme-pill');
    for (var j = 0; j < pills.length; j++) {
      var icon = pills[j].querySelector('.cp-theme-pill__icon');
      if (icon) icon.textContent = theme === 'dark' ? '\u2600\uFE0F' : '\uD83C\uDF19';
    }
  }

  function toggleTheme() {
    var current = document.documentElement.getAttribute('data-theme') || 'light';
    var next = current === 'dark' ? 'light' : 'dark';
    localStorage.setItem('cp-theme', next);
    applyTheme(next);
  }

  // Apply theme immediately (before DOM ready) to prevent flash
  applyTheme(getPreferredTheme());

  // React to OS-level changes when user hasn't manually chosen
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function (e) {
      if (!localStorage.getItem('cp-theme')) {
        applyTheme(e.matches ? 'dark' : 'light');
      }
    });
  }

  /* ---------------------------------------------------------------
     Build header DOM
     --------------------------------------------------------------- */
  function buildHeader(opts) {
    var repos     = opts.repos     || DEFAULT_REPOS;
    var brand     = opts.brand     || DEFAULT_BRAND;
    var brandUrl  = opts.brandUrl  || DEFAULT_BRAND_URL;
    var activeRepo = (opts.activeRepo || '').toLowerCase();

    var header = document.createElement('header');
    header.className = 'cp-header';
    header.setAttribute('role', 'banner');

    // Brand
    var a = document.createElement('a');
    a.className = 'cp-header__brand';
    a.href = brandUrl;
    a.textContent = brand;
    header.appendChild(a);

    // Tabs
    var nav = document.createElement('nav');
    nav.className = 'cp-tabs';
    nav.setAttribute('role', 'tablist');

    for (var i = 0; i < repos.length; i++) {
      var repo = repos[i];
      var tab = document.createElement('a');
      tab.className = 'cp-tab';
      tab.setAttribute('role', 'tab');
      tab.href = repo.url;
      tab.textContent = repo.name;

      var isActive = repo.name.toLowerCase() === activeRepo;
      tab.setAttribute('aria-selected', isActive ? 'true' : 'false');

      if (!isActive) {
        tab.addEventListener('click', makeTabHandler(nav, tab, repo.url));
      }
      nav.appendChild(tab);
    }

    header.appendChild(nav);

    // Spacer
    var spacer = document.createElement('div');
    spacer.className = 'cp-header__spacer';
    header.appendChild(spacer);

    // Theme toggle (checkbox style)
    var label = document.createElement('label');
    label.className = 'cp-theme-check';

    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = (getPreferredTheme() === 'dark');
    cb.setAttribute('aria-label', 'Toggle dark mode');
    cb.addEventListener('change', function () { toggleTheme(); });

    var span = document.createElement('span');
    span.className = 'cp-theme-check__label';
    span.textContent = 'Dark mode';

    label.appendChild(cb);
    label.appendChild(span);
    header.appendChild(label);

    return header;
  }

  /* ---------------------------------------------------------------
     Tab click handler — animate, then navigate
     --------------------------------------------------------------- */
  function makeTabHandler(nav, clickedTab, url) {
    return function (e) {
      e.preventDefault();

      // Deselect all
      var tabs = nav.querySelectorAll('.cp-tab');
      for (var i = 0; i < tabs.length; i++) {
        tabs[i].setAttribute('aria-selected', 'false');
      }

      // Select clicked
      clickedTab.setAttribute('aria-selected', 'true');

      // Trigger anchor-left animation
      nav.classList.add('cp-tabs--animating');
      clickedTab.addEventListener('animationend', function handler() {
        clickedTab.removeEventListener('animationend', handler);
        nav.classList.remove('cp-tabs--animating');
        // Navigate after animation completes
        window.location.href = url;
      });
    };
  }

  /* ---------------------------------------------------------------
     Public API
     --------------------------------------------------------------- */
  function init(opts) {
    opts = opts || {};

    function inject() {
      var header = buildHeader(opts);
      document.body.insertBefore(header, document.body.firstChild);
      document.body.classList.add('cp-has-header');
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', inject);
    } else {
      inject();
    }
  }

  root.CPTheme = {
    init:        init,
    toggle:      toggleTheme,
    apply:       applyTheme,
    getTheme:    getPreferredTheme
  };

})(window);
