// Theme bootstrap: runs in <head> so the first paint already uses the chosen theme.
(() => {
  const KEY = 'sleepwell-theme';
  let choice = 'system';
  try { choice = localStorage.getItem(KEY) || 'system'; } catch { /* storage unavailable */ }
  const apply = value => {
    if (value === 'light' || value === 'dark') document.documentElement.dataset.theme = value;
    else delete document.documentElement.dataset.theme;
  };
  apply(choice);
  window.sleepwellTheme = {
    get: () => choice,
    set: value => {
      choice = ['light', 'dark'].includes(value) ? value : 'system';
      try { localStorage.setItem(KEY, choice); } catch { /* keep in-memory choice */ }
      apply(choice);
      document.dispatchEvent(new CustomEvent('sleepwell:theme', {detail: {theme: choice}}));
    },
  };
})();
