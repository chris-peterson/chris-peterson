/*!
 * chris-peterson/shared-theme v1.2
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

    // Theme toggle (icon button)
    var btn = document.createElement('button');
    btn.className = 'cp-theme-toggle';
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Toggle dark mode');
    btn.innerHTML =
      '<svg class="cp-theme-toggle__sun" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">' +
        '<path d="M12 18a6 6 0 1 1 0-12 6 6 0 0 1 0 12zm0-2a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM11 1h2v3h-2V1zm0 19h2v3h-2v-3zM3.515 4.929l1.414-1.414L7.05 5.636 5.636 7.05 3.515 4.93zM16.95 18.364l1.414-1.414 2.121 2.121-1.414 1.414-2.121-2.121zm2.121-14.85l1.414 1.415-2.121 2.121-1.414-1.414 2.121-2.121zM5.636 16.95l1.414 1.414-2.121 2.121-1.414-1.414 2.121-2.121zM23 11v2h-3v-2h3zM4 11v2H1v-2h3z"/>' +
      '</svg>' +
      '<svg class="cp-theme-toggle__moon" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">' +
        '<path d="M10 7a7 7 0 0 0 12 4.9v.1c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2h.1A6.979 6.979 0 0 0 10 7zm-6 5a8 8 0 0 0 15.062 3.762A9 9 0 0 1 8.238 4.938 7.999 7.999 0 0 0 4 12z"/>' +
      '</svg>';
    btn.addEventListener('click', function () { toggleTheme(); });
    header.appendChild(btn);

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
