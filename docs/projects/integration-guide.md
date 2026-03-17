# Shared Theme Integration Guide

Add the shared header, dark/light toggle, and Dracula-inspired theme to any docsify site.

## Quick Start

In your docsify site's `index.html`, add these two lines to `<head>` (after the docsify base theme):

```html
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<link rel="stylesheet" href="https://chris-peterson.github.io/chris-peterson/shared/theme.css">
```

Then before your docsify `<script>` tags, add:

```html
<script src="https://chris-peterson.github.io/chris-peterson/shared/theme.js"></script>
<script>
  CPTheme.init({
    activeRepo: 'pwsh-gitlab'  // matches the tab name to highlight
  });
</script>
```

## Full Example (pwsh-gitlab)

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>pwsh-gitlab</title>

  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">

  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/docsify@4/lib/themes/vue.css">
  <link rel="stylesheet" href="https://chris-peterson.github.io/chris-peterson/shared/theme.css">
</head>
<body>
  <div id="app">Loading...</div>

  <script src="https://chris-peterson.github.io/chris-peterson/shared/theme.js"></script>
  <script>
    CPTheme.init({
      activeRepo: 'pwsh-gitlab'
    });
  </script>

  <script>
    window.$docsify = {
      name: 'pwsh-gitlab',
      // ... your existing config
    };
  </script>

  <script src="https://cdn.jsdelivr.net/npm/docsify@4/lib/docsify.min.js"></script>
</body>
</html>
```

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `activeRepo` | `''` | Tab name to highlight (e.g. `'pwsh-gitlab'`) |
| `brand` | `'Chris Peterson'` | Brand text in header |
| `brandUrl` | `'https://chris-peterson.github.io/chris-peterson/'` | Brand link |
| `repos` | See below | Custom tab list |

### Default Tabs

```js
[
  { name: 'Home',        url: 'https://chris-peterson.github.io/chris-peterson/' },
  { name: 'pwsh-gitlab', url: 'https://chris-peterson.github.io/pwsh-gitlab/' }
]
```

## API

```js
CPTheme.toggle()         // Toggle dark/light
CPTheme.apply('dark')    // Force a theme
CPTheme.getTheme()       // Get current resolved theme
```

## Adding a New Repo Tab

Edit the `DEFAULT_REPOS` array at the top of `shared/theme.js`:

```js
var DEFAULT_REPOS = [
  { name: 'Home',         url: 'https://chris-peterson.github.io/chris-peterson/' },
  { name: 'pwsh-gitlab',  url: 'https://chris-peterson.github.io/pwsh-gitlab/' },
  { name: 'new-project',  url: 'https://chris-peterson.github.io/new-project/' }
];
```

All sites that consume the shared JS will automatically show the new tab.
